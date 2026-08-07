"""
drive_reader.py
Membaca file Excel terbaru (Sales & Membership) dari Google Drive folder.
"""
import io
import os
import pandas as pd
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2.service_account import Credentials
import json


SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Folder ID untuk benchmark occupancy kompetitor (padel_benchmark_*.xlsx)
OCCUPANCY_FOLDER_ID = os.environ.get(
    "DRIVE_FOLDER_ID_OCCUPANCY", "1sxptmj5IHQ-MqxyQaRPfhyv-itGJ6S24"
)
PPC_VENUE_KEYWORD = "Puri Padel"


def get_credentials():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    info = json.loads(creds_json)
    return Credentials.from_service_account_info(info, scopes=SCOPES)


def get_drive_service():
    return build("drive", "v3", credentials=get_credentials())


def get_latest_file(drive_service, folder_id, name_contains):
    """Ambil file terbaru di folder yang namanya mengandung string tertentu."""
    query = (
        f"'{folder_id}' in parents "
        f"and name contains '{name_contains}' "
        f"and mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' "
        f"and trashed=false"
    )
    result = drive_service.files().list(
        q=query,
        orderBy="createdTime desc",
        pageSize=1,
        fields="files(id, name, createdTime)",
    ).execute()
    files = result.get("files", [])
    return files[0] if files else None


def download_excel_from_drive(file_id):
    """Download file Excel dari Drive, return sebagai BytesIO."""
    drive_service = get_drive_service()
    request = drive_service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf


def read_sales_from_drive():
    """
    Baca file Sales terbaru dari Drive.
    Header di row index 10 (sesuai format ESB Loop).
    """
    folder_id = os.environ.get("DRIVE_FOLDER_ID")
    drive_service = get_drive_service()

    file_meta = get_latest_file(drive_service, folder_id, "Sales Recapitulation")
    if not file_meta:
        raise FileNotFoundError("Tidak ada file Sales di Drive folder")

    buf = download_excel_from_drive(file_meta["id"])
    df = pd.read_excel(buf, sheet_name="Report", header=10)
    df["Sales Date"] = pd.to_datetime(df["Sales Date"], errors="coerce")
    df["name_clean"] = df["Loyalty Member Name"].str.lower().str.strip()
    return df, file_meta["name"]


def read_membership_from_drive():
    """Baca file Membership List terbaru dari Drive."""
    folder_id = os.environ.get("DRIVE_FOLDER_ID")
    drive_service = get_drive_service()

    file_meta = get_latest_file(drive_service, folder_id, "Membership List")
    if not file_meta:
        raise FileNotFoundError("Tidak ada file Membership List di Drive folder")

    buf = download_excel_from_drive(file_meta["id"])
    df = pd.read_excel(buf, sheet_name="Sheet1")
    df["name_clean"] = df["Member Name"].str.lower().str.strip()
    df["Kode ads"] = df["Kode ads"].fillna("").str.strip()
    return df


def list_benchmark_files(drive_service, folder_id, limit=30):
    """List file padel_benchmark_*.xlsx terbaru di folder, sorted by name desc."""
    query = (
        f"'{folder_id}' in parents "
        f"and name contains 'padel_benchmark_' "
        f"and mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' "
        f"and trashed=false"
    )
    result = drive_service.files().list(
        q=query,
        orderBy="name desc",
        pageSize=limit,
        fields="files(id, name)",
    ).execute()
    return result.get("files", [])


def _date_from_benchmark_name(filename: str) -> str:
    """Extract YYYY-MM-DD dari nama file padel_benchmark_YYYY-MM-DD_HHMM.xlsx."""
    try:
        part = filename.split("padel_benchmark_")[1]
        return part[:10]
    except Exception:
        return ""


