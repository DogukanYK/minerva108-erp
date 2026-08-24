"""Stoklu/reçeteli kopya hammadde kümeleri için kullanıcıya karar tablosu
üretir — SALT-OKUNUR, hiçbir şey yazmaz.

24.08.2026 numune olayını araştırırken Türkçe-katlanmış ad taramasında 40
grup çıktı.  Bunların ~16'sı zombi (stok 0, referanssız) —
`scripts/deactivate_zombie_items_20260824.py` onları ayrıca kapatır.  Geri
kalan 24 küme GERÇEKTEN stoklu ve/veya reçetede kullanılan kartlar taşıyor —
otomatik birleştirmek riskli (bazıları gerçekten AYRI ürün: "GÜL EKSTRAKTI"
vs "GÜL YAĞI" farklı hammaddeler, "GİNSENG EKSTRAKTI" 4 farklı kartta 4
farklı birimde duruyor).  Bu script hiçbir karar VERMEZ — yalnız tabloyu
basar, kullanıcı hangi kartın kalacağını (ve hangilerinin birleşeceğini)
işaretler.  Kararlar dönünce ayrı bir birleştirme script'i yazılacak (FK
yüzeyi geniş: recipe_ingredients, inventory, supplier_prices, transactions,
retention_samples, delivery_items, product_return_items, quotation_items,
distributor_prices [UNIQUE(distributor_id,item_id) çakışması], items.parent_id,
recipes.target_item_id, production_history.target_item_id — ve stock_snapshot
ASLA repoint edilmez, o dondurulmuş tarih).

Zombilerden SONRA çalıştır — onlar pasifleşince kümeden düşer, tablo
temizlenir.  Tekrar tekrar çalıştırılabilir (salt-okunur).

Kullanım:
    venv/bin/python scripts/report_duplicate_decision_table.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import (SessionLocal, Item, Transaction, RecipeIngredient,   # noqa: E402
                      Recipe, Inventory, SupplierPrice, DistributorPrice)

CLUSTERS = [
    ("LAURYL GLUCOSİDE", [131, 632]),
    ("ÇİNKO OKSİT", [120, 633]),
    ("CARNAUBA WAX", [91, 634]),
    ("KAKAO BUTTER", [128, 635]),
    ("XHANTAN GUM", [105, 636]),
    ("TATLI BADEM YAGI", [182, 639]),
    ("D-PANTHENOL", [66, 132]),
    ("BADEM YAĞI", [156, 181]),
    ("HİNT YAĞI", [164, 165]),
    ("SUSAM YAĞI", [166, 272]),
    ("LAVANTA HİDROSOLÜ", [144, 162]),
    ("GÜL HİDROSOLÜ", [141, 255]),
    ("GEVEN EKSTRAKTI", [231, 618]),
    ("KIRMIZI YONCA EXTRACT/YAĞI", [138, 619]),
    ("GİNSENG EKSTRAKTI", [161, 240, 575, 728]),
    ("MEYAN KÖKÜ EKSTRAKTI", [261, 727]),
    ("JAPON NANESİ", [198, 725]),
    ("AT KUYRUĞU EKSTRAKTI", [243, 251]),
    ("BİBERİYE", [84, 215, 245, 568]),
    ("MİSK ADAÇAYI UÇUCU YAĞI", [88, 225]),
    ("VANİLYA UÇUCU YAĞ", [223, 306]),
    ("PORTAKAL YAĞI", [167, 221, 565]),
    ("ITIR YAĞI", [188, 266]),
    ("ALMAN PAPATYASI / PAPATYA UÇUCU YAĞI", [732, 196, 216, 246]),
]


def fmt(v, w):
    s = "" if v is None else str(v)
    return (s[:w]).ljust(w)


def main() -> int:
    db = SessionLocal()
    try:
        print("# Kopya hammadde kartları — karar tablosu (24.08.2026 numune "
              "olayı taramasından)\n")
        print("Karar seçenekleri: **Birleştir → hedef ID yaz** ya da "
              "**İkisi de kalsın (farklı ürün)**.\n")
        any_row = False
        for label, ids in CLUSTERS:
            items = (db.query(Item).filter(Item.id.in_(ids)).order_by(Item.id).all())
            if not items:
                continue
            active_items = [i for i in items if i.is_active]
            if len(active_items) < 2:
                continue   # zombi temizliği sonrası tek kart kaldıysa küme kapandı
            any_row = True
            print(f"## {label}\n")
            header = ("Karar", "id", "Ad", "TR Ad", "Birim", "Stok", "Aktif",
                      "Reçete", "İşlem", "Son işlem", "Lot", "Tedar.Fiyat",
                      "Distrib.Fiyat", "Açıldı")
            print("| " + " | ".join(header) + " |")
            print("|" + "|".join(["---"] * len(header)) + "|")
            for it in items:
                tx_q = db.query(Transaction).filter(Transaction.item_id == it.id)
                tx_count = tx_q.count()
                last_tx = tx_q.order_by(Transaction.timestamp.desc()).first()
                recipe_count = db.query(RecipeIngredient).filter(
                    RecipeIngredient.item_id == it.id).count()
                lot_count = db.query(Inventory).filter(Inventory.item_id == it.id).count()
                sp_count = db.query(SupplierPrice).filter(SupplierPrice.item_id == it.id).count()
                dp_count = db.query(DistributorPrice).filter(DistributorPrice.item_id == it.id).count()
                row = [
                    "",
                    str(it.id),
                    fmt(it.name, 30),
                    fmt(it.name_tr or "—", 20),
                    it.unit or "—",
                    f"{it.current_stock or 0:g}",
                    "E" if it.is_active else "H",
                    str(recipe_count),
                    str(tx_count),
                    (last_tx.timestamp.strftime("%d.%m.%y") if last_tx else "—"),
                    str(lot_count),
                    str(sp_count),
                    str(dp_count),
                    (it.created_at.strftime("%d.%m.%y") if it.created_at else "—"),
                ]
                print("| " + " | ".join(row) + " |")
            print()
        if not any_row:
            print("_Şu an aktif ≥2 kart taşıyan küme yok — hepsi ya zaten tekleşmiş "
                  "ya da zombi temizliğiyle kapanmış._")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
