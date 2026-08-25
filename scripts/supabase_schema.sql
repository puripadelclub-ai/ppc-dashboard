-- ============================================================
-- PPC Business OS — Supabase PostgreSQL Schema
-- Sprint 1: Database Foundation
-- Jalankan di: Supabase Dashboard → SQL Editor
-- ============================================================

-- Enable UUID extension
create extension if not exists "pgcrypto";


-- ============================================================
-- 1. COURTS
-- Master data 2 lapangan PPC
-- ============================================================
create table if not exists courts (
  id            serial primary key,
  court_name    text not null unique,        -- "Court 1", "Court 2"
  court_type    text default 'padel',
  active        boolean default true,
  created_at    timestamptz default now()
);

-- Seed data
insert into courts (court_name) values ('Court 1'), ('Court 2')
on conflict (court_name) do nothing;


-- ============================================================
-- 2. MEMBERS
-- Source of truth untuk semua member PPC
-- Saat ini diimport via bulk_import_members.py ke Supabase Auth
-- Tabel ini menyimpan profil bisnis (terpisah dari auth.users)
-- ============================================================
create table if not exists members (
  id                  uuid primary key default gen_random_uuid(),
  auth_user_id        uuid references auth.users(id) on delete set null,
  member_code         text unique,                -- contoh: "MD5R7"
  name                text not null,
  phone               text unique,                -- format: 08xxxxxxxxxx
  email               text,
  gender              text,
  date_of_birth       date,
  join_date           date,
  membership_type     text,                       -- "Regular", "Premium", dll
  membership_status   text default 'active',      -- active | inactive | expired
  expiry_date         date,
  acquisition_source  text,                       -- "ADS Campaign", "OTS", "WA", dll
  kode_ads            text,                       -- kode kampanye asal (dari sheet)
  free_coaching       boolean default false,
  free_racket_rental  boolean default false,
  notes               text,
  created_at          timestamptz default now(),
  updated_at          timestamptz default now(),
  last_booking_at     timestamptz,
  -- Import tracking
  import_source       text default 'bulk_import', -- bulk_import | manual | api
  imported_at         timestamptz default now()
);

create index if not exists members_phone_idx on members(phone);
create index if not exists members_member_code_idx on members(member_code);
create index if not exists members_status_idx on members(membership_status);


-- ============================================================
-- 3. BOOKINGS
-- Data booking lapangan dari AVM (ayo.co.id)
-- Satu baris = satu slot booking di satu lapangan
-- ============================================================
create table if not exists bookings (
  id                  uuid primary key default gen_random_uuid(),
  avm_id              text unique,               -- ID dari AVM API
  booking_date        date not null,
  court_id            integer references courts(id),
  court_name          text,                      -- denormalized untuk kemudahan
  start_time          time,
  end_time            time,
  duration_hours      numeric(4,2),
  period              text,                      -- Morning | Afternoon | Evening
  booking_code        text,                      -- BP1 | BP2 | BP | CBA | CO | R | RX | FT | REGULAR
  booking_type        text,                      -- "Ball Machine Baru", "Regular", dll
  customer_name       text,
  member_id           uuid references members(id) on delete set null,
  gross_amount        numeric(12,0) default 0,
  payment_method      text,
  reservation_type    text,
  final_status        text,                      -- confirmed | completed | cancelled | no_show
  -- Sync tracking
  synced_at           timestamptz default now(),
  created_at          timestamptz default now()
);

create index if not exists bookings_date_idx on bookings(booking_date);
create index if not exists bookings_code_idx on bookings(booking_code);
create index if not exists bookings_member_idx on bookings(member_id);
create index if not exists bookings_court_idx on bookings(court_id);


