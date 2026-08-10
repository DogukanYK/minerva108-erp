"""07.08.2026'da açılan kopya bitmiş ürün kartlarını asıl ürünle birleştirir.

**Ne olmuştu:** kullanıcı stajyere "her ürünün TR ve İngilizce etiketli hâli ayrı
varyasyon olsun" dedi.  Bu sistemin kurgusuna ters — dil ayrımı ÜRÜNDE değil
ETİKETTE tutulur (`category='Ambalaj'`, `pkg_type='etiket'`, `language` +
`label_group`; üretim ekranı doğru dildeki kardeşi `label_group`'tan bulur).
Sonuç: id 689-720 arasında 32 kart açıldı, 30'u zaten var olan ürünün ikizi.
İkisi de aktif kaldığı için sistem aynı ürünü İKİ KERE sayıyor (ör. Yüz
Temizleme Jeli 36 + 36 = 72 görünüyordu).

**Stok kuralı (kullanıcıdan teyitli):** stajyer her karta o an ELİNDEKİ TOPLAMI
yazmış.  Yani yeni karttaki rakam 07.08 fiziksel sayımıdır, eski karttaki rakam
bayat sistem stoğudur.  Bir ürüne hem TR hem EN kartı açılmışsa ikisi
tamamlayıcıdır → toplanır.  Doğrulama: Vücut Peelingi 150ml → 11 (EN) + 5 (TR)
= 16 = eski kaydın rakamı.

**Sayılmamış kartlar** (Adjustment hareketi olmayan, stoğu 0 olan: 689, 715,
718, 720) hiç sayılmadı — boş kabuk.  Bunlar stoğa dahil EDİLMEZ.
`is_active=False` olan kartlar da hesaba katılmaz (stajyerin/kullanıcının
zaten eledikleri).

**Asıl kayıt = BARKODU olan kayıt.**  Pasifse yeniden aktifleştirilir; tescilli
ürün odur.  Kopyalar sıfırlanıp pasife çekilir — silinmez, çünkü Adjustment
hareketleri onlara bağlı (ledger korunur).

Her stok değişikliği `Adjustment` transaction'ı yazar — `core/snapshots.py`
yeniden kurulumu yalnız Input/Output/Adjustment'ı sayar, aksi hâlde geçmiş
raporlar bozulurdu.

Kullanım:
    venv/bin/python scripts/merge_duplicate_products.py            # kuru çalıştırma
    venv/bin/python scripts/merge_duplicate_products.py --commit   # yaz
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal, Item, Transaction                 # noqa: E402

COMMIT = "--commit" in sys.argv
ACTOR = "sistem (kopya birleştirme)"

# ── Elle doğrulanmış eşleme ──────────────────────────────────────────────
# kopya_id -> (asil_id | None, sayim_adedi | None)
# sayim_adedi None  = bu kart sayılmadı / pasif → stoğa KATILMAZ
# asil_id     None  = eşleşen ürün yok → kart olduğu gibi bırakılır
MERGE = {
    689: (293, None),   # boş kabuk, hareketi yok
    690: (60,     8),   # Betül 32 girdi, kullanıcı 8'e çekti
    691: (60,  None),   # 690'ın yazım ikizi, pasif
    692: (46,     7),   # TR
    693: (299, None),   # pasif
    694: (62,     1),
    695: (42,  None),   # pasif
    696: (302,    1),
    697: (549, None),   # pasif (716'nın ikizi)
    698: (22,     2),
    699: (33,     1),
    700: (26,     4),
    701: (28,     6),
    702: (21,     5),
    703: (31,    10),
    704: (27,     6),
    705: (24,     4),
    706: (36,     1),
    707: (None, None),  # "Body Lotion (200ml)" 22 = Lavanta 18 + Yasemin 4;
                        # koku ayrımı yok, mevcut bölünme zaten toplamı tutuyor
    708: (549,   11),   # EN
    709: (5,     12),
    710: (None, None),  # Evanira Cocoa Butter Cream — böyle bir ürün YOK (kullanıcı
                        # teyidi), GS1 listesinde de geçmiyor → sıfırla + pasife çek
    711: (46,    14),   # EN/etiketsiz
    712: (44,    15),
    713: (299,    8),   # EN
    714: (630,    1),
    715: (None, None),  # "Minerva 108 Body Scrub" — ANA ürün olarak korunur
    716: (549,    5),   # TR
    717: (45,    36),
    718: (None, None),  # Serenida Gece Kremi 100ml — GS1'de yalnız 50 ML var, stok 0
    719: (34,     3),
    720: (5,   None),   # boş kabuk, hareketi yok
}

# Kopya kabuğu ana ürün olarak kullanılan tek yer: Vücut Peelingi.
# 715 (ana) ← 549 (150ml) + 42 (350ml)
VARIATIONS = {549: (715, "150ml"), 42: (715, "350ml")}

KEEP_ACTIVE = {715}          # ana ürün kabuğu — pasife çekilmez
REACTIVATE = {46, 549}       # barkodu taşıyan asıl kayıtlar, pasif kalmışlar


def adjust(db, item, new_stock, note):
    delta = round(new_stock - (item.current_stock or 0), 4)
    if delta == 0:
        return 0
    if COMMIT:
        db.add(Transaction(item_id=item.id, transaction_type="Adjustment",
                           quantity=delta, timestamp=datetime.utcnow(),
                           notes=note, performed_by=ACTOR))
        item.current_stock = new_stock
    return delta


def main() -> int:
    db = SessionLocal()
    try:
        items = {i.id: i for i in db.query(Item).filter(
            Item.id.in_(set(MERGE) | {c for c, _ in MERGE.values() if c} | set(VARIATIONS))).all()}

        missing = [i for i in MERGE if i not in items]
        if missing:
            print(f"⛔ DURDURULDU — kopya kayıt bulunamadı: {missing}")
            return 2

        # ── asıl ürün başına yeni toplam ──
        totals = {}
        for dup_id, (main_id, cnt) in MERGE.items():
            if main_id and cnt is not None:
                totals.setdefault(main_id, []).append((dup_id, cnt))

        print("=== ① ASIL ÜRÜN STOKLARI ===")
        for main_id in sorted(totals):
            it = items[main_id]
            parts = totals[main_id]
            new = sum(c for _, c in parts)
            src = " + ".join(f"{c}(id{d})" for d, c in parts)
            old = it.current_stock or 0
            flag = "  ← DEĞİŞMİYOR" if new == old else ""
            print(f"  id={main_id:3} {it.name[:46]:46} {old:>5g} → {new:>5g}   [{src}]{flag}")
            adjust(db, it, float(new), f"07.08.2026 fiziksel sayımı — kopya kartlar birleştirildi ({src})")

        print("\n=== ② KOPYA KARTLAR SIFIRLANIP PASİFE ÇEKİLİYOR ===")
        for dup_id, (main_id, _) in sorted(MERGE.items()):
            it = items[dup_id]
            if dup_id in KEEP_ACTIVE:
                print(f"  id={dup_id:3} {it.name[:46]:46}  → ANA ÜRÜN olarak korunuyor")
                continue
            if main_id is None:
                print(f"  id={dup_id:3} {it.name[:46]:46}  → eşleşme yok, "
                      f"stok {it.current_stock:g}, pasife çekiliyor")
            else:
                print(f"  id={dup_id:3} {it.name[:46]:46}  → id{main_id}'e taşındı "
                      f"({it.current_stock:g} → 0)")
            adjust(db, it, 0.0, f"Kopya kart — stok id={main_id or '-'} kaydına taşındı")
            if COMMIT:
                it.is_active = False

        print("\n=== ③ YENİDEN AKTİFLEŞTİRİLEN ASIL KAYITLAR ===")
        for iid in sorted(REACTIVATE):
            it = items[iid]
            print(f"  id={iid:3} {it.name[:46]:46}  aktif={it.is_active} → True")
            if COMMIT:
                it.is_active = True

        print("\n=== ④ VARYASYON BAĞLARI (Vücut Peelingi) ===")
        for child, (parent, vname) in sorted(VARIATIONS.items()):
            it = items[child]
            print(f"  id={child:3} {it.name[:40]:40}  parent {it.parent_id or '-'} → {parent}"
                  f" · varyasyon '{it.variation_name or '-'}' → '{vname}'")
            if COMMIT:
                it.parent_id = parent
                it.variation_name = vname

        if COMMIT:
            db.flush()

        # ── ⑤ güvenlik: aynı barkod ≥2 AKTİF üründe olursa Shopify senkronu atlar ──
        print("\n=== ⑤ BARKOD ÇAKIŞMA KONTROLÜ (aktif ürünler) ===")
        seen = {}
        for it in db.query(Item).filter(Item.is_active == True,          # noqa: E712
                                        Item.barcode.isnot(None)).all():
            bc = (it.barcode or "").strip()
            if bc:
                seen.setdefault(bc, []).append(it.id)
        clash = {b: ids for b, ids in seen.items() if len(ids) > 1}
        if clash:
            print(f"  ⛔ {len(clash)} çakışma — Shopify bu ürünleri 'belirsiz' sayıp ATLAR:")
            for b, ids in clash.items():
                print(f"     {b} → {ids}")
            if COMMIT:
                db.rollback()
                print("\n  GERİ ALINDI — hiçbir şey yazılmadı.")
                return 3
        else:
            print("  ✓ çakışma yok")

        if COMMIT:
            db.commit()
    finally:
        db.close()

    print()
    print("✓ YAZILDI." if COMMIT
          else "KURU ÇALIŞTIRMA — hiçbir şey yazılmadı.  Uygulamak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
