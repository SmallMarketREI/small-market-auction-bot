"""Tiny Supabase REST (PostgREST) client -- no supabase-py dependency needed.

Uses the service_role key, which bypasses Row Level Security, so this must only
ever run server-side (GitHub Actions), never in the browser/frontend.
"""
import time
import requests

from . import config


def _headers(prefer=None):
    h = {
        "apikey": config.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def upsert(table: str, rows: list, on_conflict: str, retries: int = 3) -> dict:
    """Upsert a batch of rows into `table`, matching on `on_conflict` (a column
    name or comma-separated list that has a unique constraint, e.g. 'source_url').

    Returns {"inserted_or_updated": N} -- PostgREST's upsert doesn't distinguish
    inserts from updates in the response, so scrape_runs logs a single combined
    count. Good enough for the Audit tab; exact insert/update splits aren't worth
    the extra round trip.
    """
    config.require_supabase_config()
    if not rows:
        return {"inserted_or_updated": 0}

    # PostgREST rejects a batch where rows don't all share the exact same set
    # of keys (PGRST102). Normalize here so a caller that only sets a field
    # conditionally (e.g. "if stated_sqft: row['comp_sqft'] = ...") can't
    # break the whole batch -- every row gets every key seen anywhere in the
    # batch, filled with None where it was missing.
    all_keys = set()
    for row in rows:
        all_keys.update(row.keys())
    rows = [{k: row.get(k) for k in all_keys} for row in rows]

    url = f"{config.SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}"
    last_err = None
    for attempt in range(retries):
        resp = requests.post(
            url,
            headers=_headers(prefer="resolution=merge-duplicates,return=minimal"),
            json=rows,
            timeout=30,
        )
        if resp.status_code in (200, 201, 204):
            return {"inserted_or_updated": len(rows)}
        last_err = f"{resp.status_code}: {resp.text[:500]}"
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Supabase upsert to {table} failed after {retries} attempts: {last_err}")


def delete_not_in(table: str, column: str, keep_values: list) -> dict:
    """Delete every row in `table` whose `column` is NOT one of `keep_values`.

    Used to keep watch_auctions an exact mirror of "what's active right now"
    -- an auction that sold, got cancelled, or fell out of the live feed
    should disappear from Watch instead of lingering forever (which is how a
    scoping/parsing bug elsewhere can quietly leave hundreds of stale rows
    sitting in the table even after it's fixed).

    Refuses to run when `keep_values` is empty, rather than wiping the whole
    table -- a scrape that legitimately found zero rows is exactly the
    scenario we should NOT trust enough to delete everything on.
    """
    config.require_supabase_config()
    if not keep_values:
        print(f"[warn] delete_not_in({table}): keep_values is empty, skipping to avoid wiping the table")
        return {"skipped": True}
    url = f"{config.SUPABASE_URL}/rest/v1/{table}"
    resp = requests.delete(
        url,
        headers=_headers(prefer="return=minimal"),
        params={column: f"not.in.({','.join(keep_values)})"},
        timeout=30,
    )
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Supabase delete_not_in on {table} failed: {resp.status_code}: {resp.text[:500]}")
    return {"skipped": False}


def select(table: str, params: dict = None) -> list:
    """Simple SELECT via PostgREST. `params` are passed straight through as query
    params, e.g. {"comp_sqft": "is.null", "select": "id,city,tax_map,tax_parcel"}.
    """
    config.require_supabase_config()
    url = f"{config.SUPABASE_URL}/rest/v1/{table}"
    resp = requests.get(url, headers=_headers(), params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def update_by_id(table: str, row_id: str, fields: dict):
    config.require_supabase_config()
    url = f"{config.SUPABASE_URL}/rest/v1/{table}?id=eq.{row_id}"
    resp = requests.patch(url, headers=_headers(prefer="return=minimal"), json=fields, timeout=30)
    resp.raise_for_status()


def log_run(source: str, records_found=None, records_added=None, records_updated=None,
            errors: str = None, duration_ms: int = None):
    """Best-effort write to scrape_runs -- never raises, so a logging failure
    never masks the real scrape result."""
    try:
        upsert(
            "scrape_runs",
            [{
                "source": source,
                "records_found": records_found,
                "records_added": records_added,
                "records_updated": records_updated,
                "errors": errors,
                "duration_ms": duration_ms,
            }],
            on_conflict="id",
        )
    except Exception as e:  # noqa: BLE001
        print(f"[warn] could not log scrape_runs row for {source}: {e}")
