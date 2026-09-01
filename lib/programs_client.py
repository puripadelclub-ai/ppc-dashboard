"""
programs_client.py
Baca + parse Programs tracker sheet → rows siap upsert ke Supabase tabel `programs`.

Sheet: https://docs.google.com/spreadsheets/d/1LBjuUITvPO_s-WdQg8Lt1EOwzA-158VIxzsd4xExcto
Set env var COURT_PASS_SHEET_ID = 1LBjuUITvPO_s-WdQg8Lt1EOwzA-158VIxzsd4xExcto

Tab yang dibaca:
  - Tab 0 (Court Pass): layout 2-kolom (left cols 0-4, right cols 5-9), track sisa jam
  - Tab lain (Comeback, Independence, Upgrade, Trial): layout 1-kolom, track sesi

Kolom output (→ tabel `programs` di Supabase):
  purchase_date, member_name, package_type, package_raw, price,
  sessions_total, sessions_remaining, status, expiry_date
"""

import os
import re
import json
import hashlib
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

COURT_PASS_SHEET_ID = os.environ.get(
    "COURT_PASS_SHEET_ID",
    "1LBjuUITvPO_s-WdQg8Lt1EOwzA-158VIxzsd4xExcto"
)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

# ── Package type normalisation ────────────────────────────────────────────────
_PKG_MAP = [
    (r'50H.*WEEKEND',          'COURT_PASS_50H_WEEKEND'),
    (r'20H.*OFF.PEAK',         'COURT_PASS_20H_OFF_PEAK'),
    (r'20H.*COURT',            'COURT_PASS_20H'),
    (r'8H.*EVENING',           'COURT_PASS_8H_EVENING'),
    (r'8H.*OFF.PH',            'COURT_PASS_8H_OFF_PH'),
    (r'8H.*COURT',             'COURT_PASS_8H'),
    (r'COURT.*PASS',           'COURT_PASS'),
    (r'COMEBACK',              'COMEBACK_PACKAGE'),
    (r'UPGRADE.*MEMBER',       'UPGRADE_MEMBERSHIP'),
    (r'INDEPENDENCE',          'INDEPENDENCE_DEAL'),
    (r'TRIAL',                 'TRIAL'),
    (r'COURT.*SESSION',        'COURT_SESSION'),
]

def _normalise_pkg(raw: str) -> str:
    upper = raw.upper().strip()
    for pattern, label in _PKG_MAP:
        if re.search(pattern, upper):
            return label
    return 'OTHER'

def _extract_sessions(pkg_raw: str) -> int | None:
    """Extract session/hour count from package name (e.g. 8H→8, 20H→20)."""
    m = re.search(r'(\d+)H\b', pkg_raw, re.IGNORECASE)
    return int(m.group(1)) if m else None

def _parse_date(s: str) -> str | None:
    """Parse common date formats → YYYY-MM-DD, or None."""
    s = s.strip().rstrip('.')
    for fmt in ('%d/%m/%Y', '%d/%m/%y', '%d-%m-%Y', '%d-%m-%y'):
        try:
            return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
        except ValueError:
            pass
    m = re.match(r'^(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})$', s)
    if m:
        d, mo, y = m.groups()
        y = ('20' + y) if len(y) == 2 else y
        try:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except Exception:
            pass
    return None

def _safe_int(s: str) -> int | None:
    try:
        return int(str(s).replace(',', '').strip())
    except Exception:
        return None

def _extract_sisa(s: str):
    """Extract remaining count: numeric, 'Sisa Xjam'→X, 'HABIS'→0, else None."""
    if not s:
        return None
    si = _safe_int(s)
    if si is not None:
        return si
    su = s.upper()
    if 'HABIS' in su:
        return 0
    m = re.search(r'(\d+)', s)
    return int(m.group(1)) if m else None

def _is_usage_entry(s: str) -> bool:
    """Check if string looks like a court usage entry (XC XH or time HH:MM/HH.MM)."""
    return bool(
        re.search(r'\d+\s*[Cc]\s*\d*\s*[Hh]', s) or
        re.search(r'\d{1,2}[.:]\d{2}', s)
    )

def _is_date(s: str) -> bool:
    return _parse_date(s) is not None

