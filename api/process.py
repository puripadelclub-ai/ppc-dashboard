"""
api/process.py
Vercel serverless function — entry point untuk Make.com trigger dan Vercel Cron.

Routes:
  POST /api/process   — Dipanggil Make.com Scenario 2 saat file baru di Drive
  GET  /api/fetch-ads — Dipanggil Vercel Cron setiap pagi untuk pull Meta Ads data
  GET  /api/health    — Health check
"""
import sys
import os
import traceback
from flask import Flask, request, jsonify

# Tambah lib ke path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from drive_reader import read_sales_from_drive, read_membership_from_drive, read_occupancy_benchmark
from sheets_client import (
    read_raw_ads, read_actual_leads, read_court_pass, read_raw_coaching,
    read_raw_programs, read_leads_from_monthly_sheet,
    read_tab_as_df, write_df_to_tab, batch_read_tabs,
    TAB_RAW_ADS, TAB_ACTUAL_LEADS,
    TAB_OUT_MEMBERS, TAB_OUT_CAMPAIGNS, TAB_OUT_RFM,
    TAB_OUT_REVENUE, TAB_OUT_PRODUCTS, TAB_OUT_RETENTION, TAB_OUT_SUMMARY,
    TAB_RAW_AVM, TAB_OUT_AVM,
    TAB_OUT_OCCUPANCY, TAB_OUT_COMPETITORS,
    TAB_OUT_PREFERENCES, TAB_OUT_COACHING,
    TAB_OUT_PROGRAMS, TAB_OUT_PRODUCT_DETAIL,
)
from processor import (
    match_members_to_sales,
    calculate_metrics,
    calculate_rfm,
    calculate_retention,
    calculate_revenue,
    calculate_products,
    calculate_campaigns,
    calculate_summary,
    calculate_member_preferences,
    calculate_coaching,
    calculate_program_metrics,
    calculate_product_detail,
)
from meta_ads import fetch_ads_insights
from avm_client import fetch_bookings_range, calculate_avm_summary
from competitor_client import fetch_all_competitors, accumulate_competitors
import math
import pandas as pd
from collections import Counter
from datetime import datetime

app = Flask(__name__)


@app.route("/api/process", methods=["GET", "POST"])
def process():
    """
    Jalankan full pipeline: baca Drive + Sheets → proses → tulis output tabs.
    Dipanggil oleh:
    - Vercel Cron (GET) setiap pagi 07:30 WIB (00:30 UTC)
    - Make.com Scenario 2 (POST) saat file baru di Drive (opsional)
    - Manual trigger via browser (GET) kapanpun dibutuhkan
    """
    try:
        result = run_pipeline()
        return jsonify({"status": "success", "details": result})
    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({"status": "error", "message": str(e), "trace": tb}), 500


@app.route("/api/fetch-ads", methods=["GET", "POST"])
def fetch_ads():
    """
    Pull data Meta Ads per campaign per hari (90 hari terakhir),
    lalu tulis ke tab raw_ads di Google Sheets.

    Dipanggil oleh Vercel Cron setiap pagi 07:00 WIB (00:00 UTC).
    Bisa juga dipanggil manual via GET untuk test.
    """
    try:
        date_preset = request.args.get("date_preset", "last_90d")
        df_ads = fetch_ads_insights(date_preset=date_preset)

        if df_ads.empty:
            return jsonify({
                "status": "warning",
                "message": "Meta API returned no data",
                "rows": 0,
            })

        # Format Date kembali ke string sebelum tulis ke Sheets
        df_to_write = df_ads.copy()
        df_to_write["Date"] = df_to_write["Date"].dt.strftime("%Y-%m-%d")

        write_df_to_tab(df_to_write, TAB_RAW_ADS)

        # ── Supabase sync: ads_daily (per ad per hari) ─────────────────────
        sb_result = {}
        try:
            from supabase_client import (
                upsert_ads_daily, log_start, log_complete,
            )
            log_id = log_start("META_ADS", "fetch_ads_insights",
                               date_start=df_to_write["Date"].min(),
                               date_end=df_to_write["Date"].max())

            # Satu baris per ad per hari — dedup key: ad_name + report_date
            ads_rows = []
            for _, row in df_to_write.iterrows():
                ad_name = str(row.get("Ad Name", "") or "").strip()
                if not ad_name:
                    continue
                ads_rows.append({
                    "ad_name":          ad_name,
                    "adset_name":       str(row.get("Adset Name", "") or ""),
                    "campaign_meta_id": str(row.get("Campaign ID", "") or ""),
                    "campaign_name":    str(row.get("Campaign Name", "") or ""),
                    "report_date":      str(row["Date"]),
                    "spend":            int(float(row.get("Spend", 0) or 0)),
                    "impressions":      int(float(row.get("Impressions", 0) or 0)),
                    "reach":            int(float(row.get("Reach", 0) or 0)),
                    "clicks":           int(float(row.get("Clicks", 0) or 0)),
                    "results":          int(float(row.get("Results", 0) or 0)),
                })

            res_d = upsert_ads_daily(ads_rows) if ads_rows else {"inserted": 0, "error": None}

            log_complete(log_id, "success", {
                "rows_fetched":  len(df_ads),
                "rows_inserted": res_d.get("inserted", 0),
            })
            sb_result = {
                "ads_daily_upserted": res_d.get("inserted", 0),
                "error": res_d.get("error"),
            }
        except Exception as e_sb:
            sb_result = {"supabase_warning": str(e_sb)}

        campaigns = df_ads["Campaign Name"].unique().tolist()
        ads = df_ads["Ad Name"].unique().tolist() if "Ad Name" in df_ads.columns else []
        return jsonify({
            "status":      "success",
            "rows":        len(df_ads),
            "ads":         ads,
            "date_preset": date_preset,
            "supabase":    sb_result,
        })

    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({"status": "error", "message": str(e), "trace": tb}), 500


