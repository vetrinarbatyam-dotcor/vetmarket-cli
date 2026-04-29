"""Try the lbtnExcel postback on multiple sections - it likely downloads xlsx."""
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

    for section in ["invoices", "purchases", "orders-status"]:
        print(f"\n=== {section} ===")
        page.goto(f"{BASE_URL}/{section}", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1500)
        try:
            with page.expect_download(timeout=15000) as dl_info:
                page.evaluate("__doPostBack('ctl00$ContentPlaceHolder1$lbtnExcel','')")
            d = dl_info.value
            tgt = OUT / f"{section}_export_{d.suggested_filename}"
            d.save_as(str(tgt))
            print(f"  Downloaded: {d.suggested_filename} ({tgt.stat().st_size} bytes) → {tgt}")
        except Exception as e:
            print(f"  No download: {e}")

    browser.close()
