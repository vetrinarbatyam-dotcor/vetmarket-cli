"""Sync orchestrator. Pulls each section and writes to DB."""
from __future__ import annotations
import time
import traceback
from datetime import datetime
from typing import Callable

from . import db, parsers
from .client import client


def _log(section: str, fn: Callable[[], dict]) -> dict:
    started = datetime.utcnow().isoformat()
    err = None
    counts = {"added": 0, "updated": 0}
    try:
        counts = fn() or counts
    except Exception as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
    finished = datetime.utcnow().isoformat()
    with db.cursor() as c:
        c.execute(
            "INSERT INTO sync_log (section, started_at, finished_at, rows_added, rows_updated, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (section, started, finished, counts.get("added", 0), counts.get("updated", 0), err),
        )
    return {"section": section, "ok": err is None, "error": err, **counts}


def sync_my_products() -> dict:
    cli = client()
    html = cli.fetch_section("my-vetmarket")
    products = parsers.parse_my_products(html)
    added = 0
    with db.cursor() as c:
        for p in products:
            db.upsert_product(c, last_seen=datetime.utcnow().isoformat(), **p)
            added += 1
    return {"added": added, "updated": 0}


def sync_favorites() -> dict:
    cli = client()
    html = cli.fetch_section("favorites")
    favs = parsers.parse_favorites(html)
    added = 0
    with db.cursor() as c:
        # First mark all as not-favorite, then re-set found ones
        c.execute("UPDATE products SET is_favorite = 0")
        for p in favs:
            db.upsert_product(c, last_seen=datetime.utcnow().isoformat(), is_favorite=1, **p)
            added += 1
    return {"added": added, "updated": 0}


def sync_orders() -> dict:
    cli = client()
    html = cli.fetch_section("orders-status")
    parsed = parsers.parse_orders_list(html)
    added = 0
    now = datetime.utcnow().isoformat()
    with db.cursor() as c:
        for o in parsed["orders"]:
            c.execute(
                "INSERT INTO orders (order_id, order_date, status, total_amount, fetched_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(order_id) DO UPDATE SET status=excluded.status, fetched_at=excluded.fetched_at",
                (o["order_id"], o["order_date"], o["status"], None, now),
            )
            # Replace items
            c.execute("DELETE FROM order_items WHERE order_id = ?", (o["order_id"],))
            for it in o.get("items", []):
                c.execute(
                    "INSERT INTO order_items (order_id, sku, name, qty, unit_price, line_total) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (o["order_id"], it.get("sku"), it.get("name"), it.get("qty"), None, None),
                )
            added += 1
    return {"added": added, "updated": 0, "open_balance": parsed.get("open_balance")}


def sync_invoices(detail: bool = False, limit: int | None = None) -> dict:
    """List invoices. If detail=True, also fetch each invoice page for line items."""
    cli = client()
    html = cli.fetch_section("invoices")
    invoices = parsers.parse_invoices_list(html)
    added = 0
    now = datetime.utcnow().isoformat()
    with db.cursor() as c:
        for inv in invoices:
            c.execute(
                "INSERT INTO invoices (invoice_id, invoice_date, total_amount, fetched_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(invoice_id) DO UPDATE SET total_amount=excluded.total_amount, fetched_at=excluded.fetched_at",
                (inv["invoice_id"], inv["invoice_date"], inv.get("total_amount"), now),
            )
            added += 1

    if detail:
        # Fetch each invoice's detail page (URL pattern unknown — try several)
        with db.cursor() as c:
            target = invoices[: limit] if limit else invoices
            for inv in target:
                lines = _fetch_invoice_lines(cli, inv["invoice_id"])
                if not lines:
                    continue
                c.execute("DELETE FROM invoice_lines WHERE invoice_id = ?", (inv["invoice_id"],))
                for ln in lines:
                    c.execute(
                        "INSERT INTO invoice_lines (invoice_id, sku, name, qty, unit_price, line_total, discount_pct) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (inv["invoice_id"], ln.get("sku"), ln.get("name"),
                         ln.get("qty"), ln.get("unit_price"), ln.get("line_total"),
                         ln.get("discount_pct")),
                    )
                    if ln.get("sku") and ln.get("unit_price"):
                        db.add_price_observation(
                            c, ln["sku"], inv["invoice_date"], ln["unit_price"],
                            "invoice", qty=ln.get("qty"), source_id=inv["invoice_id"],
                        )
    return {"added": added, "updated": 0}


