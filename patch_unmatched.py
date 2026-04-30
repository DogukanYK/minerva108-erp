#!/usr/bin/env python3
"""
Minerva108 — Patch unmatched recipe ingredients.

Augments the 31 existing recipes by adding the 14 ingredient rows that didn't
match in the initial seed. Strategy:
  1. Create 2 missing hammadde (MEYAN KÖKÜ EKSTRAKTI, METİL SELÜLOZ)
  2. For each cost-Excel BOM, locate the unmatched ingredient names
  3. Resolve them via PATCH_MAP to existing DB hammadde
  4. Append RecipeIngredient rows to the affected recipes
  5. Idempotent: skips ingredients already on the recipe

Usage on server:
    cd /var/www/minerva
    set -a; source .env; set +a
    venv/bin/python patch_unmatched.py
"""

import os, sys, json, re, unicodedata
from database import SessionLocal, Item, Supplier, Recipe, RecipeIngredient


# ── New Hammadde to create ───────────────────────────────────────────────
NEW_HAMMADDE = [
    ("MEYAN KÖKÜ EKSTRAKTI", "g",  None),
    ("METİL SELÜLOZ",         "g", "TİTO"),
]


# ── Map unmatched cost-Excel names → DB hammadde names ──────────────────
PATCH_MAP = {
    "MEYAN KÖKÜ ESKTRATI":          "MEYAN KÖKÜ EKSTRAKTI",
    "MEYAN KÖKÜ EKSTRAKTI":         "MEYAN KÖKÜ EKSTRAKTI",
    "CARNAUBA MUMU":                "CARNAUBA WAX",
    "METIL SELÜLOZ":                "METİL SELÜLOZ",
    "METİL SELÜLOZ":                "METİL SELÜLOZ",
    "metil  selüloz":               "METİL SELÜLOZ",
    "kaysı çekirdegi butter":       "KAYISI ÇEKİRDEGİ YAGI",
    "KAYSI ÇEKİRDEGİ BUTTER":       "KAYISI ÇEKİRDEGİ YAGI",
    "K-705 (SHORMİXE )":            "K-705 (SHAROMIX KORUYUCU)",
    "KORUYUCU ( PANTYLENE GLİKOL )":"PENTİLEN GLİKOL",
    "GLYCERYL STEARAT":             "GMS (GLİSERİL MONO STEARAT)",
    # 'SİRKE8PH ph için kullanmaya biliriz. )' skipped intentionally —
    # the Excel comment itself says "could use" (suggestive, not required)
}


# ── Cost-file → catalog product list (same as seed_recipes.py) ───────────
PRODUCT_MAP = {
    "EVANIRA BİKİNİ BÖLGESİ RENK AÇIÇI KREM FUAR": [
        ("Evanira Bikini Area Skin Tone Balancing Lotion 500ml", 500),
        ("Evanira Bikini Area Skin Tone Balancing Lotion 200ml", 200),
    ],
    "EVANIRA C VİTAMİN YÜZ KREM FUAR": [
        ("Evanira Vitamin C Radiance Cream 500ml", 500),
        ("Evanira Vitamin C Radiance Cream 100ml", 100),
    ],
    "EVANIRA CİLT RENK ACICI LOSYON FUAR MALİYET": [
        ("Evanira Niacinamide Radiance Body Lotion 500ml", 500),
        ("Evanira Niacinamide Radiance Body Lotion 200ml", 200),
    ],
    "EVANIRA DARK SPOT CREAM FUAR MALİYET": [
        ("Evanira Red Clover Anti-Dark Spot Cream 500ml", 500),
        ("Evanira Red Clover Anti-Dark Spot Cream 100ml", 100),
    ],
    "EVANIRA SHOWER GEL FUAR MALİYET": [
        ("Evanira Fresh Shower Gel 500ml", 500),
        ("Evanira Fresh Shower Gel 200ml", 200),
    ],
    "EVANIRA SIVILCE LOSYON FUAR MALİYET": [
        ("Evanira Purifying Anti-Acne Lotion 500ml", 500),
        ("Evanira Purifying Anti-Acne Lotion 200ml", 200),
    ],
    "SERENİDA BİKİNİ BÖLGESİ EURO": [("Serenida Bikini Area Skin Tone Balancing Cream (200ml)", 200)],
    "SERENİDA DARK SPOT CREAM EURO": [("Serenida Anti-Dark Spot Cream (50ml)", 50)],
    "SERENİDA DEO STICK EURO": [("Serenida Stick Deodorant (15ml)", 15)],
    "SERENİDA DİŞ MACUNU SADE EURO": [("Serenida Whitening Toothpaste (100ml)", 100)],
    "SERENİDA EL KREMİ EURO": [("Serenida Moisturizing Hand Cream (50ml)", 50)],
    "SERENİDA GÖZ MAKYAJ TEMİZLEYİCİ EURO": [("Serenida Gentle and Nourishing Eye Makeup Remover (100ml)", 100)],
    "SERENİDA HASSAS CİLTLER RENKACICI EURO": [("Serenida Niacinamide Radiance Lotion (200ml)", 200)],
    "SERENİDA HYALURONİK ASİT EURO": [("Serenida Moisture Boost Hyaluronic Acid Serum (30ml)", 30)],
    "SERENİDA KİL MASKE EURO": [("Serenida Bentonite Anti-Aging Clay Face Mask (50ml)", 50)],
    "SERENİDA KÖMÜR DİŞ MACUNU EURO": [("Serenida Charcoal Whitening Toothpaste (100ml)", 100)],
    "SERENİDA KURU SAÇ ŞAMPUAN EURO": [("Serenida Nourishing Shampoo for Dry Hair (200ml)", 200)],
    "SERENİDA SAÇ KREMİ EURO": [("Serenida Nourishing Hair Conditioner (100ml)", 100)],
    "SERENİDA SİVİLCE SERUM EURO": [("Serenida Anti-Acne Face and Body Lotion (100ml)", 100)],
    "SERENİDA TONİK EURO": [("Serenida Facial Toner (100ml)", 100)],
    "SERENİDA VÜCUT LOSYONU MALİYET EURO": [("Serenida Nourishing Body Lotion (200ml)", 200)],
    "SERENİDA YAĞLI SAÇ ŞAMPUAN EURO": [("Serenida Purifying Shampoo for Oily Hair (200ml)", 200)],
    "SERENİDA YÜZ KREMİ EURO": [("Serenida Anti-Aging Face Cream (50ml)", 50)],
    "SERENİDA YÜZ TEMİZLEME JELİ MALİYET EURO": [("Serenida Illuminating Facial Cleansing Gel (100ml)", 100)],
    "SERENİDE DUŞ JELİ MALİYET EURO": [("Serenida Fresh Shower Gel (200ml)", 200)],
}


