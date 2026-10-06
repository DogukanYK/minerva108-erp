"""
Lotu başka karta taşı — POST /api/inventory/lots/{id}/move (06.10.2026).

Naturalya'nın numune lotları KRK GIDA / İPEDA kartlarına girilmişti; lab
lotları kendisi doğru tedarikçinin kartına taşıyacak (kullanıcı kararı).

Defter kuralı: normal lot Output/Input DEĞİL Adjustment çiftiyle taşınır
(aylık raporda sahte tüketim/alım olmasın); numune lotu deftersiz.  Kart
bazlı rekonstrüksiyon (`compute_stock_at`) iki tarafta da tutarlı kalmalı.
"""
import json
from datetime import datetime, timedelta

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from core.snapshots import compute_stock_at
from core.stock_lots import LOT_MOVE_MARK
from database import (AdminAuditLog, Inventory, Item, RetentionSample, SampleAnalysis,
                      SampleAnalysisIngredient, Supplier, Transaction, User)

_HDR = {"Origin": "http://testserver"}


def _sup(db, name):
    s = Supplier(name=name)
    db.add(s); db.flush()
    return s.id


def _card(db, name, stock=0.0, unit="ml", category="Hammadde", domain="cosmetics", active=True):
    it = Item(name=name, category=category, unit=unit, current_stock=stock, domain=domain,
              is_active=active)
    db.add(it); db.flush()
    return it


def _lot(db, item_id, lot, qty, *, supplier_id=None, is_sample=False, age_days=20, **kw):
    inv = Inventory(item_id=item_id, lot_number=lot, quantity=qty, status="APPROVED",
                    supplier_id=supplier_id, is_sample=is_sample, domain="cosmetics",
                    created_at=datetime.utcnow() - timedelta(days=age_days), **kw)
    db.add(inv); db.flush()
    return inv


def _move(c, inv_id, **body):
    body.setdefault("reason", "yanlış karta girilmiş")
    return c.post(f"/api/inventory/lots/{inv_id}/move", json=body, headers=_HDR)


def _fresh(db, model, pk):
    db.expire_all()
    return db.query(model).filter(model.id == pk).first()


def _tx(db, item_id):
    db.expire_all()
    return db.query(Transaction).filter(Transaction.item_id == item_id).order_by(Transaction.id).all()


def _login(client, db, username, role):
    db.add(User(username=username, full_name=username, role=role, is_active=True,
                password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode()))
    db.commit()
    r = client.post("/api/login", json={"username": username, "password": "minerva123"},
                    headers=_HDR)
    assert r.status_code == 200, r.text
    return client


# ─── Normal lot ─────────────────────────────────────────────────────────────

def test_full_move_writes_adjustment_pair_only(authed_client: TestClient, db_session: Session):
    nat = _sup(db_session, "NATURALYA")
    a = _card(db_session, "JOJOBA YAĞI", stock=5760, unit="g")
    b = _card(db_session, "JOJOBA YAĞI (NUMUNE)", stock=0, unit="gr")
    lot = _lot(db_session, a.id, "MİNERVA", 50, supplier_id=nat)
    db_session.commit()

    r = _move(authed_client, lot.id, target_item_id=b.id)
    assert r.status_code == 200, r.text
    assert r.json()["partial"] is False and r.json()["inventory_id"] == lot.id

    row = _fresh(db_session, Inventory, lot.id)
    assert (row.item_id, row.moved_from_item_id, row.quantity) == (b.id, a.id, 50)
    assert _fresh(db_session, Item, a.id).current_stock == 5710
    assert _fresh(db_session, Item, b.id).current_stock == 50
    out_tx, in_tx = _tx(db_session, a.id), _tx(db_session, b.id)
    assert [(t.transaction_type, t.quantity, t.lot_number) for t in out_tx] == [("Adjustment", -50, "MİNERVA")]
    assert [(t.transaction_type, t.quantity, t.lot_number) for t in in_tx] == [("Adjustment", 50, "MİNERVA")]
    assert out_tx[0].notes.startswith(LOT_MOVE_MARK) and in_tx[0].notes.startswith(LOT_MOVE_MARK)
    assert "Tedarikçi: NATURALYA" in out_tx[0].notes
    assert "Sebep: yanlış karta girilmiş" in in_tx[0].notes
    # çift sayım korumasına karışmasın
    assert not any(t.notes.startswith("Stok düzeltme") for t in out_tx + in_tx)

    audit = db_session.query(AdminAuditLog).filter(AdminAuditLog.action == "inventory.lot_move").one()
    det = json.loads(audit.details)
    assert (det["source_item_id"], det["target_item_id"], det["quantity"]) == (a.id, b.id, 50)


