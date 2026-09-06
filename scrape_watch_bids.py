#!/usr/bin/env python3
"""Daily job: pull every currently active/upcoming auction from BidWrangler's
own feed (bid.joerpyleauctions.com/api/feed/all) and upsert current bid state
into watch_auctions. This is the live-bid tracking the prototype's Watch tab
did by hand -- now a straight JSON API call, no headless browser required.
"""
import sys

from common import bidwrangler, parsing_utils, supabase_client


def build_watch_row(detail: dict) -> dict:
    s = bidwrangler.summarize_auction(detail)
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
        "property_type": "House",
        "status": "Upcoming" if s["status"] not in ("complete", "closed") else "Closing",
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
    import datetime

    session = bidwrangler.new_session()
    bidwrangler.warm_session(session)

    ids = bidwrangler.list_feed_auction_ids(session)
    print(f"Found {len(ids)} active/upcoming auction ids from the BidWrangler feed")

    checked_at = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    rows = []
    errors = []
    for auction_id in ids:
        try:
            detail = bidwrangler.get_auction_detail(session, auction_id)
            row = build_watch_row(detail)
            row["bid_last_checked"] = checked_at
            rows.append(row)
        except Exception as e:  # noqa: BLE001
            errors.append(f"auction {auction_id}: {e}")

    print(f"{len(rows)} watch rows to upsert, {len(errors)} lookups failed")

    result = {"inserted_or_updated": 0}
    if rows:
        result = supabase_client.upsert("watch_auctions", rows, on_conflict="source_url")

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
    sys.exit(1 if outcome["ids_found"] == 0 else 0)
