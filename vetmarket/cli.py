"""CLI entry point — `python -m vetmarket <command>` or `vetmarket <command>`."""
from __future__ import annotations
import json as _json
import sys
from typing import Optional

import os
import sys
from datetime import date, datetime
from pathlib import Path
import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

from . import db, reports, sync as sync_mod, catalog as catalog_mod, compare as compare_mod
from .client import client
from .config import DB_PATH, USERNAME, BASE_URL, DATA_DIR

# Force UTF-8 stdout on Windows; otherwise Rich falls into legacy CP1252 path
# that crashes on em-dash, Hebrew etc.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

app = typer.Typer(
    help="Vetmarket CLI - search, analyze, and report on your vetmarket.co.il account.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console(legacy_windows=False, force_terminal=True)


def _table(title: str, rows: list[dict], cols: list[str] | None = None,
           empty_msg: str = "אין נתונים"):
    if not rows:
        console.print(f"[dim]{empty_msg}[/dim]")
        return
    if cols is None:
        cols = list(rows[0].keys())
    t = Table(title=title, box=box.SIMPLE_HEAVY, show_lines=False)
    for c in cols:
        t.add_column(c, overflow="fold")
    for r in rows:
        t.add_row(*[("" if r.get(c) is None else str(r.get(c))) for c in cols])
    console.print(t)


def _maybe_json(rows, as_json: bool) -> bool:
    if as_json:
        typer.echo(_json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return True
    return False


# ============= status / login =============

@app.command()
def status():
    """Show DB row counts + last sync runs."""
    s = reports.status_summary()
    console.print(Panel.fit(
        f"[bold]משתמש:[/bold] {USERNAME}\n"
        f"[bold]Base URL:[/bold] {BASE_URL}\n"
        f"[bold]DB:[/bold] {DB_PATH}\n\n"
        f"מוצרים: {s['products']} (מועדפים: {s['favorites']})\n"
        f"הזמנות: {s['orders']}    חשבוניות: {s['invoices']} ({s['invoice_lines']} שורות)\n"
        f"תצפיות-מחיר: {s['price_observations']}    תעודות משלוח: {s['shipping_docs']}    הצעות מחיר: {s['price_offers']}",
        title="vetmarket-cli status",
    ))
    if s["last_sync"]:
        _table("Last sync runs", s["last_sync"], ["section", "finished_at", "error"])


@app.command()
def login(force: bool = typer.Option(False, "--force", "-f", help="Re-login even if cached session exists")):
    """Test login. Saves session cookies to disk."""
    cli = client()
    ok = cli.login(force=force)
    if ok:
        console.print("[green]✓ Logged in as[/green] " + USERNAME)
    else:
        console.print("[red]✗ Login failed.[/red]")
        raise typer.Exit(1)


# ============= sync =============

sync_app = typer.Typer(help="Pull data from vetmarket → local DB")
app.add_typer(sync_app, name="sync")


@sync_app.command("all")
def sync_all_cmd(invoice_detail: bool = typer.Option(False, "--invoice-detail",
                                                     help="Also fetch each invoice's line items (slow)")):
    """Sync every section."""
    results = sync_mod.sync_all(invoice_detail=invoice_detail)
    _table("Sync results", results,
           ["section", "ok", "added", "error"])


@sync_app.command("products")
def sync_products_cmd():
    r = sync_mod.sync_my_products()
    console.print(f"[green]Synced[/green] {r['added']} products")


@sync_app.command("favorites")
def sync_favorites_cmd():
    r = sync_mod.sync_favorites()
    console.print(f"[green]Synced[/green] {r['added']} favorites")


@sync_app.command("orders")
def sync_orders_cmd():
    r = sync_mod.sync_orders()
    console.print(f"[green]Synced[/green] {r['added']} orders")
    if r.get("open_balance"):
        console.print(f"[bold]ת.מ. פתוחות:[/bold] {r['open_balance']:,.2f} ₪")


@sync_app.command("invoices")
def sync_invoices_cmd(detail: bool = typer.Option(False, "--detail", "-d"),
                      limit: Optional[int] = typer.Option(None, "--limit", "-n")):
    r = sync_mod.sync_invoices(detail=detail, limit=limit)
    console.print(f"[green]Synced[/green] {r['added']} invoices")


@sync_app.command("purchases")
def sync_purchases_cmd():
    r = sync_mod.sync_purchases()
    console.print(f"[green]Synced[/green] {r['added']} purchase summary rows")
    if r.get("period_total"):
        console.print(f"[bold]Period total:[/bold] {r['period_total']:,.2f} ₪")


@sync_app.command("shipping")
def sync_shipping_cmd():
    r = sync_mod.sync_shipping_docs()
    console.print(f"[green]Synced[/green] {r['added']} shipping docs")


@sync_app.command("offers")
def sync_offers_cmd():
    r = sync_mod.sync_price_offers()
    console.print(f"[green]Synced[/green] {r['added']} price offers")


# ============= search / product =============

@app.command()
def search(query: str = typer.Argument(..., help="Hebrew/English/SKU search"),
           limit: int = typer.Option(20, "--limit", "-n"),
           json: bool = typer.Option(False, "--json")):
    """Search local product catalog (sync first to populate)."""
    rows = reports.search_products(query, limit=limit)
    if _maybe_json(rows, json): return
    _table(f"חיפוש: {query}", rows,
           ["sku", "name_he", "manufacturer", "is_favorite"])


@app.command()
def product(sku: str = typer.Argument(...),
            json: bool = typer.Option(False, "--json")):
    """Show product detail + price summary + last 10 invoice prices."""
    p = reports.product_detail(sku)
    if not p:
        console.print(f"[red]לא נמצא מוצר עם מק\"ט {sku}[/red]")
        raise typer.Exit(1)
    summary = reports.price_summary(sku)
    history = reports.price_history(sku)[:10]
    if json:
        typer.echo(_json.dumps({"product": p, "price_summary": summary, "history": history},
                                ensure_ascii=False, indent=2, default=str))
        return
    console.print(Panel.fit(
        f"[bold]{p['name_he'] or ''}[/bold]\n"
        f"{p.get('name_en','') or ''}\n\n"
        f"מק\"ט: {p['sku']}    יצרן: {p.get('manufacturer','—')}\n"
        f"מועדף: {'★' if p.get('is_favorite') else '—'}    תוקף: {p.get('expiry_date','—')}\n"
        f"URL: {p.get('url','')}",
        title=p['sku'],
    ))
    if summary["n"]:
        console.print(
            f"\n[bold]מחיר:[/bold] חשבונית אחרונה {summary.get('latest_invoice_price') or '—'}₪ "
            f"({summary.get('latest_invoice_date') or '—'})    ממוצע: {summary.get('mean_price') or '—'}    "
            f"מינ׳/מקס׳: {summary.get('min_price') or '—'}–{summary.get('max_price') or '—'}\n"
        )
        _table("מחירים אחרונים", history,
               ["observed_date", "unit_price", "qty", "source", "source_id"])
    else:
        console.print("[dim]אין תצפיות מחיר עדיין — הרץ `vetmarket sync invoices --detail`[/dim]")


# ============= orders / invoices / shipping =============

@app.command()
def orders(open_only: bool = typer.Option(False, "--open"),
           limit: int = typer.Option(50, "--limit", "-n"),
           json: bool = typer.Option(False, "--json")):
    """List orders."""
    rows = reports.open_orders() if open_only else reports.all_orders(limit)
    if _maybe_json(rows, json): return
    _table("הזמנות", rows, ["order_id", "order_date", "status"])


@app.command()
def order(order_id: str,
          json: bool = typer.Option(False, "--json")):
    """Show items in a single order."""
    rows = reports.order_items(order_id)
    if _maybe_json(rows, json): return
    _table(f"הזמנה {order_id}", rows, ["sku", "name", "qty"])


@app.command()
def invoices(limit: int = typer.Option(50, "--limit", "-n"),
             json: bool = typer.Option(False, "--json")):
    """List invoices."""
    rows = reports.all_invoices(limit)
    if _maybe_json(rows, json): return
    _table("חשבוניות", rows, ["invoice_id", "invoice_date", "total_amount"])


@app.command()
def invoice(invoice_id: str,
            json: bool = typer.Option(False, "--json")):
    """Show line items in a single invoice."""
    rows = reports.invoice_lines(invoice_id)
    if _maybe_json(rows, json): return
    _table(f"חשבונית {invoice_id}", rows,
           ["sku", "name", "qty", "unit_price", "line_total", "discount_pct"])


# ============= prices =============

prices_app = typer.Typer(help="Price database queries")
app.add_typer(prices_app, name="prices")


@prices_app.command("list")
def prices_list_cmd(limit: int = typer.Option(200, "--limit", "-n"),
                    json: bool = typer.Option(False, "--json")):
    """All known SKUs with latest prices."""
    rows = reports.all_known_prices(limit)
    if _maybe_json(rows, json): return
    _table("מחירון אישי (היסטורי)", rows,
           ["sku", "name_he", "latest_invoice_price", "latest_invoice_date", "latest_avg_price"])


@prices_app.command("show")
def prices_show_cmd(sku: str,
                    json: bool = typer.Option(False, "--json")):
    """Full price history for one SKU."""
    rows = reports.price_history(sku)
    if _maybe_json(rows, json): return
    _table(f"היסטוריית מחיר {sku}", rows,
           ["observed_date", "unit_price", "qty", "source", "source_id"])


@prices_app.command("trend")
def prices_trend_cmd(sku: str,
                     json: bool = typer.Option(False, "--json")):
    """Chronological price trend with delta % between consecutive invoices."""
    rows = reports.price_trend(sku)
    if _maybe_json(rows, json): return
    _table(f"מגמת מחיר {sku}", rows, ["observed_date", "unit_price", "delta", "delta_pct"])


@prices_app.command("anomalies")
def prices_anomalies_cmd(threshold: float = typer.Option(15.0, "--threshold", "-t"),
                         json: bool = typer.Option(False, "--json")):
    """SKUs with abnormal price changes (default ≥ 15% vs prior mean)."""
    rows = reports.price_anomalies(threshold_pct=threshold)
    if _maybe_json(rows, json): return
    _table(f"שינויי מחיר חריגים (≥ {threshold}%)", rows,
           ["sku", "name", "latest_price", "prior_mean", "delta_pct", "direction"])


# ============= analytics =============

analytics = typer.Typer(help="Spend / trend reports")
app.add_typer(analytics, name="spend")


@analytics.command("month")
def spend_month_cmd(months: int = typer.Option(12, "--months", "-m"),
                    json: bool = typer.Option(False, "--json")):
    """Spend per month."""
    rows = reports.spend_by_month(months)
    if _maybe_json(rows, json): return
    _table(f"הוצאה חודשית — {months} חודשים אחרונים", rows,
           ["ym", "invoices", "total"])


@analytics.command("year")
def spend_year_cmd(json: bool = typer.Option(False, "--json")):
    """Spend per year."""
    rows = reports.spend_by_year()
    if _maybe_json(rows, json): return
    _table("הוצאה שנתית", rows, ["y", "invoices", "total"])


@app.command()
def top(by: str = typer.Option("spend", "--by", help="spend|qty"),
        limit: int = typer.Option(20, "--limit", "-n"),
        json: bool = typer.Option(False, "--json")):
    """Top SKUs by spend or quantity (from purchases summary)."""
    rows = reports.top_skus(by=by, limit=limit)
    if _maybe_json(rows, json): return
    _table(f"Top {limit} מוצרים לפי {by}", rows,
           ["sku", "name", "qty", "spend", "avg_unit_price"])


# ============= catalog =============

catalog_app = typer.Typer(help="Build/export net-price catalog from invoices")
app.add_typer(catalog_app, name="catalog")


@catalog_app.command("build")
def catalog_build_cmd(
    from_: str = typer.Option("2025-01-01", "--from", help="Start date YYYY-MM-DD"),
    to: str = typer.Option(None, "--to", help="End date YYYY-MM-DD (default today)"),
    no_history: bool = typer.Option(False, "--no-history",
                                    help="Skip month-by-month history (faster)"),
):
    """Build the net-price catalog (since FROM) + monthly price history.

    Sources from /purchases Excel export. All prices are NET (pre-VAT).
    Site VAT confirmed at 18% by cross-checking invoice totals.
    """
    df = date.fromisoformat(from_)
    dt = date.fromisoformat(to) if to else date.today()
    console.print(f"[bold]Building catalog[/bold] {df} → {dt}"
                  + (" (no monthly history)" if no_history else ""))
    result = catalog_mod.build_catalog(df, dt, monthly_history=not no_history)
    console.print(Panel.fit(
        f"[green]✓ done[/green]\n"
        f"מוצרים שנקנו בתקופה: {result['products']}\n"
        f"שורות catalog: {result['catalog_rows_written']}\n"
        f"תצפיות מחיר חודשיות: {result['monthly_history_obs']}",
        title="catalog build",
    ))


@catalog_app.command("show")
def catalog_show_cmd(
    from_: str = typer.Option("2025-01-01", "--from"),
    to: str = typer.Option(None, "--to"),
    limit: int = typer.Option(50, "--limit", "-n"),
    json: bool = typer.Option(False, "--json"),
):
    """Show the catalog (top N by spend) for a date range."""
    df = date.fromisoformat(from_)
    dt = date.fromisoformat(to) if to else date.today()
    rows = catalog_mod.latest_catalog_view(df, dt)[:limit]
    if not rows:
        console.print("[yellow]No catalog data — run `catalog build` first[/yellow]")
        return
    if _maybe_json(rows, json): return
    _table(f"Vetmarket catalog (NET, pre-VAT) — top {limit} by spend  | {df} → {dt}",
           rows, ["sku", "name", "total_qty", "total_amount", "avg_unit_price"])


@catalog_app.command("history")
def catalog_history_cmd(
    sku: str,
    json: bool = typer.Option(False, "--json"),
):
    """Monthly net-price history for a single SKU."""
    rows = catalog_mod.price_history_per_sku(sku)
    if _maybe_json(rows, json): return
    _table(f"Net price history (monthly) — SKU {sku}", rows,
           ["observed_date", "unit_price", "qty", "source_id"])


@catalog_app.command("export")
def catalog_export_cmd(
    from_: str = typer.Option("2025-01-01", "--from"),
    to: str = typer.Option(None, "--to"),
    fmt: str = typer.Option("csv", "--fmt", help="csv|json"),
    out: str = typer.Option(None, "--out", help="Output file (default ./data/exports/...)"),
):
    """Export the catalog to CSV or JSON (ready for Medi-Market comparison)."""
    df = date.fromisoformat(from_)
    dt = date.fromisoformat(to) if to else date.today()
    if not out:
        out_dir = DATA_DIR / "exports"
        out = out_dir / f"vetmarket_catalog_{df}_{dt}.{fmt}"
    out = Path(out)
    if fmt == "csv":
        catalog_mod.export_catalog_csv(df, dt, out)
    elif fmt == "json":
        catalog_mod.export_catalog_json(df, dt, out)
    else:
        console.print(f"[red]Unknown format: {fmt}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]✓ Exported[/green] → {out}")


# ============= compare with Medi-Market =============

compare_app = typer.Typer(help="Compare Vetmarket prices with Medi-Market equivalents")
app.add_typer(compare_app, name="compare")


@compare_app.command("run")
def compare_run_cmd(
    from_: str = typer.Option("2025-01-01", "--from"),
    by: str = typer.Option("savings", "--by", help="savings|gap"),
    limit: int = typer.Option(30, "--limit", "-n"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Match Vetmarket products against Medi-Market and show top by potential savings."""
    rows = compare_mod.find_matches(from_)
    if not rows:
        console.print("[yellow]No matches found. Run `catalog build` first?[/yellow]")
        return
    top = compare_mod.top_n(rows, n=limit, mode=by)

    if json_out:
        from dataclasses import asdict
        typer.echo(_json.dumps([asdict(r) for r in top], ensure_ascii=False, indent=2, default=str))
        return

    # Stats banner
    total = sum(r.annual_savings_if_switched for r in rows if r.annual_savings_if_switched > 0)
    n_v = sum(1 for r in rows if r.cheaper_at == "vetmarket")
    n_m = sum(1 for r in rows if r.cheaper_at == "medimarket")
    console.print(Panel.fit(
        f"[bold]התאמות:[/bold] {len(rows)}\n"
        f"זול ב-Vetmarket: {n_v}    זול ב-Medi-Market: {n_m}\n"
        f"[green]חיסכון פוטנציאלי שנתי אם נעבור ל-Medi-Market במוצרים שזולים שם:[/green] "
        f"{total:,.0f} ₪",
        title="Compare summary",
    ))

    # Table
    rows_display = [{
        "act": (r.active_ingredient or "")[:14],
        "dose": (f"{r.dosage_mg:.0f}" if r.dosage_mg else ""),
        "pack": r.pack_size or "",
        "vetmarket": r.vetmarket_name[:40],
        "VM net":  f"{r.vetmarket_avg_net:.2f}",
        "VM gross": f"{r.vetmarket_avg_gross:.2f}",
        "medimarket": r.medimarket_name[:40],
        "MM": f"{r.medimarket_price:.2f}",
        "Δ%": f"{r.delta_pct:+.1f}",
        "cheaper": "VM" if r.cheaper_at == "vetmarket" else ("MM" if r.cheaper_at == "medimarket" else "="),
        "year ₪": f"{r.annual_savings_if_switched:+,.0f}",
        "match": r.match_type[:10],
    } for r in top]
    _table(f"Top {limit} by {by}", rows_display,
           ["act", "dose", "pack", "vetmarket", "VM net", "VM gross",
            "medimarket", "MM", "Δ%", "cheaper", "year ₪", "match"])


@compare_app.command("export")
def compare_export_cmd(
    from_: str = typer.Option("2025-01-01", "--from"),
):
    """Export full comparison as CSV + JSON."""
    stats = compare_mod.export_comparison(date_from=from_)
    console.print(Panel.fit(
        f"[bold]Matches:[/bold] {stats['matches']}\n"
        f"זול ב-Vetmarket: {stats['vetmarket_cheaper']}\n"
        f"זול ב-Medi-Market: {stats['medimarket_cheaper']}\n"
        f"שווה: {stats['tie']}\n"
        f"[green]חיסכון שנתי אם עוברים ל-MM במוצרים שזול שם: {stats['annual_savings_if_switched_to_medi']:,.0f} ₪[/green]\n"
        f"[red]הפסד אם בטעות נעבור הכל: {stats['annual_loss_if_switched_to_medi']:,.0f} ₪[/red]\n\n"
        f"CSV: {stats['csv']}\n"
        f"JSON: {stats['json']}",
        title="Export complete",
    ))


# ============= dashboard =============

@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(3030, "--port", "-p"),
    reload: bool = typer.Option(False, "--reload",
                                help="Auto-reload on code changes (dev)"),
):
    """Launch the web dashboard (FastAPI + HTML)."""
    import uvicorn
    console.print(Panel.fit(
        f"[bold]Vetmarket dashboard[/bold]\n"
        f"Open: [link]http://{host}:{port}[/link]\n"
        f"Stop: Ctrl-C",
        title="🐾 dashboard",
    ))
    uvicorn.run("vetmarket.web:app", host=host, port=port,
                reload=reload, log_level="info")


# ============= reports list =============

@app.command()
def commands():
    """Print every available command (alias of --help, easier scan)."""
    typer.echo(__doc__ or "")
    typer.echo("\nUse `vetmarket --help` for full reference.")


if __name__ == "__main__":
    app()
