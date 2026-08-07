#!/usr/bin/env python3
"""
scripts/update_readme_tab.py
Tambah panduan Filter View ke tab README di PPC Coaching Log.
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

COLOR_BRAND = {"red": 0.012, "green": 0.282, "blue": 0.259}
COLOR_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
COLOR_SECTION = {"red": 0.8, "green": 0.9, "blue": 0.9}


def get_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        print("ERROR: GOOGLE_CREDENTIALS env var tidak ditemukan.")
        sys.exit(1)
    creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
    return gspread.authorize(creds)


def update_readme():
    gc = get_client()
    sh = gc.open_by_key(COACHING_SHEET_ID)

    try:
        rm = sh.worksheet("README")
        rm.clear()
    except gspread.WorksheetNotFound:
        rm = sh.add_worksheet(title="README", rows=60, cols=3)

    readme_data = [
        # Header
        ["PPC COACHING LOG — PANDUAN PENGISIAN & PENGGUNAAN"],
        [""],

        # Bagian 1: Cara Pengisian
        ["BAGIAN 1: CARA PENGISIAN DATA", "", ""],
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

        # Aturan penting
        ["ATURAN PENTING", "", ""],
        ["1.", "Satu baris = satu sesi coaching. Jangan gabung dua sesi dalam satu baris.", ""],
        ["2.", "Jika satu member coaching 2x dalam sehari, buat 2 baris terpisah.", ""],
        ["3.", "Nama member HARUS konsisten — sama persis setiap bulan (dipakai untuk cross-data).", ""],
        ["4.", "Jangan hapus/ubah baris yang sudah Done — tambahkan baris baru jika ada koreksi.", ""],
        ["5.", "Add-on player (Rp100k): masukkan di kolom Notes, bukan ubah Participants.", ""],
        ["6.", "Pipeline otomatis baca sheet ini setiap hari. Cukup isi di tab raw_coaching.", ""],
        [""],

        # Bagian 2: Filter View
        ["BAGIAN 2: CARA LIHAT DATA PER BULAN (FILTER VIEW)", "", ""],
        ["", "", ""],
        ["Karena semua data ada di satu tab, gunakan Filter View untuk tampilan per bulan.", "", ""],
        ["", "", ""],
        ["CARA BUAT FILTER VIEW:", "", ""],
        ["Langkah 1", "Klik menu Data (di toolbar atas)", ""],
        ["Langkah 2", "Pilih: Filter views → Create new filter view", ""],
        ["Langkah 3", "Klik icon filter (▼) di header kolom Date", ""],
        ["Langkah 4", "Pilih: Filter by condition → Text contains", ""],
        ["Langkah 5", "Ketik kode bulan, contoh: 2026-04 (untuk April 2026)", ""],
        ["Langkah 6", "Klik OK — data langsung terfilter", ""],
        ["Langkah 7", "Beri nama filter view di kotak nama (pojok kiri atas): contoh 'April 2026'", ""],
        ["", "", ""],
        ["KODE BULAN:", "", ""],
        ["April 2026", "→ ketik: 2026-04", ""],
        ["Mei 2026", "→ ketik: 2026-05", ""],
        ["Juni 2026", "→ ketik: 2026-06", ""],
        ["Juli 2026", "→ ketik: 2026-07", ""],
        ["Agustus 2026", "→ ketik: 2026-08", ""],
        ["September 2026", "→ ketik: 2026-09", ""],
        ["", "", ""],
        ["CARA PAKAI FILTER VIEW YANG SUDAH DISIMPAN:", "", ""],
        ["Langkah 1", "Klik menu Data → Filter views", ""],
        ["Langkah 2", "Pilih nama bulan yang diinginkan (misal 'April 2026')", ""],
        ["Langkah 3", "Data otomatis terfilter. Klik X (pojok kanan atas) untuk keluar dari filter.", ""],
        ["", "", ""],

        # Bagian 3: Package Type
        ["BAGIAN 3: PENJELASAN PACKAGE TYPE", "", ""],
        ["", "", ""],
        ["PACKAGE TYPE", "KETERANGAN", "SESSIONS_REMAINING"],
        ["Free_Coaching", "Sesi gratis (trial / promo)", "Kosongkan"],
        ["First_Timer", "Coaching pertama kali (onboarding)", "Kosongkan"],
        ["Private", "Sesi private berbayar (per sesi)", "Kosongkan"],
        ["Bundling_4x", "Paket 4 sesi berbayar", "Isi sisa sesi (4, 3, 2, 1, 0)"],
        ["Bundling_6x", "Paket 6 sesi berbayar", "Isi sisa sesi (6, 5, 4, ..., 0)"],
        ["Coaching_Kids", "Coaching anak-anak", "Isi jika pakai paket"],
    ]

    rm.update(range_name="A1", values=readme_data)
    print("README content ditulis...")

    # Format batch
    requests = [
        # Header utama (baris 1)
        {"repeatCell": {
            "range": {"sheetId": rm.id, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": 3},
            "cell": {"userEnteredFormat": {
                "backgroundColor": COLOR_BRAND,
                "textFormat": {"foregroundColor": COLOR_WHITE, "bold": True, "fontSize": 13},
                "horizontalAlignment": "CENTER"
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
        }},
        # Section headers (baris 3, 17, 24, 46)
        *[{"repeatCell": {
            "range": {"sheetId": rm.id, "startRowIndex": r, "endRowIndex": r + 1,
                      "startColumnIndex": 0, "endColumnIndex": 3},
            "cell": {"userEnteredFormat": {
                "backgroundColor": COLOR_SECTION,
                "textFormat": {"bold": True, "fontSize": 10},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat)"
        }} for r in [2, 15, 23, 45]],
        # Sub-section headers
        *[{"repeatCell": {
            "range": {"sheetId": rm.id, "startRowIndex": r, "endRowIndex": r + 1,
                      "startColumnIndex": 0, "endColumnIndex": 3},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True},
            }},
            "fields": "userEnteredFormat.textFormat"
        }} for r in [3, 29, 36, 46]],
        # Column widths
        {"updateDimensionProperties": {
            "range": {"sheetId": rm.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 200}, "fields": "pixelSize"
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": rm.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 420}, "fields": "pixelSize"
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": rm.id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
            "properties": {"pixelSize": 280}, "fields": "pixelSize"
        }},
    ]

    sh.batch_update({"requests": requests})
    print("Formatting selesai")
    print(f"\n✅ README updated!")
    print(f"   Sheet: https://docs.google.com/spreadsheets/d/{COACHING_SHEET_ID}/edit")


if __name__ == "__main__":
    update_readme()
