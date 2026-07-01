"""FastAPI dashboard for searching/comparing Vetmarket vs Medi-Market."""
from __future__ import annotations
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

import hashlib

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db, compare as compare_mod
from .config import DATA_DIR, DASHBOARD_PIN

app = FastAPI(title="Vetmarket Dashboard")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

MEDIMARKET_DB = DATA_DIR / "medimarket" / "prices.db"


# ---------- PIN gate (same convention as Gil's other portals) ----------

_AUTH_COOKIE = "vm_auth"
# Cookie value is a stable token derived from the PIN — not the raw PIN, so the
# code itself isn't sitting readable in the cookie jar.
_AUTH_TOKEN = hashlib.sha256(f"vetmarket-dash::{DASHBOARD_PIN}".encode()).hexdigest()[:32]

# Brute-force guard: a 6-digit PIN is only 10^6, so throttle /login per client IP.
# In-memory (single worker) is enough for a one-box internal dashboard.
import time as _time
_LOGIN_FAILS: dict[str, list] = {}          # ip -> [timestamps of recent fails]
_MAX_FAILS = 8                               # allowed fails within the window
_FAIL_WINDOW = 15 * 60                       # 15 minutes


def _rate_limited(ip: str) -> bool:
    now = _time.time()
    fails = [t for t in _LOGIN_FAILS.get(ip, []) if now - t < _FAIL_WINDOW]
    _LOGIN_FAILS[ip] = fails
    return len(fails) >= _MAX_FAILS


def _record_fail(ip: str) -> None:
    _LOGIN_FAILS.setdefault(ip, []).append(_time.time())

_LOGIN_HTML = """<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>כניסה — Vetmarket Dashboard</title>
<style>body{background:#0f1419;color:#e6e8eb;font-family:-apple-system,"Segoe UI",Arial,sans-serif;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.box{background:#1a2128;border:1px solid #2a3340;border-radius:10px;padding:32px;width:280px;text-align:center}
h1{font-size:18px;color:#4a9eff;margin:0 0 18px}
input{width:100%;box-sizing:border-box;background:#0f1419;border:1px solid #2a3340;color:#e6e8eb;
font-size:22px;text-align:center;letter-spacing:6px;padding:12px;border-radius:6px;margin-bottom:12px}
button{width:100%;background:#4a9eff;color:#fff;border:0;padding:12px;border-radius:6px;font-size:15px;cursor:pointer}
.err{color:#ff5a5a;font-size:13px;min-height:18px;margin-bottom:6px}</style></head>
<body><form class="box" method="post" action="/login">
<h1>🐾 Vetmarket Dashboard</h1><div class="err">__ERR__</div>
<input name="pin" type="password" inputmode="numeric" autocomplete="off" autofocus placeholder="PIN">
<button>כניסה</button></form></body></html>"""


@app.middleware("http")
async def _pin_gate(request: Request, call_next):
    path = request.url.path
    if path == "/login":
        return await call_next(request)
    if request.cookies.get(_AUTH_COOKIE) == _AUTH_TOKEN:
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return HTMLResponse(_LOGIN_HTML.replace("__ERR__", ""), status_code=401)


@app.post("/login")
async def login(request: Request):
    import hmac
    from urllib.parse import parse_qs
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
          or (request.client.host if request.client else "unknown"))
    if _rate_limited(ip):
        return HTMLResponse(
            _LOGIN_HTML.replace("__ERR__", "יותר מדי נסיונות — נסה שוב בעוד 15 דקות"),
            status_code=429)
    # Parse the urlencoded body by hand to avoid a python-multipart dependency.
    body = (await request.body()).decode("utf-8", "ignore")
    pin = (parse_qs(body).get("pin", [""])[0] or "").strip()
    # Timing-safe compare so response time doesn't leak the PIN digit-by-digit.
    if hmac.compare_digest(pin, DASHBOARD_PIN):
        _LOGIN_FAILS.pop(ip, None)
        resp = RedirectResponse(url="/", status_code=303)
        # NOTE: no secure=True — served over plain HTTP (nip.io, no TLS). Add it
        # together with a 301→https redirect once the box has a certificate.
        resp.set_cookie(_AUTH_COOKIE, _AUTH_TOKEN, httponly=True,
                        max_age=60 * 60 * 24 * 30, samesite="lax")
        return resp
    _record_fail(ip)
    return HTMLResponse(_LOGIN_HTML.replace("__ERR__", "PIN שגוי"), status_code=401)


