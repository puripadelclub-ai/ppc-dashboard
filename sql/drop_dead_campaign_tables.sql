-- ============================================================
-- Drop dead tables: campaign_daily & campaigns
--
-- Terverifikasi tidak dipakai kode manapun (2026-09-14):
-- - upsert_campaigns() / upsert_campaign_daily() didefinisikan di
--   lib/supabase_client.py tapi TIDAK dipanggil di mana pun di repo
-- - dashboard.html tidak ada _SB.from('campaigns') / _SB.from('campaign_daily')
-- - "campaigns" key di /api/dashboard-data (process.py) berasal dari
--   Google Sheets TAB_OUT_CAMPAIGNS, bukan tabel Supabase ini
-- - Data terakhir masuk 24 Agustus 2026 — arsitektur sudah pindah ke
--   ads_daily (grain per-ad) sejak itu
--
-- Urutan: campaign_daily dulu (FK ke campaigns), baru campaigns
-- ============================================================
drop table if exists campaign_daily;
drop table if exists campaigns;
