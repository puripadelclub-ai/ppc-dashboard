"""
processor.py
Core analysis engine — porting dari analisis Python PPC Business Brain.
Menghitung: member matching, CLV, RFM, retention, campaign performance, products.
"""
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta


TODAY = pd.Timestamp.today().normalize()

# ─────────────────────────────────────────────
# 1. KLASIFIKASI SOURCE TYPE
# ─────────────────────────────────────────────

def classify_source(kode):
    if not kode or pd.isna(kode):
        return "Unclear"
    kl = str(kode).lower().replace("[", "").replace("]", "").strip()
    if kl in ("trial",):
        return "Trial/Free"
    if kl in ("free",):
        return "Trial/Free"
    if kl == "ads":
        return "ADS (generic)"
    if kl == "ots":
        return "OTS"
    if kl == "wa":
        return "WhatsApp"
    if kl == "x":
        return "Unclear"
    if "winner reclub" in kl or "1st winner" in kl:
        return "Event/Reclub"
    # Campaign spesifik — ada angka dan si/vid
    parts = kl.split("-")
    if parts and any(c.isdigit() for c in parts[0].strip()):
        return "ADS Campaign"
    return "ADS Campaign"


def extract_offer(campaign_name):
    """Extract offer type dari nama campaign. Contoh: [041 - SI - Membership - Batch 18.07] → Membership"""
    name = str(campaign_name).upper()
    if "BALL MACHINE" in name:
        return "Ball Machine"
    if "MEMBERSHIP" in name:
        return "Membership"
    if "COACHING" in name:
        return "Coaching"
    if "AWARENESS" in name:
        return "Awareness"
    if "BUNDLING" in name:
        return "Bundling"
    if "TOURNAMENT" in name:
        return "Tournament"
    return "Other"


def extract_ad_type(campaign_name):
    """Extract tipe iklan dari nama campaign. SI = Static Image, VID = Video."""
    name = str(campaign_name)
    if " - SI - " in name or "- SI -" in name:
        return "Static Image"
    if " - VID - " in name or "- VID -" in name:
        return "Video"
    return "Other"


def normalize_kode(kode):
    if not kode or pd.isna(kode):
        return ""
    return str(kode).strip().replace("[", "").replace("]", "").strip()


# ─────────────────────────────────────────────
# 2. MEMBER MATCHING
# ─────────────────────────────────────────────

def match_members_to_sales(df_mem, df_sales):
    """
    Match Membership List ke POS Sales via name_clean.
    Deduplikasi member dengan nama sama (multiple loyalty codes).
    """
    # Sales per loyalty code
    df_sl = df_sales[df_sales["Loyalty Member Code"].notna()].copy()
    df_sl["name_clean"] = df_sl["name_clean"].fillna("")

    sales_agg = df_sl.groupby("Loyalty Member Code").agg(
        name_clean   = ("name_clean", lambda x: x.mode()[0] if len(x) else ""),
        total_spending = ("Total", "sum"),
        nett_sales   = ("Nett Sales", "sum"),
        total_bills  = ("Bill Number", "nunique"),
        first_visit  = ("Sales Date", "min"),
        last_visit   = ("Sales Date", "max"),
    ).reset_index()

    # Membership — normalize
    df_mem = df_mem.copy()
    df_mem["Kode_norm"]   = df_mem["Kode ads"].apply(normalize_kode)
    df_mem["Source Type"] = df_mem["Kode ads"].apply(classify_source)
    df_mem["name_clean"]  = df_mem["name_clean"].fillna("")

    # Merge via name_clean
    merged = df_mem.merge(
        sales_agg.rename(columns={
            "Loyalty Member Code": "Loyalty_Code",
            "total_spending": "total_spending_raw",
            "total_bills": "total_bills_raw",
            "first_visit": "first_visit_raw",
            "last_visit": "last_visit_raw",
        }),
        on="name_clean",
        how="left",
    )

    # Deduplikasi: member dengan nama sama → aggregate
    grp_cols = ["No", "Member Name", "Kode ads", "Source Type", "Join Date",
                "Phone Number", "name_clean", "Kode_norm"]
    grp_cols = [c for c in grp_cols if c in merged.columns]

    fd = merged.groupby(grp_cols, dropna=False).agg(
        Loyalty_Code   = ("Loyalty_Code", lambda x: ", ".join(x.dropna().astype(str).unique())),
        total_spending = ("total_spending_raw", "sum"),
        nett_sales     = ("nett_sales", "sum"),
        total_bills    = ("total_bills_raw", "sum"),
        first_visit    = ("first_visit_raw", "min"),
        last_visit     = ("last_visit_raw", "max"),
    ).reset_index()

    fd = fd.rename(columns={"Member Name": "Member Name"})

    # Matched = ada di POS
    fd["Matched"] = fd["total_spending"] > 0

    return fd


# ─────────────────────────────────────────────
# 3. METRICS CALCULATION
# ─────────────────────────────────────────────

def calculate_metrics(fd):
    fd = fd.copy()

    fd["tenure_days"]      = (fd["last_visit"] - fd["first_visit"]).dt.days.fillna(0)
    fd["days_since_join"]  = (TODAY - fd["first_visit"]).dt.days.clip(lower=0).fillna(0)
    fd["days_since_last"]  = (TODAY - fd["last_visit"]).dt.days.clip(lower=0).fillna(999)

    fd["avg_spend_per_visit"] = (
        fd["total_spending"] / fd["total_bills"].replace(0, np.nan)
    ).round(0).fillna(0)

    # Monthly spend: pakai days_since_join, floor 1 bulan
    fd["months_active"] = (fd["days_since_join"] / 30).clip(lower=1)
    fd["monthly_spend"] = (fd["total_spending"] / fd["months_active"]).round(0)

    # CLV scenarios
    RETENTION_RATE = 0.422  # dari analisis Mar-Jun 2026
    LIFETIME_CONSERVATIVE = round(1 / (1 - RETENTION_RATE), 2)  # ~1.73
    LIFETIME_MODERATE = 6
    LIFETIME_OPTIMISTIC = 12

    fd["CLV_Conservative"]   = (fd["monthly_spend"] * LIFETIME_CONSERVATIVE).round(0)
    fd["CLV_Moderate_6mo"]   = (fd["monthly_spend"] * LIFETIME_MODERATE).round(0)
    fd["CLV_Optimistic_12mo"] = (fd["monthly_spend"] * LIFETIME_OPTIMISTIC).round(0)

    return fd


