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


if __name__ == "__main__":
    test_summarize_auction()
    test_parse_tax_reference()
    test_parse_sqft_from_text()
    test_compute_ppsf()
    test_get_assessment_detail_parsing()
    test_get_assessment_detail_fallback_to_card_sum()
    print("\nAll manual smoke tests passed.")
