"""SQLite schema + helpers. All tables created idempotently on import."""
from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    name_he TEXT,
    name_en TEXT,
    description TEXT,
    category TEXT,
    manufacturer TEXT,
    url TEXT,
    image_url TEXT,
    expiry_date TEXT,         -- batch expiry, latest seen
    is_favorite INTEGER DEFAULT 0,
    last_seen TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    order_date TEXT,
    status TEXT,
    total_amount REAL,        -- nullable; not always exposed
    raw_html TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT,
    sku TEXT,
    name TEXT,
    qty REAL,
    unit_price REAL,
    line_total REAL,
    FOREIGN KEY(order_id) REFERENCES orders(order_id)
);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id TEXT PRIMARY KEY,
    invoice_date TEXT,
    total_amount REAL,
    raw_html TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id TEXT,
    sku TEXT,
    name TEXT,
    qty REAL,
    unit_price REAL,
    line_total REAL,
    discount_pct REAL,
    FOREIGN KEY(invoice_id) REFERENCES invoices(invoice_id)
);

CREATE TABLE IF NOT EXISTS prices (
    -- Append-only price observations. One row per (sku, date, source).
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    observed_date TEXT NOT NULL,    -- date of invoice / order / quote
    unit_price REAL NOT NULL,
    qty REAL,
    source TEXT NOT NULL,           -- 'invoice' | 'order' | 'purchases-avg' | 'price-offer'
    source_id TEXT,                 -- e.g. invoice_id
    notes TEXT,
    UNIQUE(sku, observed_date, source, source_id)
);

CREATE TABLE IF NOT EXISTS shipping_documents (
    doc_id TEXT PRIMARY KEY,
    doc_date TEXT,
    status TEXT,
    raw_html TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS price_offers (
    offer_id TEXT PRIMARY KEY,
    offer_date TEXT,
    status TEXT,
    raw_html TEXT,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS purchases_summary (
    -- Aggregated per (sku, period) — the /purchases page itself.
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT,
    period_from TEXT,
    period_to TEXT,
    name TEXT,
    total_qty REAL,
    total_amount REAL,
    avg_unit_price REAL,
    fetched_at TEXT,
    UNIQUE(sku, period_from, period_to)
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section TEXT,
    started_at TEXT,
    finished_at TEXT,
    rows_added INTEGER,
    rows_updated INTEGER,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_invoice_lines_sku ON invoice_lines(sku);
CREATE INDEX IF NOT EXISTS idx_order_items_sku ON order_items(sku);
CREATE INDEX IF NOT EXISTS idx_prices_sku ON prices(sku);
CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(invoice_date);
CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(order_date);

CREATE TABLE IF NOT EXISTS supplier_catalog_items (
    -- Structured supplier price-list items (Royal Canin, VetLife, Purina, Monge, Beit Erez, Hill's, ...)
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier TEXT NOT NULL,
    sku TEXT,
    name TEXT NOT NULL,
    category TEXT,
    animal TEXT,
    price_no_vat REAL,
    price_with_vat REAL,
    price_list_date TEXT,     -- e.g. '2026-05'
    source TEXT,              -- where this row came from (file name / clinic-pal-hub table)
    notes TEXT,
    imported_at TEXT
);

CREATE TABLE IF NOT EXISTS pricing_calc (
    -- User-entered margin-calculator inputs, keyed per product regardless of which
    -- source table (supplier_catalog_items / products / medimarket) it came from.
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,     -- 'supplier' | 'vetmarket' | 'medimarket'
    item_key TEXT NOT NULL,   -- sku if available, else supplier+name
    purchase_discount_pct REAL DEFAULT 0,
    markup_pct REAL DEFAULT 0,
    updated_at TEXT,
    UNIQUE(source, item_key)
);

CREATE INDEX IF NOT EXISTS idx_supplier_catalog_supplier ON supplier_catalog_items(supplier);
CREATE INDEX IF NOT EXISTS idx_supplier_catalog_name ON supplier_catalog_items(name);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_conn() as c:
        c.executescript(SCHEMA)


@contextmanager
def cursor():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- Convenience writers ---

def upsert_product(c: sqlite3.Connection, **fields):
    sku = fields.pop("sku")
    cols = list(fields.keys())
    placeholders = ", ".join(["?"] * (len(cols) + 1))
    cols_sql = ", ".join(["sku"] + cols)
    update_sql = ", ".join([f"{k}=excluded.{k}" for k in cols])
    c.execute(
        f"INSERT INTO products ({cols_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT(sku) DO UPDATE SET {update_sql}",
        [sku] + list(fields.values()),
    )


def add_price_observation(
    c: sqlite3.Connection,
    sku: str,
    observed_date: str,
    unit_price: float,
    source: str,
    qty: float | None = None,
    source_id: str | None = None,
    notes: str | None = None,
):
    c.execute(
        "INSERT OR IGNORE INTO prices "
        "(sku, observed_date, unit_price, qty, source, source_id, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sku, observed_date, unit_price, qty, source, source_id, notes),
    )


# Auto-init on import
init_db()
