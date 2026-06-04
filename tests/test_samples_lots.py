"""
Faz 2 — Numune girişi + gerçek lot/tedarikçi bazlı üretim tüketimi testleri.

Kapsam:
  • Numune kabulü ayrı lot + tedarikçi + is_sample; normal lotla birleşmez
  • /api/inventory/samples  &  by-item (is_sample + supplier_name)
  • available-lots (APPROVED, qty>0)
  • Üretimde seçilen lottan düşüş + current_stock + kaynak lot izleme
  • Seçilen lot yetersizse NET HATA (mutasyon yok)
  • Lotsuz hammadde → eski davranış (aggregate) korunur
  • Seçim yokken FIFO (en eski lot)
"""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from database import Item, Inventory, Recipe, RecipeIngredient, Supplier, Transaction


def _supplier(db, name):
    s = Supplier(name=name)
    db.add(s); db.flush()
    return s.id


def _hammadde(db, name, stock):
    it = Item(name=name, sku=f"SKU-{name}", category="Hammadde", unit="ml", current_stock=stock)
    db.add(it); db.flush()
    return it


def _lot(db, item_id, lot, qty, supplier_id=None, is_sample=False, age_days=0):
    inv = Inventory(
        item_id=item_id, supplier_id=supplier_id, lot_number=lot, quantity=qty,
        status="APPROVED", is_sample=is_sample,
        created_at=datetime.utcnow() - timedelta(days=age_days),
    )
    db.add(inv); db.flush()
    return inv


def _recipe_one_ingredient(db, target_name, ing_item_id, net_qty):
    target = Item(name=target_name, sku=f"SKU-{target_name}", category="Bitmiş Ürün",
                  unit="adet", current_stock=0)
    db.add(target); db.flush()
    rec = Recipe(name=target_name, output_quantity=1, output_unit="adet",
                 target_item_id=target.id, waste_percentage=0)
    db.add(rec); db.flush()
    db.add(RecipeIngredient(recipe_id=rec.id, item_id=ing_item_id, quantity=net_qty, unit="ml"))
    db.commit()
    return rec.id


# ─── Numune girişi ──────────────────────────────────────────────────────────

def test_sample_receive_creates_separate_lot(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "Gliserin", 0); db_session.commit()
    sup = _supplier(db_session, "Numune Tedarikçi Y"); db_session.commit()
    r = authed_client.post("/api/inventory/receive", json={
        "item_id": it.id, "supplier_id": sup, "lot_number": "NUM-1",
        "quantity": 25, "is_sample": True,
    }, headers={"Origin": "http://testserver"})
    assert r.status_code == 201, r.text
    inv = db_session.query(Inventory).filter(Inventory.lot_number == "NUM-1").first()
    assert inv.is_sample is True
    assert inv.supplier_id == sup
    assert inv.location == "Numune"          # numune varsayılan konum
    db_session.refresh(it)
    assert it.current_stock == 25            # numune stoğa girer


def test_sample_does_not_merge_with_normal_lot(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "Setil Alkol", 0); db_session.commit()
    hdr = {"Origin": "http://testserver"}
    # Aynı lot no, biri normal biri numune → İKİ ayrı satır kalmalı
    authed_client.post("/api/inventory/receive", json={"item_id": it.id, "lot_number": "L9", "quantity": 10}, headers=hdr)
    authed_client.post("/api/inventory/receive", json={"item_id": it.id, "lot_number": "L9", "quantity": 5, "is_sample": True}, headers=hdr)
    rows = db_session.query(Inventory).filter(Inventory.item_id == it.id, Inventory.lot_number == "L9").all()
    assert len(rows) == 2
    assert {bool(r.is_sample) for r in rows} == {True, False}


def test_samples_endpoint_lists(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "Lanolin", 0); db_session.commit()
    sup = _supplier(db_session, "Alt Tedarikçi"); db_session.commit()
    authed_client.post("/api/inventory/receive", json={
        "item_id": it.id, "supplier_id": sup, "lot_number": "NUM-Z", "quantity": 12, "is_sample": True,
    }, headers={"Origin": "http://testserver"})
    r = authed_client.get("/api/inventory/samples")
    assert r.status_code == 200
    data = r.json()
    assert any(x["lot_number"] == "NUM-Z" and x["supplier_name"] == "Alt Tedarikçi" for x in data)


