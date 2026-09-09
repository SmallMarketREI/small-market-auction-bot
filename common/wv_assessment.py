"""WV Property Viewer / WV Real Estate Assessment client.

Recon findings (confirmed 2026-09-06 via browser network inspection):

  * mapwv.gov/parcel (the "WV Property Viewer" map) is powered by a public Esri
    ArcGIS REST service: services.wvgis.wvu.edu/arcgis/rest/services/
    Planning_Cadastre/WV_Parcels/MapServer. Layer 0 ("WVParcels") holds one
    polygon per parcel with County / Dist(rict) / Map / Parcel / acreage /
    owner / address fields -- but NOT building square footage.
  * Square footage lives in the separate WV Real Estate Assessment app
    (mapwv.gov/assessment), which is county CAMA data. Its search page
    (Assessment.aspx) is a classic ASP.NET WebForms postback (no JSON API),
    but once you have a match it links to a clean, static, GET-able detail
    page: mapwv.gov/Assessment/Detail/?PID={RootParcelID}, which contains a
    "Sum of Structure Areas" figure -- confirmed to match the auctioneer's own
    stated square footage in a live listing during recon (784 sq ft both
    places, parcel 20-19-004G-0071-0000, 1855 Oakhurst Dr, Charleston WV).

Pipeline used here: geocode nothing -- we already have lat/lng from the
BidWrangler listing. Query the ArcGIS parcel layer by point to get the
authoritative County/District/Map/Parcel (cross-checked against whatever the
auction description itself stated), then drive the Assessment.aspx search by
County+Map+Parcel to get the Root Parcel ID, then GET the Detail page.
"""
import re

import requests
from bs4 import BeautifulSoup

from . import config

ARCGIS_PARCELS_URL = (
    "https://services.wvgis.wvu.edu/arcgis/rest/services/Planning_Cadastre/"
    "WV_Parcels/MapServer/0/query"
)
ASSESSMENT_SEARCH_URL = "https://mapwv.gov/assessment/Assessment.aspx"
# Confirmed 2026-09-06 via live browser testing: mapwv.gov also runs this same
# search off a plain GET query string (no ".aspx", capital-A "Assessment"),
# e.g. mapwv.gov/assessment/Assessment?Counties=3&Map=21&Parcel=6 -- and
# returns the identical results markup _parse_search_results already expects.
# This replaced an earlier approach that POSTed a simulated copy of the
# ASP.NET webforms search page (reading its viewstate, filling hidden fields,
# etc.) -- that approach silently failed to match ANY real parcel in
# production even when the county/map/parcel values were correct, while this
# GET form matched every one of the same inputs when tested live. Simpler and
# it actually works, so there's no reason to keep the old POST/viewstate path
# around.
ASSESSMENT_SEARCH_QUERY_URL = "https://mapwv.gov/assessment/Assessment"
ASSESSMENT_DETAIL_URL = "https://mapwv.gov/Assessment/Detail/"

# From the Assessment Search county dropdown (captured 2026-09-06). WV has 55
# counties and this list does not change.
COUNTY_NAME_TO_CODE = {
    "Barbour": 1, "Berkeley": 2, "Boone": 3, "Braxton": 4, "Brooke": 5,
    "Cabell": 6, "Calhoun": 7, "Clay": 8, "Doddridge": 9, "Fayette": 10,
    "Gilmer": 11, "Grant": 12, "Greenbrier": 13, "Hampshire": 14, "Hancock": 15,
    "Hardy": 16, "Harrison": 17, "Jackson": 18, "Jefferson": 19, "Kanawha": 20,
    "Lewis": 21, "Lincoln": 22, "Logan": 23, "Marion": 24, "Marshall": 25,
    "Mason": 26, "McDowell": 27, "Mercer": 28, "Mineral": 29, "Mingo": 30,
    "Monongalia": 31, "Monroe": 32, "Morgan": 33, "Nicholas": 34, "Ohio": 35,
    "Pendleton": 36, "Pleasants": 37, "Pocahontas": 38, "Preston": 39,
    "Putnam": 40, "Raleigh": 41, "Randolph": 42, "Ritchie": 43, "Roane": 44,
    "Summers": 45, "Taylor": 46, "Tucker": 47, "Tyler": 48, "Upshur": 49,
    "Wayne": 50, "Webster": 51, "Wetzel": 52, "Wirt": 53, "Wood": 54,
    "Wyoming": 55,
}