-- ============================================================
-- 4. TRANSACTIONS
-- Revenue & sales dari ESB (Google Sheets)
-- Satu baris = satu baris transaksi
-- ============================================================
create table if not exists transactions (
  id                  uuid primary key default gen_random_uuid(),
  transaction_date    date not null,
  category            text,                      -- Court Rent | Coaching Program | Membership | dll
  sub_category        text,
  product_name        text,
  gross_amount        numeric(12,0) not null default 0,
  discount            numeric(12,0) default 0,
  net_amount          numeric(12,0) generated always as (gross_amount - discount) stored,
  payment_method      text,
  customer_name       text,
  member_id           uuid references members(id) on delete set null,
  is_member           boolean default false,
  source              text default 'ESB',        -- ESB | AVM | Manual
  notes               text,
  -- Dedup key (hash dari date+category+amount untuk hindari duplikat import)
  row_hash            text unique,
  synced_at           timestamptz default now(),
  created_at          timestamptz default now()
);

create index if not exists transactions_date_idx on transactions(transaction_date);
create index if not exists transactions_category_idx on transactions(category);
create index if not exists transactions_member_idx on transactions(member_id);


-- ============================================================
-- 5. CAMPAIGNS
-- Meta Ads campaign master data
-- ============================================================
create table if not exists campaigns (
  id                  uuid primary key default gen_random_uuid(),
  campaign_id         text unique,               -- ID dari Meta API
  campaign_name       text not null,             -- "[045 - VID - Awareness - Batch 03.08]"
  campaign_num        text,                      -- "045"
  campaign_type       text,                      -- "SI" | "VID"
  offer_name          text,                      -- "Awareness" | "Membership" | "Student Trial"
  batch               text,                      -- "03.08"
  status              text,                      -- ACTIVE | PAUSED | ARCHIVED
  objective           text,
  created_at          timestamptz default now(),
  updated_at          timestamptz default now()
);

create index if not exists campaigns_offer_idx on campaigns(offer_name);


-- ============================================================
-- 6. CAMPAIGN_DAILY
-- Metrics harian per campaign dari Meta Ads
-- ============================================================
create table if not exists campaign_daily (
  id                  uuid primary key default gen_random_uuid(),
  campaign_id         uuid references campaigns(id) on delete cascade,
  campaign_meta_id    text,                      -- Meta campaign ID (denormalized)
  report_date         date not null,
  spend               numeric(12,0) default 0,
  impressions         integer default 0,
  reach               integer default 0,
  clicks              integer default 0,
  results             integer default 0,          -- conversions (Meta "results")
  cpr                 numeric(12,0)               -- cost per result (computed)
                        generated always as (
                          case when results > 0 then spend / results else null end
                        ) stored,
  -- Actual leads dari tracking manual (jika ada)
  actual_leads        integer,
  qualified_leads     integer,
  actual_bookings     integer,
  actual_revenue      numeric(12,0),
  synced_at           timestamptz default now(),
  -- Unique per campaign per hari
  unique (campaign_meta_id, report_date)
);

create index if not exists campaign_daily_date_idx on campaign_daily(report_date);
create index if not exists campaign_daily_campaign_idx on campaign_daily(campaign_id);


-- ============================================================
-- 7. PRODUCTS
-- Master produk/layanan PPC
-- ============================================================
create table if not exists products (
  id                  uuid primary key default gen_random_uuid(),
  name                text not null unique,
  category            text,                      -- court | membership | coaching | racket_rental | event | other
  price               numeric(12,0),
  member_price        numeric(12,0),
  active              boolean default true,
  created_at          timestamptz default now()
);

-- Seed produk PPC
insert into products (name, category, price) values
  ('Court Off Peak', 'court', null),
  ('Court Peak', 'court', null),
  ('Membership Regular', 'membership', null),
  ('Membership Premium', 'membership', null),
  ('Private Coaching', 'coaching', null),
  ('Coaching Clinic', 'coaching', null),
  ('Ball Machine', 'court', null),
  ('Racket Rental', 'racket_rental', null),
  ('Student Package', 'membership', null),
  ('Student Trial', 'membership', null),
  ('Event/Tournament', 'event', null),
  ('Beverage', 'other', null),
  ('Food', 'other', null)
on conflict (name) do nothing;


