#!/usr/bin/env python3
"""
scripts/setup_coaching_sheet.py
================================
One-time setup untuk Google Sheet "PPC Coaching Log".
Membuat tab raw_coaching dengan:
  - Header row (frozen, warna PPC)
  - Dropdown validation: Package_Type & Status
  - Conditional formatting: Status warna
  - Column widths & number formats

Cara pakai:
  export GOOGLE_CREDENTIALS='{"type":"service_account",...}'
  export COACHING_SHEET_ID='1u43tbbA-wYPTpDsEBv-DYiHjmy4VLxOcdfmLo_SVgtw'
  python scripts/setup_coaching_sheet.py
"""
import os, json, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import gspread
from google.oauth2.service_account import Credentials

COACHING_SHEET_ID = os.environ.get(
    "COACHING_SHEET_ID", "1u43tbbA-wYPTpDsEBv-DYiHjmy4VLxOcdfmLo_SVgtw"
)
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── Kolom raw_coaching ──────────────────────────────────────────────────────
HEADERS = [
    "Date",               # A  YYYY-MM-DD
    "Member_Name",        # B  nama standar (lowercase untuk join)
    "Package_Type",       # C  dropdown enum
    "Participants",       # D  angka (1, 2, 3…)
    "Start_Time",         # E  HH:MM
    "End_Time",           # F  HH:MM
    "Sessions_Remaining", # G  angka, 0 = HABIS, kosong = tidak berlaku
    "Coach",              # H  nama coach
    "Status",             # I  dropdown enum
    "Notes",              # J  teks bebas
]

PACKAGE_TYPES = [
    "Free_Coaching",
    "Bundling_4x",
    "Bundling_6x",
    "Private",
    "Coaching_Kids",
    "First_Timer",
]

STATUSES = ["Done", "Reschedule", "Cancel", "No_Show"]

SAMPLE_ROWS = [
    ["2026-08-01", "Gabriela",      "Bundling_4x",   "1", "08:00", "09:00", "2", "Coach A", "Done", ""],
    ["2026-08-01", "Harvey",        "Coaching_Kids",  "3", "10:00", "11:00", "",  "Coach A", "Done", "kids group - 3 anak"],
    ["2026-08-02", "Annisa Sutomo", "Free_Coaching",  "1", "11:00", "12:00", "",  "Coach B", "Done", "rencana ambil bundling"],
    ["2026-08-02", "Anton",         "Bundling_4x",   "1", "16:00", "17:00", "1", "Coach A", "Done", ""],
]

# ── Warna brand PPC ──────────────────────────────────────────────────────────
COLOR_BRAND   = {"red": 0.012, "green": 0.282, "blue": 0.259}  # #034842
COLOR_WHITE   = {"red": 1.0,   "green": 1.0,   "blue": 1.0}
COLOR_DONE    = {"red": 0.204, "green": 0.659, "blue": 0.325}  # hijau
COLOR_SCHED   = {"red": 0.984, "green": 0.737, "blue": 0.020}  # kuning
COLOR_CANCEL  = {"red": 0.957, "green": 0.263, "blue": 0.212}  # merah
COLOR_NOSHOW  = {"red": 0.608, "green": 0.349, "blue": 0.714}  # ungu


