#!/usr/bin/env python3
"""Manual smoke tests against real samples captured during recon (2026-09-06)
-- not a pytest suite, just a script to run by hand: `python tests_manual/test_parsing.py`.
No network access required; everything here is mocked from data actually
observed on bid.joerpyleauctions.com and mapwv.gov.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from common import bidwrangler, parsing_utils, wv_assessment  # noqa: E402

# --- Real BidWrangler /api/auctions/166759 response, trimmed to the fields we use ---
SAMPLE_DETAIL = {
    "id": 166759,
    "name": "Updated 2 Bedroom on a Spacious Lot in Charleston",
    "status": "accepting_bids",
    "complete": False,
    "archived": False,
    "location": {
        "street": "1855 Oakhurst Dr.", "city": "Charleston", "state": "WV", "zip": "25309",
        "lat": "38.3344481", "lng": "-81.7058576",
    },
    "items": [{
        "scheduled_end_time": "2026-09-08T23:00:00.000Z",
        "description_without_html": (
            "ONLINE REAL ESTATE AUCTION ... Updated 2 Bedroom Home on 0.79 +/- Acres "
            "Property Highlights: Move-in Ready 2 Bedroom, 1 Bathroom Home 784 +/- Sq. Ft. "
            "0.79 +/- Acres (as assessed) ... District 19, Map 4G, Parcel 71 "
            "10% buyer's premium. 60 days to close."
        ),
        "api_bidding_state": {
            "accepted_bid_count": 21,
            "high": {"amount": 34000},
            "ask_amount": 35000,
        },
        "bidding_configuration": {"reserve_amount": 50000},
    }],
}


def test_summarize_auction():
    s = bidwrangler.summarize_auction(SAMPLE_DETAIL)
    assert s["bidwrangler_id"] == 166759
    assert s["address"] == "1855 Oakhurst Dr."
    assert s["city"] == "Charleston"
    assert s["current_high_bid"] == 34000
    assert s["reserve_amount"] == 50000
    assert s["detail_url"] == "https://bid.joerpyleauctions.com/ui/auctions/166759"
    print("OK: summarize_auction ->", s["address"], s["current_high_bid"], s["detail_url"])


def test_parse_tax_reference():
    district, map_, parcel = parsing_utils.parse_tax_reference(SAMPLE_DETAIL["items"][0]["description_without_html"])
    assert (district, map_, parcel) == ("19", "4G", "71"), (district, map_, parcel)
    print("OK: parse_tax_reference ->", district, map_, parcel)


def test_parse_sqft_from_text():
    sqft = parsing_utils.parse_sqft_from_text(SAMPLE_DETAIL["items"][0]["description_without_html"])
    assert sqft == 784.0, sqft
    print("OK: parse_sqft_from_text ->", sqft)


def test_compute_ppsf():
    ppsf = parsing_utils.compute_ppsf(35750, 784)
    assert abs(ppsf - 45.60) < 0.01, ppsf
    print("OK: compute_ppsf ->", ppsf)


# --- Real mapwv.gov/Assessment/Detail/?PID=2019004G007100000000 markup (the two
# tables that matter), captured during recon ---
SAMPLE_DETAIL_HTML = """
<html><body>
<table class="table"><tr><td>Physical Address</td><td>1855 OAKHURST</td></tr></table>
<table class="table"><tr><td>Parcel ID</td><td>20-19-004G-0071-0000</td></tr></table>
<table class="table"><tr><td>Owner(s)</td><td>PRICE EMILEE SHAE</td></tr></table>
<table class="table"><tr><td>Sum of Structure Areas</td><td>784</td></tr></table>
<table class="table"><tr><td># of Buildings (Cards)</td><td>1</td></tr></table>
<table class="table"><thead><tr>
  <th>Card</th><th>Year Built</th><th>Stories</th><th>CG</th><th>Architectural Style</th>
  <th>Exterior Wall</th><th>Basement Type</th><th>Square Footage (SFLA)</th><th>Building Value</th>
