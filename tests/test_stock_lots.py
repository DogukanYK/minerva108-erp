"""Lot farkındalıklı stok düşümü — `core/stock_lots.py` + bağlandığı yollar.

NEDEN (09.09.2026): `Item.current_stock` ile `Inventory` lot satırları iki ayrı
sayaçtı; teslimat / Shopify satışı / elle düzeltme yalnız birincisini hareket
ettiriyordu.  Sahada 25 bitmiş üründe 274 adetlik sapma birikmişti.

Bu dosya iki şeyi kilitler:
  1. Motorun kendisi (FIFO sırası, numune dışlama, lot yetmezse engellememe)
  2. Defter kuralı: yazılan Output'ların TOPLAMI daima düşülen miktara eşit —
     yani `compute_stock_at` rekonstrüksiyonu bozulmaz.
"""
from datetime import datetime, timedelta

from core.snapshots import compute_stock_at
from core.stock_lots import consume, draw_down, lot_summary, plan_fifo
from database import Inventory, Item, Transaction

ORIGIN = {"Origin": "http://testserver"}


def _item(db, name="TEST ÜRÜN", stock=0.0, unit="adet", category="Bitmiş Ürün"):
    it = Item(name=name, category=category, unit=unit,
              current_stock=stock, domain="cosmetics")
    db.add(it)
    db.flush()
    return it


def _lot(db, item, lot_number, qty, *, days_old=0, status="APPROVED",
         is_sample=False, location=None):
    inv = Inventory(
        item_id=item.id, lot_number=lot_number, quantity=qty, status=status,
        is_sample=is_sample, domain="cosmetics", location=location,
        created_at=datetime.utcnow() - timedelta(days=days_old),
    )
    db.add(inv)
    db.flush()
    return inv


# ─── Motor ──────────────────────────────────────────────────────────────────

def test_fifo_takes_oldest_lot_first(db_session):
    it = _item(db_session, "FIFO ÜRÜN", stock=30)
    eski = _lot(db_session, it, "L-ESKI", 10, days_old=30)
    yeni = _lot(db_session, it, "L-YENI", 20, days_old=1)
    db_session.commit()

    allocations, uncovered = plan_fifo(db_session, it, 15)
    assert uncovered == 0
    assert [(a.lot_number, q) for a, q in allocations] == [
        ("L-ESKI", 10), ("L-YENI", 5)]
    assert eski.id and yeni.id          # satırlar hâlâ canlı (plan yazmaz)
    assert eski.quantity == 10          # plan_fifo MUTASYON YAPMAZ


def test_consume_decrements_lots_stock_and_writes_output_per_lot(db_session):
    it = _item(db_session, "TÜKETİM ÜRÜNÜ", stock=30)
    _lot(db_session, it, "L-A", 10, days_old=5)
    _lot(db_session, it, "L-B", 20, days_old=1)
    db_session.commit()

    used = consume(db_session, it, 12, note="Teslimat testi", actor="test")
    db_session.commit()
    db_session.expire_all()

    assert used == [("L-A", 10), ("L-B", 2)]
    lots = {l.lot_number: l.quantity for l in
            db_session.query(Inventory).filter(Inventory.item_id == it.id)}
    assert lots == {"L-A": 0, "L-B": 18}
    assert db_session.query(Item).get(it.id).current_stock == 18

    txs = (db_session.query(Transaction)
           .filter(Transaction.item_id == it.id,
                   Transaction.transaction_type == "Output").all())
    assert len(txs) == 2                                  # lot başına ayrı satır
    assert sum(t.quantity for t in txs) == 12             # DEFTER: toplam = miktar
    assert {t.lot_number for t in txs} == {"L-A", "L-B"}   # izlenebilirlik


def test_consume_without_lots_still_decrements_stock(db_session):
    """Lot kaydı olmayan eski kalem teslimatı ENGELLEMEZ — artık lotsuz Output."""
    it = _item(db_session, "LOTSUZ ÜRÜN", stock=5)
    db_session.commit()

    used = consume(db_session, it, 3, note="Lotsuz", actor="test")
    db_session.commit()
    db_session.expire_all()

    assert used == [("—", 3)]
    assert db_session.query(Item).get(it.id).current_stock == 2
    tx = (db_session.query(Transaction)
          .filter(Transaction.item_id == it.id).one())
    assert tx.quantity == 3 and tx.lot_number is None


def test_consume_partial_lot_coverage_splits_output(db_session):
    it = _item(db_session, "KISMİ LOT", stock=10)
    _lot(db_session, it, "L-KISMI", 4)
    db_session.commit()

    used = consume(db_session, it, 10, note="Kısmi", actor="test")
    db_session.commit()

    assert used == [("L-KISMI", 4), ("—", 6)]
    txs = db_session.query(Transaction).filter(Transaction.item_id == it.id).all()
    assert sum(t.quantity for t in txs) == 10             # defter yine tam


def test_sample_lots_never_consumed(db_session):
    """Numune lotu satış/teslimata KONU OLAMAZ (24.08.2026 kuralı)."""
    it = _item(db_session, "NUMUNELİ ÜRÜN", stock=10)
    _lot(db_session, it, "L-NUMUNE", 8, days_old=30, is_sample=True)
    _lot(db_session, it, "L-NORMAL", 2, days_old=1)
    db_session.commit()

    used = consume(db_session, it, 5, note="Numune atlanmalı", actor="test")
    db_session.commit()
    db_session.expire_all()

    assert used == [("L-NORMAL", 2), ("—", 3)]
    numune = (db_session.query(Inventory)
              .filter(Inventory.lot_number == "L-NUMUNE").one())
    assert numune.quantity == 8                            # dokunulmadı


