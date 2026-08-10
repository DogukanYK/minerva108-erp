"""Şahit numune kayıtlarının BOYUNU atar + lot sayaçlarını boy bazına indirir.

**Sorun:** 03.08.2026 el yazısı sayım listesinde ürün boyu yazmıyordu, bu yüzden
21 dolap kaydı SOYUT ana ürüne yazıldı (`items.parent_id IS NULL` ama alt
varyasyonu var).  Boy bilinmediği için hem dolap görünümü hem lot sayacı
bozuldu.

**Kaynak:** kullanıcının 10.08.2026'da gönderdiği fotoğraflar — aynı sayım
listesinin boy notu eklenmiş hâli (her satırın yanına "200mL / 500mL / 100mL"
yazılmış).

**Lot numarası BOY BAZINDADIR** — belgeden doğrulandı: aynı üretim tarihinde
500 ml ve 200 ml FARKLI numara almış (Fresh Shower Gel 28.04.2026 → 500 ml
EV002, 200 ml EV006; Purifying Anti-Acne 29.04.2026 → 500 ml EV001, 200 ml
EV007) ve her seri kendi içinde tarih sırasına uyuyor (500 ml: 16.03 → 28.04
→ 08.05 = EV001 → EV002 → EV003).  Yani `core/lots.py`'nin bugünkü `Item.id`
bazlı sayacı DOĞRU; tek sorun sayacın ana üründe birikmiş olması.

Yaptıkları — hepsi salt kayıt düzeltmesi, **stoğa ve Transaction'a DOKUNMAZ**:

  ① Boy ataması — kayıt doğru varyasyon `Item`'ına taşınır, `duzeltme`
     hareketi yazılır.  Adet dolapta aynı kalır; kazanç şu: bundan sonraki
     çıkış/imha DOĞRU boyun stoğundan düşer.
  ② Aynı (ürün, lot) ikinci kez oluşursa kayıtlar SİLİNMEZ/BİRLEŞTİRİLMEZ —
     `needs_review` işaretlenir.  Sayım listesinde bazı numuneler hem
     İngilizce hem Türkçe adla iki sayfada geçiyor; bunlar gerçekten iki ayrı
     numune mi yoksa çift kayıt mı, kullanıcı dolaba bakıp karar verecek.
     Sessizce birleştirmek dolap toplamını (220 adet) değiştirirdi.
  ③ Lot sayacı — her varyasyonun `Item.lot_seq`'i o varyasyona ait lot
     numaralarının en büyüğüne çekilir; ANA ürünlerin sayacı sıfırlanır
     (soyut ürün üretilmez).  Bu olmadan Evanira Kırmızı Yonca Leke Karşıtı
     Krem 100 ml için sistem EV001 önerirdi — lab EV008'de.

  ④ Kağıtta olup sistemde olmayan numune eklenir (06.08.2026 ek sayfası;
     o sayfadaki 8 kaydın 7'si girilmişti, tik almayan tek satır kalmıştı).

ÇÖZÜLEN ÇELİŞKİ: Evanira Kırmızı Yonca Leke Karşıtı Krem'in EV007/EV008
kayıtlarına belgede "200 mL" yazılmıştı, ama ürün 100/500 olarak var —
kullanıcı 10.08.2026'da "100 ml olacak" dedi, öyle atandı.

Kullanım:
    venv/bin/python scripts/assign_retention_variation.py            # kuru çalıştırma
    venv/bin/python scripts/assign_retention_variation.py --commit   # yaz
"""
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import (SessionLocal, Item, RetentionSample,          # noqa: E402
                      RetentionSampleMovement)
from core.brands import cabinet_of                                  # noqa: E402
from core.lots import lot_prefix, parse_sequence                    # noqa: E402

COMMIT = "--commit" in sys.argv
ACTOR = "sistem (boy ataması)"

# ── Fotoğraflardan okunan boy notları: {ana_urun_id: {lot: boy}} ──
ASSIGN = {
    1:  {"EV002": "500ml", "EV003": "500ml",                 # Bikini Bölgesi Losyon
         "EV006": "200ml", "EV007": "200ml"},
    4:  {"EV001": "500ml", "EV002": "500ml",                 # Fresh Duş Jeli
         "EV005": "200ml"},
    7:  {"EV001": "500ml", "EV002": "500ml",                 # Niasinamid Vücut Losyonu
         "EV007": "200ml"},
    10: {"EV001": "500ml",                                   # Arındırıcı Akne Karşıtı
         "EV006": "200ml", "EV007": "200ml"},
    # Kırmızı Yonca Leke Karşıtı — EV007/EV008'e belgede "200 mL" yazılmış ama
    # bu ürün 100/500 olarak var; kullanıcı 10.08.2026'da "100 ml olacak" dedi.
    13: {"EV002": "500ml", "EV006": "100ml",
         "EV007": "100ml", "EV008": "100ml"},
    16: {"EV003": "500ml",                                   # C Vitamini Işıltı Kremi
         "EV006": "100ml", "EV008": "100ml"},
    61: {"MNR001": "200ml"},                                 # Su Bazlı Saç Maskesi
}

