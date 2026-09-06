"""Environment configuration shared by every script.

Locally, put these in a `.env` file (never committed -- see .gitignore) and
`source` it, or export them in your shell. In GitHub Actions they come from
repo secrets (see .github/workflows/daily-update.yml).
"""
import os

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# A friendly, identifying User-Agent for the scrapers. Being a good citizen on
# sites we hit daily and unattended: identify ourselves and keep request rates low.
USER_AGENT = os.environ.get(
    "SCRAPER_USER_AGENT",
    "SmallMarketAuctionBot/1.0 (+internal tool for Small Market REI; contact: "
    + os.environ.get("SCRAPER_CONTACT_EMAIL", "elijahchatcabella@gmail.com") + ")",
)

REQUEST_DELAY_SECONDS = float(os.environ.get("SCRAPER_REQUEST_DELAY_SECONDS", "1.5"))


def require_supabase_config():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set (env vars or "
            "GitHub Actions secrets) before writing data. Read-only recon can run "
            "without them."
        )