# The reverse of the above. Confirmed necessary in production 2026-09: the
# ArcGIS parcel layer's COUNTY attribute actually comes back as this same
# numeric code (e.g. '40', '06'), not a spelled-out name -- so enrich_sqft_wv.py
# needs to go both directions depending on what a given response shape hands it.
COUNTY_CODE_TO_NAME = {code: name for name, code in COUNTY_NAME_TO_CODE.items()}


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT})
    return s


def lookup_parcel_by_latlng(session: requests.Session, lat: float, lng: float, address_hint: str = None):
    """Point-in-polygon query against the statewide parcel layer. Returns a
    dict with County/Dist/Map/Parcel/CleanParcelID/FullPhysicalAddress, or
    None if no parcel could be found even after widening the search.

    A listing's lat/lng sometimes lands just outside its own parcel's polygon
    (rounding, or a pin dropped near a driveway/road edge) -- confirmed in
    production 2026-09 for several real addresses that have coordinates but
    still missed on an exact-point query. So this tries the exact point
    first, then retries with a small buffer (ArcGIS Server supports this via
    the distance/units params on a point query), widening twice before giving
    up. A buffered search can return several nearby parcels, not just one --
    when it does, `address_hint` (the auction's own stated address) is used
    to pick whichever candidate's own address text overlaps it best, rather
    than blindly taking the first result.
    """
    if lat is None or lng is None:
        return None
    base_params = {
        "f": "json",
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "COUNTY,Dist,Map,Parcel,CleanParcelID,FullPhysicalAddress",
        "returnGeometry": "false",
    }
    hint_tokens = set(re.findall(r"[a-z0-9]+", address_hint.lower())) if address_hint else set()

    for distance in (0, 50, 150):
        params = dict(base_params)
        if distance:
            params["distance"] = distance
            params["units"] = "esriSRUnit_Meter"
        resp = session.get(ARCGIS_PARCELS_URL, params=params, timeout=20)
        resp.raise_for_status()
        features = resp.json().get("features") or []
        if not features:
            continue
        if len(features) == 1 or not hint_tokens:
            return features[0].get("attributes")

        def _overlap(feature):
            addr = (feature.get("attributes", {}).get("FullPhysicalAddress") or "").lower()
            return len(hint_tokens & set(re.findall(r"[a-z0-9]+", addr)))

        return max(features, key=_overlap).get("attributes")
    return None


_STREET_SUFFIX_WORDS = {
    "RD", "ROAD", "DR", "DRIVE", "LN", "LANE", "ST", "STREET", "AVE", "AVENUE",
    "HWY", "HIGHWAY", "BLVD", "BOULEVARD", "CIR", "CIRCLE", "CT", "COURT",
    "PL", "PLACE", "WAY", "TRL", "TRAIL", "LOOP", "PIKE", "RUN", "PATH",
    "ROW", "SQ", "SQUARE", "TER", "TERRACE", "EXT", "EXTENSION", "ALY",
    "ALLEY", "XING", "CROSSING",
}


def _parse_house_number_and_street(address):
    """'2998 Owl Creek Road' -> ('2998', 'OWL CREEK'). Strips a single
    trailing street-type suffix word (Road/Dr/Ln/...) since a listing's
    wording and this layer's own FullPhysicalAddress text don't always agree
    on it -- confirmed live 2026-09-09: an auction listed "111 Mylan Park
    Dr", but the county's own record for that road is "Mylan Park LN", not
    "Dr". Stripping the suffix and matching with a trailing wildcard (see
    lookup_parcel_by_address_text) survives that kind of disagreement.

    Returns (None, None) for a bare street name or a "0 [Street]" vacant-land
    placeholder -- neither has a real house number to anchor an exact match
    on, and guessing which nearby parcel it might be isn't this function's
    job.
    """
    if not address:
        return None, None
    text = re.sub(r"[.,]", "", address.strip().upper())
    m = re.match(r"^(\d+[A-Z]?)\s+(.+)$", text)
    if not m:
        return None, None
    house_number, rest = m.group(1), m.group(2).strip()
    if house_number == "0":
        return None, None
    words = rest.split()
    if words and words[-1] in _STREET_SUFFIX_WORDS:
        words = words[:-1]
    if not words:
        return None, None
    return house_number, " ".join(words)


