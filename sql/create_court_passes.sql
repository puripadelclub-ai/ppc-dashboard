-- ============================================================
-- Tabel: court_passes
-- Sumber: Court Pass Tracker Google Sheet (manual admin input)
-- Sync: process.py → court_pass_client.py → upsert_court_passes()
-- ============================================================

CREATE TABLE IF NOT EXISTS court_passes (
  id               TEXT PRIMARY KEY,          -- sha256(purchase_date|member_name|package_type)[:20]
  purchase_date    DATE       NOT NULL,
  member_name      TEXT       NOT NULL,
  package_type     TEXT,                       -- normalised: COURT_PASS_20H_OFF_PEAK, COMEBACK_PACKAGE, dll
  package_raw      TEXT,                       -- original text dari sheet
  price            INTEGER,                    -- IDR, NULL jika tidak tercatat di sheet
  hours_total      INTEGER,                    -- dari nama paket: 8H→8, 20H→20, 50H→50
  hours_remaining  INTEGER,                    -- sisa jam terakhir tercatat
  status           TEXT DEFAULT 'active',      -- active | habis | hangus
  expiry_date      DATE,                       -- dari baris VALID di sheet
  created_at       TIMESTAMPTZ DEFAULT now(),
  updated_at       TIMESTAMPTZ DEFAULT now()
);

-- Index untuk query dashboard
CREATE INDEX IF NOT EXISTS idx_cp_member      ON court_passes (member_name);
CREATE INDEX IF NOT EXISTS idx_cp_status      ON court_passes (status);
CREATE INDEX IF NOT EXISTS idx_cp_expiry      ON court_passes (expiry_date);
CREATE INDEX IF NOT EXISTS idx_cp_pkg         ON court_passes (package_type);
CREATE INDEX IF NOT EXISTS idx_cp_purchase    ON court_passes (purchase_date);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_cp_updated_at ON court_passes;
CREATE TRIGGER trg_cp_updated_at
  BEFORE UPDATE ON court_passes
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- RLS: anon key boleh SELECT (untuk dashboard client-side)
ALTER TABLE court_passes ENABLE ROW LEVEL SECURITY;
CREATE POLICY "anon read court_passes"
  ON court_passes FOR SELECT
  TO anon USING (true);
