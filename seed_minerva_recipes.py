#!/usr/bin/env python3
"""
Minerva108 — Phase 4 recipe seeder for the Minerva 108 product line.

What it does:
  Phase 0: Update default fire (waste_percentage) from 5% → 10% on all existing recipes.
  Phase 1: Add missing hammadde encountered in Minerva formulas (oils, clays, etc.).
  Phase 2: Add 11 new catalog items for formulas that exist but aren't in catalog yet
           (extra body lotions, dry/oily shampoos, intensive hair mask, etc.).
  Phase 3: Create one recipe per matched Minerva formula file (with 10% default fire).

Idempotent — re-running skips already-created recipes & duplicate ingredients.
"""
import os, sys, json, re, unicodedata
from difflib import SequenceMatcher
from database import SessionLocal, Item, Supplier, Recipe, RecipeIngredient


def _nkey(s):
    if s is None: return ''
    s = unicodedata.normalize('NFKC', str(s))
    s = re.sub(r'\s+', ' ', s).strip().upper()
    return s

def normalize(s):
    s = _nkey(s)
    return s.replace('İ', 'I').replace('Ş', 'S').replace('Ç', 'C').replace('Ğ', 'G').replace('Ü', 'U').replace('Ö', 'O')


# ─── Ingredients in Minerva formulas not yet in DB ─────────────────────────
NEW_HAMMADDE = [
    ("OLİVEM 1000",                "g",  "ANYSIA NATURAL COSMETIC"),
    ("KIRMIZI YONCA MASERASYONU",  "ml", None),
    ("AT KUYRUĞU EKSTRAKTI",       "ml", None),
    ("LEXGARD (K-705)",            "g",  "SABUNARIA"),
    ("ITIR UÇUCU YAĞI",            "ml", "DOALINN"),
    ("SİYAH MİNERAL KİL",          "g",  None),
    ("MAVİ KİL",                   "g",  None),
    ("GÜL SUYU",                   "ml", None),
    ("YOSUN EKSTRAKTI",            "g",  None),
    ("KIRMIZI YONCA YAĞI",         "ml", None),
    ("ÖKALİPTÜS UÇUCU YAĞI",       "ml", "DOALINN"),
    ("SUSAM YAĞI",                 "ml", None),
    ("HİNT YAĞI",                  "ml", "UMAYCHEM"),
    ("ARI BALMUMU",                "g",  None),
    ("KAKAO BUTTER",               "g",  "TATLIDİLİMLER"),
    ("ZİNK OKSİT (NANO)",          "g",  None),
    ("TİTANYUM DİOKSİT",           "g",  None),
    ("MAGNEZYUM ALÜMİNYUM SİLİKAT","g",  None),
    ("ASKORBİL PALMİTAT",          "g",  "UMAYCHEM"),
    ("KAKAO YAĞI",                 "g",  "TATLIDİLİMLER"),
    ("SODİUM HYALÜRONAT",          "g",  "UMAYCHEM"),
    ("TALK",                       "g",  None),
    ("MİSK",                       "ml", "DOALINN"),
    ("YASEMİN UÇUCU YAĞI",         "ml", "DOALINN"),
    ("LAVANTA YAĞI",               "ml", "DOALINN"),
    ("CETOSTEARYL ALCOHOL",        "g",  "TATLIDİLİMLER"),
    ("SODYUM PCA",                 "g",  None),
    ("MENTOL",                     "g",  None),
    ("KAFEİN",                     "g",  None),
    ("ZEYTİN YAĞI",                "ml", None),
    ("SODYUM LAURİL SÜLFAT",       "g",  None),
    ("CETYL PALMITATE",            "g",  "TATLIDİLİMLER"),
    ("PROVITAMIN B5",              "g",  "TATLIDİLİMLER"),
]


