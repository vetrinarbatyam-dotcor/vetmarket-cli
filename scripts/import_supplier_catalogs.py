# -*- coding: utf-8 -*-
"""One-off import: Royal Canin (freshly parsed PDF) + clinic-pal-hub's
supplier_catalogs export (VetLife, Purina, Monge, Beit Erez, Hill's, ...)
into vetmarket-cli's own supplier_catalog_items table.

Run once from repo root: python scripts/import_supplier_catalogs.py
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vetmarket.db import get_conn  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RC_JSON = ROOT / "data" / "royal_canin_catalog.json"
EXPORT_JSON = ROOT / "data" / "supplier_catalogs_export.json"

# Sources already covered live elsewhere in vetmarket-cli (vetmarket.db / medimarket)
# or not relevant here (insurance) -> excluded from the import.
SKIP_SUPPLIERS = {"וטמרקט", "מדי-מרקט", "מרפאט"}

SUPPLIER_RENAME = {
    "Purina": "Purina Pro Plan",
    "Purina חנויות": "Purina Pro Plan (חנויות)",
    "Hill": "Hill's",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def import_royal_canin(conn):
    data = json.loads(RC_JSON.read_text(encoding="utf-8"))
    rows = 0
    ts = now_iso()
    for it in data["items"]:
        conn.execute(
            """INSERT INTO supplier_catalog_items
               (supplier, sku, name, category, animal, price_no_vat, price_with_vat,
                price_list_date, source, notes, imported_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["supplier"],
                it["sku"],
                it["name"],
                it.get("tag"),
                it.get("animal"),
                it["price_no_vat"],
                it["price_with_vat"],
                data["valid_from"],
                data["source_file"],
                None,
                ts,
            ),
        )
        rows += 1
    return rows


def import_clinicpal_export(conn):
    data = json.loads(EXPORT_JSON.read_text(encoding="utf-8"))
    ts = now_iso()
    rows = 0
    for r in data:
        supplier = r["supplier_name"]
        if supplier in SKIP_SUPPLIERS:
            continue
        supplier = SUPPLIER_RENAME.get(supplier, supplier)
        created = (r.get("created_at") or "")[:7]  # 'YYYY-MM'
        conn.execute(
            """INSERT INTO supplier_catalog_items
               (supplier, sku, name, category, animal, price_no_vat, price_with_vat,
                price_list_date, source, notes, imported_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                supplier,
                r.get("code") or None,
                r["item_name"],
                r.get("category"),
                r.get("notes"),  # animal is stashed in notes for this table
                r.get("price_no_vat"),
                r.get("price_with_vat"),
                created or None,
                "clinic-pal-hub/supplier_catalogs",
                None,
                ts,
            ),
        )
        rows += 1
    return rows


def main():
    conn = get_conn()
    conn.execute("DELETE FROM supplier_catalog_items")
    n1 = import_royal_canin(conn)
    n2 = import_clinicpal_export(conn)
    conn.commit()
    cur = conn.execute(
        "SELECT supplier, COUNT(*) FROM supplier_catalog_items GROUP BY supplier ORDER BY COUNT(*) DESC"
    )
    print(f"Royal Canin (parsed): {n1} rows")
    print(f"clinic-pal-hub export: {n2} rows")
    print("--- by supplier ---")
    for supplier, count in cur.fetchall():
        print(f"{supplier}: {count}")
    conn.close()


if __name__ == "__main__":
    main()
