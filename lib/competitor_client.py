"""
lib/competitor_client.py
Public Ayo.co.id venue availability scraper for competitor intelligence.
No authentication required — endpoint is fully public.

API endpoint:
  https://ayo.co.id/venues-ajax/op-times-and-fields?venue_id={id}&date={YYYY-MM-DD}

Occupancy methodology:
  - is_available=0 → slot is booked
  - is_available=1 → slot is still bookable
  - Occupancy % = booked_slots / total_visible_slots

IMPORTANT — Fetch timing:
  Past slots expire from the API response as the day progresses.
  Fetch at 07:00 WIB (00:00 UTC) via Vercel Cron for full-day snapshot.
  Morning occupancy will be null/understated if fetched mid-day.

Venue IDs sourced from each club's Ayo page:
  https://ayo.co.id/v/{venue-slug}  →  deep link: link.ayo.co.id/l/direct?type=venue&venue_id={ID}

Output format matches Drive benchmark (padel_benchmark_*.xlsx) column structure
so it is compatible with the existing Intelligence dashboard page.
"""

import requests
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed


# ── Competitor Registry ─────────────────────────────────────────────────────
# To add a new venue: find venue_id from the "Download App" deep link on ayo.co.id/v/<slug>
# Pattern: https://link.ayo.co.id/l/direct?type=venue&venue_id={ID}&...

