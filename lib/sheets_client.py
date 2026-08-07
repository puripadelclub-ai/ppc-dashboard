"""
sheets_client.py
Baca dan tulis data dari/ke Google Sheets.

Sheet yang digunakan:
  - PPC Dashboard Hub (SHEET_ID)        : output pipeline + raw ads/leads/avm
  - PPC Coaching Log (COACHING_SHEET_ID): input admin harian (raw_coaching)
  - WA Manual Data   (LEADS_SHEET_ID)   : input admin leads per minggu per bulan
"""
import os
import re
import json
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials


SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

# ── PPC Dashboard Hub tabs ───────────────────────────────────────────────────
TAB_RAW_ADS       = "raw_ads"
TAB_ACTUAL_LEADS  = "actual_leads"
TAB_COURT_PASS    = "court_pass"

TAB_OUT_MEMBERS   = "out_members"
TAB_OUT_CAMPAIGNS = "out_campaigns"
TAB_OUT_RFM       = "out_rfm"
TAB_OUT_REVENUE   = "out_revenue"
TAB_OUT_PRODUCTS  = "out_products"
TAB_OUT_RETENTION = "out_retention"
TAB_OUT_SUMMARY   = "out_summary"

# AVM (Ayo Venue Management) booking data
TAB_RAW_AVM       = "raw_avm"
TAB_OUT_AVM       = "out_avm"

# Occupancy benchmark (dari Drive padel_benchmark_*.xlsx)
TAB_OUT_OCCUPANCY   = "out_occupancy"
TAB_OUT_COMPETITORS = "out_competitors"

# Member product preferences (dari calculate_member_preferences)
TAB_OUT_PREFERENCES = "out_preferences"

# Coaching analytics output (ditulis pipeline, dibaca dashboard)
TAB_OUT_COACHING  = "out_coaching"

# Product detail breakdown (dari ESB, per menu/SKU)
TAB_OUT_PRODUCT_DETAIL = "out_product_detail"

# Program performance tracker (legacy — diganti Product Detail)
TAB_RAW_PROGRAMS  = "raw_programs"
TAB_OUT_PROGRAMS  = "out_programs"

# ── PPC Coaching Log tabs (sheet terpisah) ───────────────────────────────────
TAB_RAW_COACHING  = "raw_coaching"   # diisi admin harian


def get_gspread_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_sheet():
    """Buka PPC Dashboard Hub."""
    gc = get_gspread_client()
    sheet_id = os.environ.get("SHEET_ID")
    return gc.open_by_key(sheet_id)


def get_coaching_sheet():
    """Buka PPC Coaching Log (sheet terpisah untuk input admin)."""
    gc = get_gspread_client()
    coaching_sheet_id = os.environ.get(
        "COACHING_SHEET_ID", "1u43tbbA-wYPTpDsEBv-DYiHjmy4VLxOcdfmLo_SVgtw"
    )
    return gc.open_by_key(coaching_sheet_id)


def read_tab_as_df(tab_name):
    """Baca tab Google Sheet sebagai DataFrame."""
    sh = get_sheet()
    ws = sh.worksheet(tab_name)
    data = ws.get_all_records()
    return pd.DataFrame(data)


def batch_read_tabs(tab_names: list[str]) -> dict:
    """
    Baca beberapa tab sekaligus dalam 1 API call (batchGet).
    Jauh lebih cepat daripada read_tab_as_df() dipanggil N kali.
    Returns: {tab_name: DataFrame}
    """
    sh = get_sheet()
    ranges = [f"'{name}'" for name in tab_names]

    try:
        # gspread 6.x uses values_batch_get on the Spreadsheet object
        raw = sh.values_batch_get(ranges)
        results = [r.get('values', []) for r in raw.get('valueRanges', [])]
        dfs = {}
        for tab_name, values in zip(tab_names, results):
            try:
                if not values or len(values) < 2:
                    dfs[tab_name] = pd.DataFrame()
                    continue
                headers = [str(h) for h in values[0]]
                rows = values[1:]
                padded = [list(row) + [''] * (len(headers) - len(row)) for row in rows]
                df = pd.DataFrame(padded, columns=headers)
                dfs[tab_name] = df.fillna('')
            except Exception:
                dfs[tab_name] = pd.DataFrame()
        return dfs
    except Exception:
        # Fallback ke individual reads jika batch gagal
        return {name: _safe_read_tab(name) for name in tab_names}


