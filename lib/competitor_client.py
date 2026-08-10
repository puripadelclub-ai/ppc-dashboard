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
  Morning occupancy will be understated if fetched mid-day.

Venue IDs sourced from each club's Ayo page:
  https://ayo.co.id/v/{venue-slug}  →  deep link: link.ayo.co.id/l/direct?type=venue&venue_id={ID}
"""

import requests
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed


# ── Competitor Registry ─────────────────────────────────────────────────────
# To add a new venue: find venue_id from the "Download App" deep link on ayo.co.id/v/<slug>
# Pattern: https://link.ayo.co.id/l/direct?type=venue&venue_id={ID}&...

COMPETITOR_VENUES = {
    2519: {
        "name":       "Hachi Padel Puri Indah",
        "slug":       "hachi-padel-puri-indah-jakarta",
        "area":       "Puri Indah",
        "courts":     4,
        "price_tier": "Premium",
        "base_price": 288000,
    },
    3358: {
        "name":       "Kaia Padel Reserve",
        "slug":       "kaia-padel-reserve",
        "area":       "Kembangan",
        "courts":     5,
        "price_tier": "Mid",
        "base_price": 150000,
    },
    1982: {
        "name":       "Glass House Padel",
        "slug":       "glass-house-padel",
        "area":       "Tomang",
        "courts":     2,
        "price_tier": "Mid",
        "base_price": 125000,
    },
    1930: {
        "name":       "Padelgrounds",
        "slug":       "padelgrounds",
        "area":       "Jakarta Barat",
        "courts":     4,
        "price_tier": "Value",
        "base_price": 99000,
    },
    # Add more venues below as needed:
    # XXXXX: {
    #     "name": "Kobana Padel", "slug": "kobana-padel",
    #     "area": "...", "courts": N, "price_tier": "...", "base_price": 0,
    # },
}

AYO_API_BASE    = "https://ayo.co.id/venues-ajax/op-times-and-fields"
REQUEST_TIMEOUT = 20  # seconds per request
_HEADERS        = {"User-Agent": "Mozilla/5.0 (compatible)"}


# ── Period classification ────────────────────────────────────────────────────
# Same bands as avm_client.py so data is directly comparable

def _period(start_time: str) -> str:
    """Morning=06:00-12:00 / Afternoon=12:00-18:00 / Evening=18:00+."""
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


# ── Core fetch + occupancy functions ────────────────────────────────────────

def fetch_venue_availability(venue_id: int, date_str: str) -> dict:
    """
    Fetch raw slot availability for a venue on a given date.

    Returns:
        {
            venue_id, date, is_open: bool,
            fields: [{"field_name": str, "slots": [...]}],
            error: str|None
        }
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

    Returns dict with:
      - per-period slot counts (total, booked)
      - Overall Occ %, Morning Occ %, Afternoon Occ %, Evening Occ %
      - Values are 0–1 decimals (e.g. 0.625 = 62.5%), or None if no data
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
        return round(booked / total, 4) if total > 0 else None

    return {
        "is_open":          raw.get("is_open", False),
        "total_slots":      total_slots,
        "booked_slots":     booked_slots,
        "morning_total":    period_total["Morning"],
        "morning_booked":   period_booked["Morning"],
        "afternoon_total":  period_total["Afternoon"],
        "afternoon_booked": period_booked["Afternoon"],
        "evening_total":    period_total["Evening"],
        "evening_booked":   period_booked["Evening"],
        "Overall Occ %":    _pct(booked_slots, total_slots),
        "Morning Occ %":    _pct(period_booked["Morning"],   period_total["Morning"]),
        "Afternoon Occ %":  _pct(period_booked["Afternoon"], period_total["Afternoon"]),
        "Evening Occ %":    _pct(period_booked["Evening"],   period_total["Evening"]),
    }


