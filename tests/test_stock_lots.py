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


# ─── 16.10.2026: şahit / QC lotu korunur, raf sayımı, lot seçimi ────────────
#
# Serenida Niacinamide #32: "66 → 15 yanlış sayım" düzeltmesi FIFO ile SR002-S
# şahidini ve aynı sabah üretilen SR003'ü düştü.  Elle düzeltme ve satış artık
# şahit lotuna, düzeltme QC bekleyen lota da dokunmaz; lab rafı sayar.

def _witness(db, item, lot_number, qty, *, days_old=0, rs_qty=None, active=True,
             status="stored"):
    """`-S` lotu + ona bağlı şahit dolabı kaydı."""
    from database import RetentionSample
    inv = _lot(db, item, lot_number, qty, days_old=days_old, location="Şahit Numune Dolabı")
    db.add(RetentionSample(inventory_id=inv.id, item_id=item.id, item_name=item.name,
                           lot_number=lot_number, brand="Serenida",
                           quantity=qty if rs_qty is None else rs_qty,
                           initial_quantity=qty if rs_qty is None else rs_qty,
                           status=status, source="production", is_active=active,
                           domain="cosmetics"))
    db.flush()
    return inv


def _qc_lot(db, item, lot_number, qty, *, days_old=0):
    inv = _lot(db, item, lot_number, qty, days_old=days_old, location="Showroom")
    inv.qc_required = True
    db.flush()
    return inv


def _niacinamide(db):
    """Kart 16 = SR003 raf 8 + şahit (SR002-S 2, SR003-S 2) + lotsuz 4."""
    it = _item(db, "Serenida Niacinamide TEST", stock=16)
    sr002 = _lot(db, it, "SR002", 0, days_old=40, location="Showroom")
    sr002s = _witness(db, it, "SR002-S", 2, days_old=40)
    sr003 = _lot(db, it, "SR003", 8, days_old=8, location="Showroom")
    sr003s = _witness(db, it, "SR003-S", 2, days_old=8)
    db.commit()
    return it, {"SR002": sr002.id, "SR002-S": sr002s.id, "SR003": sr003.id, "SR003-S": sr003s.id}


def _qtys(db, item_id):
    db.expire_all()
    return {l.lot_number: l.quantity for l in
            db.query(Inventory).filter(Inventory.item_id == item_id)}


def _adj(client, **body):
    return client.post("/api/inventory/adjust", headers=ORIGIN, json=body)


def _adj_tx(db, item_id):
    return (db.query(Transaction)
            .filter(Transaction.item_id == item_id,
                    Transaction.transaction_type == "Adjustment").all())


def test_draw_down_skips_witness_and_qc_lots(db_session):
    it = _item(db_session, "ŞAHİTLİ DÜŞÜM", stock=20)
    _witness(db_session, it, "L1-S", 2, days_old=30)          # en eski — şahit
    _qc_lot(db_session, it, "L2", 6, days_old=20)            # QC bekliyor
    _lot(db_session, it, "L3", 5, days_old=10)               # raf
    db_session.commit()

    touched, uncovered = draw_down(db_session, it, 8)
    db_session.commit()
    assert touched == [("L3", 5)] and uncovered == 3
    assert _qtys(db_session, it.id) == {"L1-S": 2, "L2": 6, "L3": 0}


def test_consume_skips_witness_but_keeps_qc_pool(db_session):
    """Teslimat / Shopify şahit lotunu düşmez; artık lotsuz Output'tur."""
    it = _item(db_session, "ŞAHİTLİ SATIŞ", stock=10)
    _witness(db_session, it, "P1-S", 2, days_old=30)
    _qc_lot(db_session, it, "P2", 3, days_old=5)
    db_session.commit()

    used = consume(db_session, it, 4, note="Satış", actor="test")
    db_session.commit()
    assert used == [("P2", 3), ("—", 1)]
    assert _qtys(db_session, it.id) == {"P1-S": 2, "P2": 0}
    assert db_session.query(Item).get(it.id).current_stock == 6
    # B2B sevkiyatı (released_only) QC bekleyeni de almaz
    it2 = _item(db_session, "ŞAHİTLİ B2B", stock=10)
    _qc_lot(db_session, it2, "B1", 4)
    db_session.commit()
    assert consume(db_session, it2, 2, note="B2B", actor="t", released_only=True) == [("—", 2)]


