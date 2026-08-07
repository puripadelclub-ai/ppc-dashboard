"""
lib/avm_client.py
Fetch booking data dari AVM (Ayo Venue Management) API.

Venue: PPC - Puri Padel Club
Venue ID: 2780
API Base: https://api-new.ayo.co.id/api/v2/venue_booking/path

Env var yang dibutuhkan:
  AVM_MOBILE_TOKEN — token dari avm.ayo.co.id (lihat tutorial untuk cara dapat)
"""
import os
import requests
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed


AVM_BASE = "https://api-new.ayo.co.id/api/v2/venue_booking/path"
VENUE_ID = "2780"

# ── PPC venue capacity constants ──────────────────────────
# 2 lapangan, operasional 06:00–22:00 (last booking 21:00, selesai 22:00)
# Morning   06:00–12:00 → 6 jam/lapangan × 2 = 12 total slot
# Afternoon 12:00–18:00 → 6 jam/lapangan × 2 = 12 total slot
# Evening   18:00–22:00 → 4 jam/lapangan × 2 = 8 total slot
PPC_COURTS              = 2
PPC_MORNING_TOTAL       = PPC_COURTS * 6   # 12
PPC_AFTERNOON_TOTAL     = PPC_COURTS * 6   # 12
PPC_EVENING_TOTAL       = PPC_COURTS * 4   # 8
PPC_TOTAL_SLOTS         = PPC_MORNING_TOTAL + PPC_AFTERNOON_TOTAL + PPC_EVENING_TOTAL  # 32

# Booking code legend sesuai keterangan dari owner PPC
BOOKING_CODES = {
    "BP1": "Ball Machine Baru",
    "BP2": "Ball Machine Lama",
    "BP":  "Ball Machine",
    "CBA": "Coach Bani",
    "CO":  "Compliment",
    "R":   "Reclub Aktif",
    "RX":  "Reclub Tidak Aktif",
    "FT":  "Free Trial",
}


def get_token():
    token = os.environ.get("AVM_MOBILE_TOKEN", "")
    if not token:
        raise ValueError("AVM_MOBILE_TOKEN belum diset di environment variables")
    return token


def _parse_user_name(user_name: str) -> tuple[str, str]:
    """
    Parse user_name dari AVM API.
    Format: 'BP1/CUSTOMER NAME' → ('BP1', 'CUSTOMER NAME')
    Regular: 'John Doe'         → ('REGULAR', 'John Doe')
    """
    if not user_name:
        return ("UNKNOWN", "")
    user_name = user_name.strip()
    if "/" in user_name:
        parts = user_name.split("/", 1)
        code = parts[0].strip().upper()
        name = parts[1].strip()
        return (code, name)
    return ("REGULAR", user_name)


def _period(start_time: str) -> str:
    """Klasifikasi slot waktu ke Morning/Afternoon/Evening.
    Morning   06:00–12:00
    Afternoon 12:00–18:00
    Evening   18:00–22:00
    """
    try:
        hour = int(start_time.split(":")[0])
        if hour < 12:
            return "Morning"
        elif hour < 18:
            return "Afternoon"
        else:
            return "Evening"
    except Exception:
        return "Unknown"


def _parse_duration(start_time: str, end_time: str) -> float:
    """Hitung durasi booking dalam jam dari string HH:MM."""
    try:
        sh = int(start_time.split(":")[0])
        sm = int(start_time.split(":")[1]) if len(start_time.split(":")) > 1 else 0
        eh = int(end_time.split(":")[0])
        em = int(end_time.split(":")[1]) if len(end_time.split(":")) > 1 else 0
        duration = (eh * 60 + em - sh * 60 - sm) / 60
        return max(0.5, min(float(duration), 4.0))  # sanity: 0.5–4 jam
    except Exception:
        return 1.0  # default 1 jam


def fetch_bookings_by_date(date_str: str) -> list[dict]:
    """
    Fetch semua booking untuk satu tanggal dari endpoint reservations-calendar.
    Returns list of booking dicts.
    """
    token = get_token()
    url = (
        f"{AVM_BASE}?url_path=venue/{VENUE_ID}/reservations-calendar"
        f"&mobile_token={token}&date={date_str}"
    )

    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if data.get("error"):
        raise Exception(f"AVM API error: {data.get('message')}")

    rows = []
    for court in data.get("data", []):
        court_name = court.get("field_name", "")
        for booking in court.get("data", []):
            user_name = booking.get("user_name", "")
            booking_code, customer_name = _parse_user_name(user_name)
            booking_type = BOOKING_CODES.get(booking_code, booking_code if booking_code != "REGULAR" else "Regular")

            start_time = booking.get("start_time", "")
            end_time   = booking.get("end_time", "")

            rows.append({
                "date":             date_str,
                "court":            court_name,
                "start_time":       start_time,
                "end_time":         end_time,
                "period":           _period(start_time),
                "booking_code":     booking_code,
                "booking_type":     booking_type,
                "customer_name":    customer_name,
                "total_price":      booking.get("total_price", 0),
                "payment_method":   booking.get("payment_method", ""),
                "reservation_type": booking.get("reservation_type", ""),
                "final_status":     booking.get("final_status", ""),
                "avm_id":           booking.get("id", ""),
            })
    return rows


