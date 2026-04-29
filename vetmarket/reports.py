"""Analytics queries against the local DB."""
from __future__ import annotations
from datetime import datetime, timedelta
from . import db


def open_orders() -> list[dict]:
    with db.cursor() as c:
        rows = c.execute(
            "SELECT order_id, order_date, status FROM orders "
            "WHERE status IN ('טיוטא','בהכנה למשלוח','במשלוח') "
            "ORDER BY order_date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def all_orders(limit: int = 50) -> list[dict]:
    with db.cursor() as c:
        rows = c.execute(
            "SELECT order_id, order_date, status FROM orders "
            "ORDER BY order_date DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def order_items(order_id: str) -> list[dict]:
    with db.cursor() as c:
        rows = c.execute(
            "SELECT sku, name, qty FROM order_items WHERE order_id = ?", (order_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def all_invoices(limit: int = 50) -> list[dict]:
    with db.cursor() as c:
        rows = c.execute(
            "SELECT invoice_id, invoice_date, total_amount FROM invoices "
            "ORDER BY invoice_date DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def invoice_lines(invoice_id: str) -> list[dict]:
    with db.cursor() as c:
        rows = c.execute(
            "SELECT sku, name, qty, unit_price, line_total, discount_pct "
            "FROM invoice_lines WHERE invoice_id = ?", (invoice_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def search_products(q: str, limit: int = 20) -> list[dict]:
    pat = f"%{q}%"
    with db.cursor() as c:
        rows = c.execute(
            "SELECT sku, name_he, name_en, manufacturer, is_favorite "
            "FROM products WHERE name_he LIKE ? OR name_en LIKE ? OR sku LIKE ? "
            "ORDER BY is_favorite DESC, name_he LIMIT ?",
            (pat, pat, pat, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def product_detail(sku: str) -> dict | None:
    with db.cursor() as c:
        row = c.execute(
            "SELECT sku, name_he, name_en, description, category, manufacturer, "
            "url, image_url, expiry_date, is_favorite, last_seen "
            "FROM products WHERE sku = ?", (sku,)
        ).fetchone()
    return dict(row) if row else None


def price_history(sku: str) -> list[dict]:
    with db.cursor() as c:
        rows = c.execute(
            "SELECT observed_date, unit_price, qty, source, source_id, notes "
            "FROM prices WHERE sku = ? ORDER BY observed_date DESC", (sku,)
        ).fetchall()
    return [dict(r) for r in rows]


def price_summary(sku: str) -> dict:
    """Latest invoice price + period averages + min/max."""
    history = price_history(sku)
    if not history:
        return {"sku": sku, "n": 0}
    invoice_prices = [h for h in history if h["source"] == "invoice"]
    avg_prices = [h for h in history if h["source"] == "purchases-avg"]
    units = [h["unit_price"] for h in history if h["unit_price"]]
    out = {
        "sku": sku,
        "n": len(history),
        "latest_invoice_price": invoice_prices[0]["unit_price"] if invoice_prices else None,
        "latest_invoice_date": invoice_prices[0]["observed_date"] if invoice_prices else None,
        "latest_avg_price": avg_prices[0]["unit_price"] if avg_prices else None,
        "min_price": min(units) if units else None,
        "max_price": max(units) if units else None,
        "mean_price": round(sum(units) / len(units), 4) if units else None,
    }
    return out


def all_known_prices(limit: int = 200) -> list[dict]:
    """One row per SKU with latest invoice price + name."""
    with db.cursor() as c:
        rows = c.execute("""
            SELECT p.sku, p.name_he, p.manufacturer,
                   (SELECT unit_price FROM prices WHERE sku = p.sku AND source='invoice' ORDER BY observed_date DESC LIMIT 1) AS latest_invoice_price,
                   (SELECT observed_date FROM prices WHERE sku = p.sku AND source='invoice' ORDER BY observed_date DESC LIMIT 1) AS latest_invoice_date,
                   (SELECT unit_price FROM prices WHERE sku = p.sku AND source='purchases-avg' ORDER BY observed_date DESC LIMIT 1) AS latest_avg_price
            FROM products p
            WHERE EXISTS (SELECT 1 FROM prices WHERE sku = p.sku)
            ORDER BY p.name_he
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def spend_by_month(months: int = 12) -> list[dict]:
    with db.cursor() as c:
        rows = c.execute("""
            SELECT substr(invoice_date, 1, 7) AS ym,
                   COUNT(*) AS invoices,
                   ROUND(SUM(total_amount), 2) AS total
            FROM invoices
            WHERE invoice_date IS NOT NULL
            GROUP BY ym
            ORDER BY ym DESC
            LIMIT ?
        """, (months,)).fetchall()
    return [dict(r) for r in rows]


def spend_by_year() -> list[dict]:
    with db.cursor() as c:
        rows = c.execute("""
            SELECT substr(invoice_date, 1, 4) AS y,
                   COUNT(*) AS invoices,
                   ROUND(SUM(total_amount), 2) AS total
            FROM invoices
            WHERE invoice_date IS NOT NULL
            GROUP BY y
            ORDER BY y DESC
        """).fetchall()
    return [dict(r) for r in rows]


def top_skus(by: str = "spend", limit: int = 20) -> list[dict]:
    """Top SKUs by spend or qty (from purchases_summary, the most reliable aggregate)."""
    order_col = "total_amount" if by == "spend" else "total_qty"
    with db.cursor() as c:
        rows = c.execute(f"""
            SELECT sku, name,
                   SUM(total_qty) AS qty,
                   ROUND(SUM(total_amount), 2) AS spend,
                   ROUND(SUM(total_amount)/NULLIF(SUM(total_qty),0), 4) AS avg_unit_price
            FROM purchases_summary
            GROUP BY sku
            ORDER BY {('spend' if by=='spend' else 'qty')} DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def price_trend(sku: str) -> list[dict]:
    """Return invoice-priced observations chronologically + flag direction."""
    with db.cursor() as c:
        rows = c.execute("""
            SELECT observed_date, unit_price
            FROM prices WHERE sku = ? AND source = 'invoice'
            ORDER BY observed_date ASC
        """, (sku,)).fetchall()
    out = []
    prev = None
    for r in rows:
        d = dict(r)
        if prev is not None:
            d["delta"] = round(d["unit_price"] - prev, 4)
            d["delta_pct"] = round((d["unit_price"] - prev) / prev * 100, 2) if prev else None
        prev = d["unit_price"]
        out.append(d)
    return out


def price_anomalies(threshold_pct: float = 15.0) -> list[dict]:
    """SKUs whose latest invoice price differs from prior mean by > threshold %."""
    with db.cursor() as c:
        rows = c.execute("""
            SELECT sku FROM prices WHERE source='invoice'
            GROUP BY sku HAVING COUNT(*) >= 2
        """).fetchall()
    flags = []
    for r in rows:
        sku = r["sku"]
        trend = price_trend(sku)
        if len(trend) < 2:
            continue
        latest = trend[-1]
        prior = [t["unit_price"] for t in trend[:-1]]
        prior_mean = sum(prior) / len(prior)
        if not prior_mean:
            continue
        delta_pct = (latest["unit_price"] - prior_mean) / prior_mean * 100
        if abs(delta_pct) >= threshold_pct:
            with db.cursor() as c:
                p = c.execute("SELECT name_he FROM products WHERE sku=?", (sku,)).fetchone()
            flags.append({
                "sku": sku,
                "name": p["name_he"] if p else "",
                "latest_price": latest["unit_price"],
                "prior_mean": round(prior_mean, 4),
                "delta_pct": round(delta_pct, 2),
                "direction": "↑" if delta_pct > 0 else "↓",
            })
    return sorted(flags, key=lambda x: abs(x["delta_pct"]), reverse=True)


def open_balance_now() -> float | None:
    with db.cursor() as c:
        r = c.execute("SELECT rows_added FROM sync_log "
                      "WHERE section='orders' ORDER BY id DESC LIMIT 1").fetchone()
    # Open balance is captured ad-hoc from sync_orders return — for now read from sync log notes
    # Better: re-fetch from /orders-status. For now report null.
    return None


def status_summary() -> dict:
    with db.cursor() as c:
        nm = c.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        no = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        ni = c.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        nl = c.execute("SELECT COUNT(*) FROM invoice_lines").fetchone()[0]
        np = c.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        nf = c.execute("SELECT COUNT(*) FROM products WHERE is_favorite=1").fetchone()[0]
        ns = c.execute("SELECT COUNT(*) FROM shipping_documents").fetchone()[0]
        npo = c.execute("SELECT COUNT(*) FROM price_offers").fetchone()[0]
        last_sync = c.execute(
            "SELECT section, finished_at, error FROM sync_log ORDER BY id DESC LIMIT 8"
        ).fetchall()
    return {
        "products": nm,
        "favorites": nf,
        "orders": no,
        "invoices": ni,
        "invoice_lines": nl,
        "price_observations": np,
        "shipping_docs": ns,
        "price_offers": npo,
        "last_sync": [dict(r) for r in last_sync],
    }