@app.route("/api/fetch-competitors", methods=["GET", "POST"])
def fetch_competitors():
    """
    Scrape competitor venue occupancy from Ayo.co.id public API.
    Accumulates data in TAB_OUT_COMPETITORS — does NOT overwrite history.

    Runs twice daily via Vercel Cron:
      Morning: 0 22 * * * (22:00 UTC = 05:00 WIB) — full-day pre-booked snapshot
      Evening: 0 10 * * * (10:00 UTC = 17:00 WIB) — smart merge, updates afternoon+evening only

    Query params:
      date:     YYYY-MM-DD (optional, defaults to today Jakarta time)
      run_type: "morning" | "evening" | "auto" (default: auto-detect from WIB hour)
    """
    from datetime import datetime as _dt
    try:
        date_str = request.args.get("date", None)
        run_type = request.args.get("run_type", "auto")

        # Auto-detect morning vs evening from current WIB hour
        if run_type == "auto":
            wib_hour = (_dt.utcnow().hour + 7) % 24
            is_evening_run = wib_hour >= 12
        else:
            is_evening_run = (run_type == "evening")

        run_label = "evening (smart merge)" if is_evening_run else "morning (full snapshot)"

        log = []
        log.append(f"Fetching competitor occupancy via Ayo API ({run_label}){f' for {date_str}' if date_str else ' (today + tomorrow)'}...")

        # Fetch from Ayo public API — today + tomorrow (advance bookings)
        df_new = fetch_all_competitors(date_str=date_str, include_tomorrow=True)
        if df_new.empty:
            return jsonify({"status": "warning", "message": "No competitor data returned", "rows": 0})

        dates_fetched = sorted(df_new["date"].unique())
        log.append(f"  → {len(df_new)} rows scraped for dates: {', '.join(dates_fetched)}")

        # Accumulate with existing history in Sheets
        try:
            df_existing = read_tab_as_df(TAB_OUT_COMPETITORS)
            if not df_existing.empty:
                df_combined = accumulate_competitors(df_new, df_existing, is_evening_run=is_evening_run)
                merge_note = "smart-merged (afternoon+evening updated)" if is_evening_run else "merged"
                log.append(f"  → {merge_note} with {len(df_existing)} existing rows → {len(df_combined)} total")
            else:
                df_combined = df_new
                log.append("  → No existing data, writing fresh")
        except Exception as e_read:
            df_combined = df_new
            log.append(f"  → Could not read existing data ({e_read}), writing fresh")

        # Write back to Sheets
        write_df_to_tab(df_combined, TAB_OUT_COMPETITORS)
        log.append(f"  → Written {len(df_combined)} rows to {TAB_OUT_COMPETITORS}")

        # Build summary per venue
        venues_summary = []
        for _, row in df_new.iterrows():
            occ = row.get("Overall Occ %")
            venues_summary.append({
                "venue":       row["Venue"],
                "area":        row.get("area", ""),
                "overall_occ": round(float(occ), 4) if occ is not None else None,
                "morning_occ": row.get("Morning Occ %"),
                "afternoon_occ": row.get("Afternoon Occ %"),
                "evening_occ": row.get("Evening Occ %"),
                "total_slots": int(row.get("total_slots", 0)),
                "booked_slots": int(row.get("booked_slots", 0)),
                "error":       row.get("error"),
            })

        return jsonify({
            "status":        "success",
            "date":          actual_date,
            "venues_scraped": len(df_new),
            "total_rows":    len(df_combined),
            "venues":        venues_summary,
            "log":           log,
        })

    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({"status": "error", "message": str(e), "trace": tb}), 500


@app.route("/dashboard")
def dashboard():
    """Serve dashboard SPA HTML."""
    html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/dashboard-data", methods=["GET"])
def dashboard_data():
    """
    Return semua data dari out_* Sheets tabs sebagai JSON.
    Dikonsumsi oleh dashboard.html via fetch() saat page load.
    """
    try:
        def sf(v):
            """Safe float — handles empty string, NaN, Inf."""
            try:
                f = float(v)
                return 0.0 if (math.isnan(f) or math.isinf(f)) else f
            except Exception:
                return 0.0

        def si(v):
            try:
                return int(float(v))
            except Exception:
                return 0

        # ── Single batch read — 1 API call untuk semua tabs ──────────
        # Tab-tab core yang PASTI ada — dibaca dalam 1 batch call
        DASHBOARD_TABS = [
            TAB_OUT_SUMMARY, TAB_OUT_MEMBERS, TAB_OUT_RFM,
            TAB_OUT_CAMPAIGNS, TAB_OUT_REVENUE, TAB_OUT_PRODUCTS,
            TAB_OUT_RETENTION, TAB_OUT_AVM, TAB_OUT_OCCUPANCY, TAB_OUT_COMPETITORS,
            TAB_RAW_ADS, TAB_ACTUAL_LEADS, TAB_RAW_AVM,
            TAB_OUT_PREFERENCES, TAB_OUT_COACHING,
        ]
        tabs = batch_read_tabs(DASHBOARD_TABS)

        def rows(tab):
            df = tabs.get(tab, pd.DataFrame())
            return df.to_dict(orient="records") if not df.empty else []

        def safe_rows(tab_name):
            """Baca tab opsional yang mungkin belum ada — tidak ikut batch."""
            try:
                df = read_tab_as_df(tab_name)
                return df.fillna("").to_dict(orient="records") if not df.empty else []
            except Exception:
                return []

        summary_rows   = rows(TAB_OUT_SUMMARY)
        members_rows   = rows(TAB_OUT_MEMBERS)
        rfm_rows       = rows(TAB_OUT_RFM)
        campaigns_rows = rows(TAB_OUT_CAMPAIGNS)
        revenue_rows   = rows(TAB_OUT_REVENUE)
        products_rows  = rows(TAB_OUT_PRODUCTS)
        retention_rows = rows(TAB_OUT_RETENTION)
        avm_rows          = rows(TAB_OUT_AVM)
        occupancy_rows    = rows(TAB_OUT_OCCUPANCY)
        competitors_rows  = rows(TAB_OUT_COMPETITORS)
        raw_ads_rows      = rows(TAB_RAW_ADS)
        # Baca actual_leads dari WA Manual Data sheet (primary) atau fallback ke Hub Sheet tab
        try:
            _leads_df = read_leads_from_monthly_sheet()
            actual_leads_rows = _leads_df.to_dict("records") if not _leads_df.empty else rows(TAB_ACTUAL_LEADS)
        except Exception:
            actual_leads_rows = rows(TAB_ACTUAL_LEADS)
        raw_avm_rows      = rows(TAB_RAW_AVM)
        preferences_rows  = rows(TAB_OUT_PREFERENCES)
        coaching_rows     = rows(TAB_OUT_COACHING)
        # Tab opsional — dibaca terpisah agar tidak merusak batch jika belum ada
        programs_rows      = safe_rows(TAB_OUT_PROGRAMS)
        product_detail_rows= safe_rows(TAB_OUT_PRODUCT_DETAIL)

        summary = summary_rows[0] if summary_rows else {}

        # Enrich members with RFM segment + recommended action
        rfm_lookup = {r.get("Member Name", ""): r for r in rfm_rows}
        for m in members_rows:
            rfm = rfm_lookup.get(m.get("Member Name", ""), {})
            m["RFM_Segment"]        = rfm.get("RFM_Segment", "")
            m["Recommended_Action"] = rfm.get("Recommended_Action", "")

        # RFM segment counts — sorted descending
        seg_counter = Counter(r.get("RFM_Segment", "Lainnya") for r in rfm_rows)
        seg_order = ["Champions", "Loyal", "New", "Needs Attention",
                     "At Risk", "Cannot Lose", "Lost"]
        rfm_segments = []
        for seg in seg_order:
            if seg in seg_counter:
                rfm_segments.append({"segment": seg, "count": seg_counter[seg]})
        for seg, cnt in seg_counter.items():
            if seg not in seg_order:
                rfm_segments.append({"segment": seg, "count": cnt})

        # Latest retention rate from out_retention
        latest_retention = sf(retention_rows[-1].get("Retention_Rate", 0)) if retention_rows else 0.0

        # Average CLV moderate from members
        clv_vals = [sf(m.get("CLV_Moderate_6mo", 0)) for m in members_rows
                    if sf(m.get("CLV_Moderate_6mo", 0)) > 0]
        avg_clv = round(sum(clv_vals) / len(clv_vals)) if clv_vals else 0

        return jsonify({
            "meta": {
                "last_updated":  str(summary.get("Last_Updated", "")),
                "generated_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
            },
            "summary": {
                "total_members":       si(summary.get("Total_Members", 0)),
                "matched":             si(summary.get("Matched_to_POS", 0)),
                "revenue_this_month":  si(summary.get("Revenue_This_Month", 0)),
                "revenue_last_month":  si(summary.get("Revenue_Last_Month", 0)),
                "revenue_mom_pct":     sf(summary.get("Revenue_MoM_Pct", 0)),
                "total_ads_budget":    si(summary.get("Total_Ads_Budget", 0)),
                "overall_roas":        sf(summary.get("Overall_ROAS", 0)),
                "ads_members":         si(summary.get("ADS_Campaign_Members", 0)),
                "total_spending":                si(summary.get("Total_Spending", 0)),
                "total_transactions_this_month": si(summary.get("Total_Transactions_This_Month", 0)),
                "retention_rate":               latest_retention,
                "avg_clv_moderate":    avg_clv,
            },
            "revenue":      revenue_rows,
            "campaigns":    campaigns_rows,
            "members":      members_rows,
            "rfm":          rfm_rows,
            "rfm_segments": rfm_segments,
            "retention":    retention_rows,
            "products":     products_rows,
            "avm":          avm_rows,
            "occupancy":    occupancy_rows,
            "competitors":  competitors_rows,
            "raw_ads":      raw_ads_rows,
            "actual_leads": actual_leads_rows,
            "raw_avm":      raw_avm_rows,
            "preferences":  preferences_rows,
            "coaching":        coaching_rows,
            "programs":        programs_rows,
            "product_detail":  product_detail_rows,
        })

    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({"status": "error", "message": str(e), "trace": tb}), 500


