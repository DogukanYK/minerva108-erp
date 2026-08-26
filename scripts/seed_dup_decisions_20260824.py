"""24.08.2026 kopya taramasının 21 kümesini karar tablosuna tohumlar (idempotent).

Kaynak: scripts/report_duplicate_decision_table.py çıktısı — stoklu/reçeteli
gerçek kopya adayları.  Patron karar veremeyeceğini söyledi ("ben bilemem");
kararı lab verir: Ürünler sayfasındaki popup bu kayıtları sorar, "birleştir"
kararı core/item_merge.merge_items ile anında uygulanır.

Zombi kartlar (24.08'de pasifleşen 16 + 573/574/581/582) kümelere DAHİL
EDİLMEDİ; pending_clusters zaten yalnız aktif kartları gösterir ve tek aktif
kart kalan kümeyi kendiliğinden kapatır.

Kullanım:
    venv/bin/python scripts/seed_dup_decisions_20260824.py            # kuru
    venv/bin/python scripts/seed_dup_decisions_20260824.py --commit   # yaz
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal, DuplicateItemDecision, Item      # noqa: E402

COMMIT = "--commit" in sys.argv

CLUSTERS = [
    ("lauryl-glucoside",  "LAURYL GLUCOSİDE",              [131, 632]),
    ("cinko-oksit",       "ÇİNKO OKSİT",                   [120, 633]),
    ("carnauba-wax",      "CARNAUBA WAX",                  [91, 634]),
    ("kakao-butter",      "KAKAO BUTTER",                  [128, 635]),
    ("xhantan-gum",       "XHANTAN GUM",                   [105, 636]),
    ("tatli-badem",       "TATLI BADEM YAĞI",              [182, 639]),
    ("d-panthenol",       "D-PANTHENOL",                   [66, 132]),
    ("badem-yagi",        "BADEM YAĞI",                    [156, 181]),
    ("hint-yagi",         "HİNT YAĞI",                     [164, 165]),
    ("susam-yagi",        "SUSAM YAĞI",                    [166, 272]),
    ("lavanta-hidrosolu", "LAVANTA HİDROSOLÜ",             [144, 162]),
    ("gul-hidrosolu",     "GÜL HİDROSOLÜ",                 [141, 255]),
    ("geven-ekstrakti",   "GEVEN EKSTRAKTI",               [231, 618]),
    ("kirmizi-yonca",     "KIRMIZI YONCA EKSTRAKT",        [138, 619]),
    ("ginseng",           "GİNSENG EKSTRAKTI",             [161, 240, 575, 728]),
    ("meyan-koku",        "MEYAN KÖKÜ EKSTRAKTI",          [261, 727]),
    ("japon-nanesi",      "JAPON NANESİ",                  [198, 725]),
    ("at-kuyrugu",        "AT KUYRUĞU EKSTRAKTI",          [243, 251]),
    ("biberiye",          "BİBERİYE",                      [84, 215, 245]),
    ("misk-adacayi",      "MİSK ADAÇAYI UÇUCU YAĞI",       [88, 225]),
    ("papatya",           "PAPATYA / ALMAN PAPATYASI",     [196, 216, 246, 732]),
]


def main() -> int:
    db = SessionLocal()
    added, skipped, warned = 0, 0, []
    try:
        for key, title, ids in CLUSTERS:
            if db.query(DuplicateItemDecision.id).filter(
                    DuplicateItemDecision.cluster_key == key).first():
                skipped += 1
                continue
            live = (db.query(Item)
                    .filter(Item.id.in_(ids), Item.is_active == True)   # noqa: E712
                    .count())
            if live < 2:
                warned.append(f"{key}: yalnız {live} aktif kart — tohumlanmadı")
                continue
            added += 1
            print(f"  + {key:20} {title:32} kartlar={ids}")
            if COMMIT:
                db.add(DuplicateItemDecision(
                    cluster_key=key, title=title,
                    item_ids=json.dumps(ids)))
        if COMMIT:
            db.commit()
    finally:
        db.close()

    print(f"\nEklendi: {added} · Zaten vardı: {skipped}")
    for w in warned:
        print(f"  ⚠ {w}")
    print("✓ YAZILDI." if COMMIT
          else "KURU ÇALIŞTIRMA — yazmak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
