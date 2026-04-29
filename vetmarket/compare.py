"""Match Vetmarket products with Medi-Market equivalents and compare prices.

Strategy:
1. Extract structured features (active ingredient, dosage_mg, pack_size) from each product name.
2. Build a normalized key per product.
3. Match exactly on (active_ingredient, dosage, pack_size). If only 1-of-2 attrs match,
   keep as a "weak" candidate for human review.
4. For non-pharmaceutical brands (Hill's, Royal Canin, Virbac), match by product name similarity.
5. Compute price gap (assume Medi-Market is GROSS — multiply Vetmarket NET by 1.18 for fair comparison)
"""
from __future__ import annotations
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

from .config import DATA_DIR
from . import db

MEDIMARKET_DB = DATA_DIR / "medimarket" / "prices.db"

# --- Active ingredient dictionary ---
# Map all known synonyms (Hebrew, English, brand names) to a canonical key.
# Brand names in PARENS are not interchangeable — only generic names go here.
ACTIVE_INGREDIENTS = {
    # Doxycycline family
    "doxycycline": [
        "doxycycline", "doxycyclin", "doxylin", "doxyval", "doxycare",
        "דוקסיציקלין", "דוקסיציקלין", "דוקסילין", "דוקסיקר", "דוקסיוול",
    ],
    # Marbofloxacin
    "marbofloxacin": [
        "marbofloxacin", "marbocare", "marbosyva", "marbocyl", "marbofloks",
        "מרבופלוקסצין", "מרבופלוקסציין", "מרבוסילין", "מרבוצין", "מרבוקר",
    ],
    # Metronidazole
    "metronidazole": [
        "metronidazole", "metrocare", "metronid", "flagyl",
        "מטרונידזול", "מטרוקר", "מטרונ",
    ],
    # Cephalexin
    "cephalexin": [
        "cephalexin", "cephadrin", "cefabactin", "cefalexin",
        "צפלקסין", "צפבקטין", "צפלקסין",
    ],
    # Famotidine
    "famotidine": [
        "famotidine", "famotin",
        "פמוטידין",
    ],
    # Fluoxetine
    "fluoxetine": [
        "fluoxetine", "prozac",
        "פלוקסטין",
    ],
    # Meloxicam
    "meloxicam": [
        "meloxicam", "loxicom", "metacam",
        "מלוקסיקם", "לוקסיקום", "מטקאם",
    ],
    # Sarolaner (Simparica family)
    "sarolaner": [
        "sarolaner", "simparica", "סימפריקה", "simparika",
    ],
    # Fluralaner (Bravecto family)
    "fluralaner": [
        "fluralaner", "bravecto", "ברבקטו", "ברווקטו",
    ],
    # Afoxolaner (NexGard family)
    "afoxolaner": [
        "afoxolaner", "nexgard", "נקסגרד",
    ],
    # Lotilaner (Credelio)
    "lotilaner": [
        "lotilaner", "credelio", "קרדיליו",
    ],
    # Maropitant
    "maropitant": [
        "maropitant", "cerenia", "סרניה", "מרופיטנט",
    ],
    # Amoxicillin / clavulanic acid (Synulox / Augmentin family)
    "amoxicillin-clavulanic": [
        "amoxicillin clavulanic", "amoxicillin / clav", "synulox", "augmentin",
        "סינולוקס", "אוגמנטין", "אמוקסיצילין קלובולנית",
    ],
    # Apoquel
    "oclacitinib": [
        "oclacitinib", "apoquel", "אפוקוול", "אפקוול",
    ],
    # Cytopoint (lokivetmab) — biologic, only one brand
    # Frunevetmab (Solensia) — biologic
    # Drontal (praziquantel/pyrantel/febantel)
    "drontal-combo": [
        "drontal", "drontal plus", "דרונטל",
    ],
    # Praziquantel
    "praziquantel": [
        "praziquantel", "droncit",
        "פרזיקוונטל", "דרונציט",
    ],
    # Furosemide
    "furosemide": [
        "furosemide", "lasix", "fursemid",
        "פורוסמיד", "לסיקס",
    ],
    # Buprenorphine
    "buprenorphine": [
        "buprenorphine", "vetergesic",
        "בופרנורפין",
    ],
    # Tramadol
    "tramadol": [
        "tramadol", "tramal",
        "טרמדול",
    ],
    # Gabapentin
    "gabapentin": [
        "gabapentin",
        "גבפנטין",
    ],
    # Insulin
    "insulin-glargine": ["lantus", "glargine"],
    "insulin-zinc": ["caninsulin", "vetsulin"],
    # Cytopoint
    "lokivetmab": ["cytopoint", "ציטופוינט"],
    # Pimobendan
    "pimobendan": ["pimobendan", "vetmedin", "פימובנדן", "וטמדין"],
    # Phenobarbital
    "phenobarbital": ["phenobarbital", "פנוברביטל"],
    # Levetiracetam
    "levetiracetam": ["levetiracetam", "keppra", "לבטירצטם"],
    # Trilostane
    "trilostane": ["trilostane", "vetoryl", "טרילוסטן", "וטוריל"],
    # Methimazole / felimazole
    "methimazole": ["methimazole", "felimazole", "carbimazole",
                    "מתימזול", "פלימזול", "קרבימזול"],
    # Levothyroxine
    "levothyroxine": ["levothyroxine", "forthyron", "thyroxine",
                      "לבותירוקסין", "פורתירון"],
    # Enrofloxacin
    "enrofloxacin": ["enrofloxacin", "baytril", "אנרופלוקסצין", "בייטריל"],
    # Atopica (cyclosporine)
    "cyclosporine": ["cyclosporine", "atopica", "אטופיקה", "ציקלוספורין"],
    # Drontal (already above), Milbemax
    "milbemycin": ["milbemax", "milbemycin", "מילבמקס"],
    # Selamectin
    "selamectin": ["selamectin", "stronghold", "revolution", "סטרונגהולד", "רבולושן"],
    # Imidacloprid+permethrin (Advantix)
    "imidacloprid-permethrin": ["advantix", "אדבנטיקס"],
    # Fipronil (Frontline)
    "fipronil": ["fipronil", "frontline", "פרונטליין", "פרונט ליין"],
    # Ondansetron
    "ondansetron": ["ondansetron", "zofran", "אונדנסטרון", "זופרן"],
    # Maropitant already above
    # Prednisolone
    "prednisolone": ["prednisolone", "פרדניזולון"],
    # Dexamethasone
    "dexamethasone": ["dexamethasone", "דקסמתזון", "דקסה"],
    # Carprofen (Rimadyl)
    "carprofen": ["carprofen", "rimadyl", "קרפרופן", "רימדיל"],
    # Firocoxib (Previcox)
    "firocoxib": ["firocoxib", "previcox", "פירוקוקסיב", "פרביקוקס"],
    # Robenacoxib (Onsior)
    "robenacoxib": ["robenacoxib", "onsior", "אונסיור"],
    # Trazodone
    "trazodone": ["trazodone", "טרזודון"],
    # Cisapride
    "cisapride": ["cisapride", "ציסאפריד"],
    # Diphenhydramine
    "diphenhydramine": ["diphenhydramine", "benadryl", "דיפנהידרמין"],
    # Spironolactone
    "spironolactone": ["spironolactone", "ספירונולקטון"],
    # Enalapril / benazepril
    "enalapril": ["enalapril", "אנלפריל"],
    "benazepril": ["benazepril", "fortekor", "בנזפריל", "פורטקור"],
    # Theophylline
    "theophylline": ["theophylline", "תיאופילין"],
}

