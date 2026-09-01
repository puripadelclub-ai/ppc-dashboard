"""
programs_tracker_client.py
Read + write PPC Programs Tracker Google Sheet.

Sheet: https://docs.google.com/spreadsheets/d/1bQtGhaSpZ4hht_36f3ffax2yreu1qEL2N_YNpB6YnA4
Tabs: COURT PASS, COMEBACK, TRIAL STUDENT, UPGRADE MEMBERSHIP, INDEPENDENCE DEAL

Column structures (0-indexed):
  COURT PASS: A=TYPE,B=NAMA,C=TGL_BELI,D=PAKET,E=HARGA,F=KUOTA,G=TGL_EXPIRE,
               H=TGL_PAKAI,I=JUMLAH_COURT,J=JAM_MULAI,K=JAM_SELESAI,L=JAM_PAKAI,
               M=SISA,N=STATUS,O=CATATAN
  COMEBACK:   A=TYPE,B=NAMA,C=TGL_BELI,D=BULAN,E=PAKET,F=HARGA,G=KUOTA,H=TGL_EXPIRE,
               I=TGL_PAKAI,J=JUMLAH_COURT,K=JAM_MULAI,L=JAM_SELESAI,M=JAM_PAKAI,
               N=SISA,O=STATUS,P=CATATAN
  TRIAL STUDENT: A=TYPE,B=NAMA,C=TGL_BELI,D=PAKET,E=HARGA,F=TGL_PAKAI,G=COURT,
                  H=JAM_MULAI,I=JAM_SELESAI,J=JAM_PAKAI,K=STATUS,L=CATATAN
  UPGRADE MEMBERSHIP: A=NAMA,B=TGL_UPGRADE,C=DARI,D=KE,E=HARGA,F=STATUS,G=CATATAN
  INDEPENDENCE DEAL: (read-only reference)
"""

import os
import json
from datetime import date, timedelta

import gspread
from google.oauth2.service_account import Credentials

