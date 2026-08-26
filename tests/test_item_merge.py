"""Kopya kart birleştirme motoru + karar popup'ı API testleri.

Kritik sözleşmeler:
  • Birleştirme stok toplamını DEĞİŞTİRMEZ (kaybeden −X, kazanan +X — Adjustment
    çifti) ve her iki kartta da snapshot rekonstrüksiyonu tutarlı kalır
  • Transaction satırları taşınmaz/silinmez (defter kuralı)
  • Reçete kalemleri taşınır; aynı reçete iki kopyayı da içeriyorsa tek satıra
    toplanır
  • Reçete HEDEFİ olan kart birleştirilemez (kopya değil işareti)
  • 'keep' kararı hiçbir şeye dokunmaz; RBAC: items.edit ister (LabTech'te yok)
"""
import json
from datetime import datetime

from sqlalchemy.orm import Session

from core.item_merge import MergeError, merge_items, pending_clusters
from core.snapshots import compute_stock_at
from database import (DuplicateItemDecision, Inventory, Item, Recipe,
                      RecipeIngredient, SupplierPrice, Transaction)

_HDR = {"Origin": "http://testserver"}


def _mat(db: Session, name, stock=0.0, unit="g", **kw):
    it = Item(name=name, category="Hammadde", unit=unit,
              current_stock=stock, domain="cosmetics", **kw)
    db.add(it)
    db.flush()
    if stock:
        db.add(Transaction(item_id=it.id, transaction_type="Input",
                           quantity=stock, timestamp=datetime.utcnow(),
                           notes="test girişi", performed_by="test"))
    db.commit()
    return it


def _decision(db: Session, key, title, ids):
    row = DuplicateItemDecision(cluster_key=key, title=title,
                                item_ids=json.dumps(ids))
    db.add(row)
    db.commit()
    return row


# ─── Motor ──────────────────────────────────────────────────────────────────

def test_merge_moves_stock_with_adjustment_pair(db_session):
    a = _mat(db_session, "LAURYL GLUCOSIDE KOPYA", stock=1500)
    b = _mat(db_session, "LAURYL GLUCOSIDE ASIL", stock=200000)
    total_before = a.current_stock + b.current_stock

    summary = merge_items(db_session, a.id, b.id, "test")
    db_session.commit()
    db_session.expire_all()

    a2, b2 = db_session.query(Item).get(a.id), db_session.query(Item).get(b.id)
    assert a2.current_stock == 0 and a2.is_active is False
    assert b2.current_stock == total_before
    assert summary["moved_stock"] == 1500
    # Adjustment çifti yazıldı; Input geçmişi kaybeden kartta KALDI
    adj = (db_session.query(Transaction)
           .filter(Transaction.transaction_type == "Adjustment").all())
    assert sorted(t.quantity for t in adj) == [-1500, 1500]
    assert (db_session.query(Transaction)
            .filter(Transaction.item_id == a.id,
                    Transaction.transaction_type == "Input").count() == 1)
    # Defter tutarlılığı: rekonstrüksiyon == current_stock (kaybeden pasif —
    # aktif-item taramasına girmez; kazanan tam toplamı göstermeli)
    recon, _items = compute_stock_at(db_session, datetime.utcnow())
    assert recon.get(a.id, 0) == 0
    assert recon.get(b.id) == total_before


def test_merge_repoints_recipe_rows_and_sums_twins(db_session):
    a = _mat(db_session, "GLISERIN KOPYA")
    b = _mat(db_session, "GLISERIN ASIL")
    tgt = Item(name="Test Krem", category="Bitmiş Ürün", unit="adet",
               current_stock=0, domain="cosmetics")
    db_session.add(tgt); db_session.flush()
    r1 = Recipe(name="R1", output_quantity=1, target_item_id=tgt.id,
                domain="cosmetics")
    r2 = Recipe(name="R2", output_quantity=1, domain="cosmetics")
    db_session.add_all([r1, r2]); db_session.flush()
    # R1 iki kopyayı da içeriyor (aynı birim) → toplanmalı; R2 yalnız kaybedeni
    db_session.add_all([
        RecipeIngredient(recipe_id=r1.id, item_id=a.id, quantity=10, unit="g"),
        RecipeIngredient(recipe_id=r1.id, item_id=b.id, quantity=5, unit="g"),
        RecipeIngredient(recipe_id=r2.id, item_id=a.id, quantity=7, unit="g"),
    ])
    db_session.commit()

    merge_items(db_session, a.id, b.id, "test")
    db_session.commit()

    rows_r1 = (db_session.query(RecipeIngredient)
               .filter(RecipeIngredient.recipe_id == r1.id).all())
    assert len(rows_r1) == 1
    assert rows_r1[0].item_id == b.id and rows_r1[0].quantity == 15
    row_r2 = (db_session.query(RecipeIngredient)
              .filter(RecipeIngredient.recipe_id == r2.id).one())
    assert row_r2.item_id == b.id and row_r2.quantity == 7


def test_merge_moves_inventory_and_supplier_prices(db_session):
    a = _mat(db_session, "SHEA KOPYA", stock=100)
    b = _mat(db_session, "SHEA ASIL")
    db_session.add(Inventory(item_id=a.id, lot_number="L1", quantity=100,
                             status="APPROVED", domain="cosmetics"))
    db_session.add(SupplierPrice(item_id=a.id, supplier_name="X Kimya",
                                 unit_price=10, domain="cosmetics"))
    db_session.commit()

    merge_items(db_session, a.id, b.id, "test")
    db_session.commit()

    assert (db_session.query(Inventory)
            .filter(Inventory.item_id == b.id).count() == 1)
    assert (db_session.query(SupplierPrice)
            .filter(SupplierPrice.item_id == b.id).count() == 1)


