#!/usr/bin/env python3
"""
Minerva108 — Bulk recipe seeder.

Reads minerva_boms.json (26 BOMs extracted from cost Excels) and:
  1. Auto-creates missing common cosmetic ingredients (DİSTİLE SU, K-705, etc.)
  2. Fuzzy-matches each cost-Excel ingredient name to a DB Hammadde item
  3. Creates Recipe + RecipeIngredient rows for each catalog product
  4. For Evanira products: creates one recipe per variation (200ml, 500ml, etc.)
     with quantities scaled proportionally from the 500ml source recipe

Idempotent — re-running skips items/recipes that already exist.

Usage on server:
    cd /var/www/minerva
    set -a; source .env; set +a
    venv/bin/python seed_recipes.py
"""
import os, sys, json, re
from difflib import SequenceMatcher
from database import SessionLocal, Item, Supplier, Recipe, RecipeIngredient


# ── Essential cosmetic ingredients NOT in the original Hammadde import ──────
# (name, unit, default_supplier_name)
MISSING_HAMMADDE = [
    ("DİSTİLE SU",                 "ml", None),
    ("K-705 (SHAROMIX KORUYUCU)",  "g",  "SABUNARIA"),
    ("D-PANTHENOL",                "g",  "TATLIDİLİMLER"),
    ("STEARYL ALCOHOL",            "g",  "TATLIDİLİMLER"),
    ("CETEARYL ALCOHOL",           "g",  "TATLIDİLİMLER"),
    ("COCO BETAİN",                "g",  "TATLIDİLİMLER"),
    ("LAURYL GLUCOSIDE",           "g",  "BUTİKHAMMADDE"),
    ("COCO GLUCOSIDE",             "g",  "TATLIDİLİMLER"),
    ("KOJİK ASİT",                 "g",  "SABUNARIA"),
    ("LACTIC ASİT",                "g",  "HAMMADDE SEPETİ"),
    ("HYDRATED SİLİCA",            "g",  None),
    ("SODYUM BİKARBONAT",          "g",  "HAMMADDE SEPETİ"),
    ("BETAİN",                     "g",  "KIMYACINIZ"),
    ("CİTRONELLA UÇUCU YAĞI",      "ml", "DOALINN"),
    ("LEMONGRASS UÇUCU YAĞI",      "ml", "DOALINN"),
    ("E VİTAMİNİ",                 "ml", "TATLIDİLİMLER"),
    ("UNDECANE / TRIDECANE",       "ml", "BUTİKHAMMADDE"),
    ("HİDROLİZE İPEK PROTEİN",     "g",  "TATLIDİLİMLER"),
    ("PAPATYA HİDROSOLÜ",          "ml", "ULUDAĞ HERBAL"),
    ("PAPATYA UÇUCU YAĞI",         "ml", "DOALINN"),
    ("BİBERİYE UÇUCU YAĞI",        "ml", "DOALINN"),
    ("BERGAMOT UÇUCU YAĞI",        "ml", "DOALINN"),
    ("LAVANTA UÇUCU YAĞI",         "ml", "DOALINN"),
    ("PORTAKAL UÇUCU YAĞI (TATLI)","ml", "DOALINN"),
    ("MİSK ADAÇAYI UÇUCU YAĞI",    "ml", "DOALINN"),
    ("PALMAROSA UÇUCU YAĞI",       "ml", "DOALINN"),
    ("SODYUM LAKTAT",              "g",  None),
    ("CARNAUBA WAX",               "g",  "TATLIDİLİMLER"),
    ("EMULGADE SE-PF",             "g",  "TATLIDİLİMLER"),
    ("GMS (GLİSERİL MONO STEARAT)","g",  "SABUNARIA"),
]


# ── Cost-Excel filename → catalog product(s) + base size ────────────────────
# Evanira files: cost is for 500ml; we generate recipe for each existing variation
# Serenida files: single size, recipe targets exactly one variation
# (catalog_name, base_size_ml = the size the cost Excel was calculated for)
PRODUCT_MAP = {
    # Evanira — base 500ml, scale to 200ml or 100ml as needed
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
    # Serenida — single size each
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
    # SERENİDA BİKİNİ BÖLGESİ EURO (3) is duplicate — skip
}


