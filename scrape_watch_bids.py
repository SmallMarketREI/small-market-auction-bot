#!/usr/bin/env python3
"""Daily job: track every current and recently-tracked Joe R. Pyle real
estate auction through to a final outcome, instead of just mirroring
"what's active right now."

Discovery: joerpyleauctions.com's homepage (see bidwrangler.ROOT_BASE) embeds
every currently-listed auction id in one page load -- real estate and
personal property mixed. Every id found there, PLUS every id already sitting
in watch_auctions (so a row we're tracking is always re-checked even if it
falls out of today's homepage listing -- see resolve_one()), gets its detail
fetched from BidWrangler directly and is resolved to exactly one outcome:

    active      -- still accepting bids. Upserted into watch_auctions with
                   final_status='Active', current bid info refreshed.
    sold        -- BidWrangler's own item.status says "sold". Written straight
                   to past_auctions (same field mapping /results-driven sales
                   use) and removed from watch_auctions -- it graduates.
    unsold      -- item.status says "no_sale": closed with no buyer.
    postponed   -- the auctioneer edited the title/description to say so.
    cancelled   -- same, for "cancelled".
    needs_review-- auction is gone (404), or closed with an outcome we can't
                   read cleanly. We do NOT delete these -- an auction that
                   disappears from the live listing without a confirmed sale
                   stays in watch_auctions and gets flagged, rather than
                   silently vanishing from the dashboard.

Cancelled/Postponed/Unsold/Needs-Review rows are never deleted -- they stay
in Auction Watch AND are what the dashboard's "Unsold Opportunities" section
collects (a closed-without-a-sale property is a lead worth following up on).
Nothing in this table is ever wiped by an exclusion filter the way it used to
be; a row only ever leaves watch_auctions by graduating to Sold.
"""
import datetime
import sys

import requests

from common import bidwrangler, parsing_utils, supabase_client

TERMINAL_STATUSES = {"Unsold", "Cancelled", "Postponed", "Needs Review"}


def _closing_label(scheduled_end_time_raw, now_utc):
    if not scheduled_end_time_raw:
        return "Upcoming"
    try:
        end_dt = datetime.datetime.fromisoformat(scheduled_end_time_raw.replace("Z", "+00:00"))
        if (end_dt - now_utc) <= datetime.timedelta(hours=72):
            return "Closing"
    except ValueError:
        pass
    return "Upcoming"


def build_active_row(s: dict, now_utc: datetime.datetime, checked_at: str) -> dict:
    district, map_, parcel = parsing_utils.parse_tax_reference(s["description"])

    premium_rate = 0.10  # Pyle's standard 10% buyer's premium, seen on every listing
                          # description during recon; override per-listing if a
                          # description explicitly states a different rate.
    with_premium = None
    if s["current_high_bid"] is not None:
        with_premium = round(s["current_high_bid"] * (1 + premium_rate), 2)

    return {
        "bidwrangler_id": s["bidwrangler_id"],
        "auction_date": (s["scheduled_end_time"] or "")[:10] or None,
        "auction_time": (s["scheduled_end_time"] or "")[11:16] or None,
        "address": s["address"],
        "city": s["city"],
        "state": s["state"],
        "zip": s["zip"],
        "title": s["name"],
        "property_type": parsing_utils.guess_property_type(s["name"], s["description"]),
        "status": _closing_label(s["scheduled_end_time"], now_utc),
        "final_status": "Active",
        "final_status_reason": None,
        "current_high_bid": s["current_high_bid"],
        "current_bid_with_premium": with_premium,
        "reserve_amount": s["reserve_amount"],
        "bid_last_checked": checked_at,
        "bid_source_url": s["detail_url"],
        "source_url": s["detail_url"],
        "tax_district": district,
        "tax_map": map_,
        "tax_parcel": parcel,
        "lat": s["lat"],
        "lng": s["lng"],
        "auction_company": "joe_pyle_auctions",
    }


def resolve_one(session, auction_id, checked_at: str, now_utc: datetime.datetime):
    """Returns (outcome, payload) where outcome is one of
    'active' | 'sold' | 'terminal' | 'gone' | 'not_real_estate', and payload
    is either a ready-to-upsert row dict (active/terminal), a past_auctions
    row dict (sold), or None (gone/not_real_estate)."""
    try:
        detail = bidwrangler.get_auction_detail(session, auction_id)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return "gone", None
        raise

    s = bidwrangler.summarize_auction(detail)
    if not bidwrangler.is_real_estate_auction(detail, s):
        return "not_real_estate", None

    text = f"{s['name'] or ''} {s['description'] or ''}".lower()
    if "postpon" in text:
        row = build_active_row(s, now_utc, checked_at)
        row["final_status"] = "Postponed"
        row["status"] = "Postponed"
        return "terminal", row
    if "cancel" in text:
        row = build_active_row(s, now_utc, checked_at)
        row["final_status"] = "Cancelled"
        row["status"] = "Cancelled"
        return "terminal", row

    if not detail.get("complete"):
        return "active", build_active_row(s, now_utc, checked_at)

    item_status = s.get("item_status")
    if item_status == "sold":
        tax_ref = parsing_utils.parse_tax_reference(s["description"])
        stated_sqft = parsing_utils.parse_sqft_from_text(s["description"])
        past_row = bidwrangler.build_past_auction_row(s, tax_ref, stated_sqft)
        past_row["property_type"] = parsing_utils.guess_property_type(s["name"], s["description"])
        return "sold", past_row

    if item_status == "no_sale":
        row = build_active_row(s, now_utc, checked_at)
        row["final_status"] = "Unsold"
        row["status"] = "Unsold"
        return "terminal", row

    row = build_active_row(s, now_utc, checked_at)
    row["final_status"] = "Needs Review"
    row["status"] = "Needs Review"
    row["final_status_reason"] = f"Auction closed but its outcome wasn't clear (item status: {item_status!r})"
    return "terminal", row


