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


def get_assessment_detail(session: requests.Session, root_pid: str):
    """Fetch mapwv.gov/Assessment/Detail/?PID=... and pull out the fields we
    care about. Returns a dict; comp_sqft is None if not found on the page."""
    resp = session.get(ASSESSMENT_DETAIL_URL, params={"PID": root_pid}, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

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

    if sqft is None:
        # Fallback: sum the "Square Footage (SFLA)" column across the
        # per-building "Card" table(s) if the summary label wasn't present.
        for table in soup.find_all("table"):
            header_text = table.find("tr")
            if header_text and "Square Footage (SFLA)" in header_text.get_text():
                idx = None
                header_cells = [c.get_text(strip=True) for c in header_text.find_all(["th", "td"])]
                if "Square Footage (SFLA)" in header_cells:
                    idx = header_cells.index("Square Footage (SFLA)")
                if idx is not None:
                    total = 0.0
                    found_any = False
                    for tr in table.find_all("tr")[1:]:
                        cells = tr.find_all(["td", "th"])
                        if len(cells) > idx:
                            try:
                                total += float(cells[idx].get_text(strip=True).replace(",", ""))
                                found_any = True
                            except ValueError:
                                pass
                    if found_any:
                        sqft = total
                break

    return {
        "root_pid": root_pid,
        "parcel_id_formatted": labels.get("Parcel ID"),
        "physical_address": labels.get("Physical Address"),
        "owner": labels.get("Owner(s)"),
        "comp_sqft": sqft,
        "total_appraisal": labels.get("Total Appraisal"),
        # e.g. "R-Residential", "X-Exempt", "C-Commercial" -- used to flag a
        # matched parcel that doesn't actually look like the home the auction
        # was for (see enrich_sqft_wv.py's review_reason logic).
        "property_class": labels.get("Property Class"),
        "source_url": f"{ASSESSMENT_DETAIL_URL}?PID={root_pid}",
    }
