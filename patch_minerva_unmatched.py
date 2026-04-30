#!/usr/bin/env python3
"""
Patch the 17 still-unmatched ingredients in the Minerva recipes.

Adds 5 new hammadde + maps the rest to existing DB names + appends to recipes.
Idempotent.
"""
import os, sys, json, re, unicodedata
from database import SessionLocal, Item, Supplier, Recipe, RecipeIngredient


def _nkey(s):
    if s is None: return ''
    s = unicodedata.normalize('NFKC', str(s))
    s = re.sub(r'\s+', ' ', s).strip().upper()
    return s


# ── New hammadde to add ────────────────────────────────────────────────
NEW_HAMMADDE = [
    ("VANİLYA UÇUCU YAĞI",         "ml", "DOALINN"),
    ("SEDEF (PEARL PİGMENT)",      "g",  None),
    ("KAHVE ÇEKİRDEĞİ EKSTRAKTI",  "ml", None),
    ("TIBBİ NANE HİDROSOLÜ",       "ml", "ULUDAĞ HERBAL"),
    ("TIBBİ NANE UÇUCU YAĞI",      "ml", "DOALINN"),
    ("ELMA SİRKESİ",               "ml", None),
]


# ── Map unmatched cost-Excel names → DB hammadde names ───────────────
PATCH_MAP = {
    "CAPRYLIC CAPRIC TRIDYRECİDE":     "COPRLİC /CAPRİK TRİGLİSERİDE",
    "CAPRYLİC CAPRİC (TRIGLYRECİDE)":  "COPRLİC /CAPRİK TRİGLİSERİDE",
    "K-705(KORUYUCU)":                 "K-705 (SHAROMIX KORUYUCU)",
    "YASEMİN UY.":                     "YASEMİN UÇUCU YAĞI",
    "VANİLYA UY.":                     "VANİLYA UÇUCU YAĞI",
    "SİRKE":                           "ELMA SİRKESİ",
    "KUŞBURNU ÇEKİRDEĞİ YAĞI":         "KUŞBURNU ÇEKİRDEĞİ YAĞI",   # if exists in original DB
    "SEDEF (EFEKT PİGMENTİ)":          "SEDEF (PEARL PİGMENT)",
    "KAHVE ÇEKİRDEĞİ EKSTRAKTI":       "KAHVE ÇEKİRDEĞİ EKSTRAKTI",
    "PAPATYA UY. ( SARI )":            "PAPATYA UÇUCU YAĞI",
    "KORUYUCU (PANTYLENE GLİKOL)":     "PENTİLEN GLİKOL",
    "TIBBİ NANE HİDROSOLÜ":            "TIBBİ NANE HİDROSOLÜ",
    "TIBBİ NANE YAĞI":                 "TIBBİ NANE UÇUCU YAĞI",
    "AKGÜNLÜK UY":                     "AKGÜNLÜK UÇUCU YAĞI",
    "NEROLİ (PORTAKAL ÇİÇEĞİ HİDROSOLÜ)": "PORTAKAL ÇİÇEGİ (NEROLİ) SUYU",
    "PENTHANOL (B-5 VİTAMİNİ)":        "D-PANTHENOL",
}