def fetch_daily_occupancy(venue_id: int, date_str: str) -> dict:
    """
    Fetch + calculate occupancy for one venue on one date.
    Returns a flat dict ready for DataFrame construction.
    """
    info = COMPETITOR_VENUES.get(venue_id, {
        "name": f"Venue {venue_id}", "slug": "", "area": "",
        "courts": 0, "price_tier": "Unknown", "base_price": 0,
    })
    raw = fetch_venue_availability(venue_id, date_str)
    occ = calculate_occupancy(raw)

    return {
        "date":             date_str,
        "venue_id":         venue_id,
        "Venue":            info["name"],
        "area":             info["area"],
        "courts":           info["courts"],
        "price_tier":       info["price_tier"],
        "base_price":       info["base_price"],
        "is_open":          occ["is_open"],
        "total_slots":      occ["total_slots"],
        "booked_slots":     occ["booked_slots"],
        "morning_total":    occ["morning_total"],
        "morning_booked":   occ["morning_booked"],
        "afternoon_total":  occ["afternoon_total"],
        "afternoon_booked": occ["afternoon_booked"],
        "evening_total":    occ["evening_total"],
        "evening_booked":   occ["evening_booked"],
        "Overall Occ %":    occ["Overall Occ %"],
        "Morning Occ %":    occ["Morning Occ %"],
        "Afternoon Occ %":  occ["Afternoon Occ %"],
        "Evening Occ %":    occ["Evening Occ %"],
        "snapshot_date":    date_str,
        "error":            raw.get("error"),
    }


def fetch_all_competitors(date_str: str = None, max_workers: int = 4) -> pd.DataFrame:
    """
    Fetch occupancy for all registered competitors in parallel.

    Args:
        date_str:    YYYY-MM-DD (defaults to today in Jakarta time UTC+7)
        max_workers: parallel HTTP requests — keep ≤4, be polite to Ayo

    Returns:
        DataFrame, one row per venue.
        Key columns: date, Venue, Overall Occ %, Morning Occ %,
                     Afternoon Occ %, Evening Occ %, snapshot_date
        Occ % values are 0–1 decimals (compatible with drive_reader format).
    """
    if date_str is None:
        # Jakarta = UTC+7
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
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Sort: Premium first, then Mid, then Value
    tier_order = {"Premium": 0, "Mid": 1, "Value": 2, "Unknown": 9}
    df["_tier_rank"] = df["price_tier"].map(tier_order).fillna(9)
    df = (df.sort_values(["_tier_rank", "Venue"])
            .drop(columns=["_tier_rank"])
            .reset_index(drop=True))

    # Print summary
    ok = df[df["error"].isna() | (df["error"] == "")].shape[0]
    print(f"Competitor scrape {date_str}: {ok}/{len(df)} venues OK")
    for _, row in df.iterrows():
        occ = row.get("Overall Occ %")
        occ_str = f"{float(occ):.1%}" if occ is not None else "N/A"
        icon = "✓" if not row.get("error") else "✗"
        print(f"  {icon} {row['Venue']}: {occ_str} "
              f"({int(row.get('booked_slots', 0))}/{int(row.get('total_slots', 0))} slots)")

    return df


def accumulate_competitors(df_new: pd.DataFrame, df_existing: pd.DataFrame) -> pd.DataFrame:
    """
    Merge new scrape results with existing historical data.
    Replaces today's rows (if any) with fresh data, keeps all history.

    Args:
        df_new:      Today's scrape results (from fetch_all_competitors)
        df_existing: Existing data from Sheets TAB_OUT_COMPETITORS

    Returns:
        Combined DataFrame sorted by date desc, then Venue.
    """
    if df_new.empty:
        return df_existing

    if df_existing.empty:
        return df_new

    # Drop today's rows from existing (will be replaced by df_new)
    today_dates = df_new["date"].unique().tolist()
    df_hist = df_existing[~df_existing["date"].isin(today_dates)].copy()

    # Align columns: use union of both sets
    all_cols = list(dict.fromkeys(list(df_new.columns) + list(df_hist.columns)))
    for col in all_cols:
        if col not in df_new.columns:
            df_new[col] = None
        if col not in df_hist.columns:
            df_hist[col] = None

    combined = pd.concat([df_hist[all_cols], df_new[all_cols]], ignore_index=True)
    combined = combined.sort_values(["date", "Venue"], ascending=[False, True]).reset_index(drop=True)

    return combined