# ─────────────────────────────────────────────
# 4. RFM SCORING
# ─────────────────────────────────────────────

def calculate_rfm(fd):
    df = fd[fd["Matched"]].copy()
    if df.empty:
        return df

    # R = recency (hari sejak kunjungan terakhir) → lower = better
    # F = frequency (total bills)
    # M = monetary (total spending)

    df["R_score"] = pd.qcut(df["days_since_last"].rank(method="first"),
                             q=5, labels=[5, 4, 3, 2, 1]).astype(int)
    df["F_score"] = pd.qcut(df["total_bills"].rank(method="first"),
                             q=5, labels=[1, 2, 3, 4, 5]).astype(int)
    df["M_score"] = pd.qcut(df["total_spending"].rank(method="first"),
                             q=5, labels=[1, 2, 3, 4, 5]).astype(int)

    df["RFM_Score"] = df["R_score"].astype(str) + df["F_score"].astype(str) + df["M_score"].astype(str)

    def rfm_segment(row):
        r, f, m = row["R_score"], row["F_score"], row["M_score"]
        if r >= 4 and f >= 4 and m >= 4:
            return "Champions"
        elif r >= 3 and f >= 3:
            return "Loyal"
        elif r >= 4 and f <= 2:
            return "New"
        elif r <= 2 and f >= 3:
            return "At Risk"
        elif r <= 2 and f <= 2 and m >= 3:
            return "Cannot Lose"
        elif r == 1:
            return "Lost"
        else:
            return "Needs Attention"

    df["RFM_Segment"] = df.apply(rfm_segment, axis=1)

    def recommend_action(row):
        seg = row["RFM_Segment"]
        actions = {
            "Champions":     "Referral program, VIP treatment, early access promo",
            "Loyal":         "Loyalty reward, upsell coaching / court pass",
            "New":           "Onboarding WA, ajak coba coaching gratis",
            "At Risk":       "Win-back WA: 'Kami kangen kamu, ini promo spesial'",
            "Cannot Lose":   "WA personal dari owner/coach, special offer",
            "Lost":          "Re-engagement campaign, atau accept churn",
            "Needs Attention": "Monitor, send reminder WA setelah 30 hari tidak datang",
        }
        return actions.get(seg, "-")

    df["Recommended_Action"] = df.apply(recommend_action, axis=1)
    return df


# ─────────────────────────────────────────────
# 5. COHORT RETENTION
# ─────────────────────────────────────────────

def calculate_retention(df_sales):
    df_sl = df_sales[df_sales["Loyalty Member Code"].notna()].copy()
    df_sl["year_month"] = df_sl["Sales Date"].dt.to_period("M")

    monthly = df_sl.groupby(["year_month", "Loyalty Member Code"]).size().reset_index()
    monthly.columns = ["month", "member", "txn"]
    months = sorted(monthly["month"].unique())

    rows = []
    for i in range(len(months) - 1):
        m_now  = months[i]
        m_next = months[i + 1]
        active_now  = set(monthly[monthly["month"] == m_now]["member"])
        active_next = set(monthly[monthly["month"] == m_next]["member"])
        retained = len(active_now & active_next)
        rate = retained / len(active_now) if active_now else 0
        rows.append({
            "Period":        f"{m_now} → {m_next}",
            "Active_Now":    len(active_now),
            "Retained":      retained,
            "Churned":       len(active_now) - retained,
            "Retention_Rate": round(rate * 100, 1),
            "Churn_Rate":    round((1 - rate) * 100, 1),
        })

    return pd.DataFrame(rows)


# ─────────────────────────────────────────────
# 6. REVENUE ANALYSIS
# ─────────────────────────────────────────────

def calculate_revenue(df_sales):
    df = df_sales.copy()
    df["year_month"]    = df["Sales Date"].dt.to_period("M").astype(str)
    df["Menu Category"] = df["Menu Category"].fillna("OTHER")

    # Revenue per bulan — filter NaT rows, no redundant Month_str column
    rev_monthly = df[df["year_month"].notna() & (df["year_month"] != "NaT")].groupby("year_month")["Total"].sum().reset_index()
    rev_monthly.columns = ["Month", "Total_Revenue"]

    # Revenue per kategori per bulan
    rev_cat = df.groupby(["year_month", "Menu Category"])["Total"].sum().reset_index()
    rev_cat.columns = ["Month", "Category", "Revenue"]

    # Peak day of week
    df["day_of_week"] = df["Sales Date"].dt.day_name()
    peak_day = df.groupby("day_of_week")["Bill Number"].nunique().reset_index()
    peak_day.columns = ["Day", "Unique_Bills"]

    return rev_monthly, rev_cat, peak_day


# ─────────────────────────────────────────────
# 7. PRODUCT ANALYSIS
# ─────────────────────────────────────────────

def calculate_products(df_sales):
    df = df_sales[df_sales["Menu"].notna()].copy()

    products = df.groupby(["Menu Category", "Menu"]).agg(
        Total_Qty     = ("Qty", "sum"),
        Total_Revenue = ("Total", "sum"),
        Total_Orders  = ("Bill Number", "nunique"),
    ).reset_index().sort_values("Total_Revenue", ascending=False)

    products["Avg_Revenue_Per_Order"] = (
        products["Total_Revenue"] / products["Total_Orders"].replace(0, np.nan)
    ).round(0)

    return products


# ─────────────────────────────────────────────
# 8. CAMPAIGN PERFORMANCE
# ─────────────────────────────────────────────

def parse_actions_results(value):
    """
    Parse Facebook Insights Actions JSON → angka results (leads/conversions).
    FB API mengembalikan array: [{"action_type":"lead","value":"5"}, ...]
    Priority: lead → offsite_conversion.fb_pixel_lead → complete_registration → nilai pertama.
    Kalau sudah angka biasa, langsung return.
    """
    if pd.isna(value) or value == "":
        return 0.0
    # Sudah angka
    try:
        return float(value)
    except (ValueError, TypeError):
        pass
    # Parse JSON string
    try:
        actions = json.loads(str(value))
        if not isinstance(actions, list):
            return 0.0
        lead_types = {
            "lead",
            "offsite_conversion.fb_pixel_lead",
            "offsite_conversion.lead",
            "complete_registration",
        }
        for action in actions:
            if action.get("action_type") in lead_types:
                return float(action.get("value", 0))
        # Fallback: nilai action pertama
        if actions:
            return float(actions[0].get("value", 0))
    except Exception:
        pass
    return 0.0

