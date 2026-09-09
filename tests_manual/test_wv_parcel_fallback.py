#!/usr/bin/env python3
"""Manual smoke tests for wv_assessment.lookup_parcel_by_address_text() and its
address-parsing helper -- not a pytest suite, just a script to run by hand:
`python tests_manual/test_wv_parcel_fallback.py`. No network access required;
the mocked responses below are real WV ArcGIS parcel-layer output captured
2026-09-09 against actual past_auctions addresses stuck in the "No
coordinates" review bucket.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from common import wv_assessment  # noqa: E402

# --- Real response for "424 Dusty Field Dr, Poca, WV" -- an exact match ---
DUSTY_FIELD_MATCH = {
    "features": [
        {
            "attributes": {"COUNTY": "20", "Dist": "24", "Map": "0005", "Parcel": "0033",
                            "FullPhysicalAddress": "424 DUSTY FIELD DR"},
            "geometry": {"rings": [[
                [-81.699951558921526, 38.603689622245909],
                [-81.700327303376483, 38.603356208635773],
                [-81.700263518844778, 38.60329238291726],
                [-81.700984212976195, 38.603005025605938],
                [-81.701177815188061, 38.603927236751446],
                [-81.701315110700421, 38.604581219152294],
                [-81.701002919492964, 38.6047295399161],
                [-81.699951558921526, 38.603689622245909],
            ]]},
        },
    ],
}

# --- Real response for "2998 Owl Creek Road, Morgantown, WV" -- the street
# exists in county data, but not that house number (closest on file: 3011,
# 3001, 3330, 3368, 3388) -- a genuine gap, not a bug ---
OWL_CREEK_NO_EXACT_MATCH = {
    "features": [
        {"attributes": {"COUNTY": "31", "Dist": "05", "Map": "0013", "Parcel": "0035",
                         "FullPhysicalAddress": "3368 OWL CREEK RD"}, "geometry": {"rings": [[[0, 0], [0, 1], [1, 1], [0, 0]]]}},
        {"attributes": {"COUNTY": "31", "Dist": "05", "Map": "0018", "Parcel": "0006",
                         "FullPhysicalAddress": "3011 OWL CREEK RD"}, "geometry": {"rings": [[[0, 0], [0, 1], [1, 1], [0, 0]]]}},
    ],
}

# --- Real response for "111 Mylan Park Dr, Morgantown, WV" -- county has
# this road as "Mylan Park LN", numbers 101/270/300/400/889, no "111" under
# either spelling ---
MYLAN_PARK_NO_EXACT_MATCH = {
    "features": [
        {"attributes": {"COUNTY": "31", "Dist": "07", "Map": "006B", "Parcel": "0002",
                         "FullPhysicalAddress": "101 MYLAN PARK LN"}, "geometry": {"rings": [[[0, 0], [0, 1], [1, 1], [0, 0]]]}},
        {"attributes": {"COUNTY": "31", "Dist": "07", "Map": "0006", "Parcel": "0030",
                         "FullPhysicalAddress": "300 MYLAN PARK LN"}, "geometry": {"rings": [[[0, 0], [0, 1], [1, 1], [0, 0]]]}},
    ],
}

# --- Synthetic: two features share a house number but disagree on
# County/Map/Parcel -- must be treated as ambiguous, not guessed ---
AMBIGUOUS_MATCH = {
    "features": [
        {"attributes": {"COUNTY": "20", "Dist": "01", "Map": "0001", "Parcel": "0001",
                         "FullPhysicalAddress": "100 MAIN ST"}, "geometry": {"rings": [[[0, 0], [0, 1], [1, 1], [0, 0]]]}},
        {"attributes": {"COUNTY": "20", "Dist": "02", "Map": "0002", "Parcel": "0002",
                         "FullPhysicalAddress": "100 MAIN ST"}, "geometry": {"rings": [[[0, 0], [0, 1], [1, 1], [0, 0]]]}},
    ],
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


def test_parse_house_number_and_street():
    assert wv_assessment._parse_house_number_and_street("2998 Owl Creek Road") == ("2998", "OWL CREEK")
    assert wv_assessment._parse_house_number_and_street("111 Mylan Park Dr") == ("111", "MYLAN PARK")
    # Suffix-less street name -- nothing to strip, still fine.
    assert wv_assessment._parse_house_number_and_street("424 Dusty Field") == ("424", "DUSTY FIELD")
    # "0 [Street]" placeholder -- vacant land, not a real number to search with.
    assert wv_assessment._parse_house_number_and_street("0 Adams Ave") == (None, None)
    # Bare street name, no house number at all.
    assert wv_assessment._parse_house_number_and_street("Owl Creek Road") == (None, None)
    assert wv_assessment._parse_house_number_and_street("") == (None, None)
    assert wv_assessment._parse_house_number_and_street(None) == (None, None)
    print("OK: _parse_house_number_and_street splits a real address and blanks out "
          "cleanly for placeholders and bare street names")


def test_lookup_finds_an_exact_match():
    session = _FakeSession(DUSTY_FIELD_MATCH)
    result = wv_assessment.lookup_parcel_by_address_text(session, "424 Dusty Field Dr")
    assert result is not None
    assert result["county"] == "20"
    assert result["map"] == "0005"
    assert result["parcel"] == "0033"
    assert result["matched_address"] == "424 DUSTY FIELD DR"
    assert result["lat"] is not None and result["lng"] is not None
    # Sent query actually anchors on the house number, not a loose substring search.
    assert "424 DUSTY FIELD%" in session.last_params["where"]
    print("OK: lookup_parcel_by_address_text() resolves a real exact house-number match "
          "to its County/Map/Parcel and a centroid lat/lng")


def test_lookup_returns_none_when_house_number_absent():
    session = _FakeSession(OWL_CREEK_NO_EXACT_MATCH)
    result = wv_assessment.lookup_parcel_by_address_text(session, "2998 Owl Creek Road")
    assert result is None
    print("OK: lookup_parcel_by_address_text() returns None (not the nearest guess) when "
          "the exact house number isn't in county records either")


def test_lookup_returns_none_on_suffix_mismatch_with_no_number_match():
    session = _FakeSession(MYLAN_PARK_NO_EXACT_MATCH)
    result = wv_assessment.lookup_parcel_by_address_text(session, "111 Mylan Park Dr")
    assert result is None
    print("OK: lookup_parcel_by_address_text() survives a street-suffix mismatch (Dr vs "
          "LN) via the query, but still correctly returns None when no house number matches")


def test_lookup_returns_none_when_ambiguous():
    session = _FakeSession(AMBIGUOUS_MATCH)
    result = wv_assessment.lookup_parcel_by_address_text(session, "100 Main St")
    assert result is None
    print("OK: lookup_parcel_by_address_text() refuses to guess between two features "
          "sharing a house number but disagreeing on County/Map/Parcel")


def test_lookup_skips_rows_with_no_usable_address():
    session = _FakeSession(DUSTY_FIELD_MATCH)
    assert wv_assessment.lookup_parcel_by_address_text(session, "0 Some Road") is None
    assert session.last_params is None
    assert wv_assessment.lookup_parcel_by_address_text(session, "") is None
    print("OK: lookup_parcel_by_address_text() skips the request entirely for a "
          "placeholder or empty address")


if __name__ == "__main__":
    test_parse_house_number_and_street()
    test_lookup_finds_an_exact_match()
    test_lookup_returns_none_when_house_number_absent()
    test_lookup_returns_none_on_suffix_mismatch_with_no_number_match()
    test_lookup_returns_none_when_ambiguous()
    test_lookup_skips_rows_with_no_usable_address()
    print("\nAll manual smoke tests passed.")
