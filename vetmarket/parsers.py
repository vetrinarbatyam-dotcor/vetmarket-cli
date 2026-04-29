"""HTML parsers for each vetmarket page.

Each parser returns a list/dict of dicts ready for DB insertion.
Conservative: missing fields → None, never raise.
"""
from __future__ import annotations
import re
from datetime import datetime
from typing import Any
from bs4 import BeautifulSoup, Tag


CURRENCY_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*₪")
DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{2,4})")
SKU_RE = re.compile(r"מק[״\"']?ט[:\s]+([\w\d-]+)")


def parse_money(s: str) -> float | None:
    if not s:
        return None
    m = CURRENCY_RE.search(s)
    if m:
        return float(m.group(1).replace(",", ""))
    # Sometimes shown without symbol
    s2 = s.strip().replace(",", "")
    try:
        return float(s2)
    except ValueError:
        return None


def parse_date(s: str) -> str | None:
    if not s:
        return None
    m = DATE_RE.search(s)
    if not m:
        return None
    raw = m.group(1)
    # dd/mm/yy → ISO
    parts = raw.split("/")
    if len(parts[2]) == 2:
        parts[2] = "20" + parts[2]
    try:
        d = datetime(int(parts[2]), int(parts[1]), int(parts[0]))
        return d.strftime("%Y-%m-%d")
    except ValueError:
        return None


# ---------- /orders-status ----------

def parse_orders_list(html: str) -> dict:
    """Returns {open_balance: float, orders: [{order_id, date, status, items: [...]}]}"""
    soup = BeautifulSoup(html, "lxml")
    out = {"open_balance": None, "orders": []}

    # Open balance ("ת.מ. פתוחות 135,166.64₪")
    block = soup.find(string=re.compile(r"ת\.\s*מ\.\s*פתוחות"))
    if block:
        out["open_balance"] = parse_money(block.parent.get_text(" ", strip=True) if block.parent else block)

    # Orders: each row has date / order_id / status, then expandable items
    # The container is repContent
    rep = soup.find(id="ContentPlaceHolder1_repContent")
    if not rep:
        return out

    # Find each order row — they have a date pattern and order id
    for row in rep.find_all("div", recursive=True):
        text = row.get_text(" ", strip=True)
        m_oid = re.search(r"\b(5\d{9,})\b", text)
        m_date = DATE_RE.search(text)
        if not (m_oid and m_date):
            continue
        order_id = m_oid.group(1)
        # Avoid dup
        if any(o["order_id"] == order_id for o in out["orders"]):
            continue
        # Status: known values
        status = None
        for kw in ("טיוטא", "בהכנה למשלוח", "במשלוח", "סופק", "בוטל"):
            if kw in text:
                status = kw
                break
        out["orders"].append({
            "order_id": order_id,
            "order_date": parse_date(m_date.group(1)),
            "status": status,
            "items": parse_order_items(row),
        })
    return out


def parse_order_items(row: Tag) -> list[dict]:
    items = []
    # Each item row has: name (he+en) / qty / SKU
    for item in row.find_all("div", id=re.compile(r"divOrderItemRow")):
        text = item.get_text(" ", strip=True)
        sku_m = SKU_RE.search(text)
        # Name: usually first line
        name = item.get_text("\n", strip=True).split("\n")[0]
        qty = None
        # Look for "כמות: N" or numeric near end
        qty_m = re.search(r"כמות[:\s]+(\d+(?:\.\d+)?)", text)
        if qty_m:
            qty = float(qty_m.group(1))
        items.append({
            "sku": sku_m.group(1) if sku_m else None,
            "name": name,
            "qty": qty,
        })
    return items


# ---------- /invoices ----------