def test_partial_move_copies_lot_metadata(authed_client: TestClient, db_session: Session):
    s1 = _sup(db_session, "Tedarikçi 1")
    a = _card(db_session, "Kısmi Kaynak", stock=100)
    b = _card(db_session, "Kısmi Hedef", stock=7)
    lot = _lot(db_session, a.id, "P-1", 100, supplier_id=s1, age_days=90,
               expiry_date="2027-01-01", location="Raf 3", qc_form_data='{"q1": "ok"}',
               qc_notes="uygun", received_by="Songül")
    db_session.commit()
    created = _fresh(db_session, Inventory, lot.id).created_at

    r = _move(authed_client, lot.id, target_item_id=b.id, quantity=30)
    assert r.status_code == 200, r.text
    assert r.json()["partial"] is True
    assert _fresh(db_session, Inventory, lot.id).quantity == 70          # bu lottan (FIFO değil)
    new = _fresh(db_session, Inventory, r.json()["inventory_id"])
    assert new.item_id == b.id and new.quantity == 30 and new.lot_number == "P-1"
    assert (new.supplier_id, new.expiry_date, new.location) == (s1, "2027-01-01", "Raf 3")
    assert (new.qc_form_data, new.qc_notes, new.received_by) == ('{"q1": "ok"}', "uygun", "Songül")
    assert new.created_at == created and new.moved_from_item_id == a.id   # FIFO yaşı korunur
    assert _fresh(db_session, Item, a.id).current_stock == 70
    assert _fresh(db_session, Item, b.id).current_stock == 37


def test_supplier_override_and_moved_from_preserved(authed_client: TestClient, db_session: Session):
    krk, nat = _sup(db_session, "KRK GIDA"), _sup(db_session, "NATURALYA")
    a = _card(db_session, "A Kart", stock=10)
    b = _card(db_session, "B Kart")
    c = _card(db_session, "C Kart")
    lot = _lot(db_session, a.id, "X", 10, supplier_id=krk)
    db_session.commit()
    assert _move(authed_client, lot.id, target_item_id=b.id, supplier_id=nat).status_code == 200
    assert _move(authed_client, lot.id, target_item_id=c.id).status_code == 200
    row = _fresh(db_session, Inventory, lot.id)
    assert (row.item_id, row.supplier_id, row.moved_from_item_id) == (c.id, nat, a.id)


def test_reconstruction_consistent_on_both_cards(authed_client: TestClient, db_session: Session):
    a = _card(db_session, "Rekon A", stock=40)
    b = _card(db_session, "Rekon B", stock=5)
    lot = _lot(db_session, a.id, "R", 40)
    db_session.commit()
    eoms = [datetime.utcnow() - timedelta(days=d) for d in (40, 10)]
    before = [compute_stock_at(db_session, e)[0] for e in eoms]

    assert _move(authed_client, lot.id, target_item_id=b.id, quantity=15).status_code == 200
    db_session.expire_all()
    after = [compute_stock_at(db_session, e)[0] for e in eoms]
    for x, y in zip(before, after):
        assert (x[a.id], x[b.id]) == (y[a.id], y[b.id])                # geçmiş aylar değişmez
    now, _ = compute_stock_at(db_session, datetime.utcnow() + timedelta(days=1))
    assert (now[a.id], now[b.id]) == (25, 20)


# ─── Red yolları ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", ["unit", "inactive", "finished", "packaging", "same", "other_domain"])
def test_target_rules_reject(authed_client: TestClient, db_session: Session, case):
    a = _card(db_session, "Kaynak", stock=10)
    target = {
        "unit":         lambda: _card(db_session, "Gram", unit="g"),
        "inactive":     lambda: _card(db_session, "Pasif", active=False),
        "finished":     lambda: _card(db_session, "Krem", category="Bitmiş Ürün"),
        "packaging":    lambda: _card(db_session, "Şişe", category="Ambalaj"),
        "same":         lambda: a,
        "other_domain": lambda: _card(db_session, "Takviye", domain="supplement"),
    }[case]()
    lot = _lot(db_session, a.id, "L", 10)
    db_session.commit()
    r = _move(authed_client, lot.id, target_item_id=target.id)
    assert r.status_code == (404 if case == "other_domain" else 400), r.text
    assert _fresh(db_session, Inventory, lot.id).item_id == a.id
    assert db_session.query(Transaction).count() == 0


