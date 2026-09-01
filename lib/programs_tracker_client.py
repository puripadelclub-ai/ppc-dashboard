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
  UPGRADE MEMBERSHIP (NEW): A=TYPE,B=NAMA,C=TGL_BELI,D=PAKET,E=HARGA,F=TGL_EXPIRE,
                              G=TGL_PAKAI,H=BENEFIT,I=STATUS,J=CATATAN
    PAKET options: "165k - 1x Coaching" | "215k - 1x Coaching + 1x Racket"
    BENEFIT options (PAKAI): "Coaching" | "Racket Rental"
    Valid: 30 hari dari TGL_BELI
  INDEPENDENCE DEAL: (read-only reference)

NOTE: UPGRADE MEMBERSHIP tab perlu di-clear dan pakai header baru:
  TYPE | NAMA | TGL_BELI | PAKET | HARGA | TGL_EXPIRE | TGL_PAKAI | BENEFIT | STATUS | CATATAN
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

EXPIRE_DAYS    = 90   # Comeback validity (default)
UM_EXPIRE_DAYS = 30   # Upgrade Membership validity

# Court Pass validity by pass size
def _cp_expire_days(paket):
    """Return validity days for a Court Pass based on package name."""
    p = str(paket).upper()
    if "50H" in p:
        return 180
    if "20H" in p:
        return 90
    if "8H" in p:
        return 30
    return 90  # fallback
WARN_DAYS      = 14
TODAY          = date.today()

TAB_NAMES = {
    "COURT_PASS":         "COURT PASS",
    "COMEBACK":           "COMEBACK",
    "TRIAL_STUDENT":      "TRIAL STUDENT",
    "UPGRADE_MEMBERSHIP": "UPGRADE MEMBERSHIP",
    "INDEPENDENCE_DEAL":  "INDEPENDENCE DEAL",
}

# Upgrade Membership packages: what each includes
# Keys match actual dropdown values in the Google Sheet
UPGRADE_PACKAGES = {
    "Upgrade Membership 165k": {"harga": 165000, "coaching": 1, "racket": 0},
    "Upgrade Membership 215k": {"harga": 215000, "coaching": 1, "racket": 1},
    # Fallback keys from old naming
    "165k - 1x Coaching":             {"harga": 165000, "coaching": 1, "racket": 0},
    "215k - 1x Coaching + 1x Racket": {"harga": 215000, "coaching": 1, "racket": 1},
}

