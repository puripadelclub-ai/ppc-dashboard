"""
esb_client.py
Download "Sales Recapitulation Detail Report" dari ESB Core (esbcore.co.id) tanpa browser.

Alurnya sama persis dengan klik manual di website:
1. POST /site/check-session  → ESB memutuskan perlu CAPTCHA atau tidak
2. POST /site/login          → dapat session cookie
3. POST form report ?downloadReport=true → ESB memasukkan file ke "Reporting Queue"
4. Poll /site/get-data-report-queue sampai file baru selesai dibuat
5. GET /download/s3-report-file?fileUrl=... → isi file xlsx

Kalau ESB meminta CAPTCHA (Cloudflare Turnstile), robot BERHENTI dengan EsbError.
CAPTCHA tidak dicoba ditembus — login manual di browser yang harus menyelesaikannya.
"""
import html
import json
import os
import re
import time
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

BASE_URL = "https://esbcore.co.id"
REPORT_PATH = "/report/report-sales-recapitulation-detail"
REPORT_NAME = "Sales Recapitulation Detail Report"
FIELD = "SalesRecapitulationDetailReport[{}]"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0 Safari/537.36"
)


class EsbError(RuntimeError):
    pass


class _FormParser(HTMLParser):
    """Ambil nilai default semua field di <form id=form_id>, seperti yang dikirim browser."""

    def __init__(self, form_id: str):
        super().__init__()
        self.form_id = form_id
        self.in_form = False
        self.fields: list[tuple[str, str]] = []
        self._select = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.in_form = a.get("id") == self.form_id
            return
        if not self.in_form:
            return
        name = a.get("name")
        if tag == "input" and name:
            kind = (a.get("type") or "text").lower()
            if kind in ("button", "submit", "reset", "image", "file"):
                return
            if kind in ("checkbox", "radio") and "checked" not in a:
                return
            self.fields.append((name, a.get("value") or ""))
        elif tag == "select" and name:
            self._select = {"name": name, "multiple": "multiple" in a, "selected": [], "first": None}
        elif tag == "option" and self._select is not None:
            value = a.get("value") or ""
            if self._select["first"] is None:
                self._select["first"] = value
            if "selected" in a:
                self._select["selected"].append(value)

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False
        elif tag == "select" and self._select is not None:
            sel = self._select
            values = sel["selected"] or ([] if sel["multiple"] or sel["first"] is None else [sel["first"]])
            self.fields.extend((sel["name"], v) for v in values)
            self._select = None


def _form_fields(page_html: str, form_id: str) -> list[tuple[str, str]]:
    parser = _FormParser(form_id)
    parser.feed(page_html)
    if not parser.fields:
        raise EsbError(f"Form '{form_id}' tidak ditemukan di halaman ESB (tampilan website berubah?)")
    return parser.fields


def _csrf(page_html: str) -> tuple[str, str]:
    param = re.search(r'<meta name="csrf-param" content="([^"]+)"', page_html)
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page_html)
    if not (param and token):
        raise EsbError("CSRF token ESB tidak ditemukan (tampilan website berubah?)")
    return param.group(1), token.group(1)


def _override(fields: list[tuple[str, str]], values: dict) -> list[tuple[str, str]]:
    """Ganti nilai field yang ada di `values`; tambahkan kalau belum ada."""
    out = [(k, values[k] if k in values else v) for k, v in fields]
    present = {k for k, _ in fields}
    out.extend((k, v) for k, v in values.items() if k not in present)
    return out


