import re

# Keyword-based property type guess, used because Pyle's real-estate auctions
# aren't all houses (the historical data includes Land, Commercial, Mobile
# Home, and similar) -- hardcoding "House" for everything breaks "same
# property type" comp matching for anything that isn't. Checked in this order
# (most specific first) against the auction's own title + description.
_MOBILE_HOME_KEYWORDS = (
    "mobile home", "manufactured home", "singlewide", "single wide", "doublewide", "double wide",
)
_COMMERCIAL_KEYWORDS = (
    "commercial", "retail space", "office building", "warehouse", "storefront",
    "restaurant building", "gas station", "auto shop", "church building", "industrial",
)
_HOUSE_KEYWORDS = (
    "bedroom", "bathroom", "single family", "ranch home", "cape cod", "colonial",
    "duplex", "townhouse", "condo",
)
_LAND_KEYWORDS = (
    "vacant land", "vacant lot", "building lot", "buildable lot", "wooded lot", "acreage",
    "tract of land", "unimproved lot",
)


def guess_property_type(name: str, description: str) -> str:
    """Best-effort property type from an auction's title + description text.
    Defaults to "House" when nothing distinctive is found, since that's still
    the overwhelming majority of what this auctioneer lists -- matches the
    prior hardcoded behavior for the common case, while actually detecting
    the exceptions instead of mislabeling them."""
    text = f"{name or ''} {description or ''}".lower()
    if any(k in text for k in _MOBILE_HOME_KEYWORDS):
        return "Mobile Home"
    if any(k in text for k in _COMMERCIAL_KEYWORDS):
        return "Commercial"
    if any(k in text for k in _HOUSE_KEYWORDS):
        return "House"
    if re.search(r"\b\d\s*(bed|br)\b", text):
        return "House"
    if any(k in text for k in _LAND_KEYWORDS):
        return "Land"
    return "House"


