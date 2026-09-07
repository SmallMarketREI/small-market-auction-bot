#!/usr/bin/env python3
"""Daily job: fill in official square footage for past_auctions rows that are
missing it (or only have the auctioneer's stated figure), using the WV Real
Estate Assessment county record. Also maintains review_flag/review_reason on
every row it touches, so the dashboard's "Needs Review" tab has something
concrete to show: missing square footage, a stated-vs-county size mismatch,
or a matched parcel that doesn't look residential.

Pipeline per row:
  1. Use the listing's lat/lng (captured by scrape_past_sales.py) to query the
     statewide ArcGIS parcel layer for the authoritative County/District/Map/
     Parcel -- widening the search area in a couple of steps if the exact
     point doesn't land inside a mapped parcel (see wv_assessment.py), and
     preferring whichever nearby parcel's own address text best matches the
     listing's stated address when more than one comes back.
  2. Search mapwv.gov's WV Assessment app by County + Map + Parcel to resolve
     the Root Parcel ID.
  3. Fetch that parcel's Assessment Detail page and read "Sum of Structure
     Areas" plus its Property Class (used for the residential-match check).

Rows outside West Virginia are recognized and flagged plainly rather than
run through a lookup that can never work for them.
"""
import sys

from common import supabase_client, wv_assessment

# Auctioneer-stated vs. WV Assessment verified sqft differing by more than
# this fraction gets flagged for a human to double check (typo in the
# listing, a mismatched parcel, or a genuinely renovated/expanded structure
# the county record hasn't caught up to yet).
CONFLICT_THRESHOLD = 0.20

_SELECT_FIELDS = (
    "id,city,state,zip,address,lat,lng,tax_district,tax_map,tax_parcel,acreage,"
    "comp_sqft,comp_sqft_source,property_type"
)


def candidates():
    """Rows worth (re-)checking: no comp_sqft yet, or comp_sqft came only from
    the auctioneer's listing copy rather than a verified county record."""
    seen = {}
    missing = supabase_client.select(
        "past_auctions", {"select": _SELECT_FIELDS, "comp_sqft": "is.null"},
    )
    for r in missing:
        seen[r["id"]] = r

    stated_only = supabase_client.select(
        "past_auctions",
        {"select": _SELECT_FIELDS, "comp_sqft_source": "eq.Auction listing (auctioneer-stated)"},
    )
    for r in stated_only:
        seen[r["id"]] = r

    return list(seen.values())


