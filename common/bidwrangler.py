"""Client for Joe R. Pyle Auctions' real backend.

Recon findings (confirmed 2026-09-06 via browser network inspection -- see the
plan/README for how this was found): both the public marketing site
(joerpyleauctions.com) and the live bidding site (bid.joerpyleauctions.com) are
powered by the "BidWrangler" auction platform. Every auction -- past (sold) and
upcoming -- has a numeric BidWrangler id and a clean JSON detail endpoint:

    GET https://bid.joerpyleauctions.com/api/auctions/{id}
        ?page=active&include_items_data=true&include_documents=true

No headless browser is needed anywhere in this pipeline -- plain HTTP suffices.

Two ways to discover auction ids:
  1. Static HTML on joerpyleauctions.com/results (and /results/P15, /P30, ...)
     links to each auction as /auctions/detail/bw{id} -- this is the practical
     source of PAST/SOLD auction ids (the marketing site is the durable public
     archive; BidWrangler itself may not keep completed auctions in its own
     feed indefinitely).
  2. bid.joerpyleauctions.com/api/feed/all (paginated) lists currently
     active/upcoming auctions directly -- the source for the Watch tab.

Each auction's item description text also contains the county tax map
reference the auctioneer includes in every listing, e.g.
"District 19, Map 4G, Parcel 71" -- this feeds enrich_sqft_wv.py directly, no
address fuzzy-matching required.
"""
import re
import time

import requests

from . import config

RESULTS_BASE = "https://www.joerpyleauctions.com/results"
BID_BASE = "https://bid.joerpyleauctions.com"

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


def list_result_page_auction_ids(session: requests.Session, max_pages: int = 40, page_size: int = 15):
    """Walk joerpyleauctions.com/results, /results/P15, /results/P30, ...
    collecting every distinct BidWrangler auction id linked from the page.
    Stops after `max_pages` pages or as soon as a page contributes zero ids
    that we haven't already seen (the site repeats/pads the tail page).
    """
    seen_ids = []
    seen_set = set()
    offset = 0
    for _ in range(max_pages):
        url = RESULTS_BASE if offset == 0 else f"{RESULTS_BASE}/P{offset}"
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


def list_feed_auction_ids(session: requests.Session, max_pages: int = 20, per_page: int = 50):
    """Page through bid.joerpyleauctions.com's feed endpoint(s) for every
    currently listed (active or upcoming) auction id.

    There are two feed endpoints seen during recon:
      /api/feed/all              -- looked like a general/global listing
      /api/feed?active=true&indices=20  -- 'indices=20' matches Joe R. Pyle's
                                            own company_id (20), seen on every
                                            auction detail response -- likely
                                            the properly site-scoped feed.
    We try /api/feed/all first (simpler params) and fall back to the
    indices=20 form if that comes back empty, logging enough detail either way
    to diagnose from the Action's log output if both come back empty.
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

    def _fetch(url, extra_params, label):
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
            for item in items:
                if item.get("id"):
                    found.append(item["id"])
            if len(items) < per_page:
                break
            time.sleep(config.REQUEST_DELAY_SECONDS)
        return found

    ids = _fetch(f"{BID_BASE}/api/feed/all", {"include_syndicated": "true", "version": 2}, "feed/all")
    if not ids:
        print("[feed] /api/feed/all returned nothing -- trying the company-scoped /api/feed endpoint")
        ids = _fetch(
            f"{BID_BASE}/api/feed",
            {"active": "true", "include_recently_complete_auctions_to_active": "false", "indices": 20},
            "feed?indices=20",
        )
    return ids


def get_auction_detail(session: requests.Session, auction_id) -> dict:
    resp = session.get(
        f"{BID_BASE}/api/auctions/{auction_id}",
        params={"page": "active", "include_items_data": "true", "include_documents": "true"},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


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
