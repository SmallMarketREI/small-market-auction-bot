# Small Market Auction Bot

A hosted, self-updating version of the auction-tracking dashboard: past Joe R.
Pyle auction sales, upcoming auctions with live bid tracking, and official WV
square footage -- refreshed automatically every day via GitHub Actions, stored
in a free Supabase Postgres database, served as a static site on GitHub Pages.

See `/root/.claude/plans/shimmering-fluttering-thimble.md` (or ask Claude) for
the full architecture writeup and the recon notes on how each data source
works. Short version: **no headless browser is needed anywhere** -- all three
data sources (past sales, live bids, and WV square footage) turned out to have
clean, scriptable HTTP endpoints once we looked at what the pages actually
call under the hood.

## What's in this repo

```
common/                  shared Python: Supabase client, BidWrangler client, WV Assessment client
migrate_from_html.py     one-time: seeds Supabase from the original prototype's baked-in data
scrape_past_sales.py     daily: finds newly-sold auctions, upserts past_auctions
scrape_watch_bids.py     daily: current bid state for upcoming auctions, upserts watch_auctions
enrich_sqft_wv.py        daily: fills in official square footage from WV Assessment records
supabase/schema.sql      the database schema + row-level-security policies
site/index.html          the dashboard itself (same UI as the prototype, reads live from Supabase)
.github/workflows/       the daily cron job that runs the three scripts above
tests_manual/            offline smoke tests against real captured sample data (no network needed)
```

## One-time setup

### 1. Create a GitHub repo

1. Create a **private** repo (this is your business data) at github.com, e.g. `small-market-auction-bot`.
2. Push everything in this folder to it:
   ```
   git init
   git add .
   git commit -m "Initial hosted auction bot"
   git branch -M main
   git remote add origin <your-repo-url>
   git push -u origin main
   ```

### 2. Create a Supabase project

1. Sign up at [supabase.com](https://supabase.com) (free tier is plenty for this).
2. Create a new project. Save the database password somewhere safe.
3. Open **SQL Editor -> New query**, paste in the contents of `supabase/schema.sql`, and run it. This creates `past_auctions`, `watch_auctions`, `scrape_runs`, and locks writes down to the service-role key only (see the RLS comments in that file).
4. Go to **Project Settings -> API**. You'll need three values:
   - **Project URL** (`SUPABASE_URL`)
   - **anon / public key** (`SUPABASE_ANON_KEY`) -- safe to expose in the frontend, read-only thanks to RLS
   - **service_role key** (`SUPABASE_SERVICE_ROLE_KEY`) -- **secret**, never put this in the site, only in GitHub Actions secrets

### 3. Seed the database from the existing prototype

Locally (with Python 3.10+):
```
pip install -r requirements.txt
export SUPABASE_URL="https://xxxx.supabase.co"
export SUPABASE_SERVICE_ROLE_KEY="ey..."
python migrate_from_html.py /path/to/small_market_auction_bot_STEP2_WV_VIEWER.html
```
This loads the 233 past + ~70 watch records already sitting in the prototype's HTML so the dashboard isn't empty on day one. Run it without the env vars first if you just want to sanity-check the parsed data in `fixtures/*.json` before writing anything.

### 4. Wire up the frontend

Open `site/index.html` and replace the two placeholder constants near the bottom with your real values:
```js
const SUPABASE_URL = "https://xxxx.supabase.co";
const SUPABASE_ANON_KEY = "ey...";   // the anon key, NOT the service_role key
```
Commit that change.

### 5. Turn on GitHub Pages

Repo **Settings -> Pages -> Source**: deploy from a branch, `main`, folder `/site`. GitHub will give you a URL like `https://<you>.github.io/small-market-auction-bot/` -- that's the dashboard.

Since it's a private repo, GitHub Pages built from a private repo needs GitHub Pro/Team, OR the `/site` folder can be pushed to a small separate **public** repo just for the site (the underlying data still lives in Supabase, so the published HTML+JS doesn't contain anything sensitive beyond the Supabase URL/anon key, which are meant to be public-safe). If keeping everything in one private repo without a paid plan matters to you, say so and we'll switch the frontend to Vercel or Netlify's free tier instead -- same result, different host.