def get_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        print("ERROR: GOOGLE_CREDENTIALS env var tidak ditemukan.")
        print("  export GOOGLE_CREDENTIALS='$(cat credentials.json)'")
        sys.exit(1)
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def setup():
    gc = get_client()
    sh = gc.open_by_key(COACHING_SHEET_ID)
    print(f"Opened: {sh.title} ({COACHING_SHEET_ID})")

    # ── 1. Buat / ambil tab raw_coaching ─────────────────────────────────────
    try:
        ws = sh.worksheet("raw_coaching")
        print("Tab 'raw_coaching' sudah ada, akan di-reset.")
        ws.clear()
    except gspread.WorksheetNotFound:
        # Rename default Sheet1
        existing = sh.worksheets()
        if existing and existing[0].title in ("Sheet1", "Lembar 1"):
            ws = existing[0]
            ws.update_title("raw_coaching")
            print("Renamed Sheet1 → raw_coaching")
        else:
            ws = sh.add_worksheet(title="raw_coaching", rows=2000, cols=10)
            print("Tab 'raw_coaching' dibuat")

    sheet_id = ws.id  # numeric ID untuk batch requests

    # ── 2. Tulis headers + sample rows ───────────────────────────────────────
    data = [HEADERS] + SAMPLE_ROWS
    ws.update(range_name="A1", values=data)
    print(f"Headers + {len(SAMPLE_ROWS)} sample rows ditulis")

    # ── 3. Batch update: formatting, validation, column widths ───────────────
    requests = []

    # 3a. Freeze baris pertama
    requests.append({"updateSheetProperties": {
        "properties": {
            "sheetId": sheet_id,
            "gridProperties": {"frozenRowCount": 1}
        },
        "fields": "gridProperties.frozenRowCount"
    }})

    # 3b. Header formatting (baris 1)
    requests.append({"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                  "startColumnIndex": 0, "endColumnIndex": 10},
        "cell": {"userEnteredFormat": {
            "backgroundColor":    COLOR_BRAND,
            "textFormat":         {"foregroundColor": COLOR_WHITE, "bold": True, "fontSize": 10},
            "horizontalAlignment": "CENTER",
            "verticalAlignment":  "MIDDLE"
        }},
        "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"
    }})

    # 3c. Alternating row colors untuk data rows
    requests.append({"addBanding": {
        "bandedRange": {
            "bandedRangeId": 1,
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2000,
                      "startColumnIndex": 0, "endColumnIndex": 10},
            "rowProperties": {
                "headerColor":     {"red": 0.012, "green": 0.282, "blue": 0.259},
                "firstBandColor":  {"red": 1.0, "green": 1.0, "blue": 1.0},
                "secondBandColor": {"red": 0.949, "green": 0.988, "blue": 0.988},
            }
        }
    }})

    # 3d. Column widths (pixels)
    col_widths = [110, 180, 130, 90, 80, 80, 140, 130, 110, 260]
    for i, w in enumerate(col_widths):
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": w},
            "fields": "pixelSize"
        }})

    # 3e. Number format: Date column (A) → YYYY-MM-DD text agar tidak dikonversi
    requests.append({"repeatCell": {
        "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2000,
                  "startColumnIndex": 0, "endColumnIndex": 1},
        "cell": {"userEnteredFormat": {
            "numberFormat": {"type": "TEXT"},
            "horizontalAlignment": "CENTER"
        }},
        "fields": "userEnteredFormat(numberFormat,horizontalAlignment)"
    }})

    # 3f. Number format: Participants (D) & Sessions_Remaining (G) → center
    for col in [3, 6]:
        requests.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2000,
                      "startColumnIndex": col, "endColumnIndex": col + 1},
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "NUMBER", "pattern": "0"},
                "horizontalAlignment": "CENTER"
            }},
            "fields": "userEnteredFormat(numberFormat,horizontalAlignment)"
        }})

    # 3g. Time columns (E, F) → center
    for col in [4, 5]:
        requests.append({"repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2000,
                      "startColumnIndex": col, "endColumnIndex": col + 1},
            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
            "fields": "userEnteredFormat.horizontalAlignment"
        }})

    # 3h. Data validation: Package_Type (column C, index 2)
    requests.append({"setDataValidation": {
        "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2000,
                  "startColumnIndex": 2, "endColumnIndex": 3},
        "rule": {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [{"userEnteredValue": v} for v in PACKAGE_TYPES]
            },
            "showCustomUi": True,
            "strict": True
        }
    }})

    # 3i. Data validation: Status (column I, index 8)
    requests.append({"setDataValidation": {
        "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2000,
                  "startColumnIndex": 8, "endColumnIndex": 9},
        "rule": {
            "condition": {
                "type": "ONE_OF_LIST",
                "values": [{"userEnteredValue": v} for v in STATUSES]
            },
            "showCustomUi": True,
            "strict": False
        }
    }})

    # 3j. Conditional formatting: Status warna (column I)
    status_cf = [
        ("Done",       COLOR_DONE),
        ("Reschedule", COLOR_SCHED),
        ("Cancel",     COLOR_CANCEL),
        ("No_Show",    COLOR_NOSHOW),
    ]
    for i, (val, color) in enumerate(status_cf):
        requests.append({"addConditionalFormatRule": {
            "rule": {
                "ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 2000,
                            "startColumnIndex": 8, "endColumnIndex": 9}],
                "booleanRule": {
                    "condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": val}]},
                    "format": {"backgroundColor": color}
                }
            },
            "index": i
        }})

    sh.batch_update({"requests": requests})
    print("Formatting, validation, conditional formatting: DONE")

    # ── 4. Buat tab README (instruksi untuk admin) ────────────────────────────
    try:
        rm = sh.worksheet("README")
        rm.clear()
    except gspread.WorksheetNotFound:
        rm = sh.add_worksheet(title="README", rows=30, cols=3)

    readme_data = [
        ["PPC COACHING LOG — PANDUAN PENGISIAN"],
        [""],
        ["KOLOM", "FORMAT", "CONTOH / KETERANGAN"],
        ["Date", "YYYY-MM-DD", "2026-08-01  (wajib, jangan ubah format)"],
        ["Member_Name", "Nama asli (konsisten)", "Gabriela  (sama persis dengan nama di ESB/AVM)"],
        ["Package_Type", "Pilih dari dropdown", "Free_Coaching / Bundling_4x / Bundling_6x / Private / Coaching_Kids / First_Timer"],
        ["Participants", "Angka saja", "1  atau  2  (jumlah orang ikut sesi)"],
        ["Start_Time", "HH:MM (24 jam)", "08:00  atau  16:30"],
        ["End_Time", "HH:MM (24 jam)", "09:00  atau  17:30"],
        ["Sessions_Remaining", "Angka sisa sesi", "3 = sisa 3x,  0 = HABIS,  kosong = tidak pakai paket"],
        ["Coach", "Nama coach", "Coach Andri  (konsisten tiap bulan)"],
        ["Status", "Pilih dari dropdown", "Done / Reschedule / Cancel / No_Show"],
        ["Notes", "Opsional, teks bebas", "Reschedule ke tgl 5,  Add-on player +Rp100k"],
        [""],
        ["ATURAN PENTING"],
        ["1. Satu baris = satu sesi coaching. Jangan gabung dua sesi dalam satu baris."],
        ["2. Jika satu member coaching 2x dalam sehari, buat 2 baris terpisah."],
        ["3. Nama member HARUS konsisten — sama persis setiap bulan (dipakai untuk cross-data)."],
        ["4. Jangan hapus/ubah baris yang sudah Done — tambahkan baris baru jika ada koreksi."],
        ["5. Add-on player (Rp100k): masukkan di kolom Notes, bukan ubah Participants."],
        ["6. Pipeline otomatis baca sheet ini setiap hari. Cukup isi di tab raw_coaching."],
    ]
    rm.update(range_name="A1", values=readme_data)

    # Format README header
    sh.batch_update({"requests": [
        {"repeatCell": {
            "range": {"sheetId": rm.id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 3},
            "cell": {"userEnteredFormat": {
                "backgroundColor": COLOR_BRAND,
                "textFormat": {"foregroundColor": COLOR_WHITE, "bold": True, "fontSize": 12}
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"
        }},
        {"repeatCell": {
            "range": {"sheetId": rm.id, "startRowIndex": 2, "endRowIndex": 3,
                      "startColumnIndex": 0, "endColumnIndex": 3},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.8, "green": 0.9, "blue": 0.9},
                "textFormat": {"bold": True}
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": rm.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 180}, "fields": "pixelSize"
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": rm.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 160}, "fields": "pixelSize"
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": rm.id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
            "properties": {"pixelSize": 500}, "fields": "pixelSize"
        }},
    ]})
    print("Tab README dibuat dengan panduan pengisian")

    print(f"\n✅ Setup selesai!")
    print(f"   Sheet: https://docs.google.com/spreadsheets/d/{COACHING_SHEET_ID}/edit")
    print(f"   Tab raw_coaching: siap diisi")
    print(f"   Tab README: panduan untuk admin")
    print(f"\n   Langkah berikutnya:")
    print(f"   1. Jalankan: python scripts/migrate_coaching_data.py")
    print(f"   2. Tambahkan COACHING_SHEET_ID ke Vercel env vars")


if __name__ == "__main__":
    setup()
