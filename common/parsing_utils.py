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
    #
    # The separator after the subject number is usually a colon, but not
    # always -- confirmed live 2026-09-08: a 4-parcel auction ("Premier
    # Quarry Creek Building Lots") named its items "Subject One – Quarry
    # Ridge South- Charleston, WV" (en-dash, no colon at all). Since
    # is_multi_parcel_real_estate_auction() below counts how many items
    # match this prefix to decide whether an auction is multi-parcel at
    # all, missing this shape didn't just leave one parcel unconfidently
    # parsed -- it meant NONE of that auction's items were recognized as
    # "Subject N" items, so the whole auction was misread as a single-lot
    # sale and only its first item ($5,000) was ever recorded, silently
    # dropping the other three sold parcels ($21,000 + $12,000 + $33,000)
    # entirely. Accepting a colon or a hyphen/en-dash/em-dash here fixes
    # that at the root rather than just for this one auction.
    r"^subject\s*#?\s*[a-z0-9]{1,12}\s*[:\-–—]\s*",
    re.IGNORECASE,
)
# Matches "<street/area>, <city>, ST [, ZIP]" allowing a comma, hyphen, or
# en/em dash as the street/city separator, since Pyle's own listings use all
# three inconsistently (observed live: "Big Ugly Rd E, Harts, WV 25524",
# "261 Ronda Road - Dry Branch, WV 25061", "Bufflick Run- Clendenin, WV").
# The comma before the zip is also optional -- confirmed live: "2018 1/2 2nd
# Street, Moundsville, WV, 26041" and "670 Walker Ridge Rd, Walton, WV,
# 25286" both punctuate the zip like a fourth list item rather than just
# trailing whitespace-separated, which the old \s*zip$ tail never matched.
#
# The plain hyphen alternative deliberately excludes one right after "+/"
# (a negative lookbehind) -- confirmed live 2026-09-08: acreage figures are
# routinely written "0.53 +/- Acres on Jerome St Morgantown, WV", and
# without this exclusion the street group's own non-greedy match stops at
# the FIRST hyphen it finds, which is the one inside "+/-" itself, long
# before the real street/city separator -- producing a confidently-wrong
# split ("0.53 +/" as the address, "Acres on Jerome St Morgantown" as the
# city) instead of ever reaching the real separator or falling through to
# the safer known-city matching below.
_TRAILING_CITY_STATE_ZIP_RE = re.compile(
    r"^(?P<street>.*?)(?:,|–|—|(?<!\+/)-)\s*(?P<city>[A-Za-z .]+?),?\s*"
    r"(?P<state>WV|PA|OH|KY|VA|MD)\b,?\s*(?P<zip>\d{4,5})?\s*$"
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

# Town names this auctioneer's listings have already resolved to, mapped to
# their state -- used ONLY to find the street/city boundary in a run-on name
# with no separator at all (e.g. "1323 Adams Avenue Clarksburg, WV"), or a
# trailing city with no state code at all (e.g. "105 Lavender Street- Oak
# Hill"). Regex alone can't place that boundary safely: any purely
# punctuation-driven split guesses wrong on a multi-word town name (observed
# live in this auctioneer's own area -- "South Charleston", "Cross Lanes",
# "St. Albans" would each get cut in half, e.g. "...Avenue South" /
# "Charleston" instead of "...Avenue" / "South Charleston"). Matching
# against a real, already-seen place name instead of guessing is what keeps
# this safe -- confirmed 2026-09-08 against every known live example of
# this bug shape.
#
# Sourced from every already-resolved (city, state) pair actually sitting in
# past_auctions/watch_auctions today (2026-09-08 snapshot), which is why the
# state isn't just assumed to be WV -- this auctioneer does occasionally
# list PA/KY/VA/MD/OH properties near the state line, and a handful of town
# names are only ever seen there ("Uniontown" -> PA, "Ashland" -> KY,
# "Roanoke" -> VA, "Oakland" -> MD, "Portsmouth" -> OH -- all confirmed,
# never WV, in this account's own history). A town name whose OWN history
# has appeared under more than one state ("Washington" -- both WV and PA)
# is deliberately left out rather than guessed. A few real WV town names
# with no prior successful parse yet were manually confirmed and added
# during this same investigation (Summersville, Nutter Fort, West Logan,
# Smithers) after turning up run-on in live listing text with no ambiguity
# risk -- flagged inline below rather than mixed in silently.
_KNOWN_CITY_STATE = {
    "Advent": "WV", "Alderson": "WV", "Alum Bridge": "WV", "Alum Creek": "WV",
    "Amherstdale": "WV", "Amigo": "WV", "Amma": "WV", "Arnoldsburg": "WV",
    "Ashland": "KY", "Aurora": "WV", "Bancroft": "WV", "Barboursville": "WV",
    "Beckley": "WV", "Belle": "WV", "Belleville": "WV", "Benwood": "WV",
    "Big Creek": "WV", "Blacksville": "WV", "Blount": "WV", "Bluefield": "WV",
    "Branchland": "WV", "Bridgeport": "WV", "Bruceton Mills": "WV", "Buckhannon": "WV",
    "Buffalo": "WV", "Canvas": "WV", "Cedar Grove": "WV", "Chapmanville": "WV",
    "Charleston": "WV", "Chelyan": "WV", "Chesapeake": "WV", "Clarksburg": "WV",
    "Clarksville": "PA", "Clendenin": "WV", "Coalton": "WV", "Connellsville": "PA",
    "Cowen": "WV", "Cross Lanes": "WV", "Culloden": "WV", "Daniels": "WV",
    "Delbarton": "WV", "Dry Branch": "WV", "Drybranch": "WV", "Dunbar": "WV",
    "Dunmore": "WV", "East Bank": "WV", "Eccles": "WV", "Elkview": "WV",
    "Eskdale": "WV", "Fairchance": "PA", "Fairmont": "WV", "Fairview": "WV",
    "Farmington": "WV", "Fayettevillle": "WV", "Fort Gay": "WV", "Friendly": "WV",
    "Gilbert": "WV", "Glasgow": "WV", "Glen": "WV", "Glenwood": "WV",
    "Grafton": "WV", "Grant Town": "WV", "Grayson": "KY",
    "Harpers Ferry": "WV",  # manually confirmed 2026-09-08, see block comment above
    "Harts": "WV",
    "Hedgesville": "WV", "Hilton Village": "WV", "Huntington": "WV", "Hurricane": "WV",
    "Inez": "KY", "Jolo": "WV", "Josephine": "WV", "Julian": "WV",
    "Kanawha City": "WV", "Kermit": "WV", "Kimberly": "WV", "Kingwood": "WV",
    "Lavalette": "WV", "Left Hand": "WV", "Leon": "WV", "Letart": "WV",
    "Logan": "WV", "London": "WV", "Lumberport": "WV", "Maidsville": "WV",
    "Mannington": "WV", "Marmet": "WV", "Martinsburg": "WV", "Matewan": "WV",
    "McGraw": "WV", "Miami": "WV", "Midway": "WV", "Milton": "WV",
    "Monongah": "WV", "Monterville": "WV", "Montgomery": "WV", "Morgantown": "WV",
    "Moundsville": "WV", "Mount Clare": "WV", "Mt Hope": "WV", "Mt. Hope": "WV",
    "Mullens": "WV", "Nettie": "WV", "Newberg": "WV", "Nitro": "WV",
    "Nutter Fort": "WV",  # manually confirmed 2026-09-08, see block comment above
    "Oak Hill": "WV", "Oakland": "MD", "Ona": "WV", "Orgas": "WV",
    "Paden City": "WV", "Parkersburg": "WV", "Parsons": "WV", "Petersburg": "WV",
    "Philippi": "WV", "Poca": "WV", "Portsmouth": "OH", "Prichard": "WV",
    "Princewick": "WV", "Pullman": "WV", "Red House": "WV", "Rhodell": "WV",
    "Richmond": "KY", "Ridgeview": "WV", "Ripley": "WV", "Rivesville": "WV",
    "Roanoke": "VA", "Rowlesburg": "WV", "Saint Albans": "WV", "Salem": "WV",
    "Salt Rock": "WV", "Scott Depot": "WV", "Seneca Rocks": "WV", "Shinnston": "WV",
    "Sistersville": "WV",
    "Smithers": "WV",  # manually confirmed 2026-09-08, see block comment above
    "Smithfield": "WV", "Sophia": "WV", "South Charleston": "WV", "Spelter": "WV",
    "Spencer": "WV", "St. Albans": "WV", "Stonewood": "WV",
    "Summersville": "WV",  # manually confirmed 2026-09-08, see block comment above
    "Terra Alta": "WV", "Thomas": "WV", "Tornado": "WV", "Tunnelton": "WV",
    "Uniontown": "PA", "Vienna": "WV", "Walker": "WV", "Wallace": "WV",
    "Waynesburg": "PA", "Welch": "WV",
    "West Logan": "WV",  # manually confirmed 2026-09-08, see block comment above
    "Weston": "WV", "Wheeling": "WV", "Whitesville": "WV", "Williamson": "WV",
    "Winfield": "WV", "Worthington": "WV",
}
# Longest first (by word count, then character count) so a multi-word name
# is tried -- and wins -- before any single-word name it happens to contain
# ("South Charleston" before "Charleston").
_KNOWN_CITIES_BY_LENGTH = sorted(_KNOWN_CITY_STATE, key=lambda c: (-c.count(" "), -len(c)))
_STATE_CODE_RE = re.compile(r"\b(WV|PA|OH|KY|VA|MD)\b")


def _match_run_on_known_city(text: str):
    """Best-effort street/city split for a run-on address with no separator
    ("1323 Adams Avenue Clarksburg, WV") -- returns the usual dict shape, or
    None if no known city name lines up right before a state code. Never
    guesses a street/city boundary from punctuation alone (see
    _KNOWN_CITY_STATE's docstring for why that's unsafe). Trusts whatever
    state code is actually in the text (not the known-city table) since an
    explicitly stated state is the most reliable signal available."""
    if not text:
        return None
    m = _STATE_CODE_RE.search(text)
    if not m:
        return None
    prefix = text[: m.start()].rstrip(" ,-–—")
    if not re.match(r"^\d", prefix):
        return None  # no leading street number -- not confident this is an address at all
    rest = text[m.end() :]
    zip_m = re.match(r"[,\s]*(\d{4,5})?", rest)
    zip_code = zip_m.group(1) if zip_m else None
    lower_prefix = prefix.lower()
    for city in _KNOWN_CITIES_BY_LENGTH:
        if lower_prefix.endswith(city.lower()):
            street = prefix[: len(prefix) - len(city)].rstrip(" ,-–—")
            if street:
                return {
                    "address": street,
                    "city": city,
                    "state": m.group(1),
                    "zip": zip_code,
                    "confident": True,
                }
    return None


def _match_trailing_known_city_no_state(text: str):
    """Same idea as _match_run_on_known_city, for the case where the state
    code is missing from the text entirely -- observed live 2026-09-08 as
    this auctioneer's other common shorthand: "<street>- <City>" or
    "<street> <City>" with no state anywhere in the item name (e.g. "105
    Lavender Street- Oak Hill", "16 Wilson Street Smithers"). Only ever
    returns a match when the trailing text is an EXACT known city name (see
    _KNOWN_CITY_STATE's docstring) sitting at the very end of the string --
    the state then comes from that same already-verified table, never
    assumed. A trailing word that isn't a recognized city (or isn't at the
    very end) returns None rather than guessing."""
    if not text:
        return None
    stripped = text.rstrip(" .")
    lower = stripped.lower()
    for city in _KNOWN_CITIES_BY_LENGTH:
        if lower.endswith(city.lower()):
            prefix = stripped[: len(stripped) - len(city)].rstrip(" ,-–—")
            if not re.match(r"^\d", prefix):
                continue  # no leading street number -- not confident this is an address
            # Reject if there's still a lowercase word butted right up
            # against the city with no separator at all (e.g. "...Avenuecity")
            # -- rstrip above only trims actual separator characters, so a
            # genuine run-on-with-no-space case is correctly left unmatched
            # rather than guessed.
            if prefix and stripped[len(prefix)] not in " ,-–—":
                continue
            return {
                "address": prefix,
                "city": city,
                "state": _KNOWN_CITY_STATE[city],
                "zip": None,
                "confident": True,
            }
    return None


def _match_via_auction_title(item_text: str, auction_title: str):
    """Last-resort fallback for a multi-parcel item whose OWN name/description
    has a real street address but no city/state anywhere in it at all
    (observed live 2026-09-08: a 16-parcel auction titled "16 Uniontown
    Investment Properties" where every single item is just "130 Walnut
    Street", "68 Millview Street", etc. -- no city, on any item, anywhere).
    Only fires when item_text itself looks like a real street (leading
    digit) and the auction's own title contains one whole, exact known city
    name (see _KNOWN_CITY_STATE's docstring) -- e.g. "Uniontown" in that
    title, which this account's own history confirms is always Uniontown,
    PA, never a WV town of the same name. This is deliberately narrow: it
    does NOT scan free-text listing descriptions for an incidental city
    mention (a description can say "minutes from Huntington" about a
    property that isn't actually in Huntington -- an auction's own title
    naming the properties' shared location is a much stronger signal)."""
    if not item_text or not auction_title:
        return None
    if not re.match(r"^\d", item_text.strip()):
        return None
    lower_title = f" {auction_title.lower()} "
    for city in _KNOWN_CITIES_BY_LENGTH:
        if re.search(r"[^a-z]" + re.escape(city.lower()) + r"[^a-z]", lower_title):
            return {
                "address": item_text.strip(),
                "city": city,
                "state": _KNOWN_CITY_STATE[city],
                "zip": None,
                "confident": True,
            }
    return None


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


def parse_subject_address(item_name: str, item_description: str = None, auction_title: str = None):
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

    auction_title is optional -- the auction's own top-level name (e.g. "16
    Uniontown Investment Properties") -- used only as the very last resort
    when nothing in the item's own name/description gives a city at all; see
    _match_via_auction_title's docstring for why this is safe and narrow.
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

    # The name has a street number run straight into the city with no
    # separator at all (see _KNOWN_CITY_STATE's docstring) -- try matching
    # against a real, already-seen town name before giving up on it. This is
    # deliberately NOT a regex guess (an earlier attempt using a
    # street/city-splitting regex here passed ad-hoc tests but silently
    # mis-split multi-word town names like "South Charleston" -- see
    # _match_run_on_known_city's docstring).
    m1b = _match_run_on_known_city(stripped)
    if m1b:
        return m1b

    # Same idea, but the state code is missing entirely -- this
    # auctioneer's other common shorthand ("<street>- <City>", no state).
    m1c = _match_trailing_known_city_no_state(stripped)
    if m1c:
        return m1c

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

    # Same no-state shorthand can also show up only in the description.
    m2b = _match_trailing_known_city_no_state((item_description or "").strip())
    if m2b:
        return m2b

    # Last resort: the item itself has a real street number but genuinely no
    # city anywhere in its own text -- fall back to the auction's own title,
    # if one was given (see _match_via_auction_title's docstring).
    m3 = _match_via_auction_title(stripped, auction_title)
    if m3:
        return m3

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
