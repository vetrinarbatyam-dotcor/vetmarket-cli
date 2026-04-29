"""Catalog builder.

The /purchases Excel export gives net (pre-VAT) per-SKU totals.
- Full-range export → current catalog of avg-unit-price per SKU
- Month-by-month exports → price history per SKU
"""
from __future__ import annotations
import openpyxl
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from . import db
from .client import client
from .config import DATA_DIR


CATALOG_DIR = DATA_DIR / "catalog"
CATALOG_DIR.mkdir(exist_ok=True)


def _fmt(d: date) -> str:
    """Site uses dd/mm/yy."""
    return d.strftime("%d/%m/%y")


def _month_starts(start: date, end: date) -> list[tuple[date, date]]:
    """Yield (first, last) of each month covering [start..end]."""
    out = []
    cur = start.replace(day=1)
    while cur <= end:
        # last day of month
        if cur.month == 12:
            nxt = cur.replace(year=cur.year + 1, month=1)
        else:
            nxt = cur.replace(month=cur.month + 1)
        last = min(nxt - timedelta(days=1), end)
        first = max(cur, start)
        out.append((first, last))
        cur = nxt
    return out


def _parse_purchases_xlsx(path: Path) -> list[dict]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    items = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or row[0] is None:
            continue
        sku = str(row[0]).strip()
        name = str(row[1]).strip() if row[1] else ""
        try:
            qty = float(row[2]) if row[2] is not None else 0.0
        except ValueError:
            qty = 0.0
        try:
            total_net = float(row[4]) if row[4] is not None else 0.0
        except ValueError:
            total_net = 0.0
        avg = round(total_net / qty, 4) if qty else None
        items.append({
            "sku": sku,
            "name": name,
            "qty": qty,
            "total_net": total_net,
            "avg_unit_price_net": avg,
        })
    return items


def download_period(date_from: date, date_to: date) -> Path:
    """Download /purchases xlsx for a date range."""
    cli = client()
    fname = f"purchases_{date_from.isoformat()}_{date_to.isoformat()}.xlsx"
    path = CATALOG_DIR / fname
    if path.exists() and path.stat().st_size > 1000:
        return path
    cli.download_excel("purchases", path,
                       date_from=_fmt(date_from), date_to=_fmt(date_to))
    return path


def build_catalog(date_from: date, date_to: date | None = None,
                  monthly_history: bool = True) -> dict:
    """Download full catalog + (optionally) monthly history. Returns counts."""
    if date_to is None:
        date_to = date.today()

    # 1. Full-range catalog (the "current" net prices over the whole period)
    full_path = download_period(date_from, date_to)
    catalog_items = _parse_purchases_xlsx(full_path)

    # 2. Month-by-month history (one file per month)
    history_observations = 0
    if monthly_history:
        for first, last in _month_starts(date_from, date_to):
            try:
                p = download_period(first, last)
                items = _parse_purchases_xlsx(p)
            except Exception:
                continue
            obs_date = last.isoformat()
            with db.cursor() as c:
                for it in items:
                    if not it["avg_unit_price_net"]:
                        continue
                    db.upsert_product(c, sku=it["sku"], name_he=it["name"],
                                      last_seen=datetime.utcnow().isoformat())
                    db.add_price_observation(
                        c, it["sku"], obs_date, it["avg_unit_price_net"],
                        "purchases-avg-month",
                        qty=it["qty"],
                        source_id=f"{first.isoformat()}_{last.isoformat()}",
                        notes=f"net price; period {first}..{last}; "
                              f"{it['qty']} units @ {it['total_net']:.2f}₪ net",
                    )
                    history_observations += 1

    # 3. Catalog snapshot for the full range (one row per SKU)
    catalog_rows = 0
    now = datetime.utcnow().isoformat()
    with db.cursor() as c:
        for it in catalog_items:
            db.upsert_product(c, sku=it["sku"], name_he=it["name"], last_seen=now)
            if it["avg_unit_price_net"]:
                c.execute(
                    "INSERT INTO purchases_summary "
                    "(sku, period_from, period_to, name, total_qty, total_amount, avg_unit_price, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(sku, period_from, period_to) DO UPDATE SET "
                    "total_qty=excluded.total_qty, total_amount=excluded.total_amount, "
                    "avg_unit_price=excluded.avg_unit_price, fetched_at=excluded.fetched_at",
                    (it["sku"], date_from.isoformat(), date_to.isoformat(), it["name"],
                     it["qty"], it["total_net"], it["avg_unit_price_net"], now),
                )
                # Also write a "catalog" price observation for the full range
                db.add_price_observation(
                    c, it["sku"], date_to.isoformat(),
                    it["avg_unit_price_net"], "purchases-avg-range",
                    qty=it["qty"],
                    source_id=f"{date_from.isoformat()}_{date_to.isoformat()}",
                    notes=f"net catalog avg; full range "
                          f"{date_from}..{date_to}; {it['qty']} units @ {it['total_net']:.2f}₪ net",
                )
                catalog_rows += 1

    return {
        "from": date_from.isoformat(),
        "to": date_to.isoformat(),
        "products": len(catalog_items),
        "catalog_rows_written": catalog_rows,
        "monthly_history_obs": history_observations,
    }


def latest_catalog_view(date_from: date, date_to: date) -> list[dict]:
    """Return the catalog rows for a given date range from purchases_summary."""
    with db.cursor() as c:
        rows = c.execute(
            "SELECT sku, name, total_qty, total_amount, avg_unit_price "
            "FROM purchases_summary WHERE period_from = ? AND period_to = ? "
            "ORDER BY total_amount DESC",
            (date_from.isoformat(), date_to.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


def price_history_per_sku(sku: str) -> list[dict]:
    """Monthly net price observations for one SKU."""
    with db.cursor() as c:
        rows = c.execute(
            "SELECT observed_date, unit_price, qty, source_id, notes "
            "FROM prices WHERE sku = ? AND source = 'purchases-avg-month' "
            "ORDER BY observed_date ASC",
            (sku,),
        ).fetchall()
    return [dict(r) for r in rows]


def export_catalog_csv(date_from: date, date_to: date, out_path: Path) -> Path:
    """Export the catalog as CSV — column-aligned for Medi-Market matching."""
    import csv
    rows = latest_catalog_view(date_from, date_to)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sku", "name", "total_qty", "total_amount_net",
                    "avg_unit_price_net", "supplier"])
        for r in rows:
            w.writerow([r["sku"], r["name"], r["total_qty"],
                        r["total_amount"], r["avg_unit_price"], "vetmarket"])
    return out_path


def export_catalog_json(date_from: date, date_to: date, out_path: Path) -> Path:
    import json
    rows = latest_catalog_view(date_from, date_to)
    payload = {
        "supplier": "vetmarket",
        "currency": "ILS",
        "vat_status": "net",  # all prices are pre-VAT
        "vat_rate_pct": 18,
        "period_from": date_from.isoformat(),
        "period_to": date_to.isoformat(),
        "products": [
            {
                "sku": r["sku"],
                "name": r["name"],
                "qty_purchased": r["total_qty"],
                "total_paid_net": r["total_amount"],
                "avg_unit_price_net": r["avg_unit_price"],
            }
            for r in rows
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return out_path