def derive_kill_dates(df_ads):
    """
    Derive tanggal campaign di-kill dari Meta Ads daily export.

    Logika:
    - Jika ada kolom status/delivery dan campaign berstatus PAUSED/DELETED,
      kill_date = hari terakhir campaign punya spend > 0.
    - Jika tidak ada kolom status, kill_date = hari terakhir ada spend > 0
      untuk campaign yang sudah tidak tayang (tidak ada row di 7 hari terakhir).

    Return: dict {campaign_name_normalized: kill_date (pd.Timestamp or NaT)}
    """
    if df_ads.empty or "Date" not in df_ads.columns:
        return {}

    # Temukan kolom campaign name
    camp_col = next(
        (c for c in df_ads.columns if "campaign name" in c.lower() or c.lower() == "campaign"),
        None,
    )
    if not camp_col:
        return {}

    # Temukan kolom spend
    spend_col = next(
        (c for c in df_ads.columns if "amount spent" in c.lower() or c.lower() in ("spend", "cost")),
        None,
    )

    # Temukan kolom status/delivery
    status_col = next(
        (c for c in df_ads.columns if c.lower() in ("status", "delivery", "campaign status")),
        None,
    )

    df = df_ads.copy()
    df["_campaign_norm"] = df[camp_col].str.strip().str.replace(r"[\[\]]", "", regex=True).str.strip()

    if spend_col:
        df["_spend"] = pd.to_numeric(df[spend_col], errors="coerce").fillna(0)
    else:
        df["_spend"] = 0

    kill_dates = {}
    latest_data_date = df["Date"].max()
    cutoff_active = latest_data_date - pd.Timedelta(days=7)

    for camp, grp in df.groupby("_campaign_norm"):
        # Cek apakah campaign berstatus PAUSED / DELETED
        is_killed = False
        if status_col:
            statuses = grp[status_col].dropna().str.upper().unique()
            if any(s in ("PAUSED", "DELETED", "ARCHIVED") for s in statuses):
                is_killed = True
        else:
            # Jika tidak ada kolom status: anggap killed jika tidak ada row dalam 7 hari terakhir
            if grp["Date"].max() < cutoff_active:
                is_killed = True

        if is_killed:
            # Kill date = hari terakhir ada spend > 0
            spent_rows = grp[grp["_spend"] > 0]
            if not spent_rows.empty:
                kill_dates[camp] = spent_rows["Date"].max()
            else:
                kill_dates[camp] = grp["Date"].max()

    return kill_dates


def calculate_campaigns(fd, df_ads, df_leads=None):
    """
    Hitung CAC, ROAS, CIR, dan post-kill CIR per campaign.
    fd       = member dataframe (hasil match)
    df_ads   = raw Meta Ads daily data dari Sheets (raw_ads tab)
    df_leads = actual leads & convert customer (actual_leads tab), opsional
    """
    if df_ads.empty:
        return pd.DataFrame()

    # Normalize nama kolom Meta Ads
    col_map = {}
    for c in df_ads.columns:
        cl = c.lower()
        if cl == "ad name":
            col_map[c] = "ad_name"
        elif "campaign name" in cl or cl == "campaign":
            col_map[c] = "campaign_name"   # keep campaign as separate field
        elif "adset name" in cl:
            col_map[c] = "adset_name"
        elif "amount spent" in cl or cl in ("spend", "cost"):
            col_map[c] = "budget_spent"
        elif "result" in cl and "cost" not in cl:
            col_map[c] = "results"
        elif "cost per result" in cl:
            col_map[c] = "cpr"
        elif "leads" in cl and "cost" not in cl:
            col_map[c] = "leads"
        elif "cost per lead" in cl or "cpl" in cl:
            col_map[c] = "cpl"
        elif "ctr" in cl:
            col_map[c] = "ctr"
        elif "cpm" in cl:
            col_map[c] = "cpm"
        elif "impressions" in cl:
            col_map[c] = "impressions"

    df_ads = df_ads.rename(columns=col_map)

    # Parse kolom numerik — handle JSON array dari Facebook Insights
    for num_col in ("budget_spent", "results", "leads", "cpr"):
        if num_col in df_ads.columns:
            if num_col in ("results", "cpr"):
                df_ads[num_col] = df_ads[num_col].apply(parse_actions_results)
            else:
                df_ads[num_col] = pd.to_numeric(df_ads[num_col], errors="coerce").fillna(0)

    # Derive kill dates dari data harian
    kill_dates = derive_kill_dates(df_ads)

    # Aggregate total per individual ad (sum semua hari)
    agg_cols = {k: "sum" for k in ["budget_spent", "results", "leads"]
                if k in df_ads.columns}
    if not agg_cols or "ad_name" not in df_ads.columns:
        return pd.DataFrame()

    # Also carry campaign_name (take first value per ad)
    extra_cols = {}
    if "campaign_name" in df_ads.columns:
        extra_cols["campaign_name"] = ("campaign_name", "first")
    if "adset_name" in df_ads.columns:
        extra_cols["adset_name"] = ("adset_name", "first")

    if extra_cols:
        agg_spec = {k: "sum" for k in agg_cols}
        agg_spec.update({alias: func for alias, (col, func) in extra_cols.items()
                         if col in df_ads.columns})
        # Use named aggregation
        named_agg = {}
        for k in agg_cols:
            named_agg[k] = (k, "sum")
        for alias, (col, func) in extra_cols.items():
            if col in df_ads.columns:
                named_agg[alias] = (col, func)
        ads_summary = df_ads.groupby("ad_name", as_index=False).agg(**named_agg)
    else:
        ads_summary = df_ads.groupby("ad_name", as_index=False).agg(agg_cols)
    ads_summary["ad_name_norm"] = (
        ads_summary["ad_name"].str.strip().str.replace(r"[\[\]]", "", regex=True).str.strip()
    )

    # Spending per campaign dari POS (hanya ADS Campaign)
    camp_spend = fd[fd["Source Type"] == "ADS Campaign"].groupby("Kode_norm").agg(
        member_count   = ("Member Name", "count"),
        total_spending = ("total_spending", "sum"),
        converts       = ("Member Name", "count"),
    ).reset_index()

    # Merge — left join (member campaigns with optional ads match)
    result = camp_spend.merge(
        ads_summary, left_on="Kode_norm", right_on="ad_name_norm", how="left"
    )

    # Also include Meta Ads campaigns that have NO matching Kode_norm
    # (shows actual spend even without member attribution)
    matched_norms = set(camp_spend["Kode_norm"].str.strip())
    unmatched_ads = ads_summary[~ads_summary["ad_name_norm"].isin(matched_norms)].copy()
    if not unmatched_ads.empty:
        unmatched_ads["Kode_norm"] = unmatched_ads["ad_name_norm"]
        unmatched_ads["member_count"]    = 0
        unmatched_ads["total_spending"]  = 0
        unmatched_ads["converts"]        = 0
        result = pd.concat([result, unmatched_ads], ignore_index=True)

    # Hitung metrics
    b = result.get("budget_spent", pd.Series(dtype=float)).replace(0, np.nan)
    result["CAC"]  = (b / result["converts"].replace(0, np.nan)).round(0)
    result["ROAS"] = (result["total_spending"] / b).round(2)
    result["CIR"]  = (b / result["total_spending"].replace(0, np.nan) * 100).round(1)
    if "leads" in result.columns:
        result["CPL"] = (b / result["leads"].replace(0, np.nan)).round(0)

    # Offer & Ad Type — auto dari nama campaign
    result["Offer"]   = result["Kode_norm"].apply(extract_offer)
    result["Ad_Type"] = result["Kode_norm"].apply(extract_ad_type)

    # Kill date dari data harian Meta Ads
    result["Kill_Date"]        = result["Kode_norm"].map(kill_dates)
    result["CIR_PostKill_H30"] = None
    result["CIR_PostKill_H60"] = None

    # ── JOIN ACTUAL LEADS DATA ──────────────────────────────────────
    if df_leads is not None and not df_leads.empty:
        # Aggregate actual leads per campaign (sum semua periode)
        leads_agg = df_leads.groupby("Ads_Campaign", as_index=False).agg(
            Actual_Leads     = ("Actual_Leads", "sum"),
            Convert_Customer = ("Convert_Customer", "sum"),
        )
        leads_agg["camp_norm"] = (
            leads_agg["Ads_Campaign"]
            .str.strip()
            .str.replace(r"[\[\]]", "", regex=True)
            .str.strip()
        )

        result = result.merge(
            leads_agg[["camp_norm", "Actual_Leads", "Convert_Customer"]],
            left_on="Kode_norm", right_on="camp_norm", how="left"
        ).drop(columns=["camp_norm"], errors="ignore")

        # CPL aktual = Budget / Actual Leads (bukan Meta leads)
        result["CPL_Actual"] = (
            result["budget_spent"] / result["Actual_Leads"].replace(0, np.nan)
        ).round(0).fillna(0)

        # CAC aktual = Budget / Convert Customer
        result["CAC_Actual"] = (
            result["budget_spent"] / result["Convert_Customer"].replace(0, np.nan)
        ).round(0).fillna(0)

        # Conversion rates
        result["Conv_Rate_Leads"] = (
            result["Actual_Leads"] / result.get("results", pd.Series(dtype=float)).replace(0, np.nan) * 100
        ).round(1).fillna(0)

        result["Conv_Rate_Convert"] = (
            result["Convert_Customer"] / result["Actual_Leads"].replace(0, np.nan) * 100
        ).round(1).fillna(0)

    return result