@app.route("/api/copilot-data", methods=["GET"])
def copilot_data():
    """
    Rule-based AI Copilot — generate member tags, behavior insights,
    dan budget recommendations tanpa API AI eksternal.
    Membaca dari raw_avm + raw_ads + out_rfm + out_summary di Sheets.
    """
    try:
        import math as _math
        tabs = batch_read_tabs([TAB_RAW_AVM, TAB_RAW_ADS, TAB_OUT_RFM, TAB_OUT_SUMMARY, TAB_ACTUAL_LEADS])

        def _sf(v):
            try:
                f = float(v); return 0.0 if (_math.isnan(f) or _math.isinf(f)) else f
            except Exception: return 0.0

        df_raw_avm  = tabs.get(TAB_RAW_AVM,  pd.DataFrame())
        df_raw_ads  = tabs.get(TAB_RAW_ADS,  pd.DataFrame())
        df_rfm      = tabs.get(TAB_OUT_RFM,  pd.DataFrame())
        df_actual   = tabs.get(TAB_ACTUAL_LEADS, pd.DataFrame())
        summary_tab = tabs.get(TAB_OUT_SUMMARY, pd.DataFrame())
        summary     = summary_tab.to_dict(orient="records")[0] if not summary_tab.empty else {}

        # ── 1. MEMBER TAGS dari raw_avm ───────────────────────
        member_tags = {}
        if not df_raw_avm.empty:
            df_raw_avm["date"]        = pd.to_datetime(df_raw_avm["date"], errors="coerce")
            df_raw_avm["total_price"] = pd.to_numeric(df_raw_avm["total_price"], errors="coerce").fillna(0)
            cutoff_14d = pd.Timestamp.today() - pd.Timedelta(days=14)
            cutoff_30d = pd.Timestamp.today() - pd.Timedelta(days=30)

            for name, grp in df_raw_avm.groupby("customer_name"):
                if not name or str(name).strip() in ("", "nan"): continue
                tags = []
                grp14 = grp[grp["date"] >= cutoff_14d]
                grp30 = grp[grp["date"] >= cutoff_30d]

                # Ball Machine User
                bm_codes = {"BP1", "BP2", "BP"}
                bm_14 = grp14[grp14["booking_code"].isin(bm_codes)]
                if len(bm_14) >= 2: tags.append("Ball Machine User")

                # Coaching Interest
                if grp30[grp30["booking_code"] == "CBA"].shape[0] >= 1:
                    tags.append("Coaching User")

                # Time preference
                if len(grp30) > 0:
                    periods = grp30["period"].value_counts()
                    if len(periods) > 0:
                        top_period = periods.index[0]
                        pct = periods.iloc[0] / len(grp30)
                        if pct >= 0.6:
                            tags.append(f"{top_period} Regular")

                # Frequency
                visits_30 = grp30[grp30["booking_code"] == "REGULAR"].shape[0]
                if visits_30 >= 8:    tags.append("Power User")
                elif visits_30 >= 4:  tags.append("Active User")
                elif visits_30 == 0 and len(grp) > 0:
                    # Check if previously active
                    days_since = (pd.Timestamp.today() - grp["date"].max()).days
                    if days_since > 30: tags.append("Inactive")

                # Court preference
                if "court" in grp30.columns and len(grp30) >= 3:
                    courts = grp30["court"].value_counts()
                    if len(courts) > 0 and courts.iloc[0] / len(grp30) >= 0.7:
                        tags.append(f"Prefers {courts.index[0]}")

                if tags:
                    member_tags[str(name).strip()] = tags

        # ── 2. OFFER OPPORTUNITIES ────────────────────────────
        # Ball Machine User → tawarkan ball machine jika slot tersedia
        offers = []
        bm_users = [n for n, t in member_tags.items() if "Ball Machine User" in t]
        inactive_members = [n for n, t in member_tags.items() if "Inactive" in t]
        coaching_users = [n for n, t in member_tags.items() if "Coaching User" in t]

        if bm_users:
            offers.append({
                "type": "Ball Machine Promo",
                "target_count": len(bm_users),
                "message": f"{len(bm_users)} member booking ball machine ≥2x dalam 14 hari. Tawarkan diskon ball machine atau paket BM bulanan.",
                "action": "Kirim WA: 'Hai [nama], udah lama nih ga main ball machine. Ada slot spesial buat kamu bulan ini!'",
                "priority": "HIGH",
            })
        if inactive_members:
            offers.append({
                "type": "Win-back Campaign",
                "target_count": len(inactive_members),
                "message": f"{len(inactive_members)} member tidak aktif >30 hari. Cocok untuk win-back WA.",
                "action": "Kirim WA: 'Kami kangen kamu di lapangan! Ada diskon 20% untuk booking berikutnya.'",
                "priority": "HIGH",
            })
        if coaching_users:
            offers.append({
                "type": "Coaching Upsell",
                "target_count": len(coaching_users),
                "message": f"{len(coaching_users)} member sudah pernah coaching. Tawarkan paket coaching lanjutan.",
                "action": "Hubungi personal: 'Sudah merasakan manfaat coaching? Coba paket 5 sesi dengan harga spesial.'",
                "priority": "MEDIUM",
            })

        # ── 3. ADS BUDGET RECOMMENDATIONS dari raw_ads ────────
        ads_recs = []
        if not df_raw_ads.empty:
            df_raw_ads.columns = [c.strip() for c in df_raw_ads.columns]
            spend_col = next((c for c in df_raw_ads.columns if c.lower() in ("spend","amount spent")), None)
            res_col   = next((c for c in df_raw_ads.columns if c.lower() in ("results",)), None)
            ad_col    = next((c for c in df_raw_ads.columns if c.lower() == "ad name"), None)
            date_col  = next((c for c in df_raw_ads.columns if c.lower() == "date"), None)

            if spend_col and ad_col and date_col:
                df_raw_ads[date_col]  = pd.to_datetime(df_raw_ads[date_col], errors="coerce")
                df_raw_ads[spend_col] = pd.to_numeric(df_raw_ads[spend_col], errors="coerce").fillna(0)
                if res_col:
                    df_raw_ads[res_col] = pd.to_numeric(df_raw_ads[res_col], errors="coerce").fillna(0)

                today_dt  = pd.Timestamp.today().normalize()
                cutoff_w  = today_dt - pd.Timedelta(days=7)   # this week
                cutoff_w2 = today_dt - pd.Timedelta(days=14)  # last week

                for ad_name, grp in df_raw_ads.groupby(ad_col):
                    if not ad_name: continue
                    this_week = grp[grp[date_col] >= cutoff_w]
                    last_week = grp[(grp[date_col] >= cutoff_w2) & (grp[date_col] < cutoff_w)]

                    spend_w  = this_week[spend_col].sum()
                    spend_pw = last_week[spend_col].sum()
                    res_w    = this_week[res_col].sum() if res_col else 0
                    res_pw   = last_week[res_col].sum() if res_col else 0
                    cpr_w    = spend_w / res_w   if res_w  > 0 else 0
                    cpr_pw   = spend_pw / res_pw if res_pw > 0 else 0

                    action, reason, priority = None, None, "MEDIUM"

                    if spend_w == 0 and spend_pw > 0:
                        action   = "Tidak aktif minggu ini"
                        reason   = f"Spent Rp0 vs Rp{int(spend_pw):,} minggu lalu. Kill atau review."
                        priority = "HIGH"
                    elif spend_w > 0 and res_w == 0:
                        action   = "0 results — pertimbangkan pause"
                        reason   = f"Sudah spent Rp{int(spend_w):,} minggu ini tanpa result."
                        priority = "HIGH"
                    elif cpr_w > 0 and cpr_pw > 0 and cpr_w > cpr_pw * 1.4:
                        action   = f"Turunkan budget 30–40%"
                        reason   = f"CPR naik {int((cpr_w/cpr_pw-1)*100)}%: Rp{int(cpr_pw):,} → Rp{int(cpr_w):,}"
                        priority = "HIGH"
                    elif res_w >= 3 and (cpr_pw == 0 or cpr_w <= cpr_pw * 0.85):
                        action   = "Scale up 20–30%"
                        reason   = f"{int(res_w)} results, CPR efisien Rp{int(cpr_w):,}"
                        priority = "MEDIUM"

                    if action:
                        ads_recs.append({
                            "ad_name":  str(ad_name)[:50],
                            "action":   action,
                            "reason":   reason,
                            "priority": priority,
                            "spend_w":  int(spend_w),
                            "res_w":    int(res_w),
                            "cpr_w":    int(cpr_w),
                        })

                ads_recs.sort(key=lambda x: (0 if x["priority"]=="HIGH" else 1, -x["spend_w"]))

        # ── 4. MEMBER TAG SUMMARY ─────────────────────────────
        tag_counts = {}
        for tags in member_tags.values():
            for t in tags:
                tag_counts[t] = tag_counts.get(t, 0) + 1

        return jsonify({
            "member_tags":  member_tags,
            "tag_summary":  sorted(tag_counts.items(), key=lambda x: -x[1]),
            "offers":       offers,
            "ads_recs":     ads_recs[:10],
            "summary": {
                "total_tagged":    len(member_tags),
                "bm_users":        len(bm_users),
                "inactive":        len(inactive_members),
                "coaching_users":  len(coaching_users),
                "high_priority_ads": sum(1 for r in ads_recs if r["priority"]=="HIGH"),
            },
        })

    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({"status": "error", "message": str(e), "trace": tb}), 500


