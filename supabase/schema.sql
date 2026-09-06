-- Small Market Auction Bot -- Supabase schema
-- Run this once in the Supabase SQL editor (Project -> SQL Editor -> New query) before
-- running migrate_from_html.py.

create extension if not exists pgcrypto;

-- Past / sold auctions -------------------------------------------------------
create table if not exists past_auctions (
  id                    uuid primary key default gen_random_uuid(),
  bidwrangler_id        bigint,                    -- BidWrangler auction id (bid.joerpyleauctions.com)
  auction_date          date,
  address               text,
  city                  text,
  state                 text,
  zip                   text,
  property_type         text,
  published_final_sold_price numeric,
  status                text,
  title_notes           text,
  source_url            text unique,               -- canonical detail URL; unique -> makes daily upsert idempotent
  comp_sqft             numeric,
  comp_sqft_source      text,                       -- e.g. "WV Assessment (verified)", "Auction listing (stated)", "Public record"
  comp_sqft_source_url  text,
  comp_sqft_quality     text,
  comp_sqft_note        text,
  tax_county            text,                       -- parsed/looked-up tax map reference, feeds the sqft enrichment job
  tax_district          text,
  tax_map               text,
  tax_parcel             text,
  lat                   double precision,
  lng                   double precision,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);

create index if not exists past_auctions_city_zip_idx on past_auctions (city, zip);
create index if not exists past_auctions_property_type_idx on past_auctions (property_type);
create index if not exists past_auctions_missing_sqft_idx on past_auctions (comp_sqft) where comp_sqft is null;

-- Upcoming / watch auctions ---------------------------------------------------
create table if not exists watch_auctions (
  id                     uuid primary key default gen_random_uuid(),
  bidwrangler_id         bigint,
  auction_date           date,
  auction_time           text,
  address                text,
  city                   text,
  state                  text,
  zip                    text,
  title                  text,
  property_type          text,
  status                 text,
  current_high_bid       numeric,
  current_bid_with_premium numeric,
  reserve_amount         numeric,
  bid_last_checked       text,
  bid_source_url         text,
  source_url             text unique,
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

drop policy if exists "public read scrape_runs" on scrape_runs;
create policy "public read scrape_runs" on scrape_runs for select using (true);

-- No insert/update/delete policies are created for the anon/authenticated roles,
-- so the frontend (anon key) can only ever SELECT. All writes happen from the
-- GitHub Actions scrapers using the service_role key, which bypasses RLS entirely.
