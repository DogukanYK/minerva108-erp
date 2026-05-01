"""
Phase 9 hotfix — bulk barcode + ingredient verification import from the
3 brand product-list Excels (Serenida / Minerva 108 / Evanira).

USAGE
─────
  Dry-run (default — only reports, no DB writes):
      python3 scripts/import_barcodes.py

  Apply confident barcode updates to DB:
      python3 scripts/import_barcodes.py --commit

  Also export per-product ingredient list (TSV for review):
      python3 scripts/import_barcodes.py --ingredients-tsv ingredients.tsv

The Excel paths are hard-coded for the user's machine; override via:
      python3 scripts/import_barcodes.py --excel-dir "/path/to/PRODUCT LIST/İNGİLİZCE"

MATCHING
────────
Each Excel row carries (brand, product_name, qty). Local DB Items use
varied naming conventions ("Serenida MHC 50ml" or parent+variation pairs).
We score candidate matches with difflib SequenceMatcher on normalized
strings (uppercase, ASCII-folded, brand+name+qty concatenated) and only
auto-update barcodes for matches scoring ≥ 0.78 (calibrated to be tight
enough to avoid mismatches but loose enough to catch real variants).

INGREDIENT VERIFICATION
───────────────────────
Recipe ingredients store raw-material item references (e.g. "ARGAN YAĞI"),
not INCI names ("Argania Spinosa Kernel Oil"). So we DO NOT auto-modify
recipes. We dump the Excel ingredient list per matched product, so the
lab can reconcile manually or use it for future seeding scripts.
"""
import argparse
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd

# Ensure project root on path so we can import database/* — script is in scripts/
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import SessionLocal, Item            # noqa: E402

DEFAULT_EXCEL_DIR = Path(
    "/Users/dogukan/Downloads/AFRİKA FUAR-1/PRODUCT LIST/İNGİLİZCE"
)
SOURCES = {
    "SERENIDA":    "SERENIDA PRODUCT LIST_EN.xlsx",
    "MİNERVA 108": "MİNERVA PRODUCT LIST_EN.xlsx",
    "EVANIRA":     "EVANIRA PRODUCT LIST_EN.xlsx",
}

CONFIDENCE_THRESHOLD = 0.78        # below this we report but don't write


# ─── Helpers ────────────────────────────────────────────────────────────────

def fold(s: str) -> str:
    """Lowercase + strip diacritics — gives 'minerva' for 'MİNERVA'."""
    if s is None:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(s))
    no_diacritics = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    # Also fold Turkish dotted i variants the NFKD pass leaves alone
    no_diacritics = no_diacritics.replace("İ", "I").replace("ı", "i")
    return re.sub(r"\s+", " ", no_diacritics.lower()).strip()


def parse_qty(raw) -> str:
    """Normalize "50 ML " → "50ml".  Drops spaces, keeps unit."""
    s = re.sub(r"\s+", "", str(raw or "").strip().lower())
    return s


def split_ingredients(raw) -> list[str]:
    """Split INCI string into individual entries — comma OR semicolon."""
    if not raw or pd.isna(raw):
        return []
    parts = re.split(r"[,;]", str(raw))
    return [p.strip() for p in parts if p.strip()]


def normalize_for_match(brand: str, name: str, qty: str) -> str:
    """Build the canonical token blob we score similarity against."""
    blob = f"{brand} {name} {qty}".strip()
    blob = re.sub(r"[^a-z0-9 ]+", " ", fold(blob))
    return re.sub(r"\s+", " ", blob).strip()