def lookup_parcel_by_address_text(session: requests.Session, address: str):
    """Fallback for a row with a real house number but no lat/lng yet --
    including addresses the Census geocoder can't match because its
    TIGER/Line data has no address range for that road at all (confirmed
    live 2026-09-09 for genuine, well-formed rural WV addresses like "2998
    Owl Creek Road, Morgantown" and "670 Walker Ridge Rd, Walton" -- both
    zero matches from Census despite being real numbered addresses).

    Queries this same statewide parcel layer (see ARCGIS_PARCELS_URL above)
    by address TEXT instead of a point -- confirmed live that
    FullPhysicalAddress supports a real attribute (WHERE-clause) query, not
    just point/polygon lookups. This layer is sourced from county
    assessor/GIS data, which sometimes has rural addresses TIGER doesn't.

    Only ever returns an EXACT house-number match on the parsed street name
    -- never the nearest one -- to keep the same "flag it, don't guess"
    discipline as the rest of this pipeline. Confirmed live against 5 real
    addresses stuck in the "No coordinates" review bucket: 2/5 had an exact
    match in county data (424 Dusty Field Dr and 5211 Aarons Fork Rd, both
    Kanawha County) -- the other 3/5 genuinely don't exist in county records
    under that house number either (closest on file for "2998 Owl Creek Rd"
    is 3011/3001/3330/3368/3388; for "670 Walker Ridge Rd" is
    1904/2215/2291). So this recovers a real minority, not everything, and
    it's expected to keep returning None for the rest.

    Returns {"lat", "lng", "county", "map", "parcel", "matched_address"} on
    an exact, unambiguous match, or None.
    """
    house_number, street = _parse_house_number_and_street(address)
    if not house_number or not street:
        return None

    escaped_street = street.replace("'", "''")
    where = f"UPPER(FullPhysicalAddress) LIKE '{house_number} {escaped_street}%'"
    params = {
        "f": "json",
        "where": where,
        "outFields": "COUNTY,Dist,Map,Parcel,FullPhysicalAddress",
        "returnGeometry": "true",
        "outSR": 4326,
    }
    resp = session.get(ARCGIS_PARCELS_URL, params=params, timeout=20)
    resp.raise_for_status()
    features = resp.json().get("features") or []

    exact = []
    for feat in features:
        addr = (feat.get("attributes", {}).get("FullPhysicalAddress") or "").strip().upper()
        addr_number = addr.split(" ", 1)[0] if addr else ""
        if addr_number == house_number:
            exact.append(feat)

    if not exact:
        return None

    # More than one feature can carry the same house number (a parcel split
    # across rings, or two genuinely different parcels sharing a mailing
    # address) -- if they don't all agree on County+Map+Parcel, that's real
    # ambiguity, and this returns nothing rather than guessing which one.
    keys = {
        (f["attributes"].get("COUNTY"), f["attributes"].get("Map"), f["attributes"].get("Parcel"))
        for f in exact
    }
    if len(keys) > 1:
        return None

    attrs = exact[0]["attributes"]
    lat = lng = None
    rings = (exact[0].get("geometry") or {}).get("rings")
    if rings and rings[0]:
        pts = rings[0][:-1] if len(rings[0]) > 1 else rings[0]  # drop the closing duplicate point
        if pts:
            lng = sum(p[0] for p in pts) / len(pts)
            lat = sum(p[1] for p in pts) / len(pts)

    return {
        "lat": lat,
        "lng": lng,
        "county": attrs.get("COUNTY"),
        "map": attrs.get("Map"),
        "parcel": attrs.get("Parcel"),
        "matched_address": attrs.get("FullPhysicalAddress"),
    }


def search_assessment(session: requests.Session, county_code, map_=None, parcel=None,
                       street_name=None):
    """Search the WV Assessment database via its GET-able query-string search
    (see ASSESSMENT_SEARCH_QUERY_URL above). Returns a list of result dicts,
    each with the parsed grid columns plus 'root_pid' (the Detail-page id)
    when a link could be found for that row.
    """
    params = {"Counties": county_code}
    if map_:
        params["Map"] = map_
    if parcel:
        params["Parcel"] = parcel
    if street_name:
        params["StreetName"] = street_name
    resp = session.get(ASSESSMENT_SEARCH_QUERY_URL, params=params, timeout=20)
    resp.raise_for_status()
    return _parse_search_results(resp.text)


def _parse_search_results(html: str):
    soup = BeautifulSoup(html, "lxml")
    grid = None
    for table in soup.find_all("table"):
        header_cells = table.find("tr")
        if header_cells and "Root Parcel ID" in header_cells.get_text():
            grid = table
            break
    if grid is None:
        return []

    rows = grid.find_all("tr")
    if len(rows) < 2:
        return []
    headers = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
    results = []
    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        row = {headers[i]: cells[i].get_text(strip=True) for i in range(min(len(headers), len(cells)))}
        m = re.search(r"Assessment/Detail/\?PID=(\d+)", str(tr))
        row["root_pid"] = m.group(1) if m else None
        results.append(row)
    return results


