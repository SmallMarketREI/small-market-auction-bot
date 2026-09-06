#!/usr/bin/env python3
"""Daily job: discover every auction linked from joerpyleauctions.com/results,
pull its full detail from the BidWrangler API, and upsert the ones that have
actually sold (status complete/archived with a winning bid) into
past_auctions.

Also parses the county tax District/Map/Parcel out of the listing description
when present -- enrich_sqft_wv.py uses that to look up official square
footage, so this should run before that job in the daily workflow.
"""
import sys
import time

from common import bidwrangler, parsing_utils, supabase_client
from common.parsing_utils import compute_ppsf


def build_past_row(detail: dict) -> dict:
    s = bidwrangler.summarize_auction(detail)
    district, map_, parcel = parsing_utils.parse_tax_reference(s["description"])
    stated_sqft = parsing_utils.parse_sqft_from_text(s["description"])

    row = {
        "bidwrangler_id": s["bidwrangler_id"],
        "auction_date": (s["scheduled_end_time"] or "")[:10] or None,
        "address": s["address"],
        "city": s["city"],
        "state": s["state"],
        "zip": s["zip"],
        "property_type": parsing_utils.guess_property_type(s["name"], s["description"]),
        "published_final_sold_price": s["current_high_bid"],
        "status": "Sold",
        "title_notes": s["name"],
        "source_url": s["detail_url"],
        "tax_district": district,
        "tax_map": map_,
        "tax_parcel": parcel,
        "lat": s["lat"],
        "lng": s["lng"],
        # These five must always be present (even as null) -- Supabase's bulk
        # upsert rejects a batch where different rows have different sets of
        # keys (PGRST102: "All object keys must match").
        "comp_sqft": None,
        "comp_sqft_source": None,
        "comp_sqft_source_url": None,
        "comp_sqft_quality": None,
        "comp_sqft_note": None,
    }
    if stated_sqft:
        row["comp_sqft"] = stated_sqft
        row["comp_sqft_source"] = "Auction listing (auctioneer-stated)"
        row["comp_sqft_source_url"] = s["marketing_url"]
        row["comp_sqft_quality"] = "Unverified -- from listing copy, not the county record"
        row["comp_sqft_note"] = (
            f"Auctioneer's listing states {stated_sqft:g} sq ft. Run enrich_sqft_wv.py to "
            "confirm/replace with the official WV Assessment figure."
        )
    return row


def run(max_result_pages: int = 60):
    session = bidwrangler.new_session()
    bidwrangler.warm_session(session)

    ids = bidwrangler.list_result_page_auction_ids(session, max_pages=max_result_pages)
    print(f"Found {len(ids)} auction ids linked from joerpyleauctions.com/results")

    rows = []
    errors = []
    for auction_id in ids:
        try:
            detail = bidwrangler.get_auction_detail(session, auction_id)
        except Exception as e:  # noqa: BLE001
            errors.append(f"auction {auction_id}: {e}")
            continue

        status = (detail.get("status") or "").lower()
        is_sold = detail.get("complete") or detail.get("archived") or status in ("complete", "closed")
        if not is_sold:
            continue  # still upcoming/active -- that's scrape_watch_bids.py's job

        row = build_past_row(detail)
        if row["published_final_sold_price"] is None:
            continue  # no winning bid recorded (cancelled/unsold lot) -- skip
        rows.append(row)
        time.sleep(0.3)

    print(f"{len(rows)} sold auctions to upsert, {len(errors)} lookups failed")

    result = {"inserted_or_updated": 0}
    if rows:
        result = supabase_client.upsert("past_auctions", rows, on_conflict="source_url")

    supabase_client.log_run(
        "past_sales",
        records_found=len(ids),
        records_added=result["inserted_or_updated"],
        errors="; ".join(errors[:20]) if errors else None,
    )
    print(f"Done: {result}")
    return {"ids_found": len(ids), "rows": rows, "errors": errors}


if __name__ == "__main__":
    outcome = run()
    # Fail the Action loudly only on total breakage (e.g. the site's markup
    # changed and our link-scraping regex stopped matching any auction ids at
    # all) -- a handful of individual lookup errors among hundreds of auctions
    # is normal and shouldn't block the rest of the daily pipeline, and a
    # quiet day with zero new sales is not a failure either.
    sys.exit(1 if outcome["ids_found"] == 0 else 0)
