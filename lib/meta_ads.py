"""
meta_ads.py
Ambil data Facebook/Meta Ads Insights langsung via Meta Graph API.
Tidak perlu Make.com — lebih reliable, tidak ada batas operasi, tidak ada token expire masalah.

Env vars yang dibutuhkan:
  META_ACCESS_TOKEN  — System User token dari Meta Business Manager
  META_AD_ACCOUNT_ID — ID akun iklan (angka saja, tanpa "act_")
"""
import os
import requests
import pandas as pd


META_API_VERSION = "v20.0"
META_GRAPH_URL = f"https://graph.facebook.com/{META_API_VERSION}"

# Action type yang dianggap sebagai "lead"
LEAD_ACTION_TYPES = {
    "lead",
    "offsite_conversion.fb_pixel_lead",
    "offsite_conversion.lead",
    "complete_registration",
    "contact",
}


def _extract_lead_value(actions_list: list) -> float:
    """Ambil nilai lead dari list actions Meta API."""
    if not actions_list:
        return 0.0
    for action in actions_list:
        if action.get("action_type") in LEAD_ACTION_TYPES:
            return float(action.get("value", 0) or 0)
    return 0.0


def _extract_cpl_value(cost_list: list) -> float:
    """Ambil cost per lead dari list cost_per_action_type Meta API."""
    if not cost_list:
        return 0.0
    for item in cost_list:
        if item.get("action_type") in LEAD_ACTION_TYPES:
            return float(item.get("value", 0) or 0)
    return 0.0


def fetch_ads_insights(date_preset: str = "last_90d") -> pd.DataFrame:
    """
    Ambil insights per individual ad per hari dari Meta Ads API.

    Return DataFrame dengan kolom:
      Ad Name, Campaign Name, Date, Spend, Impressions, Reach,
      Clicks, CTR, CPM, Results, Cost_Per_Result

    Level "ad" memberikan breakdown per iklan individual,
    sehingga performa tiap ad bisa dibandingkan.
    """
    access_token = os.environ.get("META_ACCESS_TOKEN")
    ad_account_id = os.environ.get("META_AD_ACCOUNT_ID")

    if not access_token or not ad_account_id:
        raise ValueError(
            "META_ACCESS_TOKEN dan META_AD_ACCOUNT_ID harus diset di Vercel env vars"
        )

    # Hapus prefix "act_" jika ada (untuk keamanan)
    ad_account_id = ad_account_id.replace("act_", "")

    url = f"{META_GRAPH_URL}/act_{ad_account_id}/insights"
    params = {
        "level": "ad",                # per-individual-ad breakdown
        "time_increment": "1",        # daily breakdown
        "date_preset": date_preset,
        "fields": ",".join([
            "ad_name",
            "adset_name",
            "campaign_name",
            "date_start",
            "spend",
            "impressions",
            "reach",
            "clicks",
            "ctr",
            "cpm",
            "actions",
            "cost_per_action_type",
        ]),
        "access_token": access_token,
        "limit": "500",
    }

    rows = []
    next_url = url
    next_params = params

    while next_url:
        resp = requests.get(next_url, params=next_params, timeout=60)

        if resp.status_code != 200:
            error_data = resp.json().get("error", {})
            raise RuntimeError(
                f"Meta API error {resp.status_code}: "
                f"{error_data.get('message', resp.text)}"
            )

        payload = resp.json()

        for item in payload.get("data", []):
            rows.append({
                "Ad Name":         item.get("ad_name", ""),
                "Adset Name":      item.get("adset_name", ""),
                "Campaign Name":   item.get("campaign_name", ""),
                "Date":            item.get("date_start", ""),
                "Spend":           float(item.get("spend", 0) or 0),
                "Impressions":     int(item.get("impressions", 0) or 0),
                "Reach":           int(item.get("reach", 0) or 0),
                "Clicks":          int(item.get("clicks", 0) or 0),
                "CTR":             float(item.get("ctr", 0) or 0),
                "CPM":             float(item.get("cpm", 0) or 0),
                "Results":         _extract_lead_value(item.get("actions", [])),
                "Cost_Per_Result": _extract_cpl_value(item.get("cost_per_action_type", [])),
            })

        # Pagination
        paging = payload.get("paging", {})
        next_url = paging.get("next")
        next_params = {}  # next URL sudah include semua params

    df = pd.DataFrame(rows)
    if not df.empty:
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.sort_values(["Campaign Name", "Ad Name", "Date"]).reset_index(drop=True)

    return df