def parse_sqft_from_text(text: str):
    """Pull a stated square footage out of an auction description, e.g.
    '784 +/- Sq. Ft.' or '1,552 sq ft'. Returns a float or None."""
    if not text:
        return None
    m = re.search(r"([\d,]{2,6})\s*\+?/?-?\s*sq\.?\s*ft", text, re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_tax_reference(text: str):
    """Pull 'District 19, Map 4G, Parcel 71' (or similar orderings/spacing) out
    of a BidWrangler item description. Returns (district, map_, parcel) with
    any piece set to None if not found. This is WV Pyle-listing-specific
    phrasing observed in practice; tweak the patterns if the auctioneer changes
    their listing template."""
    if not text:
        return None, None, None
    district = None
    map_ = None
    parcel = None

    m = re.search(r"District\s+([A-Za-z0-9]+)", text, re.IGNORECASE)
    if m:
        district = m.group(1)
    m = re.search(r"Map\s+([A-Za-z0-9]+)", text, re.IGNORECASE)
    if m:
        map_ = m.group(1)
    m = re.search(r"Parcel\s+([A-Za-z0-9.]+)", text, re.IGNORECASE)
    if m:
        parcel = m.group(1).rstrip(".")

    return district, map_, parcel


def parse_acreage_from_text(text: str):
    """Pull a stated acreage out of listing text, e.g. '3.27+/- Acre Lot' or
    '58.93 +/- Acre'. Returns a float or None. Vacant land parcels (common in
    Pyle's multi-parcel auctions) usually have this instead of a usable
    square footage, so the dashboard can fall back to a $/acre figure."""
    if not text:
        return None
    # Pyle's own listings write the +/- tolerance both ways -- literal
    # "+/-" and the unicode "±" (confirmed live 2026-09-07: "27.16± Assessed
    # Acres" used only the unicode form, which the old plain-ASCII pattern
    # never matched, silently leaving a clearly-stated acreage as None.
    m = re.search(r"([\d,]{1,6}\.?\d*)\s*(?:\+/-|±)?\s*acres?\b", text, re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


_SUBJECT_PREFIX_RE = re.compile(
    # The parcel number itself is never parsed into anything (parcel_key
    # comes from the item's raw position in the auction, not this text), so
    # rather than spelling out every number word, this just accepts a short
    # alphanumeric token after "Subject" -- confirmed necessary live: a
    # 16-parcel auction used "Subject Eleven:" through "Subject Seventeen:",
    # which an earlier one/two/.../ten word-list silently failed to match
    # and dropped those parcels from every downstream table entirely.
    r"^subject\s*#?\s*[a-z0-9]{1,12}\s*:\s*",
    re.IGNORECASE,
)
# Matches "<street/area>, <city>, ST [ZIP]" allowing a comma, hyphen, or en/em
# dash as the street/city separator, since Pyle's own listings use all three
# inconsistently (observed live: "Big Ugly Rd E, Harts, WV 25524",
# "261 Ronda Road - Dry Branch, WV 25061", "Bufflick Run- Clendenin, WV").
_TRAILING_CITY_STATE_ZIP_RE = re.compile(
    r"^(?P<street>.*?)[,\-–—]\s*(?P<city>[A-Za-z .]+?),?\s*"
    r"(?P<state>WV|PA|OH|KY|VA|MD)\b\s*(?P<zip>\d{4,5})?\s*$"
)
# Fallback for when the address is buried in the item's DESCRIPTION rather
# than its name (observed live: item name "Subject #4: Stone Commercial
# Building on 0.4+/- Acres", with "288 E Grafton Rd Fairmont, WV" only
# appearing in the description text) -- requires a leading street number so
# it doesn't false-match on acreage/sqft figures elsewhere in the text.
_DESC_ADDRESS_RE = re.compile(
    r"(?P<street>\d{1,6}\s+[A-Za-z0-9'.# ]+)\s+(?P<city>[A-Za-z .]+?),\s*"
    r"(?P<state>WV|PA|OH|KY|VA|MD)\b\s*(?P<zip>\d{4,5})?"
)
_BUNDLE_NAME_RE = re.compile(r"propert(?:y|ies)\s+in\s+entiret", re.IGNORECASE)
_MINERAL_NAME_RE = re.compile(r"mineral\s+interests?", re.IGNORECASE)


def is_subject_item_name(name: str) -> bool:
    """True for a BidWrangler item name following Pyle's 'Subject N:' /
    'Subject #N:' convention, used only on multi-parcel real-estate
    auctions -- never observed on a personal-property multi-lot auction
    (those use names like 'Preview Information', '2003 Freightliner...')."""
    return bool(name and _SUBJECT_PREFIX_RE.match(name.strip()))


def is_bundle_item_name(name: str) -> bool:
    """True for a multi-parcel auction's 'buy it all as one lot' option (e.g.
    'Subject #5: Property in Entirety') -- not an individual parcel."""
    return bool(name and _BUNDLE_NAME_RE.search(name))


def is_mineral_interest_item_name(name: str) -> bool:
    """True for a mineral-rights-only line item (e.g. 'Subject #6: Mineral
    Interests') -- not a comparable piece of real estate, so it's excluded
    from past_auctions/watch_auctions entirely rather than mis-comped."""
    return bool(name and _MINERAL_NAME_RE.search(name))


def parse_subject_address(item_name: str, item_description: str = None):
    """Best-effort address/city/state/zip for one parcel in a multi-parcel
    auction (a BidWrangler item named like 'Subject Two: 261 Ronda Road -
    Dry Branch, WV 25061'). Returns a dict:
        {"address": str, "city": str|None, "state": str|None, "zip": str|None,
         "confident": bool}
    "address" always has something usable (falls back to the raw item name
    with the "Subject N:" prefix stripped) so a row is never silently
    dropped -- but confident=False means the city/state/zip split is
    unverified and the caller should flag the row for review instead of
    trusting it blindly, per "Needs Review instead of guessing."
    """
    raw = (item_name or "").strip()
    stripped = _SUBJECT_PREFIX_RE.sub("", raw).strip()

    m = _TRAILING_CITY_STATE_ZIP_RE.match(stripped)
    if m:
        street = m.group("street").strip()
        city = m.group("city").strip()
        # A run-on name with no real street/city separator (observed live:
        # "1323 Adams Avenue Clarksburg, WV", no dash/comma before the city)
        # still matches here because the greedy \s* before the city group can
        # backtrack and let the (space-permitting) city group swallow just
        # the whitespace before the state code -- street then wrongly absorbs
        # the actual city name and "city" comes back blank. Don't trust that
        # split; fall through to the description fallback / not-confident
        # case below instead of returning a confident empty city.
        if street and city:
            return {
                "address": street,
                "city": city,
                "state": m.group("state"),
                "zip": m.group("zip"),
                "confident": True,
            }

    # Address wasn't in the name -- try the description (observed live: see
    # _DESC_ADDRESS_RE docstring above).
    m2 = _DESC_ADDRESS_RE.search(item_description or "")
    if m2:
        return {
            "address": m2.group("street").strip(),
            "city": m2.group("city").strip(),
            "state": m2.group("state"),
            "zip": m2.group("zip"),
            "confident": True,
        }

    # Nothing parseable -- keep the raw (prefix-stripped) text as the best
    # available label rather than dropping the row, but mark it unconfident
    # so the caller flags it for review instead of comping on a guess.
    return {
        "address": stripped or raw or None,
        "city": None,
        "state": None,
        "zip": None,
        "confident": False,
    }


def to_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[^\d.\-]", "", str(value))
    if not s or s in ("-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def compute_ppsf(sold_price, sqft):
    price = to_float(sold_price)
    sf = to_float(sqft)
    if price and sf:
        return round(price / sf, 2)
    return None