def _safe_read_tab(tab_name: str) -> pd.DataFrame:
    try:
        return read_tab_as_df(tab_name).fillna('')
    except Exception:
        return pd.DataFrame()


def read_raw_ads():
    """
    Baca tab raw_ads yang diisi oleh /api/fetch-ads dari Meta Ads API (level ad).
    Kolom:
      Ad Name, Adset Name, Campaign Name, Date, Spend, Impressions, Reach,
      Clicks, CTR, CPM, Results, Cost_Per_Result
    """
    df = read_tab_as_df(TAB_RAW_ADS)
    if df.empty:
        return df
    df.columns = [c.strip() for c in df.columns]
    # Normalize kolom tanggal
    if "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    return df


def read_actual_leads():
    """
    Baca tab actual_leads — diisi manual oleh admin setiap periode.
    Kolom wajib: Period_Start, Period_End, Ads_Campaign, Offer,
                 Actual_Leads, Convert_Customer, Note
    Pipeline akan join dengan raw_ads untuk hitung CPL, CAC, Conversion Rate.
    """
    df = read_tab_as_df(TAB_ACTUAL_LEADS)
    if df.empty:
        return df
    df.columns = [c.strip() for c in df.columns]
    df["Period_Start"] = pd.to_datetime(df["Period_Start"], errors="coerce")
    df["Period_End"]   = pd.to_datetime(df["Period_End"],   errors="coerce")
    df["Actual_Leads"]     = pd.to_numeric(df.get("Actual_Leads", 0),     errors="coerce").fillna(0)
    df["Convert_Customer"] = pd.to_numeric(df.get("Convert_Customer", 0), errors="coerce").fillna(0)
    return df


# ── WA Manual Data Sheet (Leads per minggu, input admin) ────────────────────
LEADS_SHEET_ID = "1uBnwUV9Eo4ntN2aSRDDFwHswPhoxpGii9FXGwgqNXZA"

_MONTH_MAP = {
    'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,
    'july':7,'august':8,'september':9,'october':10,'november':11,'december':12,
    'jan':1,'feb':2,'mar':3,'apr':4,'jun':6,'jul':7,'aug':8,
    'sep':9,'oct':10,'nov':11,'dec':12,
}

def _parse_period_str(s):
    """
    Parse period string:
    - "18 July - 24 July 2026"
    - "1 - 7 August 2026"
    Returns (YYYY-MM-DD, YYYY-MM-DD) or (None, None)
    """
    s = s.strip().lower()
    # "18 july - 24 july 2026"
    m = re.search(r'(\d+)\s+(\w+)\s*[-–]\s*(\d+)\s+(\w+)\s+(\d{4})', s)
    if m:
        d1, mn1, d2, mn2, yr = m.groups()
        m1, m2 = _MONTH_MAP.get(mn1), _MONTH_MAP.get(mn2)
        if m1 and m2:
            return (f"{yr}-{m1:02d}-{int(d1):02d}", f"{yr}-{m2:02d}-{int(d2):02d}")
    # "1 - 7 august 2026"
    m = re.search(r'(\d+)\s*[-–]\s*(\d+)\s+(\w+)\s+(\d{4})', s)
    if m:
        d1, d2, mn, yr = m.groups()
        mn_num = _MONTH_MAP.get(mn)
        if mn_num:
            return (f"{yr}-{mn_num:02d}-{int(d1):02d}", f"{yr}-{mn_num:02d}-{int(d2):02d}")
    return (None, None)


