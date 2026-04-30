#!/usr/bin/env python3
"""
Minerva108 — Cost data seeder for Bitmiş Ürün catalog.

Populates ingredient_cost + packaging_cost (and derived cost_price) for items
matched against the catalog using the AFRİKA FUAR-1/MAALİYET Excel files.

Coverage at this stage:
  - 6 Evanira 500ml: real data from cost files (ingredient only, no packaging)
  - 6 Evanira 200ml: scaled linearly from 500ml (× 0.4)
  - 2 Evanira 100ml: scaled linearly from 500ml (× 0.2)
  - 21 Serenida: real data from cost files (ingredient + packaging)
  - 22 Minerva 108: NOT YET — pending cost files

Idempotent — running twice doesn't double-charge anything.

Usage on server:
    cd /var/www/minerva
    set -a; source .env; set +a
    venv/bin/python seed_costs.py
"""
import sys
from database import SessionLocal, Item


# ── Cost data: (catalog_name, ingredient_TL, packaging_TL) ─────────────────
# All values in TRY, per single bottle/jar.
# Catalog names match exactly what seed_products.py inserted.

EVANIRA_500ML = [
    # name, ingredient_tl, packaging_tl
    ("Evanira Bikini Area Skin Tone Balancing Lotion 500ml",  29.60, 0.0),
    ("Evanira Fresh Shower Gel 500ml",                        21.99, 0.0),
    ("Evanira Niacinamide Radiance Body Lotion 500ml",        29.97, 0.0),
    ("Evanira Purifying Anti-Acne Lotion 500ml",              27.45, 0.0),
    ("Evanira Red Clover Anti-Dark Spot Cream 500ml",         42.26, 0.0),
    ("Evanira Vitamin C Radiance Cream 500ml",                40.93, 0.0),
]

# 200ml = 500ml × (200/500) = × 0.4 — same formula, only volume changes
EVANIRA_200ML = [
    ("Evanira Bikini Area Skin Tone Balancing Lotion 200ml",  29.60 * 0.4, 0.0),
    ("Evanira Fresh Shower Gel 200ml",                        21.99 * 0.4, 0.0),
    ("Evanira Niacinamide Radiance Body Lotion 200ml",        29.97 * 0.4, 0.0),
    ("Evanira Purifying Anti-Acne Lotion 200ml",              27.45 * 0.4, 0.0),
]

# 100ml = 500ml × 0.2 — only Red Clover Anti-Dark and Vitamin C have 100ml SKU
EVANIRA_100ML = [
    ("Evanira Red Clover Anti-Dark Spot Cream 100ml",  42.26 * 0.2, 0.0),
    ("Evanira Vitamin C Radiance Cream 100ml",         40.93 * 0.2, 0.0),
]

SERENIDA = [
    ("Serenida Anti-Aging Face Cream (50ml)",                       13.15, 10.00),
    ("Serenida Anti-Dark Spot Cream (50ml)",                        29.99, 0.0),    # pkg missing in source
    ("Serenida Bentonite Anti-Aging Clay Face Mask (50ml)",         36.54, 10.00),
    ("Serenida Bikini Area Skin Tone Balancing Cream (200ml)",      123.50, 12.90),
    ("Serenida Charcoal Whitening Toothpaste (100ml)",              45.26, 0.0),
    ("Serenida Facial Toner (100ml)",                               46.88, 12.90),
    ("Serenida Fresh Shower Gel (200ml)",                           43.22, 12.90),
    ("Serenida Gentle and Nourishing Eye Makeup Remover (100ml)",   34.18, 12.90),
    ("Serenida Illuminating Facial Cleansing Gel (100ml)",          69.91, 12.90),
    ("Serenida Moisture Boost Hyaluronic Acid Serum (30ml)",        11.67, 0.0),    # pkg missing
    ("Serenida Moisturizing Hand Cream (50ml)",                     16.76, 0.0),    # pkg missing
    ("Serenida Niacinamide Radiance Lotion (200ml)",                126.06, 0.0),   # HASSAS CİLTLER RENKACICI
    ("Serenida Nourishing Body Lotion (200ml)",                     76.77, 12.90),
    ("Serenida Nourishing Hair Conditioner (100ml)",                79.81, 0.0),    # pkg missing
    ("Serenida Nourishing Night Cream (50ml)",                      21.79, 0.0),    # pkg missing
    ("Serenida Nourishing Shampoo for Dry Hair (200ml)",            73.09, 12.90),
    ("Serenida Purifying Shampoo for Oily Hair (200ml)",            108.95, 12.90),
    ("Serenida Stick Deodorant (15ml)",                             19.81, 0.0),    # pkg missing
    ("Serenida Whitening Toothpaste (100ml)",                       43.98, 0.0),    # pkg missing
    # Files map to these — if catalog name differs, update here
    ("Serenida Anti-Acne Face and Body Lotion (100ml)",             27.78, 12.90),  # was "SİVİLCE SERUM"
]

ALL_COSTS = EVANIRA_500ML + EVANIRA_200ML + EVANIRA_100ML + SERENIDA


def main():
    db = SessionLocal()
    try:
        updated = 0
        not_found = []

        for entry in ALL_COSTS:
            name, ing_tl, pkg_tl = entry
            ing_tl = round(float(ing_tl), 4)
            pkg_tl = round(float(pkg_tl), 4)
            total  = round(ing_tl + pkg_tl, 4)

            item = db.query(Item).filter(Item.name == name).first()
            if not item:
                not_found.append(name)
                continue

            item.ingredient_cost = ing_tl
            item.packaging_cost  = pkg_tl
            item.cost_price      = total
            updated += 1

        db.commit()
        print(f"\n🎉 Maliyet güncellemesi tamamlandı.")
        print(f"   Güncellenen ürün:  {updated}")
        print(f"   Bulunamayan:       {len(not_found)}")
        if not_found:
            print(f"\n⚠️  Kataloğunda eşleşmeyen kayıtlar (isim eksiği veya yazım farkı olabilir):")
            for n in not_found:
                print(f"      • {n}")
        return 0

    except Exception as e:
        db.rollback()
        print(f"❌ Hata: {e}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
