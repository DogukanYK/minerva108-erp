# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Aylık stok snapshot motoru — kayıt + hesaplama + backfill.

Mimari
──────
Aylık stok raporu iki kaynaktan beslenir:

  1. **Snapshot (kesin)** — `stock_snapshot` tablosunda dondurulmuş ay-sonu
     fotoğrafı.  Geçmiş aylar için tercih edilir; transaction silinse bile
     değişmez.
  2. **Rekonstrüksiyon (canlı)** — snapshot yoksa: `Item.current_stock`'tan
     başlayıp seçilen andan sonraki hareketleri geri sararak hesap.

Snapshot ne zaman alınır
────────────────────────
  • Her ayın 1'i 00:30 — core/scheduler.py bir önceki ayı dondurur.
  • Uygulama açılışında — `backfill_missing_snapshots()` veri olan ama
    snapshot'ı olmayan tüm tamamlanmış ayları idempotent doldurur.

Stok hesabı (compute_stock_at)
──────────────────────────────
  stok(eom) = Item.current_stock − Σ(eom'dan SONRAKİ signed hareket)

  `current_stock` source-of-truth'tur (Stoklar sayfası da onu gösterir).
  Geçmişte transaction'sız girilmiş açılış bakiyeleri sonucu bozmaz —
  kesin değerden geriye sarıyoruz, ileriye toplamıyoruz.
"""
import calendar
import datetime as _dt
import logging
from typing import Optional

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from database import Item, Transaction, StockSnapshot

logger = logging.getLogger("minerva108.snapshots")


# ── Zaman pencereleri ──────────────────────────────────────────────────────

def month_end(year: int, month: int) -> _dt.datetime:
    """Verilen yıl/ay için ayın son günü 23:59:59.999999."""
    last_day = calendar.monthrange(year, month)[1]
    return _dt.datetime(year, month, last_day, 23, 59, 59, 999999)


# ── Stok hesabı — tek doğru kaynak ─────────────────────────────────────────

def compute_stock_at(db: Session, eom: _dt.datetime, *item_filters) -> tuple[dict, list]:
    """
    `eom` anı itibarıyla her aktif item'ın stoğu.

    Döner: ({item_id: stock}, [Item, ...])
    `item_filters` Item satırına ek WHERE — kategori sınırlamak için.
    """
    item_rows = db.query(Item).filter(Item.is_active == True, *item_filters).all()
    if not item_rows:
        return {}, item_rows
    item_ids = [i.id for i in item_rows]

    signed = case(
        (Transaction.transaction_type == "Input",      Transaction.quantity),
        (Transaction.transaction_type == "Output",    -Transaction.quantity),
        (Transaction.transaction_type == "Adjustment", Transaction.quantity),
        else_=0,
    )
    after = dict(
        db.query(Transaction.item_id, func.coalesce(func.sum(signed), 0.0))
        .filter(Transaction.item_id.in_(item_ids),
                Transaction.timestamp > eom)
        .group_by(Transaction.item_id)
        .all()
    )
    result = {
        it.id: round(float(it.current_stock or 0.0) - float(after.get(it.id, 0.0)), 6)
        for it in item_rows
    }
    return result, item_rows


# ── Snapshot kayıt ─────────────────────────────────────────────────────────

def snapshot_exists(db: Session, year: int, month: int) -> bool:
    """Bu yıl/ay için dondurulmuş snapshot var mı?"""
    return db.query(StockSnapshot.id).filter(
        StockSnapshot.year == year, StockSnapshot.month == month
    ).first() is not None


def capture_month_snapshot(db: Session, year: int, month: int) -> int:
    """
    Verilen ayın stok durumunu `stock_snapshot` tablosuna dondurur.

    İdempotent: o yıl/ay için mevcut kayıtları siler, yeniden yazar.
    Ay sonu gelecekteyse kesim noktası 'şimdi' alınır.
    Döner: yazılan satır sayısı.  Caller commit eder.
    """
    eom = min(month_end(year, month), _dt.datetime.utcnow())
    stocks, items = compute_stock_at(db, eom)

    db.query(StockSnapshot).filter(
        StockSnapshot.year == year, StockSnapshot.month == month
    ).delete(synchronize_session=False)

    now = _dt.datetime.utcnow()
    written = 0
    for it in items:
        db.add(StockSnapshot(
            year=year, month=month,
            item_id=it.id,
            item_name=it.name,
            category=it.category,
            pkg_type=it.pkg_type,
            unit=it.unit,
            stock=stocks.get(it.id, 0.0),
            captured_at=now,
        ))
        written += 1
    return written


# ── Backfill — eksik geçmiş ayları doldur ──────────────────────────────────

def backfill_missing_snapshots(db: Session) -> list[str]:
    """
    Veri olan ama snapshot'ı olmayan tüm TAMAMLANMIŞ ayları doldurur.

    'Tamamlanmış ay' = içinde bulunulan aydan önceki aylar.  İçinde
    bulunulan ay henüz bitmediği için dondurulmaz (rapor onu canlı hesaplar).

    İdempotent — her açılışta güvenle çağrılabilir, var olanı atlar.
    Döner: doldurulan "YYYY-MM" listesi.
    """
    first_tx = db.query(func.min(Transaction.timestamp)).scalar()
    if not first_tx:
        return []   # hiç hareket yok — dondurulacak ay da yok

    now = _dt.datetime.utcnow()
    y, m = first_tx.year, first_tx.month
    filled: list[str] = []

    # first_tx ayından, içinde bulunulan aydan bir önceki aya kadar yürü
    while (y, m) < (now.year, now.month):
        if not snapshot_exists(db, y, m):
            try:
                n = capture_month_snapshot(db, y, m)
                db.commit()
                filled.append(f"{y}-{m:02d} ({n} kalem)")
            except Exception:
                db.rollback()
                logger.exception("backfill %d-%02d başarısız", y, m)
        # sonraki ay
        m += 1
        if m > 12:
            m = 1
            y += 1

    if filled:
        logger.info("Stok snapshot backfill: %s", ", ".join(filled))
    return filled


def capture_previous_month(db: Session) -> Optional[str]:
    """
    İçinde bulunulan aydan bir önceki ayı dondurur — aylık cron job'un işi.
    Döner: "YYYY-MM" ya da None.
    """
    now = _dt.datetime.utcnow()
    y, m = now.year, now.month - 1
    if m < 1:
        m = 12
        y -= 1
    try:
        n = capture_month_snapshot(db, y, m)
        db.commit()
        logger.info("Aylık snapshot alındı: %d-%02d (%d kalem)", y, m, n)
        return f"{y}-{m:02d}"
    except Exception:
        db.rollback()
        logger.exception("Aylık snapshot %d-%02d başarısız", y, m)
        return None