class EsbClient:
    def __init__(self, username: str = None, password: str = None, timeout: int = 60):
        self.username = (username or os.environ.get("ESB_USERNAME", "")).strip()
        self.password = (password or os.environ.get("ESB_PASSWORD", "")).strip()
        if not self.username or not self.password:
            raise EsbError("ESB_USERNAME / ESB_PASSWORD belum diset di environment variables")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def _page(self, path: str) -> requests.Response:
        resp = self.session.get(BASE_URL + path, timeout=self.timeout)
        resp.raise_for_status()
        return resp

    @staticmethod
    def _ajax_headers(token: str) -> dict:
        return {"X-CSRF-Token": token, "X-Requested-With": "XMLHttpRequest"}

    # ── 1-2. Login ─────────────────────────────────────────────────────────
    def login(self):
        page = self._page("/site/login")
        param, token = _csrf(page.text)

        resp = self.session.post(
            BASE_URL + "/site/check-session",
            headers=self._ajax_headers(token),
            data={"username": self.username, "password": self.password, "challengeToken": ""},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        info = json.loads(resp.text)
        if isinstance(info, str):
            info = json.loads(info)
        info = info[0] if isinstance(info, list) and info else {}

        # Urutan pengecekan mengikuti JavaScript di halaman login ESB
        if info.get("requiredChallenges"):
            raise EsbError(
                "ESB meminta verifikasi CAPTCHA (Cloudflare Turnstile) sehingga robot berhenti. "
                "Untuk sementara download manual dari browser."
            )
        if str(info.get("forceSuspend")) == "1" or str(info.get("flagSuspend")) == "1":
            raise EsbError("Akun ESB ditangguhkan — cek status tagihan ESB")
        count_user, active = str(info.get("countUser")), str(info.get("flagActive"))
        if count_user == "0" or (active == "0" and count_user == "1"):
            raise EsbError("Login ESB gagal — username/password salah atau user nonaktif")

        fields = _override(_form_fields(page.text, "login-form"), {
            param: token,
            "LoginForm[username]": self.username,
            "LoginForm[password]": self.password,
            "cf-turnstile-response": "",
        })
        resp = self.session.post(BASE_URL + "/site/login", data=fields, timeout=self.timeout)
        resp.raise_for_status()
        if "/site/login" in resp.url:
            raise EsbError("Login ESB ditolak (kembali ke halaman login)")

    # ── 3. Minta report masuk antrian ─────────────────────────────────────
    def request_report(self, date_from: date, date_to: date) -> set:
        """Return nama file yang SUDAH ada di antrian sebelum request (untuk mengenali file baru)."""
        page = self._page(REPORT_PATH)
        if "/site/login" in page.url:
            raise EsbError("Session ESB tidak valid (diarahkan ke halaman login)")
        param, token = _csrf(page.text)
        d_from, d_to = date_from.strftime("%d-%m-%Y"), date_to.strftime("%d-%m-%Y")
        fields = _override(_form_fields(page.text, "report-form"), {
            param: token,
            FIELD.format("reportDate"): f"{d_from} - {d_to}",
            FIELD.format("dateFrom"): d_from,
            FIELD.format("dateTo"): d_to,
        })

        existing = {row["name"] for row in self.queue()}
        resp = self.session.post(
            BASE_URL + REPORT_PATH,
            params={"downloadReport": "true"},
            headers=self._ajax_headers(token),
            data=fields,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return existing

    # ── 4. Reporting Queue ────────────────────────────────────────────────
    def queue(self) -> list[dict]:
        resp = self.session.get(
            BASE_URL + "/site/get-data-report-queue",
            params={"draw": 1, "start": 0, "length": 10},
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        rows = []
        for row in resp.json().get("data", []):
            text = html.unescape(re.sub(r"<[^>]+>", " ", row.get("fileAndMessage") or ""))
            name = re.sub(r"\s+", " ", text.split("File expired")[0]).strip()
            link = re.search(r'href="([^"]*s3-report-file[^"]*)"', row.get("btnAction") or "")
            rows.append({"name": name, "href": html.unescape(link.group(1)) if link else None})
        return rows

    def wait_for_file(self, existing: set, timeout: int = 90, interval: int = 3) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for row in self.queue():
                if row["name"].startswith(REPORT_NAME) and row["name"] not in existing and row["href"]:
                    return row["href"]
            time.sleep(interval)
        raise EsbError(f"File report tidak muncul di Reporting Queue ESB setelah {timeout} detik")

    # ── 5. Download ───────────────────────────────────────────────────────
    def download(self, href: str) -> bytes:
        resp = self.session.get(urljoin(BASE_URL, href), timeout=120)
        resp.raise_for_status()
        if not resp.content.startswith(b"PK"):
            raise EsbError("File dari ESB bukan xlsx")
        return resp.content


def fetch_sales_recap_xlsx(date_from: date, date_to: date) -> bytes:
    """Login ke ESB lalu download Sales Recapitulation Detail Report untuk periode tsb."""
    client = EsbClient()
    client.login()
    existing = client.request_report(date_from, date_to)
    href = client.wait_for_file(existing)
    return client.download(href)
