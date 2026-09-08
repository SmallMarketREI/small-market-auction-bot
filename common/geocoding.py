"""Address -> lat/lng, via the US Census Bureau's public geocoder.

Why the Census geocoder specifically: it's free, requires no API key or
account, has no documented rate limit for reasonable/polite use, and --
most importantly for feeding WV Assessment's point-in-parcel lookup (see
wv_assessment.lookup_parcel_by_latlng) -- it only ever returns a match when
it found a real, numbered address range to match against. That precision
matters here: a looser "approximate street location" geocoder (e.g. a plain
OpenStreetMap/Nominatim search on just the street name) can return a point
a mile or more from the actual parcel on a long street, which risks feeding
enrich_sqft_wv.py a coordinate that lands inside the WRONG neighboring
parcel instead of just failing cleanly. Census either matches confidently
or returns nothing -- no guessing.

The real limitation this creates: any listing whose only "address" is a
placeholder house number (this auctioneer commonly writes vacant land as
"0 Some Road, City, WV" when there's no real street number) will never
match here, because Census needs a real numbered range to match against.
Deliberately not adding a fallback for those right now -- see
geocode_addresses.py's module docstring.
"""
import time

import requests

from . import config

_GEOCODER_BASE = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"

# Being a good citizen against a shared federal public service we now hit
# routinely (once a day, plus whenever the backlog is worked through) --
# same spirit as config.REQUEST_DELAY_SECONDS for the auction sites.
REQUEST_DELAY_SECONDS = 0.5


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT, "Accept": "application/json"})
    return s


def build_oneline_address(address: str, city: str = None, state: str = None, zip_code: str = None) -> str:
    """Assembles "STREET, CITY, STATE ZIP" for the Census one-line endpoint,
    omitting whichever parts are missing rather than passing the literal
    string "None" through. Returns "" if there's no street address at all --
    callers should skip geocoding entirely in that case."""
    address = (address or "").strip()
    if not address:
        return ""
    parts = [address]
    tail = ", ".join(p for p in (city or "", state or "") if p.strip())
    if tail:
        parts.append(tail + (f" {zip_code.strip()}" if zip_code and zip_code.strip() else ""))
    elif zip_code and zip_code.strip():
        parts.append(zip_code.strip())
    return ", ".join(parts)


def geocode(session: requests.Session, address: str, city: str = None, state: str = None,
            zip_code: str = None) -> dict | None:
    """Returns {"lat": float, "lng": float, "matched_address": str} on a
    confident match, or None if the Census geocoder couldn't match this
    address at all (most commonly: no real house number to match against --
    see the module docstring). Raises requests exceptions on a genuine
    network/service failure so the caller can distinguish "no match" from
    "couldn't check.\""""
    one_line = build_oneline_address(address, city, state, zip_code)
    if not one_line:
        return None

    resp = session.get(
        _GEOCODER_BASE,
        params={"address": one_line, "benchmark": "Public_AR_Current", "format": "json"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    matches = (data.get("result") or {}).get("addressMatches") or []
    if not matches:
        return None

    best = matches[0]
    coords = best.get("coordinates") or {}
    lat, lng = coords.get("y"), coords.get("x")
    if lat is None or lng is None:
        return None
    return {"lat": lat, "lng": lng, "matched_address": best.get("matchedAddress")}