def test_stock_breakdown_niacinamide(db_session):
    from core.stock_lots import stock_breakdown
    it, _ = _niacinamide(db_session)
    bd = stock_breakdown(db_session, it)
    assert (bd["shelf"], bd["witness"], bd["qc_pending"], bd["lotless"]) == (8, 4, 0, 4)
    assert bd["shelf_quantity"] == 12 and bd["lot_total"] == 12 and bd["lot_excess"] == 0
    assert bd["shelf"] + bd["witness"] + bd["qc_pending"] + bd["lotless"] == bd["card"]


def test_shelf_count_default_takes_lotless_first_then_oldest_shelf(db_session, authed_client):
    it, ids = _niacinamide(db_session)
    # Lab rafta 10 saydı → yeni kart = 10 + şahit 4 = 14 (−2, hepsi lotsuzdan)
    r = _adj(authed_client, item_id=it.id, new_shelf_quantity=10, reason="raf sayımı")
    assert r.status_code == 201, r.text
    assert r.json()["new_qty"] == 14 and r.json()["lot_changes"] == []
    assert _qtys(db_session, it.id) == {"SR002": 0, "SR002-S": 2, "SR003": 8, "SR003-S": 2}
    tx = _adj_tx(db_session, it.id)[-1]
    assert tx.quantity == -2 and tx.lot_number is None
    assert tx.notes.startswith("Stok düzeltme — Eski: 16.0 adet → Yeni: 14.0 adet (Δ -2.0)")
    assert "Raf sayımı: 10 adet (+ şahit 4)" in tx.notes and "Lotsuz stoktan: 2" in tx.notes

    # Rafta 6: kalan lotsuz 2 + en eski raf lotu SR003'ten 2 — şahitler sabit
    r = _adj(authed_client, item_id=it.id, new_shelf_quantity=6, reason="raf sayımı 2")
    assert r.status_code == 201, r.text
    assert _qtys(db_session, it.id) == {"SR002": 0, "SR002-S": 2, "SR003": 6, "SR003-S": 2}
    tx = _adj_tx(db_session, it.id)[-1]
    assert tx.quantity == -4 and "Lot düşümü: SR003×2" in tx.notes and "Lotsuz stoktan: 2" in tx.notes
    assert db_session.query(Item).get(it.id).current_stock == 10


def test_shelf_count_with_explicit_draw_from(db_session, authed_client):
    it, ids = _niacinamide(db_session)
    r = _adj(authed_client, item_id=it.id, new_shelf_quantity=9, reason="kırık şişe",
             draw_from=[{"inventory_id": ids["SR003"], "quantity": 3}])
    assert r.status_code == 201, r.text
    assert _qtys(db_session, it.id)["SR003"] == 5
    tx = _adj_tx(db_session, it.id)[-1]
    assert tx.lot_number == "SR003" and tx.quantity == -3        # tek lot → lot no dolu
    # Lotsuz + lot karışık seçim
    r = _adj(authed_client, item_id=it.id, new_shelf_quantity=6, reason="sayım",
             draw_from=[{"inventory_id": None, "quantity": 2}, {"inventory_id": ids["SR003"], "quantity": 1}])
    assert r.status_code == 201, r.text
    assert _qtys(db_session, it.id)["SR003"] == 4
    assert _adj_tx(db_session, it.id)[-1].lot_number is None