def _nkey(s):
    if s is None: return ''
    s = unicodedata.normalize('NFKC', str(s))
    s = re.sub(r'\s+', ' ', s).strip().upper()
    return s


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    boms_path = os.path.join(here, "minerva_boms.json")
    if not os.path.exists(boms_path):
        print(f"❌ {boms_path} bulunamadı")
        return 1
    with open(boms_path) as f:
        boms = json.load(f)

    db = SessionLocal()
    try:
        # ── Phase 1: create the 2 missing hammadde ────────────────────────
        print(f"\n→ Phase 1: Eksik hammadde'leri ekle")
        added = 0
        skipped = 0
        for name, unit, supplier_name in NEW_HAMMADDE:
            existing = db.query(Item).filter(Item.name == name).first()
            if existing:
                skipped += 1
                continue
            supplier_id = None
            if supplier_name:
                supp = db.query(Supplier).filter(Supplier.name.ilike(f"%{supplier_name}%")).first()
                if supp: supplier_id = supp.id
            db.add(Item(
                name=name, category="Hammadde", unit=unit,
                current_stock=0.0, min_stock_level=0.0, cost_price=0.0,
                supplier_id=supplier_id, is_active=True,
            ))
            added += 1
        db.commit()
        print(f"   ✅ Yeni: {added}, atlandı (mevcut): {skipped}")

        # ── Phase 2: Index DB hammadde + recipes ──────────────────────────
        all_hammadde = db.query(Item).filter(
            Item.category == "Hammadde", Item.is_active == True
        ).all()
        hammadde_by_nkey = {_nkey(i.name): i for i in all_hammadde}

        norm_product_map = {_nkey(k): v for k, v in PRODUCT_MAP.items()}
        norm_patch_map   = {_nkey(k): v for k, v in PATCH_MAP.items()}

        # ── Phase 3: walk BOMs, patch the affected recipes ────────────────
        print(f"\n→ Phase 2: Eksik ingredient'ları reçetelere ekle")
        patched = 0
        already_present = 0
        still_missing = []

        for bom in boms:
            cost_file = bom["file"]
            if "(3)" in cost_file: continue

            target_products = norm_product_map.get(_nkey(cost_file))
            if not target_products: continue

            # Find ingredients in this BOM that need patching
            for ing in bom["ingredients"]:
                ing_name = ing["name"]
                target_db_name = norm_patch_map.get(_nkey(ing_name))
                if not target_db_name:
                    continue   # not in patch list
                target_item = hammadde_by_nkey.get(_nkey(target_db_name))
                if not target_item:
                    still_missing.append((ing_name, target_db_name))
                    continue

                # For each target product (variation), check if its recipe already has this ingredient
                for product_name, product_size in target_products:
                    recipe_target = db.query(Item).filter(Item.name == product_name).first()
                    if not recipe_target: continue
                    recipe = db.query(Recipe).filter(
                        Recipe.target_item_id == recipe_target.id, Recipe.is_active == True
                    ).first()
                    if not recipe: continue

                    # Already in this recipe?
                    existing_link = db.query(RecipeIngredient).filter(
                        RecipeIngredient.recipe_id == recipe.id,
                        RecipeIngredient.item_id == target_item.id,
                    ).first()
                    if existing_link:
                        already_present += 1
                        continue

                    # Compute scaled quantity (% × size / 100)
                    qty = ing["percent"] * product_size / 100.0
                    db.add(RecipeIngredient(
                        recipe_id=recipe.id,
                        item_id=target_item.id,
                        quantity=round(qty, 4),
                        unit=target_item.unit or "ml",
                    ))
                    patched += 1
                    print(f"   ✅ {product_name:<55s}  + {ing_name} ({qty:.2f} {target_item.unit})")

        db.commit()

        print(f"\n{'='*70}")
        print(f"🎉 Patch tamamlandı")
        print(f"   Yeni ingredient bağlandı:    {patched}")
        print(f"   Zaten ekliydi (idempotent):  {already_present}")
        if still_missing:
            print(f"   ⚠️  Hala bulunamayan: {len(still_missing)}")
            for n, t in still_missing[:5]:
                print(f"      • {n}  →  {t}")
        return 0

    except Exception as e:
        db.rollback()
        print(f"❌ Hata: {e}")
        import traceback; traceback.print_exc()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