def _make_id(purchase_date: str, member_name: str, package_type: str) -> str:
    key = f"{purchase_date}|{member_name.upper()}|{package_type}"
    return hashlib.sha256(key.encode()).hexdigest()[:20]

def _finalize(cur: dict) -> dict:
    """Fill defaults and set status based on remaining sessions."""
    pkg_raw = cur.get('package_raw', '') or ''
    cur.setdefault('package_type', _normalise_pkg(pkg_raw))
    cur.setdefault('sessions_total', _extract_sessions(pkg_raw))

    rem = cur.get('sessions_remaining')
    if cur.get('status') == 'active' and rem is not None and rem == 0:
        cur['status'] = 'habis'

    cur['id'] = _make_id(
        cur.get('purchase_date', ''),
        cur.get('member_name', ''),
        cur.get('package_type', ''),
    )
    if 'session_dates' in cur:
        import json as _json
        cur['session_dates'] = _json.dumps(cur['session_dates'])
    else:
        cur['session_dates'] = None
    return cur

# ── Court Pass tab parser (2-column layout) ───────────────────────────────────

def _process_side(all_values: list, col_start: int) -> list[dict]:
    """
    Parse one column range (left=0 or right=5) of the Court Pass tab.
    Returns list of program purchase dicts.
    """
    records = []
    cur = None
    next_is_expiry = False

    for raw_row in all_values:
        row = list(raw_row) + [''] * 15
        c = [str(row[col_start + i]).strip() for i in range(5)]
        c0, c1, c2, c3, c4 = c

        if not any(c):
            continue

        if c0.upper() == 'TANGGAL':
            continue

        if c0.upper() == 'VALID':
            next_is_expiry = True
            continue

        if next_is_expiry:
            next_is_expiry = False
            expiry = _parse_date(c0)
            if cur:
                if expiry:
                    cur['expiry_date'] = expiry
                records.append(_finalize(cur))
                cur = None
            continue

        if _is_date(c0) and c1 and c1.upper() not in ('VALID',):
            if cur:
                records.append(_finalize(cur))

            price = None
            pkg_raw = c2
            if re.match(r'^[\d,]+$', c2.replace(',', '')):
                price = _safe_int(c2)
                pkg_raw = ''

            cur = {
                'purchase_date':    _parse_date(c0),
                'member_name':      c1.upper().strip(),
                'package_raw':      pkg_raw,
                'package_type':     _normalise_pkg(pkg_raw) if pkg_raw else None,
                'price':            price,
                'sessions_total':   _extract_sessions(pkg_raw) if pkg_raw else None,
                'sessions_remaining': _safe_int(c4),
                'status':           'active',
                'expiry_date':      None,
            }
            if _safe_int(c4) == 0:
                cur['status'] = 'habis'
            continue

        if cur:
            if c2 and not cur.get('package_raw') and not re.match(r'^[\d,]+$', c2.replace(',', '')):
                cur['package_raw']    = c2
                cur['package_type']   = _normalise_pkg(c2)
                cur['sessions_total'] = _extract_sessions(c2)

            if c2 and re.match(r'^[\d,]+$', c2.replace(',', '')) and not cur.get('price'):
                cur['price'] = _safe_int(c2)

            c2_up = c2.upper()
            if 'HANGUS' in c2_up:
                cur['status'] = 'hangus'
            elif 'HABIS' in c2_up or 'HABIS' in c4.upper():
                cur['status'] = 'habis'

            cnt = _safe_int(c4)
            if cnt is not None:
                cur['sessions_remaining'] = cnt
                if cnt == 0:
                    cur['status'] = 'habis'

    if cur:
        records.append(_finalize(cur))

    return records


# ── Generic tab parser (single-column layout: Comeback, Independence, dll) ────