# ---------- helpers ----------

# Allowed sort columns per supplier
_VM_SORT_COLS = {
    "name": "ps.name",
    "price": "ps.avg_unit_price",
    "spend": "ps.total_amount",
    "qty": "ps.total_qty",
}
_MM_SORT_COLS = {
    "name": "name",
    "price": "price",
}


# Basis for Vetmarket invoice pricing: the last N invoices per product.
# The list price (מחירון) is ALWAYS the latest invoice's unit price, so a price
# change shows the current price, never a stale/averaged one. The avg is the
# weighted mean actually paid across those last N invoices.
VM_N_INVOICES = 12

# sort_by -> key in the invoice_price_table row dict
_VM_SORT_KEYS = {
    "name": "name", "price": "avg_gross", "spend": "total_net", "qty": "total_qty",
}


def _vetmarket_search(q: str = "", limit: int = 100, offset: int = 0,
                      sort_by: str = "spend", order: str = "desc") -> list[dict]:
    """Vetmarket pricing from the Yahoo order-confirmation invoices (FRESH source).

    Each row carries BOTH:
      - list_gross : מחירון — unit price on the latest invoice (before bonus/discount)
      - avg_gross  : מחיר ממוצע משוקלל — effectively paid over the last N invoices
      - total_gross: סה"כ עלות — total spend on the item across those N invoices
      - n_orders   : how many invoices backed the number (basis = last N invoices)
    price_gross/price_net mirror the weighted-avg so cross-supplier compare keeps working.
    """
    from . import reports
    rows = reports.invoice_price_table(n_invoices=VM_N_INVOICES, q=q, limit=100000)
    key = _VM_SORT_KEYS.get(sort_by, "total_net")
    rows.sort(key=lambda r: (r.get(key) is None, r.get(key) or 0),
              reverse=(order.lower() == "desc"))
    page = rows[offset:offset + limit]
    out = []
    for r in page:
        out.append({
            "sku": r["sku"],
            "name": r["name"],
            "total_qty": r["total_qty"],
            "n_orders": r["n_orders"],
            "last_date": r["last_date"],
            "list_gross": r["list_gross"],          # מחירון
            "avg_gross": r["avg_gross"],            # ממוצע משוקלל
            "total_gross": r["total_gross"],        # סה"כ עלות (ברוטו)
            "discount_pct": r["discount_pct"],
            "basis": "invoices",                    # תמחור מחשבוניות
            "n_invoices_basis": VM_N_INVOICES,      # מבוסס על 12 חשבוניות אחרונות
            "price_gross": r["avg_gross"],          # legacy + cross-compare anchor
            "price_net": r["avg_net"],
        })
    return out


def _vetmarket_count(q: str = "") -> int:
    from . import reports
    return len(reports.invoice_price_table(n_invoices=VM_N_INVOICES, q=q, limit=100000))


