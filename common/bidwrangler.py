"""Client for Joe R. Pyle Auctions' real backend.

Recon findings (confirmed 2026-09-06 via browser network inspection -- see the
plan/README for how this was found): both the public marketing site
(joerpyleauctions.com) and the live bidding site (bid.joerpyleauctions.com) are
powered by the "BidWrangler" auction platform. Every auction -- past (sold) and
upcoming -- has a numeric BidWrangler id and a clean JSON detail endpoint:

    GET https://bid.joerpyleauctions.com/api/auctions/{id}
        ?page=active&include_items_data=true&include_documents=true

No headless browser is needed anywhere in this pipeline -- plain HTTP suffices.

Two ways to discover auction ids, both plain static HTML on the marketing
site (joerpyleauctions.com), not BidWrangler's own API:
  1. /results (and /results/P15, /P30, ...) links to each SOLD auction as
     /auctions/detail/bw{id} -- the practical source of past/sold auction
     ids. Confirmed 2026-09-07: this archive only goes back ~3.5 months
     before its pagination hits a 404; there's no deeper public archive.
  2. The homepage (ROOT_BASE) embeds every currently-listed auction id --
     real estate and personal property mixed -- in a single page load; no
     pagination needed (see list_upcoming_page_auction_ids). This replaced
     bid.joerpyleauctions.com/api/feed?indices={company_id}, which was
     confirmed in production to return a small, incomplete slice of Pyle's
     actual live listings (as few as 1, when the site itself showed 79 real
     estate auctions that same day) -- kept below only as a last-resort
     fallback, not the primary source anymore.

Every auction found either way still gets its full detail from BidWrangler's
own JSON API (get_auction_detail) -- that part was always reliable; it's only
the *discovery* of which ids to look up that needed a different source.

Each auction's item description text also contains the county tax map
reference the auctioneer includes in every listing, e.g.
"District 19, Map 4G, Parcel 71" -- this feeds enrich_sqft_wv.py directly, no
address fuzzy-matching required.
"""
import re
import time

import requests

from . import config, parsing_utils

RESULTS_BASE = "https://www.joerpyleauctions.com/results"
# The site's own homepage (confirmed 2026-09-07 via browser recon) embeds
# EVERY currently-listed auction id -- real estate and personal property,
# upcoming and some recently-closed -- in a single page load; its own P15/P30
# "page 2/3" links re-render the exact same embedded set client-side rather
# than fetching more, so one plain GET here is the complete discovery source
# for "what's currently listed," no pagination needed. This replaced relying
# on BidWrangler's own /api/feed endpoint, which was confirmed to only surface
# a small, incomplete slice of Pyle's actual live listings (1 auction, when
# the site itself was showing 79 real estate auctions that day).
ROOT_BASE = "https://www.joerpyleauctions.com"
BID_BASE = "https://bid.joerpyleauctions.com"

# BidWrangler is a shared platform -- confirmed in production 2026-09 that
# /api/feed/all is a platform-WIDE feed covering every auction company hosted
# on BidWrangler, not just Joe R Pyle's (it returned things like a Uniontown,
# PA tools/mower estate auction that isn't a Pyle real-estate listing at all).
# Every Pyle auction detail response carries this same company_id, and
# /api/feed?indices=<company_id> is the properly scoped, Pyle-only feed.
PYLE_COMPANY_ID = 20

_AUCTION_LINK_RE = re.compile(r"/auctions/detail/bw(\d+)")


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT, "Accept": "application/json, text/html"})
    return s


def warm_session(session: requests.Session):
    """Hit the bidding site once so it can set whatever guest/session cookie it
    wants before we call the JSON API. Cheap insurance against the API someday
    requiring a session cookie that a bare GET wouldn't have."""
    try:
        session.get(BID_BASE + "/", timeout=20)
    except requests.RequestException as e:
        print(f"[warn] could not warm session against {BID_BASE}: {e}")