def score(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


# ─── Load Excels ────────────────────────────────────────────────────────────

def load_excel_rows(excel_dir: Path) -> list[dict]:
    """Returns list of dicts: {barcode, brand, name, qty, ingredients[]}."""
    rows: list[dict] = []
    for brand, fname in SOURCES.items():
        path = excel_dir / fname
        if not path.exists():
            print(f"  ⚠ {brand}: file not found at {path}", file=sys.stderr)
            continue
        df = pd.read_excel(path, header=0)
        df.columns = [str(c).strip() for c in df.columns]
        bc_col, _, name_col, qty_col, ing_col = df.columns[:5]
        # Keep only numeric barcode rows (skip header artifacts, blanks)
        df = df[df[bc_col].astype(str).str.match(r"^\d{8,}$", na=False)]

        for _, r in df.iterrows():
            rows.append({
                "barcode": str(r[bc_col]).strip(),
                "brand":   brand,
                "name":    str(r[name_col]).strip(),
                "qty":     parse_qty(r[qty_col]),
                "qty_raw": str(r[qty_col]).strip(),
                "ingredients": split_ingredients(r[ing_col]),
            })
    return rows


# ─── Match against DB ───────────────────────────────────────────────────────

def find_best_match(row: dict, db_items: list[Item]) -> tuple[Item | None, float]:
    target = normalize_for_match(row["brand"], row["name"], row["qty"])
    best, best_s = None, 0.0
    for item in db_items:
        # Try item.name alone, and parent_name + variation if it's a variation
        candidates = [item.name or ""]
        if item.parent_id:
            parent = next((p for p in db_items if p.id == item.parent_id), None)
            if parent:
                candidates.append(f"{parent.name} {item.variation_name or ''}")
                candidates.append(f"{parent.name}")
        for cand in candidates:
            cand_norm = re.sub(r"[^a-z0-9 ]+", " ", fold(cand))
            cand_norm = re.sub(r"\s+", " ", cand_norm).strip()
            s = score(target, cand_norm)
            if s > best_s:
                best_s, best = s, item
    return best, best_s


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true",
                    help="Apply barcode updates to DB (default: dry-run)")
    ap.add_argument("--create-missing", action="store_true",
                    help="Also CREATE Item rows for Excel products with no DB match. "
                         "Creates brand-named parents and per-qty variations. "
                         "Implies --commit; only sensible with a clean target DB.")
    ap.add_argument("--excel-dir", type=Path, default=DEFAULT_EXCEL_DIR,
                    help="Directory containing the 3 brand Excels")
    ap.add_argument("--ingredients-tsv", type=Path,
                    help="Optional: write per-product ingredient TSV here")
    ap.add_argument("--threshold", type=float, default=CONFIDENCE_THRESHOLD,
                    help=f"Match confidence cut-off (default {CONFIDENCE_THRESHOLD})")
    args = ap.parse_args()
    if args.create_missing:
        args.commit = True

    rows = load_excel_rows(args.excel_dir)
    print(f"Loaded {len(rows)} product rows from Excels.")

    db = SessionLocal()
    try:
        db_items = (
            db.query(Item)
            .filter(Item.is_active == True)
            .all()
        )
        print(f"DB has {len(db_items)} active items in scope.")

        # ── Match each Excel row to a DB item ───────────────────────────────
        matched, low, unmatched = 0, 0, 0
        updates_applied = 0
        creations_applied = 0
        ingredient_rows = []
        # Cache of {brand_uppercased: parent Item} created during this run
        brand_parents: dict[str, Item] = {}

        print()
        hdr = f"{'BARCODE':<14}  {'BRAND':<12}  {'NAME':<40}  {'QTY':<7}  {'CONF':>5}  {'DB':<5}  ACTION"
        print(hdr)
        print("-" * len(hdr))

        for row in rows:
            best, s = find_best_match(row, db_items)
            db_id_str = f"#{best.id}" if best else "—"
            if best and s >= args.threshold:
                action = "OK (match)"
                matched += 1
                # Upsert barcode if changed
                if (best.barcode or "") != row["barcode"]:
                    if args.commit:
                        best.barcode = row["barcode"]
                        updates_applied += 1
                        action = "✓ UPDATED"
                    else:
                        action = "would update"
                ingredient_rows.append({
                    "barcode":     row["barcode"],
                    "brand":       row["brand"],
                    "excel_name":  row["name"],
                    "qty":         row["qty_raw"],
                    "db_match":    f"{db_id_str} {best.name}",
                    "ingredients": " | ".join(row["ingredients"]),
                })
            elif best and s >= 0.55:
                action = f"low-conf → {best.name[:40]}"
                low += 1
            else:
                action = "no match"
                unmatched += 1
                # Optional: create a fresh Item from this Excel row
                if args.create_missing:
                    # One brand-level parent per brand (e.g. "Serenida"),
                    # then this row becomes a variation under it.
                    brand_key = row["brand"].split()[0].title()  # MİNERVA 108 → Minerva
                    parent = brand_parents.get(brand_key)
                    if not parent:
                        parent = (
                            db.query(Item)
                            .filter(Item.name == f"{brand_key} — {row['name']}",
                                    Item.parent_id.is_(None))
                            .first()
                        )
                    # Create per-product parent (brand prefix + product name) so
                    # variations of the same product (e.g. 100ml + 500ml of
                    # Vitamin C Cream) sit under the same parent.
                    product_parent_name = f"{brand_key} — {row['name'].strip()}"
                    parent = (
                        db.query(Item)
                        .filter(Item.name == product_parent_name,
                                Item.parent_id.is_(None))
                        .first()
                    )
                    if not parent:
                        parent = Item(
                            name=product_parent_name,
                            category="Bitmiş Ürün",
                            unit="adet",
                            is_active=True,
                        )
                        db.add(parent)
                        db.flush()
                        creations_applied += 1
                    # Create the variation row
                    variation = Item(
                        name=row["name"].strip(),
                        category="Bitmiş Ürün",
                        unit=row["qty"].replace("ml", "ml").replace("g", "g") or "adet",
                        parent_id=parent.id,
                        variation_name=row["qty_raw"],
                        barcode=row["barcode"],
                        is_active=True,
                    )
                    db.add(variation)
                    db.flush()
                    creations_applied += 1
                    action = f"+ CREATED #{variation.id} (parent #{parent.id})"
                    ingredient_rows.append({
                        "barcode":     row["barcode"],
                        "brand":       row["brand"],
                        "excel_name":  row["name"],
                        "qty":         row["qty_raw"],
                        "db_match":    f"#{variation.id} {variation.name} (NEW)",
                        "ingredients": " | ".join(row["ingredients"]),
                    })

            print(f"{row['barcode']:<14}  {row['brand']:<12}  "
                  f"{row['name'][:40]:<40}  {row['qty']:<7}  "
                  f"{s:>5.2f}  {db_id_str:<5}  {action}")

        # FIX: commit if --commit was passed, regardless of which counter moved.
        # Earlier `args.commit and updates_applied` rolled back when
        # --create-missing produced 0 updates but many creations.
        if args.commit:
            db.commit()
        else:
            db.rollback()

        # ── Summary ──
        print()
        print(f"Summary:")
        print(f"  ✓ confident matches:  {matched}/{len(rows)}")
        print(f"  ~ low-confidence:      {low}")
        print(f"  ✗ unmatched:           {unmatched}")
        if args.commit:
            print(f"  ⚡ DB updates applied:  {updates_applied}")
            if args.create_missing:
                print(f"  + items created:       {creations_applied}")
        else:
            print(f"  (dry-run — re-run with --commit to apply)")

        # ── Ingredient export ──
        if args.ingredients_tsv and ingredient_rows:
            with open(args.ingredients_tsv, "w", encoding="utf-8") as f:
                cols = ["barcode", "brand", "excel_name", "qty", "db_match", "ingredients"]
                f.write("\t".join(cols) + "\n")
                for r in ingredient_rows:
                    f.write("\t".join(r[c].replace("\t", " ") for c in cols) + "\n")
            print(f"  📄 ingredient TSV: {args.ingredients_tsv}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