-- ============================================================
-- 8. KPI_DEFINITIONS
-- Satu definisi resmi per KPI — hindari formula berbeda di tiap modul
-- ============================================================
create table if not exists kpi_definitions (
  id                  serial primary key,
  kpi_key             text not null unique,       -- "occupancy", "cac", "cpr", "retention_rate"
  kpi_name            text not null,              -- "Occupancy Rate"
  formula             text not null,              -- "Booked Hours / Total Available Hours"
  unit                text,                       -- "%" | "IDR" | "count"
  description         text,
  source_table        text,                       -- tabel mana yang dipakai
  notes               text,
  created_at          timestamptz default now()
);

insert into kpi_definitions (kpi_key, kpi_name, formula, unit, description) values
  ('occupancy',
   'Occupancy Rate',
   'Booked Hours / Total Available Hours × 100',
   '%',
   'Total jam terisi / total jam tersedia (2 court × jam operasional). Morning=12jam, Afternoon=12jam, Evening=8jam, Total=32jam/hari'),
  ('cpr',
   'Cost Per Result',
   'Ad Spend / Results',
   'IDR',
   'Spend campaign dibagi jumlah hasil (conversions) dari Meta Ads. Semakin rendah semakin efisien.'),
  ('cac',
   'Customer Acquisition Cost',
   'Total Marketing Spend / New Customers Acquired',
   'IDR',
   'Total spend iklan dibagi jumlah customer baru yang berhasil booking dalam periode yang sama.'),
  ('retention_rate',
   'Member Retention Rate',
   'Members Active This Month Who Were Active Last Month / Members Active Last Month × 100',
   '%',
   'Persentase member bulan lalu yang masih aktif di bulan ini.'),
  ('mom_revenue',
   'Revenue Month-over-Month Growth',
   '(Revenue This Month - Revenue Last Month) / Revenue Last Month × 100',
   '%',
   'Pertumbuhan revenue dibanding bulan sebelumnya.'),
  ('cir',
   'Cost-to-Income Ratio',
   'Total Ad Spend / Total Revenue × 100',
   '%',
   'Proporsi biaya iklan terhadap total revenue. Target < 20%.'),
  ('ltv',
   'Customer Lifetime Value',
   'Average Revenue per Member × Average Member Lifespan (months)',
   'IDR',
   'Estimasi total revenue dari satu member selama aktif.'),
  ('roas',
   'Return on Ad Spend',
   'Revenue Attributed to Ads / Total Ad Spend',
   'x',
   'Berapa rupiah revenue yang dihasilkan per rupiah iklan.')
on conflict (kpi_key) do nothing;


-- ============================================================
-- 9. SYNC_LOGS
-- Pipeline observability — tiap sync job catat hasilnya
-- ============================================================
create table if not exists sync_logs (
  id                  uuid primary key default gen_random_uuid(),
  source              text not null,              -- "AVM" | "ESB" | "META_ADS" | "MEMBERS"
  job_name            text,                       -- "fetch_bookings_range" | "fetch_ads_insights"
  started_at          timestamptz not null,
  completed_at        timestamptz,
  status              text not null default 'running',  -- running | success | failed | partial
  rows_fetched        integer default 0,
  rows_inserted       integer default 0,
  rows_updated        integer default 0,
  rows_skipped        integer default 0,
  rows_failed         integer default 0,
  date_range_start    date,
  date_range_end      date,
  error_message       text,
  metadata            jsonb,                      -- info tambahan bebas
  created_at          timestamptz default now()
);

create index if not exists sync_logs_source_idx on sync_logs(source);
create index if not exists sync_logs_status_idx on sync_logs(status);
create index if not exists sync_logs_started_idx on sync_logs(started_at desc);


