-- ============================================================
-- Fix: RLS performance advisor findings
-- - auth_rls_initplan (WARN): auth.role() dievaluasi ulang per baris
-- - multiple_permissive_policies (WARN): 2 policy overlap di action SELECT
--
-- Kenapa aman menghapus policy "service_role full access ...":
-- Role `service_role` di Supabase selalu punya atribut BYPASSRLS,
-- jadi RLS di-skip total untuk role ini terlepas dari isi policy-nya.
-- Policy itu tidak pernah benar-benar mengizinkan/menolak apa pun secara
-- praktis — menghapusnya TIDAK mengubah akses service_role (pipeline
-- Flask tetap full access seperti biasa).
--
-- Hasil: 1 policy SELECT per tabel untuk role `authenticated`, ditulis
-- pakai (select auth.role()) supaya dihitung sekali per query, bukan
-- per baris. Anon & role lain tetap tidak punya akses (default deny).
--
-- Jalankan di: Supabase SQL Editor / MCP apply_migration
-- ============================================================

-- 1. members
drop policy if exists "service_role full access members" on members;
drop policy if exists "authenticated read members" on members;
create policy "authenticated read members" on members
  for select using ((select auth.role()) = 'authenticated');

-- 2. bookings
drop policy if exists "service_role full access bookings" on bookings;
drop policy if exists "authenticated read bookings" on bookings;
create policy "authenticated read bookings" on bookings
  for select using ((select auth.role()) = 'authenticated');

-- 3. transactions
drop policy if exists "service_role full access transactions" on transactions;
drop policy if exists "authenticated read transactions" on transactions;
create policy "authenticated read transactions" on transactions
  for select using ((select auth.role()) = 'authenticated');

-- 4. campaigns
drop policy if exists "service_role full access campaigns" on campaigns;
drop policy if exists "authenticated read campaigns" on campaigns;
create policy "authenticated read campaigns" on campaigns
  for select using ((select auth.role()) = 'authenticated');

-- 5. campaign_daily
drop policy if exists "service_role full access campaign_daily" on campaign_daily;
drop policy if exists "authenticated read campaign_daily" on campaign_daily;
create policy "authenticated read campaign_daily" on campaign_daily
  for select using ((select auth.role()) = 'authenticated');

-- 6. sync_logs
drop policy if exists "service_role full access sync_logs" on sync_logs;
drop policy if exists "authenticated read sync_logs" on sync_logs;
create policy "authenticated read sync_logs" on sync_logs
  for select using ((select auth.role()) = 'authenticated');

-- 7. daily_summaries
drop policy if exists "service_role full access daily_summaries" on daily_summaries;
drop policy if exists "authenticated read daily_summaries" on daily_summaries;
create policy "authenticated read daily_summaries" on daily_summaries
  for select using ((select auth.role()) = 'authenticated');

-- 8. products
drop policy if exists "service_role full access products" on products;
drop policy if exists "authenticated read products" on products;
create policy "authenticated read products" on products
  for select using ((select auth.role()) = 'authenticated');

-- 9. kpi_definitions
drop policy if exists "service_role full access kpi_definitions" on kpi_definitions;
drop policy if exists "authenticated read kpi_definitions" on kpi_definitions;
create policy "authenticated read kpi_definitions" on kpi_definitions
  for select using ((select auth.role()) = 'authenticated');

-- 10. courts
drop policy if exists "service_role full access courts" on courts;
drop policy if exists "authenticated read courts" on courts;
create policy "authenticated read courts" on courts
  for select using ((select auth.role()) = 'authenticated');

-- 11. ads_daily
drop policy if exists "service_role full access ads_daily" on ads_daily;
drop policy if exists "authenticated read ads_daily" on ads_daily;
create policy "authenticated read ads_daily" on ads_daily
  for select using ((select auth.role()) = 'authenticated');

-- ============================================================
-- Verifikasi setelah run — harus 1 baris per tabel di atas
-- ============================================================
select tablename, policyname, cmd
from pg_policies
where schemaname = 'public'
  and tablename in (
    'members','bookings','transactions','campaigns','campaign_daily',
    'sync_logs','daily_summaries','products','kpi_definitions','courts','ads_daily'
  )
order by tablename;