def _parse_float(value):
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def select_best_match(results, parcel=None, district=None, acreage=None):
    """When a Map+Parcel search returns more than one row, pick the one that
    actually matches -- instead of blindly taking the first, which is what
    every caller used to do.

    This matters because the search is a substring/prefix match, not exact:
    confirmed live 2026-09-07 that county=24 (Marion) map=24 parcel=33
    returns 13 distinct properties -- every "33.0" through "33.10"
    sub-parcel -- spanning 3 different districts, from a 27-acre farm tract
    with no structure to an unrelated $426,900 house two sub-parcels over.
    An auctioneer listing that states a bare "Parcel 33" (no decimal) means
    the root sub-parcel "33.0", but the site's own result order isn't
    ranked by that at all, so results[0] can land on any sibling.

    Scoring, most specific first:
      1. Exact numeric Parcel match (so a bare "33" prefers "33.0" over
         "33.5") -- by far the strongest signal, since it directly encodes
         which of the sub-parcels this is.
      2. Same District (raw map/parcel numbers repeat across districts --
         confirmed live: three separate "33.0" parcels in three districts).
      3. Closest Deeded Acres to the auction listing's own stated acreage,
         when both are available -- distinguishes siblings that share both
         parcel number and district.
    Falls back to the first result when nothing scores higher than anything
    else, matching the old behavior for the common single-result case.
    """
    if not results:
        return None
    if len(results) == 1:
        return results[0]

    target_parcel = _parse_float(parcel)
    target_district = None
    if district is not None:
        m = re.search(r"\d+", str(district))
        if m:
            target_district = int(m.group())
    target_acreage = _parse_float(acreage)

    def _score(r):
        score = 0
        r_parcel = _parse_float(r.get("Parcel"))
        if target_parcel is not None and r_parcel is not None and abs(r_parcel - target_parcel) < 0.001:
            score += 100
        if target_district is not None:
            dm = re.search(r"\d+", str(r.get("District") or ""))
            if dm and int(dm.group()) == target_district:
                score += 10
        if target_acreage is not None:
            r_acres = _parse_float(r.get("Deeded Acres"))
            if r_acres is not None:
                diff = abs(r_acres - target_acreage)
                if diff < 0.05:
                    score += 5
                elif target_acreage and diff < target_acreage * 0.05 + 0.05:
                    score += 2
        return score

    return max(results, key=_score)


def _parse_money(value):
    """'$91,900' -> 91900.0, '---' / '' / None -> None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in ("---", "-", "N/A"):
        return None
    try:
        return float(text.replace("$", "").replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_int(value):
    parsed = _parse_money(value)
    return int(parsed) if parsed is not None else None


def _table_with_header(soup, *required_substrings):
    """First <table> whose header row's text contains every given substring."""
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        header_text = header_row.get_text(" ", strip=True)
        if all(s in header_text for s in required_substrings):
            return table
    return None


def _table_data_rows(table):
    """[{header: cell_text, ...}, ...] for every row after the header row."""
    rows = table.find_all("tr")
    if len(rows) < 2:
        return []
    headers = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
    out = []
    for tr in rows[1:]:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        out.append({headers[i]: cells[i].get_text(strip=True) for i in range(min(len(headers), len(cells)))})
    return out


def _parse_paired_value_table(table):
    """The 'Cost Value / Appraisal Value' table is laid out as two label:value
    pairs side by side per row (4 cells: label, value, label, value), not a
    plain header+rows grid -- e.g. one row reads
    'Dwelling Value | $66,400 | Land Appraisal | $25,500'. Flatten both pairs
    from every row after the header into one label -> value dict."""
    out = {}
    rows = table.find_all("tr")
    for tr in rows[1:]:
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) >= 2 and cells[0]:
            out[cells[0]] = cells[1]
        if len(cells) >= 4 and cells[2]:
            out[cells[2]] = cells[3]
    return out