TRACKER_SHEET_ID = os.environ.get(
    "PROGRAMS_TRACKER_SHEET_ID",
    "1bQtGhaSpZ4hht_36f3ffax2yreu1qEL2N_YNpB6YnA4"
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

EXPIRE_DAYS = 90
WARN_DAYS = 14
TODAY = date.today()

TAB_NAMES = {
    "COURT_PASS":         "COURT PASS",
    "COMEBACK":           "COMEBACK",
    "TRIAL_STUDENT":      "TRIAL STUDENT",
    "UPGRADE_MEMBERSHIP": "UPGRADE MEMBERSHIP",
    "INDEPENDENCE_DEAL":  "INDEPENDENCE DEAL",
}

# Package defaults untuk auto-fill di frontend
PACKAGE_DEFAULTS = {
    "COURT_PASS": {
        "20H Off-Peak Pass":  {"harga": 184500, "kuota": 20},
        "50H Weekend Pass":   {"harga": 175500, "kuota": 50},
        "8H Evening Pass":    {"harga": 243000, "kuota": 8},
        "8H Off-PH Pass":     {"harga": 193500, "kuota": 8},
        "8H Off-Peak Pass":   {"harga": 193500, "kuota": 8},
    },
    "COMEBACK": {
        "Buy 3 Get 1 Free (4 Jam)": {"harga": 607500, "kuota": 4},
    },
}


def _gc():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS env var tidak ditemukan")
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _parse_date(s):
    """Parse date string (YYYY-MM-DD or DD/MM/YYYY) to date object, or None."""
    if not s or str(s).strip() == "":
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return date(*[int(x) for x in __import__("re").split(r"[-/]", s)])
        except Exception:
            pass
    return None


def _dur(jam_mulai, jam_selesai):
    """Hitung durasi jam dari string HH:MM - HH:MM. Return 0 kalau invalid."""
    try:
        h1 = int(str(jam_mulai).split(":")[0])
        h2 = int(str(jam_selesai).split(":")[0])
        return max(h2 - h1, 0)
    except Exception:
        return 0


def _cp_status(sisa, expiry_date):
    """Hitung status COURT PASS berdasarkan sisa jam dan expiry."""
    if sisa <= 0:
        return "HABIS"
    if expiry_date is None:
        return "AKTIF"
    days_left = (expiry_date - TODAY).days
    if days_left <= 0:
        return "HANGUS"
    if days_left < WARN_DAYS:
        return "HAMPIR EXPIRE"
    return "AKTIF"


def _cb_status(sisa, expiry_date):
    """Hitung status COMEBACK berdasarkan sisa jam dan expiry."""
    if sisa <= 0:
        return "HABIS"
    if expiry_date is None:
        return "AKTIF"
    days_left = (expiry_date - TODAY).days
    if days_left < WARN_DAYS:
        return "HAMPIR EXPIRE"
    return "AKTIF"


# ─── READ FUNCTIONS ─────────────────────────────────────────────────────────

def read_raw_tab(tab_key):
    """
    Baca semua baris dari tab (termasuk header).
    Returns: (header_row, data_rows) atau ([], []) kalau error.
    """
    gc = _gc()
    sh = gc.open_by_key(TRACKER_SHEET_ID)
    ws = sh.worksheet(TAB_NAMES[tab_key])
    all_rows = ws.get_all_values()
    if not all_rows:
        return [], []
    return all_rows[0], all_rows[1:]


def parse_court_pass():
    """
    Parse tab COURT PASS → list of member dicts dengan BELI + PAKAI rows.
    Setiap entry menyertakan sheet_row (1-indexed, termasuk header di row 1).
    """
    _, rows = read_raw_tab("COURT_PASS")
    result = []
    current_beli = None

    for idx, row in enumerate(rows):
        sheet_row = idx + 2  # row 1 = header, data mulai row 2
        while len(row) < 15:
            row.append("")

        typ = str(row[0]).strip().upper()

        if typ == "BELI":
            try:
                kuota = int(str(row[5]).replace("H", "").strip() or 0)
            except Exception:
                kuota = 0
            try:
                harga = int(str(row[4]).replace(".", "").replace(",", "").replace("Rp", "").strip() or 0)
            except Exception:
                harga = 0

            tgl_beli_str = str(row[2]).strip()
            tb = _parse_date(tgl_beli_str)
            tgl_expire = (tb + timedelta(days=EXPIRE_DAYS)) if tb else None
            tgl_expire_str = tgl_expire.isoformat() if tgl_expire else str(row[6]).strip()

            current_beli = {
                "type": "BELI",
                "sheet_row": sheet_row,
                "nama": str(row[1]).strip(),
                "tgl_beli": tgl_beli_str,
                "paket": str(row[3]).strip(),
                "harga": harga,
                "kuota": kuota,
                "tgl_expire": tgl_expire_str,
                "status": str(row[13]).strip() or "AKTIF",
                "catatan": str(row[14]).strip() if len(row) > 14 else "",
                "pakai_rows": [],
                "_sisa_running": kuota,
            }
            result.append(current_beli)

        elif typ == "PAKAI" and current_beli is not None:
            try:
                courts = int(str(row[8]).strip() or 1)
            except Exception:
                courts = 1
            jm = str(row[9]).strip()
            js = str(row[10]).strip()
            dur = _dur(jm, js)
            jam_pakai = courts * dur

            current_beli["_sisa_running"] -= jam_pakai
            sisa = max(current_beli["_sisa_running"], 0)

            pakai = {
                "type": "PAKAI",
                "sheet_row": sheet_row,
                "tgl_pakai": str(row[7]).strip(),
                "jumlah_court": courts,
                "jam_mulai": jm,
                "jam_selesai": js,
                "jam_pakai": jam_pakai,
                "sisa": sisa,
                "catatan": str(row[14]).strip() if len(row) > 14 else "",
            }
            current_beli["pakai_rows"].append(pakai)

    for beli in result:
        sisa_final = beli["_sisa_running"]
        exp = _parse_date(beli["tgl_expire"])
        beli["status"] = _cp_status(sisa_final, exp)
        beli["sisa_total"] = max(sisa_final, 0)
        del beli["_sisa_running"]

    return result


def parse_comeback():
    """Parse tab COMEBACK → list of member dicts dengan sheet_row."""
    _, rows = read_raw_tab("COMEBACK")
    result = []
    current_beli = None

    for idx, row in enumerate(rows):
        sheet_row = idx + 2
        while len(row) < 16:
            row.append("")
        typ = str(row[0]).strip().upper()

        if typ == "BELI":
            try:
                kuota = int(str(row[6]).replace("H", "").strip() or 4)
            except Exception:
                kuota = 4
            try:
                harga = int(str(row[5]).replace(".", "").replace(",", "").replace("Rp", "").strip() or 0)
            except Exception:
                harga = 0
            tgl_beli_str = str(row[2]).strip()
            tb = _parse_date(tgl_beli_str)
            tgl_expire = (tb + timedelta(days=EXPIRE_DAYS)) if tb else None
            tgl_expire_str = tgl_expire.isoformat() if tgl_expire else str(row[7]).strip()

            current_beli = {
                "type": "BELI",
                "sheet_row": sheet_row,
                "nama": str(row[1]).strip(),
                "tgl_beli": tgl_beli_str,
                "bulan": str(row[3]).strip(),
                "paket": str(row[4]).strip(),
                "harga": harga,
                "kuota": kuota,
                "tgl_expire": tgl_expire_str,
                "status": str(row[14]).strip() or "AKTIF",
                "catatan": str(row[15]).strip() if len(row) > 15 else "",
                "pakai_rows": [],
                "_sisa_running": kuota,
            }
            result.append(current_beli)

        elif typ == "PAKAI" and current_beli is not None:
            try:
                courts = int(str(row[9]).strip() or 1)
            except Exception:
                courts = 1
            jm = str(row[10]).strip()
            js = str(row[11]).strip()
            dur = _dur(jm, js)
            jam_pakai = courts * dur
            current_beli["_sisa_running"] -= jam_pakai
            sisa = max(current_beli["_sisa_running"], 0)

            pakai = {
                "type": "PAKAI",
                "sheet_row": sheet_row,
                "tgl_pakai": str(row[8]).strip(),
                "jumlah_court": courts,
                "jam_mulai": jm,
                "jam_selesai": js,
                "jam_pakai": jam_pakai,
                "sisa": sisa,
                "catatan": str(row[15]).strip() if len(row) > 15 else "",
            }
            current_beli["pakai_rows"].append(pakai)

    for beli in result:
        sisa_final = beli["_sisa_running"]
        exp = _parse_date(beli["tgl_expire"])
        beli["status"] = _cb_status(sisa_final, exp)
        beli["sisa_total"] = max(sisa_final, 0)
        del beli["_sisa_running"]

    return result


def parse_trial_student():
    """Parse tab TRIAL STUDENT → list of entry dicts dengan sheet_row."""
    _, rows = read_raw_tab("TRIAL_STUDENT")
    result = []
    for idx, row in enumerate(rows):
        sheet_row = idx + 2
        while len(row) < 12:
            row.append("")
        typ = str(row[0]).strip().upper()
        if not typ or typ not in ("BELI", "PAKAI"):
            continue
        try:
            harga = int(str(row[4]).replace(".", "").replace(",", "").replace("Rp", "").strip() or 0)
        except Exception:
            harga = 0
        result.append({
            "type": typ,
            "sheet_row": sheet_row,
            "nama": str(row[1]).strip(),
            "tgl_beli": str(row[2]).strip(),
            "paket": str(row[3]).strip(),
            "harga": harga,
            "tgl_pakai": str(row[5]).strip(),
            "court": str(row[6]).strip(),
            "jam_mulai": str(row[7]).strip(),
            "jam_selesai": str(row[8]).strip(),
            "jam_pakai": str(row[9]).strip(),
            "status": str(row[10]).strip(),
            "catatan": str(row[11]).strip(),
        })
    return result


def parse_upgrade_membership():
    """Parse tab UPGRADE MEMBERSHIP → list of entry dicts dengan sheet_row."""
    _, rows = read_raw_tab("UPGRADE_MEMBERSHIP")
    result = []
    for idx, row in enumerate(rows):
        sheet_row = idx + 2
        while len(row) < 7:
            row.append("")
        nama = str(row[0]).strip()
        if not nama:
            continue
        try:
            harga = int(str(row[4]).replace(".", "").replace(",", "").replace("Rp", "").strip() or 0)
        except Exception:
            harga = 0
        result.append({
            "sheet_row": sheet_row,
            "nama": nama,
            "tgl_upgrade": str(row[1]).strip(),
            "dari": str(row[2]).strip(),
            "ke": str(row[3]).strip(),
            "harga": harga,
            "status": str(row[5]).strip(),
            "catatan": str(row[6]).strip() if len(row) > 6 else "",
        })
    return result


def get_all_tracker_data():
    """Baca semua tab dan return dict per tab."""
    return {
        "COURT_PASS":         parse_court_pass(),
        "COMEBACK":           parse_comeback(),
        "TRIAL_STUDENT":      parse_trial_student(),
        "UPGRADE_MEMBERSHIP": parse_upgrade_membership(),
        "package_defaults":   PACKAGE_DEFAULTS,
    }


# ─── WRITE FUNCTIONS ────────────────────────────────────────────────────────

def _find_insert_row(ws, nama, tab_key):
    """
    Cari nomor baris terakhir PAKAI untuk member 'nama' (gspread 1-indexed).
    Kalau tidak ditemukan, return None (append di bawah).
    """
    all_rows = ws.get_all_values()
    last_pakai_row = None
    for i, row in enumerate(all_rows[1:], start=2):  # skip header, 1-indexed
        if len(row) < 2:
            continue
        row_nama = str(row[1]).strip().upper() if tab_key != "UPGRADE_MEMBERSHIP" else str(row[0]).strip().upper()
        row_type = str(row[0]).strip().upper() if tab_key != "UPGRADE_MEMBERSHIP" else "BELI"
        if row_nama == nama.upper() and row_type == "PAKAI":
            last_pakai_row = i
    return last_pakai_row


def append_beli(tab_key, row_data):
    """
    Tambah baris BELI ke tab. row_data = list sesuai urutan kolom tab.
    Appends di akhir sheet.
    """
    gc = _gc()
    sh = gc.open_by_key(TRACKER_SHEET_ID)
    ws = sh.worksheet(TAB_NAMES[tab_key])
    ws.append_row(row_data, value_input_option="USER_ENTERED")
    return {"ok": True, "action": "appended_beli"}


def append_pakai(tab_key, nama, row_data):
    """
    Tambah baris PAKAI setelah PAKAI terakhir untuk nama + tab_key.
    Kalau tidak ada PAKAI sebelumnya, insert setelah BELI row-nya.
    Kalau nama tidak ditemukan, append di akhir.
    """
    gc = _gc()
    sh = gc.open_by_key(TRACKER_SHEET_ID)
    ws = sh.worksheet(TAB_NAMES[tab_key])

    # Cari posisi insert
    all_rows = ws.get_all_values()
    insert_after = None  # 1-indexed row number

    # Cari BELI terakhir untuk nama ini, lalu PAKAI terakhir setelah BELI itu
    found_beli = None
    for i, row in enumerate(all_rows[1:], start=2):
        if len(row) < 2:
            continue
        row_nama = str(row[1]).strip().upper()
        row_type = str(row[0]).strip().upper()
        if row_nama == nama.upper() and row_type == "BELI":
            found_beli = i  # update ke BELI terakhir
        elif row_nama == nama.upper() and row_type == "PAKAI" and found_beli is not None:
            insert_after = i  # update ke PAKAI terakhir setelah BELI ini

    # Kalau tidak ada PAKAI tapi ada BELI, insert setelah BELI
    if insert_after is None and found_beli is not None:
        insert_after = found_beli

    if insert_after is not None:
        # Insert row after insert_after (insert_after + 1 dalam gspread 1-indexed)
        ws.insert_row(row_data, index=insert_after + 1, value_input_option="USER_ENTERED")
        return {"ok": True, "action": "inserted_pakai", "after_row": insert_after}
    else:
        # Fallback: append di akhir
        ws.append_row(row_data, value_input_option="USER_ENTERED")
        return {"ok": True, "action": "appended_pakai"}


def build_court_pass_beli_row(data):
    """
    Build row array untuk COURT PASS BELI dari dict input.
    data keys: nama, tgl_beli, paket, harga, kuota, catatan
    """
    from datetime import datetime
    tgl_beli = data.get("tgl_beli", "")
    tb = _parse_date(tgl_beli)
    tgl_expire = (tb + timedelta(days=EXPIRE_DAYS)).isoformat() if tb else ""
    kuota = int(data.get("kuota", 0))
    return [
        "BELI",
        data.get("nama", "").upper(),
        tgl_beli,
        data.get("paket", ""),
        int(data.get("harga", 0)),
        kuota,
        tgl_expire,
        "",  # H=TGL_PAKAI (kosong untuk BELI)
        "",  # I=JUMLAH_COURT
        "",  # J=JAM_MULAI
        "",  # K=JAM_SELESAI
        "",  # L=JAM_PAKAI
        kuota,  # M=SISA (awal = kuota)
        "AKTIF",  # N=STATUS
        data.get("catatan", ""),  # O=CATATAN
    ]


def build_court_pass_pakai_row(data):
    """
    Build row array untuk COURT PASS PAKAI dari dict input.
    data keys: nama, tgl_pakai, jumlah_court, jam_mulai, jam_selesai, catatan
    """
    courts = int(data.get("jumlah_court", 1))
    jm = data.get("jam_mulai", "")
    js = data.get("jam_selesai", "")
    dur = _dur(jm, js)
    jam_pakai = courts * dur
    return [
        "PAKAI",
        data.get("nama", "").upper(),
        "",  # C=TGL_BELI (kosong untuk PAKAI)
        "",  # D=PAKET
        "",  # E=HARGA
        "",  # F=KUOTA
        "",  # G=TGL_EXPIRE
        data.get("tgl_pakai", ""),  # H=TGL_PAKAI
        courts,                     # I=JUMLAH_COURT
        jm,                         # J=JAM_MULAI
        js,                         # K=JAM_SELESAI
        jam_pakai,                  # L=JAM_PAKAI
        "",                         # M=SISA (akan dihitung saat display)
        "",                         # N=STATUS
        data.get("catatan", ""),    # O=CATATAN
    ]


def build_comeback_beli_row(data):
    """
    Build row array untuk COMEBACK BELI.
    data keys: nama, tgl_beli, bulan, paket, harga, kuota, catatan
    """
    tgl_beli = data.get("tgl_beli", "")
    tb = _parse_date(tgl_beli)
    tgl_expire = (tb + timedelta(days=EXPIRE_DAYS)).isoformat() if tb else ""
    kuota = int(data.get("kuota", 4))
    return [
        "BELI",
        data.get("nama", "").upper(),
        tgl_beli,
        data.get("bulan", ""),
        data.get("paket", "Buy 3 Get 1 Free (4 Jam)"),
        int(data.get("harga", 607500)),
        kuota,
        tgl_expire,
        "",  # I=TGL_PAKAI
        "",  # J=JUMLAH_COURT
        "",  # K=JAM_MULAI
        "",  # L=JAM_SELESAI
        "",  # M=JAM_PAKAI
        kuota,  # N=SISA
        "AKTIF",  # O=STATUS
        data.get("catatan", ""),  # P=CATATAN
    ]


def build_comeback_pakai_row(data):
    courts = int(data.get("jumlah_court", 1))
    jm = data.get("jam_mulai", "")
    js = data.get("jam_selesai", "")
    dur = _dur(jm, js)
    jam_pakai = courts * dur
    return [
        "PAKAI",
        data.get("nama", "").upper(),
        "",  # C=TGL_BELI
        "",  # D=BULAN
        "",  # E=PAKET
        "",  # F=HARGA
        "",  # G=KUOTA
        "",  # H=TGL_EXPIRE
        data.get("tgl_pakai", ""),  # I=TGL_PAKAI
        courts,   # J=JUMLAH_COURT
        jm,       # K=JAM_MULAI
        js,       # L=JAM_SELESAI
        jam_pakai,  # M=JAM_PAKAI
        "",       # N=SISA
        "",       # O=STATUS
        data.get("catatan", ""),  # P=CATATAN
    ]


def build_trial_student_row(data):
    """
    Build row array untuk TRIAL STUDENT (single row, no BELI/PAKAI split).
    data keys: nama, tgl_beli, paket, harga, tgl_pakai, court, jam_mulai, jam_selesai, status, catatan
    """
    jm = data.get("jam_mulai", "")
    js = data.get("jam_selesai", "")
    courts = int(data.get("court", 1))
    dur = _dur(jm, js)
    jam_pakai = courts * dur
    return [
        "BELI",
        data.get("nama", "").upper(),
        data.get("tgl_beli", ""),
        data.get("paket", "Trial Student 1 Jam"),
        int(data.get("harga", 150000)),
        data.get("tgl_pakai", ""),
        courts,
        jm,
        js,
        jam_pakai,
        data.get("status", "TERJADWAL"),
        data.get("catatan", ""),
    ]


def build_upgrade_membership_row(data):
    """
    Build row array untuk UPGRADE MEMBERSHIP.
    data keys: nama, tgl_upgrade, dari, ke, harga, status, catatan
    """
    return [
        data.get("nama", "").upper(),
        data.get("tgl_upgrade", ""),
        data.get("dari", ""),
        data.get("ke", ""),
        int(data.get("harga", 0)),
        data.get("status", "SELESAI"),
        data.get("catatan", ""),
    ]


# ─── UPDATE & DELETE ─────────────────────────────────────────────────────────

def update_row(tab_key, sheet_row, row_data):
    """
    Update baris tertentu di sheet (sheet_row = 1-indexed termasuk header).
    row_data = list nilai sesuai kolom tab.
    """
    gc = _gc()
    sh = gc.open_by_key(TRACKER_SHEET_ID)
    ws = sh.worksheet(TAB_NAMES[tab_key])
    # Tentukan range: mulai dari kolom A, panjang sesuai row_data
    col_end = chr(ord('A') + len(row_data) - 1)
    cell_range = f"A{sheet_row}:{col_end}{sheet_row}"
    ws.update(cell_range, [row_data], value_input_option="USER_ENTERED")
    return {"ok": True, "action": "updated", "sheet_row": sheet_row}


def delete_row(tab_key, sheet_row):
    """
    Hapus baris tertentu dari sheet (sheet_row = 1-indexed termasuk header).
    """
    gc = _gc()
    sh = gc.open_by_key(TRACKER_SHEET_ID)
    ws = sh.worksheet(TAB_NAMES[tab_key])
    ws.delete_rows(sheet_row)
    return {"ok": True, "action": "deleted", "sheet_row": sheet_row}