def test_draw_from_validation_rejects_and_writes_nothing(db_session, authed_client):
    it, ids = _niacinamide(db_session)
    other = _item(db_session, "BAŞKA KART", stock=5)
    foreign = _lot(db_session, other, "X1", 5)
    qc = _qc_lot(db_session, it, "SR004", 3)
    it.current_stock = 19                                  # kart QC lotunu da içersin
    db_session.commit()
    before = _qtys(db_session, it.id)
    cases = [
        ({"draw_from": [{"inventory_id": ids["SR003"], "quantity": 1}]}, "eşit olmalı"),
        ({"draw_from": [{"inventory_id": ids["SR003-S"], "quantity": 2}]}, "şahit"),
        ({"draw_from": [{"inventory_id": qc.id, "quantity": 2}]}, "QC"),
        ({"draw_from": [{"inventory_id": foreign.id, "quantity": 2}]}, "bu karta ait değil"),
        ({"draw_from": [{"inventory_id": None, "quantity": 2}, {"inventory_id": None, "quantity": 0.5}]},
         "iki kez"),
        ({"draw_from": [{"inventory_id": ids["SR002"], "quantity": 2}]}, "içinde 0"),
        ({"add_to_inventory_id": ids["SR003"]}, "düşülecek satırlar"),
    ]
    for extra, msg in cases:
        r = _adj(authed_client, item_id=it.id, new_shelf_quantity=10, reason="sayım", **extra)
        assert r.status_code == 400 and msg in r.json()["detail"], (extra, r.text)
    # lotsuz stok yalnız 4 — 5 düşülemez
    r = _adj(authed_client, item_id=it.id, new_shelf_quantity=7, reason="sayım",
             draw_from=[{"inventory_id": None, "quantity": 5}])
    assert r.status_code == 400 and "Lot kaydı olmayan stok 4" in r.json()["detail"]
    # İki mod birden / hiçbiri
    assert _adj(authed_client, item_id=it.id, new_shelf_quantity=7, new_quantity=7,
                reason="sayım").status_code == 400
    assert _adj(authed_client, item_id=it.id, reason="sayım").status_code == 400
    # Artışta düşülecek satır olmaz
    r = _adj(authed_client, item_id=it.id, new_shelf_quantity=20, reason="sayım",
             draw_from=[{"inventory_id": ids["SR003"], "quantity": 1}])
    assert r.status_code == 400
    assert _qtys(db_session, it.id) == before
    assert _adj_tx(db_session, it.id) == []
    assert db_session.query(Item).get(it.id).current_stock == 19


def test_positive_shelf_count_goes_to_selected_lot_or_lotless(db_session, authed_client):
    it, ids = _niacinamide(db_session)
    r = _adj(authed_client, item_id=it.id, new_shelf_quantity=15, reason="sayım fazlası",
             add_to_inventory_id=ids["SR003"])
    assert r.status_code == 201, r.text
    assert _qtys(db_session, it.id)["SR003"] == 11
    tx = _adj_tx(db_session, it.id)[-1]
    assert tx.quantity == 3 and tx.lot_number == "SR003" and "Lota eklendi: SR003×3" in tx.notes
    # Hedef yok → lotsuz; şahit lota hedef verilemez
    r = _adj(authed_client, item_id=it.id, new_shelf_quantity=16, reason="sayım fazlası")
    assert r.status_code == 201 and "Lotsuz stoğa: 1" in _adj_tx(db_session, it.id)[-1].notes
    assert _qtys(db_session, it.id)["SR003"] == 11
    r = _adj(authed_client, item_id=it.id, new_shelf_quantity=17, reason="sayım",
             add_to_inventory_id=ids["SR002-S"])
    assert r.status_code == 400 and "şahit" in r.json()["detail"]
    assert db_session.query(Item).get(it.id).current_stock == 20


def test_recent_production_asks_before_large_drop(db_session, authed_client):
    from database import ProductionHistory
    it = _item(db_session, "YENİ ÜRETİM", stock=66)
    _lot(db_session, it, "SR003", 50, location="Showroom")
    db_session.add(ProductionHistory(recipe_name="r", target_item_id=it.id, target_item_name=it.name,
                                     produced_quantity=50, produced_at=datetime.utcnow(),
                                     lot_number="SR003", domain="cosmetics"))
    db_session.commit()

    r = _adj(authed_client, item_id=it.id, new_quantity=15, reason="yanlış sayım")
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "recent_production" and body["production"]["produced_quantity"] == 50
    assert "50 adet üretim girildi" in body["detail"] and "Üretimi iptal et" in body["detail"]
    assert _adj_tx(db_session, it.id) == [] and _qtys(db_session, it.id) == {"SR003": 50}
    # Küçük düşüş (< yarısı) sormaz
    r = _adj(authed_client, item_id=it.id, new_quantity=60, reason="kırık")
    assert r.status_code == 201, r.text
    # Onaylı tekrar gönderim yazar (önce kalan lotsuz 10, sonra SR003 35)
    r = _adj(authed_client, item_id=it.id, new_quantity=15, reason="yanlış sayım",
             acknowledge_recent_production=True)
    assert r.status_code == 201, r.text
    assert _qtys(db_session, it.id) == {"SR003": 15}
    assert db_session.query(Item).get(it.id).current_stock == 15


