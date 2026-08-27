"""
coaching_client.py
Baca + parse Coaching Log sheet → rows siap upsert ke Supabase tabel `coaching_sessions`.

Sheet: https://docs.google.com/spreadsheets/d/1dtmKhpbAeVu-YX9Lx1ayU4OPmdyYcRMg4IZGPBgd0iA
Set env var COACHING_SHEET_ID (atau gunakan default di bawah)

Struktur sheet:
  - Tab Summary/Index: dilewati
  - Tab per bulan ("COACHING SESSION BULAN YYYY"):
      Tanggal | Nama Member | Persons | KET (paket) | Durasi/Jam | Sisa

Merged cells: day number dan group session di-merge lintas baris.
gspread mengembalikan nilai hanya di baris pertama, baris lanjutan = "".
Parser melakukan carry-forward untuk: day, persons, time_slot, package_type.
"""

import os
import re
import json
import hashlib
from datetime import datetime, date

import gspread
from google.oauth2.service_account import Credentials

COACHING_SHEET_ID = os.environ.get(
    "COACHING_SHEET_ID",
    "1dtmKhpbAeVu-YX9Lx1ayU4OPmdyYcRMg4IZGPBgd0iA"
)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

# ── Indonesian month map ──────────────────────────────────────────────────────
_BULAN = {
    'JANUARI':1,'FEBRUARI':2,'MARET':3,'APRIL':4,
    'MEI':5,'JUNI':6,'JULI':7,'AGUSTUS':8,
    'SEPTEMBER':9,'OKTOBER':10,'NOVEMBER':11,'DESEMBER':12,
}

# ── Package type normalisation ────────────────────────────────────────────────
_PKG_MAP = [
    (r'BUNDLING.*6',        'BUNDLING_6X'),
    (r'BUNDLING.*4',        'BUNDLING_4X'),
    (r'BUNDLING',           'BUNDLING'),
    (r'COACHING.*KIDS',     'COACHING_KIDS'),
    (r'KIDS.*COACHING',     'COACHING_KIDS'),
    (r'PRIVATE',            'PRIVATE'),
    (r'FREE.*1X|FREE.*1',   'FREE_1X'),
    (r'ADD.*ON.*PLAYER',    'ADDON_PLAYER'),
    (r'ADD.*ON',            'ADDON_FREE'),
    (r'FREE',               'FREE_COACHING'),
    (r'TRIAL',              'TRIAL'),
]

def _normalise_pkg(raw: str) -> str:
    if not raw:
        return 'FREE_COACHING'
    upper = raw.upper().strip()
    for pattern, label in _PKG_MAP:
        if re.search(pattern, upper):
            return label
    return 'OTHER'

def _extract_persons(s: str) -> int | None:
    m = re.search(r'(\d+)', s)
    return int(m.group(1)) if m else None

def _safe_int(s: str) -> int | None:
    try:
        return int(str(s).replace(',','').strip())
    except Exception:
        return None

def _make_id(session_date: str, member_name: str, time_slot: str) -> str:
    key = f"{session_date}|{member_name.upper()}|{time_slot}"
    return hashlib.sha256(key.encode()).hexdigest()[:20]

def _parse_month_year(title: str):
    """Extract (month_int, year_int) from tab title like 'COACHING SESSION AGUSTUS 2026'."""
    title_up = title.upper()
    year_m = re.search(r'(\d{4})', title_up)
    year = int(year_m.group(1)) if year_m else None
    month = None
    for name, num in _BULAN.items():
        if name in title_up:
            month = num
            break
    return month, year

def _parse_sisa(s: str):
    """Parse 'Sisa 3x' → 3, 'HABIS' → 0, '' → None."""
    if not s:
        return None, 'active'
    s_up = s.upper().strip()
    if 'HABIS' in s_up:
        return 0, 'habis'
    m = re.search(r'SISA\s*(\d+)', s_up)
    if m:
        return int(m.group(1)), 'active'
    return None, 'active'

# ── Monthly tab parser ────────────────────────────────────────────────────────

def _parse_monthly_tab(all_values: list, month: int, year: int) -> list[dict]:
    """
    Parse satu tab bulan. Tiap baris = satu sesi member.
    Carry-forward: day, persons, time_slot, package_type dalam satu grup.
    """
    records = []
    current_day    = None
    current_persons = None
    current_time   = None
    current_pkg    = None
    current_pkg_raw = None

    for raw_row in all_values:
        row = list(raw_row) + [''] * 8
        c0 = str(row[0]).strip()
        c1 = str(row[1]).strip()
        c2 = str(row[2]).strip()
        c3 = str(row[3]).strip()
        c4 = str(row[4]).strip()
        c5 = str(row[5]).strip() if len(row) > 5 else ''

        # Skip header rows
        c0_up = c0.upper()
        if 'COACHING SESSION' in c0_up or c0_up == 'TANGGAL':
            continue
        if not c0 and not c1:
            continue
        # Skip summary/note rows (no member name, has text like "NOTE" or numbers only)
        if c0 and not c1 and not _safe_int(c0):
            continue

        # Update current day if c0 is a number
        day_num = _safe_int(c0)
        if day_num and 1 <= day_num <= 31:
            current_day = day_num
            # New day resets group context
            current_persons = None
            current_time    = None
            current_pkg     = None
            current_pkg_raw = None

        if not current_day:
            continue

        # Skip rows with no member name
        if not c1:
            continue

        # Carry forward or update group attributes
        if c2:
            current_persons  = _extract_persons(c2)
        if c4:
            current_time     = c4
        if c3:
            current_pkg      = _normalise_pkg(c3)
            current_pkg_raw  = c3

        # Parse sisa / status
        sisa, status = _parse_sisa(c5)

        # Build session_date
        try:
            session_date = date(year, month, current_day).isoformat()
        except ValueError:
            continue  # invalid date (e.g. day 31 in April)

        record = {
            'id':                 _make_id(session_date, c1, current_time or ''),
            'session_date':       session_date,
            'member_name':        c1.upper().strip(),
            'persons':            current_persons,
            'package_type':       current_pkg or 'FREE_COACHING',
            'package_raw':        current_pkg_raw or '',
            'time_slot':          current_time or '',
            'sessions_remaining': sisa,
            'status':             status,
        }
        records.append(record)

    return records


# ── Main entry point ──────────────────────────────────────────────────────────

def read_and_parse_coaching_sessions() -> list[dict]:
    """
    Buka Coaching Log sheet, parse semua tab bulan.
    Return combined list of dicts, dedup by id.
    """
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    gc = gspread.authorize(creds)

    sh = gc.open_by_key(COACHING_SHEET_ID)
    worksheets = sh.worksheets()

    all_rows = []

    for ws in worksheets:
        title = ws.title.strip().upper()

        # Skip summary/index tabs
        if 'COACHING SESSION' not in title:
            continue

        month, year = _parse_month_year(title)
        if not month or not year:
            continue

        try:
            values = ws.get_all_values()
            rows = _parse_monthly_tab(values, month, year)
            all_rows.extend(rows)
        except Exception as e:
            import warnings
            warnings.warn(f"coaching_client: skip tab '{ws.title}': {e}")
            continue

    # Dedup by id (first occurrence wins)
    seen = {}
    for r in all_rows:
        rid = r.get('id', '')
        if rid and rid not in seen:
            seen[rid] = r

    rows = list(seen.values())

    # Filter junk
    rows = [
        r for r in rows
        if r.get('session_date') and r.get('member_name')
        and len(r['member_name']) > 1
    ]

    return rows
