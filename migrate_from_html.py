#!/usr/bin/env python3
"""One-time migration: pull the `past` / `watch` arrays baked into the original
prototype (small_market_auction_bot_STEP2_WV_VIEWER.html) and seed Supabase
with them, so the hosted dashboard starts with real data on day one instead of
an empty database.

Usage:
    python migrate_from_html.py /path/to/small_market_auction_bot_STEP2_WV_VIEWER.html

Without SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY set, this runs in dry-run
mode and just writes fixtures/past_seed.json + fixtures/watch_seed.json so you
can inspect the parsed data first.

Note on source_url: the prototype's past-auction rows don't each have their
own permalink -- several rows share the same /results/P15-style listing-page
URL they were scraped from by hand. Since past_auctions.source_url is UNIQUE
(that's what makes the daily scraper's upserts idempotent), this script tags
each legacy row with a synthetic '#legacy-N' suffix so they don't collide with
each other -- and so they don't collide with the real per-auction URLs
(bid.joerpyleauctions.com/ui/auctions/{id}) the daily scraper will add going
forward. That does mean an auction present in this seed AND later
rediscovered by the scraper can end up as two rows; if that turns out to
matter in practice, de-dupe on (address, auction_date) in a follow-up pass.
"""
import json
import re
import sys
from pathlib import Path

from common import supabase_client
from common.config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY


def extract_array(text: str, name: str, stop_marker: str):
    start_token = f"const {name}="
    start = text.index(start_token) + len(start_token)
    end = text.index(stop_marker, start)
    snippet = text[start:end].rstrip()
    if snippet.endswith(";"):
        snippet = snippet[:-1]
    return json.loads(snippet)


def migrate(html_path: str):
    html = Path(html_path).read_text(encoding="utf-8")

    past_raw = extract_array(html, "past", "const watch=")
    watch_raw = extract_array(html, "watch", "const recent=")
    print(f"Parsed {len(past_raw)} past records and {len(watch_raw)} watch records from {html_path}")

    past_rows = []
    for i, r in enumerate(past_raw):
        row = dict(r)
        row["source_url"] = f"{r.get('source_url', 'legacy')}#legacy-{i}"
        past_rows.append(row)

    watch_rows = []
    for i, r in enumerate(watch_raw):
        row = dict(r)
        base = r.get("bid_source_url") or r.get("source_url") or "legacy"
        row["source_url"] = f"{base}#legacy-{i}"
        watch_rows.append(row)

    fixtures_dir = Path(__file__).parent / "fixtures"
    fixtures_dir.mkdir(exist_ok=True)
    (fixtures_dir / "past_seed.json").write_text(json.dumps(past_rows, indent=2))
    (fixtures_dir / "watch_seed.json").write_text(json.dumps(watch_rows, indent=2))
    print(f"Wrote fixtures/past_seed.json ({len(past_rows)} rows) and fixtures/watch_seed.json ({len(watch_rows)} rows)")

    if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        print("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set -- dry run only, nothing written to Supabase.")
        return

    result = supabase_client.upsert("past_auctions", past_rows, on_conflict="source_url")
    print(f"Upserted past_auctions: {result}")
    result = supabase_client.upsert("watch_auctions", watch_rows, on_conflict="source_url")
    print(f"Upserted watch_auctions: {result}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    migrate(sys.argv[1])