# ─────────────────────────────────────────────
# 9. SUMMARY KPIs
# ─────────────────────────────────────────────

def calculate_summary(fd, rev_monthly, df_ads):
    total_members  = len(fd)
    matched        = fd["Matched"].sum()
    total_spending = fd["total_spending"].sum()

    current_month = TODAY.to_period("M").strftime("%Y-%m")
    last_month    = (TODAY - pd.DateOffset(months=1)).to_period("M").strftime("%Y-%m")

    # Gunakan copy untuk lookup agar tidak mutate rev_monthly yang akan ditulis ke Sheets
    _rev = rev_monthly.copy()
    _rev["Month_str"] = _rev["Month"].astype(str)
    rev_this  = _rev[_rev["Month_str"] == current_month]["Total_Revenue"].sum()
    rev_last  = _rev[_rev["Month_str"] == last_month]["Total_Revenue"].sum()
    rev_delta = round((rev_this - rev_last) / rev_last * 100, 1) if rev_last else 0

    total_budget = 0
    if not df_ads.empty:
        spent_col = next((c for c in df_ads.columns if "budget_spent" in c.lower() or "amount spent" in c.lower()), None)
        if spent_col:
            total_budget = pd.to_numeric(df_ads[spent_col], errors="coerce").sum()

    overall_roas = round(total_spending / total_budget, 2) if total_budget > 0 else 0

    ads_members = fd[fd["Source Type"] == "ADS Campaign"]["Member Name"].count()

    return pd.DataFrame([{
        "Total_Members":        total_members,
        "Matched_to_POS":       int(matched),
        "Total_Spending":       int(total_spending),
        "Revenue_This_Month":   int(rev_this),
        "Revenue_Last_Month":   int(rev_last),
        "Revenue_MoM_Pct":      rev_delta,
        "Total_Ads_Budget":     int(total_budget),
        "Overall_ROAS":         overall_roas,
        "ADS_Campaign_Members": int(ads_members),
        "Last_Updated":         TODAY.strftime("%Y-%m-%d"),
    }])


# ─────────────────────────────────────────────
# 10. MEMBER PRODUCT PREFERENCES
# ─────────────────────────────────────────────

# Fuzzy keyword mapping: ESB Menu Category → standard product type
# Urutan penting — lebih spesifik dulu (Ball Machine sebelum Court)
_CATEGORY_MAP = [
    ("Ball Machine", ["ball machine", "mesin bola"]),
    ("Coaching",     ["coaching", "coach", "private lesson", "private training", "latihan", "pelatihan"]),
    ("F&B",          ["f&b", "food", "beverage", "makan", "minum", "minuman", "makanan", "snack", "drink", "cafe", "coffee", "tea"]),
    ("Merchandise",  ["merchandise", "alat", "aksesoris", "raket", "grip", "equipment", "bag", "tas", "bola padel", "shuttlecock"]),
    ("Tournament",   ["tournament", "tournamen", "kompetisi", "competition"]),
    ("Court",        ["lapangan", "court", "reservasi", "sewa", "booking"]),
]

def map_esb_category(cat_str: str) -> str:
    """Fuzzy-map ESB Menu Category ke kategori produk standar."""
    c = str(cat_str).lower()
    for prod_type, keywords in _CATEGORY_MAP:
        if any(k in c for k in keywords):
            return prod_type
    return "Other"


