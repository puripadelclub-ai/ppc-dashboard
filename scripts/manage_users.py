"""
PPC Dashboard — Supabase User Management Script
Jalankan dari PowerShell:

  $env:SUPABASE_SERVICE_KEY="sb_secret_..."
  $env:OWNER_PASSWORD="..."
  $env:SUPERADMIN_PASSWORD="..."
  $env:ADMIN_PASSWORD="..."
  python scripts/manage_users.py

Semua env var di atas wajib diisi — tidak ada default password.

Requires: pip install requests
"""

import os, sys, requests

SB_URL = "https://vdtcrgbrnibyasjjnckw.supabase.co"
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

if not SB_KEY:
    print("ERROR: SUPABASE_SERVICE_KEY env var tidak ditemukan.")
    sys.exit(1)

HEADERS = {
    "apikey":        SB_KEY,
    "Authorization": f"Bearer {SB_KEY}",
    "Content-Type":  "application/json",
}

# ── Users to create ──────────────────────────────────────────────────────────
# Password diambil dari environment variable — tidak ada default.
def _require_pw(env_var):
    val = os.environ.get(env_var, "")
    if not val:
        raise RuntimeError(f"set {env_var} env var dulu.")
    return val


NEW_USERS = [
    {"email": "owner@ppc.com",      "password": _require_pw("OWNER_PASSWORD"),      "role": "owner"},
    {"email": "superadmin@ppc.com", "password": _require_pw("SUPERADMIN_PASSWORD"), "role": "superadmin"},
    {"email": "admin@ppc.com",      "password": _require_pw("ADMIN_PASSWORD"),      "role": "admin"},
]

# ── Users to delete ──────────────────────────────────────────────────────────
DELETE_EMAILS = [
    "puripadel.content@gmail.com",
    "anggaranurrushydi@gmail.com",
]


def list_users():
    r = requests.get(f"{SB_URL}/auth/v1/admin/users?per_page=200", headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json().get("users", [])


def create_user(email, password, role):
    payload = {
        "email":         email,
        "password":      password,
        "email_confirm": True,
        "app_metadata": {"role": role},
    }
    r = requests.post(f"{SB_URL}/auth/v1/admin/users", headers=HEADERS, json=payload, timeout=15)
    if r.status_code in (200, 201):
        print(f"  ✓ Created: {email}  (role={role})")
    elif r.status_code == 422 and "already" in r.text.lower():
        # User exists — update password + metadata instead
        users = list_users()
        uid = next((u["id"] for u in users if u["email"] == email), None)
        if uid:
            r2 = requests.put(
                f"{SB_URL}/auth/v1/admin/users/{uid}",
                headers=HEADERS,
                json={"password": password, "app_metadata": {"role": role}},
                timeout=15,
            )
            if r2.status_code == 200:
                print(f"  ↻ Updated existing: {email}  (role={role})")
            else:
                print(f"  ✗ Update failed: {email} — {r2.text}")
    else:
        print(f"  ✗ Create failed: {email} — {r.status_code} {r.text}")


def delete_user_by_email(email, all_users):
    uid = next((u["id"] for u in all_users if u["email"] == email), None)
    if not uid:
        print(f"  ~ Not found (skip): {email}")
        return
    r = requests.delete(f"{SB_URL}/auth/v1/admin/users/{uid}", headers=HEADERS, timeout=15)
    if r.status_code == 200:
        print(f"  ✓ Deleted: {email}")
    else:
        print(f"  ✗ Delete failed: {email} — {r.status_code} {r.text}")


def main():
    print("\n=== PPC User Management ===\n")

    print("Fetching current users...")
    all_users = list_users()
    print(f"  Found {len(all_users)} user(s) in Supabase Auth\n")

    print("--- Creating / updating users ---")
    for u in NEW_USERS:
        create_user(u["email"], u["password"], u["role"])

    print("\n--- Deleting old users ---")
    for email in DELETE_EMAILS:
        delete_user_by_email(email, all_users)

    print("\nDone.\n")


if __name__ == "__main__":
    main()
