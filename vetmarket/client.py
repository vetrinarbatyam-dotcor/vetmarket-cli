"""HTTP client for vetmarket.co.il (ASP.NET WebForms).

Uses a persistent requests.Session and handles ViewState/EVENTVALIDATION/etc.
Sessions cookies are pickled to disk so we don't re-login on every CLI call.
"""
from __future__ import annotations
import json
import pickle
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .config import BASE_URL, USERNAME, PASSWORD, USER_AGENT, SESSION_PATH, HTML_CACHE


ASPNET_HIDDEN_FIELDS = (
    "__VIEWSTATE",
    "__VIEWSTATEGENERATOR",
    "__EVENTVALIDATION",
    "__EVENTTARGET",
    "__EVENTARGUMENT",
    "__VIEWSTATEENCRYPTED",
    "__PREVIOUSPAGE",
    "__LASTFOCUS",
)


class VetmarketClient:
    def __init__(self, debug: bool = False):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "he-IL,he;q=0.9,en;q=0.8",
        })
        self.debug = debug
        self._logged_in = False
        self._load_session()

    # --- session persistence ---

    def _load_session(self):
        if SESSION_PATH.exists():
            try:
                with open(SESSION_PATH, "rb") as f:
                    cookies = pickle.load(f)
                    self.session.cookies.update(cookies)
                    self._logged_in = True
            except Exception:
                pass

    def _save_session(self):
        with open(SESSION_PATH, "wb") as f:
            pickle.dump(self.session.cookies, f)

    # --- HTTP ---

    def get(self, path: str, **kw) -> requests.Response:
        url = urljoin(BASE_URL + "/", path.lstrip("/"))
        r = self.session.get(url, timeout=30, **kw)
        r.raise_for_status()
        if self.debug:
            self._cache_html(path, "get", r.text)
        return r

    def post(self, path: str, data: dict, **kw) -> requests.Response:
        url = urljoin(BASE_URL + "/", path.lstrip("/"))
        r = self.session.post(url, data=data, timeout=30, **kw)
        r.raise_for_status()
        if self.debug:
            self._cache_html(path, "post", r.text)
        return r

    def _cache_html(self, path: str, method: str, html: str):
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", path)[:80] or "_"
        ts = int(time.time())
        (HTML_CACHE / f"{ts}_{method}_{safe}.html").write_text(html, encoding="utf-8")

    # --- ASP.NET helpers ---

    @staticmethod
    def extract_hidden(html: str) -> dict:
        """Pull all __VIEWSTATE etc. from a page so a postback works."""
        soup = BeautifulSoup(html, "lxml")
        out = {}
        for name in ASPNET_HIDDEN_FIELDS:
            el = soup.find("input", {"name": name})
            if el and el.has_attr("value"):
                out[name] = el["value"]
            else:
                out[name] = ""
        # Also collect any other hidden input — some Update Panels need them
        for el in soup.find_all("input", {"type": "hidden"}):
            n = el.get("name")
            if n and n not in out:
                out[n] = el.get("value", "")
        return out

    # --- login ---

    def login(self, force: bool = False) -> bool:
        """Login via Playwright headless (the form is JS-rendered),
        then transfer cookies to the requests session for fast subsequent calls.
        """
        if self._logged_in and not force:
            if self.is_authenticated():
                return True
        if not USERNAME or not PASSWORD:
            raise RuntimeError(
                f"Missing creds. Set VETMARKET_USERNAME / VETMARKET_PASSWORD "
                f"in ~/.clinic-secrets/vetmarket.env"
            )
        ok = self._login_via_playwright()
        if ok:
            self._logged_in = True
            self._save_session()
            return True
        return False

    def _login_via_playwright(self) -> bool:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=USER_AGENT, locale="he-IL")
            page = ctx.new_page()
            page.goto(f"{BASE_URL}/login", wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(1500)  # form is JS-rendered
            # Form is JS-rendered; locate via JS rather than Playwright selector
            # (state="attached" still requires the element to be considered "in DOM"
            # which can race with hidden-by-default form rendering).
            page.evaluate(f"""() => {{
                const u = document.getElementById('txtUserLogin');
                const p = document.getElementById('txtUserPass');
                if (!u || !p) throw new Error('login fields missing');
                u.value = {USERNAME!r};
                p.value = {PASSWORD!r};
                u.dispatchEvent(new Event('input', {{bubbles:true}}));
                p.dispatchEvent(new Event('input', {{bubbles:true}}));
            }}""")
            page.evaluate("document.getElementById('btnLogin').click()")
            try:
                page.wait_for_url("**/my-vetmarket", timeout=15000)
            except Exception:
                pass
            # Transfer cookies
            for c in ctx.cookies():
                self.session.cookies.set(c["name"], c["value"],
                                         domain=c.get("domain", ".vetmarket.co.il").lstrip("."),
                                         path=c.get("path", "/"))
            current_url = page.url
            browser.close()
            return "/my-vetmarket" in current_url or self.is_authenticated()

    def is_authenticated(self, html: str | None = None) -> bool:
        if html is None:
            try:
                html = self.get("my-vetmarket").text
            except requests.HTTPError:
                return False
        # Look for logout link or greeting
        return ("שלום" in html and ("יציאה" in html or "logout" in html.lower())) or \
               "lbtnLogoutMobile" in html

    # --- generic page fetcher ---

    def fetch_section(self, slug: str) -> str:
        """Fetch /<slug> after login. Returns HTML."""
        if not self._logged_in:
            self.login()
        r = self.get(slug)
        return r.text

    # --- postback (e.g. pagination) ---

    def postback(self, slug: str, event_target: str, event_argument: str = "",
                 extra: dict | None = None) -> str:
        r = self.get(slug)
        hidden = self.extract_hidden(r.text)
        data = {**hidden,
                "__EVENTTARGET": event_target,
                "__EVENTARGUMENT": event_argument}
        if extra:
            data.update(extra)
        r2 = self.post(slug, data=data)
        return r2.text

    # --- Excel export via Playwright (download trigger) ---

    def download_excel(self, section: str, save_to: str | Path,
                       date_from: str | None = None,
                       date_to: str | None = None) -> Path:
        """Download the .xlsx export of a section using its lbtnExcel postback.

        Date format: dd/mm/yy (matching the site's txtFromDate placeholder).
        """
        from playwright.sync_api import sync_playwright
        save_to = Path(save_to)
        save_to.parent.mkdir(parents=True, exist_ok=True)

        # Ensure logged in
        self.login()

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=USER_AGENT, locale="he-IL")
            # Inject our cookies
            cookies = []
            for c in self.session.cookies:
                cookies.append({
                    "name": c.name, "value": c.value,
                    "domain": c.domain or ".vetmarket.co.il",
                    "path": c.path or "/",
                })
            ctx.add_cookies(cookies)
            page = ctx.new_page()
            page.goto(f"{BASE_URL}/{section.lstrip('/')}",
                      wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(1000)
            # Set dates if requested
            if date_from:
                page.evaluate(f"document.getElementById('ContentPlaceHolder1_txtFromDate').value = {date_from!r}")
            if date_to:
                page.evaluate(f"document.getElementById('ContentPlaceHolder1_txtToDate').value = {date_to!r}")
            # If dates were changed, click the "חפש" filter button (id=btnSubmit)
            if date_from or date_to:
                try:
                    page.evaluate("document.getElementById('btnSubmit')?.click()")
                    page.wait_for_load_state("networkidle", timeout=20000)
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
            # Download Excel
            with page.expect_download(timeout=30000) as dl_info:
                page.evaluate(
                    "__doPostBack('ctl00$ContentPlaceHolder1$lbtnExcel','')"
                )
            d = dl_info.value
            d.save_as(str(save_to))
            browser.close()
        return save_to


# Singleton
_client: VetmarketClient | None = None


def client(debug: bool = False) -> VetmarketClient:
    global _client
    if _client is None:
        _client = VetmarketClient(debug=debug)
    return _client