def calculate_member_preferences(df_sales: pd.DataFrame, df_raw_avm: pd.DataFrame = None) -> pd.DataFrame:
    """
    Hitung product preference per member dari ESB sales + AVM booking data.

    - ESB  → revenue per kategori produk (Court, BM, Coaching, F&B, dll)
    - AVM  → jam favorit, court favorit, jumlah sesi BM/Coaching dalam 30 hari

    Returns DataFrame dengan satu baris per name_clean (join key ke out_members).
    Kolom output:
      Rev_Court, Rev_Ball_Machine, Rev_Coaching, Rev_FnB, Rev_Other
      Sess_Court, Sess_Ball_Machine, Sess_Coaching, Sess_FnB
      AVM_BM_30d, AVM_Coaching_30d
      Fav_Period, Fav_Court
      Top_Product, Pref_Tags
    """
    if df_sales.empty:
        return pd.DataFrame()

    df = df_sales.copy()

    # ── name_clean untuk join ─────────────────────────
    name_col = next((c for c in df.columns if "loyalty member name" in c.lower() or c == "Loyalty Member Name"), None)
    if name_col:
        df["name_clean"] = df[name_col].astype(str).str.lower().str.strip()
    elif "name_clean" in df.columns:
        pass  # sudah ada
    else:
        return pd.DataFrame()

    df = df[df["name_clean"].notna() & (df["name_clean"] != "") & (df["name_clean"] != "nan")]

    # ── Map kategori produk ───────────────────────────
    cat_col = "Menu Category" if "Menu Category" in df.columns else None
    df["product_type"] = df[cat_col].fillna("Other").apply(map_esb_category) if cat_col else "Other"

    total_col = "Total" if "Total" in df.columns else None
    bill_col  = "Bill Number" if "Bill Number" in df.columns else None
    if not total_col:
        df["Total"] = 0

    # ── Aggregate per member × kategori ──────────────
    agg_dict = {"Total": "sum"}
    if bill_col:
        agg_dict["Bill Number"] = "nunique"

    mem_prod = df.groupby(["name_clean", "product_type"]).agg(agg_dict).reset_index()
    mem_prod.columns = ["name_clean", "product_type", "revenue"] + (["sessions"] if bill_col else [])
    if "sessions" not in mem_prod.columns:
        mem_prod["sessions"] = 0

    # ── Pivot ke wide format ──────────────────────────
    def safe_col(t):
        return t.replace(" ", "_").replace("&", "n").replace("/", "_")

    rev_piv = mem_prod.pivot_table(
        index="name_clean", columns="product_type", values="revenue", aggfunc="sum", fill_value=0
    ).rename(columns=lambda c: f"Rev_{safe_col(c)}")

    sess_piv = mem_prod.pivot_table(
        index="name_clean", columns="product_type", values="sessions", aggfunc="sum", fill_value=0
    ).rename(columns=lambda c: f"Sess_{safe_col(c)}")

    pref = rev_piv.join(sess_piv, how="outer").fillna(0).reset_index()

    # Ensure standard columns exist
    for cat in ["Court", "Ball_Machine", "Coaching", "FnB", "Merchandise", "Tournament", "Other"]:
        if f"Rev_{cat}" not in pref.columns:  pref[f"Rev_{cat}"] = 0
        if f"Sess_{cat}" not in pref.columns: pref[f"Sess_{cat}"] = 0

    # Top product by ESB revenue
    rev_cols = [c for c in pref.columns if c.startswith("Rev_")]
    pref["Top_Product"] = (
        pref[rev_cols].idxmax(axis=1)
        .str.replace("Rev_", "").str.replace("_", " ").str.replace("Fn", "F&")
    ) if rev_cols else "Other"

    # ── AVM: jam & court preferences ─────────────────
    pref["Fav_Period"]     = ""
    pref["Fav_Court"]      = ""
    pref["AVM_BM_30d"]     = 0
    pref["AVM_Coaching_30d"] = 0

    if df_raw_avm is not None and not df_raw_avm.empty:
        avm = df_raw_avm.copy()
        avm["name_clean"] = avm["customer_name"].astype(str).str.lower().str.strip()
        avm["date"]       = pd.to_datetime(avm["date"], errors="coerce")
        cutoff_30d        = pd.Timestamp.today() - pd.Timedelta(days=30)
        avm30             = avm[avm["date"] >= cutoff_30d]

        # Periode favorit (dari semua history, bukan hanya 30 hari)
        if "period" in avm.columns:
            per_pref = (
                avm.groupby("name_clean")["period"]
                .agg(lambda x: x.value_counts().index[0] if len(x) > 0 else "")
                .reset_index().rename(columns={"period": "Fav_Period"})
            )
            pref = pref.merge(per_pref, on="name_clean", how="left", suffixes=("_x", ""))
            if "Fav_Period_x" in pref.columns:
                pref["Fav_Period"] = pref["Fav_Period"].fillna(pref["Fav_Period_x"])
                pref.drop(columns=["Fav_Period_x"], inplace=True)
            pref["Fav_Period"] = pref["Fav_Period"].fillna("")

        # Court favorit
        if "court" in avm.columns:
            court_pref = (
                avm[avm["court"].astype(str).str.strip() != ""]
                .groupby("name_clean")["court"]
                .agg(lambda x: x.value_counts().index[0] if len(x) > 0 else "")
                .reset_index().rename(columns={"court": "Fav_Court"})
            )
            pref = pref.merge(court_pref, on="name_clean", how="left", suffixes=("_x", ""))
            if "Fav_Court_x" in pref.columns:
                pref["Fav_Court"] = pref["Fav_Court"].fillna(pref["Fav_Court_x"])
                pref.drop(columns=["Fav_Court_x"], inplace=True)
            pref["Fav_Court"] = pref["Fav_Court"].fillna("")

        # BM sessions (30 hari) dari AVM booking code
        if "booking_code" in avm.columns:
            bm_codes = {"BP1", "BP2", "BP"}
            bm_s = (
                avm30[avm30["booking_code"].isin(bm_codes)]
                .groupby("name_clean").size().reset_index().rename(columns={0: "AVM_BM_30d"})
            )
            pref = pref.merge(bm_s, on="name_clean", how="left", suffixes=("_x", ""))
            if "AVM_BM_30d_x" in pref.columns:
                pref["AVM_BM_30d"] = pref["AVM_BM_30d"].combine_first(pref["AVM_BM_30d_x"])
                pref.drop(columns=["AVM_BM_30d_x"], inplace=True)
            pref["AVM_BM_30d"] = pref["AVM_BM_30d"].fillna(0).astype(int)

            # Coaching sessions (30 hari)
            coa_s = (
                avm30[avm30["booking_code"] == "CBA"]
                .groupby("name_clean").size().reset_index().rename(columns={0: "AVM_Coaching_30d"})
            )
            pref = pref.merge(coa_s, on="name_clean", how="left", suffixes=("_x", ""))
            if "AVM_Coaching_30d_x" in pref.columns:
                pref["AVM_Coaching_30d"] = pref["AVM_Coaching_30d"].combine_first(pref["AVM_Coaching_30d_x"])
                pref.drop(columns=["AVM_Coaching_30d_x"], inplace=True)
            pref["AVM_Coaching_30d"] = pref["AVM_Coaching_30d"].fillna(0).astype(int)

    # ── Preference tags ───────────────────────────────
    def build_tags(row):
        tags = []
        # Ball Machine (prioritaskan AVM karena lebih akurat untuk session count)
        bm_total = int(row.get("AVM_BM_30d", 0)) + int(row.get("Sess_Ball_Machine", 0))
        if bm_total >= 4:   tags.append("Ball Machine Regular")
        elif bm_total >= 1: tags.append("Ball Machine User")

        # Coaching
        coa_total = int(row.get("AVM_Coaching_30d", 0)) + int(row.get("Sess_Coaching", 0))
        if coa_total >= 3:   tags.append("Coaching Regular")
        elif coa_total >= 1: tags.append("Coaching User")

        # F&B — dari ESB revenue
        fnb_rev = float(row.get("Rev_FnB", 0))
        if fnb_rev >= 300_000:   tags.append("F&B Loyal")
        elif fnb_rev >= 50_000:  tags.append("F&B User")

        # Jam favorit dari AVM
        period = str(row.get("Fav_Period", "")).strip()
        if period and period != "nan":
            tags.append(f"{period} Player")

        return " | ".join(tags) if tags else ""

    pref["Pref_Tags"] = pref.apply(build_tags, axis=1)

    # Rename FnB kolom ke lebih readable
    pref = pref.rename(columns={
        "Rev_Ball_Machine": "Rev_BM",
        "Sess_Ball_Machine": "Sess_BM",
        "Rev_FnB": "Rev_FnB",
        "Sess_FnB": "Sess_FnB",
    })

    return pref


