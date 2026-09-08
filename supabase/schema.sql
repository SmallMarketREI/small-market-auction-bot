-- Small Market Auction Bot -- Supabase schema
--
-- This file is a CONSOLIDATED snapshot of past_auctions/watch_auctions/scrape_runs,
-- folding in schema_v2.sql + schema_v3.sql + schema_v4.sql (verified 2026-09-08
-- against a live `select=*` on each table) plus schema_v5.sql's WV Assessment
-- appraisal/year-built/bed-bath columns -- those incremental files accumulated
-- real, applied changes over time but were never folded back into one file
-- until now. This is the source of truth going forward; the old incremental
-- files are left in this folder for history.
--
-- For a brand-new Supabase project, running this single file (once, in the SQL
-- Editor) is enough -- it creates both tables with every column and policy the
-- live database currently has. For an EXISTING project that's already run
-- schema.sql through schema_v4.sql, just run schema_v5.sql -- this file's
-- `create table if not exists` won't add columns to a table that already
-- exists.

create extension if not exists pgcrypto;

-- Past / sold auctions -------------------------------------------------------
create table if not exists past_auctions (
  id                    uuid primary key default gen_random_uuid(),
  bidwrangler_id        bigint,                    -- BidWrangler auction id (bid.joerpyleauctions.com)
  auction_company       text not null default 'joe_pyle_auctions',
  auction_date          date,
  address               text,
  city                  text,
  state                 text,
  zip                   text,
  property_type         text,
  property_class        text,                      -- e.g. WV Assessment's class ("F - Farm"); feeds the "may be the wrong parcel" review check
  published_final_sold_price numeric,
  status                text,
  title_notes           text,
  source_url            text,                       -- canonical detail URL for the auction page (no longer unique alone -- see parcel_key)
  parcel_key            text not null,               -- unique row identity: source_url for a single-lot auction, "<source_url>#1", "#2", ... for a multi-parcel auction
  parcel_label          text,                        -- optional human label for a multi-parcel row, e.g. "Subject Two"
  comp_sqft             numeric,
  comp_sqft_source      text,                       -- e.g. "WV Assessment (verified)", "Auction listing (stated)", "Public record"
  comp_sqft_source_url  text,
  comp_sqft_quality     text,
  comp_sqft_note        text,
  comp_deeded_acres     numeric,                    -- WV Assessment's own recorded acreage (distinct from `acreage` below, which is the auctioneer's stated figure)
  comp_land_value       numeric,                    -- WV Assessment land appraisal
  comp_building_value   numeric,                    -- WV Assessment building appraisal
  comp_total_appraisal  numeric,                    -- WV Assessment total appraisal (land + building)
  comp_year_built       integer,
  comp_bedrooms         integer,
  comp_full_baths       integer,
  comp_half_baths       integer,
  acreage               numeric,                    -- stated acreage for land-only parcels, so the dashboard can show $/acre instead of $/sqft
  review_flag           boolean not null default false,
  review_reason         text,
  tax_county            text,                       -- parsed/looked-up tax map reference, feeds the sqft enrichment job
  tax_district          text,
  tax_map               text,
  tax_parcel            text,
  lat                   double precision,
  lng                   double precision,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);

create index if not exists past_auctions_city_zip_idx on past_auctions (city, zip);
create index if not exists past_auctions_property_type_idx on past_auctions (property_type);
create index if not exists past_auctions_missing_sqft_idx on past_auctions (comp_sqft) where comp_sqft is null;
create index if not exists past_auctions_review_flag_idx on past_auctions (review_flag) where review_flag;
create index if not exists past_auctions_source_url_idx on past_auctions (source_url);
create index if not exists past_auctions_auction_company_idx on past_auctions (auction_company);
create unique index if not exists past_auctions_parcel_key_key on past_auctions (parcel_key);

-- Upcoming / watch auctions ---------------------------------------------------
create table if not exists watch_auctions (
  id                     uuid primary key default gen_random_uuid(),
  bidwrangler_id         bigint,
  auction_company        text not null default 'joe_pyle_auctions',
  auction_date           date,
  auction_time           text,
  address                text,
  city                   text,
  state                  text,
  zip                    text,
  title                  text,
  property_type          text,
  status                 text,
  final_status           text not null default 'Active',
  -- Active | Cancelled | Postponed | Unsold | Needs Review
  -- (Sold auctions never sit in this column's value for long -- the scraper
  -- moves them straight to past_auctions and removes the watch_auctions row.)
  final_status_reason    text,
  current_high_bid       numeric,
  current_bid_with_premium numeric,
  reserve_amount         numeric,
  bid_last_checked       text,
  bid_source_url         text,
  source_url             text,                       -- canonical detail URL (no longer unique alone -- see parcel_key)
  parcel_key             text not null,               -- unique row identity, same convention as past_auctions
  parcel_label           text,
  acreage                numeric,
  review_flag            boolean not null default false,
  review_reason          text,
  tax_county             text,
  tax_district           text,
  tax_map                text,
  tax_parcel             text,
  lat                    double precision,
  lng                    double precision,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now()
);

create index if not exists watch_auctions_city_zip_idx on watch_auctions (city, zip);
create index if not exists watch_auctions_final_status_idx on watch_auctions (final_status);
create index if not exists watch_auctions_review_flag_idx on watch_auctions (review_flag) where review_flag;
create index if not exists watch_auctions_source_url_idx on watch_auctions (source_url);
create index if not exists watch_auctions_auction_company_idx on watch_auctions (auction_company);
create unique index if not exists watch_auctions_parcel_key_key on watch_auctions (parcel_key);

-- Daily run log -> powers a live Audit tab ------------------------------------
create table if not exists scrape_runs (
  id               uuid primary key default gen_random_uuid(),
  run_at           timestamptz not null default now(),
  source           text not null,     -- 'past_sales' | 'watch_bids' | 'sqft_enrichment'
  records_found    integer,
  records_added    integer,
  records_updated  integer,
  errors           text,
  duration_ms      integer
);

-- Keep updated_at current on every upsert -------------------------------------
create or replace function set_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_past_auctions_updated_at on past_auctions;
create trigger trg_past_auctions_updated_at before update on past_auctions
  for each row execute function set_updated_at();

drop trigger if exists trg_watch_auctions_updated_at on watch_auctions;
create trigger trg_watch_auctions_updated_at before update on watch_auctions
  for each row execute function set_updated_at();

-- Row Level Security: public read-only, writes only via the service-role key --
alter table past_auctions enable row level security;
alter table watch_auctions enable row level security;
alter table scrape_runs enable row level security;

drop policy if exists "public read past_auctions" on past_auctions;
create policy "public read past_auctions" on past_auctions for select using (true);

drop policy if exists "public read watch_auctions" on watch_auctions;
create policy "public read watch_auctions" on watch_auctions for select using (true);

-- scrape_runs has NO select policy for anon/authenticated -- only the
-- service_role key (used by the GitHub Actions scrapers) can read or write it.
-- This is intentional: the repo and site are public, so this keeps internal
-- error/debug logs from being readable by anyone who knows to query the table
-- directly. If a policy named "public read scrape_runs" exists from an older
-- run of the original schema.sql, drop it:
drop policy if exists "public read scrape_runs" on scrape_runs;

-- No insert/update/delete policies are created for the anon/authenticated roles,
-- so the frontend (anon key) can only ever SELECT. All writes happen from the
-- GitHub Actions scrapers using the service_role key, which bypasses RLS entirely.