@pytest.mark.parametrize("case", ["qc", "retention", "too_much", "zero", "short_stock"])
def test_lot_rules_reject(authed_client: TestClient, db_session: Session, case):
    a = _card(db_session, "Kaynak", stock=3 if case == "short_stock" else 10)
    b = _card(db_session, "Hedef")
    lot = _lot(db_session, a.id, "L", 10)
    qty = None
    if case == "qc":
        lot.status = "QUARANTINE"
    elif case == "retention":
        db_session.add(RetentionSample(inventory_id=lot.id, item_id=a.id, item_name=a.name,
                                       lot_number="L", brand="Minerva", quantity=1,
                                       initial_quantity=1, unit="ml", domain="cosmetics"))
    elif case == "too_much":
        qty = 11
    elif case == "zero":
        qty = 0
    db_session.commit()
    body = {"target_item_id": b.id}
    if qty is not None:
        body["quantity"] = qty
    r = _move(authed_client, lot.id, **body)
    assert r.status_code == 400, r.text
    assert _fresh(db_session, Inventory, lot.id).item_id == a.id
    assert db_session.query(Transaction).count() == 0


def test_reason_required(authed_client: TestClient, db_session: Session):
    a, b = _card(db_session, "A", stock=1), _card(db_session, "B")
    lot = _lot(db_session, a.id, "L", 1)
    db_session.commit()
    assert _move(authed_client, lot.id, target_item_id=b.id, reason="").status_code == 422


# ─── Lot no çakışması ───────────────────────────────────────────────────────

def test_collision_409_then_lot_number(authed_client: TestClient, db_session: Session):
    s1, s2 = _sup(db_session, "S1"), _sup(db_session, "S2")
    a, b = _card(db_session, "A", stock=10), _card(db_session, "B", stock=4)
    lot = _lot(db_session, a.id, "L", 10, supplier_id=s1)
    _lot(db_session, b.id, "L", 4, supplier_id=s2)
    db_session.commit()
    r = _move(authed_client, lot.id, target_item_id=b.id)
    assert r.status_code == 409 and r.json()["code"] == "lot_collision"
    assert _fresh(db_session, Item, a.id).current_stock == 10           # hiçbir şey yazılmadı

    r = _move(authed_client, lot.id, target_item_id=b.id, lot_number="L-S1")
    assert r.status_code == 200, r.text
    row = _fresh(db_session, Inventory, lot.id)
    assert (row.item_id, row.lot_number) == (b.id, "L-S1")
    assert [t.lot_number for t in _tx(db_session, b.id)] == ["L-S1"]
    assert [t.lot_number for t in _tx(db_session, a.id)] == ["L"]


def test_compatible_twin_merges(authed_client: TestClient, db_session: Session):
    s1 = _sup(db_session, "S1")
    a, b = _card(db_session, "A", stock=10), _card(db_session, "B", stock=4)
    lot = _lot(db_session, a.id, "L", 10, supplier_id=s1)
    twin = _lot(db_session, b.id, "L", 4, supplier_id=None)
    db_session.commit()
    r = _move(authed_client, lot.id, target_item_id=b.id)
    assert r.status_code == 200, r.text
    assert r.json()["merged"] is True and r.json()["inventory_id"] == twin.id
    assert _fresh(db_session, Inventory, lot.id) is None
    t = _fresh(db_session, Inventory, twin.id)
    assert (t.quantity, t.supplier_id) == (14, s1)
    assert _fresh(db_session, Item, b.id).current_stock == 14


# ─── Numune lotu ────────────────────────────────────────────────────────────

