-- ============================================================
-- Migration: court_passes → programs
-- Rename table dan kolom agar lebih generik (semua jenis program)
-- Jalankan di Supabase SQL Editor
-- ============================================================

-- 1. Rename table
ALTER TABLE court_passes RENAME TO programs;

-- 2. Rename kolom
ALTER TABLE programs RENAME COLUMN hours_total     TO sessions_total;
ALTER TABLE programs RENAME COLUMN hours_remaining TO sessions_remaining;

-- 3. Update RLS policy (nama lama perlu di-drop, buat baru)
DROP POLICY IF EXISTS "anon read court_passes" ON programs;
CREATE POLICY "anon read programs"
  ON programs FOR SELECT
  TO anon USING (true);

-- 4. Index otomatis ikut rename tabel — tidak perlu diubah
--    (idx_cp_member, idx_cp_status, dst masih valid)

-- Verifikasi
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'programs'
ORDER BY ordinal_position;