CONFLICTS: dict = {}       # çözüldü — yukarıya bakınız

# ── 06.08.2026 ek sayfasında olup sisteme girilmemiş numune ──
# O sayfadaki 8 kaydın 7'si elle girilmiş (sayfadaki ✓ işaretleri tutuyor);
# tik almayan tek satır buydu.  Kağıt esas alınır (kullanıcı kararı).
MISSING = [
    {"item_id": 20, "lot": "SR005", "qty": 2.0,
     "produced": "24.07.2026", "until": "24.07.2028",
     "note": "06.08.2026 ek sayfası — sayımda atlanmış"},
]


def _norm(s) -> str:
    return re.sub(r"\s+", "", (s or "")).lower()


def main() -> int:
    db = SessionLocal()
    moved, dup_flagged, seq_rows, missing, added = [], [], [], [], []
    # Kuru çalıştırma sadakati: --commit yokken kayıtlar hâlâ ana üründe
    # duruyor.  ②/③ aşamaları bu plana bakar, DB'nin anlık hâline değil —
    # yoksa kuru çalıştırma taşımanın YARATACAĞI çift kayıtları ve sayaç
    # değişikliklerini göstermezdi.
    plan: dict = {}                       # sample_id → hedef item_id

    def eff(r) -> int:
        return plan.get(r.id, r.item_id)

    try:
        # ── ① Boy ataması ──
        for parent_id, lots in ASSIGN.items():
            parent = db.query(Item).filter(Item.id == parent_id).first()
            if not parent:
                missing.append(f"ana ürün id={parent_id} yok")
                continue
            variants = db.query(Item).filter(Item.parent_id == parent_id).all()
            by_size = {_norm(v.variation_name): v for v in variants}
            for lot, size in lots.items():
                tgt = by_size.get(_norm(size))
                if not tgt:
                    missing.append(f"id={parent_id} «{parent.name}» için {size} varyasyonu yok")
                    continue
                rows = (db.query(RetentionSample)
                        .filter(RetentionSample.item_id == parent_id,
                                RetentionSample.lot_number == lot,
                                RetentionSample.is_active == True).all())   # noqa: E712
                if not rows:
                    missing.append(f"id={parent_id} · {lot} dolapta bulunamadı")
                    continue
                for r in rows:
                    moved.append((r.id, parent.name, lot, r.quantity, size, tgt.id))
                    plan[r.id] = tgt.id
                    if COMMIT:
                        old = r.item_name or parent.name
                        r.item_id = tgt.id
                        r.item_name = tgt.name
                        db.add(RetentionSampleMovement(
                            sample_id=r.id, movement_type="duzeltme", quantity=0,
                            note=f"Boy atandı ({size}): {old} → {tgt.name}",
                            performed_by=ACTOR))
        if COMMIT:
            db.flush()

        # ── ①b Kağıtta olup sistemde olmayan numuneyi ekle ──
        for m in MISSING:
            it = db.query(Item).filter(Item.id == m["item_id"]).first()
            if not it:
                missing.append(f"eksik numune için ürün id={m['item_id']} yok")
                continue
            dup = (db.query(RetentionSample)
                   .filter(RetentionSample.item_id == it.id,
                           RetentionSample.lot_number == m["lot"],
                           RetentionSample.is_active == True).first())   # noqa: E712
            if dup:
                added.append((it.name, m["lot"], m["qty"], "zaten var — atlandı"))
                continue
            added.append((it.name, m["lot"], m["qty"], "eklendi"))
            if COMMIT:
                rs = RetentionSample(
                    item_id=it.id, item_name=it.name, lot_number=m["lot"],
                    brand=cabinet_of(it.name),
                    quantity=m["qty"], initial_quantity=m["qty"],
                    unit=it.unit or "adet",
                    produced_at=datetime.strptime(m["produced"], "%d.%m.%Y"),
                    retention_until=datetime.strptime(m["until"], "%d.%m.%Y").date(),
                    status="stored", source="sayim", placed_by="06.08.2026 sayfası",
                    note=m["note"], domain=it.domain or "cosmetics")
                db.add(rs)
                db.flush()
                db.add(RetentionSampleMovement(
                    sample_id=rs.id, movement_type="giris", quantity=m["qty"],
                    note=m["note"], performed_by=ACTOR))
                db.flush()

        # ── ② Aynı (ürün, lot) çift kaldıysa işaretle — SİLME/BİRLEŞTİRME YOK ──
        seen: dict = {}
        for r in (db.query(RetentionSample)
                  .filter(RetentionSample.is_active == True,             # noqa: E712
                          RetentionSample.status == "stored").all()):
            seen.setdefault((eff(r), r.lot_number), []).append(r)
        for (iid, lot), group in sorted(seen.items()):
            if len(group) < 2:
                continue
            it = db.query(Item).filter(Item.id == iid).first()
            dup_flagged.append((iid, it.name if it else "?", lot,
                                [g.id for g in group],
                                sum(g.quantity or 0 for g in group)))
            if COMMIT:
                for g in group:
                    g.needs_review = True

        # ── ③ Lot sayaçları: boy bazına indir, ana ürünleri sıfırla ──
        best: dict = {}
        for r in (db.query(RetentionSample)
                  .filter(RetentionSample.is_active == True).all()):      # noqa: E712
            it = db.query(Item).filter(Item.id == eff(r)).first()
            if not it:
                continue
            seq = parse_sequence(r.lot_number, lot_prefix(it))
            if seq:
                best[it.id] = max(best.get(it.id, 0), seq)
        # Kuru çalıştırmada ①b henüz yazmadı — eklenecek lotları da hesaba kat
        for m in MISSING:
            it = db.query(Item).filter(Item.id == m["item_id"]).first()
            seq = parse_sequence(m["lot"], lot_prefix(it)) if it else None
            if seq:
                best[it.id] = max(best.get(it.id, 0), seq)
        touched = (set(ASSIGN)
                   | {v.id for pid in ASSIGN for v in
                      db.query(Item).filter(Item.parent_id == pid).all()}
                   | {m["item_id"] for m in MISSING})
        for iid in sorted(touched):
            it = db.query(Item).filter(Item.id == iid).first()
            if not it:
                continue
            is_parent = db.query(Item.id).filter(Item.parent_id == iid).first() is not None
            want = 0 if is_parent else best.get(iid, 0)
            if (it.lot_seq or 0) != want:
                seq_rows.append((iid, it.name, it.lot_seq or 0, want, is_parent))
                if COMMIT:
                    it.lot_seq = want

        if COMMIT:
            db.commit()
    finally:
        db.close()

    print(f"=== ① BOY ATAMASI ({len(moved)} kayıt) ===")
    for sid, pname, lot, qty, size, tgt in moved:
        print(f"  #{sid:4} {pname[:40]:40} {lot:8} {qty:g} adet → {size} (id={tgt})")

    print(f"\n=== ①b KAĞITTA OLUP SİSTEMDE OLMAYAN ({len(added)}) ===")
    for name, lot, qty, how in added:
        print(f"  {name[:50]:50} {lot:8} {qty:g} adet — {how}")
    if not added:
        print("  (yok)")

    print(f"\n=== ② AYNI ÜRÜN+LOT ÇİFT KALDI ({len(dup_flagged)}) — teyit bayrağı ===")
    for iid, name, lot, sids, qty in dup_flagged:
        print(f"  id={iid:3} {name[:44]:44} {lot:8} kayıtlar={sids} toplam {qty:g} adet")
    if not dup_flagged:
        print("  (yok)")

    print(f"\n=== ③ LOT SAYAÇLARI ({len(seq_rows)}) ===")
    for iid, name, old, new, is_parent in seq_rows:
        tag = "  ← ANA ürün, sıfırlanır" if is_parent else ""
        print(f"  id={iid:3} {name[:44]:44} {old:3} → {new:3}{tag}")

    if CONFLICTS or missing:
        print(f"\n=== ATANMAYAN / UYARI ===")
        for pid, lots in CONFLICTS.items():
            for lot, why in lots.items():
                print(f"  ana id={pid} · {lot} — {why}")
        for m in missing:
            print(f"  ⚠ {m}")

    print()
    print("✓ YAZILDI — stok ve Transaction'a DOKUNULMADI." if COMMIT
          else "KURU ÇALIŞTIRMA — hiçbir şey yazılmadı.  Uygulamak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