def parse_invoices_list(html: str) -> list[dict]:
    """Returns list of {invoice_id, invoice_date, total_amount, pdf_url}."""
    soup = BeautifulSoup(html, "lxml")
    rep = soup.find(id="ContentPlaceHolder1_repContent") or soup.find(id="ContentPlaceHolder1_up")
    invoices = []
    if not rep:
        return invoices
    text = rep.get_text(" | ", strip=True)
    # Triples appear flat: "DATE | INV_ID | AMOUNT ₪" repeated
    pat = re.compile(
        r"(\d{1,2}/\d{1,2}/\d{2,4})\s*\|?\s*(SI\d+|IN\d+|[A-Z]{1,4}\d{6,})\s*\|?\s*([\d,]+(?:\.\d+)?)\s*₪",
        re.UNICODE,
    )
    for m in pat.finditer(text):
        invoices.append({
            "invoice_id": m.group(2),
            "invoice_date": parse_date(m.group(1)),
            "total_amount": parse_money(m.group(3) + " ₪"),
        })
    # Also collect any PDF links
    for a in soup.find_all("a", href=re.compile(r"\.pdf|invoice|chesh", re.I)):
        href = a.get("href", "")
        # Try to attach to corresponding invoice id
        for inv in invoices:
            if inv["invoice_id"] in href or inv["invoice_id"] in a.get_text(" "):
                inv["pdf_url"] = href
    return invoices


def parse_invoice_detail(html: str) -> list[dict]:
    """Best-effort: extract line items from a single invoice page.

    We don't know the exact structure yet — try several strategies.
    Returns: [{sku, name, qty, unit_price, line_total, discount_pct}]
    """
    soup = BeautifulSoup(html, "lxml")
    lines = []

    # Strategy 1: <table> with header row containing מחיר/כמות/סה"כ
    for table in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        header_text = " ".join(headers)
        if not (("מחיר" in header_text or "ש\"ח" in header_text) and "כמות" in header_text):
            continue
        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if not cells:
                continue
            # Try to map: first guess — sku, name, qty, price, total
            row_text = " ".join(cells)
            sku_m = re.search(r"\b(\d{6,9}|[A-Z0-9]{4,12})\b", cells[0]) if cells else None
            qty = None
            unit_price = None
            line_total = None
            for c in cells:
                if re.fullmatch(r"\d+(?:\.\d+)?", c):
                    if qty is None:
                        qty = float(c)
                    continue
                price = parse_money(c)
                if price is not None:
                    if unit_price is None:
                        unit_price = price
                    else:
                        line_total = price
            if sku_m and (unit_price or line_total):
                lines.append({
                    "sku": sku_m.group(1),
                    "name": cells[1] if len(cells) > 1 else "",
                    "qty": qty,
                    "unit_price": unit_price,
                    "line_total": line_total,
                    "discount_pct": None,
                })

    # Strategy 2: row divs
    if not lines:
        for div in soup.find_all("div", class_=re.compile(r"invoice|line|row|item", re.I)):
            text = div.get_text(" ", strip=True)
            sku_m = SKU_RE.search(text) or re.search(r"\b(\d{7,9})\b", text)
            money = CURRENCY_RE.findall(text)
            if sku_m and money:
                # Heuristic: last money = total, second-to-last = unit
                unit_price = float(money[-2].replace(",", "")) if len(money) >= 2 else None
                line_total = float(money[-1].replace(",", ""))
                qty_m = re.search(r"\b(\d+(?:\.\d+)?)\s*יח", text)
                qty = float(qty_m.group(1)) if qty_m else None
                lines.append({
                    "sku": sku_m.group(1),
                    "name": re.sub(r"\s+", " ", text)[:120],
                    "qty": qty,
                    "unit_price": unit_price,
                    "line_total": line_total,
                    "discount_pct": None,
                })

    return lines


# ---------- /purchases (aggregated by SKU over a period) ----------

def parse_purchases(html: str) -> dict:
    """Returns {period_from, period_to, items: [{sku, name, total_qty, total_amount, avg_unit_price}], total_period_amount}."""
    soup = BeautifulSoup(html, "lxml")
    out = {"period_from": None, "period_to": None, "items": [], "total_period_amount": None}

    # Date filters
    f = soup.find("input", id="ContentPlaceHolder1_txtFromDate")
    t = soup.find("input", id="ContentPlaceHolder1_txtToDate")
    if f:
        out["period_from"] = parse_date(f.get("value", ""))
    if t:
        out["period_to"] = parse_date(t.get("value", ""))

    rep = soup.find(id="ContentPlaceHolder1_repContent") or soup.find(id="ContentPlaceHolder1_up")
    if not rep:
        return out

    text = rep.get_text("\n", strip=True)
    # First line of total: "102,618.00₪"
    total_m = re.search(r"([\d,]+\.\d+)\s*₪", text)
    if total_m:
        out["total_period_amount"] = float(total_m.group(1).replace(",", ""))

    # Each row: "<sku> <product name> <qty> יח' <amount> ₪"
    # Use regex that doesn't accidentally consume too much
    pat = re.compile(
        r"(\d{6,9})\s+(.+?)\s+(\d+(?:\.\d+)?)\s*יח'?\s+([\d,]+(?:\.\d+)?)\s*₪",
        re.UNICODE,
    )
    for m in pat.finditer(text):
        sku = m.group(1)
        name = m.group(2).strip()
        qty = float(m.group(3))
        amount = float(m.group(4).replace(",", ""))
        avg = round(amount / qty, 4) if qty else None
        out["items"].append({
            "sku": sku,
            "name": name,
            "total_qty": qty,
            "total_amount": amount,
            "avg_unit_price": avg,
        })
    return out


