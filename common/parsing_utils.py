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