COMPETITOR_VENUES = {
    # ── Jakarta Barat – PPC's primary competitive set ──────────────────────────
    # courts=0 → counted dynamically from Ayo API fields response
    # base_price=0 → revenue metrics will be null (not misleading)
    2519: {"name": "Hachi Padel Puri Indah",          "slug": "hachi-padel-puri-indah-jakarta",       "area": "Puri Indah",     "courts": 4, "price_tier": "Premium", "base_price": 288000},
    3358: {"name": "Kaia Padel Reserve",               "slug": "kaia-padel-reserve",                   "area": "Kembangan",      "courts": 5, "price_tier": "Mid",     "base_price": 150000},
    1982: {"name": "Glass House Padel",                "slug": "glass-house-padel",                    "area": "Tomang",         "courts": 2, "price_tier": "Mid",     "base_price": 125000},
    1930: {"name": "Padelgrounds",                     "slug": "padelgrounds",                         "area": "Jakarta Barat",  "courts": 4, "price_tier": "Value",   "base_price": 99000},
    1922: {"name": "Kobana Padel",                     "slug": "kobana-padel",                         "area": "Jakarta Barat",  "courts": 0, "price_tier": "Mid",     "base_price": 0},
    1628: {"name": "Anwa Racquet Privé",               "slug": "anwa-racquet-prive-ex-anwa-tennis-club","area": "Slipi",          "courts": 0, "price_tier": "Mid",     "base_price": 0},
    2110: {"name": "Hobi Padel Meruya",                "slug": "hobi-padel-meruya",                    "area": "Meruya",         "courts": 0, "price_tier": "Value",   "base_price": 0},
    2008: {"name": "Sini Padel Court",                 "slug": "sini-padel-court",                     "area": "Jakarta Barat",  "courts": 0, "price_tier": "Value",   "base_price": 0},
    1113: {"name": "Sunday Padel Meruya",              "slug": "sunday-padel-meruya",                  "area": "Meruya",         "courts": 0, "price_tier": "Value",   "base_price": 0},
    3274: {"name": "Clubhouse Meruya",                 "slug": "clubhouse-meruya",                     "area": "Meruya",         "courts": 0, "price_tier": "Mid",     "base_price": 0},
    3565: {"name": "Commune Courts",                   "slug": "commune-courts",                       "area": "Jakarta Barat",  "courts": 0, "price_tier": "Mid",     "base_price": 0},
    2443: {"name": "Courtyard Padel Club",             "slug": "courtyard-padel-club",                 "area": "Jakarta Barat",  "courts": 0, "price_tier": "Mid",     "base_price": 0},
    2165: {"name": "HQ Padel & Tennis",                "slug": "hq-padel-tennis",                      "area": "Jakarta Barat",  "courts": 0, "price_tier": "Mid",     "base_price": 0},
    2711: {"name": "Coco Padel Court Jakarta",         "slug": "coco-padel-court-jakarta",             "area": "Jakarta Barat",  "courts": 0, "price_tier": "Mid",     "base_price": 0},
    1214: {"name": "Orange Garden Padel Club",         "slug": "orange-garden-padel-club",             "area": "Jakarta Barat",  "courts": 0, "price_tier": "Mid",     "base_price": 0},
    2667: {"name": "Paddle Padel",                     "slug": "paddle-padel",                         "area": "Jakarta Barat",  "courts": 0, "price_tier": "Value",   "base_price": 0},
    3261: {"name": "MMT Padel",                        "slug": "mmt-padel",                            "area": "Puri",           "courts": 0, "price_tier": "Value",   "base_price": 0},
    607:  {"name": "Metro Puri Tennis Indoor",         "slug": "metro-puri-tennis-indoor",             "area": "Puri",           "courts": 0, "price_tier": "Value",   "base_price": 0},
    1590: {"name": "KSC Racquet Club",                 "slug": "ksc-racquet-club",                     "area": "Jakarta Barat",  "courts": 0, "price_tier": "Value",   "base_price": 0},
    583:  {"name": "Jungle Tennis",                    "slug": "jungle-tennis",                        "area": "Jakarta Barat",  "courts": 0, "price_tier": "Value",   "base_price": 0},
    694:  {"name": "Lucky Tennis Indoor",              "slug": "lucky-tennis-indoor",                  "area": "Jakarta Barat",  "courts": 0, "price_tier": "Value",   "base_price": 0},
    1406: {"name": "Boss Padel Kedoya",                "slug": "boss-padel-kedoya",                    "area": "Kedoya",         "courts": 0, "price_tier": "Value",   "base_price": 0},
    868:  {"name": "House of Tennis Kedoya",           "slug": "house-of-tennis-kedoya",               "area": "Kedoya",         "courts": 0, "price_tier": "Value",   "base_price": 0},
    1679: {"name": "IPC Padel Club Kedoya",            "slug": "ipc-padel-club-kedoya",                "area": "Kedoya",         "courts": 0, "price_tier": "Value",   "base_price": 0},
    2617: {"name": "JC Padel Kedoya",                  "slug": "jc-padel-kedoya",                      "area": "Kedoya",         "courts": 0, "price_tier": "Value",   "base_price": 0},

    # ── Jakarta Utara (Ancol, Bandengan) ───────────────────────────────────────
    927:  {"name": "Ancol Tennis Warehouse",           "slug": "ancol-tennis-warehouse",               "area": "Ancol",          "courts": 0, "price_tier": "Value",   "base_price": 0},
    776:  {"name": "BBS KJ Secondary Sports Field",    "slug": "bbs-kj-secondary-sports-field",        "area": "Kebon Jeruk",    "courts": 0, "price_tier": "Value",   "base_price": 0},
    617:  {"name": "Olympus Tennis Center",            "slug": "olympus-tennis-center",                "area": "Ancol",          "courts": 0, "price_tier": "Value",   "base_price": 0},
    1336: {"name": "Olympus Prime Bandengan",          "slug": "olympus-prime-sports-center-bandengan","area": "Bandengan",      "courts": 0, "price_tier": "Value",   "base_price": 0},

    # ── Greater Jakarta / Tangerang ────────────────────────────────────────────
    1678: {"name": "Padel Spot",                       "slug": "padel-spot",                           "area": "Tangerang",      "courts": 0, "price_tier": "Value",   "base_price": 0},
    1252: {"name": "Padel One Club",                   "slug": "padel-one-club",                       "area": "Tangerang",      "courts": 0, "price_tier": "Mid",     "base_price": 0},
    1222: {"name": "Westside Padel",                   "slug": "westside-padel",                       "area": "Tangerang",      "courts": 0, "price_tier": "Mid",     "base_price": 0},
    1589: {"name": "River Padel",                      "slug": "river-padel",                          "area": "Tangerang",      "courts": 0, "price_tier": "Mid",     "base_price": 0},
    1300: {"name": "The Citadel",                      "slug": "the-citadel",                          "area": "Tangerang",      "courts": 0, "price_tier": "Mid",     "base_price": 0},
    2318: {"name": "Vamos Arena Indonesia",            "slug": "vamos-arena-indonesia",                "area": "Tangerang",      "courts": 0, "price_tier": "Mid",     "base_price": 0},
    1368: {"name": "Vlocity Arena",                    "slug": "vlocity-arena",                        "area": "Tangerang",      "courts": 0, "price_tier": "Mid",     "base_price": 0},
    1832: {"name": "Sky Padel Club",                   "slug": "sky-padel-club",                       "area": "Tangerang",      "courts": 0, "price_tier": "Mid",     "base_price": 0},
    1812: {"name": "Terra Padel Court",                "slug": "terra-padel-court",                    "area": "Tangerang",      "courts": 0, "price_tier": "Mid",     "base_price": 0},
    2132: {"name": "The Padel Point",                  "slug": "the-padel-point",                      "area": "Tangerang",      "courts": 0, "price_tier": "Mid",     "base_price": 0},
    3249: {"name": "Ola Padel Club",                   "slug": "ola-padel-club",                       "area": "Tangerang",      "courts": 0, "price_tier": "Mid",     "base_price": 0},
}

