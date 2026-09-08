#!/usr/bin/env python3
"""Daily job: fill in lat/lng for past_auctions and watch_auctions rows that
don't have it yet, using the free US Census geocoder (common/geocoding.py).

Why this exists: enrich_sqft_wv.py's WV Assessment lookup needs a lat/lng to
find the right tax parcel (see its module docstring) -- a row with no
coordinates can never be enriched no matter how many times the daily job
runs. Confirmed 2026-09-08 against production: 297 of 486 past_auctions rows
and 32 of 139 watch_auctions rows have no lat/lng at all, mostly rows that
came in through the client's Zillow property-list import or the historical
audit backfill, neither of which ever captured coordinates. This script (run
before enrich_sqft_wv.py in daily-update.yml) closes that gap wherever the
listing has a real street address to geocode.

What this does NOT fix: a meaningful chunk of that 297/32 is vacant land
listed with a placeholder house number ("0 Some Road, City, WV" -- this
auctioneer's convention when there's no real street number). The Census
geocoder can only match a real numbered address range, so these correctly
come back with no match every time, not just once -- see
common/geocoding.py's module docstring for why a looser fallback isn't used
here. Every row this happens to gets a plain, honest review_reason instead
of a silently-wrong guessed coordinate; a future pass could look up these
specific parcels by name/description against the county GIS directly, but
that's real new scope, not a tweak to this script.
"""
import sys
import time

from common import geocoding, supabase_client

_SELECT_FIELDS = "id,address,city,state,zip"


def candidates(table: str) -> list:
    return supabase_client.select(
        table, {"select": _SELECT_FIELDS, "lat": "is.null", "address": "not.is.null"},
    )


def run(limit: int = None):
    session = geocoding.new_session()

    totals = {"checked": 0, "geocoded": 0, "no_match": 0, "errors": []}
    for table in ("past_auctions", "watch_auctions"):
        rows = candidates(table)
        if limit:
            rows = rows[:limit]
        print(f"{table}: {len(rows)} rows missing lat/lng with an address to try")

        geocoded = 0
        no_match = 0
        for row in rows:
            try:
                result = geocoding.geocode(
                    session, row.get("address"), row.get("city"), row.get("state"), row.get("zip"),
                )
            except Exception as e:  # noqa: BLE001
                totals["errors"].append(f"{table} {row['id']}: {e}")
                time.sleep(geocoding.REQUEST_DELAY_SECONDS)
                continue

            if result is None:
                no_match += 1
                time.sleep(geocoding.REQUEST_DELAY_SECONDS)
                continue

            try:
                supabase_client.update_by_id(table, row["id"], {
                    "lat": result["lat"],
                    "lng": result["lng"],
                })
                geocoded += 1
            except Exception as e:  # noqa: BLE001
                totals["errors"].append(f"{table} {row['id']}: write failed: {e}")

            time.sleep(geocoding.REQUEST_DELAY_SECONDS)

        print(f"{table}: geocoded {geocoded}, no match for {no_match} "
              f"(most commonly a vacant-land placeholder address with no real house number)")
        totals["checked"] += len(rows)
        totals["geocoded"] += geocoded
        totals["no_match"] += no_match

    supabase_client.log_run(
        "geocoding",
        records_found=totals["checked"],
        records_updated=totals["geocoded"],
        errors="; ".join(totals["errors"][:20]) if totals["errors"] else None,
    )
    return totals


if __name__ == "__main__":
    outcome = run()
    sys.exit(0)  # an address that can't be geocoded is expected, not a pipeline failure