# ---------- /my-vetmarket (top-119 personal products) ----------

def parse_my_products(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    products = []
    for art in soup.find_all("div", id=re.compile(r"ucProduct")):
        link = art.find("a", href=re.compile(r"^product/"))
        if not link:
            continue
        name = link.get_text("\n", strip=True).split("\n")[0]
        url = link.get("href")
        # SKU
        sku = None
        sku_text = art.get_text(" ", strip=True)
        sku_m = SKU_RE.search(sku_text)
        if sku_m:
            sku = sku_m.group(1)
        else:
            # Sometimes shown as "12345 | תאריך תוקף"
            alt = re.search(r"\b(\d{6,9}|[A-Z0-9]{4,12})\s*\|", sku_text)
            if alt:
                sku = alt.group(1)
        # Expiry
        exp = re.search(r"תוקף[:\s]+(\d{1,2}/\d{1,2}/\d{2,4})", sku_text)
        # English name
        en = ""
        for span in art.find_all(["span", "div"]):
            t = span.get_text(" ", strip=True)
            if re.match(r"^[A-Za-z][A-Za-z0-9\s\-/.,'\"]+$", t) and len(t) > 5:
                en = t
                break
        img = art.find("img")
        products.append({
            "sku": sku,
            "name_he": name,
            "name_en": en,
            "url": url,
            "expiry_date": parse_date(exp.group(1)) if exp else None,
            "image_url": img.get("src") if img else None,
        })
    return [p for p in products if p["sku"]]


# ---------- /favorites ----------

def parse_favorites(html: str) -> list[dict]:
    # Same structure as my-products list
    return parse_my_products(html)


# ---------- /shipping-documents ----------

def parse_shipping_docs(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rep = soup.find(id="ContentPlaceHolder1_repContent") or soup.find(id="ContentPlaceHolder1_up")
    docs = []
    if not rep:
        return docs
    text = rep.get_text(" | ", strip=True)
    pat = re.compile(r"(\d{1,2}/\d{1,2}/\d{2,4})\s*\|?\s*([A-Z]{1,4}\d{6,})", re.UNICODE)
    for m in pat.finditer(text):
        docs.append({
            "doc_id": m.group(2),
            "doc_date": parse_date(m.group(1)),
            "status": "",
        })
    return docs


# ---------- /price-offers ----------

def parse_price_offers(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    rep = soup.find(id="ContentPlaceHolder1_repContent") or soup.find(id="ContentPlaceHolder1_up")
    offers = []
    if not rep:
        return offers
    text = rep.get_text(" | ", strip=True)
    # "DATE | OFFER_ID | STATUS"
    pat = re.compile(r"(\d{1,2}/\d{1,2}/\d{2,4})\s*\|?\s*(\d{8,})\s*\|?\s*(נשלחה|פעילה|בוטלה|פתוחה|הושלמה)?", re.UNICODE)
    for m in pat.finditer(text):
        offers.append({
            "offer_id": m.group(2),
            "offer_date": parse_date(m.group(1)),
            "status": m.group(3) or "",
        })
    return offers


# ---------- catalog search ----------

def parse_search_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    results = []
    for a in soup.find_all("a", href=re.compile(r"^product/")):
        name = a.get_text("\n", strip=True).split("\n")[0]
        if not name:
            continue
        results.append({
            "name": name,
            "url": a.get("href"),
        })
    return results