# ─────────────────────────────────────────────
# 11. COACHING ANALYTICS
# ─────────────────────────────────────────────

def calculate_coaching(df_coaching: pd.DataFrame) -> dict:
    """
    Hitung analytics coaching dari raw_coaching (PPC Coaching Log sheet).

    Input kolom: Date, Member_Name, Package_Type, Participants, Start_Time,
                 End_Time, Sessions_Remaining, Coach, Status, Notes, name_clean

    Returns dict dengan keys:
      - monthly    : DataFrame ringkasan per bulan
      - by_package : DataFrame sesi per Package_Type
      - conversion : dict free-to-paid conversion rate
      - active_bundles : DataFrame member dengan sisa bundling aktif
      - summary    : dict KPIs keseluruhan
    """
    if df_coaching is None or df_coaching.empty:
        empty = pd.DataFrame()
        return {
            "monthly": empty, "by_package": empty,
            "conversion": {}, "active_bundles": empty, "summary": {}
        }

    df = df_coaching.copy()

    # Pastikan kolom Date adalah datetime
    if "Date" not in df.columns:
        return {"monthly": pd.DataFrame(), "by_package": pd.DataFrame(),
                "conversion": {}, "active_bundles": pd.DataFrame(), "summary": {}}

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])

    # Hanya sesi Done & Reschedule (bukan Cancel/No_Show)
    status_col = "Status" if "Status" in df.columns else None
    if status_col:
        df_done = df[df[status_col].isin(["Done", ""])].copy()
    else:
        df_done = df.copy()

    # Normalisasi numerik
    df_done["Participants"] = pd.to_numeric(df_done.get("Participants", 1), errors="coerce").fillna(1)
    df_done["Sessions_Remaining"] = pd.to_numeric(
        df_done.get("Sessions_Remaining", pd.Series(dtype=float)), errors="coerce"
    )

    df_done["YearMonth"] = df_done["Date"].dt.to_period("M").astype(str)

    # ── 1. Ringkasan bulanan ──────────────────────────────────────────────────
    monthly_agg = (
        df_done.groupby("YearMonth")
        .agg(
            Total_Sessions=("Date", "count"),
            Unique_Members=("name_clean" if "name_clean" in df_done.columns else "Member_Name", "nunique"),
            Free_Coaching=(
                "Package_Type",
                lambda x: (x == "Free_Coaching").sum()
            ),
            Paid_Sessions=(
                "Package_Type",
                lambda x: x.isin(["Bundling_4x", "Bundling_6x", "Private"]).sum()
            ),
            Kids_Sessions=(
                "Package_Type",
                lambda x: (x == "Coaching_Kids").sum()
            ),
            Total_Participants=("Participants", "sum"),
        )
        .reset_index()
        .sort_values("YearMonth")
    )
    monthly_agg["Paid_Rate_Pct"] = (
        monthly_agg["Paid_Sessions"] / monthly_agg["Total_Sessions"].replace(0, 1) * 100
    ).round(1)

    # ── 2. Breakdown per Package_Type ─────────────────────────────────────────
    pkg_agg = (
        df_done.groupby("Package_Type")
        .agg(
            Sessions=("Date", "count"),
            Unique_Members=("name_clean" if "name_clean" in df_done.columns else "Member_Name", "nunique"),
            Total_Participants=("Participants", "sum"),
        )
        .reset_index()
        .sort_values("Sessions", ascending=False)
    )

    # ── 3. Konversi Free → Paid ───────────────────────────────────────────────
    conversion = {}
    if "name_clean" in df_done.columns:
        free_members = set(
            df_done[df_done["Package_Type"] == "Free_Coaching"]["name_clean"].unique()
        )
        paid_members = set(
            df_done[df_done["Package_Type"].isin(["Bundling_4x", "Bundling_6x", "Private"])]["name_clean"].unique()
        )
        converted = free_members & paid_members
        conversion = {
            "total_free_members":    len(free_members),
            "converted_to_paid":     len(converted),
            "conversion_rate_pct":   round(len(converted) / len(free_members) * 100, 1) if free_members else 0,
            "still_free_only":       len(free_members - paid_members),
        }

    # ── 4. Active bundles (sisa sesi > 0) ────────────────────────────────────
    bundles_df = df_done[
        df_done["Package_Type"].isin(["Bundling_4x", "Bundling_6x"]) &
        df_done["Sessions_Remaining"].notna() &
        (df_done["Sessions_Remaining"] > 0)
    ].copy()

    if not bundles_df.empty:
        # Ambil baris terbaru per member per package_type
        active_bundles = (
            bundles_df.sort_values("Date", ascending=False)
            .groupby(["name_clean" if "name_clean" in bundles_df.columns else "Member_Name", "Package_Type"])
            .first()
            .reset_index()
            [["name_clean" if "name_clean" in bundles_df.columns else "Member_Name",
              "Package_Type", "Sessions_Remaining", "Date"]]
        )
        active_bundles = active_bundles.rename(columns={
            "Date": "Last_Session_Date",
            "name_clean": "Member_Name"
        })
        active_bundles["Last_Session_Date"] = active_bundles["Last_Session_Date"].dt.strftime("%Y-%m-%d")
        active_bundles["Sessions_Remaining"] = active_bundles["Sessions_Remaining"].astype(int)
    else:
        active_bundles = pd.DataFrame()

    # ── 5. Summary KPIs ───────────────────────────────────────────────────────
    cutoff_30d = TODAY - pd.Timedelta(days=30)
    df_30d = df_done[df_done["Date"] >= cutoff_30d]

    total_sessions_all  = len(df_done)
    total_sessions_30d  = len(df_30d)
    free_sessions_30d   = (df_30d["Package_Type"] == "Free_Coaching").sum() if not df_30d.empty else 0
    paid_sessions_30d   = df_30d["Package_Type"].isin(["Bundling_4x", "Bundling_6x", "Private"]).sum() if not df_30d.empty else 0
    kids_sessions_30d   = (df_30d["Package_Type"] == "Coaching_Kids").sum() if not df_30d.empty else 0
    unique_members_30d  = df_30d["name_clean"].nunique() if "name_clean" in df_30d.columns else 0

    summary = {
        "Total_Sessions_All":    int(total_sessions_all),
        "Total_Sessions_30d":    int(total_sessions_30d),
        "Free_Sessions_30d":     int(free_sessions_30d),
        "Paid_Sessions_30d":     int(paid_sessions_30d),
        "Kids_Sessions_30d":     int(kids_sessions_30d),
        "Unique_Members_30d":    int(unique_members_30d),
        "Active_Bundles":        len(active_bundles),
        "Conversion_Rate_Pct":   conversion.get("conversion_rate_pct", 0),
        "Last_Updated":          TODAY.strftime("%Y-%m-%d"),
    }

    return {
        "monthly":        monthly_agg,
        "by_package":     pkg_agg,
        "conversion":     conversion,
        "active_bundles": active_bundles,
        "summary":        summary,
    }


