#!/usr/bin/env python3
"""One-time backfill: import the genuine pre-2026-05-27 historical records
that /results' 3.5-month archive limit means the daily scraper can never
reach on its own (see scrape_past_sales.py's docstring).

Where this data came from (2026-09-07):
  - 34 single-property rows: the client's original 233-row hand-built
    database, filtered down to just the 42 rows dated before 2026-05-27
    (the live scraper's earliest reach), with 8 further excluded because
    they were themselves placeholder rows for multi-parcel/portfolio
    auctions with no price recorded (see EXCLUDED_PLACEHOLDER_TITLES in
    build_backfill_dataset.py) -- 5 of those 8 and all 30 of the client's
    "Confirmed Missing Audit" sheet rows are superseded by:
  - 41 multi-parcel rows: fetched LIVE from BidWrangler's own
    /api/auctions/{id} endpoint for the 5 multi-parcel real-estate auctions
    the client's audit identified (16 Raleigh County Parcels, 7 Properties
    in Multiple Counties, 5 Land Tracts in Mingo County, 2 Wood County
    Properties, Investment Properties in 4 Counties) and run through the
    same is_multi_parcel_real_estate_auction/multi_parcel_items/
    build_multi_parcel_row pipeline scrape_past_sales.py uses daily -- so
    these 41 rows carry real per-parcel sold prices, not just a confirmed
    address, and needed two real bugs fixed in common/parsing_utils.py to
    come out right (see CHANGES.md / the delivery notes: the "Subject N:"
    name matcher only recognized number words up to "ten" and was silently
    dropping parcels 11+ on larger auctions; a run-on "123 Main St City, WV"
    item name with no separator before the city could match with a blank
    city and false confidence instead of correctly falling through to
    review).

Known gap: one parcel from the client's audit ("146 Lower Bottom Rd, Helen,
WV, ~0.13 acres", part of the 16 Raleigh County Parcels auction) is not in
this import -- it no longer appears in BidWrangler's own record for that
auction (removed/consolidated after the sale closed, best guess) and the
audit sheet has no price for it either, so there's nothing checkable to add.
Worth asking the client for a screenshot/confirmation if that record matters.

The combined 75 rows are frozen in backfill_data/audited_backfill_2026-09-07.json
(not re-derived from the client's spreadsheet at run time -- that file isn't
part of this repo, and this script is meant to run once) so this script just
needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY to run.

Usage:
    python import_audited_backfill.py            # dry run -- prints a summary, writes nothing
    python import_audited_backfill.py --apply     # actually upserts into past_auctions

Safe to re-run: every row upserts on parcel_key (same idempotent pattern as
the daily scrapers), so running this twice does not create duplicates.
"""
import argparse
import json
import os
import sys

from common import supabase_client

DATA_FILE = os.path.join(os.path.dirname(__file__), "backfill_data", "audited_backfill_2026-09-07.json")


def load_rows():
    with open(DATA_FILE) as f:
        rows = json.load(f)
    seen = set()
    for r in rows:
        if r["parcel_key"] in seen:
            raise ValueError(f"duplicate parcel_key in backfill data: {r['parcel_key']}")
        seen.add(r["parcel_key"])
    return rows


def summarize(rows):
    priced = [r for r in rows if r["published_final_sold_price"] is not None]
    flagged = [r for r in rows if r["review_flag"]]
    dates = sorted(r["auction_date"] for r in rows if r["auction_date"])
    print(f"{len(rows)} rows loaded from {os.path.basename(DATA_FILE)}")
    print(f"  {len(priced)} with a published price (combined ${sum(r['published_final_sold_price'] for r in priced):,.0f})")
    print(f"  {len(flagged)} flagged Needs Review (address could not be confidently parsed)")
    print(f"  date range: {dates[0]} to {dates[-1]}")
    by_type = {}
    for r in rows:
        by_type[r["property_type"]] = by_type.get(r["property_type"], 0) + 1
    print(f"  property types: {dict(sorted(by_type.items()))}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually upsert into Supabase (default: dry run)")
    args = ap.parse_args()

    rows = load_rows()
    summarize(rows)

    if not args.apply:
        print("\nDry run only -- nothing written. Re-run with --apply to upsert into past_auctions.")
        return 0

    print(f"\nUpserting {len(rows)} rows into past_auctions (on_conflict=parcel_key)...")
    result = supabase_client.upsert("past_auctions", rows, on_conflict="parcel_key")
    print(f"Done: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
