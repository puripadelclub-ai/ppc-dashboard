"""
lib/supabase_client.py
Supabase PostgreSQL client untuk PPC Business OS.
Digunakan oleh semua pipeline: AVM, ESB, Meta Ads, Members.
"""

import os
import hashlib
import re
from datetime import datetime, timezone
from typing import Optional

import requests as _requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _headers() -> dict:
    return {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _url(table: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"


def _rpc(fn: str, params: dict) -> dict:
    return f"{SUPABASE_URL}/rest/v1/rpc/{fn}"


# ---------------------------------------------------------------------------
# Generic upsert / insert
# ---------------------------------------------------------------------------

def upsert(table: str, rows: list[dict], on_conflict: str = "id") -> dict:
    """
    Upsert rows into a Supabase table.
    Returns {"inserted": N, "error": None|str}
    """
    if not rows:
        return {"inserted": 0, "error": None}

    resp = _requests.post(
        _url(table),
        headers={**_headers(), "Prefer": f"resolution=merge-duplicates,return=representation"},
        params={"on_conflict": on_conflict},
        json=rows,
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        return {"inserted": 0, "error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
    data = resp.json()
    return {"inserted": len(data) if isinstance(data, list) else 1, "error": None}


def select(table: str, filters: dict = None, limit: int = 1000) -> list[dict]:
    """
    Simple SELECT from a Supabase table with optional eq filters.
    """
    params = {"limit": limit}
    headers = {**_headers(), "Prefer": "return=representation"}
    if filters:
        for k, v in filters.items():
            params[k] = f"eq.{v}"

    resp = _requests.get(_url(table), headers=headers, params=params, timeout=30)
    if resp.status_code != 200:
        return []
    return resp.json()


# ---------------------------------------------------------------------------
# sync_logs helpers
# ---------------------------------------------------------------------------

def log_start(source: str, job_name: str, date_start=None, date_end=None) -> Optional[str]:
    """Insert a sync_log row with status=running, return its id."""
    row = {
        "source": source,
        "job_name": job_name,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
    }
    if date_start:
        row["date_range_start"] = str(date_start)
    if date_end:
        row["date_range_end"] = str(date_end)

    resp = _requests.post(_url("sync_logs"), headers=_headers(), json=row, timeout=15)
    if resp.status_code in (200, 201):
        data = resp.json()
        return data[0]["id"] if isinstance(data, list) else data.get("id")
    return None


def log_complete(log_id: str, status: str, counts: dict = None, error: str = None):
    """PATCH sync_log row to completed."""
    if not log_id:
        return
    patch = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
    }
    if counts:
        patch.update(counts)
    if error:
        patch["error_message"] = error[:1000]

    _requests.patch(
        _url("sync_logs"),
        headers={**_headers(), "Prefer": "return=minimal"},
        params={"id": f"eq.{log_id}"},
        json=patch,
        timeout=15,
    )


# ---------------------------------------------------------------------------
# Storage: arsip file export mentah (ESB xlsx) di bucket privat
# ---------------------------------------------------------------------------

ESB_EXPORT_BUCKET = "esb-exports"
ESB_SALES_PREFIX = "sales-recap"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _storage_headers() -> dict:
    return {"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"}


def storage_upload(bucket: str, path: str, content: bytes, content_type: str = XLSX_MIME):
    resp = _requests.post(
        f"{SUPABASE_URL}/storage/v1/object/{bucket}/{path}",
        headers={**_storage_headers(), "Content-Type": content_type, "x-upsert": "true"},
        data=content,
        timeout=60,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Upload ke Supabase Storage gagal: HTTP {resp.status_code}: {resp.text[:300]}")


def storage_latest(bucket: str, prefix: str, suffix: str = ".xlsx") -> Optional[dict]:
    """File terbaru di bucket/prefix → {"path", "created_at"}, atau None kalau kosong."""
    resp = _requests.post(
        f"{SUPABASE_URL}/storage/v1/object/list/{bucket}",
        headers=_storage_headers(),
        json={"prefix": prefix, "limit": 20, "offset": 0,
              "sortBy": {"column": "created_at", "order": "desc"}},
        timeout=15,
    )
    resp.raise_for_status()
    files = [f for f in resp.json() if f.get("id") and f["name"].endswith(suffix)]
    if not files:
        return None
    newest = max(files, key=lambda f: f["created_at"])
    return {"path": f"{prefix}/{newest['name']}", "created_at": newest["created_at"]}


def storage_download(bucket: str, path: str) -> bytes:
    resp = _requests.get(
        f"{SUPABASE_URL}/storage/v1/object/{bucket}/{path}",
        headers=_storage_headers(),
        timeout=60,
    )
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------------------
# row_hash helper (untuk transactions dedup)
# ---------------------------------------------------------------------------

def make_row_hash(*parts) -> str:
    """SHA256 hash dari beberapa field untuk dedup."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def build_esb_transaction_rows(df_sales) -> tuple[list[dict], int]:
    """
    Baris ESB "Sales Recapitulation Detail Report" → baris tabel transactions.
    Return (rows, n_kembar). Baris dengan row_hash kembar dibuang karena satu
    upsert tidak boleh berisi conflict key yang sama dua kali.

    row_hash memuat Sales Number + Batch Order: satu bill bisa berisi item yang
    persis sama di batch order berbeda (mis. 2x "225K COURT RENT SOFT OPENING"
    di PPC01202605170013). Tanpa dua kolom itu baris kedua ikut terbuang.
    Angka di-hash sebagai int supaya hash tidak berubah kalau pandas membaca
    kolomnya sebagai float ("1.0") di satu file dan int ("1") di file lain.

    Revenue = Nett Sales (setelah diskon item dan bill discount):
    gross_amount = Subtotal (sebelum diskon), discount = Subtotal - Nett Sales,
    net_amount (kolom generated) = gross_amount - discount = Nett Sales.
    """
    import pandas as pd

    def _si(v, default=0) -> int:
        try:
            f = float(v)
            return default if f != f else int(f)  # f!=f → NaN check
        except Exception:
            return default

    rows: dict = {}
    n_dup = 0
    for _, r in df_sales.iterrows():
        _sd_raw = r.get("Sales Date", "")
        # Skip baris tanpa tanggal valid (NaT, NaN, None, blank) — baris total di footer
        if pd.isna(_sd_raw) or str(_sd_raw).lower() in ("nat", "nan", "none", ""):
            continue
        sale_date   = str(_sd_raw)[:10]
        sales_no    = str(r.get("Sales Number", "") or "").strip()
        batch_order = _si(r.get("Batch Order"), 0)
        member_raw  = r.get("Loyalty Member Name")
        # Input hash: NaN tetap jadi "nan" supaya row_hash baris yang sudah ada tidak berubah
        member_name = str(member_raw or "").strip()
        product     = str(r.get("Menu",   "") or r.get("Product",  "") or "").strip()
        category    = str(r.get("Menu Category", "") or r.get("Category", "") or "").strip()
        bill_no     = str(r.get("Bill No", "") or r.get("Bill Number", "") or "").strip()
        qty         = _si(r.get("Qty",  r.get("Quantity", 1)), 0)
        total       = _si(r.get("Total", r.get("Amount", r.get("Subtotal", 0))), 0)
        gross       = _si(r.get("Subtotal", total), total)
        nett        = _si(r.get("Nett Sales", total), total)
        pay_method  = str(r.get("Payment Method", "") or r.get("Payment", "") or "").strip()

        rh = make_row_hash(sale_date, sales_no, batch_order, bill_no,
                           member_name, product, qty, total)
        if rh in rows:
            n_dup += 1
            continue

        rows[rh] = {
            "row_hash":         rh,
            "transaction_date": sale_date or None,
            # Walk-in tanpa loyalty member → NULL, bukan string "nan"
            "customer_name":    None if pd.isna(member_raw) else (member_name or None),
            "product_name":     product or None,
            "category":         category or None,
            "gross_amount":     gross,
            "discount":         gross - nett,
            "payment_method":   pay_method or None,
            "source":           "ESB",
            # TIDAK kirim: net_amount (generated column), qty, unit_price
        }
    return list(rows.values()), n_dup


def prune_esb_transactions(keep_hashes: set, date_from: str, date_to: str,
                           max_ratio: float = 0.02) -> dict:
    """
    Hapus baris ESB di [date_from, date_to] yang row_hash-nya tidak ada lagi di
    export terbaru: bill di-void/dikoreksi di ESB, atau member/menu di-rename.
    Upsert saja tidak pernah menghapus, jadi tanpa ini hash lama tertinggal dan
    revenue terhitung dobel.

    Pengaman: kalau yang akan dihapus lebih dari max(20, max_ratio × baris di
    rentang itu), batal. Itu tanda file export-nya parsial/terfilter.
    Return {"stale": N, "deleted": N, "skipped": None | alasan}.
    """
    existing: list = []
    while True:
        resp = _requests.get(_url("transactions"), headers=_headers(), params=[
            ("select", "row_hash"), ("source", "eq.ESB"),
            ("transaction_date", f"gte.{date_from}"), ("transaction_date", f"lte.{date_to}"),
            ("order", "id"), ("limit", "1000"), ("offset", str(len(existing))),
        ], timeout=30)
        resp.raise_for_status()
        page = resp.json()
        existing += [r["row_hash"] for r in page]
        if len(page) < 1000:
            break

    stale = [h for h in existing if h and h not in keep_hashes]
    out = {"stale": len(stale), "deleted": 0, "skipped": None}
    limit = max(20, int(max_ratio * len(existing)))
    if len(stale) > limit:
        out["skipped"] = (f"{len(stale)} dari {len(existing)} baris {date_from}..{date_to} "
                          f"tidak ada di file export (batas {limit}); cek apakah file-nya lengkap")
        return out

    for i in range(0, len(stale), 100):
        chunk = stale[i : i + 100]
        resp = _requests.delete(
            _url("transactions"),
            headers={**_headers(), "Prefer": "return=minimal"},
            params={"source": "eq.ESB", "row_hash": f"in.({','.join(chunk)})"},
            timeout=30,
        )
        resp.raise_for_status()
        out["deleted"] += len(chunk)
    return out


# ---------------------------------------------------------------------------
# Campaign name parser (mirror dari dashboard.html extractOffer)
# ---------------------------------------------------------------------------

def parse_campaign_name(name: str) -> dict:
    """
    Parse "[045 - VID - Student Trial - Batch 03.08]" menjadi komponen.
    Returns: {num, type, offer, batch, raw}
    """
    result = {"num": None, "type": None, "offer": None, "batch": None, "raw": name}
    if not name:
        return result

    m = re.match(
        r"\[(\d+)\s*-\s*(\S+)\s*-\s*(.+?)\s*-\s*(?:Batch\s*)?(\d{2}\.\d{2})\]",
        name.strip(),
        re.IGNORECASE,
    )
    if m:
        result["num"]   = m.group(1)
        result["type"]  = m.group(2).upper()
        result["offer"] = _title_case(m.group(3).strip())
        result["batch"] = m.group(4)
    else:
        # Fallback: keyword match
        n = name.lower()
        if "student trial"       in n: result["offer"] = "Student Trial"
        elif "student package"   in n: result["offer"] = "Student Package"
        elif "ball machine"      in n: result["offer"] = "Ball Machine"
        elif "upgrade membership"in n: result["offer"] = "Upgrade Membership"
        elif "coaching"          in n: result["offer"] = "Coaching"
        elif "awareness"         in n: result["offer"] = "Awareness"
        elif "membership"        in n: result["offer"] = "Membership"
        else:                          result["offer"] = "Lainnya"

    return result


def _title_case(s: str) -> str:
    return " ".join(w.capitalize() for w in s.split())


# ---------------------------------------------------------------------------
# Convenience: upsert_bookings
# Dipakai oleh avm_client setelah migrasi
# ---------------------------------------------------------------------------

def upsert_bookings(rows: list[dict]) -> dict:
    return upsert("bookings", rows, on_conflict="avm_id")


def upsert_campaigns(rows: list[dict]) -> dict:
    return upsert("campaigns", rows, on_conflict="campaign_id")


def upsert_campaign_daily(rows: list[dict]) -> dict:
    return upsert("campaign_daily", rows, on_conflict="campaign_meta_id,report_date")


def upsert_ads_daily(rows: list[dict]) -> dict:
    """Upsert per-ad daily metrics. Dedup key: ad_name + report_date."""
    return upsert("ads_daily", rows, on_conflict="ad_name,report_date")


def upsert_transactions(rows: list[dict]) -> dict:
    return upsert("transactions", rows, on_conflict="row_hash")


def upsert_members(rows: list[dict]) -> dict:
    return upsert("members", rows, on_conflict="phone")


def upsert_daily_summaries(rows: list[dict]) -> dict:
    return upsert("daily_summaries", rows, on_conflict="summary_date")


def upsert_programs(rows: list[dict]) -> dict:
    """Upsert program purchases (court pass, comeback, dll). Dedup key: id."""
    return upsert("programs", rows, on_conflict="id")

# Backward-compat alias
upsert_court_passes = upsert_programs


def upsert_coaching_sessions(rows: list[dict]) -> dict:
    """Upsert coaching session rows. Dedup key: id (sha256 of date+member+time)."""
    return upsert("coaching_sessions", rows, on_conflict="id")