</tr></thead><tbody><tr>
  <td>1</td><td>1956</td><td>1.00</td><td>2M</td><td>Conventional</td><td>Aluminum</td>
  <td>Full</td><td>784</td><td>$66,400</td>
</tr></tbody></table>
</body></html>
"""


class _FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _FakeSession:
    def get(self, url, params=None, timeout=None):
        return _FakeResponse(SAMPLE_DETAIL_HTML)


def test_get_assessment_detail_parsing():
    detail = wv_assessment.get_assessment_detail(_FakeSession(), "2019004G007100000000")
    assert detail["comp_sqft"] == 784.0, detail
    assert detail["physical_address"] == "1855 OAKHURST"
    assert detail["parcel_id_formatted"] == "20-19-004G-0071-0000"
    print("OK: get_assessment_detail parsing ->", detail)


def test_get_assessment_detail_fallback_to_card_sum():
    # Same page but with the summary label missing -- parser should fall back
    # to summing the Card table's SFLA column.
    html_without_summary = SAMPLE_DETAIL_HTML.replace(
        "<tr><td>Sum of Structure Areas</td><td>784</td></tr>", ""
    )
    session = _FakeSession()
    session.get = lambda url, params=None, timeout=None: _FakeResponse(html_without_summary)
    detail = wv_assessment.get_assessment_detail(session, "2019004G007100000000")
    assert detail["comp_sqft"] == 784.0, detail
    print("OK: get_assessment_detail fallback-to-card-sum ->", detail["comp_sqft"])


# --- Real-estate classification + lifecycle resolution, from live samples
# captured 2026-09-07 (see bidwrangler.is_real_estate_auction / scrape_watch_bids.resolve_one) ---
import scrape_watch_bids  # noqa: E402

REAL_ESTATE_ACTIVE = {
    "id": 166759, "items_count": 1, "complete": False, "archived": False,
    "name": "Updated 2 Bedroom on a Spacious Lot in Charleston",
    "description": "<p>ONLINE REAL ESTATE AUCTION Bidding begins closing Tuesday, September 8th</p>",
    "location": {"street": "1855 Oakhurst Dr.", "city": "Charleston", "state": "WV", "zip": "25309",
                 "lat": "38.3344481", "lng": "-81.7058576"},
    "items": [{
        "status": "active",
        "scheduled_end_time": "2026-09-08T23:00:00.000Z",
        "description_without_html": "ONLINE REAL ESTATE AUCTION Bidding begins closing Tuesday, September 8th at 7:00PM 1855 Oakhurst Dr. Charleston, WV",
        "api_bidding_state": {"high": {"amount": 34000}},
        "bidding_configuration": {"reserve_amount": 50000},
    }],
}

REAL_ESTATE_SOLD = {
    "id": 160625, "items_count": 1, "complete": True, "archived": False,
    "name": "SOLD - Two-Story 3 Bedroom in Charleston",
    "description": "<p>Real Estate Auction</p>",
    "location": {"street": "123 Main St", "city": "Charleston", "state": "WV", "zip": "25301",
                 "lat": "38.35", "lng": "-81.63"},
    "items": [{
        "status": "sold",
        "scheduled_end_time": "2026-05-27T23:00:00.000Z",
        "description_without_html": "Real Estate Auction. 1,200 sq ft.",
        "api_bidding_state": {"high": {"amount": 90000}},
        "bidding_configuration": {},
    }],
}

REAL_ESTATE_UNSOLD = {
    "id": 165834, "items_count": 1, "complete": True, "archived": True,
    "name": "5.3 Acre Former School Campus on the River",
    "description": "<p>Real Estate Auction</p>",
    "location": {"street": "100 Brannon Street", "city": "East Bank", "state": "WV", "zip": "25067",
                 "lat": "38.29", "lng": "-81.56"},
    "items": [{
        "status": "no_sale",
        "scheduled_end_time": "2026-08-25T14:00:00.000Z",
        "description_without_html": "Real Estate Auction. 5.3 acres.",
        "api_bidding_state": {"high": {"amount": 220000}},
        "bidding_configuration": {},
    }],
}

PERSONAL_PROPERTY = {
    "id": 167246, "items_count": 623, "complete": False, "archived": False,
    "name": "Coalton, WV - 2011 Toyota RAV4, Antiques & More!",
    "description": "<p>Online Personal Property Auction</p>",
    "location": {"street": "1 Main St", "city": "Coalton", "state": "WV", "zip": "26257"},
    "items": [{"status": "active", "scheduled_end_time": None, "description_without_html": "Sept 1, 2-5pm",
               "api_bidding_state": {}, "bidding_configuration": {}}],
}


def test_is_real_estate_auction():
    s = bidwrangler.summarize_auction(REAL_ESTATE_ACTIVE)
    assert bidwrangler.is_real_estate_auction(REAL_ESTATE_ACTIVE, s) is True
    s2 = bidwrangler.summarize_auction(PERSONAL_PROPERTY)
    assert bidwrangler.is_real_estate_auction(PERSONAL_PROPERTY, s2) is False
    print("OK: is_real_estate_auction classifies real estate vs personal property")


def test_resolve_one_lifecycle():
    class _FakeSession:
        pass

    fixtures = {
        166759: REAL_ESTATE_ACTIVE,
        160625: REAL_ESTATE_SOLD,
        165834: REAL_ESTATE_UNSOLD,
        167246: PERSONAL_PROPERTY,
    }
    orig = bidwrangler.get_auction_detail
    bidwrangler.get_auction_detail = lambda session, auction_id: fixtures[auction_id]
    try:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        [(outcome, payload)] = scrape_watch_bids.resolve_one(_FakeSession(), 166759, "2026-09-07 12:00 UTC", now)
        assert outcome == "active" and payload["final_status"] == "Active", (outcome, payload)

        [(outcome, payload)] = scrape_watch_bids.resolve_one(_FakeSession(), 160625, "2026-09-07 12:00 UTC", now)
        assert outcome == "sold" and payload["published_final_sold_price"] == 90000, (outcome, payload)

        [(outcome, payload)] = scrape_watch_bids.resolve_one(_FakeSession(), 165834, "2026-09-07 12:00 UTC", now)
        assert outcome == "terminal" and payload["final_status"] == "Unsold", (outcome, payload)

        [(outcome, payload)] = scrape_watch_bids.resolve_one(_FakeSession(), 167246, "2026-09-07 12:00 UTC", now)
        assert outcome == "not_real_estate" and payload is None, (outcome, payload)
    finally:
        bidwrangler.get_auction_detail = orig
    print("OK: resolve_one -> active/sold/unsold/not_real_estate all classify correctly")


def test_resolve_one_postponed_cancelled_text_override():
    class _FakeSession:
        pass

    postponed = dict(REAL_ESTATE_ACTIVE)
    postponed["name"] = "POSTPONED - Updated 2 Bedroom on a Spacious Lot in Charleston"
    cancelled = dict(REAL_ESTATE_ACTIVE)
    cancelled["name"] = "CANCELLED - Updated 2 Bedroom on a Spacious Lot in Charleston"

    orig = bidwrangler.get_auction_detail
    try:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        bidwrangler.get_auction_detail = lambda session, auction_id: postponed
        [(outcome, payload)] = scrape_watch_bids.resolve_one(_FakeSession(), 166759, "2026-09-07 12:00 UTC", now)
        assert outcome == "terminal" and payload["final_status"] == "Postponed", (outcome, payload)

        bidwrangler.get_auction_detail = lambda session, auction_id: cancelled
        [(outcome, payload)] = scrape_watch_bids.resolve_one(_FakeSession(), 166759, "2026-09-07 12:00 UTC", now)
        assert outcome == "terminal" and payload["final_status"] == "Cancelled", (outcome, payload)
    finally:
        bidwrangler.get_auction_detail = orig
    print("OK: resolve_one -> title text overrides to Postponed/Cancelled")


def test_resolve_one_gone():
    import requests

    class _FakeSession:
        pass

    def _raise_404(session, auction_id):
        resp = requests.Response()
        resp.status_code = 404
        raise requests.HTTPError(response=resp)

    orig = bidwrangler.get_auction_detail
    bidwrangler.get_auction_detail = _raise_404
    try:
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        [(outcome, payload)] = scrape_watch_bids.resolve_one(_FakeSession(), 999999, "2026-09-07 12:00 UTC", now)
        assert outcome == "gone" and payload is None, (outcome, payload)
    finally:
        bidwrangler.get_auction_detail = orig
    print("OK: resolve_one -> 404 classifies as gone")


# --- Multi-parcel real estate auctions, from live samples captured
# 2026-09-07 (see bidwrangler.is_multi_parcel_real_estate_auction /
# multi_parcel_items / build_multi_parcel_row) ---

MULTI_PARCEL_MIXED_STATUS = {
    "id": 990001, "items_count": 3, "complete": False, "archived": False,
    "name": "3 Parcels in Test County Selling to the Highest Bidders",
    "description": "", "location": None,
    "scheduled_end_time": "2026-09-10T22:00:00.000Z",
    "items": [
        {"name": "Subject One: 100 Main St, Testville, WV 25000", "status": "sold",
         "description_without_html": "0.5 Acres District 1, Map 1, Parcel 1",
         "api_bidding_state": {"high": {"amount": 50000}}},
        {"name": "Subject Two: 200 Oak Ave, Testville, WV 25000", "status": "accepting_bids",
         "description_without_html": "1.2 Acres District 1, Map 1, Parcel 2",
         "api_bidding_state": {"high": {"amount": 30000}}},
        {"name": "Subject Three: 300 Elm Rd, Testville, WV 25000", "status": "no_sale",
         "description_without_html": "0.8 Acres District 1, Map 1, Parcel 3",
         "api_bidding_state": {}},
    ],
}

MULTI_PARCEL_BUNDLE_SOLD = {
    "id": 990002, "items_count": 3, "complete": True, "archived": False,
    "name": "2 Parcels in Test County Selling to the Highest Bidders",
    "description": "", "location": None,
    "scheduled_end_time": "2026-09-05T22:00:00.000Z",
    "items": [
        {"name": "Subject One: 100 Main St, Testville, WV 25000", "status": "no_sale",
         "description_without_html": "0.5 Acres", "api_bidding_state": {}},
        {"name": "Subject Two: 200 Oak Ave, Testville, WV 25000", "status": "no_sale",
         "description_without_html": "1.2 Acres", "api_bidding_state": {}},
        {"name": "Subject #3: Property in Entirety", "status": "sold",
         "description_without_html": "Combination of Subjects 1-2",
         "api_bidding_state": {"high": {"amount": 90000}}},
    ],
}

PERSONAL_PROPERTY_MULTI_LOT = {
    "id": 990003, "items_count": 3, "complete": False, "archived": False,
    "name": "Morgantown, WV - Secured Party Auction - Delivery Trucks",
    "description": "", "location": None, "scheduled_end_time": None,
    "items": [
        {"name": "Preview Information", "status": "pending", "description_without_html": "No preview", "api_bidding_state": {}},
        {"name": "2003 Freightliner FedEx Parcel Delivery Truck", "status": "accepting_bids",
         "description_without_html": "VIN: 4UZAANCP43CL84774", "api_bidding_state": {}},
        {"name": "2004 Freightliner FedEx Parcel Delivery Truck", "status": "accepting_bids",
         "description_without_html": "VIN: 4UZAANCP94CL85145", "api_bidding_state": {}},
    ],
}


def test_is_multi_parcel_real_estate_auction():
    assert bidwrangler.is_multi_parcel_real_estate_auction(MULTI_PARCEL_MIXED_STATUS) is True
    assert bidwrangler.is_multi_parcel_real_estate_auction(MULTI_PARCEL_BUNDLE_SOLD) is True
    assert bidwrangler.is_multi_parcel_real_estate_auction(PERSONAL_PROPERTY_MULTI_LOT) is False
    # A single-lot auction (items_count == 1) is never multi-parcel.
    assert bidwrangler.is_multi_parcel_real_estate_auction(SAMPLE_DETAIL) is False
    print("OK: is_multi_parcel_real_estate_auction distinguishes multi-parcel land auctions "
          "from personal-property multi-lot auctions")


def test_multi_parcel_items_bundle_supersedes_individuals():
    mixed_ids = [i for i, _ in bidwrangler.multi_parcel_items(MULTI_PARCEL_MIXED_STATUS)]
    assert mixed_ids == [1, 2, 3], mixed_ids  # no bundle sold -- every individual parcel yielded

    bundle_ids = [i for i, _ in bidwrangler.multi_parcel_items(MULTI_PARCEL_BUNDLE_SOLD)]
    assert bundle_ids == [3], bundle_ids  # bundle (original position 3) sold -- only it is yielded,
    # and crucially its index is its OWN original position, not 1 (see multi_parcel_items docstring
    # on why a filtered-list-relative index would silently create duplicate rows across runs).
    print("OK: multi_parcel_items yields every parcel when no bundle sold, and preserves each "
          "item's true original index so parcel_key stays stable when a bundle sale collapses "
          "the yielded set down to one item")


def test_build_multi_parcel_row_land_vs_house_and_review_flag():
    s_top = bidwrangler.summarize_auction(MULTI_PARCEL_MIXED_STATUS)
    items = dict(bidwrangler.multi_parcel_items(MULTI_PARCEL_MIXED_STATUS))

    row1 = bidwrangler.build_multi_parcel_row(s_top, items[1], 1)
    assert row1["address"] == "100 Main St" and row1["city"] == "Testville" and row1["state"] == "WV"
    assert row1["acreage"] == 0.5 and row1["tax_district"] == "1" and row1["tax_parcel"] == "1"
    assert row1["parcel_key"].endswith("#1") and row1["review_flag"] is False

    # Unparseable address (no "Subject N:" address text) -> flagged for review,
    # not silently guessed.
    unparseable = {"name": "Subject #1: Main Facility on 13.5 +/- Acres", "status": "accepting_bids",
                   "description_without_html": "13.5 +/- Acres, no structure", "api_bidding_state": {}}
    row_unparseable = bidwrangler.build_multi_parcel_row(s_top, unparseable, 9)
    assert row_unparseable["review_flag"] is True and row_unparseable["city"] is None
    assert row_unparseable["address"]  # still has *something* usable, just unconfident

    # Acreage present, no sqft/bedroom language -> Land, not the "House" default.
    land_only = {"name": "Subject Two: Vacant Rd, Testville, WV 25000", "status": "sold",
                 "description_without_html": "3.0 +/- Acres, wooded, no structure", "api_bidding_state": {"high": {"amount": 8000}}}
    row_land = bidwrangler.build_multi_parcel_row(s_top, land_only, 2)
    assert row_land["property_type"] == "Land", row_land["property_type"]
    print("OK: build_multi_parcel_row parses address/acreage/tax-ref correctly, flags unparseable "
          "addresses for review instead of guessing, and classifies structureless acreage as Land")


def test_is_subject_item_name_handles_numbers_past_ten():
    """Regression test for a real bug found 2026-09-07 importing a genuine
    16-parcel auction: the old regex only recognized the number words "one"
    through "ten", so "Subject Eleven:" through "Subject Seventeen:" silently
    failed to match and those 7 parcels were dropped from every downstream
    table entirely (not flagged, not logged -- just missing)."""
    for name in (
        "Subject Eleven: 201 Astoria Road – Beckley, WV",
        "Subject Twelve: Hedrick Street - Beckley, WV",
        "Subject Seventeen: 127 Grear Ln - Beckley WV",
        "Subject #23: Some Later Parcel",
        "Subject 5: 34 Shaffer Lane Worthington, WV",
    ):
        assert parsing_utils.is_subject_item_name(name), name
    print("OK: is_subject_item_name matches 'Subject N:' for any parcel number, not just one..ten")


def test_parse_subject_address_run_on_name_flags_review_instead_of_blank_city():
    """Regression test for a real bug found 2026-09-07: an item name with no
    separator before the city (e.g. '1323 Adams Avenue Clarksburg, WV', vs.
    the usual 'Street – City, WV') let the trailing-city regex backtrack its
    whitespace into the city group, returning confident=True with a blank
    city and the city silently folded into the address. Must fall through to
    the not-confident case instead so the row gets flagged for review."""
    addr = parsing_utils.parse_subject_address(
        "Subject 1: 1323 Adams Avenue Clarksburg, WV",
        "1.053 +/- Total SF 0.08 +/- Acres (as assessed) Ranch Style 3 Bedroom",
    )
    assert addr["confident"] is False, addr
    assert addr["city"] is None, addr
    assert addr["address"], addr  # still keeps the raw text, just unconfident

    # Sanity: the normal dash-separated case still parses confidently.
    addr2 = parsing_utils.parse_subject_address("Subject Two: 261 Ronda Road - Dry Branch, WV 25061")
    assert addr2["confident"] is True and addr2["city"] == "Dry Branch", addr2
    print("OK: parse_subject_address flags a run-on 'street city, state' name for review instead "
          "of returning a confidently-wrong blank city")


def test_scrape_watch_bids_multi_parcel_lifecycle():
    """End-to-end: resolve_one() on a mixed-status multi-parcel auction
    yields one independent outcome per parcel (sold/active/unsold all at
    once from a single auction), matching real BidWrangler behavior."""
    import datetime

    class _FakeSession:
        pass

    orig = bidwrangler.get_auction_detail
    bidwrangler.get_auction_detail = lambda session, auction_id: MULTI_PARCEL_MIXED_STATUS
    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        results = scrape_watch_bids.resolve_one(_FakeSession(), 990001, "2026-09-07 12:00 UTC", now)
        assert len(results) == 3, results
        by_key = {payload["parcel_key"][-1]: (outcome, payload) for outcome, payload in results}
        assert by_key["1"][0] == "sold" and by_key["1"][1]["published_final_sold_price"] == 50000
        assert by_key["2"][0] == "active" and by_key["2"][1]["final_status"] == "Active"
        assert by_key["3"][0] == "terminal" and by_key["3"][1]["final_status"] == "Unsold"
    finally:
        bidwrangler.get_auction_detail = orig
    print("OK: scrape_watch_bids.resolve_one resolves each parcel of a multi-parcel auction "
          "independently (sold/active/unsold simultaneously from one auction page)")


if __name__ == "__main__":
    test_summarize_auction()
    test_parse_tax_reference()
    test_parse_sqft_from_text()
    test_compute_ppsf()
    test_get_assessment_detail_parsing()
    test_get_assessment_detail_fallback_to_card_sum()
    test_is_real_estate_auction()
    test_resolve_one_lifecycle()
    test_resolve_one_postponed_cancelled_text_override()
    test_resolve_one_gone()
    test_is_multi_parcel_real_estate_auction()
    test_multi_parcel_items_bundle_supersedes_individuals()
    test_build_multi_parcel_row_land_vs_house_and_review_flag()
    test_is_subject_item_name_handles_numbers_past_ten()
    test_parse_subject_address_run_on_name_flags_review_instead_of_blank_city()
    test_scrape_watch_bids_multi_parcel_lifecycle()
    print("\nAll manual smoke tests passed.")