# ─── Catalog items NOT in DB yet — add as Bitmiş Ürün ──────────────────────
# (catalog_name, size_str, parent_name_or_None_for_standalone)
NEW_CATALOG_ITEMS = [
    # Standalone Minerva extras (no parent)
    ("Minerva 108 Hand Cream (50ml)",                     None),
    ("Minerva 108 Anti-Aging Hand Cream (50ml)",          None),
    ("Minerva 108 Hyaluronic Acid Serum (30ml)",          None),
    ("Minerva 108 Body Lotion — Lavender (200ml)",        None),
    ("Minerva 108 Body Lotion — Jasmine (200ml)",         None),
    ("Minerva 108 Stick Deodorant (15ml)",                None),
    ("Minerva 108 Anti-Blemish Sun Protection SPF 55 (200ml)", None),
    ("Minerva 108 Nourishing Hair Conditioner (200ml)",   None),
    ("Minerva 108 Shampoo for Dry Hair (200ml)",          None),
    ("Minerva 108 Shampoo for Oily Hair (200ml)",         None),
    ("Minerva 108 Intense Hair Mask (100ml)",             None),
]


# ─── Formula filename → catalog product mapping ─────────────────────────
PRODUCT_MAP = {
    # Direct catalog matches
    "MİNERVA AFTER SUN GEL":                                "Minerva 108 After Sun Gel Cream",
    "MİNERVA-108 GÜNEŞ KREMİ 50 SPF-200 ML":              "Minerva 108 Anti-Blemish Sun Protection Lotion SPF 50+",
    "MİNERVA 108 ALTIN SERİ FACE SCRUB YUZ PEELİNG":      "Minerva 108 Face Scrub",
    "MİNERVA 108 ALTIN SERİ YÜZ YIKAMA JELİ":             "Minerva 108 Facial Cleansing Gel",
    "MİNERVA ALTIN SERİ AYAKBAKIM KREMİ":                  "Minerva 108 Intensive Foot Care Cream",
    "ÇOK FONKSİYONLU YAĞ YASEMİNLİ":                      "Minerva 108 Multi-Functional Beauty Oil — Jasmine",
    "MİNERVA ALTIN SERİ ÇOK FONSİYONLULAVANDER YAG":       "Minerva 108 Multi-Functional Beauty Oil — Lavender",
    "MİNERVA ALTIN SERİ TONİK":                            "Minerva 108 Red Clover Anti-Aging & Pore Minimizing Toner",
    "MİNERVA 108  RED CLOVER DARK SPOT KREM":              "Minerva 108 Red Clover Anti-Dark Spot Cream",
    "MİNERVA 108 KİL MASKESİ":                             "Minerva 108 Red Clover Clay Mask",
    "MİNERVA 108 RED CLOVER  DAY CREAM GÜNDÜZ KREMİ":     "Minerva 108 Red Clover Day Cream",
    "MİNERVA 108 RED CLOVER EYE CONTUUR KREM":             "Minerva 108 Red Clover Eye Contour Cream",
    "MİNERVA 108 LİP BALM":                                "Minerva 108 Red Clover Lip Balm",
    "MİNERVA 108 GECE KREMİ":                              "Minerva 108 Red Clover Night Cream",
    "MİNERVA 108 RED CLOVER NOURİSHİNG EYE MAKEUP REMOVER": "Minerva 108 Red Clover Nourishing Eye Makeup Remover",
    "MİNERVA ALTIN SERİ SHOWER JEL":                       "Minerva 108 Shower Gel",
    "MİNERVA ALTIN SERİ +30 GÜNEŞ KREMİ":                 "Minerva 108 Sunscreen SPF 30+",
    "MİNERVA 108 ALTIN SERİ SU BAZLI SAÇ MASKESİ":        "Minerva 108 Water-Based Hair Mask 100ml",
    # Eye contour cream — duplicate file (alternate formula)
    "minerva 108  altın seri göz çevresi kremi":           "Minerva 108 Red Clover Eye Contour Cream",
    # New catalog items (created in Phase 2)
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
    "MİNERVA 108 ALTIN SERİ MAKYAJ TEMİZLEYİCİ":          "Minerva 108 Red Clover Nourishing Eye Makeup Remover",
}