def _parse_generic_tab(all_values: list, package_type_hint: str = None) -> list[dict]:
    """
    Parse tab dengan layout 1-kolom standar:
    TANGGAL | NAMA | JENIS PEMBELIAN | DIGUNAKAN | (status/sisa)

    Tiap baris tanggal = pembelian baru. Baris lanjutan = sesi yang digunakan.
    Status diambil dari kolom 4 (HABIS, Sisa Xjam) atau kolom 2 (HANGUS).
    """
    records = []
    cur = None

    for raw_row in all_values:
        row = list(raw_row) + [''] * 10
        c0 = str(row[0]).strip()
        c1 = str(row[1]).strip()
        c2 = str(row[2]).strip()
        c3 = str(row[3]).strip() if len(row) > 3 else ''
        c4 = str(row[4]).strip() if len(row) > 4 else ''

        if not c0 and not c1:
            continue
        if c0.upper() == 'TANGGAL':
            continue
        # Skip footer rows (e.g. "Total 10 org...")
        if 'TOTAL' in c0.upper() or 'TOTAL' in c1.upper():
            continue

        if _is_date(c0) and c1:
            if cur:
                records.append(_finalize(cur))

            pkg_raw = c2 if c2 and not re.match(r'^[\d,]+$', c2.replace(',', '')) else ''
            price   = _safe_int(c2) if c2 and re.match(r'^[\d,]+$', c2.replace(',', '')) else None
            pkg_type = package_type_hint or (_normalise_pkg(pkg_raw) if pkg_raw else 'OTHER')

            # Detect status from c4
            c4_up = c4.upper()
            status = 'active'
            sisa = None
            if 'HABIS' in c4_up:
                status = 'habis'
            elif 'SISA' in c4_up:
                m = re.search(r'(\d+)', c4)
                sisa = int(m.group(1)) if m else None

            cur = {
                'purchase_date':      _parse_date(c0),
                'member_name':        c1.upper().strip(),
                'package_raw':        pkg_raw,
                'package_type':       pkg_type,
                'price':              price,
                'sessions_total':     None,
                'sessions_remaining': sisa,
                'status':             status,
                'expiry_date':        None,
            }
            # First usage row is on the same row as purchase (c3 = DIGUNAKAN)
            if c3 and _is_usage_entry(c3):
                cur.setdefault('session_dates', []).append({'desc': c3, 'sisa': _extract_sisa(c4)})
            continue

        if cur:
            # Capture usage from c3 (DIGUNAKAN column); filter out label rows
            if c3 and _is_usage_entry(c3):
                cur.setdefault('session_dates', []).append({'desc': c3, 'sisa': _extract_sisa(c4)})

            c2_up = c2.upper()
            c4_up = c4.upper()
            if 'HANGUS' in c2_up:
                cur['status'] = 'hangus'
            elif 'HABIS' in c4_up or 'HABIS' in c2_up:
                cur['status'] = 'habis'
            elif 'SISA' in c4_up:
                m = re.search(r'(\d+)', c4)
                if m:
                    cur['sessions_remaining'] = int(m.group(1))

    if cur:
        records.append(_finalize(cur))

    return records


# ── Main entry point ──────────────────────────────────────────────────────────

def read_and_parse_programs() -> list[dict]:
    """
    Buka Programs sheet, parse semua tab, return combined list of dicts.
    Duplikat (same id) didedup — tab pertama takes precedence.
    """
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    gc = gspread.authorize(creds)

    sh = gc.open_by_key(COURT_PASS_SHEET_ID)
    worksheets = sh.worksheets()

    all_rows = []

    for i, ws in enumerate(worksheets):
        try:
            values = ws.get_all_values()
            title  = ws.title.upper()

            if i == 0 or 'COURT PASS' in title or 'COURTPASS' in title:
                # Tab 0: 2-column Court Pass layout
                left  = _process_side(values, col_start=0)
                right = _process_side(values, col_start=5)
                all_rows.extend(left + right)

            elif 'COMEBACK' in title:
                all_rows.extend(_parse_generic_tab(values, 'COMEBACK_PACKAGE'))

            elif 'INDEPENDENCE' in title:
                all_rows.extend(_parse_generic_tab(values, 'INDEPENDENCE_DEAL'))

            elif 'UPGRADE' in title:
                all_rows.extend(_parse_generic_tab(values, 'UPGRADE_MEMBERSHIP'))

            elif 'TRIAL' in title or 'SESSION' in title:
                all_rows.extend(_parse_generic_tab(values))

            else:
                # Tab tidak dikenal — coba parse generic
                all_rows.extend(_parse_generic_tab(values))

        except Exception:
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
        if r.get('purchase_date') and r.get('member_name')
        and len(r['member_name']) > 1
    ]

    return rows


# Backward-compat alias
read_and_parse_court_passes = read_and_parse_programs
