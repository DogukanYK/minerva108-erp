"""Bitmiş ürün adlarını + barkodlarını GS1 master listesiyle hizalar (idempotent).

Kaynak: `MINERVA_TUM_URUNLER_Master.xlsx` (65 kozmetik + 4 gıda takviyesi, GS1 ✓
ÜTS ✓) → `data/gs1_master.json` (barkod → resmî marka/ad/ölçü).

Yaptıkları — HEPSİ salt metin/barkod, **stoğa ve Transaction'a DOKUNMAZ**:

  ① `name_tr` = resmî GS1/ÜTS adı.  Sistemdeki Türkçe adlar elle girilmişti ve
     28'i tescilli addan sapıyordu ("ALOEVERA…", "HYLURONIC", "ÇUBUK DEODORANT").
     Etiket/ÜTS ile tutarlılık için tescilli ad esas alınır.
  ② Gıda takviyesi barkodları (614-617) — sistemde 4 takviye barkodsuzdu.
     Eşleme hacimden: 150CC şişe = büyük gramaj, 80CC = küçük gramaj.
  ③ Adlardaki `(TR)` / `(EN)` / `(ING)` dil etiketleri silinir.  Dil ayrımı bu
     sistemde ÜRÜNDE değil ETİKETTE tutulur (`category='Ambalaj'`,
     `pkg_type='etiket'`, `language` + `label_group`); ada yazılmaları hataydı.
  ④ `Minerva-108` → `Minerva 108`.  Tire marka tespitini SESSİZCE bozuyor:
     `core.brands.brand_of` ilk kelimeyi alır, tireli ad tek kelime sayılır →
     şahit numune dolabı ayrı açılır, lot kodu MNR yerine MIN olur ve
     `core.shopify.canonical_brand` None döner (ürün Shopify'a hiç senkronlanmaz).
  ⑤ Çift boşluklar tekilleştirilir.

KAPSAM DIŞI (bilinçli): stajyerin açtığı kopya kayıtların BİRLEŞTİRİLMESİ ve
stok aktarımı.  O ayrı bir karar — kopya kayda barkod yazmak da yasak, çünkü
`core/shopify.py` aynı barkodu ≥2 aktif üründe görürse ürünü "belirsiz" sayıp
senkronu ATLAR.

Kullanım:
    venv/bin/python scripts/fix_finished_products.py            # kuru çalıştırma
    venv/bin/python scripts/fix_finished_products.py --commit   # yaz
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal, Item                          # noqa: E402

COMMIT = "--commit" in sys.argv
DATA = Path(__file__).resolve().parent.parent / "data" / "gs1_master.json"

# Gıda takviyeleri master listede gramajla, sistemde şişe hacmiyle (CC) duruyor.
# Aynı ürünün büyük şişesi büyük gramajı taşır — eşleme buradan.
SUPPLEMENT_BARCODES = {
    614: "8685282019180",   # GEVEN&BOR KAPSÜL 80CC      → Çin Geveni & Bor 28.72 g
    615: "8685282019173",   # GEVEN&BOR KAPSÜL 150CC     → Çin Geveni & Bor 63.19 g
    616: "8685282019166",   # KIRMIZI YONCA KAPSÜL 80CC  → Kırmızı Yonca    26.16 g
    617: "8685282019159",   # KIRMIZI YONCA KAPSÜL 150CC → Kırmızı Yonca    57.56 g
}

LANG_TAG = re.compile(r"\s*\((?:TR|EN|ING)\)")


def clean_name(name: str) -> str:
    """Dil etiketini at, `Minerva-108`'i düzelt, boşlukları tekilleştir."""
    out = LANG_TAG.sub("", name or "")
    out = re.sub(r"\bMinerva-108\b", "Minerva 108", out, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", out).strip()


def main() -> int:
    gs1 = json.loads(DATA.read_text(encoding="utf-8"))
    db = SessionLocal()
    name_fix, tr_fix, bc_fix = [], [], []
    try:
        items = (db.query(Item)
                 .filter(Item.category == "Bitmiş Ürün")
                 .order_by(Item.id).all())

        for it in items:
            # ── ② eksik takviye barkodu ──
            want_bc = SUPPLEMENT_BARCODES.get(it.id)
            if want_bc and (it.barcode or "").strip() != want_bc:
                bc_fix.append((it.id, it.name, it.barcode or "—", want_bc))
                if COMMIT:
                    it.barcode = want_bc

            # ── ③④⑤ ad temizliği ──
            new_name = clean_name(it.name)
            if new_name and new_name != it.name:
                name_fix.append((it.id, it.name, new_name))
                if COMMIT:
                    it.name = new_name

            # ── ① resmî Türkçe ad ──
            bc = (want_bc or (it.barcode or "").strip())
            rec = gs1.get(bc)
            if rec and (it.name_tr or "").strip() != rec["name_tr"]:
                tr_fix.append((it.id, new_name or it.name,
                               it.name_tr or "(BOŞ)", rec["name_tr"]))
                if COMMIT:
                    it.name_tr = rec["name_tr"]

        if COMMIT:
            db.commit()
    finally:
        db.close()

    print(f"=== ② EKSİK BARKOD ({len(bc_fix)}) ===")
    for iid, name, old, new in bc_fix:
        print(f"  id={iid:3} {name[:44]:44} {old:>13} → {new}")

    print(f"\n=== ③④⑤ AD TEMİZLİĞİ ({len(name_fix)}) ===")
    for iid, old, new in name_fix:
        print(f"  id={iid:3} {old[:52]:52} → {new}")

    print(f"\n=== ① RESMÎ TÜRKÇE AD ({len(tr_fix)}) ===")
    for iid, name, old, new in tr_fix:
        print(f"  id={iid:3} {name[:40]:40}\n        eski: {old}\n        yeni: {new}")

    print()
    print("✓ YAZILDI — stok ve Transaction'a DOKUNULMADI." if COMMIT
          else "KURU ÇALIŞTIRMA — hiçbir şey yazılmadı.  Uygulamak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