@app.route("/api/ai-chat", methods=["POST"])
def ai_chat():
    """
    Proxy ke Anthropic API — digunakan oleh AI Copilot di dashboard.
    Membutuhkan ANTHROPIC_API_KEY di environment variables.
    """
    try:
        import requests as req_lib

        # Support Gemini (gratis) atau Anthropic
        gemini_key = os.environ.get("GEMINI_API_KEY")
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

        if not gemini_key and not anthropic_key:
            return jsonify({"error": "ANTHROPIC_API_KEY tidak dikonfigurasi di Vercel"}), 400

        body = request.get_json() or {}
        user_message = body.get("message", "")
        context = body.get("context", {})
        history = body.get("history", [])

        # Ping check — cukup balas OK
        if user_message == "ping":
            return jsonify({"response": "ok"})

        rfm_segs = context.get("rfm_segments", [])
        rfm_summary = ", ".join(
            f"{r.get('segment','?')} ({r.get('count',0)})"
            for r in rfm_segs[:7]
        ) if rfm_segs else "—"

        system_prompt = f"""Kamu adalah Business Consultant AI untuk Puri Padel Club (PPC), premium private padel club di Jakarta Barat. Tagline: "Better Every Time".

Tujuan bisnis: meningkatkan occupancy, membership, coaching revenue, tournament participation, dan sponsorship revenue.
Brand: Premium, Friendly, Community-driven, Professional.

DATA BISNIS REAL-TIME HARI INI:
- Revenue bulan ini: Rp{int(context.get('revenue_this_month', 0)):,} ({context.get('revenue_mom_pct', 0):+.1f}% vs bulan lalu)
- Total member: {context.get('total_members', 0)} ({context.get('matched', 0)} matched POS)
- Retention rate: {context.get('retention_rate', 0):.1f}%
- Avg CLV (6 bulan): Rp{int(context.get('avg_clv', 0)):,}
- Meta Ads spend: Rp{int(context.get('total_ads_budget', 0)):,} | ROAS: {context.get('overall_roas', 0):.2f}x
- Occupancy hari ini: {context.get('occupancy', 'N/A')}
- Posisi pasar: #{context.get('market_rank', '?')} dari {context.get('total_venues', 36)} venue di Jakarta
- RFM member breakdown: {rfm_summary}

INSTRUKSI:
- Berikan analisis, rekomendasi, dan action plan yang spesifik dan actionable
- Gunakan data di atas sebagai basis analisis — jangan generik
- Jawab dalam Bahasa Indonesia yang profesional namun friendly
- Maksimum 3-4 paragraf atau bullet points — concise, no fluff"""

        # Build messages including history
        messages = []
        for h in history[:-1]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": user_message})

        # ── Gemini (gratis) ──────────────────────────
        if gemini_key:
            # Gabungkan system prompt ke pesan pertama user
            full_first = f"{system_prompt}\n\n---\n{messages[0]['content']}" if messages else system_prompt
            gemini_contents = []
            for i, m in enumerate(messages):
                role = "user" if m["role"] == "user" else "model"
                text = full_first if i == 0 else m["content"]
                gemini_contents.append({"role": role, "parts": [{"text": text}]})

            response = req_lib.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={gemini_key}",
                headers={"content-type": "application/json"},
                json={"contents": gemini_contents, "generationConfig": {"maxOutputTokens": 1024}},
                timeout=30,
            )
            if response.status_code != 200:
                return jsonify({"error": f"Gemini API error {response.status_code}: {response.text[:200]}"}), 500
            result = response.json()
            reply = result["candidates"][0]["content"]["parts"][0]["text"]

        # ── Anthropic (berbayar, fallback) ───────────
        else:
            response = req_lib.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1024,
                    "system": system_prompt,
                    "messages": messages,
                },
                timeout=30,
            )
            if response.status_code != 200:
                return jsonify({"error": f"Anthropic API error {response.status_code}: {response.text[:200]}"}), 500
            result = response.json()
            reply = result["content"][0]["text"]

        return jsonify({"response": reply})

    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({"error": str(e), "trace": tb}), 500


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


