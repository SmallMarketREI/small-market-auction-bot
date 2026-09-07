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

Multi-parcel auctions (e.g. "16 Raleigh County Parcels" -- see
bidwrangler.is_multi_parcel_real_estate_auction) get resolved per PARCEL, not
per auction: one auction page can have some parcels already sold, some still
active, and some unsold, all at once, and each needs its own independent
outcome. resolve_one() therefore returns a LIST of (outcome, payload) tuples
(almost always length 1, longer for a multi-parcel auction). A parcel-level
wrinkle worth knowing about: if the auction's "Property in Entirety" bundle
option is the one that sells, the individual parcels never separately
transacted, so any watch_auctions rows previously tracking them by their own
parcel_key are now stale -- run() reconciles this per auction (matching on
source_url) after resolving, deleting any watch row whose parcel_key isn't
in this run's freshly-resolved set for that same auction.
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
        "parcel_key": s["detail_url"],  # single-lot: one property == the whole page
        "parcel_label": None,
        "acreage": None,
        "review_flag": False,
        "review_reason": None,
    }


def _multi_parcel_watch_row(s_top: dict, item: dict, idx: int, now_utc: datetime.datetime,
                             checked_at: str, final_status: str, final_status_reason=None) -> dict:
    """A multi-parcel row shaped for watch_auctions (active/terminal, never
    sold -- sold parcels go through _multi_parcel_past_row instead)."""
    row = bidwrangler.build_multi_parcel_row(s_top, item, idx)
    # watch_auctions has no comp_sqft/quality columns -- that's a
    # past_auctions-only concept (enrich_sqft_wv.py only ever touches past
    # sales), same as the existing single-lot build_active_row never sets
    # them either. Drop whatever build_multi_parcel_row conditionally added.
    for k in ("comp_sqft", "comp_sqft_source", "comp_sqft_source_url", "comp_sqft_quality", "comp_sqft_note"):
        row.pop(k, None)
    row["auction_time"] = (s_top["scheduled_end_time"] or "")[11:16] or None
    row["title"] = row.pop("title_notes")
    row["status"] = final_status
    row["final_status"] = final_status
    row["final_status_reason"] = final_status_reason or row.get("review_reason")
    row["current_bid_with_premium"] = (
        round(row["current_high_bid"] * 1.10, 2) if row.get("current_high_bid") is not None else None
    )
    row["reserve_amount"] = None  # not exposed per-item in the API response observed
    row["bid_last_checked"] = checked_at
    row["bid_source_url"] = s_top["detail_url"]
    return row


def _multi_parcel_past_row(s_top: dict, item: dict, idx: int) -> dict:
    row = bidwrangler.build_multi_parcel_row(s_top, item, idx)
    row["published_final_sold_price"] = row.pop("current_high_bid")
    row["status"] = "Sold"
    return row


def _resolve_multi_parcel(detail: dict, s: dict, checked_at: str, now_utc: datetime.datetime):
    """Per-parcel resolution for a multi-parcel auction -- see module
    docstring. Returns a list of (outcome, payload) tuples, one per parcel
    currently relevant (bidwrangler.multi_parcel_items already collapses to
    just the bundle item if the bundle is what actually sold)."""
    text = f"{s['name'] or ''} {s['description'] or ''}".lower()
    forced_status = None
    if "postpon" in text:
        forced_status = "Postponed"
    elif "cancel" in text:
        forced_status = "Cancelled"

    results = []
    for idx, item in bidwrangler.multi_parcel_items(detail):
        item_status = item.get("status")

        if forced_status:
            row = _multi_parcel_watch_row(s, item, idx, now_utc, checked_at, forced_status)
            results.append(("terminal", row))
            continue

        if item_status == "sold":
            results.append(("sold", _multi_parcel_past_row(s, item, idx)))
        elif item_status == "no_sale":
            row = _multi_parcel_watch_row(s, item, idx, now_utc, checked_at, "Unsold")
            results.append(("terminal", row))
        elif item_status in ("active", "accepting_bids", "pending", None):
            row = _multi_parcel_watch_row(s, item, idx, now_utc, checked_at, "Active", None)
            row["status"] = _closing_label(s["scheduled_end_time"], now_utc)
            results.append(("active", row))
        else:
            row = _multi_parcel_watch_row(
                s, item, idx, now_utc, checked_at, "Needs Review",
                f"Parcel closed but its outcome wasn't clear (item status: {item_status!r})",
            )
            results.append(("terminal", row))
    return results