def list_result_page_auction_ids(session: requests.Session, max_pages: int = 40, page_size: int = 15,
                                  base_url: str = RESULTS_BASE):
    """Walk `base_url`, `base_url`/P15, `base_url`/P30, ... collecting every
    distinct BidWrangler auction id linked from the page. Stops after
    `max_pages` pages or as soon as a page contributes zero ids we haven't
    already seen (the site repeats/pads the tail page -- and, for the
    homepage specifically, every "page" is really the same embedded set
    re-rendered client-side, so this naturally stops after page 1 there).

    Confirmed 2026-09-07: joerpyleauctions.com/results only exposes roughly
    the last 3.5 months of sold auctions before its pagination hits a 404 --
    there is no deeper archive discoverable this way (no sitemap of individual
    auction pages either, and BidWrangler's own auction ids are too sparse to
    brute-force scan for older ones without hammering a shared platform for
    auctions that mostly aren't even Pyle's). `max_pages` above that point is
    harmless -- the loop just stops itself once a page adds nothing new.
    """
    seen_ids = []
    seen_set = set()
    offset = 0
    for _ in range(max_pages):
        url = base_url if offset == 0 else f"{base_url}/P{offset}"
        resp = session.get(url, timeout=20)
        if resp.status_code != 200:
            break
        ids_on_page = set(_AUCTION_LINK_RE.findall(resp.text))
        new_ids = ids_on_page - seen_set
        if not new_ids:
            break
        for i in new_ids:
            seen_set.add(i)
            seen_ids.append(i)
        offset += page_size
        time.sleep(config.REQUEST_DELAY_SECONDS)
    return seen_ids


def list_upcoming_page_auction_ids(session: requests.Session, max_pages: int = 5, page_size: int = 15):
    """Discovery source for 'everything currently listed' (real estate AND
    personal property, mixed) -- see ROOT_BASE above. Real-estate filtering
    happens later, per auction, via is_real_estate_auction() once we have
    each one's own detail (items_count + description), not here."""
    return list_result_page_auction_ids(session, max_pages=max_pages, page_size=page_size, base_url=ROOT_BASE)