def run_pipeline():
    """
    Main pipeline:
    1. Baca data dari Drive + Sheets
    2. Proses semua analisis
    3. Tulis hasil ke output Sheets
    """
    log = []

    # ── 1. BACA DATA ──────────────────────────────────
    log.append("Reading Sales from Drive...")
    df_sales, sales_filename = read_sales_from_drive()
    log.append(f"  → {sales_filename}: {len(df_sales)} rows")

    log.append("Reading Membership from Drive...")
    df_mem = read_membership_from_drive()
    log.append(f"  → {len(df_mem)} members")

    log.append("Reading Meta Ads from Sheets (raw_ads tab)...")
    df_ads = read_raw_ads()
    log.append(f"  → {len(df_ads)} ad rows")

    log.append("Reading actual leads & conversions...")
    df_leads = read_actual_leads()
    log.append(f"  → {len(df_leads)} lead rows")

    # ── 2. PROSES ──────────────────────────────────────
    log.append("Matching members to POS sales...")
    fd = match_members_to_sales(df_mem, df_sales)
    log.append(f"  → {fd['Matched'].sum()} / {len(fd)} members matched")

    log.append("Calculating metrics (CLV, spending)...")
    fd = calculate_metrics(fd)

    log.append("Calculating RFM scores...")
    fd_rfm = calculate_rfm(fd)

    log.append("Calculating retention cohorts...")
    df_retention = calculate_retention(df_sales)

    log.append("Calculating revenue & products...")
    rev_monthly, rev_cat, peak_day = calculate_revenue(df_sales)
    df_products = calculate_products(df_sales)

    log.append("Calculating campaign performance...")
    df_campaigns = calculate_campaigns(fd, df_ads, df_leads)

    log.append("Calculating summary KPIs...")
    df_summary = calculate_summary(fd, rev_monthly, df_ads, df_sales=df_sales)

    # ── 3. TULIS KE SHEETS ─────────────────────────────
    log.append("Writing to Google Sheets...")

    member_cols = [
        "Member Name", "Kode ads", "Source Type", "Join Date",
        "Matched", "total_spending", "total_bills", "avg_spend_per_visit",
        "monthly_spend", "first_visit", "last_visit", "days_since_last",
        "CLV_Conservative", "CLV_Moderate_6mo", "CLV_Optimistic_12mo",
    ]
    member_cols = [c for c in member_cols if c in fd.columns]
    write_df_to_tab(fd[member_cols].sort_values("total_spending", ascending=False), TAB_OUT_MEMBERS)

    if not fd_rfm.empty:
        rfm_cols = [
            "Member Name", "Source Type", "RFM_Score", "RFM_Segment",
            "R_score", "F_score", "M_score",
            "days_since_last", "total_bills", "total_spending",
            "Recommended_Action",
        ]
        rfm_cols = [c for c in rfm_cols if c in fd_rfm.columns]
        write_df_to_tab(fd_rfm[rfm_cols].sort_values("RFM_Segment"), TAB_OUT_RFM)

    write_df_to_tab(df_retention, TAB_OUT_RETENTION)
    write_df_to_tab(rev_monthly, TAB_OUT_REVENUE)
    write_df_to_tab(df_products, TAB_OUT_PRODUCTS)

    if not df_campaigns.empty:
        write_df_to_tab(df_campaigns, TAB_OUT_CAMPAIGNS)

    write_df_to_tab(df_summary, TAB_OUT_SUMMARY)

    # ── 4. AVM BOOKING DATA ────────────────────────────
    # Fetch 60 hari ke belakang secara parallel, lalu akumulasi dengan data historis
    # sehingga data lama tidak hilang tiap kali pipeline dijalankan.
    df_avm_raw = None  # will be set if fetch succeeds (needed for preferences)
    log.append("Fetching AVM booking data (last 60 days, parallel + accumulate)...")
    try:
        df_avm_new = fetch_bookings_range(days_back=60, days_forward=7)
        if not df_avm_new.empty:
            # Baca existing raw_avm untuk pertahankan data sebelum window fetch
            try:
                df_existing = read_tab_as_df(TAB_RAW_AVM)
                if not df_existing.empty and "date" in df_existing.columns:
                    df_existing["date"] = pd.to_datetime(df_existing["date"], errors="coerce")
                    df_existing["total_price"] = pd.to_numeric(
                        df_existing.get("total_price", 0), errors="coerce"
                    ).fillna(0)
                    df_existing = df_existing.dropna(subset=["date"])
                    # Simpan baris historis yang lebih tua dari window fetch baru
                    new_start = df_avm_new["date"].min()
                    df_history = df_existing[df_existing["date"] < new_start].copy()
                    df_avm_raw = pd.concat([df_history, df_avm_new], ignore_index=True)
                    log.append(
                        f"  → {len(df_history)} historical + {len(df_avm_new)} new = "
                        f"{len(df_avm_raw)} total rows"
                    )
                else:
                    df_avm_raw = df_avm_new
                    log.append(f"  → {len(df_avm_raw)} AVM rows (fresh start)")
            except Exception as e_acc:
                df_avm_raw = df_avm_new
                log.append(f"  ℹ️ Accumulation skipped ({e_acc}), using new data only")

            # Tulis raw ke Sheets (date sebagai string)
            df_avm_export = df_avm_raw.copy()  # df_avm_raw now set for preferences step
            df_avm_export["date"] = df_avm_export["date"].dt.strftime("%Y-%m-%d")
            write_df_to_tab(df_avm_export, TAB_RAW_AVM)

            # Hitung & tulis ringkasan harian
            df_avm_summary = calculate_avm_summary(df_avm_raw)
            if not df_avm_summary.empty:
                write_df_to_tab(df_avm_summary, TAB_OUT_AVM)
                log.append(f"  → {len(df_avm_summary)} days of AVM summary")

            # ── Supabase sync: bookings + daily_summaries ──────────────────
            try:
                from supabase_client import (
                    upsert_bookings, upsert_daily_summary,
                    log_start, log_complete,
                )
                sb_log_id = log_start(
                    "AVM", "sync_bookings",
                    date_start=df_avm_export["date"].min(),
                    date_end=df_avm_export["date"].max(),
                )
                # Upsert raw bookings (dedup by avm_id)
                import hashlib as _hl
                seen_ids = {}  # dedup dict: avm_id → row (last-write-wins)
                for _, r in df_avm_export.iterrows():
                    avm_id = str(r.get("avm_id", "") or "").strip()
                    # Jika AVM tidak return id, buat synthetic key dari composite fields
                    # Sertakan end_time agar slot berbeda durasi tidak bentrok
                    if not avm_id or avm_id in ("0", "nan"):
                        raw_key = (
                            f"{r.get('date','')}|{r.get('court','')}|"
                            f"{r.get('start_time','')}|{r.get('end_time','')}|"
                            f"{r.get('customer_name','')}"
                        )
                        avm_id = "syn_" + _hl.md5(raw_key.encode()).hexdigest()[:16]
                    # Map court name → court_id (Court 1=1, Court 2=2)
                    court_name = str(r.get("court", ""))
                    court_id = 1 if "1" in court_name else (2 if "2" in court_name else None)
                    seen_ids[avm_id] = {
                        "avm_id":           avm_id,
                        "booking_date":     str(r["date"]),
                        "court_id":         court_id,
                        "court_name":       court_name,
                        "start_time":       str(r.get("start_time", "") or ""),
                        "end_time":         str(r.get("end_time", "") or ""),
                        "period":           str(r.get("period", "") or ""),
                        "booking_code":     str(r.get("booking_code", "") or ""),
                        "booking_type":     str(r.get("booking_type", "") or ""),
                        "customer_name":    str(r.get("customer_name", "") or ""),
                        "gross_amount":     int(float(r.get("total_price", 0) or 0)),
                        "payment_method":   str(r.get("payment_method", "") or ""),
                        "reservation_type": str(r.get("reservation_type", "") or ""),
                        "final_status":     str(r.get("final_status", "") or ""),
                    }
                booking_rows = list(seen_ids.values())
                res_b = upsert_bookings(booking_rows) if booking_rows else {"inserted": 0, "error": None}

                # Upsert daily_summaries from avm_summary
                ds_inserted = 0
                for _, s in df_avm_summary.iterrows():
                    row = {
                        "summary_date":          str(s["date"]),
                        "total_bookings":         int(s.get("total_bookings", 0)),
                        "regular_bookings":        int(s.get("regular_bookings", 0)),
                        "machine_bookings":        int(s.get("machine_bookings", 0)),
                        "coaching_bookings":       int(s.get("coaching_bookings", 0)),
                        "complimentary_bookings":  int(s.get("complimentary_bookings", 0)),
                        "booked_hours":            float(s.get("Booked Hrs", 0)),
                        "total_hours":             int(s.get("Total Hrs", 32)),
                        "occupancy_pct":           float(s.get("Overall Occ %", 0)),
                        "morning_occ_pct":         float(s.get("Morning Occ %", 0)),
                        "afternoon_occ_pct":       float(s.get("Afternoon Occ %", 0)),
                        "evening_occ_pct":         float(s.get("Evening Occ %", 0)),
                    }
                    res = upsert_daily_summary(row)
                    if res.get("inserted"):
                        ds_inserted += 1

                log_complete(sb_log_id, "success", {
                    "rows_fetched":  len(booking_rows),
                    "rows_inserted": res_b.get("inserted", 0),
                })
                b_err = res_b.get("error")
                log.append(
                    f"  → Supabase: {res_b.get('inserted', 0)} bookings "
                    f"({'ERR: ' + str(b_err)[:120] if b_err else 'ok'}), "
                    f"{ds_inserted} daily_summaries upserted | "
                    f"booking_rows={len(booking_rows)}"
                )
            except Exception as e_sb:
                log.append(f"  ℹ️ Supabase AVM sync (non-fatal): {e_sb}")
        else:
            log.append("  → No AVM data returned (check AVM_MOBILE_TOKEN)")
    except ValueError as e:
        log.append(f"  ⚠️ AVM skipped: {e}")
    except Exception as e:
        log.append(f"  ⚠️ AVM error: {e}")

    # ── 5. OCCUPANCY BENCHMARK ─────────────────────────
    # Primary: Ayo.co.id public API scraper (fetch-competitors cron already ran at 07:00)
    # Fallback: Google Drive benchmark files (manual upload)
    log.append("Checking competitor occupancy data...")
    try:
        df_existing_comp = read_tab_as_df(TAB_OUT_COMPETITORS)
        if not df_existing_comp.empty:
            log.append(f"  → {len(df_existing_comp)} competitor rows already in Sheets (from Ayo scraper)")
        else:
            # Sheets is empty — try Drive fallback
            log.append("  → No Ayo data yet, trying Drive benchmark fallback...")
            try:
                df_comp_drive, df_ppc_trend, df_all_history = read_occupancy_benchmark(trend_days=14)
                write_target = df_all_history if not df_all_history.empty else df_comp_drive
                if not write_target.empty:
                    write_df_to_tab(write_target, TAB_OUT_COMPETITORS)
                    n_dates = write_target["date"].nunique() if "date" in write_target.columns else 1
                    log.append(f"  → Drive fallback: {len(write_target)} venue-day rows ({n_dates} dates)")
                if not df_ppc_trend.empty:
                    write_df_to_tab(df_ppc_trend, TAB_OUT_OCCUPANCY)
                    log.append(f"  → {len(df_ppc_trend)} days PPC occupancy trend (from Drive)")
            except Exception as e_drive:
                log.append(f"  ⚠️ Drive benchmark also failed: {e_drive}")
    except Exception as e:
        log.append(f"  ⚠️ Competitor data check error: {e}")

    # ── 6. COACHING ANALYTICS ──────────────────────────
    log.append("Reading coaching data from PPC Coaching Log sheet...")
    try:
        df_coaching = read_raw_coaching()
        log.append(f"  → {len(df_coaching)} coaching sessions loaded")

        if not df_coaching.empty:
            coaching_result = calculate_coaching(df_coaching)

            # Tulis output coaching ke Hub sebagai flat rows untuk dashboard
            # Gabungkan monthly + by_package + summary menjadi satu tab
            coaching_rows_out = []

            # Monthly summary
            if not coaching_result["monthly"].empty:
                for _, r in coaching_result["monthly"].iterrows():
                    coaching_rows_out.append({
                        "Type": "Monthly",
                        "Period": str(r.get("YearMonth", "")),
                        "Total_Sessions": int(r.get("Total_Sessions", 0)),
                        "Unique_Members": int(r.get("Unique_Members", 0)),
                        "Free_Sessions": int(r.get("Free_Coaching", 0)),
                        "Paid_Sessions": int(r.get("Paid_Sessions", 0)),
                        "Kids_Sessions": int(r.get("Kids_Sessions", 0)),
                        "Paid_Rate_Pct": float(r.get("Paid_Rate_Pct", 0)),
                        "Total_Participants": int(r.get("Total_Participants", 0)),
                        "Package_Type": "",
                        "Sessions_Remaining": "",
                        "Last_Session": "",
                    })

            # Active bundles (sisa sesi)
            if not coaching_result["active_bundles"].empty:
                for _, r in coaching_result["active_bundles"].iterrows():
                    coaching_rows_out.append({
                        "Type": "ActiveBundle",
                        "Period": "",
                        "Total_Sessions": 0,
                        "Unique_Members": 0,
                        "Free_Sessions": 0,
                        "Paid_Sessions": 0,
                        "Kids_Sessions": 0,
                        "Paid_Rate_Pct": 0,
                        "Total_Participants": 0,
                        "Package_Type": str(r.get("Package_Type", "")),
                        "Sessions_Remaining": str(r.get("Sessions_Remaining", "")),
                        "Last_Session": str(r.get("Last_Session_Date", "")),
                        "Member_Name": str(r.get("Member_Name", "")),
                    })

            # Summary KPIs sebagai satu baris
            s = coaching_result["summary"]
            coaching_rows_out.append({
                "Type": "Summary",
                "Period": s.get("Last_Updated", ""),
                "Total_Sessions": s.get("Total_Sessions_All", 0),
                "Unique_Members": s.get("Unique_Members_30d", 0),
                "Free_Sessions": s.get("Free_Sessions_30d", 0),
                "Paid_Sessions": s.get("Paid_Sessions_30d", 0),
                "Kids_Sessions": s.get("Kids_Sessions_30d", 0),
                "Paid_Rate_Pct": s.get("Conversion_Rate_Pct", 0),
                "Total_Participants": 0,
                "Package_Type": "",
                "Sessions_Remaining": str(s.get("Active_Bundles", 0)),
                "Last_Session": "",
            })

            if coaching_rows_out:
                df_coaching_out = pd.DataFrame(coaching_rows_out)
                write_df_to_tab(df_coaching_out, TAB_OUT_COACHING)
                log.append(
                    f"  → Coaching analytics written: {len(coaching_result['monthly'])} months, "
                    f"{len(coaching_result['active_bundles'])} active bundles"
                )
                log.append(
                    f"  → Conversion rate: {s.get('Conversion_Rate_Pct', 0)}% "
                    f"(free→paid dari {coaching_result['conversion'].get('total_free_members', 0)} member)"
                )
        else:
            log.append("  → Coaching sheet kosong atau belum diisi")
    except Exception as e:
        log.append(f"  ⚠️ Coaching error: {e}")

    # ── 7. PRODUCT DETAIL BREAKDOWN (dari ESB) ────────
    log.append("Calculating product detail breakdown from ESB...")
    try:
        df_prod_detail = calculate_product_detail(df_sales)
        if not df_prod_detail.empty:
            write_df_to_tab(df_prod_detail, TAB_OUT_PRODUCT_DETAIL)
            top = df_prod_detail.iloc[0]
            log.append(
                f"  → {len(df_prod_detail)} produk dari "
                f"{df_prod_detail['Menu_Category'].nunique()} kategori"
            )
            log.append(f"  → Top produk: {top['Menu']} (Rp{int(top['Total_Revenue']):,})")
        else:
            log.append("  → Kolom Menu tidak ditemukan di ESB data")
    except Exception as e:
        log.append(f"  ⚠️ Product detail error: {e}")

    # ── 8. PROGRAM PERFORMANCE TRACKER (legacy) ───────
    log.append("Calculating program performance metrics...")
    try:
        df_programs = read_raw_programs()
        if not df_programs.empty:
            # Siapkan AVM df (gunakan df_avm_raw yang sudah diambil di step AVM)
            df_avm_for_prog = None
            if df_avm_raw is not None and not df_avm_raw.empty:
                df_avm_for_prog = df_avm_raw.copy()

            df_prog_out = calculate_program_metrics(
                df_programs,
                df_avm=df_avm_for_prog,
                df_sales=df_sales,
            )
            if not df_prog_out.empty:
                write_df_to_tab(df_prog_out, TAB_OUT_PROGRAMS)
                log.append(f"  → {len(df_prog_out)} programs calculated")
                # Log top performer by ROI
                best = df_prog_out.loc[df_prog_out["ROI"].idxmax()] if "ROI" in df_prog_out.columns and df_prog_out["ROI"].notna().any() else None
                if best is not None:
                    log.append(f"  → Top ROI: {best['Program_Name']} ({best['ROI']}x)")
            else:
                log.append("  → No program data calculated")
        else:
            log.append("  → raw_programs tab kosong — tambahkan data program di Hub Sheet")
    except Exception as e:
        log.append(f"  ⚠️ Program metrics error: {e}")

    # ── 8. MEMBER PRODUCT PREFERENCES ─────────────────
    log.append("Calculating member product preferences...")
    try:
        df_prefs = calculate_member_preferences(df_sales, df_avm_raw)
        if not df_prefs.empty:
            write_df_to_tab(df_prefs, TAB_OUT_PREFERENCES)
            log.append(f"  → {len(df_prefs)} member preference profiles written")
        else:
            log.append("  → No preference data (empty ESB sales?)")
    except Exception as e:
        log.append(f"  ⚠️ Preferences error: {e}")

    # ── 9. SUPABASE SYNC: MEMBERS (business profile) ──────────────
    log.append("Syncing member profiles to Supabase members table...")
    try:
        import re as _re, io as _io
        import requests as _req_mem

        from supabase_client import (
            upsert_members, log_start as _ls_m, log_complete as _lc_m,
        )

        SHEET_ID_MEM = "1MAlR1WG7184GTCBmCTPUcX4OZKd09H1gx_0aSTKjt4k"
        csv_url = (
            f"https://docs.google.com/spreadsheets/d/{SHEET_ID_MEM}"
            f"/export?format=csv&gid=0"
        )
        resp_m = _req_mem.get(csv_url, timeout=30)
        resp_m.raise_for_status()

        df_sheet = pd.read_csv(_io.StringIO(resp_m.text), header=0)
        df_sheet.columns = df_sheet.columns.str.strip()
        df_sheet = df_sheet.dropna(how="all")

        def _norm_phone(raw) -> str | None:
            if not raw:
                return None
            p = _re.sub(r"[-\s.\(\)]", "", str(raw).strip())
            if p.startswith("62"):
                p = "0" + p[2:]
            elif not p.startswith("0") and p.isdigit():
                p = "0" + p
            if not p.isdigit() or not p.startswith("0") or not (10 <= len(p) <= 13):
                return None
            return p

        log_id_m = _ls_m("MEMBERS", "sync_member_profiles")
        mem_rows = []
        for _, row in df_sheet.iterrows():
            name      = str(row.get("Member Name", "") or "").strip()
            phone_raw = str(row.get("Phone Number", "") or "").strip()
            if not name or not phone_raw:
                continue
            phone = _norm_phone(phone_raw)
            if not phone:
                continue
            join_raw = str(row.get("Join Date", "") or "").strip()
            # Normalise date string — keep only first 10 chars (YYYY-MM-DD)
            join_date = join_raw[:10] if join_raw and join_raw not in ("nan", "None", "") else None

            mem_rows.append({
                "phone":              phone,
                "name":               name,
                "member_code":        str(row.get("Member Code", "") or "").strip() or None,
                "email":              f"{phone}@puripadelclub.com",
                "join_date":          join_date,
                "membership_type":    str(row.get("Membership Type", "") or "").strip() or None,
                "acquisition_source": str(row.get("Source", "") or "").strip() or None,
                "kode_ads":           str(
                    row.get("Kode ads", "") or row.get("Kode Ads", "") or ""
                ).strip() or None,
            })

        # Batch upsert (500 per call, sama seperti pattern yg sudah ada)
        mem_total = 0
        for i in range(0, len(mem_rows), 500):
            batch = mem_rows[i : i + 500]
            res_m = upsert_members(batch)
            mem_total += res_m.get("inserted", 0)
            if res_m.get("error"):
                log.append(f"  ⚠️ members batch {i//500+1} error: {res_m['error'][:120]}")
                break

        _lc_m(log_id_m, "success", {"rows_inserted": mem_total})
        log.append(
            f"  → Supabase members: {mem_total} upserted "
            f"({len(mem_rows)} valid dari {len(df_sheet)} baris)"
        )
    except Exception as e_mem:
        log.append(f"  ⚠️ Supabase members sync (non-fatal): {e_mem}")

    # ── 10. SUPABASE SYNC: TRANSACTIONS (ESB revenue) ─────────────
    log.append("Syncing ESB transactions to Supabase transactions table...")
    try:
        from supabase_client import (
            upsert_transactions, make_row_hash,
            log_start as _ls_t, log_complete as _lc_t,
        )

        _date_min = str(df_sales["Sales Date"].min())[:10]
        _date_max = str(df_sales["Sales Date"].max())[:10]
        log_id_t = _ls_t("ESB", "sync_transactions",
                          date_start=_date_min, date_end=_date_max)

        # Map ESB columns — ESB "Sales Recapitulation" header baris 10
        # Kolom kunci: Sales Date, Loyalty Member Name, Bill No,
        #              Menu, Menu Category, Qty, Price, Total, Payment Method
        seen_hashes: dict = {}
        for _, r in df_sales.iterrows():
            sale_date   = str(r.get("Sales Date",  "") or "")[:10]
            member_name = str(r.get("Loyalty Member Name", "") or "").strip()
            product     = str(r.get("Menu",   "") or r.get("Product",  "") or "").strip()
            category    = str(r.get("Menu Category", "") or r.get("Category", "") or "").strip()
            bill_no     = str(r.get("Bill No", "") or r.get("Bill Number", "") or "").strip()
            qty_raw     = r.get("Qty",  r.get("Quantity", 1))
            price_raw   = r.get("Price", r.get("Unit Price", 0))
            total_raw   = r.get("Total", r.get("Amount", r.get("Subtotal", 0)))
            pay_method  = str(r.get("Payment Method", "") or r.get("Payment", "") or "").strip()

            rh = make_row_hash(sale_date, bill_no, member_name, product, qty_raw, total_raw)
            if rh in seen_hashes:
                continue  # dedup within batch

            seen_hashes[rh] = {
                "row_hash":       rh,
                "sale_date":      sale_date or None,
                "member_name":    member_name or None,
                "product_name":   product or None,
                "category":       category or None,
                "qty":            int(float(qty_raw or 1)),
                "unit_price":     int(float(price_raw or 0)),
                "total_price":    int(float(total_raw or 0)),
                "payment_method": pay_method or None,
                "source":         "ESB",
            }

        tx_rows  = list(seen_hashes.values())
        tx_total = 0
        tx_err   = None
        for i in range(0, len(tx_rows), 500):
            batch = tx_rows[i : i + 500]
            res_t = upsert_transactions(batch)
            tx_total += res_t.get("inserted", 0)
            if res_t.get("error"):
                tx_err = res_t["error"]
                log.append(f"  ⚠️ transactions batch {i//500+1} error: {tx_err[:120]}")
                break

        _lc_t(log_id_t, "success" if not tx_err else "error",
              {"rows_fetched": len(df_sales), "rows_inserted": tx_total},
              error=tx_err)
        log.append(
            f"  → Supabase transactions: {tx_total} upserted "
            f"({len(tx_rows)} unique dari {len(df_sales)} ESB rows)"
        )
    except Exception as e_tx:
        log.append(f"  ⚠️ Supabase transactions sync (non-fatal): {e_tx}")

    log.append("✅ All done!")
    return log


# ── LOCAL DEV ENTRY POINT ──────────────────────
if __name__ == "__main__":
    print("Running pipeline locally...")
    result = run_pipeline()
    for line in result:
        print(line)