def _fetch_invoice_lines(cli, invoice_id: str) -> list[dict]:
    """Try several URL patterns for an invoice detail page."""
    candidates = [
        f"invoice/{invoice_id}",
        f"invoice-detail/{invoice_id}",
        f"invoices/{invoice_id}",
        f"invoice?id={invoice_id}",
        f"invoice.aspx?id={invoice_id}",
    ]
    for path in candidates:
        try:
            r = cli.get(path)
            if r.status_code == 200 and "404" not in r.text[:500]:
                lines = parsers.parse_invoice_detail(r.text)
                if lines:
                    return lines
        except Exception:
            continue
    return []


def sync_purchases() -> dict:
    """Sync the /purchases aggregated report → also seeds price observations."""
    cli = client()
    html = cli.fetch_section("purchases")
    parsed = parsers.parse_purchases(html)
    added = 0
    now = datetime.utcnow().isoformat()
    pf = parsed.get("period_from") or "2000-01-01"
    pt = parsed.get("period_to") or now[:10]
    with db.cursor() as c:
        for it in parsed["items"]:
            c.execute(
                "INSERT INTO purchases_summary (sku, period_from, period_to, name, total_qty, total_amount, avg_unit_price, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(sku, period_from, period_to) DO UPDATE SET "
                "total_qty=excluded.total_qty, total_amount=excluded.total_amount, "
                "avg_unit_price=excluded.avg_unit_price, fetched_at=excluded.fetched_at",
                (it["sku"], pf, pt, it["name"], it["total_qty"],
                 it["total_amount"], it["avg_unit_price"], now),
            )
            # Seed product
            db.upsert_product(c, sku=it["sku"], name_he=it["name"], last_seen=now)
            # Seed price observation as period-avg
            if it["avg_unit_price"]:
                db.add_price_observation(
                    c, it["sku"], pt, it["avg_unit_price"],
                    "purchases-avg", qty=it["total_qty"],
                    source_id=f"{pf}_{pt}",
                    notes=f"Avg from {it['total_qty']} units @ {it['total_amount']}₪",
                )
            added += 1
    return {"added": added, "updated": 0,
            "period_total": parsed.get("total_period_amount")}


def sync_shipping_docs() -> dict:
    cli = client()
    html = cli.fetch_section("shipping-documents")
    docs = parsers.parse_shipping_docs(html)
    added = 0
    now = datetime.utcnow().isoformat()
    with db.cursor() as c:
        for d in docs:
            c.execute(
                "INSERT INTO shipping_documents (doc_id, doc_date, status, fetched_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(doc_id) DO UPDATE SET status=excluded.status, fetched_at=excluded.fetched_at",
                (d["doc_id"], d["doc_date"], d["status"], now),
            )
            added += 1
    return {"added": added, "updated": 0}


def sync_price_offers() -> dict:
    cli = client()
    html = cli.fetch_section("price-offers")
    offers = parsers.parse_price_offers(html)
    added = 0
    now = datetime.utcnow().isoformat()
    with db.cursor() as c:
        for o in offers:
            c.execute(
                "INSERT INTO price_offers (offer_id, offer_date, status, fetched_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(offer_id) DO UPDATE SET status=excluded.status, fetched_at=excluded.fetched_at",
                (o["offer_id"], o["offer_date"], o["status"], now),
            )
            added += 1
    return {"added": added, "updated": 0}


def sync_all(invoice_detail: bool = False) -> list[dict]:
    return [
        _log("my-products", sync_my_products),
        _log("favorites", sync_favorites),
        _log("orders", sync_orders),
        _log("invoices", lambda: sync_invoices(detail=invoice_detail)),
        _log("purchases", sync_purchases),
        _log("shipping-docs", sync_shipping_docs),
        _log("price-offers", sync_price_offers),
    ]