def list_feed_auction_ids(session: requests.Session, max_pages: int = 20, per_page: int = 50):
    """Page through bid.joerpyleauctions.com's feed endpoint(s) for every
    currently listed (active or upcoming) Joe R. Pyle auction id.

    There are two feed endpoints seen during recon:
      /api/feed?active=true&indices=20  -- scoped to Pyle's own company_id
                                            (20, confirmed on every auction
                                            detail response) -- the correct
                                            source for this business, tried
                                            first.
      /api/feed/all                     -- confirmed in production to be a
                                            platform-WIDE feed across every
                                            company hosted on BidWrangler, not
                                            just Pyle (it returned an unrelated
                                            estate/tools auction from another
                                            auctioneer). Used only as a
                                            fallback, and even then filtered
                                            down to company_id == PYLE_COMPANY_ID
                                            so an unrelated auctioneer's
                                            listings never end up on this
                                            dashboard.
    """
    fields = (
        "type,id,items_count,published_items_count,name,status,"
        "scheduled_end_time,starts_at,timezone,location,company_id,published,online_only"
    )

    def _extract_items(data):
        """The feed endpoints don't return a flat list -- confirmed from a real
        run's logged response bodies:
          /api/feed/all      -> {"active": {"results": [...]}, "completed": {...}, ...}
          /api/feed?indices= -> {"results": [...]}
        Handle both shapes (flat "results"/"data"/"items", or one level nested
        under a status key like "active") instead of assuming a top-level list
        or a "data"/"items" key that this API doesn't actually use.
        """
        if isinstance(data, list):
            return data
        if not isinstance(data, dict):
            return []
        for key in ("results", "data", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        collected = []
        for value in data.values():
            if isinstance(value, dict):
                for key in ("results", "data", "items"):
                    if isinstance(value.get(key), list):
                        collected.extend(value[key])
        return collected

    def _fetch_items(url, extra_params, label):
        found = []
        for page in range(1, max_pages + 1):
            params = {"fields": fields, "page": page, "per_page": per_page}
            params.update(extra_params)
            resp = session.get(url, params=params, timeout=20)
            print(f"[{label}] page {page}: HTTP {resp.status_code}, body starts: {resp.text[:300]!r}")
            if resp.status_code != 200:
                break
            try:
                data = resp.json()
            except ValueError:
                print(f"[{label}] page {page}: response was not JSON, stopping")
                break
            items = _extract_items(data)
            print(f"[{label}] page {page}: parsed {len(items)} item(s)")
            if not items:
                break
            found.extend(items)
            if len(items) < per_page:
                break
            time.sleep(config.REQUEST_DELAY_SECONDS)
        return found

    scoped_items = _fetch_items(
        f"{BID_BASE}/api/feed",
        {"active": "true", "include_recently_complete_auctions_to_active": "false", "indices": PYLE_COMPANY_ID},
        "feed?indices=20",
    )
    ids = [item["id"] for item in scoped_items if item.get("id")]

    if not ids:
        print("[feed] company-scoped feed returned nothing -- falling back to the "
              "platform-wide feed, filtered to Joe R. Pyle's company_id")
        all_items = _fetch_items(f"{BID_BASE}/api/feed/all", {"include_syndicated": "true", "version": 2}, "feed/all")
        ids = [item["id"] for item in all_items if item.get("id") and item.get("company_id") == PYLE_COMPANY_ID]
        print(f"[feed] platform-wide feed had {len(all_items)} total item(s); "
              f"{len(ids)} belong to company_id={PYLE_COMPANY_ID}")

    return ids


def get_auction_detail(session: requests.Session, auction_id) -> dict:
    """Raises requests.HTTPError (404) if the id doesn't exist / isn't
    reachable -- callers that need to distinguish "gone" from other failures
    should catch that specifically."""
    resp = session.get(
        f"{BID_BASE}/api/auctions/{auction_id}",
        params={"page": "active", "include_items_data": "true", "include_documents": "true"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def is_real_estate_auction(detail: dict, summarized: dict) -> bool:
    """Confirmed 2026-09-07 via recon across ~120 currently-listed Pyle
    auctions: every real-estate auction is single-lot (items_count == 1) and
    its item description contains the phrase "real estate" (from the
    auctioneer's own "ONLINE REAL ESTATE AUCTION" / "Real Estate Auction"
    boilerplate); every personal-property auction on this account is
    multi-lot (dozens to hundreds of items) and its first item's description
    is just a preview-time blurb, not that phrase. Both signals together
    avoid misclassifying a rare single-lot personal-property listing (e.g. a
    single vehicle) as real estate."""
    if detail.get("items_count") != 1:
        return False
    return "real estate" in (summarized.get("description") or "").lower()


def build_past_auction_row(s: dict, tax_ref: tuple, stated_sqft) -> dict:
    """Shared field-mapping for a SOLD real-estate auction -> a past_auctions
    row. Used by both scrape_past_sales.py (discovers sold auctions via the
    site's /results archive) and scrape_watch_bids.py (also writes here
    directly the moment it sees an auction it was tracking flip to sold, so a
    sale doesn't have to wait for /results to catch up) so the two paths
    can't drift into different field mappings over time."""
    district, map_, parcel = tax_ref
    row = {
        "bidwrangler_id": s["bidwrangler_id"],
        "auction_date": (s["scheduled_end_time"] or "")[:10] or None,
        "address": s["address"],
        "city": s["city"],
        "state": s["state"],
        "zip": s["zip"],
        "property_type": None,  # filled in by the caller via parsing_utils.guess_property_type
        "published_final_sold_price": s["current_high_bid"],
        "status": "Sold",
        "title_notes": s["name"],
        "source_url": s["detail_url"],
        "parcel_key": s["detail_url"],  # single-lot auction: one property == the whole page
        "parcel_label": None,
        "tax_district": district,
        "tax_map": map_,
        "tax_parcel": parcel,
        "lat": s["lat"],
        "lng": s["lng"],
        "auction_company": "joe_pyle_auctions",
        "acreage": None,
        "review_flag": False,
        "review_reason": None,
        "comp_sqft": None,
        "comp_sqft_source": None,
        "comp_sqft_source_url": None,
        "comp_sqft_quality": None,
        "comp_sqft_note": None,
    }
    if stated_sqft:
        row["comp_sqft"] = stated_sqft
        row["comp_sqft_source"] = "Auction listing (auctioneer-stated)"
        row["comp_sqft_source_url"] = s["marketing_url"]
        row["comp_sqft_quality"] = "Unverified -- from listing copy, not the county record"
        row["comp_sqft_note"] = (
            f"Auctioneer's listing states {stated_sqft:g} sq ft. Run enrich_sqft_wv.py to "
            "confirm/replace with the official WV Assessment figure."
        )
    return row


def is_multi_parcel_real_estate_auction(detail: dict) -> bool:
    """True for a Pyle auction that lists SEVERAL distinct real-estate
    parcels on one auction page (e.g. "16 Raleigh County Parcels Selling to
    the Highest Bidders", "2 Wood County Properties Selling to the Highest
    Bidders") -- a second real-estate auction shape this account uses
    alongside the single-lot one is_real_estate_auction() detects.

    Confirmed 2026-09-07 via recon: these auctions name every item using a
    "Subject N:" / "Subject #N:" convention (one subject per parcel, plus
    sometimes a "Property in Entirety" bundle option and/or a
    "Mineral Interests" line) -- a naming pattern never seen on any
    personal-property multi-lot auction on this account (those use item
    names like "Preview Information", "Pickup Information", or a plain
    item description such as "2003 Freightliner FedEx Parcel Delivery
    Truck"). Requiring a majority of items to follow the Subject-N
    convention (not just one) keeps this from false-triggering on an
    unrelated auction that happens to have one oddly-named item.
    """
    items = detail.get("items") or []
    if len(items) < 2:
        return False
    subject_count = sum(1 for it in items if parsing_utils.is_subject_item_name(it.get("name")))
    return subject_count >= 2 and subject_count >= len(items) / 2


def multi_parcel_items(detail: dict):
    """Yields (original_index, item) for every BidWrangler item that
    represents an actual, comparable piece of real estate within a
    multi-parcel auction. original_index is the item's 1-based position in
    the auction's FULL, unfiltered item list -- used as the parcel_key
    suffix -- so a given parcel's key stays stable across runs regardless of
    which other items get filtered in/out (a bundle sale reducing this call
    to a single yielded item, for instance, must not change that item's own
    key, or it would silently create a duplicate row instead of updating
    the existing one).

    - A "Mineral Interests" line is never yielded -- it's a conveyance of
      subsurface rights, not a comparable property.
    - If the "Property in Entirety" bundle option is the one that actually
      SOLD, only that single item is yielded (the individual "Subject N"
      parcels didn't separately transact in that case -- yielding both
      would double-count one sale as many). Otherwise the bundle option
      itself is skipped and every individual parcel is yielded instead.
    """
    items = detail.get("items") or []
    indexed = list(enumerate(items, start=1))
    subject_items = [(i, it) for i, it in indexed if parsing_utils.is_subject_item_name(it.get("name"))]
    bundle_items = [(i, it) for i, it in subject_items if parsing_utils.is_bundle_item_name(it.get("name"))]
    sold_bundle = next(((i, b) for i, b in bundle_items if b.get("status") == "sold"), None)

    if sold_bundle is not None:
        yield sold_bundle
        return

    for i, it in subject_items:
        name = it.get("name") or ""
        if parsing_utils.is_bundle_item_name(name) or parsing_utils.is_mineral_interest_item_name(name):
            continue
        yield i, it


def build_multi_parcel_row(s_top: dict, item: dict, parcel_index: int) -> dict:
    """Field mapping for ONE parcel out of a multi-parcel auction -> a row
    shaped for either past_auctions or watch_auctions (same fields cover
    both; the caller adds/overrides whichever status-tracking fields its
    table needs, same as the single-lot build_active_row/
    build_past_auction_row split). `s_top` is the auction-level
    summarize_auction() result (for the shared source_url/marketing_url/
    lat/lng); `parcel_index` makes parcel_key unique within the auction.
    """
    name = item.get("name") or ""
    desc = item.get("description_without_html") or ""
    addr = parsing_utils.parse_subject_address(name, desc, auction_title=s_top.get("name"))
    district, map_, parcel = parsing_utils.parse_tax_reference(desc)
    stated_sqft = parsing_utils.parse_sqft_from_text(desc)
    # Try the description first (it's usually more detailed), but fall back
    # to the item's own name -- confirmed live 2026-09-07: a bare acreage
    # parcel named e.g. "SUBJECT 1: 27.16+/- Acres" states the figure
    # cleanly in the name itself even when the description's own phrasing
    # (e.g. a unicode "±" typo variant) doesn't match.
    acreage = parsing_utils.parse_acreage_from_text(desc) or parsing_utils.parse_acreage_from_text(name)
    high = (item.get("api_bidding_state") or {}).get("high") or {}

    property_type = parsing_utils.guess_property_type(name, desc)
    if property_type == "House" and acreage and not stated_sqft:
        # guess_property_type's fallback is "House" when nothing distinctive
        # matched -- but a parcel with a stated acreage and no structure/sqft
        # at all is almost certainly vacant land, not a house, and comping
        # it as a house would be actively misleading.
        property_type = "Land"

    row = {
        "bidwrangler_id": s_top["bidwrangler_id"],
        "auction_date": (s_top["scheduled_end_time"] or "")[:10] or None,
        "address": addr["address"] or name or s_top["name"],
        "city": addr["city"],
        "state": addr["state"],
        "zip": addr["zip"],
        "property_type": property_type,
        "title_notes": name,
        "source_url": s_top["detail_url"],
        "parcel_key": f'{s_top["detail_url"]}#{parcel_index}',
        "parcel_label": name,
        "tax_district": district,
        "tax_map": map_,
        "tax_parcel": parcel,
        "lat": s_top["lat"],
        "lng": s_top["lng"],
        "auction_company": "joe_pyle_auctions",
        "acreage": acreage,
        "current_high_bid": high.get("amount"),
        "review_flag": not addr["confident"],
        "review_reason": None if addr["confident"] else (
            "Could not confidently parse a street/city/state for this parcel from "
            "the auction listing text -- check the Joe Pyle page directly."
        ),
    }
    if stated_sqft:
        row["comp_sqft"] = stated_sqft
        row["comp_sqft_source"] = "Auction listing (auctioneer-stated)"
        row["comp_sqft_source_url"] = s_top["marketing_url"]
        row["comp_sqft_quality"] = "Unverified -- from listing copy, not the county record"
        row["comp_sqft_note"] = (
            f"Auctioneer's listing states {stated_sqft:g} sq ft. Run enrich_sqft_wv.py to "
            "confirm/replace with the official WV Assessment figure."
        )
    return row


def summarize_auction(detail: dict) -> dict:
    """Normalize a raw /api/auctions/{id} response down to the fields the
    scrapers need. Assumes single-lot real-estate auctions (items_count == 1),
    which matches every listing observed on this account; if Pyle ever runs a
    multi-lot personal-property auction through the same feed this only looks
    at the first item, which is a reasonable place to extend later."""
    items = detail.get("items") or []
    item = items[0] if items else {}
    location = detail.get("location") or item.get("location") or {}
    bidding_state = item.get("api_bidding_state") or {}
    high = bidding_state.get("high") or {}
    bidding_config = item.get("bidding_configuration") or {}
    description = item.get("description_without_html") or ""

    return {
        "bidwrangler_id": detail.get("id"),
        "name": detail.get("name") or item.get("name"),
        "status": detail.get("status"),
        "complete": detail.get("complete"),
        "archived": detail.get("archived"),
        "items_count": detail.get("items_count"),
        # The single item's own status -- "sold" / "no_sale" are the two
        # values confirmed in production for single-lot real-estate auctions.
        # This is the definitive sold-vs-unsold signal (see
        # is_real_estate_auction's docstring for how this was found):
        # BidWrangler tracks it directly, no guessing from price/bid data
        # needed.
        "item_status": item.get("status"),
        "scheduled_end_time": item.get("scheduled_end_time") or detail.get("scheduled_end_time"),
        "address": location.get("street"),
        "city": location.get("city"),
        "state": location.get("state"),
        "zip": location.get("zip"),
        "lat": location.get("lat"),
        "lng": location.get("lng"),
        "description": description,
        "current_high_bid": high.get("amount"),
        "ask_amount": bidding_state.get("ask_amount"),
        "reserve_amount": bidding_config.get("reserve_amount"),
        "accepted_bid_count": bidding_state.get("accepted_bid_count"),
        "detail_url": f"{BID_BASE}/ui/auctions/{detail.get('id')}",
        "marketing_url": f"https://www.joerpyleauctions.com/auctions/detail/bw{detail.get('id')}",
    }