# ── Canonical output columns — must match Drive benchmark format ────────────
# Dashboard (Intelligence page) reads these exact column names.
CANONICAL_COLUMNS = [
    "date",
    "Venue",
    "Courts",               # capital C — matches dashboard r.Courts
    "Overall Occ %",
    "Morning Occ %",
    "Afternoon Occ %",
    "Evening Occ %",
    "Rev Captured (M IDR)", # booked_slots × base_price / 1_000_000
    "Rev Ceiling (M IDR)",  # total_slots × base_price / 1_000_000
    "Value Capture %",      # Rev Captured / Rev Ceiling (decimal 0–1)
    "Value Index",          # Overall Occ % × Value Capture % (decimal 0–1)
    "snapshot_date",
]

AYO_API_BASE    = "https://ayo.co.id/venues-ajax/op-times-and-fields"
REQUEST_TIMEOUT = 20
_HEADERS        = {"User-Agent": "Mozilla/5.0 (compatible)"}


# ── Period classification ────────────────────────────────────────────────────
# Same bands as avm_client.py so PPC and competitor data are directly comparable.

def _period(start_time: str) -> str:
    """Morning=06:00–12:00 / Afternoon=12:00–18:00 / Evening=18:00+."""
    try:
        hour = int(str(start_time).split(":")[0])
        if hour < 12:
            return "Morning"
        elif hour < 18:
            return "Afternoon"
        else:
            return "Evening"
    except Exception:
        return "Unknown"


# ── Core fetch + calculation ─────────────────────────────────────────────────

def fetch_venue_availability(venue_id: int, date_str: str) -> dict:
    """
    Fetch raw slot availability for a venue on a given date.
    Returns dict: {venue_id, date, is_open, fields, error}
    """
    try:
        resp = requests.get(
            AYO_API_BASE,
            params={"venue_id": venue_id, "date": date_str},
            headers=_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "venue_id": venue_id,
            "date":     date_str,
            "is_open":  data.get("op_time", {}).get("is_open", False),
            "fields":   data.get("fields", []),
            "error":    None,
        }
    except Exception as e:
        return {
            "venue_id": venue_id,
            "date":     date_str,
            "is_open":  False,
            "fields":   [],
            "error":    str(e),
        }


