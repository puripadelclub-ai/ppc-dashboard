#!/usr/bin/env python3
"""
scripts/migrate_coaching_data.py
==================================
Migrasi data coaching lama (format per-tab-per-bulan) ke flat format
di tab raw_coaching Google Sheet "PPC Coaching Log".

Data source: Sheet lama dengan tab April, Mei, Juni, Juli, Agustus 2026
             (ID: 1dtmKhpbAeVu-YX9Lx1ayU4OPmdyYcRMg4IZGPBgd0iA)
Destination: PPC Coaching Log → tab raw_coaching
             (ID: 1u43tbbA-wYPTpDsEBv-DYiHjmy4VLxOcdfmLo_SVgtw)

Cara pakai:
  export GOOGLE_CREDENTIALS='{"type":"service_account",...}'
  python scripts/migrate_coaching_data.py

  # Preview tanpa tulis ke sheet:
  python scripts/migrate_coaching_data.py --dry-run
"""
import os, json, sys, re
import argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

import gspread
from google.oauth2.service_account import Credentials

OLD_SHEET_ID  = "1dtmKhpbAeVu-YX9Lx1ayU4OPmdyYcRMg4IZGPBgd0iA"
NEW_SHEET_ID  = os.environ.get("COACHING_SHEET_ID", "1u43tbbA-wYPTpDsEBv-DYiHjmy4VLxOcdfmLo_SVgtw")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Mapping nama bulan → nomor bulan
MONTH_MAP = {
    "april": 4, "mei": 5, "june": 6, "juni": 6,
    "july": 7, "juli": 7, "agustus": 8, "august": 8,
    "september": 9, "oktober": 10, "november": 11, "desember": 12,
}

# Normalisasi Package_Type
PACKAGE_NORM = {
    "free 1x":         "Free_Coaching",
    "free coaching":   "Free_Coaching",
    "free coachin":    "Free_Coaching",
    "bundling 4x":     "Bundling_4x",
    "bundling 6x":     "Bundling_6x",
    "private":         "Private",
    "coaching kids":   "Coaching_Kids",
    "kids":            "Coaching_Kids",
    "first timer":     "First_Timer",
    "first_timer":     "First_Timer",
}

