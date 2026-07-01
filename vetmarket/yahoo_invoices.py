"""Pull Vetmarket order-confirmation PDFs from the Yahoo clinic mailbox, parse the
line items, and store them. This is the FRESH price source (updated to today),
unlike the website scrape which lags. Each PDF line gives the *list* unit price;
the weighted average across orders reveals the effective price after bonus/discount.

Source mailbox: vet_batyam@yahoo.com  (creds in ~/.yahoo-creds.env)
Sender:         vetmarket.services@vetmarket.co.il
Subject:        "Vetmarket Order Confirmation - <order_no>"
"""
from __future__ import annotations

import email
import imaplib
import io
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import pdfplumber

from . import db

YAHOO_CREDS = Path.home() / ".yahoo-creds.env"
IMAP_HOST = "imap.mail.yahoo.com"
SENDER = "vetmarket.services@vetmarket.co.il"

# Line item: numbers render left-to-right at the start of each RTL line.
#   total  חש unit  'חי qty  date  desc  sku  line
LINE_RE = re.compile(
    r"^([\d,]+\.\d{2})\s+חש\s+([\d,]+\.\d{2})\s+'חי\s+([\d,]+\.\d{2})\s+"
    r"(\d{2}/\d{2}/\d{2})\s+(.+?)\s+(\d{3,})\s+(\d+)\s*$"
)
_HE = re.compile(r"[֐-׿]")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _iso(ddmmyy: str) -> str:
    dd, mm, yy = ddmmyy.split("/")
    return f"20{yy}-{mm}-{dd}"


def reverse_he(text: str) -> str:
    """pdfplumber emits RTL runs reversed. Flip Hebrew (and adjacent) runs back so
    names read correctly, leaving Latin/number runs in place."""
    tokens = text.split()
    out, buf = [], []
    for tok in tokens:
        if _HE.search(tok):
            buf.append(tok)
        else:
            if buf:
                out.extend(reversed([t[::-1] if _HE.search(t) else t for t in buf]))
                buf = []
            out.append(tok)
    if buf:
        out.extend(reversed([t[::-1] if _HE.search(t) else t for t in buf]))
    return " ".join(out)


def _load_creds() -> dict:
    creds = {}
    with open(YAHOO_CREDS, encoding="utf-8") as f:
        for ln in f:
            if "=" in ln:
                k, v = ln.strip().split("=", 1)
                creds[k] = v
    return creds


def parse_pdf(pdf_bytes: bytes) -> list[dict]:
    """Return list of line items from one order-confirmation PDF."""
    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for ln in (page.extract_text() or "").splitlines():
                m = LINE_RE.match(ln.strip())
                if not m:
                    continue
                total, unit, qty, date, desc, sku, lineno = m.groups()
                rows.append({
                    "sku": sku,
                    "name": reverse_he(desc.strip()),
                    "date": _iso(date),
                    "qty": _num(qty),
                    "unit_price": _num(unit),
                    "line_total": _num(total),
                })
    return rows


# Order confirmations live in Inbox, but some get swept to Trash — include both.
FOLDERS = ("INBOX", "Trash")


def fetch_orders(since_days: int = 1500) -> list[dict]:
    """Download + parse every order-confirmation PDF newer than `since_days`,
    across Inbox + Trash. Returns list of {order_no, date, lines:[...]}, deduped
    by order number. since_days default ~4y = full history."""
    creds = _load_creds()
    cutoff = (datetime.utcnow() - timedelta(days=since_days)).strftime("%d-%b-%Y")
    M = imaplib.IMAP4_SSL(IMAP_HOST, 993)
    M.login(creds["YAHOO_EMAIL"], creds["YAHOO_APP_PASSWORD"])
    seen: set[str] = set()
    out = []
    try:
        for folder in FOLDERS:
            typ, _ = M.select(f'"{folder}"', readonly=True)
            if typ != "OK":
                continue
            typ, data = M.search(
                None, f'(FROM "{SENDER}" SUBJECT "Order Confirmation" SINCE {cutoff})'
            )
            for i in data[0].split():
                typ, d = M.fetch(i, b"(RFC822)")
                raw = next((x[1] for x in d if isinstance(x, tuple)), b"")
                msg = email.message_from_bytes(raw)
                order_no = (msg.get("Subject") or "").split("-")[-1].strip()
                if order_no in seen:
                    continue
                for part in msg.walk():
                    fn = part.get_filename() or ""
                    if "pdf" in (part.get_content_type() + fn).lower():
                        payload = part.get_payload(decode=True)
                        if not payload:
                            continue
                        lines = parse_pdf(payload)
                        if lines:
                            seen.add(order_no)
                            out.append({
                                "order_no": order_no,
                                "date": lines[0]["date"],
                                "lines": lines,
                            })
    finally:
        M.logout()
    return out


def store(orders: list[dict]) -> dict:
    """Persist orders into invoices + invoice_lines + prices (source='invoice')."""
    now = datetime.utcnow().isoformat()
    n_inv = n_lines = 0
    with db.cursor() as c:
        for o in orders:
            total = round(sum(l["line_total"] for l in o["lines"]), 2)
            c.execute(
                "INSERT OR REPLACE INTO invoices "
                "(invoice_id, invoice_date, total_amount, fetched_at) VALUES (?,?,?,?)",
                (o["order_no"], o["date"], total, now),
            )
            n_inv += 1
            # refresh lines for this invoice
            c.execute("DELETE FROM invoice_lines WHERE invoice_id=?", (o["order_no"],))
            for l in o["lines"]:
                c.execute(
                    "INSERT INTO invoice_lines "
                    "(invoice_id, sku, name, qty, unit_price, line_total, discount_pct) "
                    "VALUES (?,?,?,?,?,?,NULL)",
                    (o["order_no"], l["sku"], l["name"], l["qty"],
                     l["unit_price"], l["line_total"]),
                )
                db.add_price_observation(
                    c, sku=l["sku"], observed_date=l["date"],
                    unit_price=l["unit_price"], source="invoice",
                    qty=l["qty"], source_id=o["order_no"],
                    notes=l["name"],
                )
                # keep a product name (only fill if missing)
                c.execute(
                    "INSERT INTO products (sku, name_he, last_seen) VALUES (?,?,?) "
                    "ON CONFLICT(sku) DO UPDATE SET last_seen=excluded.last_seen, "
                    "name_he=COALESCE(NULLIF(products.name_he,''), excluded.name_he)",
                    (l["sku"], l["name"], o["date"]),
                )
                n_lines += 1
    return {"invoices": n_inv, "lines": n_lines}


def sync(since_days: int = 1500) -> dict:
    orders = fetch_orders(since_days)
    res = store(orders)
    res["orders_fetched"] = len(orders)
    return res