# ── Manual overrides for ingredient names that fuzzy-match wrong ────────────
# cost-name (uppercase) → DB Hammadde name
INGREDIENT_OVERRIDES = {
    "STEARYL ALCOHOL":    "STEARYL ALCOHOL",
    "STEARYL ALKOL":      "STEARYL ALCOHOL",
    "CETEARYL ALCHOOL":   "CETEARYL ALCOHOL",
    "CETEARYL ALCOHOL":   "CETEARYL ALCOHOL",
    "CETYL CETEARYL ALCOHOL": "CETEARYL ALCOHOL",
    "CETYL CTEARYL ALCOHOL":  "CETEARYL ALCOHOL",
    "CETYL STEARYL ALCHOL":   "CETEARYL ALCOHOL",
    "CETYL STEARYL ALCOHOL":  "CETEARYL ALCOHOL",
    "BETAİN":             "BETAİN",
    "COCO BETAİN":        "COCO BETAİNE%30",
    "FERRULİK ASİT":      "FERULİC ACİD",
    "DİSTİLE SU":         "DİSTİLE SU",
    "D-PANTHENOL":        "D-PANTHENOL",
    "D-PANTHENOL (B5 VİT)": "D-PANTHENOL",
    "D-PENTHENOL":        "D-PANTHENOL",
    "DPANTENOL":          "D-PANTHENOL",
    "PANTENOL":           "D-PANTHENOL",
    "PANTHENOL":          "D-PANTHENOL",
    "PANTHENOL (B5 -VİTAMİNİ )": "D-PANTHENOL",
    "PANTHENOL (B5 VİTAMİN)":    "D-PANTHENOL",
    "K-705":              "K-705 (SHAROMIX KORUYUCU)",
    "K-705 (LEXGARD)":    "K-705 (SHAROMIX KORUYUCU)",
    "K-705 (KORUYUCU)":   "K-705 (SHAROMIX KORUYUCU)",
    "K-705(KORUYUCU)":    "K-705 (SHAROMIX KORUYUCU)",
    "K705":               "K-705 (SHAROMIX KORUYUCU)",
    "K705 KORUYUCU":      "K-705 (SHAROMIX KORUYUCU)",
    "KORUYUCU 705":       "K-705 (SHAROMIX KORUYUCU)",
    "KORUYUCU ( K-705 )": "K-705 (SHAROMIX KORUYUCU)",
    "KSANTAN SAKIZI":     "XHANTAN GUM",
    "XANTHAN GUM":        "XHANTAN GUM",
    "XHANTAM  GAM":       "XHANTAN GUM",
    "COCO GLUCOSIDE":     "COCO GLUCOSIDE",
    "COCO GLUCOSİDE":     "COCO GLUCOSIDE",
    "LAURYL GLUCOSİDE":   "LAURYL GLUCOSIDE",
    "LAURI GLİCOSİDE":    "LAURYL GLUCOSIDE",
    "KOJIC ACIDE":        "KOJİK ASİT",
    "KOJIK ASIT":         "KOJİK ASİT",
    "KOJİK ASİT":         "KOJİK ASİT",
    "LACTİC ACİD":        "LACTIC ASİT",
    "LACTİC ASİT":        "LACTIC ASİT",
    "LAKTİK ASİT":        "LACTIC ASİT",
    "BADEM YAĞI":         "TATLI BADEM YAGI",
    "PORTAKAL U.Y":       "PORTAKAL UÇUCU YAĞI (TATLI)",
    "PORTAKAL UÇUCU YAĞI": "PORTAKAL UÇUCU YAĞI (TATLI)",
    "PORTOKAL U.Y.":      "PORTAKAL UÇUCU YAĞI (TATLI)",
    "ÇAY AGACI U.Y":      "ÇAY AĞACI YAĞI",
    "ÇAY AĞCI            U   Y": "ÇAY AĞACI YAĞI",
    "ÇAY AĞICI U.Y.":     "ÇAY AĞACI YAĞI",
    "ÇAY AĞACI YAĞI (SEYRELTEREK)": "ÇAY AĞACI YAĞI",
    "LAVANTA U.Y.":       "LAVANTA UÇUCU YAĞI",
    "LAVANTA UCUCU YAĞ":  "LAVANTA UÇUCU YAĞI",
    "MELEZ LAVANTA U.Y.": "LAVANTA UÇUCU YAĞI",
    "BERGAMOT        U  Y":  "BERGAMOT UÇUCU YAĞI",
    "BİBERİYE             U  Y": "BİBERİYE UÇUCU YAĞI",
    "MİSKADAÇAYI   U   Y":   "MİSK ADAÇAYI UÇUCU YAĞI",
    "MİSK ADAÇAYI UCUCU YAĞ": "MİSK ADAÇAYI UÇUCU YAĞI",
    "AKGÜNLÜK U  Y":      "AKGÜNLÜK UÇUCU YAĞI",
    "AKGÜNLÜK U.Y.":      "AKGÜNLÜK UÇUCU YAĞI",
    "GÜL AĞACI UY.":      "GÜL AGACI YAGI",
    "PAPATYA UCUCU YAĞI (SARI)": "PAPATYA UÇUCU YAĞI",
    "CİTRONELLA U.Y":      "CİTRONELLA UÇUCU YAĞI",
    "CİTRONELLA U.YAĞI":   "CİTRONELLA UÇUCU YAĞI",
    "CİTRONELLA UCUCU YAĞ":"CİTRONELLA UÇUCU YAĞI",
    "LEMONGRASS  (LİMON OTU YAĞI)": "LEMONGRASS UÇUCU YAĞI",
    "LEMON GRASS UÇUCU  YAĞI":      "LEMONGRASS UÇUCU YAĞI",
    "LEMONGRASS UÇUCU YAĞ":         "LEMONGRASS UÇUCU YAĞI",
    "PALMAROSA":          "PALMAROSA UÇUCU YAĞI",
    "E VİT.":             "E VİTAMİNİ",
    "E VİT ( TOCOPHERYL ASETATE )": "E VİTAMİNİ",
    "DİA-TOCOPHERY ACETATE": "E VİTAMİNİ",
    "ALOVERA EKSTRAKTI":  "ALEOVERA EKSTRAKTI",
    "ALEOVERA EKST.":     "ALEOVERA EKSTRAKTI",
    "ALLAN TOİN":         "ALLANTOİN",
    "ALLANTOIN":          "ALLANTOİN",
    "ALLONTOİN":          "ALLANTOİN",
    "ALPHA ARBUTIN":      "ALPHA ARBUTİN",
    "NIACINAMIDE":        "NİACİNAMİDE",
    "NİACİNAMIDE":        "NİACİNAMİDE",
    "NİACİNAMİDE":        "NİACİNAMİDE",
    "NİASİNAMİDE":        "NİACİNAMİDE",
    "GLİSERİL STEARAT":   "ELİTO GLİSERİL STEARAT    GMS",
    "GLİSERİL OLEAT":     "ELİTO GLİSERİL STEARAT    GMS",
    "GLİSERİL  OLEAT":    "ELİTO GLİSERİL STEARAT    GMS",
    "GLİSERİN OLEATE":    "ELİTO GLİSERİL STEARAT    GMS",
    "GMS":                "GMS (GLİSERİL MONO STEARAT)",
    "STEARİC ACİD":       "STERİK ASİT",
    "STEARİC ASİT":       "STERİK ASİT",
    "STEARİK ASİT":       "STERİK ASİT",
    "BUGDAY RUŞEYN YAĞI": "BUĞDAY RUŞEYM YAGI",
    "HYALURONİK ASİT (D.MOL)":  "HYALUORİK ASİT (DÜŞÜK MOL)",
    "HYOLURONİK ASİT (D. MOL)": "HYALUORİK ASİT (DÜŞÜK MOL)",
    "KAYISI ÇEKİRDEĞİ BUTTER":  "KAYISI ÇEKİRDEGİ YAGI",
    "KAYISI CEKİRDEĞİ BUTTER":  "KAYISI ÇEKİRDEGİ YAGI",
    "PEMBE KIL":          "PEMBE KAOLİN KİL",
    "PEMBE KİL":          "PEMBE KAOLİN KİL",
    "BENTONİT":           "BENTONİT KİL",
    "CAPRYLIC CAPRIC TRIGLİSERİDE":   "COPRLİC /CAPRİK TRİGLİSERİDE",
    "CAPRYLİC CAPRİC TRİGLYCERİDE":   "COPRLİC /CAPRİK TRİGLİSERİDE",
    "CAPRYLIC CARPİC TRİGLİSERİDE":   "COPRLİC /CAPRİK TRİGLİSERİDE",
    "CAPRILIY  CAPRIC / TRIGLISEIDE": "COPRLİC /CAPRİK TRİGLİSERİDE",
    "COCO CAP /TRİGLİSERİDE":         "COPRLİC /CAPRİK TRİGLİSERİDE",
    "COCO CAP./ TRİGLİSERİDE":        "COPRLİC /CAPRİK TRİGLİSERİDE",
    "COCO CAP./TRIGLİSERİDE":         "COPRLİC /CAPRİK TRİGLİSERİDE",
    "COCO CAPRYLATE":             "COCO CAPRYLATE  CAPRATE",
    "COCO CAPRYLATE / CAPRATE":   "COCO CAPRYLATE  CAPRATE",
    "COCO CAPRYLATE/CAPRATE":     "COCO CAPRYLATE  CAPRATE",
    "COCO CAPRYLATE/CAP.":        "COCO CAPRYLATE  CAPRATE",
    "COCO CAPRİLATE CAPRATE":     "COCO CAPRYLATE  CAPRATE",
    "COCO CAPRİLATE-CAPRATE":     "COCO CAPRYLATE  CAPRATE",
    "CAP. CAP (TR)":              "COCO CAPRYLATE  CAPRATE",
    "UNDECANE TRIDECANE":   "UNDECANE / TRIDECANE",
    "UNDECANE TRİDECANE":   "UNDECANE / TRIDECANE",
    "UNDECANE/TRIDECANE":   "UNDECANE / TRIDECANE",
    "UNDECANE / TRİDECANE": "UNDECANE / TRIDECANE",
    "HİDROLİZEİPEK PROTEİN":"HİDROLİZE İPEK PROTEİN",
    "HİDROLİZE İPEK PROTEİN":"HİDROLİZE İPEK PROTEİN",
    "GİNSENG EKSTRAKTI":  "GİNSENG EKSTRAKTI",  # may not exist
    "PAPATYA HİDROSOLÜ":  "PAPATYA HİDROSOLÜ",
    "MEYAN KÖKÜ EKSTRAKTI":  "MEYAN KÖKÜ EKSTRAKTI",
    "MEYAN KÖKÜ ESKTRATI":   "MEYAN KÖKÜ EKSTRAKTI",
    "C VİTAMİNİ":         "C VİTAMİNİ",
    "KAYISI ÇEKİRDEGİ YAGI": "KAYISI ÇEKİRDEGİ YAGI",
    "KIRMIZI YONCA":      "KIRMIZI YONCA YAĞI",
    "KIRMIZI YONCA YAĞI": "KIRMIZI YONCA YAĞI",
    "BUGDAY RUŞEYN YAĞI": "BUĞDAY RUŞEYM YAGI",
    "ALEOVERA EKSTRAKTI": "ALEOVERA EKSTRAKTI",
}