# ─────────────────────────────────────────────
# 11. PRODUCT DETAIL BREAKDOWN (dari ESB)
# ─────────────────────────────────────────────

def calculate_product_detail(df_sales: pd.DataFrame) -> pd.DataFrame:
    """
    Breakdown revenue dan transaksi per produk/menu dari data ESB.
    Kolom ESB yang dipakai: Menu Category, Menu, Menu Code, Qty,
                            Harga Jual / Unit Price, Grand Total,
                            Sales Date, Loyalty Member Name
    """
    if df_sales is None or df_sales.empty:
        return pd.DataFrame()

    df = df_sales.copy()

    # Flexible column name resolution
    def col(candidates):
        for c in candidates:
            if c in df.columns:
                return c
        return None

    cat_col  = col(["Menu Category", "menu_category", "MenuCategory"])
    menu_col = col(["Menu", "menu", "Menu Name", "Nama Menu"])
    code_col = col(["Menu Code", "menu_code", "MenuCode"])
    qty_col  = col(["Qty", "qty", "Quantity"])
    price_col= col(["Harga Jual", "Unit Price", "Price", "Harga"])
    rev_col  = col(["Grand Total", "grand_total", "Total", "Revenue"])
    date_col = col(["Sales Date", "Tanggal", "Date"])
    cust_col = col(["Loyalty Member Name", "Customer Name", "name_clean", "Nama"])

    if not menu_col or not rev_col:
        return pd.DataFrame()

    # Filter baris yang punya menu valid
    df = df[df[menu_col].notna() & (df[menu_col].astype(str).str.strip() != "")]
    if df.empty:
        return pd.DataFrame()

    # Normalisasi tipe
    df["_rev"]  = pd.to_numeric(df[rev_col],  errors="coerce").fillna(0)
    df["_qty"]  = pd.to_numeric(df[qty_col],  errors="coerce").fillna(0) if qty_col else 0
    df["_price"]= pd.to_numeric(df[price_col],errors="coerce").fillna(0) if price_col else 0
    df["_menu"] = df[menu_col].astype(str).str.strip()
    df["_cat"]  = df[cat_col].astype(str).str.strip() if cat_col else "Other"
    df["_code"] = df[code_col].astype(str).str.strip() if code_col else ""
    df["_date"] = pd.to_datetime(df[date_col], errors="coerce") if date_col else pd.NaT
    df["_cust"] = df[cust_col].astype(str).str.lower().str.strip() if cust_col else ""

    # Hitung total revenue keseluruhan untuk % share
    total_rev = df["_rev"].sum()

    # Group by category + menu
    grp = df.groupby(["_cat", "_menu", "_code"], as_index=False).agg(
        Total_Transactions = ("_rev", "count"),
        Total_Qty          = ("_qty", "sum"),
        Total_Revenue      = ("_rev", "sum"),
        Unique_Customers   = ("_cust", "nunique"),
        Avg_Price          = ("_price", "mean"),
        First_Date         = ("_date", "min"),
        Last_Date          = ("_date", "max"),
    )

    grp = grp.rename(columns={
        "_cat":  "Menu_Category",
        "_menu": "Menu",
        "_code": "Menu_Code",
    })

    grp["Pct_Revenue"]   = ((grp["Total_Revenue"] / total_rev * 100)
                            .round(1).fillna(0)) if total_rev > 0 else 0
    grp["Avg_Price"]     = grp["Avg_Price"].round(0).fillna(0)
    grp["First_Date"]    = grp["First_Date"].dt.strftime("%Y-%m-%d")
    grp["Last_Date"]     = grp["Last_Date"].dt.strftime("%Y-%m-%d")

    # Sort by revenue desc
    grp = grp.sort_values("Total_Revenue", ascending=False).reset_index(drop=True)

    # Reorder
    cols = ["Menu_Category","Menu","Menu_Code",
            "Total_Transactions","Total_Qty","Total_Revenue",
            "Avg_Price","Unique_Customers","Pct_Revenue",
            "First_Date","Last_Date"]
    grp = grp[[c for c in cols if c in grp.columns]]

    return grp


