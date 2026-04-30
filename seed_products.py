#!/usr/bin/env python3
"""
Minerva108 — Bitmiş Ürün catalog seeder.
Reads the 49-product catalogue extracted from EVANIRA/SERENIDA/MINERVA108 PDFs
and inserts them into the database as Bitmiş Ürün items.

Idempotent — if a product already exists (by name match), it's skipped.

Logic:
  - Single-size product → standalone Bitmiş Ürün (no parent)
  - Multi-size product → parent (abstract) + N children (variations)

Usage on server:
    cd /var/www/minerva
    set -a; source .env; set +a
    venv/bin/python seed_products.py
"""

import os, sys

# Ensure we're using whatever DATABASE_URL is set (PG on prod, SQLite on dev)
from database import SessionLocal, Item

# ── Catalogue: extracted from the 3 product PDFs + augmented from proforma ──
# Format: (full_name, [list of size strings in ml/g])
# Brand prefix included for proforma consistency.
CATALOG = [
    # ── EVANIRA ────────────────────────────────────────────────────────────
    ("Evanira Bikini Area Skin Tone Balancing Lotion",  ["200ml", "500ml"]),
    ("Evanira Fresh Shower Gel",                        ["200ml", "500ml"]),
    ("Evanira Niacinamide Radiance Body Lotion",        ["200ml", "500ml"]),
    ("Evanira Purifying Anti-Acne Lotion",              ["200ml", "500ml"]),
    ("Evanira Red Clover Anti-Dark Spot Cream",         ["100ml", "500ml"]),
    ("Evanira Vitamin C Radiance Cream",                ["100ml", "500ml"]),

    # ── SERENIDA ───────────────────────────────────────────────────────────
    ("Serenida Aloe Vera Refreshing Hand and Foot Lotion", ["200ml"]),
    ("Serenida Anti-Acne Face and Body Lotion",            ["100ml"]),
    ("Serenida Anti-Aging Face Cream",                     ["50ml"]),
    ("Serenida Anti-Dark Spot Cream",                      ["50ml"]),
    ("Serenida Bentonite Anti-Aging Clay Face Mask",       ["50ml"]),
    ("Serenida Bikini Area Skin Tone Balancing Cream",     ["200ml"]),
    ("Serenida Charcoal Whitening Toothpaste",             ["100ml"]),
    ("Serenida Facial Toner",                              ["100ml"]),
    ("Serenida Fresh Shower Gel",                          ["200ml"]),
    ("Serenida Gentle and Nourishing Eye Makeup Remover",  ["100ml"]),
    ("Serenida Illuminating Facial Cleansing Gel",         ["100ml"]),
    ("Serenida Moisture Boost Hyaluronic Acid Serum",      ["30ml"]),
    ("Serenida Moisturizing Hand Cream",                   ["50ml"]),
    ("Serenida Niacinamide Radiance Lotion",               ["200ml"]),
    ("Serenida Nourishing Body Lotion",                    ["200ml"]),
    ("Serenida Nourishing Hair Conditioner",               ["100ml"]),
    ("Serenida Nourishing Night Cream",                    ["50ml"]),
    ("Serenida Nourishing Shampoo for Dry Hair",           ["200ml"]),
    ("Serenida Purifying Shampoo for Oily Hair",           ["200ml"]),
    ("Serenida Stick Deodorant",                           ["15ml"]),
    ("Serenida Whitening Toothpaste",                      ["100ml"]),

    # ── MINERVA 108 ────────────────────────────────────────────────────────
    ("Minerva 108 After Sun Gel Cream",                              ["200ml"]),
    ("Minerva 108 Anti-Blemish Sun Protection Lotion SPF 50+",       ["200ml"]),
    ("Minerva 108 Body Scrub",                                       ["250ml"]),
    ("Minerva 108 Bor+Geven Food Supplement",                        ["100ml"]),
    ("Minerva 108 Face Scrub",                                       ["60ml"]),
    ("Minerva 108 Facial Cleansing Gel",                             ["200ml"]),
    ("Minerva 108 Intensive Foot Care Cream",                        ["50ml"]),
    ("Minerva 108 Multi-Functional Beauty Oil — Jasmine",            ["100ml"]),
    ("Minerva 108 Multi-Functional Beauty Oil — Lavender",           ["100ml"]),
    ("Minerva 108 Red Clover Anti-Aging & Pore Minimizing Toner",    ["200ml"]),
    ("Minerva 108 Red Clover Anti-Dark Spot Cream",                  ["50ml"]),
    ("Minerva 108 Red Clover Clay Mask",                             ["50ml"]),
    ("Minerva 108 Red Clover Day Cream",                             ["50ml"]),
    ("Minerva 108 Red Clover Eye Contour Cream",                     ["10ml"]),
    ("Minerva 108 Red Clover Flower",                                ["11g"]),
    ("Minerva 108 Red Clover Food Supplement",                       ["100ml"]),
    ("Minerva 108 Red Clover Lip Balm",                              ["10ml"]),
    ("Minerva 108 Red Clover Night Cream",                           ["50ml"]),
    ("Minerva 108 Red Clover Nourishing Eye Makeup Remover",         ["50ml"]),
    ("Minerva 108 Shower Gel",                                       ["400ml"]),
    ("Minerva 108 Sunscreen SPF 30+",                                ["10ml"]),
    ("Minerva 108 Water-Based Hair Mask",                            ["100ml", "200ml"]),
]