def resolve_one(session, auction_id, checked_at: str, now_utc: datetime.datetime):
    """Returns a LIST of (outcome, payload) tuples, where outcome is one of
    'active' | 'sold' | 'terminal' | 'gone' | 'not_real_estate', and payload
    is either a ready-to-upsert watch_auctions row dict (active/terminal), a
    past_auctions row dict (sold), or None (gone/not_real_estate). Almost
    always a single-item list; a multi-parcel auction yields one tuple per
    currently-relevant parcel (see _resolve_multi_parcel)."""
    try:
        detail = bidwrangler.get_auction_detail(session, auction_id)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return [("gone", None)]
        raise

    s = bidwrangler.summarize_auction(detail)

    if bidwrangler.is_multi_parcel_real_estate_auction(detail):
        return _resolve_multi_parcel(detail, s, checked_at, now_utc)

    if not bidwrangler.is_real_estate_auction(detail, s):
        return [("not_real_estate", None)]

    text = f"{s['name'] or ''} {s['description'] or ''}".lower()
    if "postpon" in text:
        row = build_active_row(s, now_utc, checked_at)
        row["final_status"] = "Postponed"
        row["status"] = "Postponed"
        return [("terminal", row)]
    if "cancel" in text:
        row = build_active_row(s, now_utc, checked_at)
        row["final_status"] = "Cancelled"
        row["status"] = "Cancelled"
        return [("terminal", row)]

    if not detail.get("complete"):
        return [("active", build_active_row(s, now_utc, checked_at))]

    item_status = s.get("item_status")
    if item_status == "sold":
        tax_ref = parsing_utils.parse_tax_reference(s["description"])
        stated_sqft = parsing_utils.parse_sqft_from_text(s["description"])
        past_row = bidwrangler.build_past_auction_row(s, tax_ref, stated_sqft)
        past_row["property_type"] = parsing_utils.guess_property_type(s["name"], s["description"])
        return [("sold", past_row)]

    if item_status == "no_sale":
        row = build_active_row(s, now_utc, checked_at)
        row["final_status"] = "Unsold"
        row["status"] = "Unsold"
        return [("terminal", row)]

    row = build_active_row(s, now_utc, checked_at)
    row["final_status"] = "Needs Review"
    row["status"] = "Needs Review"
    row["final_status_reason"] = f"Auction closed but its outcome wasn't clear (item status: {item_status!r})"
    return [("terminal", row)]