-- ============================================================
-- 10. DAILY_SUMMARIES
-- Pre-aggregated daily metrics — dibaca dashboard via KV cache
-- Ini yang mengisi Vercel KV, bukan raw tables
-- ============================================================
create table if not exists daily_summaries (
  id                  uuid primary key default gen_random_uuid(),
  summary_date        date not null unique,
  -- Occupancy
  total_bookings      integer default 0,
  regular_bookings    integer default 0,
  machine_bookings    integer default 0,
  coaching_bookings   integer default 0,
  complimentary_bookings integer default 0,
  booked_hours        numeric(5,2) default 0,
  total_hours         integer default 32,          -- 2 courts × 16 jam
  occupancy_pct       numeric(5,4) default 0,
  morning_occ_pct     numeric(5,4) default 0,
  afternoon_occ_pct   numeric(5,4) default 0,
  evening_occ_pct     numeric(5,4) default 0,
  -- Revenue (dari ESB)
  total_revenue       numeric(12,0) default 0,
  court_revenue       numeric(12,0) default 0,
  coaching_revenue    numeric(12,0) default 0,
  membership_revenue  numeric(12,0) default 0,
  other_revenue       numeric(12,0) default 0,
  -- Ads
  total_ad_spend      numeric(12,0) default 0,
  total_results       integer default 0,
  -- Computed at aggregate time
  computed_at         timestamptz default now()
);

create index if not exists daily_summaries_date_idx on daily_summaries(summary_date);


-- ============================================================
-- ROW LEVEL SECURITY (RLS)
-- Enable tapi buat policy minimal dulu — owner bisa semua
-- ============================================================
alter table members           enable row level security;
alter table bookings          enable row level security;
alter table transactions      enable row level security;
alter table campaigns         enable row level security;
alter table campaign_daily    enable row level security;
alter table sync_logs         enable row level security;
alter table daily_summaries   enable row level security;
alter table products          enable row level security;
alter table kpi_definitions   enable row level security;
alter table courts            enable row level security;

-- Policy: service_role bisa semua (untuk ETL pipeline)
-- Policy: authenticated users bisa SELECT sesuai role (expand nanti)
create policy "service_role full access members"
  on members for all using (auth.role() = 'service_role');
create policy "authenticated read members"
  on members for select using (auth.role() = 'authenticated');

create policy "service_role full access bookings"
  on bookings for all using (auth.role() = 'service_role');
create policy "authenticated read bookings"
  on bookings for select using (auth.role() = 'authenticated');

create policy "service_role full access transactions"
  on transactions for all using (auth.role() = 'service_role');
create policy "authenticated read transactions"
  on transactions for select using (auth.role() = 'authenticated');

create policy "service_role full access campaigns"
  on campaigns for all using (auth.role() = 'service_role');
create policy "authenticated read campaigns"
  on campaigns for select using (auth.role() = 'authenticated');

create policy "service_role full access campaign_daily"
  on campaign_daily for all using (auth.role() = 'service_role');
create policy "authenticated read campaign_daily"
  on campaign_daily for select using (auth.role() = 'authenticated');

create policy "service_role full access sync_logs"
  on sync_logs for all using (auth.role() = 'service_role');
create policy "authenticated read sync_logs"
  on sync_logs for select using (auth.role() = 'authenticated');

create policy "service_role full access daily_summaries"
  on daily_summaries for all using (auth.role() = 'service_role');
create policy "authenticated read daily_summaries"
  on daily_summaries for select using (auth.role() = 'authenticated');

create policy "service_role full access products"
  on products for all using (auth.role() = 'service_role');
create policy "authenticated read products"
  on products for select using (auth.role() = 'authenticated');

create policy "authenticated read kpi_definitions"
  on kpi_definitions for select using (auth.role() = 'authenticated');
create policy "service_role full access kpi_definitions"
  on kpi_definitions for all using (auth.role() = 'service_role');

create policy "authenticated read courts"
  on courts for select using (auth.role() = 'authenticated');
create policy "service_role full access courts"
  on courts for all using (auth.role() = 'service_role');


-- ============================================================
-- UPDATED_AT trigger helper
-- ============================================================
create or replace function update_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger members_updated_at
  before update on members
  for each row execute function update_updated_at();

create trigger campaigns_updated_at
  before update on campaigns
  for each row execute function update_updated_at();


-- ============================================================
-- DONE
-- Tabel yang dibuat:
--   courts, members, bookings, transactions,
--   campaigns, campaign_daily, products,
--   kpi_definitions, sync_logs, daily_summaries
-- ============================================================
