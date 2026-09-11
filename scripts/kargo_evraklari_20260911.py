"""Creator gönderimlerini aç, kargola ve teslim belgelerini üret — 11.09.2026.

NEDEN
─────
Dört creator'ın sözleşmesi ve adresi sistemde; paketler bugün çıkacak.  Elle
tek tek panelden açmak yerine tek komutta: gönderim kaydı + kargolama + PDF.

**Teslim belgesi yalnız KARGOLANDIKTAN sonra üretilir** (`routers/delivery.
delivery_document`: kargo tipinde `status != 'shipped'` → 403; belgede "eksiksiz
teslim alınmıştır" yazdığı için stok düşmeden basılması anlamsız).  Bu yüzden
akış zorunlu olarak şudur:

    gönderim aç (stok DÜŞMEZ) → kargola (stok DÜŞER) → belge üret

Takip numarası kargo şubesinden dönene kadar `TAKIP_NO` yer tutucusuyla girilir
(patronun Elvan'da elle yaptığı şeyin aynısı); numara gelince panelden
güncellenir.

NE YAPAR / NE YAPMAZ
────────────────────
✔ `build_delivery` + `ship_core` çekirdeklerini kullanır — YENİ STOK YOLU YOK,
  defter kuralı korunur (lot FIFO + lot başına `Transaction(Output)`).
✔ Zaten kargolanmış gönderimin belgesini yeniden üretir (Elvan).
✔ PDF'leri `OUT_DIR`'e okunur adlarla yazar.
✘ Stoğu doğrudan ELLEMEZ; düşüm yalnız `ship_core` içinden olur.
✘ Onaylanmamış ürün seti olan creator'ı ATLAR (`--hepsi` demedikçe).

İDEMPOTENT
──────────
Aktif gönderimi olan creator'a ikinci gönderim açılmaz; `status='shipped'` olan
tekrar kargolanmaz (ship_core 400 döner, yakalanır).  Belge her çalıştırmada
yeniden üretilir — zararsız.

KULLANIM (prod'da)
──────────────────
    venv/bin/python scripts/kargo_evraklari_20260911.py            # kuru
    venv/bin/python scripts/kargo_evraklari_20260911.py --commit   # yaz + PDF
    ... --commit --hepsi        # teyit bekleyenleri de dahil et
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.delivery_note import render_delivery_pdf                  # noqa: E402
from database import (Delivery, InfluencerCollab, InfluencerCreator,  # noqa: E402
                      InfluencerShipment, Item, SessionLocal, User)
from routers.delivery import (DeliveryCreate, DeliveryError,         # noqa: E402
                              DeliveryLine, _view, build_delivery, ship_core)

COMMIT = "--commit" in sys.argv
HEPSI = "--hepsi" in sys.argv
ACTOR = "sistem (kargo hazırlık 11.09.2026)"
TAKIP_YER_TUTUCU = "PAYLAŞILACAKTIR"
KARGO = "Yurtiçi"
OUT_DIR = Path(__file__).resolve().parent.parent / "_kargo_evraklari"

#: creator e-postası → (ürün id listesi, ürün seti onaylı mı)
SEVKIYAT = [
    ("elvanyagci@icloud.com",   [53, 58],        True),
    ("gulsahcolak95@outlook.com", [53, 294, 52], True),
    ("kckomur@yahoo.com",       [63, 301, 299],  True),
    # Patron notu "dark spot + HA" diyor ama başvuru formundaki şikâyeti
    # "cildim kuru, daha fazla neme ihtiyaç duyuyor" idi — set TEYİT BEKLİYOR.
    ("merven517@gmail.com",     [50, 294],       False),
]


def _safe(s: str) -> str:
    for a, b in (("ı", "i"), ("İ", "I"), ("ş", "s"), ("Ş", "S"), ("ğ", "g"),
                 ("Ğ", "G"), ("ü", "u"), ("Ü", "U"), ("ö", "o"), ("Ö", "O"),
                 ("ç", "c"), ("Ç", "C"), ("/", "-")):
        s = s.replace(a, b)
    return " ".join(s.split())


def main() -> int:
    db = SessionLocal()
    acilan = kargolanan = belge = atlanan = 0
    uyarilar = []
    if COMMIT:
        OUT_DIR.mkdir(exist_ok=True)
    try:
        owner = db.query(User).filter(User.username == "dogukan").first()
        print(f"Belge dizini: {OUT_DIR}")
        print(f"Takip no yer tutucusu: {TAKIP_YER_TUTUCU} · kargo: {KARGO}\n")

        for email, urun_ids, onayli in SEVKIYAT:
            cr = (db.query(InfluencerCreator)
                  .filter(InfluencerCreator.email == email).first())
            if not cr:
                print(f"✖ {email} — creator yok, atlandı\n")
                uyarilar.append(f"{email}: creator kaydı yok.")
                continue

            print(f"■ {cr.full_name}")
            if not onayli and not HEPSI:
                print("    ⏸ ÜRÜN SETİ ONAYLANMADI — atlandı (--hepsi ile dahil edilir)\n")
                atlanan += 1
                continue

            col = (db.query(InfluencerCollab)
                   .filter(InfluencerCollab.creator_id == cr.id,
                           InfluencerCollab.is_active == True).first())    # noqa: E712
            if not col:
                print("    ✖ iş birliği yok — atlandı\n")
                uyarilar.append(f"{cr.full_name}: iş birliği kaydı yok.")
                continue

            s = (db.query(InfluencerShipment)
                 .filter(InfluencerShipment.collab_id == col.id).first())

            # ── 1) Gönderim (stok DÜŞMEZ) ───────────────────────────────
            if s:
                d = db.query(Delivery).filter(Delivery.id == s.delivery_id).first()
                print(f"    · gönderim zaten var: {d.document_no} ({d.status})")
            else:
                satirlar, eksik = [], []
                for iid in urun_ids:
                    it = db.query(Item).filter(Item.id == iid).first()
                    if not it:
                        eksik.append(str(iid))
                        continue
                    print(f"    + id={iid:<4} {(it.name or '')[:48]:48} stok={float(it.current_stock or 0):g}")
                    satirlar.append(DeliveryLine(item_id=iid, quantity=1))
                if eksik:
                    print(f"    ✖ ürün bulunamadı: {', '.join(eksik)} — atlandı\n")
                    uyarilar.append(f"{cr.full_name}: ürün id {', '.join(eksik)} yok.")
                    continue
                if not COMMIT:
                    print("    ~ gönderim açılacak (kuru çalıştırma)\n")
                    acilan += 1
                    continue
                addr = cr.addresses[0] if getattr(cr, "addresses", None) else None
                from database import InfluencerAddress
                addr = (db.query(InfluencerAddress)
                        .filter(InfluencerAddress.creator_id == cr.id)
                        .order_by(InfluencerAddress.is_default.desc()).first())
                adres_txt = " ".join(x for x in [addr.line1, addr.line2, addr.district,
                                                 addr.city, addr.country] if x) if addr else ""
                from core.delivery_note import SAMPLE_NOTICE_TR
                not_ = f"Influencer gönderimi {col.code} | {SAMPLE_NOTICE_TR}"
                if adres_txt:
                    not_ += f" | Adres: {adres_txt}"
                data = DeliveryCreate(
                    recipient_name=(addr.recipient_name if addr else None) or cr.full_name,
                    recipient_phone=(addr.phone if addr else None) or cr.phone,
                    delivery_type="kargo", method="kargo", doc_lang="TR",
                    note=not_[:2000], items=satirlar)
                try:
                    d = build_delivery(db, data, ACTOR, "cosmetics")
                except DeliveryError as e:
                    db.rollback()
                    print(f"    ✖ gönderim açılamadı: {e.detail}\n")
                    uyarilar.append(f"{cr.full_name}: {e.detail}")
                    continue
                cogs = sum(float((db.query(Item).filter(Item.id == li.item_id).first().cost_price or 0))
                           * float(li.quantity) for li in d.items)
                s = InfluencerShipment(collab_id=col.id, delivery_id=d.id,
                                       address_id=addr.id if addr else None,
                                       cogs_total=round(cogs, 2), created_by=ACTOR)
                db.add(s)
                db.flush()
                print(f"    ✓ gönderim açıldı: {d.document_no}")
                acilan += 1

            # ── 2) Kargola (stok BURADA düşer) ──────────────────────────
            if COMMIT and d.status == "preparing":
                d_locked = (db.query(Delivery).filter(Delivery.id == d.id)
                            .with_for_update().first())
                try:
                    ship_core(db, d_locked, TAKIP_YER_TUTUCU, KARGO, ACTOR)
                    s.shipped_at = d_locked.shipped_at or datetime.utcnow()
                    col.stage = "kargoda"
                    col.stage_changed_at = datetime.utcnow()
                    db.flush()
                    print(f"    ✓ kargolandı — stok düştü")
                    kargolanan += 1
                    d = d_locked
                except DeliveryError as e:
                    db.rollback()
                    print(f"    ✖ kargolanamadı: {e.detail}\n")
                    uyarilar.append(f"{cr.full_name}: {e.detail}")
                    continue

            # ── 3) Teslim belgesi ───────────────────────────────────────
            if COMMIT:
                if d.status != "shipped":
                    print("    · belge üretilmedi (henüz kargolanmadı)")
                else:
                    pdf = render_delivery_pdf(_view(d))
                    ad = f"{d.document_no} - {_safe(cr.full_name)}.pdf"
                    (OUT_DIR / ad).write_bytes(pdf)
                    print(f"    ✓ belge: {ad}  ({len(pdf)/1024:.0f} KB)")
                    belge += 1
            print()

        if COMMIT:
            db.commit()
    except Exception as exc:                                   # pragma: no cover
        db.rollback()
        print(f"\n✖ HATA — geri alındı: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        db.close()

    print("═" * 76)
    print(f"Gönderim: {acilan} · kargolanan: {kargolanan} · belge: {belge} · "
          f"onay bekleyen (atlandı): {atlanan}")
    if uyarilar:
        print("\n⚠ UYARILAR:")
        for u in uyarilar:
            print(f"   · {u}")
    print(f"\nTakip numaraları geldiğinde panelden '{TAKIP_YER_TUTUCU}' "
          f"yerine gerçek numara yazılmalı.")
    print("\n✓ YAZILDI." if COMMIT else "\nKURU ÇALIŞTIRMA — yazmak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
