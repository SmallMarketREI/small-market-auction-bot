#!/usr/bin/env python3
"""Daily job: fill in official square footage for past_auctions rows that are
missing it (or only have the auctioneer's stated figure), using the WV Real
Estate Assessment county record.

Pipeline per row:
  1. Use the listing's lat/lng (captured by scrape_past_sales.py) to query the
     statewide ArcGIS parcel layer for the authoritative County/District/Map/
     Parcel -- this is more reliable than trusting the auctioneer's listing
     text alone, though we fall back to that text if the point lookup misses.
  2. Search mapwv.gov's WV Assessment app by County + Map + Parcel to resolve
     the Root Parcel ID.
  3. Fetch that parcel's Assessment Detail page and read "Sum of Structure
     Areas" (confirmed during recon to be the same figure as SFLA / stated
     listing sqft).

Rows this can't resolve (no lat/lng, no parcel match, or the county's data
just doesn't have a structure recorded) are left alone -- they keep whatever
comp_sqft they already had (possibly none), and show up in scrape_runs'
`errors` so you can see how many are outstanding.
"""
import sys

from common import supabase_client, wv_assessment


def candidates():
    """Rows worth (re-)checking: no comp_sqft yet, or comp_sqft came only from
    the auctioneer's listing copy rather than a verified county record."""
    seen = {}
    missing = supabase_client.select(
        "past_auctions",
        {"select": "id,city,state,zip,address,lat,lng,tax_district,tax_map,tax_parcel,comp_sqft_source",
         "comp_sqft": "is.null"},
    )
    for r in missing:
        seen[r["id"]] = r

    stated_only = supabase_client.select(
        "past_auctions",
        {"select": "id,city,state,zip,address,lat,lng,tax_district,tax_map,tax_parcel,comp_sqft_source",
         "comp_sqft_source": "eq.Auction listing (auctioneer-stated)"},
    )
    for r in stated_only:
        seen[r["id"]] = r

    return list(seen.values())


def resolve_one(session, row):
    lat, lng = row.get("lat"), row.get("lng")
    raw_county = map_ = parcel = None

    if lat and lng:
        arcgis = wv_assessment.lookup_parcel_by_latlng(session, lat, lng)
        if arcgis:
            raw_county = arcgis.get("COUNTY")
            map_ = arcgis.get("Map") or row.get("tax_map")
            parcel = arcgis.get("Parcel") or row.get("tax_parcel")

    if not map_:
        map_ = row.get("tax_map")
    if not parcel:
        parcel = row.get("tax_parcel")

    if not raw_county:
        # No spatial match (or no lat/lng at all) -- without a county we can't
        # search the assessment database. A future improvement: maintain a
        # WV city -> county lookup table as a fallback here.
        return None, "no county resolved (missing/failed lat-lng parcel lookup)"

    # Confirmed in production 2026-09: the ArcGIS parcel layer's COUNTY field
    # actually comes back as WV's numeric county code (e.g. '40', '06'), not a
    # spelled-out name -- the earlier "unrecognized county name" errors were
    # this client trying to look up '40' as if it were a county name. Handle
    # both shapes so this keeps working if a future ArcGIS response ever does
    # send a name instead.
    raw_county_str = str(raw_county).strip()
    if raw_county_str.isdigit():
        county_code = int(raw_county_str)
        county_name = wv_assessment.COUNTY_CODE_TO_NAME.get(county_code, raw_county_str)
    else:
        county_name = raw_county_str.title()
        county_code = wv_assessment.COUNTY_NAME_TO_CODE.get(county_name)

    if not county_code:
        return None, f"unrecognized county from ArcGIS: {raw_county!r}"

    if not (map_ and parcel):
        return None, "no tax map/parcel available to search with"

    results = wv_assessment.search_assessment(session, county_code, map_=map_, parcel=parcel)
    if not results:
        return None, f"no WV Assessment match for county={county_code} map={map_} parcel={parcel}"

    root_pid = results[0].get("root_pid")
    if not root_pid:
        return None, "matched a record but couldn't find its detail-page id"

    detail = wv_assessment.get_assessment_detail(session, root_pid)
    if not detail.get("comp_sqft"):
        return None, f"matched parcel {root_pid} but it has no recorded structure area"

    return {
        "comp_sqft": detail["comp_sqft"],
        "comp_sqft_source": "WV Assessment (verified)",
        "comp_sqft_source_url": detail["source_url"],
        "comp_sqft_quality": "County record (WV Real Estate Assessment)",
        "comp_sqft_note": f"Matched parcel {detail.get('parcel_id_formatted') or root_pid}.",
        "tax_county": county_name,
    }, None


def run(limit: int = None):
    session = wv_assessment.new_session()
    rows = candidates()
    if limit:
        rows = rows[:limit]
    print(f"{len(rows)} past_auctions rows need square footage enrichment")

    updated = 0
    unresolved = []
    for row in rows:
        try:
            fields, reason = resolve_one(session, row)
        except Exception as e:  # noqa: BLE001
            fields, reason = None, f"error: {e}"

        if fields:
            supabase_client.update_by_id("past_auctions", row["id"], fields)
            updated += 1
        else:
            unresolved.append(f"{row.get('address')}, {row.get('city')}: {reason}")

    print(f"Enriched {updated} rows; {len(unresolved)} left unresolved")

    supabase_client.log_run(
        "sqft_enrichment",
        records_found=len(rows),
        records_updated=updated,
        errors="; ".join(unresolved[:20]) if unresolved else None,
    )
    return {"checked": len(rows), "updated": updated, "unresolved": unresolved}


if __name__ == "__main__":
    outcome = run()
    sys.exit(0)  # never fail the Action on individual unresolved parcels