def test_available_lots_only_approved_positive(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "Su", 0)
    _lot(db_session, it.id, "A", 10)
    _lot(db_session, it.id, "B", 0)            # qty 0 → listelenmez
    z = _lot(db_session, it.id, "C", 5); z.status = "QUARANTINE"   # → listelenmez
    db_session.commit()
    r = authed_client.post("/api/inventory/available-lots", json={"item_ids": [it.id]},
                           headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    lots = r.json().get(str(it.id), [])
    assert [l["lot_number"] for l in lots] == ["A"]


# ─── Üretimde lot/tedarikçi tüketimi ────────────────────────────────────────

def test_production_consumes_chosen_lot(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "Yağ Bazı", 100)
    s1 = _supplier(db_session, "Tedarikçi Z"); s2 = _supplier(db_session, "Numune Y")
    a = _lot(db_session, it.id, "Z-LOT", 30, supplier_id=s1, age_days=10)
    b = _lot(db_session, it.id, "Y-NUM", 40, supplier_id=s2, is_sample=True, age_days=1)
    db_session.commit()
    rid = _recipe_one_ingredient(db_session, "Krem A", it.id, 10)   # 1 üretim = 10 ml

    r = authed_client.post("/api/production", json={
        "recipe_id": rid, "produced_quantity": 1,
        "ingredient_lot_choices": {str(it.id): b.id},     # numune lotu seç
    }, headers={"Origin": "http://testserver"})
    assert r.status_code == 201, r.text
    db_session.refresh(a); db_session.refresh(b); db_session.refresh(it)
    assert b.quantity == 30          # 40 − 10 (seçilen numune lotu düştü)
    assert a.quantity == 30          # diğer lot dokunulmadı
    assert it.current_stock == 90    # 100 − 10 (kaynak-of-truth)
    # Output transaction kaynak lotu taşımalı
    tx = (db_session.query(Transaction)
          .filter(Transaction.item_id == it.id, Transaction.transaction_type == "Output",
                  Transaction.lot_number == "Y-NUM").first())
    assert tx is not None and abs(tx.quantity - 10) < 1e-6


def test_production_chosen_lot_insufficient_errors(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "Esans", 100)
    small = _lot(db_session, it.id, "KÜÇÜK", 5)     # yetersiz
    db_session.commit()
    rid = _recipe_one_ingredient(db_session, "Krem B", it.id, 10)  # 10 ml gerek
    r = authed_client.post("/api/production", json={
        "recipe_id": rid, "produced_quantity": 1,
        "ingredient_lot_choices": {str(it.id): small.id},
    }, headers={"Origin": "http://testserver"})
    assert r.status_code == 400
    assert "yetersiz" in r.json()["detail"].lower()
    db_session.refresh(small); db_session.refresh(it)
    assert small.quantity == 5        # mutasyon YOK
    assert it.current_stock == 100    # mutasyon YOK


def test_production_no_lots_legacy_behavior(authed_client: TestClient, db_session: Session):
    # Hiç Inventory lotu olmayan hammadde — eski davranış: aggregate düşüş
    it = _hammadde(db_session, "Eski Hammadde", 50); db_session.commit()
    rid = _recipe_one_ingredient(db_session, "Krem C", it.id, 10)
    r = authed_client.post("/api/production", json={"recipe_id": rid, "produced_quantity": 1},
                           headers={"Origin": "http://testserver"})
    assert r.status_code == 201, r.text
    db_session.refresh(it)
    assert it.current_stock == 40     # 50 − 10
    tx = (db_session.query(Transaction)
          .filter(Transaction.item_id == it.id, Transaction.transaction_type == "Output").first())
    assert tx is not None and tx.lot_number is None   # lot kaydı yok


def test_production_fifo_when_no_choice(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "FIFO Bazı", 100)
    old = _lot(db_session, it.id, "ESKI", 30, age_days=20)
    new = _lot(db_session, it.id, "YENI", 40, age_days=1)
    db_session.commit()
    rid = _recipe_one_ingredient(db_session, "Krem D", it.id, 10)
    r = authed_client.post("/api/production", json={"recipe_id": rid, "produced_quantity": 1},
                           headers={"Origin": "http://testserver"})
    assert r.status_code == 201, r.text
    db_session.refresh(old); db_session.refresh(new)
    assert old.quantity == 20         # en eski lot düştü (FIFO)
    assert new.quantity == 40         # yeni lot dokunulmadı