def calculate_occupancy(raw: dict) -> dict:
    """
    Compute occupancy metrics from a fetch_venue_availability() result.

    Returns:
      - Per-period occ % as decimal 0–1 (None if no slots visible for that period)
      - Raw slot counts for revenue calculation
    """
    period_total  = {"Morning": 0, "Afternoon": 0, "Evening": 0}
    period_booked = {"Morning": 0, "Afternoon": 0, "Evening": 0}

    if raw.get("is_open") and raw.get("fields"):
        for field in raw["fields"]:
            for slot in field.get("slots", []):
                p = _period(slot.get("start_time", ""))
                if p not in period_total:
                    continue
                period_total[p] += 1
                if slot.get("is_available") == 0:
                    period_booked[p] += 1

    total_slots  = sum(period_total.values())
    booked_slots = sum(period_booked.values())

    def _pct(booked, total):
        """Return decimal 0–1, or None if no data for that period."""
        return round(booked / total, 4) if total > 0 else None

    return {
        "is_open":         raw.get("is_open", False),
        "total_slots":     total_slots,
        "booked_slots":    booked_slots,
        "morning_total":   period_total["Morning"],
        "morning_booked":  period_booked["Morning"],
        "afternoon_total": period_total["Afternoon"],
        "afternoon_booked":period_booked["Afternoon"],
        "evening_total":   period_total["Evening"],
        "evening_booked":  period_booked["Evening"],
        "Overall Occ %":   _pct(booked_slots, total_slots),
        "Morning Occ %":   _pct(period_booked["Morning"],   period_total["Morning"]),
        "Afternoon Occ %": _pct(period_booked["Afternoon"], period_total["Afternoon"]),
        "Evening Occ %":   _pct(period_booked["Evening"],   period_total["Evening"]),
    }


def _revenue_metrics(occ: dict, base_price: int) -> dict:
    """
    Calculate revenue-based metrics from slot counts and venue base price.

    Formulas (matching old Drive benchmark "Demand & Value" sheet):
      Rev Ceiling  = total_slots × base_price / 1_000_000
      Rev Captured = booked_slots × base_price / 1_000_000
      Value Capture % = Rev Captured / Rev Ceiling  (decimal 0–1)
      Value Index     = Overall Occ % × Value Capture %  (decimal 0–1)

    Assumes uniform pricing per slot (Ayo API does not expose peak/off-peak
    differential pricing per slot in the public endpoint).
    """
    total  = occ.get("total_slots", 0)
    booked = occ.get("booked_slots", 0)

    if total == 0:
        return {
            "Rev Ceiling (M IDR)":  None,
            "Rev Captured (M IDR)": None,
            "Value Capture %":      None,
            "Value Index":          None,
        }

    rev_ceiling  = round(total  * base_price / 1_000_000, 3)
    rev_captured = round(booked * base_price / 1_000_000, 3)
    value_capture = round(rev_captured / rev_ceiling, 4) if rev_ceiling > 0 else None

    overall_occ = occ.get("Overall Occ %")
    value_index = (
        round(overall_occ * value_capture, 4)
        if (overall_occ is not None and value_capture is not None)
        else None
    )

    return {
        "Rev Ceiling (M IDR)":  rev_ceiling,
        "Rev Captured (M IDR)": rev_captured,
        "Value Capture %":      value_capture,
        "Value Index":          value_index,
    }


def fetch_daily_occupancy(venue_id: int, date_str: str) -> dict:
    """
    Fetch + calculate all metrics for one venue on one date.
    Returns a flat dict with exactly CANONICAL_COLUMNS (+ error for logging).
    """
    info = COMPETITOR_VENUES.get(venue_id, {
        "name": f"Venue {venue_id}", "slug": "", "area": "",
        "courts": 0, "price_tier": "Unknown", "base_price": 0,
    })

    raw = fetch_venue_availability(venue_id, date_str)
    occ = calculate_occupancy(raw)
    rev = _revenue_metrics(occ, info["base_price"])

    # Dynamic court count: use registry value if set, else count from API fields
    courts = info.get("courts") or len(raw.get("fields", [])) or 0

    return {
        # ── Core (canonical) ──────────────────────────────────────────
        "date":                 date_str,
        "Venue":                info["name"],
        "Courts":               courts,                # capital C
        "Overall Occ %":        occ["Overall Occ %"],
        "Morning Occ %":        occ["Morning Occ %"],  # None if fetched mid-day
        "Afternoon Occ %":      occ["Afternoon Occ %"],
        "Evening Occ %":        occ["Evening Occ %"],
        "Rev Captured (M IDR)": rev["Rev Captured (M IDR)"],
        "Rev Ceiling (M IDR)":  rev["Rev Ceiling (M IDR)"],
        "Value Capture %":      rev["Value Capture %"],
        "Value Index":          rev["Value Index"],
        "snapshot_date":        date_str,
        # ── Internal (for logging only, not written to Sheets) ───────
        "_error":   raw.get("error"),
        "_booked":  occ["booked_slots"],
        "_total":   occ["total_slots"],
    }