def read_occupancy_benchmark(trend_days: int = 14) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Baca data occupancy benchmark dari Drive folder.

    Returns:
      df_competitors — snapshot terbaru, semua venue (untuk tabel kompetitor)
      df_ppc_trend  — trend PPC selama trend_days hari terakhir
    """
    drive_service = get_drive_service()
    files = list_benchmark_files(drive_service, OCCUPANCY_FOLDER_ID, limit=max(trend_days + 2, 30))

    if not files:
        return pd.DataFrame(), pd.DataFrame()

    # ── Competitor snapshot dari file terbaru ──────────
    latest = files[0]
    snapshot_date = _date_from_benchmark_name(latest["name"])

    buf = download_excel_from_drive(latest["id"])
    try:
        df_occ = pd.read_excel(buf, sheet_name="Occupancy")
        df_occ.columns = [str(c).strip() for c in df_occ.columns]
        buf.seek(0)
        df_dv = pd.read_excel(buf, sheet_name="Demand & Value")
        df_dv.columns = [str(c).strip() for c in df_dv.columns]
    except Exception as e:
        print(f"Warning: error reading latest benchmark: {e}")
        df_occ = pd.DataFrame()
        df_dv = pd.DataFrame()

    if not df_occ.empty and not df_dv.empty:
        merge_cols = [c for c in ["Venue", "Rev Captured (M IDR)", "Rev Ceiling (M IDR)", "Value Capture %", "Value Index"]
                      if c in df_dv.columns]
        df_competitors = pd.merge(df_occ, df_dv[merge_cols], on="Venue", how="left")
        df_competitors["snapshot_date"] = snapshot_date
    else:
        df_competitors = df_occ.copy()
        df_competitors["snapshot_date"] = snapshot_date

    # ── PPC trend dari trend_days file terbaru ─────────
    ppc_rows = []
    for file in files[:trend_days]:
        date_str = _date_from_benchmark_name(file["name"])
        try:
            fbuf = download_excel_from_drive(file["id"])
            df_day = pd.read_excel(fbuf, sheet_name="Occupancy")
            df_day.columns = [str(c).strip() for c in df_day.columns]

            ppc = df_day[df_day["Venue"].str.contains(PPC_VENUE_KEYWORD, na=False)]
            if not ppc.empty:
                row = ppc.iloc[0].to_dict()
                row["date"] = date_str
                ppc_rows.append(row)
        except Exception as e:
            print(f"Warning: benchmark {date_str} error: {e}")

    if ppc_rows:
        df_ppc_trend = pd.DataFrame(ppc_rows).sort_values("date").reset_index(drop=True)
    else:
        df_ppc_trend = pd.DataFrame()

    return df_competitors, df_ppc_trend


def read_ads_from_drive():
    """
    Baca file Meta Ads export terbaru dari Drive.
    Meta Ads scheduled export biasanya bernama "Facebook Ads..." atau "Meta Ads..."
    dengan breakdown harian — tiap baris = 1 campaign/adset di 1 tanggal.

    Env var:
      DRIVE_FOLDER_ID_ADS — folder tempat Meta Ads export masuk.
      Jika tidak diset, fallback ke DRIVE_FOLDER_ID.

    Kolom kunci yang diharapkan dari Meta export:
      - Campaign name (atau Ad name)
      - Date (tanggal, untuk daily breakdown)
      - Amount spent (IDR)
      - Results
      - Cost per result
      - Impressions
      - CTR
      - CPM
      - Status / Delivery (ACTIVE / PAUSED / dll)
    """
    folder_id = os.environ.get("DRIVE_FOLDER_ID_ADS") or os.environ.get("DRIVE_FOLDER_ID")
    drive_service = get_drive_service()

    # Coba beberapa pola nama file Meta export
    file_meta = None
    for keyword in ("Meta Ads", "Facebook Ads", "Ads Export", "meta_ads", "facebook_ads"):
        file_meta = get_latest_file(drive_service, folder_id, keyword)
        if file_meta:
            break

    if not file_meta:
        # Jika tidak ketemu, return DataFrame kosong (tidak error — pipeline tetap jalan)
        import warnings
        warnings.warn("Tidak ada file Meta Ads di Drive folder — campaign data akan kosong.")
        return pd.DataFrame()

    buf = download_excel_from_drive(file_meta["id"])

    # Meta export bisa berformat .xlsx; coba baca sheet pertama
    try:
        df = pd.read_excel(buf, sheet_name=0)
    except Exception:
        buf.seek(0)
        df = pd.read_csv(buf)

    # Strip whitespace dari nama kolom
    df.columns = [str(c).strip() for c in df.columns]

    # Parse kolom tanggal jika ada
    date_col = next((c for c in df.columns if c.lower() in ("date", "day", "tanggal", "reporting starts")), None)
    if date_col:
        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.rename(columns={date_col: "Date"})

    return df
