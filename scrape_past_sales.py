#!/usr/bin/env python3
"""Daily job: discover every SOLD real-estate auction linked from
joerpyleauctions.com/results, and upsert it into past_auctions.

Only real estate -- Joe Pyle's own account also runs personal-property
auctions (vehicles, tools, estate contents) through the same /results
archive and the same BidWrangler platform, and those are excluded here via
bidwrangler.is_real_estate_auction() (single-lot + "real estate" in the
listing text; a personal-property auction is multi-lot and would otherwise
get mis-recorded as a single sale using only its first lot's price).

Real estate on this account comes in TWO shapes, handled separately:
  - single-lot: one auction page == one property (is_real_estate_auction).
  - multi-parcel: one auction page lists several distinct parcels (e.g.
    "16 Raleigh County Parcels"), each its own comparable property
    (is_multi_parcel_real_estate_auction -- see bidwrangler.py for how
    these are told apart from a personal-property multi-lot auction).
    Each SOLD parcel becomes its own past_auctions row, sharing the one
    Joe Pyle source_url but with its own unique parcel_key -- confirmed
    2026-09-07 this shape was previously being silently skipped entirely
    (items_count != 1 failed the single-lot check), dropping real sales
    that were sometimes the largest dollar amounts on the books.

Also parses the county tax District/Map/Parcel out of the listing description
when present -- enrich_sqft_wv.py uses that to look up official square
footage, so this should run before that job in the daily workflow.

Confirmed 2026-09-07: /results only exposes roughly the last 3.5 months of
sold auctions before its own pagination runs out (a genuine limit of what
the auctioneer's site publicly exposes, not a setting here to raise -- see
bidwrangler.list_result_page_auction_ids's docstring). Going back further
would need a records export from Joe Pyle's office to import once by hand.
"""
import sys
import time

from common import bidwrangler, parsing_utils, supabase_client


def build_past_row(detail: dict) -> dict:
    s = bidwrangler.summarize_auction(detail)
    tax_ref = parsing_utils.parse_tax_reference(s["description"])
    stated_sqft = parsing_utils.parse_sqft_from_text(s["description"])
    row = bidwrangler.build_past_auction_row(s, tax_ref, stated_sqft)
    row["property_type"] = parsing_utils.guess_property_type(s["name"], s["description"])
    return row


def run(max_result_pages: int = 60):
    session = bidwrangler.new_session()
    bidwrangler.warm_session(session)

    ids = bidwrangler.list_result_page_auction_ids(session, max_pages=max_result_pages)
    print(f"Found {len(ids)} auction ids linked from joerpyleauctions.com/results")

    rows = []
    skipped_not_re = 0
    multi_parcel_auctions = 0
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

        s = bidwrangler.summarize_auction(detail)

        if bidwrangler.is_multi_parcel_real_estate_auction(detail):
            multi_parcel_auctions += 1
            for idx, item in bidwrangler.multi_parcel_items(detail):
                if item.get("status") != "sold":
                    continue  # this specific parcel didn't sell -- not a past-auctions row
                row = bidwrangler.build_multi_parcel_row(s, item, idx)
                row["published_final_sold_price"] = row.pop("current_high_bid")
                row["status"] = "Sold"
                if row["published_final_sold_price"] is None:
                    continue
                rows.append(row)
            time.sleep(0.3)
            continue

        if not bidwrangler.is_real_estate_auction(detail, s):
            skipped_not_re += 1
            continue  # personal property -- not a comp for this business

        tax_ref = parsing_utils.parse_tax_reference(s["description"])
        stated_sqft = parsing_utils.parse_sqft_from_text(s["description"])
        row = bidwrangler.build_past_auction_row(s, tax_ref, stated_sqft)
        row["property_type"] = parsing_utils.guess_property_type(s["name"], s["description"])

        if row["published_final_sold_price"] is None:
            continue  # no winning bid recorded (cancelled/unsold lot) -- skip
        rows.append(row)
        time.sleep(0.3)

    print(f"{len(rows)} sold real-estate rows to upsert ({multi_parcel_auctions} were multi-parcel "
          f"auctions contributing one row per sold parcel), {skipped_not_re} personal-property "
          f"auctions skipped, {len(errors)} lookups failed")

    result = {"inserted_or_updated": 0}
    if rows:
        result = supabase_client.upsert("past_auctions", rows, on_conflict="parcel_key")

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