def run():
    session = bidwrangler.new_session()
    bidwrangler.warm_session(session)

    discovered_ids = set(bidwrangler.list_upcoming_page_auction_ids(session))
    print(f"Found {len(discovered_ids)} candidate ids (real estate + personal property) on the homepage")

    existing = supabase_client.select("watch_auctions", {"select": "id,bidwrangler_id,source_url,final_status"})
    existing_by_bwid = {str(r["bidwrangler_id"]): r for r in existing if r.get("bidwrangler_id")}
    print(f"{len(existing)} rows already tracked in watch_auctions ({len(existing_by_bwid)} with a usable id)")

    # Always re-check everything we're already tracking, even if it fell out
    # of today's homepage listing -- that's the whole point: a row only
    # leaves watch_auctions by being confirmed Sold, never by silently
    # disappearing from the source page.
    ids_to_check = discovered_ids | set(existing_by_bwid.keys())

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    checked_at = now_utc.strftime("%Y-%m-%d %H:%M UTC")

    active_rows, terminal_rows, sold_rows = [], [], []
    graduated_source_urls = []
    gone_ids = []
    errors = []
    skipped_not_re = 0

    for auction_id in ids_to_check:
        try:
            outcome, payload = resolve_one(session, auction_id, checked_at, now_utc)
        except Exception as e:  # noqa: BLE001
            errors.append(f"auction {auction_id}: {e}")
            continue

        if outcome == "not_real_estate":
            skipped_not_re += 1
            continue
        if outcome == "gone":
            gone_ids.append(auction_id)
            continue
        if outcome == "active":
            active_rows.append(payload)
        elif outcome == "terminal":
            terminal_rows.append(payload)
        elif outcome == "sold":
            sold_rows.append(payload)
            graduated_source_urls.append(payload["source_url"])

    # An id we were already tracking that came back 404 ("gone"): we can't
    # rebuild a fresh row without its detail, so flag the existing row in
    # place rather than dropping it.
    flagged_gone = 0
    for auction_id in gone_ids:
        existing_row = existing_by_bwid.get(str(auction_id))
        if not existing_row or existing_row.get("final_status") in TERMINAL_STATUSES:
            continue  # nothing to update, or already resolved -- leave it
        supabase_client.update_by_id("watch_auctions", existing_row["id"], {
            "final_status": "Needs Review",
            "status": "Needs Review",
            "final_status_reason": "No longer found on the auctioneer's site or BidWrangler -- outcome not confirmed",
        })
        flagged_gone += 1

    upserted = {"inserted_or_updated": 0}
    if active_rows or terminal_rows:
        upserted = supabase_client.upsert("watch_auctions", active_rows + terminal_rows, on_conflict="source_url")

    if sold_rows:
        supabase_client.upsert("past_auctions", sold_rows, on_conflict="source_url")
        # This row now belongs in Past Auctions, not Watch -- remove it from
        # watch_auctions by its specific source_url only (never a bulk
        # "delete anything not in this list" -- that's exactly the behavior
        # we moved away from; every other row in watch_auctions is untouched).
        for url in graduated_source_urls:
            supabase_client.delete_eq("watch_auctions", "source_url", url)

    print(f"{len(active_rows)} active, {len(terminal_rows)} terminal (unsold/cancelled/postponed/needs review), "
          f"{len(sold_rows)} graduated to Sold, {flagged_gone} flagged as gone, "
          f"{skipped_not_re} personal-property skipped, {len(errors)} errors")

    supabase_client.log_run(
        "watch_bids",
        records_found=len(ids_to_check),
        records_added=upserted["inserted_or_updated"],
        errors="; ".join(errors[:20]) if errors else None,
    )
    return {
        "checked": len(ids_to_check),
        "active": len(active_rows),
        "terminal": len(terminal_rows),
        "sold": len(sold_rows),
        "errors": errors,
    }


if __name__ == "__main__":
    outcome = run()
    # Zero active auctions is a normal, unremarkable state for a small
    # regional auctioneer between listings -- it is NOT evidence the scraper
    # is broken (a real breakage, e.g. the homepage itself failing to load,
    # already raises inside run() and fails this job on its own).
    sys.exit(0)
