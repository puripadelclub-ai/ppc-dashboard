"""
court_pass_client.py
Baca + parse Court Pass tracker sheet → rows siap upsert ke Supabase.

Sheet: https://docs.google.com/spreadsheets/d/1LBjuUITvPO_s-WdQg8Lt1EOwzA-158VIxzsd4xExcto
Set env var COURT_PASS_SHEET_ID = 1LBjuUITvPO_s-WdQg8Lt1EOwzA-158VIxzsd4xExcto

Struktur sheet: tabel manual dengan merged cells, layout 2-kolom (left cols 0-4, right cols 5-9).
Parser membaca kedua sisi secara independen, hanya mengambil kolom bersih:
  purchase_date, member_name, package_type, price, hours_total,
  hours_remaining, status, expiry_date
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

def _extract_hours(pkg_raw: str) -> int | None:
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
    # bare: 05/08/26
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

def _is_date(s: str) -> bool:
    return _parse_date(s) is not None

def _make_id(purchase_date: str, member_name: str, package_type: str) -> str:
    key = f"{purchase_date}|{member_name.upper()}|{package_type}"
    return hashlib.sha256(key.encode()).hexdigest()[:20]

def _finalize(cur: dict) -> dict:
    """Fill defaults and set status based on remaining hours."""
    pkg_raw = cur.get('package_raw', '') or ''
    cur.setdefault('package_type', _normalise_pkg(pkg_raw))
    cur.setdefault('hours_total', _extract_hours(pkg_raw))

    rem = cur.get('hours_remaining')
    if cur.get('status') == 'active' and rem is not None and rem == 0:
        cur['status'] = 'habis'

    cur['id'] = _make_id(
        cur.get('purchase_date', ''),
        cur.get('member_name', ''),
        cur.get('package_type', ''),
    )
    # Serialize session usage dates as JSON string for Supabase text column
    if 'session_dates' in cur:
        import json as _json
        cur['session_dates'] = _json.dumps(cur['session_dates'])
    else:
        cur['session_dates'] = None
    return cur

# ── Core parser ───────────────────────────────────────────────────────────────

def _is_usage_entry(s: str) -> bool:
    """Check if string looks like a court usage entry (XC XH pattern or time HH:MM/HH.MM)."""
    return bool(
        re.search(r'\d+\s*[Cc]\s*\d*\s*[Hh]', s) or   # e.g. 2C2H, 1C1H
        re.search(r'\d{1,2}[.:]\d{2}', s)               # e.g. 16.00, 07:00
    )

def _extract_sisa(s: str):
    """Extract remaining count from c4: numeric string, 'Sisa Xjam', 'HABIS'→0, else None."""
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


def _process_side(all_values: list, col_start: int) -> list[dict]:
    """
    Parse one column range (left=0 or right=5) of the sheet.
    Returns list of court pass purchase dicts.

    Handles merged-cell member names: when col B is merged (gspread returns name only
    in the first row), subsequent purchase rows have c1="" — we detect them by c0=date
    AND c2=package name, then carry the last known member name forward.
    """
    records = []
    cur = None
    next_is_expiry = False
    last_name = ''  # carry forward for merged-cell rows

    for raw_row in all_values:
        # Pad row so we can always index safely
        row = list(raw_row) + [''] * 15
        c = [str(row[col_start + i]).strip() for i in range(5)]
        c0, c1, c2, c3, c4 = c

        # Completely empty section → skip
        if not any(c):
            continue

        # Header row
        if c0.upper() == 'TANGGAL':
            continue

        # "VALID" marker → next row's c0 is expiry date
        if c0.upper() == 'VALID':
            next_is_expiry = True
            continue

        # Expiry date row (row immediately after VALID)
        if next_is_expiry:
            next_is_expiry = False
            expiry = _parse_date(c0)
            if cur:
                if expiry:
                    cur['expiry_date'] = expiry
                records.append(_finalize(cur))
                cur = None
            continue

        # Track member name whenever c1 has a non-date, non-header value
        if c1 and c1.upper() not in ('VALID', 'NAMA') and not _is_date(c1):
            last_name = c1.upper().strip()

        # New purchase detection:
        #   Case A: c0=date, c1=member name (standard)
        #   Case B: c0=date, c1="" (merged cell), c2=package name (non-numeric)
        #   → carry member name from last_name
        is_pkg_name = bool(c2) and not re.match(r'^[\d,]+$', c2.replace(',', ''))
        is_new_purchase = (
            _is_date(c0) and c1.upper() not in ('VALID',) and
            (c1 or is_pkg_name)
        )

        if is_new_purchase:
            if cur:
                records.append(_finalize(cur))

            member_name = last_name if last_name else 'UNKNOWN'

            # Price sometimes appears in c2 instead of package name
            price = None
            pkg_raw = c2
            if re.match(r'^[\d,]+$', c2.replace(',', '')):
                price = _safe_int(c2)
                pkg_raw = ''

            sisa_init = _extract_sisa(c4)
            cur = {
                'purchase_date':   _parse_date(c0),
                'member_name':     member_name,
                'package_raw':     pkg_raw,
                'package_type':    _normalise_pkg(pkg_raw) if pkg_raw else None,
                'price':           price,
                'hours_total':     _extract_hours(pkg_raw) if pkg_raw else None,
                'hours_remaining': sisa_init,
                'status':          'active',
                'expiry_date':     None,
            }
            if sisa_init == 0:
                cur['status'] = 'habis'
            # First usage on same row as purchase
            if c3 and _is_usage_entry(c3):
                cur.setdefault('session_dates', []).append({'desc': c3, 'sisa': sisa_init})
            continue

        # Continuation row for the current purchase
        if cur:
            # Capture usage from c3 (DIGUNAKAN column); filter out label rows
            if c3 and _is_usage_entry(c3):
                cur.setdefault('session_dates', []).append({'desc': c3, 'sisa': _extract_sisa(c4)})

            # Package name in c2 of a continuation row (after price-only row)
            if c2 and not cur.get('package_raw') and not re.match(r'^[\d,]+$', c2.replace(',', '')) and not _is_date(c0):
                cur['package_raw'] = c2
                cur['package_type'] = _normalise_pkg(c2)
                cur['hours_total'] = _extract_hours(c2)

            # Price in c2 (numeric)
            if c2 and re.match(r'^[\d,]+$', c2.replace(',', '')) and not cur.get('price'):
                cur['price'] = _safe_int(c2)

            # Status flags from c2 / c4
            c2_up = c2.upper()
            c4_up = c4.upper()
            if 'HANGUS' in c2_up or 'HANGUS' in c4_up:
                cur['status'] = 'hangus'
            elif 'HABIS' in c2_up or 'HABIS' in c4_up:
                cur['status'] = 'habis'

            # Update remaining from c4
            cnt = _extract_sisa(c4)
            if cnt is not None:
                cur['hours_remaining'] = cnt
                if cnt == 0:
                    cur['status'] = 'habis'

    # Flush last record
    if cur:
        records.append(_finalize(cur))

    return records


def read_and_parse_court_passes() -> list[dict]:
    """
    Buka Court Pass sheet, parse kedua sisi, return combined list of dicts.
    Duplikat (same id dari left+right) didedup — left side takes precedence.
    """
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    gc = gspread.authorize(creds)

    sh = gc.open_by_key(COURT_PASS_SHEET_ID)
    # Try to get first visible worksheet
    ws = sh.get_worksheet(0)
    all_values = ws.get_all_values()

    left  = _process_side(all_values, col_start=0)
    right = _process_side(all_values, col_start=5)

    # Dedup by id
    seen = {}
    for r in left + right:
        rid = r.get('id', '')
        if rid and rid not in seen:
            seen[rid] = r

    rows = list(seen.values())

    # Filter out clearly empty/junk rows
    rows = [
        r for r in rows
        if r.get('purchase_date') and r.get('member_name')
        and len(r['member_name']) > 1
    ]

    return rows