def _medimarket_search(q: str = "", limit: int = 100, offset: int = 0,
                       sort_by: str = "price", order: str = "desc",
                       category: str | None = None) -> list[dict]:
    """Search/browse Medi-Market products. Prices are GROSS (including VAT)."""
    if not MEDIMARKET_DB.exists():
        return []
    sort_col = _MM_SORT_COLS.get(sort_by, "price")
    order_sql = "ASC" if order.lower() == "asc" else "DESC"
    conn = sqlite3.connect(MEDIMARKET_DB)
    conn.row_factory = sqlite3.Row
    where = ["price > 0"]
    params: list = []
    if q:
        where.append("(name LIKE ? OR name_normalized LIKE ? OR sku LIKE ?)")
        pat = f"%{q}%"
        params.extend([pat, pat, pat])
    if category:
        where.append("categories LIKE ?")
        params.append(f"%{category}%")
    where_sql = "WHERE " + " AND ".join(where)
    sql = f"""
        SELECT sku, name,
               price AS price_gross,
               ROUND(price / 1.18, 2) AS price_net,
               regular_price AS list_gross,   -- מחירון (לפני מבצע)
               sale_price,                    -- מחיר מבצע (אם on_sale)
               on_sale,
               permalink, in_stock, categories
        FROM products
        {where_sql}
        ORDER BY in_stock DESC, {sort_col} {order_sql}
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, (*params, limit, offset)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        # discount = how far below list the current price sits
        lp, pg = d.get("list_gross"), d.get("price_gross")
        try:
            lp = float(lp) if lp not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            lp = None
        d["list_gross"] = lp
        d["discount_pct"] = (round((lp - pg) / lp * 100, 1)
                             if lp and pg and lp > pg else None)
        d["basis"] = "site"   # מחירון אתר — לא מחשבוניות (אין רכש שמור למדימרקט)
        out.append(d)
    return out


def _medimarket_count(q: str = "", category: str | None = None) -> int:
    if not MEDIMARKET_DB.exists():
        return 0
    conn = sqlite3.connect(MEDIMARKET_DB)
    where = ["price > 0"]
    params: list = []
    if q:
        where.append("(name LIKE ? OR name_normalized LIKE ? OR sku LIKE ?)")
        pat = f"%{q}%"
        params.extend([pat, pat, pat])
    if category:
        where.append("categories LIKE ?")
        params.append(f"%{category}%")
    where_sql = "WHERE " + " AND ".join(where)
    n = conn.execute(f"SELECT COUNT(*) FROM products {where_sql}", params).fetchone()[0]
    conn.close()
    return n


def _filter_by_ingredient(items: list[dict], ingredient: str) -> list[dict]:
    """Post-filter a list of items by detected active ingredient."""
    if not ingredient:
        return items
    return [it for it in items
            if compare_mod.detect_active_ingredient(it.get("name") or "") == ingredient]


def _annotate_features(items: list[dict]) -> None:
    """Add active/dose/pack to each item in-place."""
    for it in items:
        it["active"] = compare_mod.detect_active_ingredient(it["name"] or "")
        it["dose"] = compare_mod.detect_dosage_mg(it["name"] or "")
        it["pack"] = compare_mod.detect_pack_size(it["name"] or "")
        it.setdefault("other_supplier_price", None)
        it.setdefault("color", None)


def _set_color(it: dict, other_gross: float | None,
               other_name: str | None, other_sku: str | None) -> None:
    if other_gross is None or it.get("price_gross") is None:
        return
    it["other_supplier_price"] = other_gross
    it["other_supplier_name"] = other_name
    it["other_supplier_sku"] = other_sku
    if it["price_gross"] < other_gross:
        it["color"] = "green"
    elif it["price_gross"] > other_gross:
        it["color"] = "red"
    else:
        it["color"] = "equal"


def _annotate_with_compare(vm_items: list[dict], mm_items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Cross-match each item against the FULL other catalog (not just current search results),
    so a product still gets colored even when its equivalent wasn't found by the same search term.
    """
    _annotate_features(vm_items)
    _annotate_features(mm_items)

    # Need to search the full opposite catalogs by active ingredient
    # to find true equivalents (not limited to search hits)
    actives_in_vm = {v["active"] for v in vm_items if v["active"]}
    actives_in_mm = {m["active"] for m in mm_items if m["active"]}
    actives_to_lookup_in_mm = actives_in_vm  # for VM items, find in MM
    actives_to_lookup_in_vm = actives_in_mm  # for MM items, find in VM

    # Pull all MM products that share an active with VM hits
    extra_mm_index = compare_mod.load_medimarket_products() if actives_to_lookup_in_mm else []
    extra_vm_index = compare_mod.load_vetmarket_catalog() if actives_to_lookup_in_vm else []

    # Map: active → list of products (sorted cheapest first)
    mm_by_active: dict[str, list[dict]] = {}
    for m in extra_mm_index:
        if m.get("active"):
            mm_by_active.setdefault(m["active"], []).append(m)
    for k in mm_by_active:
        mm_by_active[k].sort(key=lambda x: x.get("price") or 1e18)

    vm_by_active: dict[str, list[dict]] = {}
    for v in extra_vm_index:
        if v.get("active") and v.get("avg_unit_price"):
            vm_by_active.setdefault(v["active"], []).append(v)
    for k in vm_by_active:
        vm_by_active[k].sort(key=lambda x: (x.get("avg_unit_price") or 0) * 1.18)

    # For each VM hit → find cheapest MM with same active+dose (or just active)
    for v in vm_items:
        if not v["active"]:
            continue
        cands = mm_by_active.get(v["active"], [])
        if v["dose"]:
            tighter = [c for c in cands if c.get("dose") == v["dose"]]
            if tighter:
                cands = tighter
        if not cands:
            continue
        m = cands[0]
        m_gross = m.get("price")  # MM price IS gross
        _set_color(v, m_gross, m.get("name"), m.get("sku"))

    # For each MM hit → find cheapest VM with same active+dose
    for mh in mm_items:
        if not mh["active"]:
            continue
        cands = vm_by_active.get(mh["active"], [])
        if mh["dose"]:
            tighter = [c for c in cands if c.get("dose") == mh["dose"]]
            if tighter:
                cands = tighter
        if not cands:
            continue
        v = cands[0]
        v_gross = round((v.get("avg_unit_price") or 0) * 1.18, 2)
        _set_color(mh, v_gross, v.get("name"), v.get("sku"))

    return vm_items, mm_items


