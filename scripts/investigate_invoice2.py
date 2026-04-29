"""Click the lbtnRepFactory link and watch what happens (new page / download / modal)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pathlib import Path
from playwright.sync_api import sync_playwright
from vetmarket.config import BASE_URL, USERNAME, PASSWORD, USER_AGENT, DATA_DIR

OUT = DATA_DIR / "investigate"
OUT.mkdir(exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(user_agent=USER_AGENT, locale="he-IL", accept_downloads=True)

    requests_log = []
    responses_log = []
    downloads_log = []

    page = ctx.new_page()
    page.on("request", lambda r: requests_log.append((r.method, r.url[:200])))
    page.on("response", lambda r: responses_log.append((r.status, r.headers.get("content-type", ""), r.url[:200])))
    page.on("download", lambda d: downloads_log.append((d.suggested_filename, d.url)))

    # Login
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

    # Clear logs
    requests_log.clear()
    responses_log.clear()
    downloads_log.clear()

    # New page listener for popups
    new_pages = []
    ctx.on("page", lambda p: new_pages.append(p))

    print("Triggering postback for first invoice (ctl00 = SI26004862)...")
    # Try clicking the link directly
    try:
        with page.expect_download(timeout=10000) as dl_info:
            page.evaluate("__doPostBack('ctl00$ContentPlaceHolder1$rpt$ctl00$lbtnRepFactory','')")
        download = dl_info.value
        print(f"DOWNLOAD: {download.suggested_filename} from {download.url}")
        target = OUT / download.suggested_filename
        download.save_as(str(target))
        print(f"Saved to: {target}")
    except Exception as e:
        print(f"No download: {e}")
        page.wait_for_timeout(3000)
        # check for new pages or current state
        print(f"Current URL: {page.url}")
        print(f"New pages opened: {len(new_pages)}")
        for np in new_pages:
            print(f"  → {np.url}")

    print("\n=== Recent network activity ===")
    for status, ct, url in responses_log[-20:]:
        if "vetmarket" in url and "image" not in ct.lower() and "css" not in ct.lower() and "js" not in ct.lower():
            print(f"  [{status}] {ct[:30]:30s} {url}")

    browser.close()
print("Done.")
