-- ============================================================
-- Tabel: coaching_sessions
-- Sumber: Coaching Log Google Sheet (per tab bulan)
-- Sync: process.py → coaching_client.py → upsert_coaching_sessions()
-- ============================================================

CREATE TABLE IF NOT EXISTS coaching_sessions (
  id                  TEXT PRIMARY KEY,          -- sha256(session_date|member_name|time_slot)[:20]
  session_date        DATE       NOT NULL,
  member_name         TEXT       NOT NULL,
  persons             INTEGER,                   -- jumlah orang dalam satu slot
  package_type        TEXT,                      -- FREE_COACHING, BUNDLING_4X, COACHING_KIDS, PRIVATE, dll
  package_raw         TEXT,                      -- teks asli dari sheet
  time_slot           TEXT,                      -- "07.00-08.00"
  sessions_remaining  INTEGER,                   -- sisa sesi bundling (NULL untuk Free)
  status              TEXT DEFAULT 'active',     -- active | habis
  created_at          TIMESTAMPTZ DEFAULT now(),
  updated_at          TIMESTAMPTZ DEFAULT now()
);

-- Index untuk query dashboard
CREATE INDEX IF NOT EXISTS idx_cs_date     ON coaching_sessions (session_date);
CREATE INDEX IF NOT EXISTS idx_cs_member   ON coaching_sessions (member_name);
CREATE INDEX IF NOT EXISTS idx_cs_pkg      ON coaching_sessions (package_type);
CREATE INDEX IF NOT EXISTS idx_cs_status   ON coaching_sessions (status);

-- Auto-update updated_at (reuse fungsi dari programs jika sudah ada)
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_cs_updated_at ON coaching_sessions;
CREATE TRIGGER trg_cs_updated_at
  BEFORE UPDATE ON coaching_sessions
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- RLS: anon key boleh SELECT (untuk dashboard client-side)
ALTER TABLE coaching_sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "anon read coaching_sessions"
  ON coaching_sessions FOR SELECT
  TO anon USING (true);