def fetch_all_competitors(date_str: str = None, max_workers: int = 10) -> pd.DataFrame:
    """
    Fetch occupancy for all registered competitors in parallel.

    Args:
        date_str:    YYYY-MM-DD (defaults to today in Jakarta time UTC+7)
        max_workers: parallel HTTP requests — keep ≤4 (polite to Ayo)

    Returns:
        DataFrame with exactly CANONICAL_COLUMNS (no extra columns).
        Occ % values are decimals 0–1, matching Drive benchmark format.
    """
    if date_str is None:
        date_str = (datetime.utcnow() + timedelta(hours=7)).strftime("%Y-%m-%d")

    rows = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(fetch_daily_occupancy, vid, date_str): vid
            for vid in COMPETITOR_VENUES
        }
        for future in as_completed(future_map):
            try:
                rows.append(future.result())
            except Exception as e:
                vid = future_map[future]
                name = COMPETITOR_VENUES.get(vid, {}).get("name", str(vid))
                print(f"  ✗ {name} failed: {e}")

    if not rows:
        return pd.DataFrame(columns=CANONICAL_COLUMNS)

    df = pd.DataFrame(rows)

    # Sort: Premium first, then Mid, Value
    tier_order = {"Premium": 0, "Mid": 1, "Value": 2, "Unknown": 9}
    venue_tier = {info["name"]: info["price_tier"] for info in COMPETITOR_VENUES.values()}
    df["_tier_rank"] = df["Venue"].map(venue_tier).map(tier_order).fillna(9)
    df = df.sort_values(["_tier_rank", "Venue"]).reset_index(drop=True)

    # Print summary
    ok = df[df["_error"].isna() | (df["_error"] == "")].shape[0]
    print(f"Competitor scrape {date_str}: {ok}/{len(df)} venues OK")
    for _, row in df.iterrows():
        occ = row.get("Overall Occ %")
        occ_str = f"{float(occ):.1%}" if occ is not None else "N/A"
        icon = "✓" if not row.get("_error") else "✗"
        print(f"  {icon} {row['Venue']}: {occ_str} "
              f"({int(row.get('_booked', 0))}/{int(row.get('_total', 0))} slots) | "
              f"Rev {row.get('Rev Captured (M IDR)', 0):.2f}M / {row.get('Rev Ceiling (M IDR)', 0):.2f}M IDR")

    # Drop internal columns before returning
    internal_cols = [c for c in df.columns if c.startswith("_")]
    df = df.drop(columns=internal_cols)

    # Ensure exactly CANONICAL_COLUMNS, in order
    for col in CANONICAL_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df[CANONICAL_COLUMNS]

    return df


def accumulate_competitors(df_new: pd.DataFrame, df_existing: pd.DataFrame) -> pd.DataFrame:
    """
    Merge new scrape results with existing historical data.

    Strategy:
    - Drop today's rows from existing (replaced by fresh data)
    - Normalize both to CANONICAL_COLUMNS before concat
    - Keep all historical rows intact
    - Sort by date descending, then Venue
    """
    if df_new.empty:
        return df_existing if not df_existing.empty else pd.DataFrame(columns=CANONICAL_COLUMNS)

    if df_existing.empty:
        return df_new

    # Identify today's dates in new data
    today_dates = set(df_new["date"].dropna().unique())

    # Use 'date' or 'snapshot_date' as the date key for old rows
    date_key = "date" if "date" in df_existing.columns else "snapshot_date"
    df_hist = df_existing[~df_existing[date_key].isin(today_dates)].copy()

    # Normalize both to CANONICAL_COLUMNS
    def _normalize(df):
        for col in CANONICAL_COLUMNS:
            if col not in df.columns:
                df[col] = None
        return df[CANONICAL_COLUMNS].copy()

    df_hist_norm = _normalize(df_hist)
    df_new_norm  = _normalize(df_new)

    combined = pd.concat([df_hist_norm, df_new_norm], ignore_index=True)
    combined = (combined
                .sort_values(["date", "Venue"], ascending=[False, True])
                .reset_index(drop=True))

    return combined