# Trial Student packages
TRIAL_PACKAGES = {
    "Court 50rb":        {"harga": 50000},
    "Coaching 100rb":    {"harga": 100000},
    "Ball Machine 50rb": {"harga": 50000},
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
    "TRIAL_STUDENT": TRIAL_PACKAGES,
    "UPGRADE_MEMBERSHIP": {
        "Upgrade Membership 165k": {"harga": 165000},
        "Upgrade Membership 215k": {"harga": 215000},
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
            import re
            parts = re.split(r"[-/]", s)
            if fmt == "%d/%m/%Y":
                return date(int(parts[2]), int(parts[1]), int(parts[0]))
            elif fmt == "%Y-%m-%d" or fmt == "%Y/%m/%d":
                return date(int(parts[0]), int(parts[1]), int(parts[2]))
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
    """Status COURT PASS berdasarkan sisa jam dan expiry."""
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
    """Status COMEBACK berdasarkan sisa jam dan expiry."""
    if sisa <= 0:
        return "HABIS"
    if expiry_date is None:
        return "AKTIF"
    days_left = (expiry_date - TODAY).days
    if days_left < WARN_DAYS:
        return "HAMPIR EXPIRE"
    return "AKTIF"


def _ts_status(pakai_rows):
    """Status TRIAL STUDENT: TERJADWAL kalau belum dipakai, SELESAI kalau sudah."""
    return "SELESAI" if pakai_rows else "TERJADWAL"


def _um_status(sisa_coaching, sisa_racket, expiry_date):
    """Status UPGRADE MEMBERSHIP berdasarkan benefit sisa dan expiry."""
    has_sisa = (sisa_coaching > 0 or sisa_racket > 0)
    if not has_sisa:
        return "SELESAI"
    if expiry_date is None:
        return "AKTIF"
    days_left = (expiry_date - TODAY).days
    if days_left <= 0:
        return "EXPIRED"
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
            paket_str = str(row[3]).strip()
            tgl_expire = (tb + timedelta(days=_cp_expire_days(paket_str))) if tb else None
            tgl_expire_str = tgl_expire.isoformat() if tgl_expire else str(row[6]).strip()

            current_beli = {
                "type": "BELI",
                "sheet_row": sheet_row,
                "nama": str(row[1]).strip(),
                "tgl_beli": tgl_beli_str,
                "paket": paket_str,
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
    """
    Parse tab TRIAL STUDENT → list of BELI dicts dengan pakai_rows.
    BELI: TYPE=BELI, NAMA, TGL_BELI, PAKET, HARGA
    PAKAI: TYPE=PAKAI, NAMA, TGL_PAKAI(F), COURT(G), JAM_MULAI(H), JAM_SELESAI(I), JAM_PAKAI(J), CATATAN(L)
    """
    _, rows = read_raw_tab("TRIAL_STUDENT")
    result = []
    current_beli = None

    for idx, row in enumerate(rows):
        sheet_row = idx + 2
        while len(row) < 12:
            row.append("")
        typ = str(row[0]).strip().upper()

        if typ == "BELI":
            try:
                harga = int(str(row[4]).replace(".", "").replace(",", "").replace("Rp", "").strip() or 0)
            except Exception:
                harga = 0
            current_beli = {
                "type": "BELI",
                "sheet_row": sheet_row,
                "nama": str(row[1]).strip(),
                "tgl_beli": str(row[2]).strip(),
                "paket": str(row[3]).strip(),
                "harga": harga,
                "pakai_rows": [],
            }
            result.append(current_beli)

        elif typ == "PAKAI" and current_beli is not None:
            try:
                courts = int(str(row[6]).strip() or 1)
            except Exception:
                courts = 1
            jm = str(row[7]).strip()
            js = str(row[8]).strip()
            dur = _dur(jm, js)
            jam_pakai = courts * dur
            pakai = {
                "type": "PAKAI",
                "sheet_row": sheet_row,
                "tgl_pakai": str(row[5]).strip(),
                "court": courts,
                "jam_mulai": jm,
                "jam_selesai": js,
                "jam_pakai": jam_pakai,
                "catatan": str(row[11]).strip(),
            }
            current_beli["pakai_rows"].append(pakai)

    for beli in result:
        beli["status"] = _ts_status(beli["pakai_rows"])

    return result


def parse_upgrade_membership():
    """
    Parse tab UPGRADE MEMBERSHIP → list BELI dicts dengan pakai_rows.

    Sheet columns (14 cols, A–N):
      A=TYPE, B=NAMA, C=TGL_BELI, D=PAKET, E=HARGA,
      F=BENEFIT_TERSISA (formula/auto), G=BISA_BELI_LAGI (formula/auto = TGL_BELI+30),
      H=TGL_PAKAI, I=TIPE_BENEFIT, J=JAM_MULAI, K=JAM_SELESAI,
      L=SISA_BENEFIT (formula/auto), M=STATUS, N=CATATAN

    Package benefits:
      "Upgrade Membership 165k" → 1x Coaching
      "Upgrade Membership 215k" → 1x Coaching + 1x Racket Rental
    """
    _, rows = read_raw_tab("UPGRADE_MEMBERSHIP")
    result = []
    current_beli = None

    for idx, row in enumerate(rows):
        sheet_row = idx + 2
        while len(row) < 14:
            row.append("")
        typ = str(row[0]).strip().upper()

        if typ == "BELI":
            try:
                harga = int(str(row[4]).replace(".", "").replace(",", "").replace("Rp", "").strip() or 0)
            except Exception:
                harga = 0
            tgl_beli_str = str(row[2]).strip()
            tb = _parse_date(tgl_beli_str)
            # col G = BISA_BELI_LAGI (sheet formula = TGL_BELI+30 = next purchase date)
            tgl_expire_str = str(row[6]).strip()
            if not tgl_expire_str and tb:
                tgl_expire_str = (tb + timedelta(days=UM_EXPIRE_DAYS)).isoformat()

            paket = str(row[3]).strip()
            pkg_info = UPGRADE_PACKAGES.get(paket, {"coaching": 1, "racket": 0})

            current_beli = {
                "type": "BELI",
                "sheet_row": sheet_row,
                "nama": str(row[1]).strip(),
                "tgl_beli": tgl_beli_str,
                "paket": paket,
                "harga": harga,
                "tgl_expire": tgl_expire_str,
                "include_coaching": pkg_info["coaching"],
                "include_racket":   pkg_info["racket"],
                "status": str(row[12]).strip(),
                "catatan": str(row[13]).strip(),
                "pakai_rows": [],
                "_used_coaching": 0,
                "_used_racket":   0,
            }
            result.append(current_beli)

        elif typ == "PAKAI" and current_beli is not None:
            tipe_benefit = str(row[8]).strip()  # I=TIPE_BENEFIT
            if "racket" in tipe_benefit.lower():
                current_beli["_used_racket"] += 1
            else:
                current_beli["_used_coaching"] += 1
            pakai = {
                "type": "PAKAI",
                "sheet_row": sheet_row,
                "tgl_pakai":   str(row[7]).strip(),   # H=TGL_PAKAI
                "benefit":     tipe_benefit,           # I=TIPE_BENEFIT
                "jam_mulai":   str(row[9]).strip(),    # J=JAM_MULAI
                "jam_selesai": str(row[10]).strip(),   # K=JAM_SELESAI
                "catatan":     str(row[13]).strip(),   # N=CATATAN
            }
            current_beli["pakai_rows"].append(pakai)

    for beli in result:
        sisa_coaching = beli["include_coaching"] - beli["_used_coaching"]
        sisa_racket   = beli["include_racket"]   - beli["_used_racket"]
        beli["sisa_coaching"] = max(sisa_coaching, 0)
        beli["sisa_racket"]   = max(sisa_racket, 0)

        # Sisa benefit as human-readable description
        sisa_parts = []
        if beli["sisa_coaching"] > 0:
            sisa_parts.append(f"{beli['sisa_coaching']}x Coaching Gratis")
        if beli["sisa_racket"] > 0:
            sisa_parts.append(f"{beli['sisa_racket']}x Racket Rental")
        beli["sisa_benefit_str"] = " + ".join(sisa_parts) if sisa_parts else "—"

        exp = _parse_date(beli["tgl_expire"])
        beli["status"] = _um_status(beli["sisa_coaching"], beli["sisa_racket"], exp)
        del beli["_used_coaching"]
        del beli["_used_racket"]

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

def append_beli(tab_key, row_data):
    """Tambah baris BELI ke tab. Appends di akhir sheet."""
    gc = _gc()
    sh = gc.open_by_key(TRACKER_SHEET_ID)
    ws = sh.worksheet(TAB_NAMES[tab_key])
    ws.append_row(row_data, value_input_option="USER_ENTERED")
    return {"ok": True, "action": "appended_beli"}


def append_pakai(tab_key, nama, row_data):
    """
    Tambah baris PAKAI setelah PAKAI terakhir untuk nama + tab_key.
    Kalau tidak ada PAKAI sebelumnya, insert setelah BELI row-nya.
    """
    gc = _gc()
    sh = gc.open_by_key(TRACKER_SHEET_ID)
    ws = sh.worksheet(TAB_NAMES[tab_key])

    all_rows = ws.get_all_values()
    insert_after = None
    found_beli = None

    for i, row in enumerate(all_rows[1:], start=2):
        if len(row) < 2:
            continue
        row_nama = str(row[1]).strip().upper()  # col B = NAMA untuk semua tab baru
        row_type = str(row[0]).strip().upper()
        if row_nama == nama.upper() and row_type == "BELI":
            found_beli = i
        elif row_nama == nama.upper() and row_type == "PAKAI" and found_beli is not None:
            insert_after = i

    if insert_after is None and found_beli is not None:
        insert_after = found_beli

    if insert_after is not None:
        ws.insert_row(row_data, index=insert_after + 1, value_input_option="USER_ENTERED")
        return {"ok": True, "action": "inserted_pakai", "after_row": insert_after}
    else:
        ws.append_row(row_data, value_input_option="USER_ENTERED")
        return {"ok": True, "action": "appended_pakai"}


def build_court_pass_beli_row(data):
    """
    Build row array untuk COURT PASS BELI dari dict input.
    data keys: nama, tgl_beli, paket, harga, kuota, catatan
    """
    tgl_beli = data.get("tgl_beli", "")
    paket    = data.get("paket", "")
    tb = _parse_date(tgl_beli)
    tgl_expire = (tb + timedelta(days=_cp_expire_days(paket))).isoformat() if tb else ""
    kuota = int(data.get("kuota", 0))
    return [
        "BELI",
        data.get("nama", "").upper(),
        tgl_beli,
        paket,
        int(data.get("harga", 0)),
        kuota,
        tgl_expire,
        "",  # H=TGL_PAKAI
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
        "",  # C=TGL_BELI
        "",  # D=PAKET
        "",  # E=HARGA
        "",  # F=KUOTA
        "",  # G=TGL_EXPIRE
        data.get("tgl_pakai", ""),  # H=TGL_PAKAI
        courts,                     # I=JUMLAH_COURT
        jm,                         # J=JAM_MULAI
        js,                         # K=JAM_SELESAI
        jam_pakai,                  # L=JAM_PAKAI
        "",                         # M=SISA (dihitung saat display)
        "",                         # N=STATUS
        data.get("catatan", ""),    # O=CATATAN
    ]


def build_comeback_beli_row(data):
    """Build row array untuk COMEBACK BELI."""
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
        courts,    # J=JUMLAH_COURT
        jm,        # K=JAM_MULAI
        js,        # L=JAM_SELESAI
        jam_pakai, # M=JAM_PAKAI
        "",        # N=SISA
        "",        # O=STATUS
        data.get("catatan", ""),  # P=CATATAN
    ]


def build_trial_student_beli_row(data):
    """
    Build BELI row untuk TRIAL STUDENT.
    data keys: nama, tgl_beli, paket, harga, catatan
    Columns: A=TYPE,B=NAMA,C=TGL_BELI,D=PAKET,E=HARGA,F=TGL_PAKAI(empty),
             G=COURT(empty),H=JAM_MULAI(empty),I=JAM_SELESAI(empty),J=JAM_PAKAI(empty),K=STATUS,L=CATATAN
    """
    paket = data.get("paket", "Court 50rb")
    try:
        harga = int(data.get("harga") or TRIAL_PACKAGES.get(paket, {}).get("harga", 50000))
    except Exception:
        harga = TRIAL_PACKAGES.get(paket, {}).get("harga", 50000)
    return [
        "BELI",
        data.get("nama", "").upper(),
        data.get("tgl_beli", ""),
        paket,
        harga,
        "",  # F=TGL_PAKAI (empty for BELI)
        "",  # G=COURT
        "",  # H=JAM_MULAI
        "",  # I=JAM_SELESAI
        "",  # J=JAM_PAKAI
        "TERJADWAL",  # K=STATUS
        data.get("catatan", ""),  # L=CATATAN
    ]


def build_trial_student_pakai_row(data):
    """
    Build PAKAI row untuk TRIAL STUDENT.
    data keys: nama, tgl_pakai, court, jam_mulai, jam_selesai, catatan
    """
    courts = int(data.get("court", 1))
    jm = data.get("jam_mulai", "")
    js = data.get("jam_selesai", "")
    dur = _dur(jm, js)
    jam_pakai = courts * dur
    return [
        "PAKAI",
        data.get("nama", "").upper(),
        "",   # C=TGL_BELI (empty for PAKAI)
        "",   # D=PAKET
        "",   # E=HARGA
        data.get("tgl_pakai", ""),  # F=TGL_PAKAI
        courts,    # G=COURT
        jm,        # H=JAM_MULAI
        js,        # I=JAM_SELESAI
        jam_pakai, # J=JAM_PAKAI
        "SELESAI", # K=STATUS
        data.get("catatan", ""),  # L=CATATAN
    ]


# Keep old name for backward compat
def build_trial_student_row(data):
    return build_trial_student_beli_row(data)


def build_upgrade_membership_beli_row(data):
    """
    Build BELI row untuk UPGRADE MEMBERSHIP — 14 kolom matching sheet aktual.
    A=TYPE,B=NAMA,C=TGL_BELI,D=PAKET,E=HARGA,
    F=BENEFIT_TERSISA(formula/kosong),G=BISA_BELI_LAGI(formula/kosong),
    H=TGL_PAKAI(kosong),I=TIPE_BENEFIT(kosong),J=JAM_MULAI(kosong),K=JAM_SELESAI(kosong),
    L=SISA_BENEFIT(formula/kosong),M=STATUS,N=CATATAN
    Kolom F,G,L diisi formula di sheet — kita biarkan kosong, sheet yang hitung.
    """
    paket = data.get("paket", "Upgrade Membership 165k")
    try:
        harga = int(data.get("harga") or UPGRADE_PACKAGES.get(paket, {}).get("harga", 165000))
    except Exception:
        harga = UPGRADE_PACKAGES.get(paket, {}).get("harga", 165000)
    return [
        "BELI",                          # A=TYPE
        data.get("nama", "").upper(),    # B=NAMA
        data.get("tgl_beli", ""),        # C=TGL_BELI
        paket,                           # D=PAKET
        harga,                           # E=HARGA
        "",   # F=BENEFIT_TERSISA (formula)
        "",   # G=BISA_BELI_LAGI (formula = C+30)
        "",   # H=TGL_PAKAI (kosong untuk BELI)
        "",   # I=TIPE_BENEFIT
        "",   # J=JAM_MULAI
        "",   # K=JAM_SELESAI
        "",   # L=SISA_BENEFIT (formula)
        "AKTIF",                         # M=STATUS
        data.get("catatan", ""),         # N=CATATAN
    ]


def build_upgrade_membership_pakai_row(data):
    """
    Build PAKAI row untuk UPGRADE MEMBERSHIP — 14 kolom.
    data keys: nama, tgl_beli_parent, paket_parent, tgl_pakai, benefit, jam_mulai, jam_selesai, catatan
    """
    return [
        "PAKAI",                                      # A=TYPE
        data.get("nama", "").upper(),                 # B=NAMA
        data.get("tgl_beli_parent", ""),              # C=TGL_BELI (dari BELI parent, untuk referensi formula)
        data.get("paket_parent", ""),                 # D=PAKET (dari BELI parent)
        "",                                           # E=HARGA
        "",                                           # F=formula
        "",                                           # G=formula
        data.get("tgl_pakai", ""),                   # H=TGL_PAKAI
        data.get("benefit", "1x Coaching Gratis"),   # I=TIPE_BENEFIT
        data.get("jam_mulai", ""),                   # J=JAM_MULAI
        data.get("jam_selesai", ""),                 # K=JAM_SELESAI
        "",                                           # L=formula
        "DIGUNAKAN",                                  # M=STATUS
        data.get("catatan", ""),                     # N=CATATAN
    ]


# Keep old name for backward compat
def build_upgrade_membership_row(data):
    return build_upgrade_membership_beli_row(data)


# ─── UPDATE & DELETE ─────────────────────────────────────────────────────────

def update_row(tab_key, sheet_row, row_data):
    """
    Update baris tertentu di sheet (sheet_row = 1-indexed termasuk header).
    row_data = list nilai sesuai kolom tab.
    """
    gc = _gc()
    sh = gc.open_by_key(TRACKER_SHEET_ID)
    ws = sh.worksheet(TAB_NAMES[tab_key])
    col_end = chr(ord('A') + len(row_data) - 1)
    cell_range = f"A{sheet_row}:{col_end}{sheet_row}"
    ws.update(cell_range, [row_data], value_input_option="USER_ENTERED")
    return {"ok": True, "action": "updated", "sheet_row": sheet_row}


def delete_row(tab_key, sheet_row):
    """Hapus baris tertentu dari sheet (sheet_row = 1-indexed termasuk header)."""
    gc = _gc()
    sh = gc.open_by_key(TRACKER_SHEET_ID)
    ws = sh.worksheet(TAB_NAMES[tab_key])
    ws.delete_rows(sheet_row)
    return {"ok": True, "action": "deleted", "sheet_row": sheet_row}