def norm(s: str) -> str:
    """Compare strings case-insensitive ignoring whitespace."""
    return "".join(s.upper().split())


def main():
    db = SessionLocal()
    try:
        # Build a name → existing item map (case-insensitive)
        existing = {norm(i.name): i for i in db.query(Item).all()}

        created_parents     = 0
        created_variations  = 0
        created_standalones = 0
        skipped             = 0

        for full_name, sizes in CATALOG:
            # Only one size? → STANDALONE
            if len(sizes) == 1:
                # Append size to name for clarity in lists/dropdowns
                final_name = f"{full_name} ({sizes[0]})"
                if norm(final_name) in existing:
                    skipped += 1
                    continue
                item = Item(
                    name=final_name,
                    category="Bitmiş Ürün",
                    unit="adet",
                    current_stock=0.0,
                    cost_price=0.0,
                    is_active=True,
                )
                db.add(item)
                db.flush()
                existing[norm(final_name)] = item
                created_standalones += 1
                continue

            # Multi-size → PARENT + CHILDREN
            parent_name = full_name
            if norm(parent_name) in existing:
                parent = existing[norm(parent_name)]
                skipped += 1
            else:
                parent = Item(
                    name=parent_name,
                    category="Bitmiş Ürün",
                    unit="adet",
                    current_stock=0.0,   # parent is abstract — no real stock
                    cost_price=0.0,
                    is_active=True,
                )
                db.add(parent)
                db.flush()
                existing[norm(parent_name)] = parent
                created_parents += 1

            for size in sizes:
                child_name = f"{full_name} {size}"
                if norm(child_name) in existing:
                    skipped += 1
                    continue
                child = Item(
                    name=child_name,
                    category="Bitmiş Ürün",
                    unit="adet",
                    current_stock=0.0,
                    cost_price=0.0,
                    parent_id=parent.id,
                    variation_name=size,
                    is_active=True,
                )
                db.add(child)
                db.flush()
                existing[norm(child_name)] = child
                created_variations += 1

        db.commit()
        total = created_parents + created_variations + created_standalones
        print(f"\n🎉 Seed tamamlandı.")
        print(f"   Yeni ana ürün:     {created_parents}")
        print(f"   Yeni variation:    {created_variations}")
        print(f"   Yeni standalone:   {created_standalones}")
        print(f"   Atlanan (mevcut):  {skipped}")
        print(f"   Toplam yeni satır: {total}")
        print(f"\n📋 Kontrol için /items sayfasında 'Bitmiş Ürün' tab'ına bak.")
        return 0

    except Exception as e:
        db.rollback()
        print(f"❌ Hata: {e}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