def test_sample_move_no_ledger_and_redirects_analysis(authed_client: TestClient, db_session: Session):
    nat = _sup(db_session, "NATURALYA")
    a = _card(db_session, "JOJOBA YAĞI", stock=5710)
    b = _card(db_session, "JOJOBA YAĞI (NUMUNE)")
    lot = _lot(db_session, a.id, "MİNERVA", 50, supplier_id=nat, is_sample=True)
    sa = SampleAnalysis(document_no="NA-T-1", bulk_name="Deneme", properties="[]", domain="cosmetics")
    db_session.add(sa); db_session.flush()
    sai = SampleAnalysisIngredient(analysis_id=sa.id, item_id=a.id, item_name=a.name, source="sample",
                                   inventory_id=lot.id, quantity=5, consumed_qty=5)
    db_session.add(sai)
    db_session.commit()

    r = _move(authed_client, lot.id, target_item_id=b.id)
    assert r.status_code == 200, r.text
    row = _fresh(db_session, Inventory, lot.id)
    assert (row.item_id, row.is_sample, row.moved_from_item_id) == (b.id, True, a.id)
    assert db_session.query(Transaction).count() == 0
    assert _fresh(db_session, Item, a.id).current_stock == 5710
    assert _fresh(db_session, Item, b.id).current_stock == 0
    assert _fresh(db_session, SampleAnalysisIngredient, sai.id).item_id == b.id


def test_partial_sample_move_rejected(authed_client: TestClient, db_session: Session):
    a, b = _card(db_session, "A"), _card(db_session, "B")
    lot = _lot(db_session, a.id, "N", 50, is_sample=True)
    db_session.commit()
    assert _move(authed_client, lot.id, target_item_id=b.id, quantity=20).status_code == 400
    assert _fresh(db_session, Inventory, lot.id).item_id == a.id


# ─── RBAC ───────────────────────────────────────────────────────────────────

def test_labtech_moves_sample_but_not_stock_lot(labtech_client: TestClient, db_session: Session):
    """LabTech varsayılanı: inventory.receive VAR, inventory.adjust YOK."""
    a, b = _card(db_session, "A", stock=10), _card(db_session, "B")
    sample = _lot(db_session, a.id, "N", 5, is_sample=True)
    normal = _lot(db_session, a.id, "L", 10)
    db_session.commit()
    assert _move(labtech_client, sample.id, target_item_id=b.id).status_code == 200
    assert _move(labtech_client, normal.id, target_item_id=b.id).status_code == 403
    assert _fresh(db_session, Inventory, normal.id).item_id == a.id


def test_lablead_moves_stock_lot(client: TestClient, db_session: Session):
    a, b = _card(db_session, "A", stock=10), _card(db_session, "B")
    normal = _lot(db_session, a.id, "L", 10)
    db_session.commit()
    c = _login(client, db_session, "lab_sorumlu", "LabLead")
    assert _move(c, normal.id, target_item_id=b.id).status_code == 200


def test_staff_forbidden(client: TestClient, db_session: Session):
    a, b = _card(db_session, "A"), _card(db_session, "B")
    sample = _lot(db_session, a.id, "N", 5, is_sample=True)
    db_session.commit()
    c = _login(client, db_session, "rafci", "Staff")
    assert _move(c, sample.id, target_item_id=b.id).status_code == 403


# ─── Undo ───────────────────────────────────────────────────────────────────

def test_undo_receive_refuses_moved_lot(authed_client: TestClient, db_session: Session):
    a, b = _card(db_session, "Undo A"), _card(db_session, "Undo B")
    db_session.commit()
    r = authed_client.post("/api/inventory/receive", headers=_HDR,
                           json={"item_id": a.id, "lot_number": "U-1", "quantity": 10})
    assert r.status_code == 201, r.text
    lot = db_session.query(Inventory).filter(Inventory.lot_number == "U-1").one()
    assert _move(authed_client, lot.id, target_item_id=b.id).status_code == 200
    # A'nın stoğu başka yoldan eski değerine dönmüş olsa bile (stok kontrolü geçer)
    it = _fresh(db_session, Item, a.id)
    it.current_stock = 10
    db_session.commit()

    r = authed_client.post("/api/undo", headers=_HDR)
    assert r.status_code == 409, r.text
    row = _fresh(db_session, Inventory, lot.id)
    assert row is not None and row.item_id == b.id                    # hedefin lotu yerinde


# ─── İnceleme bulguları (06.10) ─────────────────────────────────────────────

def _post_raw(c, url, raw: str):
    """Ham JSON gövde — `NaN` json.dumps'la değil elle yazılır (Starlette kabul eder)."""
    return c.post(url, content=raw.encode(), headers={**_HDR, "Content-Type": "application/json"})