def resolve_one(session, row):
    """Returns (update_fields, log_reason).

    update_fields is always a dict to write back to the row -- it always sets
    review_flag/review_reason (even on failure, so the Needs Review tab has
    something concrete to show), and additionally carries comp_sqft/etc. on
    success. log_reason is a short string for scrape_runs' internal error
    log, or None when everything resolved cleanly with nothing to flag.
    """
    state = (row.get("state") or "").strip().upper()
    if state and state not in ("WV", "WEST VIRGINIA"):
        reason = "Outside West Virginia -- the WV Assessment lookup doesn't apply to this property"
        return {"review_flag": True, "review_reason": reason}, reason

    lat, lng = row.get("lat"), row.get("lng")
    raw_county = map_ = parcel = None

    if lat and lng:
        arcgis = wv_assessment.lookup_parcel_by_latlng(session, lat, lng, address_hint=row.get("address"))
        if arcgis:
            raw_county = arcgis.get("COUNTY")
            # Prefer the row's own already-parsed tax map/parcel (captured
            # directly from the auctioneer's own listing text for THIS
            # specific parcel -- see parsing_utils.parse_tax_reference) over
            # the ArcGIS point lookup's guess. This matters most for
            # multi-parcel auctions (common/bidwrangler.py's
            # build_multi_parcel_row): every parcel in one auction shares the
            # SAME auction-level lat/lng, since individual per-parcel
            # coordinates usually aren't available -- so an ArcGIS point
            # lookup can resolve several genuinely different parcels to the
            # same nearby record. Confirmed in production 2026-09-07: two
            # distinct land tracts from one auction (tax parcels 33 and 33.4)
            # both got matched to one shared house record via this path when
            # the ArcGIS guess was allowed to override the already-correct
            # per-parcel tax reference.
            map_ = row.get("tax_map") or arcgis.get("Map")
            parcel = row.get("tax_parcel") or arcgis.get("Parcel")

    if not map_:
        map_ = row.get("tax_map")
    if not parcel:
        parcel = row.get("tax_parcel")

    if not raw_county:
        reason = "No coordinates on file, or the point didn't land in a mapped WV parcel"
        return {"review_flag": True, "review_reason": reason}, reason

    # Confirmed in production 2026-09: the ArcGIS parcel layer's COUNTY field
    # comes back as WV's numeric county code (e.g. '40'), not a spelled-out
    # name -- handle both shapes in case that ever changes.
    raw_county_str = str(raw_county).strip()
    if raw_county_str.isdigit():
        county_code = int(raw_county_str)
        county_name = wv_assessment.COUNTY_CODE_TO_NAME.get(county_code, raw_county_str)
    else:
        county_name = raw_county_str.title()
        county_code = wv_assessment.COUNTY_NAME_TO_CODE.get(county_name)

    if not county_code:
        reason = f"Unrecognized county from the state map service: {raw_county!r}"
        return {"review_flag": True, "review_reason": reason}, reason

    if not (map_ and parcel):
        reason = "No tax map/parcel number available to search with"
        return {"review_flag": True, "review_reason": reason, "tax_county": county_name}, reason

    results = wv_assessment.search_assessment(session, county_code, map_=map_, parcel=parcel)
    if not results:
        reason = f"No WV Assessment match for county={county_code} map={map_} parcel={parcel}"
        return {"review_flag": True, "review_reason": reason, "tax_county": county_name}, reason

    # The search is a substring/prefix match, not exact -- a bare "Parcel 33"
    # can return a dozen sibling sub-parcels (33.0, 33.1, ... 33.10) across
    # multiple districts. Score them instead of blindly trusting result
    # order (see wv_assessment.select_best_match's docstring for the
    # 2026-09-07 production case that exposed this).
    best = wv_assessment.select_best_match(
        results, parcel=parcel, district=row.get("tax_district"), acreage=row.get("acreage"),
    )
    root_pid = best.get("root_pid") if best else None
    if not root_pid:
        reason = "Matched a record but couldn't find its detail-page id"
        return {"review_flag": True, "review_reason": reason, "tax_county": county_name}, reason

    detail = wv_assessment.get_assessment_detail(session, root_pid)
    property_class = detail.get("property_class")

    if not detail.get("comp_sqft"):
        reason = f"Matched parcel {detail.get('parcel_id_formatted') or root_pid}, but it has no recorded structure area"
        return {
            "review_flag": True,
            "review_reason": reason,
            "tax_county": county_name,
            "property_class": property_class,
        }, reason

    fields = {
        "comp_sqft": detail["comp_sqft"],
        "comp_sqft_source": "WV Assessment (verified)",
        "comp_sqft_source_url": detail["source_url"],
        "comp_sqft_quality": "County record (WV Real Estate Assessment)",
        "comp_sqft_note": f"Matched parcel {detail.get('parcel_id_formatted') or root_pid}.",
        "tax_county": county_name,
        "property_class": property_class,
    }

    review_reason = None
    if row.get("property_type") == "House" and property_class and "residential" not in property_class.lower():
        review_reason = (
            f"Matched parcel's property class is '{property_class}', which doesn't look "
            "residential -- may be the wrong parcel"
        )

    prior_sqft = row.get("comp_sqft")
    if prior_sqft and row.get("comp_sqft_source") == "Auction listing (auctioneer-stated)":
        diff = abs(detail["comp_sqft"] - prior_sqft) / prior_sqft
        if diff > CONFLICT_THRESHOLD:
            conflict_note = (
                f"Auctioneer listed {prior_sqft:g} sq ft; WV Assessment shows "
                f"{detail['comp_sqft']:g} sq ft ({diff * 100:.0f}% difference)"
            )
            review_reason = f"{review_reason}; {conflict_note}" if review_reason else conflict_note

    fields["review_flag"] = bool(review_reason)
    fields["review_reason"] = review_reason
    return fields, review_reason


def run(limit: int = None):
    session = wv_assessment.new_session()
    rows = candidates()
    if limit:
        rows = rows[:limit]
    print(f"{len(rows)} past_auctions rows need square footage enrichment")

    updated = 0
    flagged = 0
    logged_reasons = []
    for row in rows:
        try:
            fields, log_reason = resolve_one(session, row)
        except Exception as e:  # noqa: BLE001
            fields = {"review_flag": True, "review_reason": f"Enrichment error: {e}"}
            log_reason = f"error: {e}"

        supabase_client.update_by_id("past_auctions", row["id"], fields)
        if fields.get("comp_sqft"):
            updated += 1
        if fields.get("review_flag"):
            flagged += 1
        if log_reason:
            logged_reasons.append(f"{row.get('address')}, {row.get('city')}: {log_reason}")

    print(f"Enriched {updated} rows with verified sqft; {flagged} flagged for review")

    supabase_client.log_run(
        "sqft_enrichment",
        records_found=len(rows),
        records_updated=updated,
        errors="; ".join(logged_reasons[:20]) if logged_reasons else None,
    )
    return {"checked": len(rows), "updated": updated, "flagged": flagged}


if __name__ == "__main__":
    outcome = run()
    sys.exit(0)  # never fail the Action on individual unresolved parcels