# ─────────────────────────────────────────────
# 12. PROGRAM PERFORMANCE TRACKER (legacy, keep for reference)
# ─────────────────────────────────────────────

def calculate_program_metrics(
    df_programs: pd.DataFrame,
    df_avm: pd.DataFrame = None,
    df_sales: pd.DataFrame = None,
) -> pd.DataFrame:
    """
    Hitung metrics per program dari data AVM dan ESB.

    df_programs kolom: Program_Name, Program_Type, Date_Start, Date_End, Cost
    df_avm kolom     : start_time (datetime), member_name, duration_hours, booking_code
    df_sales kolom   : Tanggal (datetime), name_clean, Grand Total

    Returns DataFrame dengan satu baris per program + calculated metrics.
    """
    if df_programs is None or df_programs.empty:
        return pd.DataFrame()

    results = []

    # Normalize AVM
    avm_ok = df_avm is not None and not df_avm.empty
    if avm_ok:
        df_avm = df_avm.copy()
        if "start_time" in df_avm.columns:
            df_avm["start_time"] = pd.to_datetime(df_avm["start_time"], errors="coerce")
        df_avm["_date"] = df_avm["start_time"].dt.normalize() if "start_time" in df_avm.columns else pd.NaT
        df_avm["_member"] = df_avm.get("member_name", pd.Series(dtype=str)).astype(str).str.lower().str.strip()

    # Normalize ESB
    sales_ok = df_sales is not None and not df_sales.empty
    if sales_ok:
        df_sales = df_sales.copy()
        if "Tanggal" in df_sales.columns:
            df_sales["Tanggal"] = pd.to_datetime(df_sales["Tanggal"], errors="coerce")
        df_sales["_date"] = df_sales["Tanggal"].dt.normalize()
        rev_col = next((c for c in ["Grand Total", "grand_total", "Revenue"] if c in df_sales.columns), None)

    # Kumpulkan semua partisipan per Program_Type untuk hitung repeat
    type_members: dict[str, list] = {}

    for _, prog in df_programs.iterrows():
        name      = str(prog.get("Program_Name", "")).strip()
        ptype     = str(prog.get("Program_Type", "Other")).strip()
        date_s    = prog.get("Date_Start")
        date_e    = prog.get("Date_End") if pd.notna(prog.get("Date_End")) else date_s
        cost      = float(prog.get("Cost", 0) or 0)
        notes     = str(prog.get("Notes", ""))

        if not name or pd.isna(date_s):
            continue

        # ── Dari AVM ─────────────────────────────────────────
        participants     = 0
        court_hours      = 0.0
        members_this_run = set()

        if avm_ok and pd.notna(date_s):
            mask = (df_avm["_date"] >= date_s) & (df_avm["_date"] <= date_e)
            avm_prog = df_avm[mask]

            members_this_run = set(avm_prog["_member"].dropna().unique())
            participants     = len(members_this_run)

            if "duration_hours" in avm_prog.columns:
                court_hours = float(avm_prog["duration_hours"].sum())
            elif "start_time" in avm_prog.columns and "end_time" in avm_prog.columns:
                avm_prog = avm_prog.copy()
                avm_prog["end_time"] = pd.to_datetime(avm_prog["end_time"], errors="coerce")
                court_hours = float(
                    ((avm_prog["end_time"] - avm_prog["start_time"])
                     .dt.total_seconds() / 3600).sum()
                )

        # Simpan ke type_members untuk repeat calculation
        if ptype not in type_members:
            type_members[ptype] = []
        type_members[ptype].append(members_this_run)

        # ── Dari ESB ─────────────────────────────────────────
        revenue = 0.0
        if sales_ok and rev_col and pd.notna(date_s):
            mask_s = (df_sales["_date"] >= date_s) & (df_sales["_date"] <= date_e)
            revenue = float(pd.to_numeric(df_sales.loc[mask_s, rev_col], errors="coerce").sum())

        # ROI
        roi = round(revenue / cost, 2) if cost > 0 else None

        results.append({
            "Program_Name":    name,
            "Program_Type":    ptype,
            "Date_Start":      date_s.strftime("%Y-%m-%d") if pd.notna(date_s) else "",
            "Date_End":        date_e.strftime("%Y-%m-%d") if pd.notna(date_e) else "",
            "Cost":            int(cost),
            "Participants":    participants,
            "Court_Hours":     round(court_hours, 1),
            "Revenue":         int(revenue),
            "ROI":             roi,
            "Notes":           notes,
            "_members":        members_this_run,   # internal, dihapus setelah repeat calc
        })

    if not results:
        return pd.DataFrame()

    df_out = pd.DataFrame(results)

    # ── Hitung Repeat Participants ─────────────────────────
    def count_repeat(row):
        ptype   = row["Program_Type"]
        members = row["_members"]
        runs    = type_members.get(ptype, [])
        if len(runs) < 2 or not members:
            return 0
        # member dianggap repeat jika muncul di >1 run Program_Type yang sama
        repeat_count = 0
        for m in members:
            appearances = sum(1 for run in runs if m in run)
            if appearances > 1:
                repeat_count += 1
        return repeat_count

    df_out["Repeat_Participants"] = df_out.apply(count_repeat, axis=1)
    df_out["Repeat_Rate_Pct"] = (
        (df_out["Repeat_Participants"] / df_out["Participants"].replace(0, np.nan)) * 100
    ).round(1).fillna(0)

    # Hapus kolom internal
    df_out = df_out.drop(columns=["_members"])

    # Reorder kolom
    col_order = [
        "Program_Name", "Program_Type", "Date_Start", "Date_End",
        "Cost", "Participants", "Repeat_Participants", "Repeat_Rate_Pct",
        "Court_Hours", "Revenue", "ROI", "Notes"
    ]
    df_out = df_out[[c for c in col_order if c in df_out.columns]]

    return df_out