OUTPUT_HEADERS = [
    "Date", "Member_Name", "Package_Type", "Participants",
    "Start_Time", "End_Time", "Sessions_Remaining", "Coach", "Status", "Notes"
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        print("ERROR: GOOGLE_CREDENTIALS env var tidak ditemukan.")
        sys.exit(1)
    creds = Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
    return gspread.authorize(creds)


def clean_merged(val: str) -> str:
    """Hilangkan prefix '[merged] ' dari nilai sel."""
    if not val:
        return ""
    return re.sub(r"^\[merged\]\s*", "", str(val).strip())


def parse_day(val: str) -> str:
    """Ekstrak nomor hari dari nilai (bisa '[merged] 9' atau '9')."""
    cleaned = clean_merged(val)
    m = re.match(r"^(\d+)$", cleaned)
    return m.group(1) if m else ""


def normalize_package(val: str) -> str:
    """Normalisasi Package_Type ke enum standar."""
    v = clean_merged(val).lower().strip()
    for key, norm in PACKAGE_NORM.items():
        if key in v:
            return norm
    return "Free_Coaching"  # default fallback


def parse_participants(val: str) -> str:
    """Ekstrak angka dari '1 Person', '2 Persons', '2 peson', dsb."""
    v = clean_merged(val)
    m = re.match(r"(\d+)", v)
    return m.group(1) if m else "1"


def parse_time_range(val: str) -> tuple[str, str]:
    """
    Parse '08.00-09.00' atau '08:00-09:00' atau '08.00 - 09.00'
    Returns (start_time, end_time) dalam format HH:MM.
    """
    v = clean_merged(val).strip()
    # Normalize separators
    v = v.replace(".", ":").replace(" ", "")
    m = re.match(r"(\d{1,2}:\d{2})[–\-](\d{1,2}:\d{2})", v)
    if m:
        def fmt(t):
            parts = t.split(":")
            return f"{int(parts[0]):02d}:{parts[1]}"
        return fmt(m.group(1)), fmt(m.group(2))
    return "", ""


def parse_sessions_remaining(notes: str) -> str:
    """
    Ekstrak sisa sesi dari teks catatan.
    'Sisa 3x' → '3', 'HABIS' → '0', lainnya → ''
    """
    v = clean_merged(notes).strip()
    if not v:
        return ""
    v_upper = v.upper()
    if "HABIS" in v_upper:
        return "0"
    m = re.search(r"sisa\s+(\d+)x?", v, re.IGNORECASE)
    if m:
        return m.group(1)
    return ""


def infer_month_from_tab(tab_name: str) -> int | None:
    """Cari nomor bulan dari nama tab (case-insensitive)."""
    name_lower = tab_name.lower()
    for keyword, month_num in MONTH_MAP.items():
        if keyword in name_lower:
            return month_num
    return None


def parse_coaching_tab(ws: gspread.Worksheet, month_num: int, year: int = 2026) -> list[dict]:
    """
    Parse satu tab coaching session ke list of dict (flat rows).

    Struktur tab lama:
      Baris 1-3: merged header "COACHING SESSION [MONTH] YEAR"
      Baris 4:   header kolom (Tanggal | Nama Member | ... | KET | Durasi/Jam | ...)
      Baris 5+:  data
    """
    all_values = ws.get_all_values()
    if not all_values:
        return []

    # Cari baris header kolom (yang mengandung "Tanggal")
    header_row_idx = None
    for i, row in enumerate(all_values):
        if any("tanggal" in str(c).lower() for c in row):
            header_row_idx = i
            break

    if header_row_idx is None:
        print(f"  [WARN] Header 'Tanggal' tidak ditemukan di tab '{ws.title}'")
        return []

    # Identifikasi kolom berdasarkan header
    header = [str(c).lower().strip() for c in all_values[header_row_idx]]

    def find_col(keywords):
        for kw in keywords:
            for i, h in enumerate(header):
                if kw in h:
                    return i
        return None

    col_tanggal   = find_col(["tanggal"])
    col_nama      = find_col(["nama"])
    col_orang     = find_col(["orang", "person"])
    col_ket       = find_col(["ket"])
    col_durasi    = find_col(["durasi", "jam"])
    col_catatan   = find_col(["catatan", "note", "keterangan"])

    # Fallback: cari kolom catatan lebih ke kanan setelah durasi
    if col_catatan is None and col_durasi is not None:
        # Kolom catatan biasanya 1-2 kolom setelah durasi
        col_catatan = col_durasi + 1 if col_durasi + 1 < len(header) else None

    rows_out = []
    last_day = ""

    data_rows = all_values[header_row_idx + 1:]

    for row in data_rows:
        # Pad row to min length
        while len(row) < max(filter(None, [col_tanggal, col_nama, col_ket, col_durasi, 10])) + 1:
            row.append("")

        def get(idx):
            if idx is None or idx >= len(row):
                return ""
            return str(row[idx]).strip()

        raw_tanggal = get(col_tanggal)
        raw_nama    = get(col_nama)
        raw_ket     = get(col_ket)
        raw_durasi  = get(col_durasi)
        raw_orang   = get(col_orang) if col_orang else "1"
        raw_catatan = get(col_catatan) if col_catatan else ""

        # Skip baris kosong atau baris summary (berisi teks statistik)
        nama = clean_merged(raw_nama).strip()
        if not nama or len(nama) < 2:
            continue
        # Skip baris yang terlihat seperti statistik / label
        if any(skip in nama.lower() for skip in [
            "note", "coaching lebih", "coaching berapa", "free coaching",
            "coaching kids", "bundling", "private", "add on", "first timer",
            "tgl ", "rumus"
        ]):
            continue
        # Skip jika nama mengandung angka di awal (statistik)
        if re.match(r"^\d+", nama):
            continue

        # Parsing tanggal
        day_str = parse_day(raw_tanggal)
        if day_str:
            last_day = day_str
        elif not last_day:
            continue  # belum ada tanggal valid
        # Gunakan last_day (handle merged cells)
        day = last_day

        try:
            day_int = int(day)
            date_str = f"{year}-{month_num:02d}-{day_int:02d}"
        except ValueError:
            continue

        # Parsing fields lain
        package     = normalize_package(raw_ket)
        participants = parse_participants(raw_orang)
        start_t, end_t = parse_time_range(raw_durasi)
        sess_rem    = parse_sessions_remaining(raw_catatan)

        # Notes: gabungkan catatan yang informatif
        notes_parts = []
        catatan_clean = clean_merged(raw_catatan).strip()
        # Hapus info sisa sesi dari notes (sudah diparsing ke Sessions_Remaining)
        catatan_filtered = re.sub(r"\bsisa\s+\d+x?\b", "", catatan_clean, flags=re.IGNORECASE).strip()
        catatan_filtered = re.sub(r"\bhabis\b", "", catatan_filtered, flags=re.IGNORECASE).strip()
        catatan_filtered = catatan_filtered.strip(" ,.-")
        if catatan_filtered:
            notes_parts.append(catatan_filtered)

        # Tandai add-on player dari catatan "(add on 100k)", "(with friend)"
        if re.search(r"add.on|100k", catatan_clean, re.IGNORECASE):
            notes_parts.append("add-on player")

        rows_out.append({
            "Date":                date_str,
            "Member_Name":         nama,
            "Package_Type":        package,
            "Participants":        participants,
            "Start_Time":          start_t,
            "End_Time":            end_t,
            "Sessions_Remaining":  sess_rem,
            "Coach":               "",   # tidak ada di data lama
            "Status":              "Done",
            "Notes":               "; ".join(notes_parts),
        })

    return rows_out


def migrate(dry_run: bool = False):
    gc = get_client()

    # ── Baca sheet lama ───────────────────────────────────────────────────────
    print(f"Membaca sheet lama: {OLD_SHEET_ID}")
    old_sh = gc.open_by_key(OLD_SHEET_ID)
    tabs = old_sh.worksheets()
    print(f"Tab ditemukan: {[t.title for t in tabs]}")

    all_rows = []

    for tab in tabs:
        month_num = infer_month_from_tab(tab.title)
        if month_num is None:
            print(f"  Skip tab '{tab.title}' (bukan tab bulan)")
            continue
        print(f"  Parsing tab '{tab.title}' (bulan {month_num:02d})...")
        rows = parse_coaching_tab(tab, month_num, year=2026)
        print(f"    → {len(rows)} sesi ditemukan")
        all_rows.extend(rows)

    # Sort by date
    all_rows.sort(key=lambda r: r["Date"])

    print(f"\nTotal: {len(all_rows)} sesi dari semua bulan")

    # Preview
    print("\n=== PREVIEW 10 BARIS PERTAMA ===")
    print(f"{'Date':<12} {'Member_Name':<22} {'Package_Type':<15} {'Part':>4} {'Start':<6} {'End':<6} {'Rem':>4} {'Status':<8} Notes")
    print("-" * 110)
    for r in all_rows[:10]:
        print(
            f"{r['Date']:<12} {r['Member_Name']:<22} {r['Package_Type']:<15} "
            f"{r['Participants']:>4} {r['Start_Time']:<6} {r['End_Time']:<6} "
            f"{r['Sessions_Remaining']:>4} {r['Status']:<8} {r['Notes'][:40]}"
        )
    if len(all_rows) > 10:
        print(f"... dan {len(all_rows) - 10} baris lagi")

    if dry_run:
        print("\n[DRY RUN] Tidak ada data yang ditulis ke sheet baru.")
        return

    if not all_rows:
        print("\n[WARN] Tidak ada data yang bisa dimigrasikan.")
        return

    # ── Tulis ke sheet baru ───────────────────────────────────────────────────
    print(f"\nMenulis ke sheet baru: {NEW_SHEET_ID}")
    new_sh = gc.open_by_key(NEW_SHEET_ID)
    try:
        ws_new = new_sh.worksheet("raw_coaching")
    except gspread.WorksheetNotFound:
        print("ERROR: Tab 'raw_coaching' belum ada. Jalankan setup_coaching_sheet.py dulu.")
        sys.exit(1)

    # Baca data yang sudah ada (jika ada)
    existing = ws_new.get_all_records()
    existing_dates_names = {(r.get("Date", ""), r.get("Member_Name", "")) for r in existing}
    print(f"  Data existing di raw_coaching: {len(existing)} baris")

    # Filter duplikat
    new_rows = []
    duplicates = 0
    for r in all_rows:
        key = (r["Date"], r["Member_Name"])
        if key in existing_dates_names:
            duplicates += 1
        else:
            new_rows.append(r)
    print(f"  Duplikat dilewati: {duplicates}")
    print(f"  Baris baru akan ditambahkan: {len(new_rows)}")

    if not new_rows:
        print("  Tidak ada data baru untuk ditambahkan.")
        return

    # Tambahkan setelah baris terakhir
    last_row = len(existing) + 2  # +1 for header, +1 for next row
    values_to_write = [[r[col] for col in OUTPUT_HEADERS] for r in new_rows]

    # Batch write dalam chunks of 500
    chunk_size = 500
    for i in range(0, len(values_to_write), chunk_size):
        chunk = values_to_write[i:i + chunk_size]
        ws_new.update(
            range_name=f"A{last_row}",
            values=chunk
        )
        last_row += len(chunk)
        print(f"  Chunk {i // chunk_size + 1}: {len(chunk)} baris ditulis (total up to row {last_row - 1})")

    print(f"\n✅ Migrasi selesai! {len(new_rows)} sesi berhasil dimigrasikan.")
    print(f"   Sheet: https://docs.google.com/spreadsheets/d/{NEW_SHEET_ID}/edit")
    print(f"\n   Catatan:")
    print(f"   - Kolom 'Coach' dikosongkan (data lama tidak mencatat coach)")
    print(f"   - Periksa dan lengkapi data di sheet jika perlu")
    print(f"   - Package_Type sudah dinormalisasi ke enum standar")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrasi data coaching lama ke flat format")
    parser.add_argument("--dry-run", action="store_true", help="Preview tanpa menulis ke sheet")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