def read_leads_from_monthly_sheet():
    """
    Baca WA Manual Data sheet — tab per bulan (July, August, dst).
    Setiap tab berisi beberapa periode mingguan Sabtu–Jumat.
    Format baris: [Period: DD Month - DD Month YYYY] lalu [Ads Campaign | Leads | Convert | Note]
    Returns DataFrame: Ads_Campaign, Period_Start, Period_End, Actual_Leads, Convert_Customer, Note
    """
    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(LEADS_SHEET_ID)
    except Exception:
        return pd.DataFrame()

    all_records = []

    for ws in sh.worksheets():
        try:
            raw_rows = ws.get_all_values()
            if not raw_rows:
                continue

            period_start, period_end = None, None

            for row in raw_rows:
                if not row:
                    continue
                first = (row[0] or '').strip()

                # Period header row (contains "Period:" or "period")
                first_lower = first.lower()
                if 'period' in first_lower and any(c.isdigit() for c in first):
                    # Strip "Period:" prefix and any trailing "Ads Campaign" text
                    period_part = re.sub(r'(?i)period\s*:', '', first).strip()
                    # Remove "Ads Campaign" suffix if present (merged cell artifact)
                    period_part = re.sub(r'(?i)ads\s*campaign.*$', '', period_part).strip()
                    period_start, period_end = _parse_period_str(period_part)
                    continue

                # Skip column header rows
                if first_lower in ('ads campaign', 'campaign', 'iklan', ''):
                    continue

                # Only process rows that look like ad names
                if not first.startswith('['):
                    continue

                try:
                    leads    = int(float(row[1])) if len(row) > 1 and str(row[1]).strip() not in ('', '-', '0') else (int(float(row[1])) if len(row) > 1 and str(row[1]).strip() == '0' else 0)
                    converts = int(float(row[2])) if len(row) > 2 and str(row[2]).strip() not in ('', '-') else 0
                    note     = str(row[3]).strip() if len(row) > 3 else ''
                except (ValueError, TypeError):
                    leads, converts, note = 0, 0, ''

                all_records.append({
                    'Ads_Campaign':    first,
                    'Period_Start':    period_start,
                    'Period_End':      period_end,
                    'Actual_Leads':    leads,
                    'Convert_Customer': converts,
                    'Note':            note,
                })
        except Exception:
            continue

    if not all_records:
        return pd.DataFrame()

    df = pd.DataFrame(all_records)
    df['Actual_Leads']     = pd.to_numeric(df['Actual_Leads'],     errors='coerce').fillna(0).astype(int)
    df['Convert_Customer'] = pd.to_numeric(df['Convert_Customer'], errors='coerce').fillna(0).astype(int)
    return df


def read_court_pass():
    return read_tab_as_df(TAB_COURT_PASS)


def read_raw_programs() -> pd.DataFrame:
    """
    Baca tab raw_programs dari PPC Dashboard Hub.
    Kolom: Program_Name, Program_Type, Date_Start, Date_End, Cost, Notes
    """
    try:
        df = read_tab_as_df(TAB_RAW_PROGRAMS)
        if df.empty:
            return pd.DataFrame()
        df.columns = [c.strip() for c in df.columns]
        for col in ["Date_Start", "Date_End"]:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")
        if "Cost" in df.columns:
            df["Cost"] = pd.to_numeric(
                df["Cost"].astype(str).str.replace(r"[^\d.]", "", regex=True),
                errors="coerce"
            ).fillna(0)
        return df
    except Exception as e:
        import warnings
        warnings.warn(f"read_raw_programs failed: {e}")
        return pd.DataFrame()


def read_raw_coaching() -> pd.DataFrame:
    """
    Baca tab raw_coaching dari PPC Coaching Log sheet (sheet terpisah).
    Kolom: Date, Member_Name, Package_Type, Participants, Start_Time,
           End_Time, Sessions_Remaining, Coach, Status, Notes

    Returns DataFrame dengan tipe yang sudah dinormalisasi.
    """
    try:
        sh = get_coaching_sheet()
        ws = sh.worksheet(TAB_RAW_COACHING)
        data = ws.get_all_records()
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data).fillna("")
        df.columns = [c.strip() for c in df.columns]

        # Normalisasi tipe data
        if "Date" in df.columns:
            df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        for num_col in ["Participants", "Sessions_Remaining"]:
            if num_col in df.columns:
                df[num_col] = pd.to_numeric(df[num_col], errors="coerce")
        # name_clean untuk join dengan ESB/AVM
        if "Member_Name" in df.columns:
            df["name_clean"] = df["Member_Name"].astype(str).str.lower().str.strip()
        return df
    except Exception as e:
        import warnings
        warnings.warn(f"read_raw_coaching failed: {e}")
        return pd.DataFrame()


def write_df_to_tab(df, tab_name):
    """Tulis DataFrame ke tab Sheet (overwrite semua data)."""
    sh = get_sheet()
    try:
        ws = sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=5000, cols=50)

    # Clear dulu
    ws.clear()

    # Convert ke list of lists
    df = df.fillna("").astype(str)
    headers = list(df.columns)
    rows = df.values.tolist()

    ws.update([headers] + rows)
    return True
