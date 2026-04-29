"""FastAPI dashboard for searching/comparing Vetmarket vs Medi-Market."""
from __future__ import annotations
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, compare as compare_mod
from .config import DATA_DIR

app = FastAPI(title="Vetmarket Dashboard")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

MEDIMARKET_DB = DATA_DIR / "medimarket" / "prices.db"


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


def _vetmarket_search(q: str = "", limit: int = 100, offset: int = 0,
                      sort_by: str = "spend", order: str = "desc") -> list[dict]:
    """Search/browse Vetmarket products in purchases_summary (the catalog) — net prices."""
    sort_col = _VM_SORT_COLS.get(sort_by, "ps.total_amount")
    order_sql = "ASC" if order.lower() == "asc" else "DESC"
    where_clauses = []
    params: list = []
    if q:
        where_clauses.append("(ps.name LIKE ? OR ps.sku LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    where_sql = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    sql = f"""
        SELECT
            ps.sku, ps.name, ps.total_qty, ps.total_amount,
            ps.avg_unit_price AS price_net,
            ROUND(ps.avg_unit_price * 1.18, 2) AS price_gross,
            ps.period_from, ps.period_to
        FROM purchases_summary ps
        {where_sql}
        ORDER BY {sort_col} {order_sql} NULLS LAST
        LIMIT ? OFFSET ?
    """
    with db.cursor() as c:
        rows = c.execute(sql, (*params, limit, offset)).fetchall()
    return [dict(r) for r in rows]


def _vetmarket_count(q: str = "") -> int:
    where_sql = ""
    params = []
    if q:
        where_sql = "WHERE (name LIKE ? OR sku LIKE ?)"
        params = [f"%{q}%", f"%{q}%"]
    with db.cursor() as c:
        return c.execute(
            f"SELECT COUNT(*) FROM purchases_summary {where_sql}", params
        ).fetchone()[0]


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
        SELECT sku, name, price AS price_gross,
               ROUND(price / 1.18, 2) AS price_net,
               permalink, in_stock, categories
        FROM products
        {where_sql}
        ORDER BY in_stock DESC, {sort_col} {order_sql}
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, (*params, limit, offset)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
    with db.cursor() as c:
        n_v = c.execute(
            "SELECT COUNT(DISTINCT sku) FROM purchases_summary"
        ).fetchone()[0]
        v_total = c.execute(
            "SELECT SUM(total_amount) FROM purchases_summary"
        ).fetchone()[0] or 0

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
        "medimarket_products": n_m,
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
