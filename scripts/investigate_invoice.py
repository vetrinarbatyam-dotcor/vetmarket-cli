"""One-shot: login, open /invoices, click into first invoice, save URL + HTML."""
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
    print("Logged in:", page.url)

    # Go to invoices
    page.goto(f"{BASE_URL}/invoices", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1500)
    print("On invoices page:", page.url)

    # Save full HTML
    (OUT / "invoices_list.html").write_text(page.content(), encoding="utf-8")

    # Find clickable invoice rows / IDs
    structure = page.evaluate(r"""() => {
        const main = document.getElementById('ContentPlaceHolder1_repContent') ||
                     document.getElementById('ContentPlaceHolder1_up');
        if (!main) return {error: 'no main container'};

        // Look for clickable links/buttons that mention an invoice ID
        const clickables = Array.from(main.querySelectorAll('a, button'))
          .filter(el => /SI\d+|IN\d+|חשבונית|פירוט|הצג/i.test(el.innerText + ' ' + (el.href||'') + ' ' + (el.title||'')))
          .slice(0, 10).map(el => ({
            tag: el.tagName,
            href: el.getAttribute('href'),
            text: el.innerText.trim().slice(0, 80),
            onclick: el.getAttribute('onclick'),
            id: el.id,
            class: el.className
          }));

        // Look for any data-invoice-id or similar attributes
        const dataAttrs = Array.from(main.querySelectorAll('[data-invoice-id], [data-id], [data-doc]'))
          .slice(0, 5).map(el => ({
            tag: el.tagName,
            id: el.id,
            attrs: Array.from(el.attributes).map(a => `${a.name}=${a.value.slice(0,50)}`)
          }));

        // All rows + their structure
        const rows = Array.from(main.querySelectorAll('tr, div'))
          .filter(el => /SI\d+/.test(el.innerText || ''))
          .slice(0, 5).map(el => ({
            tag: el.tagName,
            class: el.className,
            id: el.id,
            html: el.outerHTML.slice(0, 600)
          }));

        return { clickables, dataAttrs, rows };
    }""")
    print("\n=== STRUCTURE ===")
    import json
    print(json.dumps(structure, ensure_ascii=False, indent=2)[:3000])

    # Try clicking the first invoice
    print("\n=== TRYING CLICK ===")
    # Strategy: click any element that contains 'SI26004862'
    target_invoice = "SI26004862"
    try:
        # Look for the link/button that contains the invoice id
        elem = page.locator(f"text={target_invoice}").first
        elem_html = elem.evaluate("el => el.outerHTML.slice(0,300)")
        print(f"Found element for {target_invoice}:", elem_html[:200])
        # Find the closest clickable parent
        before_url = page.url
        try:
            elem.click()
            page.wait_for_load_state("networkidle", timeout=15000)
            after_url = page.url
            print(f"  Clicked → URL changed: {before_url} → {after_url}")
            (OUT / f"invoice_{target_invoice}.html").write_text(page.content(), encoding="utf-8")
        except Exception as e:
            print(f"  Click failed: {e}")
            # Try parent link
            parent = elem.evaluate("el => el.closest('a')?.outerHTML || el.closest('[onclick]')?.outerHTML || 'no clickable parent'")
            print(f"  Parent clickable:", parent[:300])
    except Exception as e:
        print(f"Failed to find element: {e}")

    browser.close()
print("\nFiles saved to:", OUT)
