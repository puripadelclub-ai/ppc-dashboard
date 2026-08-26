"""
scripts/test_supabase_connection.py
Verifikasi Sprint 1: cek koneksi Supabase + seed data.

Jalankan dari PowerShell:
    $env:SUPABASE_URL="https://vdtcrgbrnibyasjjnckw.supabase.co"
    $env:SUPABASE_SERVICE_KEY="sb_secret_FM7X..."
    python scripts/test_supabase_connection.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from lib.supabase_client import select, upsert, log_start, log_complete

TESTS_PASSED = 0
TESTS_FAILED = 0

def check(label, condition, detail=""):
    global TESTS_PASSED, TESTS_FAILED
    if condition:
        print(f"  ✅  {label}")
        TESTS_PASSED += 1
    else:
        print(f"  ❌  {label}{f'  →  {detail}' if detail else ''}")
        TESTS_FAILED += 1

print("\n=== Sprint 1 Verification ===\n")

# 1. Env vars
print("[ ENV VARS ]")
url = os.environ.get("SUPABASE_URL", "")
key = os.environ.get("SUPABASE_SERVICE_KEY", "")
check("SUPABASE_URL set", bool(url), "export dulu di PowerShell")
check("SUPABASE_SERVICE_KEY set", bool(key), "export dulu di PowerShell")

if not url or not key:
    print("\n⚠️  Set env vars dulu, kemudian jalankan ulang script ini.\n")
    sys.exit(1)

# 2. Tables & seed data
print("\n[ TABLES & SEED DATA ]")

courts = select("courts")
check("courts table exists", isinstance(courts, list), courts)
check("courts has 2 rows (Court 1 & 2)", len(courts) == 2, f"got {len(courts)}")

products = select("products")
check("products table exists", isinstance(products, list))
check("products has 13 rows", len(products) == 13, f"got {len(products)}")

kpis = select("kpi_definitions")
check("kpi_definitions table exists", isinstance(kpis, list))
check("kpi_definitions has 8 rows", len(kpis) == 8, f"got {len(kpis)}")

for tbl in ["members", "bookings", "transactions", "campaigns",
            "campaign_daily", "sync_logs", "daily_summaries"]:
    rows = select(tbl, limit=1)
    check(f"{tbl} table accessible", isinstance(rows, list), str(rows)[:80])

# 3. Upsert test
print("\n[ UPSERT & SYNC_LOG ]")

log_id = log_start("TEST", "sprint1_verification")
check("sync_log insert", log_id is not None, "log_start returned None")

if log_id:
    log_complete(log_id, "success", {"rows_inserted": 0})
    logs = select("sync_logs", {"job_name": "sprint1_verification"})
    check("sync_log readable after write", len(logs) >= 1)

# 4. parse_campaign_name
print("\n[ CAMPAIGN NAME PARSER ]")
from lib.supabase_client import parse_campaign_name

cases = [
    ("[049 - SI - Ball Machine - 18.08]",       "Ball Machine"),
    ("[054 - SI - Student Package - Batch 24.08]", "Student Package"),
    ("[051 - SI - Student Trial - 20.08]",       "Student Trial"),
    ("[045 - VID - Awareness - Batch 03.08]",    "Awareness"),
]
for name, expected in cases:
    result = parse_campaign_name(name)
    check(f"parse '{name[:30]}...'", result["offer"] == expected,
          f"got '{result['offer']}', expected '{expected}'")

# Summary
print(f"\n{'='*35}")
print(f"  Passed: {TESTS_PASSED}  |  Failed: {TESTS_FAILED}")
if TESTS_FAILED == 0:
    print("  🎉  Sprint 1 VERIFIED — siap lanjut Sprint 2!")
else:
    print("  ⚠️   Ada yang perlu diperbaiki sebelum Sprint 2.")
print(f"{'='*35}\n")