def test_nan_quantity_rejected_and_nothing_written(authed_client: TestClient, db_session: Session):
    """NaN her karşılaştırmada False döner: sınırları atlayıp iki karta NaN
    Adjustment yazıyor, yanıt 500 oluyordu (veri yine de yazılmış)."""
    a = _card(db_session, "NaN Kaynak", stock=50)
    b = _card(db_session, "NaN Hedef", stock=100)
    lot = _lot(db_session, a.id, "L-NAN", 50)
    db_session.commit()

    for bad in ("NaN", "Infinity", "-Infinity"):
        r = _post_raw(authed_client, f"/api/inventory/lots/{lot.id}/move",
                      f'{{"target_item_id": {b.id}, "quantity": {bad}, "reason": "deneme"}}')
        assert r.status_code == 400, (bad, r.text)
    assert _tx(db_session, a.id) == [] and _tx(db_session, b.id) == []
    assert _fresh(db_session, Item, a.id).current_stock == 50
    assert _fresh(db_session, Item, b.id).current_stock == 100
    assert _fresh(db_session, Inventory, lot.id).item_id == a.id

    # Çekirdek de kendi başına reddeder (router dışı çağıranlar için ikinci kapı)
    from core.stock_lots import LotMoveError, move_lot
    with pytest.raises(LotMoveError) as exc:
        move_lot(db_session, _fresh(db_session, Inventory, lot.id), _fresh(db_session, Item, a.id),
                 _fresh(db_session, Item, b.id), float("nan"), actor="t", reason="deneme")
    assert exc.value.status == 400


def test_nan_rejected_on_receive_and_adjust(authed_client: TestClient, db_session: Session):
    card = _card(db_session, "NaN Mal Kabul", stock=10)
    db_session.commit()
    for bad in ("NaN", "Infinity"):
        r = _post_raw(authed_client, "/api/inventory/receive",
                      f'{{"item_id": {card.id}, "lot_number": "L1", "quantity": {bad}}}')
        assert r.status_code == 400, r.text
        r = _post_raw(authed_client, "/api/inventory/adjust",
                      f'{{"item_id": {card.id}, "new_quantity": {bad}, "reason": "sayım"}}')
        assert r.status_code == 400, r.text
    assert db_session.query(Inventory).filter(Inventory.item_id == card.id).count() == 0
    assert _tx(db_session, card.id) == []
    assert _fresh(db_session, Item, card.id).current_stock == 10


def test_card_emptied_by_sample_move_is_soft_deleted(authed_client: TestClient, db_session: Session):
    """Numune yanlış (hareketsiz) karta girilmiş, Karta taşı ile doğru karta
    gitmiş: boş kalan kartı silmek FK ihlaliyle 500 veriyordu
    (inventory.moved_from_item_id).  Artık arşivlenir, lot izi korunur."""
    wrong = _card(db_session, "YANLIS KART (NUMUNE)", unit="g")
    right = _card(db_session, "DOGRU KART", unit="g")
    lot = _lot(db_session, wrong.id, "MİNERVA", 50, is_sample=True)
    db_session.commit()
    assert _move(authed_client, lot.id, target_item_id=right.id).status_code == 200

    r = authed_client.delete(f"/api/items/{wrong.id}", headers=_HDR)
    assert r.status_code == 200, r.text
    assert r.json()["soft_deleted"] is True
    gone = _fresh(db_session, Item, wrong.id)
    assert gone is not None and gone.is_active is False
    assert _fresh(db_session, Inventory, lot.id).moved_from_item_id == wrong.id


def test_bulk_delete_with_moved_from_card_soft_deletes(authed_client: TestClient, db_session: Session):
    wrong = _card(db_session, "TOPLU YANLIS", unit="g")
    right = _card(db_session, "TOPLU DOGRU", unit="g")
    spare = _card(db_session, "TOPLU BAĞSIZ", unit="g")
    lot = _lot(db_session, wrong.id, "L-T", 5, is_sample=True)
    db_session.commit()
    wrong_id, spare_id = wrong.id, spare.id
    assert _move(authed_client, lot.id, target_item_id=right.id).status_code == 200

    r = authed_client.post("/api/items/bulk-delete", json={"item_ids": [wrong_id, spare_id]},
                           headers=_HDR)
    assert r.status_code == 200, r.text
    assert _fresh(db_session, Item, wrong_id).is_active is False      # soft
    assert _fresh(db_session, Item, spare_id) is None                 # hard (bağsız)