def test_pending_lots_not_consumed(db_session):
    it = _item(db_session, "QC BEKLEYEN", stock=10)
    _lot(db_session, it, "L-PENDING", 7, days_old=10, status="PENDING")
    _lot(db_session, it, "L-OK", 3, days_old=1)
    db_session.commit()

    allocations, uncovered = plan_fifo(db_session, it, 5)
    assert [(a.lot_number, q) for a, q in allocations] == [("L-OK", 3)]
    assert uncovered == 2


def test_draw_down_touches_lots_only(db_session):
    """Elle düzeltme yolu: lot düşer ama Transaction/stok ÇAĞIRANA ait."""
    it = _item(db_session, "SADECE LOT", stock=10)
    _lot(db_session, it, "L-X", 6)
    db_session.commit()

    touched, uncovered = draw_down(db_session, it, 4)
    db_session.commit()
    db_session.expire_all()

    assert touched == [("L-X", 4)] and uncovered == 0
    assert db_session.query(Inventory).filter(
        Inventory.lot_number == "L-X").one().quantity == 2
    assert db_session.query(Item).get(it.id).current_stock == 10   # dokunmadı
    assert db_session.query(Transaction).filter(
        Transaction.item_id == it.id).count() == 0                 # yazmadı


def test_lot_summary_format():
    assert lot_summary([("MNR006", 3), ("SR004", 1)]) == "MNR006×3 · SR004×1"
    assert lot_summary([]) == "—"


def test_ledger_reconstruction_unchanged_after_consume(db_session):
    """Lot başına ayrı Output yazmak rekonstrüksiyonu BOZMAZ."""
    it = _item(db_session, "DEFTER ÜRÜNÜ", stock=0)
    db_session.add(Transaction(item_id=it.id, transaction_type="Input",
                               quantity=20, timestamp=datetime.utcnow(),
                               performed_by="test"))
    it.current_stock = 20
    _lot(db_session, it, "L-1", 12, days_old=3)
    _lot(db_session, it, "L-2", 8, days_old=1)
    db_session.commit()

    consume(db_session, it, 15, note="Defter testi", actor="test")
    db_session.commit()
    db_session.expire_all()

    recon, _items = compute_stock_at(db_session, datetime.utcnow())
    assert recon.get(it.id) == 5
    assert db_session.query(Item).get(it.id).current_stock == 5


# ─── Bağlandığı yollar ──────────────────────────────────────────────────────

def test_delivery_decrements_lot(authed_client, db_session):
    """Teslimat artık lottan da düşer — asıl kapanan boşluk."""
    it = _item(db_session, "TESLİMAT LOTLU", stock=10)
    _lot(db_session, it, "PRD-TEST-001", 10)
    db_session.commit()

    r = authed_client.post("/api/delivery", headers=ORIGIN, json={
        "recipient_name": "Lot Testi", "delivery_type": "hediye",
        "items": [{"item_id": it.id, "quantity": 4}],
    })
    assert r.status_code in (200, 201), r.text
    db_session.expire_all()

    assert db_session.query(Item).get(it.id).current_stock == 6
    assert db_session.query(Inventory).filter(
        Inventory.lot_number == "PRD-TEST-001").one().quantity == 6
    tx = (db_session.query(Transaction)
          .filter(Transaction.item_id == it.id,
                  Transaction.transaction_type == "Output").one())
    assert tx.lot_number == "PRD-TEST-001"


def test_manual_negative_adjustment_draws_down_lot(db_session, authed_client):
    it = _item(db_session, "DÜZELTME LOTLU", stock=10)
    _lot(db_session, it, "L-ADJ", 10)
    db_session.commit()

    r = authed_client.post("/api/inventory/adjust", headers=ORIGIN, json={
        "item_id": it.id, "new_quantity": 6, "reason": "fiziksel sayım",
    })
    assert r.status_code in (200, 201), r.text
    db_session.expire_all()

    assert db_session.query(Item).get(it.id).current_stock == 6
    assert db_session.query(Inventory).filter(
        Inventory.lot_number == "L-ADJ").one().quantity == 6
    tx = (db_session.query(Transaction)
          .filter(Transaction.item_id == it.id,
                  Transaction.transaction_type == "Adjustment").one())
    assert tx.quantity == -4                       # TEK imzalı Adjustment
    assert "Lot düşümü" in (tx.notes or "")


def test_manual_positive_adjustment_leaves_lots_alone(db_session, authed_client):
    """'+' düzeltme hayalî lot AÇMAZ — stok lottan fazla kalabilir, bilinçli."""
    it = _item(db_session, "ARTI DÜZELTME", stock=2)
    _lot(db_session, it, "L-P", 2)
    db_session.commit()

    r = authed_client.post("/api/inventory/adjust", headers=ORIGIN, json={
        "item_id": it.id, "new_quantity": 9, "reason": "sayım fazlası",
    })
    assert r.status_code in (200, 201), r.text
    db_session.expire_all()

    assert db_session.query(Item).get(it.id).current_stock == 9
    assert db_session.query(Inventory).filter(
        Inventory.lot_number == "L-P").one().quantity == 2
