"""
coaching_client.py
Baca + parse Coaching Log sheet → rows siap upsert ke Supabase tabel `coaching_sessions`.

Sheet: https://docs.google.com/spreadsheets/d/1dtmKhpbAeVu-YX9Lx1ayU4OPmdyYcRMg4IZGPBgd0iA
Set env var COACHING_SHEET_ID (atau gunakan default di bawah)

Struktur sheet (raw_coaching tab):
  Date (YYYY-MM-DD) | Member_Name | Package_Type | Participants | Start_Time | End_Time
"""

import os
import re
import json
import hashlib
from datetime import datetime

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

RAW_TAB_NAME = "raw_coaching"

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
    upper = raw.upper().strip().replace('_', ' ').replace('-', ' ')
    for pattern, label in _PKG_MAP:
        if re.search(pattern, upper):
            return label
    return raw.upper().strip()  # keep original if no match

def _safe_int(s: str):
    try:
        return int(str(s).replace(',', '').strip())
    except Exception:
        return None

def _make_id(session_date: str, member_name: str, start_time: str) -> str:
    key = f"{session_date}|{member_name.upper().strip()}|{start_time}"
    return hashlib.sha256(key.encode()).hexdigest()[:20]


# ── Main entry point ──────────────────────────────────────────────────────────

def read_and_parse_coaching_sessions() -> list[dict]:
    """
    Buka Coaching Log sheet, baca tab 'raw_coaching'.
    Return list of dicts, dedup by id.
    """
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    gc = gspread.authorize(creds)

    sh = gc.open_by_key(COACHING_SHEET_ID)

    # Find raw_coaching tab (case-insensitive)
    raw_ws = None
    for ws in sh.worksheets():
        if ws.title.strip().lower().replace(' ', '_') == RAW_TAB_NAME:
            raw_ws = ws
            break

    if not raw_ws:
        raise ValueError(
            f"Tab '{RAW_TAB_NAME}' tidak ditemukan. "
            f"Tabs tersedia: {[w.title for w in sh.worksheets()]}"
        )

    all_values = raw_ws.get_all_values()

    if not all_values:
        return []

    # Detect header row — look for "Date" or "Member" in first few rows
    header_idx = 0
    for i, row in enumerate(all_values[:5]):
        row_up = [c.upper().strip() for c in row]
        if any('DATE' in c or 'MEMBER' in c for c in row_up):
            header_idx = i
            break

    header = [c.upper().strip() for c in all_values[header_idx]]

    # Map column names to indices
    def _col(names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    idx_date  = _col(['DATE'])
    idx_name  = _col(['MEMBER_NAME', 'MEMBER NAME', 'NAMA MEMBER', 'NAMA'])
    idx_pkg   = _col(['PACKAGE_TYPE', 'PACKAGE TYPE', 'PAKET', 'KET'])
    idx_pers  = _col(['PARTICIPANTS', 'PERSONS', 'PARTICIPANT'])
    idx_start = _col(['START_TIME', 'START TIME', 'JAM MULAI', 'JAM'])
    idx_end   = _col(['END_TIME', 'END TIME', 'JAM SELESAI'])

    records = []
    seen = {}

    for raw_row in all_values[header_idx + 1:]:
        row = list(raw_row) + [''] * 10

        session_date = str(row[idx_date]).strip() if idx_date is not None else ''
        member_name  = str(row[idx_name]).strip()  if idx_name  is not None else ''
        pkg_raw      = str(row[idx_pkg]).strip()   if idx_pkg   is not None else ''
        persons_raw  = str(row[idx_pers]).strip()  if idx_pers  is not None else ''
        start_time   = str(row[idx_start]).strip() if idx_start is not None else ''
        end_time     = str(row[idx_end]).strip()   if idx_end   is not None else ''

        # Skip blanks / header repeats
        if not session_date or not member_name:
            continue
        if session_date.upper() in ('DATE', 'TANGGAL'):
            continue

        # Validate date format YYYY-MM-DD
        try:
            datetime.strptime(session_date, '%Y-%m-%d')
        except ValueError:
            continue

        pkg_type = _normalise_pkg(pkg_raw)
        persons  = _safe_int(persons_raw)
        time_slot = f"{start_time}-{end_time}" if start_time and end_time else start_time

        rec_id = _make_id(session_date, member_name, start_time)

        if rec_id in seen:
            continue
        seen[rec_id] = True

        records.append({
            'id':                 rec_id,
            'session_date':       session_date,
            'member_name':        member_name.upper().strip(),
            'persons':            persons,
            'package_type':       pkg_type,
            'package_raw':        pkg_raw,
            'time_slot':          time_slot,
            'sessions_remaining': None,
            'status':             'active',
        })

    return records