Access is kept simple for now: the page is public-but-unlisted (no login), matching "just me + a couple teammates who all have the link." If access needs ever grow past a handful of known people, that's the point to add real Supabase auth -- flag it and we'll wire that in.

### 6. Add GitHub Actions secrets

Repo **Settings -> Secrets and variables -> Actions -> New repository secret**:
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SCRAPER_CONTACT_EMAIL` (optional -- goes into the scraper's User-Agent string as a courtesy to the sites we hit daily)

### 7. Test it

- **Actions tab -> Daily auction data update -> Run workflow** to trigger it manually instead of waiting for the schedule.
- Check the run logs for each of the three steps.
- Open the dashboard URL and confirm new/updated rows show up (the Audit tab shows the last 20 scrape_runs entries).

After that, it runs on its own every day (see the `cron` line in `.github/workflows/daily-update.yml` -- currently 09:00 America/New_York, since that's where the properties are; change it if you'd rather it run at a different time).

## How each data source actually works (for future maintenance)

- **Past sales**: `joerpyleauctions.com/results` (and `/results/P15`, `/P30`, ...) link every auction as `/auctions/detail/bw{id}`. That numeric id is a **BidWrangler** auction platform id (the same platform running `bid.joerpyleauctions.com`). `GET bid.joerpyleauctions.com/api/auctions/{id}?page=active&include_items_data=true` returns full JSON -- address, lat/lng, final winning bid, and the listing's item description (which is where the auctioneer's stated sq ft and the county tax District/Map/Parcel show up, e.g. "District 19, Map 4G, Parcel 71").
- **Live bids / watch**: `bid.joerpyleauctions.com/api/feed/all` lists every currently active/upcoming auction id directly -- same detail endpoint as above gives current high bid, ask amount, and reserve.
- **Square footage**: WV's official parcel GIS (`services.wvgis.wvu.edu/.../WV_Parcels/MapServer/0`) is a public ArcGIS layer you can query by lat/lng to get the authoritative County/District/Map/Parcel for a point -- but it doesn't carry building square footage. That lives in the separate WV Real Estate Assessment app (`mapwv.gov/assessment`), an older ASP.NET search form; searching it by County+Map+Parcel resolves a Root Parcel ID, and `mapwv.gov/Assessment/Detail/?PID={id}` is a plain, static, GET-able page with a "Sum of Structure Areas" figure -- confirmed during setup to match the auctioneer's own stated square footage exactly for a live test listing.

None of this needs Playwright/Selenium -- it's all `requests` + `BeautifulSoup`. If any of these sites change their markup or API shape, `tests_manual/test_parsing.py` is a good first place to update with a fresh sample and confirm the parsing logic before touching the real scrapers.

## Known limitations / good next steps

- `migrate_from_html.py`'s seeded rows use synthetic `#legacy-N` source URLs (the original prototype didn't have a unique link per sold auction). If the daily scraper later rediscovers one of those same historical sales under its real URL, you'll get a duplicate row -- fine for now, worth a de-dupe pass on (address, auction_date) later if it becomes annoying.
- `enrich_sqft_wv.py` only resolves a county when it can point-in-polygon match the listing's lat/lng against the WV parcel layer. A listing with no lat/lng (shouldn't happen from BidWrangler, but just in case) is skipped and logged, not guessed at.
- Property type is currently hardcoded to "House" for everything the scraper adds (matches every listing seen so far); worth revisiting if Pyle starts running land-only or commercial lots through the same feed.
- No auth yet (see step 5) -- fine for "me + a few teammates with the link," worth adding if that group grows.