# Build reverse lookup: token → canonical name
_INGREDIENT_LOOKUP: dict[str, str] = {}
for canon, syns in ACTIVE_INGREDIENTS.items():
    for s in syns:
        _INGREDIENT_LOOKUP[s.lower()] = canon


# --- Hebrew/English normalization ---

HE_TO_EN_TRANSLITERATIONS = {
    "א": "a", "ב": "b", "ג": "g", "ד": "d", "ה": "h", "ו": "v", "ז": "z",
    "ח": "ch", "ט": "t", "י": "y", "כ": "k", "ך": "k", "ל": "l", "מ": "m",
    "ם": "m", "נ": "n", "ן": "n", "ס": "s", "ע": "a", "פ": "p", "ף": "p",
    "צ": "ts", "ץ": "ts", "ק": "q", "ר": "r", "ש": "sh", "ת": "t",
}


def normalize(name: str) -> str:
    """Lowercase, strip HTML entities, collapse whitespace."""
    if not name:
        return ""
    s = name.lower()
    s = re.sub(r"&quot;|&amp;|&#\d+;|&[a-z]+;", " ", s)
    s = re.sub(r"[\"'״׳`]", "", s)  # strip quote-like punctuation including Hebrew gershayim
    s = re.sub(r"\s+", " ", s).strip()
    return s


def detect_active_ingredient(name: str) -> str | None:
    """Find the canonical active ingredient in a product name."""
    norm = normalize(name)
    # Check longer tokens first to avoid mis-matching (e.g. 'doxy' before 'doxycycline')
    sorted_tokens = sorted(_INGREDIENT_LOOKUP.keys(), key=len, reverse=True)
    for token in sorted_tokens:
        if token in norm:
            return _INGREDIENT_LOOKUP[token]
    return None


DOSAGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(mg|mcg|g\b|kg|ml|gr|מ[\"'״]?ג|מק[\"'״]?ג|גר|מ[\"'״]?ל|ג[\"'״]?ר)",
    re.IGNORECASE,
)


def detect_dosage_mg(name: str) -> float | None:
    """Return the dosage in mg (float). 'mg', 'mcg', 'g' converted appropriately."""
    norm = normalize(name)
    matches = DOSAGE_RE.findall(norm)
    if not matches:
        return None
    # Take the FIRST mg-like match (drugs are usually labeled 'X mg' first)
    for val_s, unit in matches:
        try:
            val = float(val_s)
        except ValueError:
            continue
        u = unit.lower()
        if u in ("mg", "מג", "מ\"ג", "מ'ג", "מ״ג", "מ׳ג"):
            return val
        if u in ("mcg", "מקג", "מק\"ג", "מק׳ג", "מק״ג"):
            return val / 1000
        if u in ("g", "gr", "גר", "ג\"ר"):
            return val * 1000
    return None


PACK_RE = re.compile(
    r"(\d{1,4})\s*(tab|tabs|tablets|טבליות|טבליה|caps|capsules|קפסולות|caps\.)",
    re.IGNORECASE,
)


def detect_pack_size(name: str) -> int | None:
    norm = normalize(name)
    matches = PACK_RE.findall(norm)
    for n_s, _unit in matches:
        try:
            n = int(n_s)
            if 1 <= n <= 9999:
                return n
        except ValueError:
            continue
    return None


# --- Matching ---

@dataclass
class CompareRow:
    vetmarket_sku: str
    vetmarket_name: str
    vetmarket_qty_purchased: float | None
    vetmarket_avg_net: float
    vetmarket_avg_gross: float    # NET * 1.18 for fair comparison
    medimarket_sku: str
    medimarket_name: str
    medimarket_price: float       # what the site shows; treated as gross
    active_ingredient: str | None
    dosage_mg: float | None
    pack_size: int | None
    delta_pct: float              # (medimarket - vetmarket_gross) / vetmarket_gross * 100
    cheaper_at: str               # 'vetmarket' / 'medimarket' / 'equal'
    annual_savings_if_switched: float
    match_type: str               # 'ingredient_dose_pack' / 'ingredient_dose' / 'name_similarity'


def load_medimarket_products(min_price: float = 0.01) -> list[dict]:
    if not MEDIMARKET_DB.exists():
        return []
    conn = sqlite3.connect(MEDIMARKET_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT sku, name, price FROM products WHERE price > ? AND in_stock = 1",
        (min_price,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["active"] = detect_active_ingredient(d["name"])
        d["dose"] = detect_dosage_mg(d["name"])
        d["pack"] = detect_pack_size(d["name"])
        d["norm"] = normalize(d["name"])
        out.append(d)
    return out


def load_vetmarket_catalog(date_from: str = "2025-01-01") -> list[dict]:
    """Pull the vetmarket catalog from purchases_summary for a given period."""
    with db.cursor() as c:
        rows = c.execute(
            "SELECT sku, name, total_qty, total_amount, avg_unit_price "
            "FROM purchases_summary WHERE period_from = ? "
            "ORDER BY total_amount DESC",
            (date_from,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["active"] = detect_active_ingredient(d["name"] or "")
        d["dose"] = detect_dosage_mg(d["name"] or "")
        d["pack"] = detect_pack_size(d["name"] or "")
        d["norm"] = normalize(d["name"] or "")
        out.append(d)
    return out


def find_matches(date_from: str = "2025-01-01") -> list[CompareRow]:
    vm = load_vetmarket_catalog(date_from)
    mm = load_medimarket_products()

    out: list[CompareRow] = []

    # Index Medi-Market by (active, dose, pack)
    mm_by_full = {}    # (active, dose, pack) → list of products
    mm_by_active_dose = {}  # (active, dose) → list
    mm_by_active = {}  # active → list
    for p in mm:
        if p["active"]:
            if p["dose"] and p["pack"]:
                mm_by_full.setdefault((p["active"], p["dose"], p["pack"]), []).append(p)
            if p["dose"]:
                mm_by_active_dose.setdefault((p["active"], p["dose"]), []).append(p)
            mm_by_active.setdefault(p["active"], []).append(p)

    seen_pairs = set()

    def make_row(v: dict, m: dict, match_type: str) -> CompareRow:
        v_net = v["avg_unit_price"] or 0
        v_gross = round(v_net * 1.18, 2)
        m_price = m["price"] or 0
        # delta: positive = medimarket more expensive
        delta = ((m_price - v_gross) / v_gross * 100) if v_gross else 0
        cheaper = "vetmarket" if v_gross < m_price else ("medimarket" if m_price < v_gross else "equal")
        # annual savings = qty_per_year * price diff
        qty_yr = (v["total_qty"] or 0) * (12 / 16)  # we have 16 months of data
        if cheaper == "medimarket":
            savings = (v_gross - m_price) * qty_yr  # vetmarket more, switching saves
        elif cheaper == "vetmarket":
            savings = -(m_price - v_gross) * qty_yr  # would lose money switching
        else:
            savings = 0
        return CompareRow(
            vetmarket_sku=v["sku"],
            vetmarket_name=v["name"],
            vetmarket_qty_purchased=v["total_qty"],
            vetmarket_avg_net=round(v_net, 2),
            vetmarket_avg_gross=v_gross,
            medimarket_sku=m["sku"] or "",
            medimarket_name=m["name"],
            medimarket_price=round(m_price, 2),
            active_ingredient=v["active"],
            dosage_mg=v["dose"],
            pack_size=v["pack"],
            delta_pct=round(delta, 2),
            cheaper_at=cheaper,
            annual_savings_if_switched=round(savings, 2),
            match_type=match_type,
        )

    for v in vm:
        if not v["active"] or not v["avg_unit_price"]:
            continue
        # Strongest: same active + same dose + same pack
        if v["dose"] and v["pack"]:
            cands = mm_by_full.get((v["active"], v["dose"], v["pack"]), [])
            for m in cands:
                key = (v["sku"], m["sku"])
                if key in seen_pairs: continue
                seen_pairs.add(key)
                out.append(make_row(v, m, "ingredient+dose+pack"))
            if cands:
                continue
        # Medium: same active + same dose
        if v["dose"]:
            cands = mm_by_active_dose.get((v["active"], v["dose"]), [])
            for m in cands:
                key = (v["sku"], m["sku"])
                if key in seen_pairs: continue
                seen_pairs.add(key)
                out.append(make_row(v, m, "ingredient+dose"))
            if cands:
                continue
        # Weakest: same active only — only if exactly one candidate exists
        cands = mm_by_active.get(v["active"], [])
        if len(cands) == 1:
            m = cands[0]
            key = (v["sku"], m["sku"])
            if key not in seen_pairs:
                seen_pairs.add(key)
                out.append(make_row(v, m, "ingredient_only"))

    # Sort: biggest annual savings first
    out.sort(key=lambda r: r.annual_savings_if_switched, reverse=True)
    return out


def export_comparison(date_from: str = "2025-01-01",
                      out_csv: Path | None = None,
                      out_json: Path | None = None) -> dict:
    rows = find_matches(date_from)
    out_dir = DATA_DIR / "exports"
    out_dir.mkdir(exist_ok=True)
    if out_csv is None:
        out_csv = out_dir / f"comparison_vetmarket_vs_medimarket_{date_from}.csv"
    if out_json is None:
        out_json = out_dir / f"comparison_vetmarket_vs_medimarket_{date_from}.json"

    import csv
    with open(out_csv, "w", encoding="utf-8-sig", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(asdict(rows[0]).keys()))
            w.writeheader()
            for r in rows:
                w.writerow(asdict(r))

    out_json.write_text(
        json.dumps([asdict(r) for r in rows], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Stats
    total_savings = sum(r.annual_savings_if_switched for r in rows
                        if r.annual_savings_if_switched > 0)
    losses_if_blindly_switch = sum(-r.annual_savings_if_switched for r in rows
                                   if r.annual_savings_if_switched < 0)
    return {
        "matches": len(rows),
        "vetmarket_cheaper": sum(1 for r in rows if r.cheaper_at == "vetmarket"),
        "medimarket_cheaper": sum(1 for r in rows if r.cheaper_at == "medimarket"),
        "tie": sum(1 for r in rows if r.cheaper_at == "equal"),
        "annual_savings_if_switched_to_medi": round(total_savings, 2),
        "annual_loss_if_switched_to_medi": round(losses_if_blindly_switch, 2),
        "csv": str(out_csv),
        "json": str(out_json),
    }


def top_n(rows: list[CompareRow], n: int = 20,
          mode: str = "savings") -> list[CompareRow]:
    """Sort by absolute price gap or annual savings."""
    if mode == "gap":
        return sorted(rows, key=lambda r: abs(r.delta_pct), reverse=True)[:n]
    return sorted(rows, key=lambda r: r.annual_savings_if_switched, reverse=True)[:n]