# ── Cost-file → catalog product (matches seed_minerva_recipes.py) ───
PRODUCT_MAP = {
    "MİNERVA AFTER SUN GEL":                                "Minerva 108 After Sun Gel Cream (200ml)",
    "MİNERVA-108 GÜNEŞ KREMİ 50 SPF-200 ML":              "Minerva 108 Anti-Blemish Sun Protection Lotion SPF 50+ (200ml)",
    "MİNERVA 108 ALTIN SERİ FACE SCRUB YUZ PEELİNG":      "Minerva 108 Face Scrub (60ml)",
    "MİNERVA 108 ALTIN SERİ YÜZ YIKAMA JELİ":             "Minerva 108 Facial Cleansing Gel (200ml)",
    "MİNERVA ALTIN SERİ AYAKBAKIM KREMİ":                  "Minerva 108 Intensive Foot Care Cream (50ml)",
    "ÇOK FONKSİYONLU YAĞ YASEMİNLİ":                      "Minerva 108 Multi-Functional Beauty Oil — Jasmine (100ml)",
    "MİNERVA ALTIN SERİ ÇOK FONSİYONLULAVANDER YAG":       "Minerva 108 Multi-Functional Beauty Oil — Lavender (100ml)",
    "MİNERVA ALTIN SERİ TONİK":                            "Minerva 108 Red Clover Anti-Aging & Pore Minimizing Toner (200ml)",
    "MİNERVA 108  RED CLOVER DARK SPOT KREM":              "Minerva 108 Red Clover Anti-Dark Spot Cream (50ml)",
    "MİNERVA 108 KİL MASKESİ":                             "Minerva 108 Red Clover Clay Mask (50ml)",
    "MİNERVA 108 RED CLOVER  DAY CREAM GÜNDÜZ KREMİ":     "Minerva 108 Red Clover Day Cream (50ml)",
    "MİNERVA 108 RED CLOVER EYE CONTUUR KREM":             "Minerva 108 Red Clover Eye Contour Cream (10ml)",
    "MİNERVA 108 LİP BALM":                                "Minerva 108 Red Clover Lip Balm (10ml)",
    "MİNERVA 108 GECE KREMİ":                              "Minerva 108 Red Clover Night Cream (50ml)",
    "MİNERVA 108 RED CLOVER NOURİSHİNG EYE MAKEUP REMOVER": "Minerva 108 Red Clover Nourishing Eye Makeup Remover (50ml)",
    "MİNERVA ALTIN SERİ SHOWER JEL":                       "Minerva 108 Shower Gel (400ml)",
    "MİNERVA ALTIN SERİ +30 GÜNEŞ KREMİ":                 "Minerva 108 Sunscreen SPF 30+ (10ml)",
    "MİNERVA 108 ALTIN SERİ SU BAZLI SAÇ MASKESİ":        "Minerva 108 Water-Based Hair Mask 100ml",
    "minerva 108  altın seri göz çevresi kremi":           "Minerva 108 Red Clover Eye Contour Cream (10ml)",
    "MİNERVA 108 EL KREMİ":                                "Minerva 108 Hand Cream (50ml)",
    "MİNERVA-108 ANTI-AGING HAND CREAM":                   "Minerva 108 Anti-Aging Hand Cream (50ml)",
    "MİNERVA 108  HYALURONİK ASİT SERUM":                  "Minerva 108 Hyaluronic Acid Serum (30ml)",
    "MİNERVA 108 ALTIN SERİ VUCUT LOSYONU LAVANTALI":      "Minerva 108 Body Lotion — Lavender (200ml)",
    "VÜCUT LOSYONU YASEMİNLİ":                             "Minerva 108 Body Lotion — Jasmine (200ml)",
    "MİNERVA -108 DEOS TIK FORMÜLASYON":                   "Minerva 108 Stick Deodorant (15ml)",
    "55 FAKTÖR LEKE KARŞITI GÜNEŞ KORUYUCU LOS.":         "Minerva 108 Anti-Blemish Sun Protection SPF 55 (200ml)",
    "MİNERVA 108 ALTIN SERİ SAC KREMİ":                    "Minerva 108 Nourishing Hair Conditioner (200ml)",
    "MİNERVA 108 ALTIN SERİ  KURU SAÇ SAMPUANI":          "Minerva 108 Shampoo for Dry Hair (200ml)",
    "MİNERVA 108 ALTIN SERİ YAGLI SAÇ SAMPUAN":           "Minerva 108 Shampoo for Oily Hair (200ml)",
    "MİNERVA 108 ALTIN SERİ YOGUN SAÇ MASKESİ":           "Minerva 108 Intense Hair Mask (100ml)",
    "MİNERVA 108 ALTIN SERİ MAKYAJ TEMİZLEYİCİ":          "Minerva 108 Red Clover Nourishing Eye Makeup Remover (50ml)",
}


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    boms_path = os.path.join(here, "minerva_minerva_boms.json")
    if not os.path.exists(boms_path):
        print(f"❌ {boms_path} bulunamadı")
        return 1
    with open(boms_path) as f:
        boms = json.load(f)

    db = SessionLocal()
    try:
        # ── Phase 1: Add new hammadde ──────────────────────────
        print(f"\n→ Phase 1: Yeni hammadde ekle")
        added = 0; skipped = 0
        for name, unit, supplier_name in NEW_HAMMADDE:
            if db.query(Item).filter(Item.name == name).first():
                skipped += 1; continue
            sid = None
            if supplier_name:
                supp = db.query(Supplier).filter(Supplier.name.ilike(f"%{supplier_name}%")).first()
                if supp: sid = supp.id
            db.add(Item(
                name=name, category="Hammadde", unit=unit,
                current_stock=0.0, cost_price=0.0,
                supplier_id=sid, is_active=True,
            ))
            added += 1
        db.commit()
        print(f"   ✅ Yeni: {added}, atlandı: {skipped}")

        # ── Phase 2: Build lookup ──────────────────────────────
        all_hammadde = db.query(Item).filter(
            Item.category == "Hammadde", Item.is_active == True
        ).all()
        ham_by_nkey = {_nkey(i.name): i for i in all_hammadde}
        norm_product_map = {_nkey(k): v for k, v in PRODUCT_MAP.items()}
        norm_patch_map = {_nkey(k): v for k, v in PATCH_MAP.items()}

        # ── Phase 3: Walk BOMs, patch missing ingredients ──────
        print(f"\n→ Phase 2: Eksik ingredient'ları reçetelere ekle")
        patched = 0
        already_present = 0
        still_missing = []

        for bom in boms:
            cost_file = bom["file"]
            target_name = norm_product_map.get(_nkey(cost_file))
            if not target_name: continue

            target_item = db.query(Item).filter(Item.name == target_name).first()
            if not target_item: continue

            recipe = db.query(Recipe).filter(
                Recipe.target_item_id == target_item.id, Recipe.is_active == True
            ).first()
            if not recipe: continue

            # Determine product size from name
            m = re.search(r'(\d{1,4})\s*ML', target_name, re.IGNORECASE)
            product_size = int(m.group(1)) if m else 100

            for ing in bom["ingredients"]:
                ing_name = ing["name"]
                target_db_name = norm_patch_map.get(_nkey(ing_name))
                if not target_db_name: continue

                target_ham = ham_by_nkey.get(_nkey(target_db_name))
                if not target_ham:
                    still_missing.append((cost_file, ing_name, target_db_name))
                    continue

                # Check if already in recipe
                if db.query(RecipeIngredient).filter(
                    RecipeIngredient.recipe_id == recipe.id,
                    RecipeIngredient.item_id == target_ham.id,
                ).first():
                    already_present += 1
                    continue

                qty = ing["percent"] * product_size / 100.0
                db.add(RecipeIngredient(
                    recipe_id=recipe.id,
                    item_id=target_ham.id,
                    quantity=round(qty, 4),
                    unit=target_ham.unit or "ml",
                ))
                patched += 1
                print(f"   ✅ {target_name:<55s}  + {ing_name} ({qty:.3f} {target_ham.unit})")

        db.commit()
        print(f"\n{'='*70}")
        print(f"🎉 Patch tamamlandı")
        print(f"   Yeni ingredient bağlandı:    {patched}")
        print(f"   Zaten ekliydi:               {already_present}")
        if still_missing:
            print(f"   ⚠️  Hala bulunamayan: {len(still_missing)}")
            for f, n, t in still_missing[:5]:
                print(f"      • {n}  →  {t}  (in {f})")
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