def get_assessment_detail(session: requests.Session, root_pid: str):
    """Fetch mapwv.gov/Assessment/Detail/?PID=... and pull out the fields we
    care about. Returns a dict; comp_sqft (and every other value field) is
    None if not present on the page -- e.g. a vacant-land parcel has no
    building cards at all, so year_built/bedrooms/baths stay None, but its
    Deeded Acres and Land Appraisal are still there and still worth capturing.

    Confirmed live 2026-09-08 against both a residential parcel (Kanawha
    20-19-004G-0071-0000, 1 building) and a vacant-land parcel (Raleigh
    41-07-0001-0020-0000, 0 buildings) -- same table layout in both cases,
    just with empty/zeroed building fields on the land one.
    """
    resp = session.get(ASSESSMENT_DETAIL_URL, params={"PID": root_pid}, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    # Plain 2-cell label:value rows, scattered across several tables
    # (Property Location, Building Information summary, etc).
    labels = {}
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) == 2:
                label = cells[0].get_text(strip=True)
                value = cells[1].get_text(strip=True)
                if label and label not in labels:
                    labels[label] = value

    sqft = None
    if labels.get("Sum of Structure Areas"):
        try:
            sqft = float(labels["Sum of Structure Areas"].replace(",", ""))
        except ValueError:
            sqft = None

    # "General Information" table: Tax Class / Book-Page / Deeded Acres /
    # Calculated Acres / Legal Description -- one header row, one data row.
    deeded_acres = calculated_acres = None
    general_table = _table_with_header(soup, "Deeded Acres")
    general_rows = _table_data_rows(general_table) if general_table else []
    if general_rows:
        deeded_acres = _parse_money(general_rows[0].get("Deeded Acres"))
        calculated_acres = _parse_money(general_rows[0].get("Calculated Acres"))

    # "Cost Value / Appraisal Value" table -- see _parse_paired_value_table.
    land_value = building_value = total_appraisal = None
    appraisal_table = _table_with_header(soup, "Appraisal Value")
    if appraisal_table:
        appraisal = _parse_paired_value_table(appraisal_table)
        land_value = _parse_money(appraisal.get("Land Appraisal"))
        building_value = _parse_money(appraisal.get("Building Appraisal"))
        total_appraisal = _parse_money(appraisal.get("Total Appraisal"))

    if sqft is None:
        # Fallback: sum the "Square Footage (SFLA)" column across the
        # per-building "Card" table(s) if the summary label wasn't present.
        sqft_table = _table_with_header(soup, "Square Footage (SFLA)")
        if sqft_table:
            total = 0.0
            found_any = False
            for row in _table_data_rows(sqft_table):
                val = _parse_money(row.get("Square Footage (SFLA)"))
                if val is not None:
                    total += val
                    found_any = True
            if found_any:
                sqft = total

    # Per-building "Card" table with Year Built / Bedrooms / Full Baths /
    # Half Baths -- one row per building. A multi-building parcel (a house
    # plus a converted garage apartment, say) gets summed bedrooms/baths and
    # the EARLIEST year_built across cards, on the theory that the original
    # structure's construction year is the more useful comp figure than
    # whichever card happens to list last. Absent entirely (0 buildings, e.g.
    # vacant land) -> all three stay None rather than 0, so they read as "not
    # on record" rather than "confirmed zero bedrooms".
    year_built = bedrooms = full_baths = half_baths = None
    card_table = _table_with_header(soup, "Bedrooms", "Full Baths")
    card_rows = _table_data_rows(card_table) if card_table else []
    if card_rows:
        years = [y for y in (_parse_int(r.get("Year Built")) for r in card_rows) if y]
        if years:
            year_built = min(years)
        bed_vals = [b for b in (_parse_int(r.get("Bedrooms")) for r in card_rows) if b is not None]
        if bed_vals:
            bedrooms = sum(bed_vals)
        full_vals = [b for b in (_parse_int(r.get("Full Baths")) for r in card_rows) if b is not None]
        if full_vals:
            full_baths = sum(full_vals)
        half_vals = [b for b in (_parse_int(r.get("Half Baths")) for r in card_rows) if b is not None]
        if half_vals:
            half_baths = sum(half_vals)

    return {
        "root_pid": root_pid,
        "parcel_id_formatted": labels.get("Parcel ID"),
        "physical_address": labels.get("Physical Address"),
        "owner": labels.get("Owner(s)"),
        "comp_sqft": sqft,
        # e.g. "R-Residential", "X-Exempt", "C-Commercial" -- used to flag a
        # matched parcel that doesn't actually look like the home the auction
        # was for (see enrich_sqft_wv.py's review_reason logic).
        "property_class": labels.get("Property Class"),
        "deeded_acres": deeded_acres,
        "calculated_acres": calculated_acres,
        "land_value": land_value,
        "building_value": building_value,
        "total_appraisal": total_appraisal,
        "year_built": year_built,
        "bedrooms": bedrooms,
        "full_baths": full_baths,
        "half_baths": half_baths,
        "source_url": f"{ASSESSMENT_DETAIL_URL}?PID={root_pid}",
    }