def test_old_or_cancelled_production_does_not_ask(db_session, authed_client):
    from database import ProductionHistory
    it = _item(db_session, "ESKİ ÜRETİM", stock=60)
    db_session.add(ProductionHistory(recipe_name="r", target_item_id=it.id, produced_quantity=50,
                                     produced_at=datetime.utcnow() - timedelta(hours=49),
                                     domain="cosmetics"))
    db_session.add(ProductionHistory(recipe_name="r", target_item_id=it.id, produced_quantity=50,
                                     produced_at=datetime.utcnow(), cancelled_at=datetime.utcnow(),
                                     domain="cosmetics"))
    db_session.commit()
    assert _adj(authed_client, item_id=it.id, new_quantity=10, reason="sayım").status_code == 201


def test_undo_restores_lot_quantities(db_session, authed_client):
    it, ids = _niacinamide(db_session)
    r = _adj(authed_client, item_id=it.id, new_shelf_quantity=5, reason="sayım")   # −7: lotsuz 4 + SR003 3
    assert r.status_code == 201, r.text
    assert _qtys(db_session, it.id)["SR003"] == 5
    r = authed_client.post("/api/undo", headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert _qtys(db_session, it.id) == {"SR002": 0, "SR002-S": 2, "SR003": 8, "SR003-S": 2}
    assert db_session.query(Item).get(it.id).current_stock == 16
    assert _adj_tx(db_session, it.id) == []


def test_undo_refuses_when_lot_consumed_since(db_session, authed_client):
    it, ids = _niacinamide(db_session)
    r = _adj(authed_client, item_id=it.id, new_shelf_quantity=14, reason="fazla",
             add_to_inventory_id=ids["SR003"])                                  # +2 → SR003 10
    assert r.status_code == 201, r.text
    lot = db_session.query(Inventory).get(ids["SR003"])
    lot.quantity = 1                                       # arada başka yoldan tüketildi
    db_session.commit()
    r = authed_client.post("/api/undo", headers=ORIGIN)
    assert r.status_code == 409, r.text
    assert _qtys(db_session, it.id)["SR003"] == 1
    assert db_session.query(Item).get(it.id).current_stock == 18


def test_legacy_lot_number_applies_delta_not_total(db_session, authed_client):
    it = _item(db_session, "ESKİ LOT DALI", stock=10)
    _lot(db_session, it, "L-A", 6, days_old=5)
    _lot(db_session, it, "L-B", 4, days_old=1)
    db_session.commit()
    r = _adj(authed_client, item_id=it.id, new_quantity=8, lot_number="L-A", reason="kırık")
    assert r.status_code == 201, r.text
    assert _qtys(db_session, it.id) == {"L-A": 4, "L-B": 4}       # eskiden L-A = 8 yazılırdı
    tx = _adj_tx(db_session, it.id)[-1]
    assert tx.lot_number == "L-A" and tx.quantity == -2 and "| Lot: L-A" in tx.notes
    r = _adj(authed_client, item_id=it.id, new_quantity=1, lot_number="L-B", reason="kırık")
    assert r.status_code == 400 and "düşülemez" in r.json()["detail"]


def test_adjust_is_domain_scoped(db_session, authed_client):
    it = Item(name="TAKVİYE KART", category="Bitmiş Ürün", unit="adet", current_stock=5,
              domain="supplement")
    db_session.add(it)
    db_session.commit()
    r = _adj(authed_client, item_id=it.id, new_quantity=3, reason="sayım")
    assert r.status_code == 404
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 5
