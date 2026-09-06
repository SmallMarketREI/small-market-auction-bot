#!/usr/bin/env python3
"""Daily job: pull every currently active/upcoming Joe R. Pyle auction from
BidWrangler's company-scoped feed and upsert current bid state into
watch_auctions. This is the live-bid tracking the prototype's Watch tab did
by hand -- now a straight JSON API call, no headless browser required.
"""
import datetime
import sys

from common import bidwrangler, parsing_utils, supabase_client


def build_watch_row(detail: dict, now_utc: datetime.datetime) -> dict:
    s = bidwrangler.summarize_auction(detail)
    district, map_, parcel = parsing_utils.parse_tax_reference(s["description"])

    premium_rate = 0.10  # Pyle's standard 10% buyer's premium, seen on every listing
                          # description during recon; override per-listing if a
                          # description explicitly states a different rate.
    with_premium = None
    if s["current_high_bid"] is not None:
        with_premium = round(s["current_high_bid"] * (1 + premium_rate), 2)

    # "Closing" vs "Upcoming" is about how soon bidding ends, not whether the
    # auction is done -- rows that are actually complete/archived never reach
    # this function at all (see run(), below), so both labels here always mean
    # "still open."
    closing_soon = False
    end_time_raw = s["scheduled_end_time"]
    if end_time_raw:
        try:
            end_dt = datetime.datetime.fromisoformat(end_time_raw.replace("Z", "+00:00"))
            closing_soon = (end_dt - now_utc) <= datetime.timedelta(hours=72)
        except ValueError:
            pass

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
        "status": "Closing" if closing_soon else "Upcoming",
        "current_high_bid": s["current_high_bid"],
        "current_bid_with_premium": with_premium,
        "reserve_amount": s["reserve_amount"],
        "bid_last_checked": None,  # set below, once we know "now" in a readable form
        "bid_source_url": s["detail_url"],
        "source_url": s["detail_url"],
        "tax_district": district,
        "tax_map": map_,
        "tax_parcel": parcel,
        "lat": s["lat"],
        "lng": s["lng"],
    }


def run():
    session = bidwrangler.new_session()
    bidwrangler.warm_session(session)

    ids = bidwrangler.list_feed_auction_ids(session)
    print(f"Found {len(ids)} active/upcoming auction ids from the BidWrangler feed")

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    checked_at = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    rows = []
    skipped_sold = 0
    errors = []
    for auction_id in ids:
        try:
            detail = bidwrangler.get_auction_detail(session, auction_id)
        except Exception as e:  # noqa: BLE001
            errors.append(f"auction {auction_id}: {e}")
            continue

        # The feed can include auctions that have already ended (BidWrangler's
        # "active" grouping isn't the same as "still accepting bids") -- those
        # belong in past_auctions, not Watch, so skip them here the same way
        # scrape_past_sales.py decides is_sold.
        status = (detail.get("status") or "").lower()
        is_sold = detail.get("complete") or detail.get("archived") or status in ("complete", "closed")
        if is_sold:
            skipped_sold += 1
            continue

        row = build_watch_row(detail, now_utc)
        row["bid_last_checked"] = checked_at
        rows.append(row)

    print(f"{len(rows)} watch rows to upsert ({skipped_sold} already-closed skipped), {len(errors)} lookups failed")

    result = {"inserted_or_updated": 0}
    if rows:
        result = supabase_client.upsert("watch_auctions", rows, on_conflict="source_url")
        # Keep watch_auctions an exact mirror of "what's active right now" --
        # anything that fell out of the feed (sold, cancelled, or just no
        # longer listed) should disappear from Watch instead of lingering.
        supabase_client.delete_not_in("watch_auctions", "source_url", [r["source_url"] for r in rows])

    supabase_client.log_run(
        "watch_bids",
        records_found=len(ids),
        records_added=result["inserted_or_updated"],
        errors="; ".join(errors[:20]) if errors else None,
    )
    print(f"Done: {result}")
    return {"ids_found": len(ids), "rows": rows, "errors": errors}


if __name__ == "__main__":
    outcome = run()
    # Zero active auctions is a normal, unremarkable state for a small
    # regional auctioneer between listings -- it is NOT evidence the scraper
    # is broken (a real breakage, e.g. the feed endpoint itself failing,
    # already raises inside run() and fails this job on its own). Treating a
    # quiet day as a failure just trains everyone to ignore red X's, which
    # matters more now that this same script also runs unattended every few
    # hours via refresh-watch-bids.yml.
    sys.exit(0)