# ─── Override map for ingredient names — map cost-Excel name → DB hammadde ──
INGREDIENT_OVERRIDES = {
    "DİSTİLE SU":                  "DİSTİLE SU",
    "GLİSERİN":                    "GLİSERİN",
    "GLİSERİN ":                   "GLİSERİN",
    "ALLANTOIN":                   "ALLANTOİN",
    "ALLANTOİN":                   "ALLANTOİN",
    "ALOVERA EKSTRAKTI":           "ALEOVERA EKSTRAKTI",
    "ALEOVERA EKSTRAKTI":          "ALEOVERA EKSTRAKTI",
    "ALPHA ARBUTIN":               "ALPHA ARBUTİN",
    "ALPHA ARBUTİN":               "ALPHA ARBUTİN",
    "C VİTAMİNİ":                  "C VİTAMİNİ",
    "E VİTAMİNİ":                  "E VİTAMİNİ",
    "E VİT.":                      "E VİTAMİNİ",
    "K-705":                       "K-705 (SHAROMIX KORUYUCU)",
    "K-705 (KORUYUCU)":            "K-705 (SHAROMIX KORUYUCU)",
    "K-705 ( KORUYUCU )":          "K-705 (SHAROMIX KORUYUCU)",
    "K705":                        "K-705 (SHAROMIX KORUYUCU)",
    "LEXGARD (K-705)":             "LEXGARD (K-705)",
    "SHAROMİX 705":                "K-705 (SHAROMIX KORUYUCU)",
    "PENTILEN GLİKOL":             "PENTİLEN GLİKOL",
    "PENTİLEN GLİKOL":             "PENTİLEN GLİKOL",
    "BETAİN":                      "BETAİN",
    "COCO BETAİN":                 "COCO BETAİNE%30",
    "COCO BETAINE":                "COCO BETAİNE%30",
    "PANTHENOL":                   "D-PANTHENOL",
    "D-PANTHENOL":                 "D-PANTHENOL",
    "D PANTHENOL":                 "D-PANTHENOL",
    "PROVITAMIN B5":               "PROVITAMIN B5",
    "DEPENTHANOL":                 "DEPENTHANOL",
    "DEPANTHANOL":                 "DEPANTHANOL",
    "STEARYL ALCHOL":              "STEARYL ALCOHOL",
    "STEARYL ALCOHOL":             "STEARYL ALCOHOL",
    "CETEARYL ALCOHOL":            "CETEARYL ALCOHOL",
    "CETOSTEARYL ALCOHOL":         "CETOSTEARYL ALCOHOL",
    "CETYL ALCOHOL":               "CETYL ALCOHOL",
    "CETYL PALMITATE":             "CETYL PALMITATE",
    "SHEA BUTTER":                 "SHEA BUTTER",
    "SHEA BUTTER ":                "SHEA BUTTER",
    "OLİVEM 1000":                 "OLİVEM 1000",
    "OLIVEM 1000":                 "OLİVEM 1000",
    "EMULGADE SE-PF":              "EMULGADE SE-PF",
    "EMULGADE SE PF":              "EMULGADE SE-PF",
    "NEOWAX SE-PF":                "NEOWAX SE-PF",
    "NEOWAX SE PF":                "NEOWAX SE-PF",
    "GLİSERİL STEARAT":            "ELİTO GLİSERİL STEARAT    GMS",
    "GLYCERYL STEARAT":            "GMS (GLİSERİL MONO STEARAT)",
    "GMS":                         "GMS (GLİSERİL MONO STEARAT)",
    "STEARİC ASİT":                "STERİK ASİT",
    "STERİK ASİT":                 "STERİK ASİT",
    "STEARİC ACİD":                "STERİK ASİT",
    "BUGDAY RUŞEYM YAĞI":          "BUĞDAY RUŞEYM YAGI",
    "BUĞDAY RUŞEYM YAĞI":          "BUĞDAY RUŞEYM YAGI",
    "BUĞDAY YAĞI":                 "BUĞDAY YAGI",
    "BADEM YAĞI":                  "TATLI BADEM YAGI",
    "TATLI BADEM YAĞI":            "TATLI BADEM YAGI",
    "ARGAN YAĞI":                  "ARGAN YAGI",
    "JOJOBA YAĞI":                 "JOJOBA YAĞI",
    "ASPİR YAĞI":                  "ASPİR YAĞI",
    "AVAKADO YAĞI":                "AVAKADO YAGI",
    "AVOKADO YAĞI":                "AVAKADO YAGI",
    "HİNDİSTAN CEVİZİ YAĞI":       "HİNDİSTAN CEVİZİ YAGI",
    "PORTAKAL YAĞI":               "PORTAKAL UÇUCU YAĞI (TATLI)",
    "PORTAKAL UY.":                "PORTAKAL UÇUCU YAĞI (TATLI)",
    "PORTAKAL U.Y":                "PORTAKAL UÇUCU YAĞI (TATLI)",
    "ÇAY AĞACI YAĞI":              "ÇAY AĞACI YAĞI",
    "LAVANTA UÇUCU YAĞI":          "LAVANTA UÇUCU YAĞI",
    "LAVANTA YAĞI":                "LAVANTA YAĞI",
    "LAVANTA HİDROSOLÜ":           "LAVANTA HİDRASOLÜ",
    "LAVANTA HİDRASOLÜ":           "LAVANTA HİDRASOLÜ",
    "ITIR UY.":                    "ITIR UÇUCU YAĞI",
    "ITIR U.Y":                    "ITIR UÇUCU YAĞI",
    "ITIR UÇUCU YAĞI":             "ITIR UÇUCU YAĞI",
    "BERGAMOT UÇUCU YAĞI":         "BERGAMOT UÇUCU YAĞI",
    "BERGAMUT YAĞI":               "BERGAMOT UÇUCU YAĞI",
    "BERGAMUT U.Y":                "BERGAMOT UÇUCU YAĞI",
    "PALMAROSA":                   "PALMAROSA UÇUCU YAĞI",
    "ÖKALİPTÜS UY.":               "ÖKALİPTÜS UÇUCU YAĞI",
    "ÖKALİPTÜS U.Y":               "ÖKALİPTÜS UÇUCU YAĞI",
    "PAPATYA UY.":                 "PAPATYA UÇUCU YAĞI",
    "MİSK ADAÇAYI U.Y":            "MİSK ADAÇAYI UÇUCU YAĞI",
    "BİBERİYE UY":                 "BİBERİYE UÇUCU YAĞI",
    "BİBERİYE UÇUCU YAĞI":         "BİBERİYE UÇUCU YAĞI",
    "AT KUYRUĞU EKSTRAKTI":        "AT KUYRUĞU EKSTRAKTI",
    "AT KESTANESİ EKSTRAKTI":      "AT KESTANESİ EKSTRAKTI",
    "MEYAN KÖKÜ EKSTRAKTI":        "MEYAN KÖKÜ EKSTRAKTI",
    "GİNSENG EKSTRAKTI":           "GİNSENG EKSTRAKTI",
    "YOSUN EKSTRAKTI":             "YOSUN EKSTRAKTI",
    "ALEOVERA EKST.":              "ALEOVERA EKSTRAKTI",
    "KIRMIZI YONCA":               "KIRMIZI YONCA YAĞI",
    "KIRMIZI YONCA YAĞI":          "KIRMIZI YONCA YAĞI",
    "KIRMIZI YONCA MASERASYONU":   "KIRMIZI YONCA MASERASYONU",
    "BENTONİT KİL":                "BENTONİT KİL",
    "BEYAZ KAOLİN KİL":            "BEYAZ KAOLİN KİL",
    "PEMBE KAOLİN KİL":            "PEMBE KAOLİN KİL",
    "SİYAH MİNERAL KİL":           "SİYAH MİNERAL KİL",
    "MAVİ KİL":                    "MAVİ KİL",
    "MAVİ YEŞİL DOGAL KİL":        "MAVİ YEŞİL DOGAL KİL",
    "GÜL SUYU":                    "GÜL SUYU",
    "GÜL HİDROLATI":               "SAF GÜL SUYU",
    "SAF GÜL SUYU":                "SAF GÜL SUYU",
    "PORTAKAL ÇİÇEGİ (NEROLİ) SUYU":"PORTAKAL ÇİÇEGİ (NEROLİ) SUYU",
    "NEROLİ HİDROSOLÜ":            "NEROLİ HİDROSOLÜ",
    "PAPATYA HİDROSOLÜ":           "PAPATYA HİDROSOLÜ",
    "ITIR SARDUNYA HİDROSOLÜ":     " ITIR SARDUNYA HİDROSOLÜ",
    "NIACINAMIDE":                 "NİACİNAMİDE",
    "NİACİNAMİDE":                 "NİACİNAMİDE",
    "HİDROLİZE İPEK PROTEİN":      "HİDROLİZE İPEK PROTEİN",
    "HYALURONİK ASİT (D.MOL)":     "HYALUORİK ASİT (DÜŞÜK MOL)",
    "HYALURONİK ASİT":             "HYALUORİK ASİT (DÜŞÜK MOL)",
    "SODİUM HYALÜRONAT":           "SODİUM HYALÜRONAT",
    "ASKORBİL PALMİTAT":           "ASKORBİL PALMİTAT",
    "FERULIC ACID":                "FERULİC ACİD",
    "KOJIK ASIT":                  "KOJİK ASİT",
    "KOJİK ASİT":                  "KOJİK ASİT",
    "LACTIC ASİT":                 "LACTIC ASİT",
    "LAKTİK ASİT":                 "LACTIC ASİT",
    "SODYUM LAKTAT":               "SODYUM LAKTAT",
    "SODYUM PCA":                  "SODYUM PCA",
    "MENTOL":                      "MENTOL",
    "KAFEİN":                      "KAFEİN",
    "TALK":                        "TALK",
    "ARI BALMUMU":                 "ARI BALMUMU",
    "BAL MUMU":                    "ARI BALMUMU",
    "KAKAO BUTTER":                "KAKAO BUTTER",
    "KAKAO YAĞI":                  "KAKAO YAĞI",
    "ZİNK OKSİT":                  "ZİNK OKSİT (NANO)",
    "ÇİNKO OKSİT":                 "ZİNK OKSİT (NANO)",
    "ZİNK OKSİT (NANO)":           "ZİNK OKSİT (NANO)",
    "TİTANYUM DİOKSİT":            "TİTANYUM DİOKSİT",
    "MAGNEZYUM ALÜMİNYUM SİLİKAT": "MAGNEZYUM ALÜMİNYUM SİLİKAT",
    "MAGNEZYUM STEARAT":           "MAGNEZYUM STEARAT",
    "ŞEKER":                       "ŞEKER",
    "TUZ":                         "TUZ",
    "MİSK":                        "MİSK",
    "YASEMİN UÇUCU YAĞI":          "YASEMİN UÇUCU YAĞI",
    "ZEYTİN YAĞI":                 "ZEYTİN YAĞI",
    "HİNT YAĞI":                   "HİNT YAĞI",
    "SUSAM YAĞI":                  "SUSAM YAĞI",
    "SUSAM YAGI":                  "SUSAM YAĞI",
    "COCO CAPRYLATE/CAP.":         "COCO CAPRYLATE  CAPRATE",
    "COCO CAPRYLATE / CAPRATE":    "COCO CAPRYLATE  CAPRATE",
    "COCO CAPRYLATE":              "COCO CAPRYLATE  CAPRATE",
    "CAPRİLİC CAPRİC / TRIGLISERİDE": "COPRLİC /CAPRİK TRİGLİSERİDE",
    "CAPRYLIC CAPRIC TRİGLİSERİDE":  "COPRLİC /CAPRİK TRİGLİSERİDE",
    "UNDECANE / TRİDECANE":        "UNDECANE / TRIDECANE",
    "UNDECANE / TRIDECANE":        "UNDECANE / TRIDECANE",
    "COCO GLUCOSIDE":              "COCO GLUCOSIDE",
    "COCO GLUCOSİDE":              "COCO GLUCOSIDE",
    "LAURYL GLUCOSIDE":            "LAURYL GLUCOSIDE",
    "LAURYL GLUCOSİDE":            "LAURYL GLUCOSIDE",
    "DECYL GLUCOSIDE":             "COCO GLUCOSIDE",  # Closest match
    "SODYUM LAURİL SÜLFAT":        "SODYUM LAURİL SÜLFAT",
    "SODYUM BİKARBONAT":           "SODYUM BİKARBONAT",
    "MİKROKRİSTALİN SELÜLOZ":      "MİKROKRİSTALİN SELÜLOZ",
    "XHANTAN GUM":                 "XHANTAN GUM",
    "XANTHAN GUM":                 "XHANTAN GUM",
    "XSANTHAN GUM":                "XHANTAN GUM",
    "KSANTAN":                     "XHANTAN GUM",
    "KARNAUBA WAX":                "CARNAUBA WAX",
    "CARNAUBA WAX":                "CARNAUBA WAX",
    "CARNAUBA MUMU":               "CARNAUBA WAX",
    "KAYISI ÇEKİRDEĞİ YAĞI":       "KAYISI ÇEKİRDEGİ YAGI",
    "KAYISI ÇEKİRDEĞİ BUTTER":     "KAYISI ÇEKİRDEGİ YAGI",
    "AKTİF KÖMÜR":                 "AKTİF KÖMÜR",
    "INULIN":                      "INULİN",
    "INULİN":                      "INULİN",
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
        # ─── Phase 0: Update default fire 5 → 10 on existing recipes ─────
        print(f"\n→ Phase 0: Mevcut reçetelerin fire %'sini 5 → 10 güncelle")
        updated_fire = (
            db.query(Recipe)
            .filter(Recipe.waste_percentage == 5.0)
            .update({Recipe.waste_percentage: 10.0})
        )
        db.commit()
        print(f"   ✅ {updated_fire} recipe güncellendi")

        # ─── Phase 1: Add missing hammadde ───────────────────────────────
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
                current_stock=0.0, min_stock_level=0.0, cost_price=0.0,
                supplier_id=sid, is_active=True,
            ))
            added += 1
        db.commit()
        print(f"   ✅ Yeni hammadde: {added}, atlandı: {skipped}")

        # ─── Phase 2: Add missing catalog items ──────────────────────────
        print(f"\n→ Phase 2: Yeni Bitmiş Ürün ekle (katalog dışı formüller için)")
        catalog_added = 0
        for name, parent_name in NEW_CATALOG_ITEMS:
            if db.query(Item).filter(Item.name == name).first():
                continue
            parent_id = None
            if parent_name:
                p = db.query(Item).filter(Item.name == parent_name).first()
                if p: parent_id = p.id
            db.add(Item(
                name=name, category="Bitmiş Ürün", unit="adet",
                current_stock=0.0, cost_price=0.0,
                parent_id=parent_id, is_active=True,
            ))
            catalog_added += 1
        db.commit()
        print(f"   ✅ Yeni katalog ürünü: {catalog_added}")

        # ─── Phase 3: Build hammadde lookup ──────────────────────────────
        all_hammadde = db.query(Item).filter(
            Item.category == "Hammadde", Item.is_active == True
        ).all()
        ham_lookup = {_nkey(i.name): i for i in all_hammadde}
        norm_overrides = {_nkey(k): v for k, v in INGREDIENT_OVERRIDES.items()}
        norm_product_map = {_nkey(k): v for k, v in PRODUCT_MAP.items()}
        print(f"   📚 DB'de {len(all_hammadde)} hammadde mevcut")

        # ─── Phase 4: Create recipes ────────────────────────────────────
        print(f"\n→ Phase 3: Minerva reçetelerini oluştur")
        recipes_created = 0
        recipes_skipped = 0
        ingredients_added = 0
        unmatched = []

        for bom in boms:
            cost_file = bom["file"]
            target_name = norm_product_map.get(_nkey(cost_file))
            if not target_name:
                print(f"   ⚠️  '{cost_file}' eşleşmedi, atlandı")
                continue

            # Find catalog item
            target_item = db.query(Item).filter(Item.name == target_name).first()
            if not target_item:
                print(f"   ⚠️  '{target_name}' DB'de bulunamadı, atlandı")
                continue

            # Skip if recipe already exists
            existing = db.query(Recipe).filter(
                Recipe.target_item_id == target_item.id, Recipe.is_active == True
            ).first()
            if existing:
                recipes_skipped += 1
                continue

            # Determine product size from name (e.g., "(50ml)" or "200ml")
            m = re.search(r'(\d{1,4})\s*ML', target_name, re.IGNORECASE)
            product_size = int(m.group(1)) if m else 100   # default 100ml

            # Create recipe (10% fire default)
            recipe = Recipe(
                name=target_item.name,
                output_quantity=1.0,
                output_unit="adet",
                target_item_id=target_item.id,
                waste_percentage=10.0,
                description=f"Hedef: {target_item.name}",
                is_active=True,
            )
            db.add(recipe); db.flush()

            # Add ingredients
            row_count = 0
            for ing in bom["ingredients"]:
                ing_name = ing["name"]
                pct = ing["percent"]

                # 1. Try override
                target_db_name = norm_overrides.get(_nkey(ing_name))
                matched = None
                if target_db_name:
                    matched = ham_lookup.get(_nkey(target_db_name))

                # 2. Fuzzy fallback
                if not matched:
                    needle = normalize(ing_name)
                    best = (None, 0.0)
                    for item, key in ham_lookup.items():
                        ratio = SequenceMatcher(None, needle, normalize(key.name)).ratio()
                        if ratio > best[1]:
                            best = (key, ratio)
                    if best[1] >= 0.78:
                        matched = best[0]

                if not matched:
                    unmatched.append((cost_file, ing_name))
                    continue

                qty = pct * product_size / 100.0
                db.add(RecipeIngredient(
                    recipe_id=recipe.id,
                    item_id=matched.id,
                    quantity=round(qty, 4),
                    unit=matched.unit or "ml",
                ))
                row_count += 1

            ingredients_added += row_count
            recipes_created += 1
            print(f"   ✅ {target_name:<60s}  {row_count} hammadde (size {product_size}ml)")

        db.commit()
        print(f"\n{'='*70}")
        print(f"🎉 Minerva recipe seed tamamlandı")
        print(f"   Recipe oluşturuldu: {recipes_created}")
        print(f"   Atlandı (mevcut):    {recipes_skipped}")
        print(f"   Toplam ingredient:   {ingredients_added}")
        if unmatched:
            print(f"\n⚠️  Eşleşmeyen ingredient ({len(unmatched)} satır):")
            seen = set()
            for f, n in unmatched:
                k = _nkey(n)
                if k in seen: continue
                seen.add(k)
                print(f"      • {n}  (örn: {f})")
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