def fetch_bookings_range(days_back: int = 60, days_forward: int = 7, max_workers: int = 10) -> pd.DataFrame:
    """
    Fetch bookings untuk range tanggal secara parallel (lebih cepat dari sequential).
    hari ini - days_back  s/d  hari ini + days_forward
    max_workers: jumlah thread paralel (default 10)
    """
    today = datetime.now().date()
    start = today - timedelta(days=days_back)
    end   = today + timedelta(days=days_forward)

    dates = []
    current = start
    while current <= end:
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)

    all_rows = []
    errors   = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(fetch_bookings_by_date, d): d for d in dates}
        for future in as_completed(future_map):
            date_str = future_map[future]
            try:
                all_rows.extend(future.result())
            except Exception as e:
                errors.append(f"{date_str}: {e}")

    if errors:
        print(f"AVM fetch warnings: {len(errors)} dates failed")
        for err in errors[:5]:
            print(f"  {err}")

    if not all_rows:
        return pd.DataFrame(columns=[
            "date", "court", "start_time", "end_time", "period",
            "booking_code", "booking_type", "customer_name",
            "total_price", "payment_method", "reservation_type",
            "final_status", "avm_id",
        ])

    df = pd.DataFrame(all_rows)
    df["date"] = pd.to_datetime(df["date"])
    df["total_price"] = pd.to_numeric(df["total_price"], errors="coerce").fillna(0)
    return df.sort_values("date").reset_index(drop=True)


def calculate_avm_summary(df: pd.DataFrame) -> pd.DataFrame:
    """
    Hitung ringkasan harian dari raw AVM data:
    - Total bookings & revenue per hari
    - Breakdown per booking code
    - Breakdown per period (Morning/Afternoon/Evening)
    - Occupancy % dihitung dari durasi booking vs total slot tersedia
      (PPC: 2 lapangan, 12 Morning / 12 Afternoon / 8 Evening = 32 jam/hari)
    """
    if df.empty:
        return pd.DataFrame()

    df = df.copy()

    # Pastikan tipe data benar (penting saat data dari Sheets dibaca ulang)
    df["total_price"] = pd.to_numeric(df["total_price"], errors="coerce").fillna(0)
    # Recompute period dari start_time agar data historis selalu benar
    df["period"] = df["start_time"].apply(_period)

    # Hitung durasi tiap booking dan assign ke period
    df["duration_h"]   = df.apply(lambda r: _parse_duration(r["start_time"], r["end_time"]), axis=1)
    df["morning_h"]    = df.apply(lambda r: r["duration_h"] if r["period"] == "Morning"   else 0, axis=1)
    df["afternoon_h"]  = df.apply(lambda r: r["duration_h"] if r["period"] == "Afternoon" else 0, axis=1)
    df["evening_h"]    = df.apply(lambda r: r["duration_h"] if r["period"] == "Evening"   else 0, axis=1)

    # Group per hari
    daily = df.groupby("date").agg(
        total_bookings=("avm_id", "count"),
        total_revenue=("total_price", "sum"),
        regular_bookings=("booking_code", lambda x: (x == "REGULAR").sum()),
        bp1_bookings=("booking_code", lambda x: (x == "BP1").sum()),
        bp2_bookings=("booking_code", lambda x: (x == "BP2").sum()),
        bp_bookings=("booking_code", lambda x: (x == "BP").sum()),
        cba_bookings=("booking_code", lambda x: (x == "CBA").sum()),
        co_bookings=("booking_code", lambda x: (x == "CO").sum()),
        r_bookings=("booking_code", lambda x: (x == "R").sum()),
        rx_bookings=("booking_code", lambda x: (x == "RX").sum()),
        ft_bookings=("booking_code", lambda x: (x == "FT").sum()),
        morning_bookings=("period",   lambda x: (x == "Morning").sum()),
        afternoon_bookings=("period", lambda x: (x == "Afternoon").sum()),
        evening_bookings=("period",   lambda x: (x == "Evening").sum()),
        morning_booked=("morning_h",   "sum"),
        afternoon_booked=("afternoon_h", "sum"),
        evening_booked=("evening_h",   "sum"),
    ).reset_index()

    daily["date"] = daily["date"].dt.strftime("%Y-%m-%d")

    # Derived booking columns
    daily["machine_bookings"]       = daily["bp1_bookings"] + daily["bp2_bookings"] + daily["bp_bookings"]
    daily["coaching_bookings"]      = daily["cba_bookings"]
    daily["complimentary_bookings"] = daily["co_bookings"] + daily["ft_bookings"]
    daily["paid_bookings"]          = daily["regular_bookings"] + daily["machine_bookings"]

    # ── Occupancy % (dari durasi booking vs total slot lapangan) ──────────
    daily["Morning Booked Hrs"]   = daily["morning_booked"].round(1)
    daily["Morning Total Hrs"]    = PPC_MORNING_TOTAL
    daily["Morning Occ %"]        = (daily["morning_booked"] / PPC_MORNING_TOTAL).round(4)

    daily["Afternoon Booked Hrs"] = daily["afternoon_booked"].round(1)
    daily["Afternoon Total Hrs"]  = PPC_AFTERNOON_TOTAL
    daily["Afternoon Occ %"]      = (daily["afternoon_booked"] / PPC_AFTERNOON_TOTAL).round(4)

    daily["Evening Booked Hrs"]   = daily["evening_booked"].round(1)
    daily["Evening Total Hrs"]    = PPC_EVENING_TOTAL
    daily["Evening Occ %"]        = (daily["evening_booked"] / PPC_EVENING_TOTAL).round(4)

    daily["Booked Hrs"]           = (daily["morning_booked"] + daily["afternoon_booked"] + daily["evening_booked"]).round(1)
    daily["Total Hrs"]            = PPC_TOTAL_SLOTS
    daily["Overall Occ %"]        = (daily["Booked Hrs"] / PPC_TOTAL_SLOTS).round(4)

    # Hapus kolom temp
    daily = daily.drop(columns=["morning_booked", "afternoon_booked", "evening_booked"])

    return daily
