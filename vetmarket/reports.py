"""Analytics queries against the local DB."""
from __future__ import annotations
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


def invoice_price_table(n_invoices: int = 12, q: str = "",
                        limit: int = 500, offset: int = 0) -> list[dict]:
    """Per-SKU pricing from the Yahoo order-confirmation invoices (the FRESH source).

    Basis = the **last `n_invoices` invoices per product** (not a calendar window).
    For each SKU we rank its invoice observations newest-first and keep the top N:
    - list price  = unit price on the *latest* invoice for the SKU (catalog price,
                    before any quantity bonus / effective discount)
    - avg  price  = Σ(unit*qty) / Σ(qty) over those last N invoices (effectively paid)
    - total cost  = Σ(unit*qty) over those last N invoices (total spend on the item)
    - n_orders    = how many invoices actually backed the number (≤ N)

    All prices are NET; gross = ×1.18. discount_pct = (list-avg)/list*100.
    """
    where = ["r.rn <= ?"]
    params: list = [n_invoices]
    if q:
        where.append("(r.sku LIKE ? OR r.notes LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    where_sql = " AND ".join(where)
    sql = f"""
        WITH ranked AS (
            SELECT p.sku, p.unit_price, p.qty, p.observed_date, p.source_id,
                   p.notes, p.id,
                   ROW_NUMBER() OVER (
                       PARTITION BY p.sku
                       ORDER BY p.observed_date DESC, p.id DESC
                   ) AS rn
            FROM prices p
            WHERE p.source='invoice'
        )
        SELECT r.sku AS sku,
            (SELECT notes FROM ranked rn1 WHERE rn1.sku=r.sku AND rn1.rn=1) AS name,
            (SELECT unit_price FROM ranked rn2 WHERE rn2.sku=r.sku AND rn2.rn=1) AS list_net,
            ROUND(SUM(r.unit_price*COALESCE(r.qty,0))/NULLIF(SUM(r.qty),0),4) AS avg_net,
            ROUND(SUM(r.unit_price*COALESCE(r.qty,0)),2) AS total_net,
            SUM(r.qty) AS total_qty,
            MAX(r.observed_date) AS last_date,
            COUNT(DISTINCT r.source_id) AS n_orders
        FROM ranked r
        WHERE {where_sql}
        GROUP BY r.sku
        ORDER BY total_net DESC
        LIMIT ? OFFSET ?
    """
    params += [limit, offset]
    with db.cursor() as c:
        rows = c.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        ln, an, tn = d.get("list_net"), d.get("avg_net"), d.get("total_net")
        d["list_gross"] = round(ln * 1.18, 2) if ln else None
        d["avg_gross"] = round(an * 1.18, 2) if an else None
        d["total_gross"] = round(tn * 1.18, 2) if tn else None
        d["discount_pct"] = round((ln - an) / ln * 100, 1) if ln and an else None
        out.append(d)
    return out


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


def last_updated() -> dict:
    """When did each price source last refresh — for the dashboard staleness banner.

    Vetmarket freshness = newest invoice fetched_at (the monthly cron writes it,
    yahoo_invoices.store). Medi-Market freshness = mtime of its scraped prices.db.
    Returns ISO strings (or None) plus age_days so the UI can flag stale data.
    """
    import datetime
    from .config import DATA_DIR

    with db.cursor() as c:
        vm_inv = c.execute("SELECT MAX(fetched_at) FROM invoices").fetchone()[0]
        vm_pur = c.execute("SELECT MAX(fetched_at) FROM purchases_summary").fetchone()[0]
    vm = max([x for x in (vm_inv, vm_pur) if x], default=None)

    mm = None
    mm_db = DATA_DIR / "medimarket" / "prices.db"
    if mm_db.exists():
        mm = datetime.datetime.fromtimestamp(
            mm_db.stat().st_mtime
        ).isoformat(timespec="seconds")

    def _age_days(iso: str | None) -> int | None:
        if not iso:
            return None
        try:
            d = datetime.datetime.fromisoformat(iso.replace("Z", "").strip())
        except ValueError:
            try:
                d = datetime.datetime.fromisoformat(iso[:19])
            except ValueError:
                return None
        return (datetime.datetime.now() - d).days

    return {
        "vetmarket": vm,
        "medimarket": mm,
        "vetmarket_age_days": _age_days(vm),
        "medimarket_age_days": _age_days(mm),
    }
