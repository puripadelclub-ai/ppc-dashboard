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

def _process_side(all_values: list, col_start: int) -> list[dict]:
    """
    Parse one column range (left=0 or right=5) of the sheet.
    Returns list of court pass purchase dicts.
    """
    records = []
    cur = None
    next_is_expiry = False

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
            if cur:
                # Don't finalize yet — waiting for expiry date
                pass
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

        # New purchase: c0 has a date AND c1 has a name
        if _is_date(c0) and c1 and c1.upper() not in ('VALID',):
            # Save previous if it wasn't saved via VALID path
            if cur:
                records.append(_finalize(cur))

            # Price sometimes appears in c2 (e.g. "175500") instead of package name
            price = None
            pkg_raw = c2
            if re.match(r'^[\d,]+$', c2.replace(',', '')):
                price = _safe_int(c2)
                pkg_raw = ''  # price only, package name must come from next row

            cur = {
                'purchase_date': _parse_date(c0),
                'member_name': c1.upper().strip(),
                'package_raw': pkg_raw,
                'package_type': _normalise_pkg(pkg_raw) if pkg_raw else None,
                'price': price,
                'hours_total': _extract_hours(pkg_raw) if pkg_raw else None,
                'hours_remaining': _safe_int(c4),
                'status': 'active',
                'expiry_date': None,
            }
            # c4 == 0 means fully used
            if _safe_int(c4) == 0:
                cur['status'] = 'habis'
            continue

        # Continuation row for the current purchase
        if cur:
            # Capture usage date: c0 has a date, c1 is empty → session usage row
            if _is_date(c0) and not c1:
                usage_date = _parse_date(c0)
                # c2 may contain time slot (e.g. "10:00-12:00") or court info
                time_info = c2.strip() if c2 and not re.match(r'^[\d,]+$', c2.replace(',', '')) else ''
                entry = usage_date + (f" {time_info}" if time_info else '')
                cur.setdefault('session_dates', []).append(entry)

            # Package name may appear in c2 of a continuation row (after price row)
            if c2 and not cur.get('package_raw') and not re.match(r'^[\d,]+$', c2.replace(',', '')) and not _is_date(c0):
                cur['package_raw'] = c2
                cur['package_type'] = _normalise_pkg(c2)
                cur['hours_total'] = _extract_hours(c2)

            # Price appears in c2 (numeric)
            if c2 and re.match(r'^[\d,]+$', c2.replace(',', '')) and not cur.get('price'):
                cur['price'] = _safe_int(c2)

            # Status flags
            c2_up = c2.upper()
            if 'HANGUS' in c2_up:
                cur['status'] = 'hangus'
            elif 'HABIS' in c2_up or 'HABIS' in c4.upper():
                cur['status'] = 'habis'

            # Update remaining hours from c4 (last numeric count wins)
            cnt = _safe_int(c4)
            if cnt is not None:
                cur['hours_remaining'] = cnt
                if cnt == 0:
                    cur['status'] = 'habis'

    # Flush last record (no VALID row encountered at end of data)
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
