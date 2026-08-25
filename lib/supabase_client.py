"""
lib/supabase_client.py
Supabase PostgreSQL client untuk PPC Business OS.
Digunakan oleh semua pipeline: AVM, ESB, Meta Ads, Members.
"""

import os
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

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

    with httpx.Client(timeout=30) as client:
        resp = client.post(
            _url(table),
            headers={**_headers(), "Prefer": f"resolution=merge-duplicates,return=representation"},
            params={"on_conflict": on_conflict},
            json=rows,
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

    with httpx.Client(timeout=30) as client:
        resp = client.get(_url(table), headers=headers, params=params)
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

    with httpx.Client(timeout=15) as client:
        resp = client.post(
            _url("sync_logs"),
            headers=_headers(),
            json=row,
        )
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

    with httpx.Client(timeout=15) as client:
        client.patch(
            _url("sync_logs"),
            headers={**_headers(), "Prefer": "return=minimal"},
            params={"id": f"eq.{log_id}"},
            json=patch,
        )


# ---------------------------------------------------------------------------
# row_hash helper (untuk transactions dedup)
# ---------------------------------------------------------------------------

def make_row_hash(*parts) -> str:
    """SHA256 hash dari beberapa field untuk dedup."""
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


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


def upsert_transactions(rows: list[dict]) -> dict:
    return upsert("transactions", rows, on_conflict="row_hash")


def upsert_members(rows: list[dict]) -> dict:
    return upsert("members", rows, on_conflict="phone")


def upsert_daily_summary(row: dict) -> dict:
    return upsert("daily_summaries", [row], on_conflict="summary_date")
