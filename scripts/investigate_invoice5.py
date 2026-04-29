"""Capture the EXACT POST body sent when clicking the invoice action."""
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

    captured = []

    def on_request(req):
        if req.method == "POST":
            captured.append({
                "url": req.url,
                "method": req.method,
                "headers": dict(req.headers),
                "post_data": req.post_data,
            })

    ctx.on("request", on_request)

    # Trigger via popup capture (we know it opens a popup)
    print("Clicking invoice repFactory...")
    try:
        with ctx.expect_page(timeout=10000) as new_page_info:
            # Click the actual link element
            page.locator("#ContentPlaceHolder1_rpt_lbtnRepFactory_0").first.click()
        np = new_page_info.value
        np.wait_for_load_state("load", timeout=15000)
        print(f"Popup URL: {np.url}")
        print(f"Popup title: {np.title()}")
        out = OUT / "invoice_popup_response.html"
        out.write_text(np.content(), encoding="utf-8")
        print(f"Saved popup content to: {out}")
        body = np.evaluate("document.body.innerText")
        print(f"\nBody preview (first 800 chars):\n{body[:800]}")
    except Exception as e:
        print(f"Click/popup failed: {e}")

    print("\n=== POST requests captured ===")
    for c in captured:
        if "vetmarket" in c["url"]:
            print(f"\n{c['method']} {c['url']}")
            print(f"  Headers: target={c['headers'].get('target','')}")
            if c["post_data"]:
                # Show first 1000 chars of post body
                pd = c["post_data"][:1000]
                # Highlight key fields
                if "__EVENTTARGET" in pd:
                    et_idx = pd.index("__EVENTTARGET")
                    print(f"  __EVENTTARGET context: ...{pd[et_idx:et_idx+200]}...")

    browser.close()
