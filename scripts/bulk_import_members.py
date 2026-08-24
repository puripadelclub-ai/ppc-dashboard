"""
scripts/bulk_import_members.py
Bulk import member PPC dari Google Sheets ke Supabase Auth.

- Login: nomor HP (normalized) sebagai username
- Password default: 12345
- Role: member
- Email format: {phone}@puripadelclub.com

Kebutuhan env:
  SUPABASE_URL          = https://vdtcrgbrnibyasjjnckw.supabase.co
  SUPABASE_SERVICE_KEY  = service_role key (bukan anon key)
  GOOGLE_CREDENTIALS    = path ke service account JSON (atau pakai GSPREAD_TOKEN)

Cara run:
  python scripts/bulk_import_members.py --dry-run   # preview saja
  python scripts/bulk_import_members.py              # import sungguhan
"""

import os, re, sys, json, time, argparse
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

# ── Config ────────────────────────────────────────────────
SHEET_ID        = "1MAlR1WG7184GTCBmCTPUcX4OZKd09H1gx_0aSTKjt4k"
SUPABASE_URL    = os.environ.get("SUPABASE_URL", "https://vdtcrgbrnibyasjjnckw.supabase.co")
SERVICE_KEY     = os.environ.get("SUPABASE_SERVICE_KEY", "")
DEFAULT_PASS    = "12345"
EMAIL_DOMAIN    = "puripadelclub.com"

HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
    "Content-Type": "application/json",
}


# ── Phone normalization ────────────────────────────────────
def normalize_phone(raw) -> str | None:
    """
    Normalisasi nomor HP ke format 08xx... (Indonesia).
    Return None jika tidak valid.
    """
    if not raw:
        return None
    p = str(raw).strip()
    p = re.sub(r"[-\s.\(\)]", "", p)   # hapus dash/spasi/titik

    # Kalau mulai 62 → ganti jadi 0
    if p.startswith("62"):
        p = "0" + p[2:]
    # Kalau tidak mulai 0 tapi angka semua → tambah 0
    elif not p.startswith("0") and p.isdigit():
        p = "0" + p

    # Validasi: harus angka, 10-13 digit, mulai 0
    if not p.isdigit() or not p.startswith("0") or not (10 <= len(p) <= 13):
        return None
    return p


# ── Read sheet via Sheets API (public) ────────────────────
def read_members_from_sheet() -> list[dict]:
    """
    Baca member list dari Google Sheets menggunakan Sheets API v4.
    Sheet harus di-share 'Anyone with the link can view'.
    """
    import gspread
    from sheets_client import get_gc

    gc = get_gc()
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.get_worksheet(0)
    rows = ws.get_all_records(head=2)   # baris ke-2 adalah header
    return rows


# ── Supabase admin: create user ────────────────────────────
def sb_create_user(email: str, password: str, metadata: dict) -> tuple[bool, str]:
    """Buat user baru di Supabase Auth. Return (success, message)."""
    url = f"{SUPABASE_URL}/auth/v1/admin/users"
    payload = {
        "email": email,
        "password": password,
        "email_confirm": True,          # skip email verification
        "user_metadata": metadata,
    }
    resp = requests.post(url, json=payload, headers=HEADERS, timeout=15)
    data = resp.json()

    if resp.status_code in (200, 201):
        return True, data.get("id", "")
    # Sudah ada → skip
    if "already" in str(data.get("msg", "")).lower() or \
       "already" in str(data.get("message", "")).lower() or \
       resp.status_code == 422:
        return True, "SKIP (already exists)"
    return False, str(data)


# ── Supabase admin: update password ───────────────────────
def sb_reset_password(user_id: str, new_password: str) -> bool:
    url = f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}"
    resp = requests.put(url, json={"password": new_password}, headers=HEADERS, timeout=15)
    return resp.status_code == 200


# ── Main ───────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Preview saja, tidak import")
    args = parser.parse_args()

    if not SERVICE_KEY and not args.dry_run:
        print("❌  SUPABASE_SERVICE_KEY belum diset. Export dulu:")
        print("    $env:SUPABASE_SERVICE_KEY='eyJ...'   (PowerShell)")
        sys.exit(1)

    print("📋  Membaca data member dari Google Sheets...")
    try:
        rows = read_members_from_sheet()
    except Exception as e:
        print(f"❌  Gagal baca sheet: {e}")
        sys.exit(1)

    print(f"    {len(rows)} baris ditemukan\n")

    ok = skip = fail = 0
    skipped_list = []
    failed_list  = []

    for row in rows:
        name         = str(row.get("Member Name", "")).strip()
        phone_raw    = str(row.get("Phone Number", "")).strip()
        member_code  = str(row.get("Member Code", "")).strip()
        join_date    = str(row.get("Join Date", "")).strip()

        if not name or not phone_raw:
            continue

        phone = normalize_phone(phone_raw)
        if not phone:
            skipped_list.append(f"  ⚠️  {name:30s} | phone invalid: '{phone_raw}'")
            skip += 1
            continue

        email    = f"{phone}@{EMAIL_DOMAIN}"
        metadata = {
            "role":        "member",
            "name":        name,
            "phone":       phone,
            "member_code": member_code,
            "join_date":   join_date,
        }

        status = "DRY RUN"
        if not args.dry_run:
            success, msg = sb_create_user(email, DEFAULT_PASS, metadata)
            status = f"✅ OK  {msg}" if success else f"❌ FAIL {msg}"
            if success:
                ok += 1
            else:
                failed_list.append(f"  ❌ {name:30s} | {email} | {msg}")
                fail += 1
            time.sleep(0.15)   # rate limit Supabase admin API
        else:
            print(f"  [DRY] {name:30s} | {phone} → {email}")
            ok += 1

    # Summary
    print(f"\n{'='*60}")
    print(f"SELESAI")
    print(f"  Berhasil : {ok}")
    print(f"  Skip     : {skip}  (phone tidak valid)")
    print(f"  Gagal    : {fail}")

    if skipped_list:
        print("\nNomor HP tidak valid (perlu dicek manual):")
        for s in skipped_list:
            print(s)

    if failed_list:
        print("\nGagal import:")
        for f in failed_list:
            print(f)

    print(f"\nMember bisa login di dashboard dengan:")
    print(f"  Username : nomor HP (contoh: 081212080824)")
    print(f"  Password : {DEFAULT_PASS}  (bisa diganti sendiri)")


if __name__ == "__main__":
    main()