def test_merge_refuses_recipe_target(db_session):
    a = _mat(db_session, "HEDEF URUN GIBI")
    b = _mat(db_session, "ASIL KART")
    db_session.add(Recipe(name="RT", output_quantity=1, target_item_id=a.id,
                          domain="cosmetics"))
    db_session.commit()
    try:
        merge_items(db_session, a.id, b.id, "test")
        assert False, "MergeError bekleniyordu"
    except MergeError as exc:
        assert "HEDEF" in str(exc) or "hedef" in str(exc).lower()
    db_session.rollback()
    db_session.expire_all()
    assert db_session.query(Item).get(a.id).is_active is True


def test_merge_coalesces_empty_fields(db_session):
    a = _mat(db_session, "KOPYA TR ADLI")
    a.name_tr = "BADEM YAĞI TR"
    a.min_stock_level = 500
    b = _mat(db_session, "ASIL TR ADSIZ")
    db_session.commit()

    merge_items(db_session, a.id, b.id, "test")
    db_session.commit()
    db_session.expire_all()
    b2 = db_session.query(Item).get(b.id)
    assert b2.name_tr == "BADEM YAĞI TR"
    assert b2.min_stock_level == 500


# ─── Küme görünümü ──────────────────────────────────────────────────────────

def test_pending_clusters_autocloses_single_card(db_session):
    a = _mat(db_session, "TEK AKTIF")
    b = _mat(db_session, "PASIF KART")
    b.is_active = False
    row = _decision(db_session, "tek-kart", "TEK", [a.id, b.id])

    out = pending_clusters(db_session)
    db_session.commit()
    assert out == []
    db_session.expire_all()
    assert db_session.query(DuplicateItemDecision).get(row.id).status == "kept"


def test_pending_clusters_returns_card_details(db_session):
    a = _mat(db_session, "KART A", stock=10)
    b = _mat(db_session, "KART B", stock=20)
    _decision(db_session, "ab", "A/B", [a.id, b.id])
    out = pending_clusters(db_session)
    assert len(out) == 1 and len(out[0]["cards"]) == 2
    card = next(c for c in out[0]["cards"] if c["id"] == b.id)
    assert card["current_stock"] == 20 and card["tx_count"] == 1


# ─── API ────────────────────────────────────────────────────────────────────

def test_api_decide_merge_and_keep(authed_client, db_session):
    a = _mat(db_session, "API KOPYA", stock=30)
    b = _mat(db_session, "API ASIL", stock=70)
    row = _decision(db_session, "api-m", "API M", [a.id, b.id])
    c = _mat(db_session, "API AYRI 1")
    d = _mat(db_session, "API AYRI 2")
    row2 = _decision(db_session, "api-k", "API K", [c.id, d.id])

    g = authed_client.get("/api/items/dup-decisions").json()
    assert g["count"] == 2

    r = authed_client.post(f"/api/items/dup-decisions/{row.id}",
                           json={"action": "merge", "target_item_id": b.id},
                           headers=_HDR)
    assert r.status_code == 200, r.text
    assert "birleştirildi" in r.json()["message"]
    db_session.expire_all()
    assert db_session.query(Item).get(b.id).current_stock == 100
    assert db_session.query(Item).get(a.id).is_active is False
    assert db_session.query(DuplicateItemDecision).get(row.id).status == "merged"

    r2 = authed_client.post(f"/api/items/dup-decisions/{row2.id}",
                            json={"action": "keep"}, headers=_HDR)
    assert r2.status_code == 200
    db_session.expire_all()
    assert db_session.query(DuplicateItemDecision).get(row2.id).status == "kept"
    assert db_session.query(Item).get(c.id).is_active is True

    # Kapanan karar tekrar karara bağlanamaz
    r3 = authed_client.post(f"/api/items/dup-decisions/{row.id}",
                            json={"action": "keep"}, headers=_HDR)
    assert r3.status_code == 404


def test_api_merge_rejects_foreign_target(authed_client, db_session):
    a = _mat(db_session, "YT KOPYA")
    b = _mat(db_session, "YT ASIL")
    other = _mat(db_session, "KUME DISI")
    row = _decision(db_session, "yt", "YT", [a.id, b.id])
    r = authed_client.post(f"/api/items/dup-decisions/{row.id}",
                           json={"action": "merge", "target_item_id": other.id},
                           headers=_HDR)
    assert r.status_code == 400
    db_session.expire_all()
    assert db_session.query(DuplicateItemDecision).get(row.id).status == "pending"


def test_api_rbac_labtech_403(client, db_session):
    a = _mat(db_session, "RB KOPYA")
    b = _mat(db_session, "RB ASIL")
    row = _decision(db_session, "rb", "RB", [a.id, b.id])
    client.post("/api/login", json={"username": "meltem", "password": "minerva123"},
                headers=_HDR)
    assert client.get("/api/items/dup-decisions").status_code == 403
    assert client.post(f"/api/items/dup-decisions/{row.id}",
                       json={"action": "keep"}, headers=_HDR).status_code == 403
