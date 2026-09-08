#!/usr/bin/env python3
"""Manual smoke tests for common/geocoding.py -- not a pytest suite, just a
script to run by hand: `python tests_manual/test_geocoding.py`. No network
access required; the mocked responses below are real Census geocoder output
captured 2026-09-08 against actual past_auctions addresses.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from common import geocoding  # noqa: E402

# --- Real response for "815 17th St W, Huntington, WV 25704" (a genuine
# past_auctions row with a real house number) ---
MATCH_RESPONSE = {
    "result": {
        "input": {"address": {"address": "815 17th St W, Huntington, WV 25704"}},
        "addressMatches": [{
            "tigerLine": {"side": "L", "tigerLineId": "101335003"},
            "coordinates": {"x": -82.483041863108, "y": 38.407619019596},
            "addressComponents": {
                "zip": "25704", "streetName": "17TH", "city": "HUNTINGTON", "state": "WV",
                "suffixType": "ST", "suffixDirection": "W",
            },
            "matchedAddress": "815 17TH ST W, HUNTINGTON, WV, 25704",
        }],
    }
}

# --- Real response for "0 Adams Ave, Huntington, WV 25701" -- a vacant-land
# placeholder house number, which Census correctly can't match ---
NO_MATCH_RESPONSE = {
    "result": {
        "input": {"address": {"address": "0 Adams Ave, Huntington, WV 25701"}},
        "addressMatches": [],
    }
}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self._payload = payload
        self.last_params = None

    def get(self, url, params=None, timeout=None):
        self.last_params = params
        return _FakeResponse(self._payload)


def test_build_oneline_address_variants():
    assert geocoding.build_oneline_address("815 17th St W", "Huntington", "WV", "25704") == \
        "815 17th St W, Huntington, WV 25704"
    # No zip -- still a usable address, just shorter.
    assert geocoding.build_oneline_address("60 Deer Creek Drive", "Canvas", "WV", None) == \
        "60 Deer Creek Drive, Canvas, WV"
    # No street at all -- nothing worth geocoding, must return "" (the
    # caller uses this to skip the row entirely rather than sending a
    # request that can never usefully match).
    assert geocoding.build_oneline_address("", "Huntington", "WV", "25704") == ""
    assert geocoding.build_oneline_address(None, None, None, None) == ""
    # City/state missing but a zip is present -- still worth trying.
    assert geocoding.build_oneline_address("123 Main St", None, None, "25701") == "123 Main St, 25701"
    print("OK: build_oneline_address assembles a usable query string and blanks out cleanly "
          "when there's no street to search with")


def test_geocode_parses_a_confident_match():
    session = _FakeSession(MATCH_RESPONSE)
    result = geocoding.geocode(session, "815 17th St W", "Huntington", "WV", "25704")
    assert result is not None
    assert round(result["lat"], 4) == 38.4076
    assert round(result["lng"], 4) == -82.4830
    assert result["matched_address"] == "815 17TH ST W, HUNTINGTON, WV, 25704"
    # Confirms the one-line address actually sent matches what a real
    # past_auctions row would produce, not some other shape.
    assert session.last_params["address"] == "815 17th St W, Huntington, WV 25704"
    assert session.last_params["benchmark"] == "Public_AR_Current"
    print("OK: geocode() parses a real Census match response into lat/lng")


def test_geocode_returns_none_on_no_match():
    session = _FakeSession(NO_MATCH_RESPONSE)
    result = geocoding.geocode(session, "0 Adams Ave", "Huntington", "WV", "25701")
    assert result is None
    print("OK: geocode() returns None (not a crash, not a guessed coordinate) for a "
          "vacant-land placeholder address Census can't match")


def test_geocode_skips_rows_with_no_street_address():
    # If build_oneline_address would come back empty, geocode() must not
    # even make a request -- confirm the fake session's .get was never called.
    session = _FakeSession(MATCH_RESPONSE)
    result = geocoding.geocode(session, "", "Huntington", "WV", "25701")
    assert result is None
    assert session.last_params is None
    print("OK: geocode() skips the request entirely for a row with no street address")


if __name__ == "__main__":
    test_build_oneline_address_variants()
    test_geocode_parses_a_confident_match()
    test_geocode_returns_none_on_no_match()
    test_geocode_skips_rows_with_no_street_address()
    print("\nAll manual smoke tests passed.")
