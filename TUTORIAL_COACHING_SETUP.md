# Tutorial: Setup PPC Coaching Log Integration

Panduan ini menjelaskan cara mengaktifkan fitur coaching analytics di PPC Dashboard.
Ikuti urutan ini dengan tepat — jangan skip langkah.

---

## Gambaran Besar

```
[Coaching Log Sheet]  ──►  [Pipeline]  ──►  [Hub Sheet]  ──►  [Dashboard]
  (input admin)              (Vercel)         (output)          (tampilan)
```

Kita akan:
1. Push kode baru ke GitHub
2. Tambah env var baru di Vercel
3. Setup Google Sheet coaching (sekali saja)
4. Migrasi data lama April–Juli
5. Jalankan pipeline dan cek dashboard

---

## Prasyarat

Pastikan kamu punya:
- [ ] Terminal / Command Prompt terbuka di folder project (`ppc-dashboard-code`)
- [ ] Python 3.10+ sudah terinstall (`python --version`)
- [ ] File `credentials.json` dari Google Cloud Console (Service Account)
- [ ] Akses ke GitHub dan Vercel project

---

## LANGKAH 1 — Push Kode ke GitHub

### 1.1 Buka terminal di folder project

```bash
cd path/to/ppc-dashboard-code
```

### 1.2 Cek file yang berubah

```bash
git status
```

Kamu akan melihat file-file ini berubah:
- `lib/sheets_client.py`
- `lib/processor.py`
- `api/process.py`
- `api/dashboard.html`
- `scripts/setup_coaching_sheet.py` *(baru)*
- `scripts/migrate_coaching_data.py` *(baru)*

### 1.3 Stage semua perubahan

```bash
git add lib/sheets_client.py lib/processor.py api/process.py api/dashboard.html scripts/setup_coaching_sheet.py scripts/migrate_coaching_data.py
```

### 1.4 Commit

```bash
git commit -m "feat: add coaching analytics integration"
```

### 1.5 Push ke GitHub

```bash
git push origin main
```

✅ Vercel akan otomatis deploy ulang setelah push ini.

---

## LANGKAH 2 — Tambah Environment Variable di Vercel

### 2.1 Buka Vercel Dashboard

Pergi ke: https://vercel.com → pilih project PPC Dashboard

### 2.2 Masuk ke Settings → Environment Variables

Klik **Settings** (tab atas) → klik **Environment Variables** di sidebar kiri.

### 2.3 Tambah variabel baru

Klik tombol **Add New** dan isi:

| Field | Nilai |
|-------|-------|
| **Name** | `COACHING_SHEET_ID` |
| **Value** | `1u43tbbA-wYPTpDsEBv-DYiHjmy4VLxOcdfmLo_SVgtw` |
| **Environment** | ✅ Production, ✅ Preview, ✅ Development |

Klik **Save**.

### 2.4 Redeploy

Setelah menyimpan env var, kamu perlu redeploy agar perubahan aktif:

Klik tab **Deployments** → klik deployment terbaru → klik tombol **...** (titik tiga) → **Redeploy**.

✅ Tunggu hingga status menjadi **Ready**.

---

## LANGKAH 3 — Setup Google Sheet Coaching (Sekali Saja)

Langkah ini membuat tab `raw_coaching` dengan format yang benar di sheet PPC Coaching Log.

### 3.1 Siapkan credentials

Buka terminal dan set environment variable `GOOGLE_CREDENTIALS`:

**Mac/Linux:**
```bash
export GOOGLE_CREDENTIALS=$(cat /path/to/credentials.json)
```

**Windows (Command Prompt):**
```cmd
set /p GOOGLE_CREDENTIALS=< C:\path\to\credentials.json
```

**Windows (PowerShell):**
```powershell
$env:GOOGLE_CREDENTIALS = Get-Content "C:\path\to\credentials.json" -Raw
```

### 3.2 Jalankan script setup

```bash
python scripts/setup_coaching_sheet.py
```

Output yang diharapkan:
```
Opened: PPC Coaching Log (1u43tbbA-...)
Tab 'raw_coaching' dibuat
Headers + 4 sample rows ditulis
Formatting, validation, conditional formatting: DONE
Tab README dibuat dengan panduan pengisian

✅ Setup selesai!
   Sheet: https://docs.google.com/spreadsheets/d/1u43tbbA-.../edit
```

### 3.3 Verifikasi di Google Sheets

Buka link sheet yang muncul di output. Kamu akan melihat:
- Tab `raw_coaching` dengan header berwarna hijau tua
- 4 baris sample data
- Dropdown di kolom `Package_Type` dan `Status`
- Tab `README` dengan panduan pengisian untuk admin

> **Catatan:** Sample data bisa dihapus setelah migrasi data lama selesai.

---

## LANGKAH 4 — Migrasi Data Lama (April–Juli 2026)

Langkah ini memindahkan data dari sheet lama (format per-tab per-bulan) ke format flat baru.

### 4.1 Preview dulu (tanpa menulis ke sheet)

```bash
python scripts/migrate_coaching_data.py --dry-run
```