def normalize(s):
    """Normalize Turkish text for comparison."""
    s = s.upper().strip()
    s = re.sub(r'\s+', ' ', s)
    return (s.replace('İ', 'I').replace('Ş', 'S').replace('Ç', 'C')
             .replace('Ğ', 'G').replace('Ü', 'U').replace('Ö', 'O'))


def fuzzy_match(name, db_items):
    """Find best DB item by normalized fuzzy match. db_items is list of Item."""
    needle = normalize(name)
    best = (None, 0.0)
    for item in db_items:
        cand = normalize(item.name)
        ratio = SequenceMatcher(None, needle, cand).ratio()
        if ratio > best[1]:
            best = (item, ratio)
    return best


def add_missing_hammadde(db):
    """Phase 1: ensure all common essentials exist."""
    added = 0
    skipped = 0
    for name, unit, supplier_name in MISSING_HAMMADDE:
        existing = db.query(Item).filter(Item.name == name).first()
        if existing:
            skipped += 1
            continue
        supplier_id = None
        if supplier_name:
            supp = db.query(Supplier).filter(Supplier.name.ilike(f"%{supplier_name}%")).first()
            if supp:
                supplier_id = supp.id
        db.add(Item(
            name=name,
            category="Hammadde",
            unit=unit,
            current_stock=0.0,
            min_stock_level=0.0,
            cost_price=0.0,
            supplier_id=supplier_id,
            is_active=True,
        ))
        added += 1
    db.commit()
    print(f"   ✅ Hammadde eklendi: {added} yeni, {skipped} mevcut")


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
        # ─── Phase 1: ensure essentials exist ──
        print(f"\n→ Phase 1: Eksik hammaddeleri ekle")
        add_missing_hammadde(db)

        # ─── Phase 2: build hammadde lookup ──
        all_hammadde = db.query(Item).filter(
            Item.category == "Hammadde", Item.is_active == True
        ).all()
        print(f"   📚 DB'de {len(all_hammadde)} hammadde mevcut")

        # ─── Phase 3: for each cost file, create recipes for each catalog product ──
        print(f"\n→ Phase 2: Recipe'leri oluştur")
        recipes_created = 0
        recipes_skipped = 0
        ingredients_added = 0
        unmatched_ingredients = []

        for bom in boms:
            cost_file = bom["file"]
            # Skip duplicate
            if "(3)" in cost_file:
                continue
            target_products = PRODUCT_MAP.get(cost_file)
            if not target_products:
                print(f"   ⚠️  '{cost_file}' eşleşmedi, atlandı")
                continue

            for product_name, product_size in target_products:
                # Find catalog item
                target_item = db.query(Item).filter(Item.name == product_name).first()
                if not target_item:
                    print(f"   ⚠️  '{product_name}' DB'de yok, atlandı")
                    continue

                # Check if recipe already exists
                existing_recipe = db.query(Recipe).filter(
                    Recipe.target_item_id == target_item.id, Recipe.is_active == True
                ).first()
                if existing_recipe:
                    recipes_skipped += 1
                    continue

                # Compute scale factor (cost Excel was for `base_size`, we want `product_size`)
                # Evanira files are 500ml, Serenida files are at exact product size already
                base_size = 500 if "EVANIRA" in cost_file else product_size
                scale = product_size / base_size

                # Create recipe
                recipe = Recipe(
                    name=target_item.name,
                    output_quantity=1.0,
                    output_unit="adet",
                    target_item_id=target_item.id,
                    waste_percentage=5.0,   # default 5% fire (will be tuned later)
                    description=f"Hedef: {target_item.name}",
                    is_active=True,
                )
                db.add(recipe)
                db.flush()

                # Add ingredients
                row_count = 0
                for ing_data in bom["ingredients"]:
                    ing_name = ing_data["name"]
                    pct = ing_data["percent"]

                    # 1. Check override
                    target_db_name = INGREDIENT_OVERRIDES.get(ing_name.upper().strip())
                    matched = None
                    if target_db_name:
                        matched = next(
                            (i for i in all_hammadde if i.name == target_db_name), None
                        )

                    # 2. Fuzzy match (lazy fallback)
                    if not matched:
                        item, ratio = fuzzy_match(ing_name, all_hammadde)
                        if ratio >= 0.78:
                            matched = item

                    if not matched:
                        unmatched_ingredients.append((cost_file, ing_name))
                        continue

                    # Compute absolute quantity per 1 piece (1 bottle of `product_size` ml)
                    qty_ml = pct * product_size / 100.0   # % × volume / 100
                    db.add(RecipeIngredient(
                        recipe_id=recipe.id,
                        item_id=matched.id,
                        quantity=round(qty_ml, 4),
                        unit=matched.unit or "ml",
                    ))
                    row_count += 1

                ingredients_added += row_count
                recipes_created += 1
                print(f"   ✅ {product_name:<55s}  ({row_count} hammadde × {scale:.1f}x scale)")

        db.commit()

        print(f"\n{'='*70}")
        print(f"🎉 Recipe seed tamamlandı")
        print(f"   Recipe oluşturuldu:  {recipes_created}")
        print(f"   Recipe atlandı:      {recipes_skipped} (zaten vardı)")
        print(f"   Toplam ingredient:   {ingredients_added}")
        if unmatched_ingredients:
            print(f"\n⚠️  Eşleşmeyen ingredient'lar ({len(unmatched_ingredients)} satır):")
            seen = set()
            for f, n in unmatched_ingredients:
                key = n.upper().strip()
                if key in seen: continue
                seen.add(key)
                print(f"      • {n:<40s}  (örn: {f})")
            print(f"\n   Bunları UI'dan manuel ekleyebilirsin.")
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
