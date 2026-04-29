"""Wait for the popup window to actually load and capture its URL+content."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
from playwright.sync_api import sync_playwright
from vetmarket.config import BASE_URL, USERNAME, PASSWORD, USER_AGENT, DATA_DIR

OUT = DATA_DIR / "investigate"
OUT.mkdir(exist_ok=True)

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

    page.goto(f"{BASE_URL}/invoices", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1500)

    print("Waiting for popup after postback...")
    with ctx.expect_page(timeout=15000) as new_page_info:
        page.evaluate("__doPostBack('ctl00$ContentPlaceHolder1$rpt$ctl00$lbtnRepFactory','')")
    new_page = new_page_info.value
    new_page.wait_for_load_state("networkidle", timeout=20000)
    print("New page URL:", new_page.url)
    print("Title:", new_page.title())
    content = new_page.content()
    print("Content length:", len(content))

    out_file = OUT / "invoice_detail_SI26004862.html"
    out_file.write_text(content, encoding="utf-8")
    print(f"Saved: {out_file}")

    # Quick text preview
    text = new_page.evaluate("document.body.innerText").strip()
    print("\nFirst 1500 chars of body text:")
    print(text[:1500])

    browser.close()
