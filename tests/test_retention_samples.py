"""
Şahit numune dolabı + üretim lot önerisi testleri.

Kritik sözleşmeler:
  • witness=0 → bugünkü davranış BİREBİR aynı (regresyon kilidi — bu özelliğin
    daha önce hiç testi yoktu)
  • witness>0 → `-S` Inventory lotu yazılmaya devam eder, `current_stock` TAM
    üretim kadar artar (dolaptaki numuneler satılabilir stokta sayılır) ve
    ayrıca bir RetentionSample yönetim kaydı oluşur
  • çıkış/imha → dolap + `Inventory` + `current_stock` düşer, `Output` yazılır
  • lot önerisi ürün bazlı sayaçla ilerler (MNR001 → MNR002), elle verilen lot
    aynı üründe kullanılmışsa 400 + öneri
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from core.brands import cabinet_of, lot_code
from core.lots import format_lot, normalize_lot, parse_sequence
from core.retention import add_months, expiry_state, retention_until
from database import (AdminAuditLog, Inventory, Item, ProductionHistory, Recipe,
                      RecipeIngredient, RetentionSample, RetentionSampleMovement,
                      Transaction)

_HDR = {"Origin": "http://testserver"}


def _recipe(db: Session, *, brand="Minerva 108", suffix="A", domain="cosmetics"):
    """Tek hammadde + tek ambalajlı basit reçete; hedef ürün bitmiş üründür."""
    raw = Item(name=f"{brand} Su {suffix}", sku=f"raw-{suffix}", category="Hammadde",
               unit="ml", current_stock=10_000, domain=domain)
    amb = Item(name=f"{brand} Şişe {suffix}", sku=f"amb-{suffix}", category="Ambalaj",
               unit="adet", current_stock=10_000, domain=domain)
    tgt = Item(name=f"{brand} Krem {suffix}", sku=f"tgt-{suffix}", category="Bitmiş Ürün",
               unit="adet", current_stock=0, domain=domain)
    db.add_all([raw, amb, tgt]); db.flush()
    rec = Recipe(name=tgt.name, output_quantity=1, output_unit="adet",
                 target_item_id=tgt.id, waste_percentage=0, domain=domain)
    db.add(rec); db.flush()
    db.add(RecipeIngredient(recipe_id=rec.id, item_id=raw.id, quantity=10, unit="ml"))
    db.add(RecipeIngredient(recipe_id=rec.id, item_id=amb.id, quantity=1, unit="adet"))
    db.commit()
    return rec, tgt


def _produce(client: TestClient, recipe_id: int, qty: float, witness=0, lot=None):
    body = {"recipe_id": recipe_id, "produced_quantity": qty, "witness_quantity": witness}
    if lot is not None:
        body["lot_number"] = lot
    return client.post("/api/production", json=body, headers=_HDR)


# ─── Saf birim: marka / lot / saklama ───────────────────────────────────────

def test_brand_and_lot_codes():
    assert cabinet_of("Minerva 108 Krem") == "Minerva 108"
    assert cabinet_of("MİNERVA Krem") == "Minerva 108"        # TR varyantı birleşir
    assert cabinet_of("Serenida Şampuan") == "Serenida"
    assert lot_code("Minerva 108 Krem") == "MNR"
    assert lot_code("Serenida Şampuan") == "SR"
    assert lot_code("Evanira Jel") == "EV"
    assert lot_code("GEVEN&BOR Sabun") == "GEV"               # tanımsız marka → 3 harf
    assert format_lot("MNR", 6) == "MNR006"
    assert parse_sequence("MNR006", "MNR") == 6
    assert parse_sequence("SR005", "MNR") is None             # önek uymuyor
    assert parse_sequence("PRD-20260731-134341", "MNR") is None
    assert normalize_lot("  mnr 006 ") == "MNR006"


def test_retention_until_and_state():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)   # ay sonu taşmaz
    assert retention_until(date(2026, 1, 15), 24, 6) == date(2028, 7, 15)
    today = date(2026, 8, 5)
    assert expiry_state(date(2026, 8, 4), today) == "expired"
    assert expiry_state(date(2026, 9, 1), today) == "due_soon"
    assert expiry_state(date(2030, 1, 1), today) == "ok"
    assert expiry_state(None, today) == "unknown"


# ─── Üretim: mevcut davranışın regresyon kilidi ─────────────────────────────

def test_witness_zero_keeps_legacy_behaviour(authed_client, db_session):
    """Şahit numune ayrılmazsa hiçbir şey değişmemeli — eski akış aynen."""
    rec, tgt = _recipe(db_session, suffix="Z")
    r = _produce(authed_client, rec.id, 10, witness=0)
    assert r.status_code == 201, r.text
    db_session.expire_all()
    assert db_session.query(Item).get(tgt.id).current_stock == 10
    assert db_session.query(RetentionSample).count() == 0
    lots_ = db_session.query(Inventory).filter(Inventory.item_id == tgt.id).all()
    assert len(lots_) == 1 and lots_[0].location == "Showroom"
    ins = (db_session.query(Transaction)
           .filter(Transaction.item_id == tgt.id,
                   Transaction.transaction_type == "Input").all())
    assert len(ins) == 1 and ins[0].quantity == 10


def test_witness_creates_cabinet_record_without_touching_stock(authed_client, db_session):
    """Şahit numune: -S lotu + dolap kaydı; satılabilir stok TAM üretim kadar artar."""
    rec, tgt = _recipe(db_session, suffix="W")
    r = _produce(authed_client, rec.id, 10, witness=3)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["witness_quantity"] == 3 and body["showroom_quantity"] == 7
    assert body["cabinet"] == "Minerva 108"

    db_session.expire_all()
    # Stok davranışı DEĞİŞMEDİ — dolaptakiler de stokta sayılır
    assert db_session.query(Item).get(tgt.id).current_stock == 10
    ins = (db_session.query(Transaction)
           .filter(Transaction.item_id == tgt.id,
                   Transaction.transaction_type == "Input").all())
    assert len(ins) == 1 and ins[0].quantity == 10

    lots_ = {i.location: i for i in db_session.query(Inventory)
             .filter(Inventory.item_id == tgt.id).all()}
    assert lots_["Showroom"].quantity == 7
    wit = lots_["Şahit Numune Dolabı — Minerva 108"]
    assert wit.quantity == 3 and wit.lot_number.endswith("-S")

    rs = db_session.query(RetentionSample).one()
    assert rs.quantity == 3 and rs.initial_quantity == 3
    assert rs.brand == "Minerva 108" and rs.inventory_id == wit.id
    assert rs.status == "stored" and rs.source == "production"
    assert rs.retention_until is not None
    mv = db_session.query(RetentionSampleMovement).one()
    assert mv.movement_type == "giris" and mv.quantity == 3
    # Üretim kaydında da kalıcı
    assert db_session.query(ProductionHistory).one().witness_quantity == 3


def test_witness_clamped_to_produced(authed_client, db_session):
    rec, tgt = _recipe(db_session, suffix="C")
    r = _produce(authed_client, rec.id, 2, witness=99)
    assert r.status_code == 201, r.text
    assert r.json()["witness_quantity"] == 2 and r.json()["showroom_quantity"] == 0
    db_session.expire_all()
    assert db_session.query(RetentionSample).one().quantity == 2


# ─── Lot numarası ───────────────────────────────────────────────────────────

def test_next_lot_suggestion_advances(authed_client, db_session):
    rec, tgt = _recipe(db_session, suffix="L")
    d = authed_client.get(f"/api/production/next-lot?recipe_id={rec.id}").json()
    assert d["lot_number"] == "MNR001" and d["last_lot"] is None
    assert "ilk üretim" in d["message"]

    assert _produce(authed_client, rec.id, 1).status_code == 201
    d2 = authed_client.get(f"/api/production/next-lot?recipe_id={rec.id}").json()
    assert d2["lot_number"] == "MNR002"
    assert d2["last_lot"] == "MNR001"
    assert "MNR001" in d2["message"] and "MNR002" in d2["message"]


def test_lot_counter_is_per_product(authed_client, db_session):
    """Her ürünün kendi sayacı var — aynı marka olsa bile."""
    rec_a, _ = _recipe(db_session, suffix="P1")
    rec_b, _ = _recipe(db_session, suffix="P2")
    assert _produce(authed_client, rec_a.id, 1).json()["lot_number"] == "MNR001"
    assert _produce(authed_client, rec_a.id, 1).json()["lot_number"] == "MNR002"
    # İkinci ürün kendi 001'inden başlar (aynı kod farklı üründe serbest)
    assert _produce(authed_client, rec_b.id, 1).json()["lot_number"] == "MNR001"


def test_manual_lot_accepted_and_counter_self_heals(authed_client, db_session):
    rec, tgt = _recipe(db_session, suffix="M")
    r = _produce(authed_client, rec.id, 1, lot="MNR020")
    assert r.status_code == 201 and r.json()["lot_number"] == "MNR020"
    # Sayaç elle verilen numaradan devam eder
    d = authed_client.get(f"/api/production/next-lot?recipe_id={rec.id}").json()
    assert d["lot_number"] == "MNR021"


def test_duplicate_lot_rejected_with_suggestion(authed_client, db_session):
    rec, tgt = _recipe(db_session, suffix="D")
    assert _produce(authed_client, rec.id, 1, lot="MNR007").status_code == 201
    r = _produce(authed_client, rec.id, 1, lot="MNR007")
    assert r.status_code == 400
    body = r.json()
    assert "zaten kullanılmış" in body["detail"]
    assert body["suggested_lot"] == "MNR008"
    db_session.expire_all()
    # Reddedilen üretim hiçbir şey yazmadı
    assert db_session.query(ProductionHistory).count() == 1


def test_same_lot_allowed_on_different_product(authed_client, db_session):
    """Lot kodu ürünü tanımlamaz — farklı üründe aynı kod serbest (labın düzeni)."""
    rec_a, _ = _recipe(db_session, suffix="S1")
    rec_b, _ = _recipe(db_session, suffix="S2")
    assert _produce(authed_client, rec_a.id, 1, lot="MNR050").status_code == 201
    assert _produce(authed_client, rec_b.id, 1, lot="MNR050").status_code == 201


def test_next_lot_requires_permission(labtech_client, db_session, authed_client):
    rec, _ = _recipe(db_session, suffix="R")
    # LabTech'in production.create yetkisi var → 200
    assert labtech_client.get(f"/api/production/next-lot?recipe_id={rec.id}").status_code == 200


# ─── Dolap: çıkış / imha / konum ────────────────────────────────────────────

def _make_sample(client, db, *, witness=5, suffix="X"):
    rec, tgt = _recipe(db, suffix=suffix)
    assert _produce(client, rec.id, 10, witness=witness).status_code == 201
    db.expire_all()
    return db.query(RetentionSample).order_by(RetentionSample.id.desc()).first(), tgt


def test_checkout_reduces_cabinet_and_stock(authed_client, db_session):
    rs, tgt = _make_sample(authed_client, db_session, suffix="CO")
    before = db_session.query(Item).get(tgt.id).current_stock
    r = authed_client.post(f"/api/retention/samples/{rs.id}/checkout",
                           json={"quantity": 2, "reason": "test", "note": "stabilite"},
                           headers=_HDR)
    assert r.status_code == 200, r.text
    db_session.expire_all()
    rs2 = db_session.query(RetentionSample).get(rs.id)
    assert rs2.quantity == 3 and rs2.status == "stored"
    # Dolaptan çıkan adet stoktan DA düşer (fiilen tüketildi)
    assert db_session.query(Item).get(tgt.id).current_stock == before - 2
    assert db_session.query(Inventory).get(rs.inventory_id).quantity == 3
    out = (db_session.query(Transaction)
           .filter(Transaction.item_id == tgt.id,
                   Transaction.transaction_type == "Output").all())
    assert len(out) == 1 and out[0].quantity == 2
    assert "Şahit numune çıkışı" in out[0].notes
    mvs = db_session.query(RetentionSampleMovement).filter_by(sample_id=rs.id).all()
    assert [m.movement_type for m in mvs] == ["giris", "cikis"]
    assert db_session.query(AdminAuditLog).filter(
        AdminAuditLog.action == "retention.checkout").count() == 1


def test_checkout_over_quantity_rejected_without_mutation(authed_client, db_session):
    rs, tgt = _make_sample(authed_client, db_session, suffix="OV")
    before = db_session.query(Item).get(tgt.id).current_stock
    r = authed_client.post(f"/api/retention/samples/{rs.id}/checkout",
                           json={"quantity": 99, "reason": "test"}, headers=_HDR)
    assert r.status_code == 400
    db_session.expire_all()
    assert db_session.query(RetentionSample).get(rs.id).quantity == 5
    assert db_session.query(Item).get(tgt.id).current_stock == before
    assert db_session.query(Transaction).filter(
        Transaction.item_id == tgt.id,
        Transaction.transaction_type == "Output").count() == 0


def test_checkout_all_marks_depleted(authed_client, db_session):
    rs, _ = _make_sample(authed_client, db_session, suffix="DP")
    r = authed_client.post(f"/api/retention/samples/{rs.id}/checkout",
                           json={"quantity": 5, "reason": "lab"}, headers=_HDR)
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.query(RetentionSample).get(rs.id).status == "depleted"
    # Tükenmiş numuneden tekrar çıkış yapılamaz
    r2 = authed_client.post(f"/api/retention/samples/{rs.id}/checkout",
                            json={"quantity": 1, "reason": "lab"}, headers=_HDR)
    assert r2.status_code == 400


def test_destroy_marks_and_reduces_stock(authed_client, db_session):
    rs, tgt = _make_sample(authed_client, db_session, suffix="DS")
    before = db_session.query(Item).get(tgt.id).current_stock
    r = authed_client.post(f"/api/retention/samples/{rs.id}/destroy",
                           json={"note": "saklama süresi doldu"}, headers=_HDR)
    assert r.status_code == 200, r.text
    db_session.expire_all()
    rs2 = db_session.query(RetentionSample).get(rs.id)
    assert rs2.status == "destroyed" and rs2.quantity == 0
    assert db_session.query(Item).get(tgt.id).current_stock == before - 5


def test_location_update_touches_no_stock(authed_client, db_session):
    rs, tgt = _make_sample(authed_client, db_session, suffix="LC")
    before = db_session.query(Item).get(tgt.id).current_stock
    r = authed_client.put(f"/api/retention/samples/{rs.id}",
                          json={"shelf": "2", "slot": "B", "note": "üst raf"},
                          headers=_HDR)
    assert r.status_code == 200
    assert r.json()["location_label"] == "Raf 2 · Göz B"
    db_session.expire_all()
    assert db_session.query(Item).get(tgt.id).current_stock == before
    assert db_session.query(Transaction).filter(
        Transaction.item_id == tgt.id,
        Transaction.transaction_type == "Output").count() == 0
    assert any(m.movement_type == "konum" for m in
               db_session.query(RetentionSampleMovement).filter_by(sample_id=rs.id))


# ─── Liste / filtre / dolap özeti ───────────────────────────────────────────

def test_cabinets_and_expired_filter(authed_client, db_session):
    rs, _ = _make_sample(authed_client, db_session, suffix="EX")
    cab = authed_client.get("/api/retention/cabinets").json()
    assert cab["totals"]["quantity"] == 5 and cab["totals"]["samples"] == 1
    assert cab["cabinets"][0]["brand"] == "Minerva 108"
    assert any(x["key"] == "test" for x in cab["reasons"])

    # Süresi dolmuş yap → expired filtresine düşer
    db_session.query(RetentionSample).filter(RetentionSample.id == rs.id).update(
        {"retention_until": date.today() - timedelta(days=1)})
    db_session.commit()
    rows = authed_client.get("/api/retention/samples?expired=1").json()["rows"]
    assert len(rows) == 1 and rows[0]["expiry_state"] == "expired"
    assert authed_client.get("/api/retention/cabinets").json()["totals"]["expired"] == 1


def test_search_and_brand_filter(authed_client, db_session):
    _make_sample(authed_client, db_session, suffix="F1")
    rec_b, _ = _recipe(db_session, brand="Serenida", suffix="F2")
    assert _produce(authed_client, rec_b.id, 4, witness=1).status_code == 201
    rows = authed_client.get("/api/retention/samples?brand=Serenida").json()["rows"]
    assert len(rows) == 1 and rows[0]["brand"] == "Serenida"
    rows2 = authed_client.get("/api/retention/samples?q=Krem F1").json()["rows"]
    assert len(rows2) == 1


def test_domain_isolation(authed_client, db_session):
    _make_sample(authed_client, db_session, suffix="DM")
    authed_client.post("/api/domain/switch", json={"domain": "supplement"}, headers=_HDR)
    assert authed_client.get("/api/retention/samples").json()["rows"] == []
    assert authed_client.get("/api/retention/cabinets").json()["totals"]["samples"] == 0


def test_detail_includes_movements(authed_client, db_session):
    rs, _ = _make_sample(authed_client, db_session, suffix="DT")
    d = authed_client.get(f"/api/retention/samples/{rs.id}").json()
    assert d["lot_number"] == rs.lot_number
    assert len(d["movements"]) == 1 and d["movements"][0]["type"] == "giris"
    assert authed_client.get("/api/retention/samples/999999").status_code == 404


# ─── RBAC ───────────────────────────────────────────────────────────────────

def test_rbac_labtech_can_checkout_not_destroy(client, authed_client, db_session):
    rs, _ = _make_sample(authed_client, db_session, suffix="RB")
    client.post("/api/login", json={"username": "meltem", "password": "minerva123"},
                headers=_HDR)                                   # LabTech'e geç
    assert client.get("/api/retention/samples").status_code == 200
    assert client.post(f"/api/retention/samples/{rs.id}/checkout",
                       json={"quantity": 1, "reason": "lab"}, headers=_HDR).status_code == 200
    assert client.post(f"/api/retention/samples/{rs.id}/destroy",
                       json={}, headers=_HDR).status_code == 403


def test_rbac_anonymous_blocked(client):
    assert client.get("/api/retention/samples").status_code in (401, 403)
    assert client.get("/sahit-numune", follow_redirects=False).status_code == 302


def test_page_renders(authed_client):
    r = authed_client.get("/sahit-numune")
    assert r.status_code == 200
    assert "Şahit Numune Dolabı" in r.text
    assert "/static/toast.js" in r.text
    assert r.text.count("<script") == r.text.count("</script>")


# ─── QC reddi ───────────────────────────────────────────────────────────────

def test_qc_rejection_closes_cabinet_record(authed_client, db_session):
    rs, tgt = _make_sample(authed_client, db_session, suffix="QC")
    r = authed_client.post(f"/api/qc/process/{rs.inventory_id}",
                           json={"status": "REJECTED", "notes": "kokusu bozuk"},
                           headers=_HDR)
    assert r.status_code == 200, r.text
    db_session.expire_all()
    rs2 = db_session.query(RetentionSample).get(rs.id)
    assert rs2.qc_status == "rejected" and rs2.status == "destroyed" and rs2.quantity == 0
    # Stok düşümü QC akışının kendi Adjustment'ı ile yapıldı — çift düşmemeli
    outs = db_session.query(Transaction).filter(
        Transaction.item_id == tgt.id,
        Transaction.transaction_type == "Output").count()
    assert outs == 0


# ─── Elle kayıt ─────────────────────────────────────────────────────────────

def test_manual_sample_creation(authed_client, db_session):
    rec, tgt = _recipe(db_session, suffix="MN")
    before = db_session.query(Item).get(tgt.id).current_stock
    r = authed_client.post("/api/retention/samples",
                           json={"item_id": tgt.id, "lot_number": "eski-001",
                                 "quantity": 4, "shelf": "1"}, headers=_HDR)
    assert r.status_code == 201, r.text
    assert r.json()["lot_number"] == "ESKI-001" and r.json()["brand"] == "Minerva 108"
    db_session.expire_all()
    # Elle kayıt stoğa DOKUNMAZ (adet zaten current_stock içinde)
    assert db_session.query(Item).get(tgt.id).current_stock == before