def run():
    session = bidwrangler.new_session()
    bidwrangler.warm_session(session)

    discovered_ids = set(bidwrangler.list_upcoming_page_auction_ids(session))
    print(f"Found {len(discovered_ids)} candidate ids (real estate + personal property) on the homepage")

    existing = supabase_client.select(
        "watch_auctions", {"select": "id,bidwrangler_id,source_url,parcel_key,final_status"}
    )
    # A bidwrangler_id (and a source_url) can now back MULTIPLE watch_auctions
    # rows -- one per parcel of a multi-parcel auction -- so both indexes map
    # to a LIST of rows, not a single row.
    existing_by_bwid = {}
    existing_by_source_url = {}
    for r in existing:
        if r.get("bidwrangler_id"):
            existing_by_bwid.setdefault(str(r["bidwrangler_id"]), []).append(r)
        if r.get("source_url"):
            existing_by_source_url.setdefault(r["source_url"], []).append(r)
    print(f"{len(existing)} rows already tracked in watch_auctions ({len(existing_by_bwid)} distinct auction ids)")

    # Always re-check everything we're already tracking, even if it fell out
    # of today's homepage listing -- that's the whole point: a row only
    # leaves watch_auctions by being confirmed Sold, never by silently
    # disappearing from the source page.
    ids_to_check = discovered_ids | set(existing_by_bwid.keys())

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    checked_at = now_utc.strftime("%Y-%m-%d %H:%M UTC")

    active_rows, terminal_rows, sold_rows = [], [], []
    graduated_parcel_keys = []
    # For every multi-parcel auction we successfully resolve this run, track
    # the full set of parcel_keys that are still relevant (whether they
    # ended up active/terminal/sold) so stale sibling rows -- e.g. individual
    # parcels superseded by a "Property in Entirety" bundle sale -- can be
    # cleaned up below, keyed by the auction's shared source_url.
    resolved_parcel_keys_by_source_url = {}
    gone_ids = []
    errors = []
    skipped_not_re = 0

    for auction_id in ids_to_check:
        try:
            results = resolve_one(session, auction_id, checked_at, now_utc)
        except Exception as e:  # noqa: BLE001
            errors.append(f"auction {auction_id}: {e}")
            continue

        for outcome, payload in results:
            if outcome == "not_real_estate":
                skipped_not_re += 1
                continue
            if outcome == "gone":
                gone_ids.append(auction_id)
                continue
            if outcome == "active":
                active_rows.append(payload)
                resolved_parcel_keys_by_source_url.setdefault(payload["source_url"], set()).add(payload["parcel_key"])
            elif outcome == "terminal":
                terminal_rows.append(payload)
                resolved_parcel_keys_by_source_url.setdefault(payload["source_url"], set()).add(payload["parcel_key"])
            elif outcome == "sold":
                sold_rows.append(payload)
                graduated_parcel_keys.append(payload["parcel_key"])
                resolved_parcel_keys_by_source_url.setdefault(payload["source_url"], set()).add(payload["parcel_key"])

    # An id we were already tracking that came back 404 ("gone"): we can't
    # rebuild fresh rows without its detail, so flag every existing row for
    # that auction in place rather than dropping any of them.
    flagged_gone = 0
    for auction_id in gone_ids:
        for existing_row in existing_by_bwid.get(str(auction_id), []):
            if existing_row.get("final_status") in TERMINAL_STATUSES:
                continue  # already resolved -- leave it
            supabase_client.update_by_id("watch_auctions", existing_row["id"], {
                "final_status": "Needs Review",
                "status": "Needs Review",
                "final_status_reason": "No longer found on the auctioneer's site or BidWrangler -- outcome not confirmed",
            })
            flagged_gone += 1

    upserted = {"inserted_or_updated": 0}
    if active_rows or terminal_rows:
        upserted = supabase_client.upsert("watch_auctions", active_rows + terminal_rows, on_conflict="parcel_key")

    if sold_rows:
        supabase_client.upsert("past_auctions", sold_rows, on_conflict="parcel_key")
        # This parcel now belongs in Past Auctions, not Watch -- remove it
        # from watch_auctions by its specific parcel_key only. Deleting by
        # source_url here would be wrong for a multi-parcel auction where
        # only SOME parcels sold this run -- it would wipe out sibling
        # parcels that are still active/unresolved.
        for key in graduated_parcel_keys:
            supabase_client.delete_eq("watch_auctions", "parcel_key", key)
        # Same stale-single-lot cleanup scrape_past_sales.py does for its own
        # discovery path: an auction that graduates here as multi-parcel for
        # the first time can still have an OLD past_auctions row keyed by the
        # bare source_url (single-lot shape, from before multi-parcel support
        # or before this auction's naming matched it) -- that row is now
        # superseded by the per-parcel #1/#2/... rows just upserted above, so
        # remove it. This path can graduate an auction to Sold before
        # /results ever lists it, so scrape_past_sales.py's own cleanup can't
        # be relied on to catch every case. Only for genuinely multi-parcel
        # sold rows (parcel_key != source_url) -- for an ordinary single-lot
        # sale parcel_key IS the source_url, and deleting by source_url there
        # would delete the row that was just upserted.
        multi_parcel_sold_source_urls = {
            r["source_url"] for r in sold_rows if r["parcel_key"] != r["source_url"]
        }
        for source_url in multi_parcel_sold_source_urls:
            supabase_client.delete_eq("past_auctions", "parcel_key", source_url)

    # Stale-parcel cleanup: for every auction we resolved this run, delete any
    # watch_auctions row still sitting under that same source_url whose
    # parcel_key ISN'T in what we just resolved -- e.g. individual "Subject N"
    # parcels that were being tracked before a "Property in Entirety" bundle
    # sale superseded them (multi_parcel_items() then yields only the bundle,
    # so those individual parcel_keys would otherwise never get revisited and
    # sit stale forever).
    stale_cleaned = 0
    for source_url, current_keys in resolved_parcel_keys_by_source_url.items():
        for existing_row in existing_by_source_url.get(source_url, []):
            key = existing_row.get("parcel_key")
            if key and key not in current_keys and key not in graduated_parcel_keys:
                supabase_client.delete_eq("watch_auctions", "parcel_key", key)
                stale_cleaned += 1

    print(f"{len(active_rows)} active, {len(terminal_rows)} terminal (unsold/cancelled/postponed/needs review), "
          f"{len(sold_rows)} graduated to Sold, {flagged_gone} flagged as gone, {stale_cleaned} stale parcels "
          f"cleaned up, {skipped_not_re} personal-property skipped, {len(errors)} errors")

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