# ---------- API endpoints ----------

@app.get("/api/search")
def api_search(
    q: str = Query("", description="Hebrew/English search term"),
    supplier: str = Query("both", pattern="^(both|vetmarket|medimarket)$"),
    sort_by: str = Query("spend", pattern="^(name|price|spend|qty)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    category: str = Query("", description="Filter MM by category substring"),
    ingredient: str = Query("", description="Filter both by detected active ingredient"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
):
    """Browse + search both catalogs with filters and sort.

    Empty q + filters/sort = browse mode.
    """
    offset = (page - 1) * page_size
    cat = category.strip() or None
    ing = ingredient.strip() or None

    # When filtering by ingredient, we must scan the FULL catalog (post-filter).
    fetch_limit = 10000 if ing else page_size

    vm: list[dict] = []
    mm: list[dict] = []
    vm_total = 0
    mm_total = 0

    if supplier in ("both", "vetmarket"):
        vm = _vetmarket_search(q, fetch_limit, offset if not ing else 0,
                               sort_by=sort_by, order=order)
        if ing:
            filtered = _filter_by_ingredient(vm, ing)
            vm_total = len(filtered)
            vm = filtered[offset:offset + page_size]
        else:
            vm_total = _vetmarket_count(q)

    if supplier in ("both", "medimarket"):
        mm_sort = sort_by if sort_by in ("name", "price") else "price"
        mm = _medimarket_search(q, fetch_limit, offset if not ing else 0,
                                sort_by=mm_sort, order=order, category=cat)
        if ing:
            filtered = _filter_by_ingredient(mm, ing)
            mm_total = len(filtered)
            mm = filtered[offset:offset + page_size]
        else:
            mm_total = _medimarket_count(q, category=cat)

    # Always annotate cross-supplier
    vm, mm = _annotate_with_compare(vm, mm)

    return {
        "vetmarket": vm,
        "medimarket": mm,
        "vetmarket_total": vm_total,
        "medimarket_total": mm_total,
        "page": page,
        "page_size": page_size,
        "query": q,
        "supplier": supplier,
        "sort_by": sort_by,
        "order": order,
        "category": cat,
        "ingredient": ing,
    }


@app.get("/api/categories")
def api_categories(supplier: str = Query("medimarket", pattern="^(medimarket)$")):
    """List Medi-Market categories with product counts (>0 price)."""
    if not MEDIMARKET_DB.exists():
        return {"categories": []}
    conn = sqlite3.connect(MEDIMARKET_DB)
    rows = conn.execute(
        "SELECT categories FROM products WHERE price > 0"
    ).fetchall()
    conn.close()
    counts: dict[str, int] = {}
    for (cats_json,) in rows:
        try:
            cats = json.loads(cats_json) if cats_json else []
        except Exception:
            cats = []
        for c in cats:
            counts[c] = counts.get(c, 0) + 1
    out = sorted(counts.items(), key=lambda kv: -kv[1])
    return {"categories": [{"name": k, "count": v} for k, v in out]}


@app.get("/api/ingredients")
def api_ingredients():
    """List all detectable active ingredients (canonical keys)."""
    return {
        "ingredients": sorted(compare_mod.ACTIVE_INGREDIENTS.keys())
    }


@app.get("/api/comparison")
def api_comparison(
    from_: str = Query("2025-01-01", alias="from"),
    sort_by: str = Query("savings", pattern="^(savings|gap|active)$"),
    limit: int = 200,
):
    """Return all matched comparison rows."""
    rows = compare_mod.find_matches(from_)
    if sort_by == "gap":
        rows.sort(key=lambda r: abs(r.delta_pct), reverse=True)
    elif sort_by == "active":
        rows.sort(key=lambda r: (r.active_ingredient or "", r.dosage_mg or 0))
    else:
        rows.sort(key=lambda r: r.annual_savings_if_switched, reverse=True)

    rows = rows[:limit]
    out = [asdict(r) for r in rows]

    # Stats
    all_rows = compare_mod.find_matches(from_)
    total = sum(r.annual_savings_if_switched for r in all_rows
                if r.annual_savings_if_switched > 0)
    losses = sum(-r.annual_savings_if_switched for r in all_rows
                 if r.annual_savings_if_switched < 0)

    return {
        "rows": out,
        "stats": {
            "matches": len(all_rows),
            "vetmarket_cheaper": sum(1 for r in all_rows if r.cheaper_at == "vetmarket"),
            "medimarket_cheaper": sum(1 for r in all_rows if r.cheaper_at == "medimarket"),
            "potential_annual_savings": round(total, 2),
            "loss_if_blind_switch": round(losses, 2),
        },
        "period_from": from_,
    }


@app.get("/api/stats")
def api_stats():
    """Top-line numbers for the dashboard banner."""
    from . import reports
    vm_rows = reports.invoice_price_table(n_invoices=VM_N_INVOICES, limit=100000)
    n_v = len(vm_rows)
    v_total = sum(r.get("total_net") or 0 for r in vm_rows)

    n_m = 0
    if MEDIMARKET_DB.exists():
        conn = sqlite3.connect(MEDIMARKET_DB)
        n_m = conn.execute(
            "SELECT COUNT(*) FROM products WHERE price > 0"
        ).fetchone()[0]
        conn.close()

    return {
        "vetmarket_products": n_v,
        "vetmarket_spend_net": round(v_total, 2),
        "vetmarket_spend_gross": round(v_total * 1.18, 2),
        "vetmarket_basis_invoices": VM_N_INVOICES,
        "medimarket_products": n_m,
        "last_updated": reports.last_updated(),
    }


# ---------- HTML ----------

@app.get("/", response_class=HTMLResponse)
def index():
    html_path = STATIC_DIR / "index.html"
    if not html_path.exists():
        return HTMLResponse(
            "<h1>index.html missing</h1>"
            "<p>Expected at: {}</p>".format(html_path),
            status_code=500,
        )
    return HTMLResponse(html_path.read_text(encoding="utf-8"))
