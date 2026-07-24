# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# ─────────────────────────────────────────────────────────────────────────────
"""
Shopify entegrasyonu için IMS barkod hizalaması (GS1 master otorite).

Amaç: MINERVA_TUM_URUNLER master listesindeki GS1 barkodlarına IMS'i hizalamak.
Fuzzy eşleşme YOK — yalnız aşağıdaki EXPLICIT (id, eski→yeni) haritası, her satırda
"mevcut barkod beklenenle uyuşuyor mu" guard'ıyla uygulanır. İdempotent: hedef zaten
doğruysa atlar; beklenmedik durumda dokunmaz, uyarır.

KULLANIM
────────
  Dry-run (varsayılan — sadece rapor, YAZMAZ):
      python3 scripts/reconcile_shopify_barcodes.py
  Uygula:
      python3 scripts/reconcile_shopify_barcodes.py --commit

  Prod'da:  cd /var/www/minerva && set -a && source .env && set +a \\
            && venv/bin/python scripts/reconcile_shopify_barcodes.py [--commit]

DEĞİŞİKLİKLER (18) — prod ID'leriyle doğrulandı 2026-07:
  • Minerva 2 barkod düzeltme  (IMS eski/yanlış barkodda; GS1 = doğru)
  • Serenida 15 ÜTS barkod göçü (…207xxx → …8685282019xxx, Fatih Bey listesi)
  • 1 çift kayıt pasife alma    (id 625, stok 0, id 46'nın kopyası)
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import SessionLocal, Item            # noqa: E402

# (ims_id, eski_barkod, yeni_barkod, etiket)
BARCODE_UPDATES = [
    # ── Minerva — IMS eski/yanlış barkodda; master+Shopify+GS1 hemfikir ──
    (45, "8683829206529", "8683829206420", "Minerva Facial Cleansing Gel 200ml"),
    (50, "8683829206208", "8683829206598", "Minerva Red Clover Dark Spot Cream 50ml"),
    # ── Serenida — ÜTS barkod göçü (eski 207 → yeni 8685282019) ──
    (19, "8683829207069", "8685282019074", "Serenida Aloe Vera El/Ayak Losyonu 200ml"),
    (20, "8683829207052", "8685282019067", "Serenida Anti-Akne Losyon 100ml"),
    (22, "8683829207144", "8685282019135", "Serenida Leke Karşıtı Krem 50ml"),
    (23, "8683829207014", "8685282019029", "Serenida Bentonit Kil Maskesi 50ml"),
    (24, "8683829207083", "8685282019098", "Serenida Bikini Bölgesi Krem 200ml"),
    (25, "8683829207076", "8685282019081", "Serenida Kömürlü Diş Macunu 100ml"),
    (26, "8683829207007", "8685282019012", "Serenida Yüz Toniği 100ml"),
    (29, "8683829207021", "8685282019036", "Serenida Aydınlatıcı Temizleme Jeli 100ml"),
    (30, "8683829207137", "8685282019142", "Serenida Hyaluronik Asit Serumu 30ml"),
    (32, "8683829207090", "8685282019104", "Serenida Niasinamid Losyon 200ml"),
    (33, "8683829207120", "8685282019005", "Serenida Besleyici Vücut Losyonu 200ml"),
    (36, "8683829207038", "8685282019043", "Serenida Kuru Saç Şampuan 200ml"),
    (37, "8683829207045", "8685282019050", "Serenida Yağlı Saç Şampuan 200ml"),
    (38, "8683829207106", "8685282019111", "Serenida Stick Deodorant 15ml"),
    (39, "8683829207113", "8685282019128", "Serenida Beyazlık Diş Macunu 100ml"),
]

# (ims_id, barkod, açıklama) — stok 0 + barkod eşleşiyorsa pasife al
SOFT_DELETES = [
    (625, "8683829206284", "Minerva Foot Care Cream (id 46'nın BÜYÜK-harf kopyası)"),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true",
                    help="Değişiklikleri uygula (varsayılan: dry-run)")
    args = ap.parse_args()

    db = SessionLocal()
    applied = skipped = warned = 0
    try:
        by_id = {it.id: it for it in db.query(Item).filter(
            Item.id.in_([u[0] for u in BARCODE_UPDATES] + [d[0] for d in SOFT_DELETES])).all()}

        print(f"{'ID':>5}  {'DURUM':<14}  {'ESKİ':<14} → {'YENİ':<14}  ETİKET")
        print("-" * 92)

        # ── Barkod güncellemeleri ──
        for iid, old, new, label in BARCODE_UPDATES:
            it = by_id.get(iid)
            if not it or not it.is_active:
                print(f"{iid:>5}  {'YOK/PASİF':<14}  {'':<14}   {'':<14}  {label}")
                warned += 1
                continue
            cur = (it.barcode or "").strip()
            if cur == new:
                print(f"{iid:>5}  {'zaten doğru':<14}  {'':<14}   {new:<14}  {label}")
                skipped += 1
                continue
            if cur != old:
                # Beklenmedik mevcut barkod — DOKUNMA, uyar
                print(f"{iid:>5}  {'UYUŞMAZ⚠':<14}  {cur:<14} ≠ {old:<14}  {label} — beklenen eski barkod tutmuyor, atlandı")
                warned += 1
                continue
            # Hedef barkod başka aktif üründe var mı? (çakışma guard'ı)
            clash = (db.query(Item).filter(Item.barcode == new, Item.is_active == True,
                                           Item.id != iid).first())
            if clash:
                print(f"{iid:>5}  {'ÇAKIŞMA⚠':<14}  {old:<14} → {new:<14}  {label} — hedef barkod id {clash.id}'de var, atlandı")
                warned += 1
                continue
            if args.commit:
                it.barcode = new
            print(f"{iid:>5}  {('✓ GÜNCELLE' if args.commit else 'güncellenecek'):<14}  {old:<14} → {new:<14}  {label}")
            applied += 1

        # ── Pasife almalar ──
        for iid, bc, label in SOFT_DELETES:
            it = by_id.get(iid)
            if not it:
                print(f"{iid:>5}  {'YOK':<14}  {'':<14}   {'':<14}  {label}")
                warned += 1
                continue
            if not it.is_active:
                print(f"{iid:>5}  {'zaten pasif':<14}  {'':<14}   {'':<14}  {label}")
                skipped += 1
                continue
            cur = (it.barcode or "").strip()
            stok = float(it.current_stock or 0)
            if cur != bc or stok != 0:
                print(f"{iid:>5}  {'GUARD⚠':<14}  barkod={cur} stok={stok:g}  {label} — (barkod/stok beklenenle uyuşmuyor) atlandı")
                warned += 1
                continue
            if args.commit:
                it.is_active = False
            print(f"{iid:>5}  {('✓ PASİFE' if args.commit else 'pasife alınacak'):<14}  {'':<14}   {'':<14}  {label}")
            applied += 1

        if args.commit:
            db.commit()
        else:
            db.rollback()

        print("-" * 92)
        print(f"Özet:  uygulanan {applied} · atlanan(zaten doğru) {skipped} · uyarı {warned}")
        if not args.commit:
            print("       (dry-run — uygulamak için --commit)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