Output yang diharapkan:
```
Membaca sheet lama: 1dtmKhpbAeVu-...
Tab ditemukan: ['April', 'Mei', 'Juni', 'Juli', 'Agustus']
  Parsing tab 'April' (bulan 04)...
    → 42 sesi ditemukan
  Parsing tab 'Mei' (bulan 05)...
    → 38 sesi ditemukan
  ...

Total: 156 sesi dari semua bulan

=== PREVIEW 10 BARIS PERTAMA ===
Date         Member_Name            Package_Type    Part  Start  End    Rem  Status
-----------  ---------------------  --------------  ----  -----  -----  ---  ------
2026-04-01   Gabriela               Bundling_4x        1  08:00  09:00    2  Done
...

[DRY RUN] Tidak ada data yang ditulis ke sheet baru.
```

### 4.2 Periksa hasilnya

Cek apakah:
- Nama member sudah benar (sesuai nama di ESB)
- Tanggal format `YYYY-MM-DD` sudah benar
- Package_Type sudah ternormalisasi (`Free_Coaching`, `Bundling_4x`, dst)

Jika ada yang salah, beritahu dan kita perbaiki script-nya sebelum lanjut.

### 4.3 Jalankan migrasi sesungguhnya

```bash
python scripts/migrate_coaching_data.py
```

Output yang diharapkan:
```
...
Total: 156 sesi dari semua bulan
...
Menulis ke sheet baru: 1u43tbbA-...
  Data existing di raw_coaching: 4 baris (sample rows)
  Duplikat dilewati: 0
  Baris baru akan ditambahkan: 156
  Chunk 1: 156 baris ditulis (total up to row 161)

✅ Migrasi selesai! 156 sesi berhasil dimigrasikan.
```

### 4.4 Hapus sample rows

Buka sheet `raw_coaching`, hapus baris 2–5 (4 sample rows dari setup).

> Cara mudah: klik nomor baris 2, shift+klik baris 5, klik kanan → Delete rows.

---

## LANGKAH 5 — Jalankan Pipeline & Cek Dashboard

### 5.1 Trigger pipeline manual

Buka browser, akses URL ini (ganti domain dengan domain Vercel kamu):

```
https://ppc-dashboard.vercel.app/api/process
```

Atau bisa trigger lewat Make.com seperti biasa.

### 5.2 Cek log pipeline

Response akan berupa JSON dengan `log` array. Pastikan ada baris:
```json
"Reading coaching data from PPC Coaching Log sheet...",
"  → 156 coaching sessions loaded",
"  → Coaching analytics written: 4 months, 12 active bundles",
```

Jika ada error, cek bagian `⚠️ Coaching error:` di log.

### 5.3 Buka Dashboard

Buka dashboard dan pergi ke halaman **Members**. Kamu akan melihat:
- Pill filter preferensi baru (Ball Machine, Coaching, Morning Player, dst)
- Chart distribusi produk per member
- Kolom `Preferensi` dengan badge di tabel member

---

## Troubleshooting

### ❌ "GOOGLE_CREDENTIALS env var tidak ditemukan"

Kamu belum set env var. Ulangi Langkah 3.1.

### ❌ "COACHING_SHEET_ID belum diset / sheet tidak ditemukan"

Pastikan `COACHING_SHEET_ID` sudah benar: `1u43tbbA-wYPTpDsEBv-DYiHjmy4VLxOcdfmLo_SVgtw`

Juga pastikan service account sudah diberi akses ke sheet tersebut:
1. Buka sheet coaching di Google Drive
2. Klik Share
3. Tambah email service account (ada di `credentials.json`, field `client_email`)
4. Beri akses **Editor**

### ❌ "Tab 'raw_coaching' belum ada"

Kamu belum jalankan Langkah 3. Jalankan `setup_coaching_sheet.py` dulu.

### ❌ Dashboard coaching tidak muncul

Pipeline belum dijalankan setelah deploy. Trigger manual lewat `/api/process`.

### ❌ Data coaching bulan Agustus tidak ada

Sheet lama (`1dtmKhpbAeVu-...`) mungkin tab Agustus kosong atau belum diisi.
Isi langsung di sheet `raw_coaching` baru.

---

## Setelah Setup: Cara Input Data Coaching Rutin

Admin cukup:
1. Buka **PPC Coaching Log** di Google Drive
2. Pergi ke tab `raw_coaching`
3. Tambah baris baru di bawah data terakhir
4. Isi kolom sesuai panduan di tab `README`

Pipeline akan otomatis baca data baru setiap hari pukul 06.00 (atau saat di-trigger manual).

---

## Ringkasan File yang Berubah

| File | Keterangan |
|------|-----------|
| `lib/sheets_client.py` | Tambah koneksi ke Coaching Log sheet |
| `lib/processor.py` | Tambah `calculate_coaching()` dan `calculate_member_preferences()` |
| `api/process.py` | Tambah coaching step di pipeline + API response |
| `api/dashboard.html` | Members page dengan filter preferensi dan chart produk |
| `scripts/setup_coaching_sheet.py` | **BARU** — one-time setup formatting sheet |
| `scripts/migrate_coaching_data.py` | **BARU** — migrasi data April–Juli |
