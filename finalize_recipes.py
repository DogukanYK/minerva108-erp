#!/usr/bin/env python3
"""
Final cleanup for the recipe seed:
  1. Add KUŞBURNU ÇEKİRDEĞİ YAĞI (rosehip seed oil)
  2. Add it to Minerva 108 Red Clover Night Cream recipe (1.5% per BOM)
  3. Dedup any duplicate (recipe_id, item_id) pairs in recipe_ingredients
"""
import os, sys, json, re, unicodedata
from collections import defaultdict
from database import SessionLocal, Item, Recipe, RecipeIngredient


def _nkey(s):
    if s is None: return ''
    s = unicodedata.normalize('NFKC', str(s))
    s = re.sub(r'\s+', ' ', s).strip().upper()
    return s


def main():
    db = SessionLocal()
    try:
        # ── Step 1: Add KUŞBURNU ÇEKİRDEĞİ YAĞI ──
        print(f"\n→ Step 1: KUŞBURNU ÇEKİRDEĞİ YAĞI ekle")
        existing = db.query(Item).filter(Item.name == "KUŞBURNU ÇEKİRDEĞİ YAĞI").first()
        if not existing:
            kusburnu = Item(
                name="KUŞBURNU ÇEKİRDEĞİ YAĞI", category="Hammadde", unit="ml",
                current_stock=0.0, cost_price=0.0, is_active=True,
            )
            db.add(kusburnu); db.flush()
            print(f"   ✅ Yeni hammadde eklendi (id={kusburnu.id})")
        else:
            kusburnu = existing
            print(f"   ⏭️  Zaten var (id={existing.id})")

        # ── Step 2: Add to Night Cream recipe ──
        print(f"\n→ Step 2: KUŞBURNU'yu Gece Kremi reçetesine ekle")
        # Read the % from the source BOM
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "minerva_minerva_boms.json")) as f:
            boms = json.load(f)
        gece_bom = next((b for b in boms if "GECE KREMİ" in b["file"]), None)
        kusburnu_pct = None
        if gece_bom:
            kusburnu_row = next(
                (i for i in gece_bom["ingredients"]
                 if "KUŞBURNU" in i["name"].upper()), None
            )
            if kusburnu_row:
                kusburnu_pct = kusburnu_row["percent"]

        if kusburnu_pct is None:
            print(f"   ⚠️  BOM'da bulunamadı, atlandı")
        else:
            night_cream = db.query(Item).filter(
                Item.name == "Minerva 108 Red Clover Night Cream (50ml)"
            ).first()
            if not night_cream:
                print(f"   ⚠️  Gece Kremi katalog'da yok")
            else:
                recipe = db.query(Recipe).filter(
                    Recipe.target_item_id == night_cream.id, Recipe.is_active == True
                ).first()
                if not recipe:
                    print(f"   ⚠️  Gece Kremi reçetesi yok")
                else:
                    # Idempotent — skip if already attached
                    already = db.query(RecipeIngredient).filter(
                        RecipeIngredient.recipe_id == recipe.id,
                        RecipeIngredient.item_id == kusburnu.id,
                    ).first()
                    if already:
                        print(f"   ⏭️  Zaten reçetede mevcut")
                    else:
                        qty = kusburnu_pct * 50 / 100.0   # 50ml product
                        db.add(RecipeIngredient(
                            recipe_id=recipe.id, item_id=kusburnu.id,
                            quantity=round(qty, 4), unit="ml",
                        ))
                        print(f"   ✅ Eklendi: {qty:.3f} ml ({kusburnu_pct}%)")

        db.commit()

        # ── Step 3: Dedup duplicate (recipe_id, item_id) pairs ──
        print(f"\n→ Step 3: Duplicate ingredient'ları temizle")
        all_links = db.query(RecipeIngredient).all()
        groups = defaultdict(list)
        for link in all_links:
            groups[(link.recipe_id, link.item_id)].append(link)

        deleted = 0
        for key, links in groups.items():
            if len(links) <= 1: continue
            # Keep the first (lowest id), delete the rest
            links_sorted = sorted(links, key=lambda l: l.id)
            for dup in links_sorted[1:]:
                db.delete(dup)
                deleted += 1

        db.commit()
        print(f"   ✅ {deleted} duplicate satır silindi")

        # ── Final stats ──
        recipe_count = db.query(Recipe).filter(Recipe.is_active == True).count()
        link_count = db.query(RecipeIngredient).count()
        print(f"\n{'='*70}")
        print(f"🎉 Sistem son hali:")
        print(f"   Toplam reçete:        {recipe_count}")
        print(f"   Toplam ingredient:    {link_count}")
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
