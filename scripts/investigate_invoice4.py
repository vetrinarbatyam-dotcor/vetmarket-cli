"""Submit the postback directly via requests with cookies from playwright session."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from vetmarket.config import BASE_URL, USERNAME, PASSWORD, USER_AGENT, DATA_DIR

OUT = DATA_DIR / "investigate"
OUT.mkdir(exist_ok=True)

# Step 1: Get cookies via Playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(user_agent=USER_AGENT, locale="he-IL")
    page = ctx.new_page()
    page.goto(f"{BASE_URL}/login", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1500)
    page.evaluate(
        f"""() => {{
            document.getElementById('txtUserLogin').value = {USERNAME!r};
            document.getElementById('txtUserPass').value = {PASSWORD!r};
            document.getElementById('btnLogin').click();
        }}"""
    )
    page.wait_for_url("**/my-vetmarket", timeout=20000)
    cookies = ctx.cookies()
    browser.close()

# Step 2: Set up requests session with cookies
s = requests.Session()
s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "he-IL,he;q=0.9"})
for c in cookies:
    s.cookies.set(c["name"], c["value"], domain=c.get("domain", ".vetmarket.co.il").lstrip("."))

# Step 3: GET /invoices
r = s.get(f"{BASE_URL}/invoices", timeout=30)
print("GET /invoices:", r.status_code, len(r.text))
soup = BeautifulSoup(r.text, "lxml")
hidden = {}
for inp in soup.find_all("input", type="hidden"):
    if inp.get("name"):
        hidden[inp["name"]] = inp.get("value", "")
print("Hidden fields:", len(hidden), "incl. __VIEWSTATE:", "__VIEWSTATE" in hidden)

# Step 4: POST with __EVENTTARGET = lbtnRepFactory for ctl00 (first invoice)
data = {
    **hidden,
    "__EVENTTARGET": "ctl00$ContentPlaceHolder1$rpt$ctl00$lbtnRepFactory",
    "__EVENTARGUMENT": "",
}
r2 = s.post(f"{BASE_URL}/invoices", data=data, timeout=60, allow_redirects=False)
print(f"POST /invoices: status={r2.status_code} ct={r2.headers.get('Content-Type')} len={len(r2.content)}")
print(f"  Location: {r2.headers.get('Location')}")
# Save body
ct = r2.headers.get("Content-Type", "")
if "pdf" in ct.lower() or r2.content[:4] == b"%PDF":
    out = OUT / "invoice_SI26004862.pdf"
    out.write_bytes(r2.content)
    print(f"Looks like PDF -> saved to {out}")
elif "html" in ct.lower():
    out = OUT / "invoice_SI26004862_postback.html"
    out.write_text(r2.text, encoding="utf-8")
    print(f"HTML response saved to {out}")
    # Search for prices
    soup2 = BeautifulSoup(r2.text, "lxml")
    body_text = soup2.get_text("\n", strip=True)
    import re
    prices = re.findall(r"[\d,]+\.\d+\s*₪?", body_text)
    print(f"Found {len(prices)} price-like strings; first 15: {prices[:15]}")
    # Find any tables
    tables = soup2.find_all("table")
    print(f"Tables: {len(tables)}")
    if tables:
        print("First table headers:", [th.get_text(strip=True) for th in tables[0].find_all("th")[:8]])
        print("First 5 rows of first table:")
        for tr in tables[0].find_all("tr")[:5]:
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            print("  |", " | ".join(cells))
else:
    out = OUT / "invoice_SI26004862_unknown.bin"
    out.write_bytes(r2.content)
    print(f"Unknown content type, saved {out} ({len(r2.content)} bytes)")
