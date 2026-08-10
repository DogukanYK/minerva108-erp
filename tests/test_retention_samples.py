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
                      RecipeIngredient, RetentionSample, RetentionSampleCheck,
                      RetentionSampleMovement, Transaction, User)

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


# ═══ Bilgi girişi turu ═══════════════════════════════════════════════════════
# Kayıt silme ≠ imha · toplu konum · periyodik kontrol · teyit bayrağı

def _check_payload(**over):
    from core.retention import CHECK_ITEM_KEYS
    p = {"items": [{"key": k, "state": "normal", "note": ""} for k in CHECK_ITEM_KEYS],
         "result": "uygun", "result_note": "ilk kontrol"}
    p.update(over)
    return p


# ─── Kayıt silme: İMHADAN farkı (asıl regresyon kilidi) ─────────────────────

def test_record_delete_does_not_touch_stock(authed_client, db_session):
    """Yanlış girilen kaydı silmek stoğa DOKUNMAZ — imhadan farkı budur."""
    rec, tgt = _recipe(db_session, suffix="RD")
    r = authed_client.post("/api/retention/samples",
                           json={"item_id": tgt.id, "lot_number": "ELLE-001", "quantity": 3},
                           headers=_HDR)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    before = db_session.query(Item).get(tgt.id).current_stock
    tx_before = db_session.query(Transaction).filter(Transaction.item_id == tgt.id).count()

    d = authed_client.request("DELETE", f"/api/retention/samples/{sid}",
                              json={"reason": "yanlış ürüne girdim"}, headers=_HDR)
    assert d.status_code == 200, d.text
    db_session.expire_all()
    assert db_session.query(Item).get(tgt.id).current_stock == before      # stok SABİT
    assert db_session.query(Transaction).filter(
        Transaction.item_id == tgt.id).count() == tx_before                # yeni Transaction YOK
    row = db_session.query(RetentionSample).get(sid)
    assert row.is_active is False and row.quantity == 3                    # adet korunur, gizlenir
    # Her yerden gizlenmiş olmalı
    assert authed_client.get(f"/api/retention/samples/{sid}").status_code == 404
    assert all(x["id"] != sid for x in authed_client.get("/api/retention/samples").json()["rows"])
    assert authed_client.post(f"/api/retention/samples/{sid}/checkout",
                              json={"quantity": 1, "reason": "lab"}, headers=_HDR).status_code == 404


def test_destroy_still_reduces_stock(authed_client, db_session):
    """İmha DAVRANIŞI DEĞİŞMEDİ — silme eklendi diye imha bozulmamalı."""
    rs, tgt = _make_sample(authed_client, db_session, suffix="DS2")
    before = db_session.query(Item).get(tgt.id).current_stock
    r = authed_client.post(f"/api/retention/samples/{rs.id}/destroy",
                           json={"note": "süre doldu"}, headers=_HDR)
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.query(Item).get(tgt.id).current_stock == before - 5   # stok DÜŞTÜ
    assert db_session.query(Transaction).filter(
        Transaction.item_id == tgt.id,
        Transaction.transaction_type == "Output").count() == 1


def test_delete_blocked_for_production_source(authed_client, db_session):
    """Üretimden gelen kayıt silinemez — yolu İmha/QC reddidir."""
    rs, _ = _make_sample(authed_client, db_session, suffix="PS")
    assert rs.source == "production"
    d = authed_client.request("DELETE", f"/api/retention/samples/{rs.id}",
                              json={"reason": "olmadı"}, headers=_HDR)
    assert d.status_code == 400 and "Üretimden gelen" in d.json()["detail"]
    db_session.expire_all()
    assert db_session.query(RetentionSample).get(rs.id).is_active is True


def test_delete_blocked_after_checkout(authed_client, db_session):
    """Çıkış görmüş kayıt stok defterine yazılmıştır — gizlenemez."""
    rec, tgt = _recipe(db_session, suffix="DC")
    sid = authed_client.post("/api/retention/samples",
                             json={"item_id": tgt.id, "lot_number": "ELLE-002", "quantity": 4},
                             headers=_HDR).json()["id"]
    assert authed_client.post(f"/api/retention/samples/{sid}/checkout",
                              json={"quantity": 1, "reason": "lab"}, headers=_HDR).status_code == 200
    d = authed_client.request("DELETE", f"/api/retention/samples/{sid}",
                              json={"reason": "silinsin"}, headers=_HDR)
    assert d.status_code == 400 and "çıkış/imha" in d.json()["detail"]
    db_session.expire_all()
    assert db_session.query(RetentionSample).get(sid).is_active is True


def test_delete_rbac(client, authed_client, db_session):
    """Silme `retention.destroy` ister — LabTech'te kapalı."""
    rec, tgt = _recipe(db_session, suffix="DR")
    sid = authed_client.post("/api/retention/samples",
                             json={"item_id": tgt.id, "lot_number": "ELLE-003", "quantity": 2},
                             headers=_HDR).json()["id"]
    client.post("/api/login", json={"username": "meltem", "password": "minerva123"}, headers=_HDR)
    assert client.request("DELETE", f"/api/retention/samples/{sid}",
                          json={"reason": "dene"}, headers=_HDR).status_code == 403


# ─── Toplu konum ────────────────────────────────────────────────────────────

def test_bulk_location_updates_many(authed_client, db_session):
    ids = []
    for i in range(3):
        rec, tgt = _recipe(db_session, suffix=f"BL{i}")
        ids.append(authed_client.post("/api/retention/samples",
                                      json={"item_id": tgt.id, "lot_number": f"BL{i}-1",
                                            "quantity": 2}, headers=_HDR).json()["id"])
    r = authed_client.put("/api/retention/samples/bulk-location", headers=_HDR,
                          json={"rows": [{"id": ids[0], "shelf": "1", "slot": "A"},
                                         {"id": ids[1], "shelf": "1", "slot": "B"},
                                         {"id": ids[2], "shelf": "2", "slot": ""}]})
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 3 and r.json()["skipped"] == 0
    db_session.expire_all()
    rows = {i: db_session.query(RetentionSample).get(i) for i in ids}
    assert (rows[ids[0]].shelf, rows[ids[0]].slot) == ("1", "A")
    assert (rows[ids[2]].shelf, rows[ids[2]].slot) == ("2", None)   # "" → temizle
    # Değişen her satıra bir 'konum' hareketi
    for i in ids:
        assert sum(1 for m in db_session.query(RetentionSample).get(i).movements
                   if m.movement_type == "konum") == 1

    # İkinci kez AYNI değerlerle → hiçbir şey değişmedi, hareket yazılmadı
    r2 = authed_client.put("/api/retention/samples/bulk-location", headers=_HDR,
                           json={"rows": [{"id": ids[0], "shelf": "1", "slot": "A"}]})
    assert r2.json()["updated"] == 0
    db_session.expire_all()
    assert sum(1 for m in db_session.query(RetentionSample).get(ids[0]).movements
               if m.movement_type == "konum") == 1


def test_bulk_location_does_not_touch_stock(authed_client, db_session):
    rs, tgt = _make_sample(authed_client, db_session, suffix="BS")
    before = db_session.query(Item).get(tgt.id).current_stock
    tx_before = db_session.query(Transaction).count()
    assert authed_client.put("/api/retention/samples/bulk-location", headers=_HDR,
                             json={"rows": [{"id": rs.id, "shelf": "9"}]}).status_code == 200
    db_session.expire_all()
    assert db_session.query(Item).get(tgt.id).current_stock == before
    assert db_session.query(Transaction).count() == tx_before


def test_bulk_location_domain_isolated(authed_client, db_session):
    """Başka panelin numunesine yazılamaz — sessizce atlanır."""
    rs, _ = _make_sample(authed_client, db_session, suffix="BD")
    authed_client.post("/api/domain/switch", json={"domain": "supplement"}, headers=_HDR)
    r = authed_client.put("/api/retention/samples/bulk-location", headers=_HDR,
                          json={"rows": [{"id": rs.id, "shelf": "X"}]})
    assert r.status_code == 200
    assert r.json()["updated"] == 0 and r.json()["skipped"] == 1
    db_session.expire_all()
    assert db_session.query(RetentionSample).get(rs.id).shelf is None      # DOKUNULMADI


def test_partial_put_does_not_wipe_slot(authed_client, db_session):
    """Yalnız `shelf` gönderen istek `slot`'u SİLMEMELİ (eski hatanın kilidi)."""
    rs, _ = _make_sample(authed_client, db_session, suffix="PW")
    authed_client.put(f"/api/retention/samples/{rs.id}",
                      json={"shelf": "3", "slot": "C"}, headers=_HDR)
    db_session.expire_all()
    assert (db_session.query(RetentionSample).get(rs.id).shelf,
            db_session.query(RetentionSample).get(rs.id).slot) == ("3", "C")
    # Sadece shelf gönder → slot korunmalı
    authed_client.put(f"/api/retention/samples/{rs.id}", json={"shelf": "4"}, headers=_HDR)
    db_session.expire_all()
    row = db_session.query(RetentionSample).get(rs.id)
    assert row.shelf == "4" and row.slot == "C", "slot silindi — kısmi güncelleme hatası"
    # Açıkça boş gönderilirse TEMİZLENİR
    authed_client.put(f"/api/retention/samples/{rs.id}", json={"slot": ""}, headers=_HDR)
    db_session.expire_all()
    assert db_session.query(RetentionSample).get(rs.id).slot is None


# ─── Periyodik kontrol ──────────────────────────────────────────────────────

def test_check_roundtrip_preserves_observations(authed_client, db_session):
    """Gözlemler birebir geri okunmalı — Türkçe karakter bozulmadan."""
    rs, _ = _make_sample(authed_client, db_session, suffix="CK")
    payload = _check_payload(result="uygun_degil", result_note="hafif renk değişimi")
    payload["items"][2] = {"key": "renk", "state": "degisim", "note": "sarıya çalıyor"}
    r = authed_client.post(f"/api/retention/samples/{rs.id}/checks",
                           json=payload, headers=_HDR)
    assert r.status_code == 201, r.text
    d = authed_client.get(f"/api/retention/samples/{rs.id}").json()
    assert len(d["checks"]) == 1
    c = d["checks"][0]
    assert c["result"] == "uygun_degil" and c["result_label"] == "UYGUN DEĞİL"
    renk = [x for x in c["items"] if x["key"] == "renk"][0]
    assert renk["state"] == "degisim" and renk["note"] == "sarıya çalıyor"
    assert renk["label"] == "Renk" and renk["state_label"] == "Değişim var"
    assert len(c["items"]) == 5


def test_check_does_not_change_stock_or_status(authed_client, db_session):
    """'Uygun değil' bile stoğa/duruma DOKUNMAZ — otomatik imha yok."""
    rs, tgt = _make_sample(authed_client, db_session, suffix="CS")
    before = db_session.query(Item).get(tgt.id).current_stock
    tx_before = db_session.query(Transaction).count()
    assert authed_client.post(f"/api/retention/samples/{rs.id}/checks",
                              json=_check_payload(result="uygun_degil"),
                              headers=_HDR).status_code == 201
    db_session.expire_all()
    row = db_session.query(RetentionSample).get(rs.id)
    assert row.status == "stored" and row.quantity == 5
    assert db_session.query(Item).get(tgt.id).current_stock == before
    assert db_session.query(Transaction).count() == tx_before


def test_check_validation(authed_client, db_session):
    rs, _ = _make_sample(authed_client, db_session, suffix="CV")
    url = f"/api/retention/samples/{rs.id}/checks"
    bad_key = _check_payload()
    bad_key["items"][0] = {"key": "viskozite", "state": "normal"}
    assert authed_client.post(url, json=bad_key, headers=_HDR).status_code == 400
    bad_state = _check_payload()
    bad_state["items"][0] = {"key": "gorunum", "state": "belki"}
    assert authed_client.post(url, json=bad_state, headers=_HDR).status_code == 400
    eksik = _check_payload()
    eksik["items"] = eksik["items"][:3]                       # 5 kalem dolmalı
    assert authed_client.post(url, json=eksik, headers=_HDR).status_code == 400
    assert authed_client.post(url, json=_check_payload(result="belki"),
                              headers=_HDR).status_code == 400
    ileri = _check_payload(checked_on=(date.today() + timedelta(days=1)).isoformat())
    assert authed_client.post(url, json=ileri, headers=_HDR).status_code == 400
    db_session.expire_all()
    assert db_session.query(RetentionSample).get(rs.id).checks == []   # hiçbiri yazılmadı


def test_check_blocked_on_destroyed_sample(authed_client, db_session):
    rs, _ = _make_sample(authed_client, db_session, suffix="CB")
    authed_client.post(f"/api/retention/samples/{rs.id}/destroy", json={}, headers=_HDR)
    r = authed_client.post(f"/api/retention/samples/{rs.id}/checks",
                           json=_check_payload(), headers=_HDR)
    assert r.status_code == 400 and "kontrol edilemez" in r.json()["detail"]


def test_check_shows_in_timeline_and_list(authed_client, db_session):
    """Kontrol hareket çizelgesinde görünür; listede EN YENİ sonuç yazar."""
    rs, _ = _make_sample(authed_client, db_session, suffix="CT")
    authed_client.post(f"/api/retention/samples/{rs.id}/checks",
                       json=_check_payload(checked_on="2026-01-10", result="uygun"), headers=_HDR)
    authed_client.post(f"/api/retention/samples/{rs.id}/checks",
                       json=_check_payload(checked_on="2026-06-20", result="uygun_degil"), headers=_HDR)
    d = authed_client.get(f"/api/retention/samples/{rs.id}").json()
    assert [m["type"] for m in d["movements"]].count("kontrol") == 2
    assert d["movements"][-1]["type_label"] == "Periyodik kontrol"
    row = [x for x in authed_client.get("/api/retention/samples").json()["rows"]
           if x["id"] == rs.id][0]
    assert row["check_count"] == 2
    assert row["last_check_on"] == "20.06.2026"                # en yenisi
    assert row["last_check_result"] == "uygun_degil"


def test_check_rbac_and_domain(client, authed_client, db_session):
    rs, _ = _make_sample(authed_client, db_session, suffix="CR")
    url = f"/api/retention/samples/{rs.id}/checks"
    # Staff (retention.edit yok) yazamaz
    su = db_session.query(User).filter(User.username == "melek.kaya").first()
    if su:
        client.post("/api/login", json={"username": su.username, "password": "minerva123"},
                    headers=_HDR)
        assert client.post(url, json=_check_payload(), headers=_HDR).status_code == 403
    # Domain izolasyonu
    authed_client.post("/api/domain/switch", json={"domain": "supplement"}, headers=_HDR)
    assert authed_client.post(url, json=_check_payload(), headers=_HDR).status_code == 404


def test_unchecked_filter(authed_client, db_session):
    a, _ = _make_sample(authed_client, db_session, suffix="U1")
    b, _ = _make_sample(authed_client, db_session, suffix="U2")
    authed_client.post(f"/api/retention/samples/{a.id}/checks",
                       json=_check_payload(), headers=_HDR)
    ids = [x["id"] for x in authed_client.get(
        "/api/retention/samples?unchecked=1").json()["rows"]]
    assert b.id in ids and a.id not in ids


# ─── Teyit bayrağı ──────────────────────────────────────────────────────────

def test_needs_review_filter_and_clear(authed_client, db_session):
    rs, _ = _make_sample(authed_client, db_session, suffix="NR")
    db_session.query(RetentionSample).filter(RetentionSample.id == rs.id).update(
        {"needs_review": True, "note": "SKT belirsiz — TEYİT BEKLİYOR"})
    db_session.commit()
    rows = authed_client.get("/api/retention/samples?needs_review=1").json()["rows"]
    assert [x["id"] for x in rows] == [rs.id] and rows[0]["needs_review"] is True
    # Teyit et → filtreden çıkar
    assert authed_client.put(f"/api/retention/samples/{rs.id}",
                             json={"needs_review": False}, headers=_HDR).status_code == 200
    db_session.expire_all()
    assert db_session.query(RetentionSample).get(rs.id).needs_review is False
    assert authed_client.get("/api/retention/samples?needs_review=1").json()["rows"] == []


def test_backfill_is_idempotent_and_respects_clearing(authed_client, db_session):
    """Backfill BİR KEZ koşar — kullanıcı bayrağı temizleyince geri getirmez.

    init_db her açılışta çalıştığı için bu kritik: koşulsuz bir UPDATE her
    deploy'da teyit edilmiş kayıtları yeniden 'bekliyor'a döndürürdü.
    """
    from database import _backfill_retention_needs_review, AppSetting
    rs, _ = _make_sample(authed_client, db_session, suffix="BF")
    db_session.query(RetentionSample).filter(RetentionSample.id == rs.id).update(
        {"note": "TEYİT BEKLİYOR"})
    db_session.query(AppSetting).filter(
        AppSetting.key == "retention.review_backfill_done").delete()
    db_session.commit()

    _backfill_retention_needs_review()
    db_session.expire_all()
    assert db_session.query(RetentionSample).get(rs.id).needs_review is True

    # Kullanıcı teyit etti (not metni yerinde duruyor) → ikinci koşu DOKUNMAMALI
    db_session.query(RetentionSample).filter(RetentionSample.id == rs.id).update(
        {"needs_review": False})
    db_session.commit()
    _backfill_retention_needs_review()
    db_session.expire_all()
    assert db_session.query(RetentionSample).get(rs.id).needs_review is False


def test_put_recomputes_retention_until_from_produced_at(authed_client, db_session):
    rs, _ = _make_sample(authed_client, db_session, suffix="PR")
    r = authed_client.put(f"/api/retention/samples/{rs.id}",
                          json={"produced_at": "2026-01-15"}, headers=_HDR)
    assert r.status_code == 200, r.text
    db_session.expire_all()
    # 24 + 6 ay varsayılan → 15.07.2028
    assert db_session.query(RetentionSample).get(rs.id).retention_until == date(2028, 7, 15)


def test_manual_create_rejects_duplicate_lot(authed_client, db_session):
    rec, tgt = _recipe(db_session, suffix="DL")
    body = {"item_id": tgt.id, "lot_number": "dup-1", "quantity": 2}
    assert authed_client.post("/api/retention/samples", json=body, headers=_HDR).status_code == 201
    r2 = authed_client.post("/api/retention/samples", json=body, headers=_HDR)
    assert r2.status_code == 400 and "zaten kayıtlı" in r2.json()["detail"]


# ─── Boy (varyasyon) ayrımı ─────────────────────────────────────────────────
#
# Aynı ürünün 200 ml'si ve 500 ml'si AYRI partiler hâlinde üretiliyor ve her
# boydan ayrı şahit numune saklanıyor (kullanıcı teyidi + 03.08.2026 sayım
# belgesi: aynı üretim tarihinde 500 ml EV002, 200 ml EV006 numarasını almış).
# Boy ayrı bir kolon DEĞİL — boy, varyasyon `Item` satırının kendisidir.

def _family(db: Session, *, suffix="F", brand="Evanira", domain="cosmetics"):
    """Ana ürün + iki boy varyasyonu.  (ana, küçük, büyük) döner."""
    parent = Item(name=f"{brand} Losyon {suffix}", sku=f"p-{suffix}",
                  category="Bitmiş Ürün", unit="adet", current_stock=0, domain=domain)
    db.add(parent); db.flush()
    small = Item(name=f"{brand} Losyon {suffix} 200ml", sku=f"s-{suffix}",
                 category="Bitmiş Ürün", unit="adet", current_stock=20,
                 parent_id=parent.id, variation_name="200ml", domain=domain)
    big = Item(name=f"{brand} Losyon {suffix} 500ml", sku=f"b-{suffix}",
               category="Bitmiş Ürün", unit="adet", current_stock=7,
               parent_id=parent.id, variation_name="500ml", domain=domain)
    db.add_all([small, big]); db.commit()
    return parent, small, big


def _cabinet_row(db: Session, item: Item, lot: str, qty=2.0):
    """Sayımdan gelmiş gibi bir dolap kaydı — `inventory_id` YOK, stok bağı yok."""
    r = RetentionSample(item_id=item.id, item_name=item.name, lot_number=lot,
                        brand=cabinet_of(item.name), quantity=qty, initial_quantity=qty,
                        unit="adet", status="stored", source="sayim",
                        placed_by="sayım", domain=item.domain or "cosmetics")
    db.add(r); db.commit()
    return r


def test_create_rejects_parent_item(authed_client, db_session):
    """Ana ürün SOYUT — fiziksel numunesi olamaz.  21 kayıtlık hatanın kapısı."""
    parent, small, _ = _family(db_session, suffix="RP")
    r = authed_client.post("/api/retention/samples",
                           json={"item_id": parent.id, "lot_number": "EV001", "quantity": 2},
                           headers=_HDR)
    assert r.status_code == 400 and "ana üründür" in r.json()["detail"]
    assert db_session.query(RetentionSample).filter(
        RetentionSample.item_id == parent.id).count() == 0
    # Varyasyona yazmak serbest
    assert authed_client.post("/api/retention/samples",
                              json={"item_id": small.id, "lot_number": "EV001", "quantity": 2},
                              headers=_HDR).status_code == 201


def test_list_exposes_size_fields(authed_client, db_session):
    parent, small, big = _family(db_session, suffix="LS")
    _cabinet_row(db_session, small, "EV006")
    _cabinet_row(db_session, parent, "EV002")          # boyu belirsiz (eski sayım)
    rows = authed_client.get("/api/retention/samples").json()["rows"]
    by_lot = {r["lot_number"]: r for r in rows}
    assert by_lot["EV006"]["variation_name"] == "200ml"
    assert by_lot["EV006"]["parent_name"] == parent.name
    assert by_lot["EV006"]["is_parent"] is False
    assert by_lot["EV002"]["variation_name"] == ""
    assert by_lot["EV002"]["is_parent"] is True        # → arayüzde "boy seçilmedi"
    # Boy seçicisi için iki varyasyon da listede
    assert {v["variation_name"] for v in by_lot["EV002"]["variants"]} == {"200ml", "500ml"}


def test_set_variation_moves_record_and_leaves_stock_alone(authed_client, db_session):
    """Boy düzeltmesi kaydın ÜRÜNÜNÜ değiştirir — stoğa/ledger'a DOKUNMAZ."""
    parent, small, big = _family(db_session, suffix="MV")
    rs = _cabinet_row(db_session, parent, "EV003")
    stock_before = (small.current_stock, big.current_stock, parent.current_stock)
    tx_before = db_session.query(Transaction).count()

    r = authed_client.put(f"/api/retention/samples/{rs.id}/variation",
                          json={"item_id": big.id}, headers=_HDR)
    assert r.status_code == 200, r.text
    assert r.json()["variation_name"] == "500ml"
    db_session.expire_all()

    moved = db_session.query(RetentionSample).get(rs.id)
    assert moved.item_id == big.id and moved.item_name == big.name
    assert moved.quantity == 2.0                        # adet aynı
    # STOK ve LEDGER değişmedi — asıl regresyon kilidi
    assert (db_session.query(Item).get(small.id).current_stock,
            db_session.query(Item).get(big.id).current_stock,
            db_session.query(Item).get(parent.id).current_stock) == stock_before
    assert db_session.query(Transaction).count() == tx_before
    # İz bırakır
    assert db_session.query(RetentionSampleMovement).filter(
        RetentionSampleMovement.sample_id == rs.id,
        RetentionSampleMovement.movement_type == "duzeltme").count() == 1


def test_set_variation_rejects_foreign_and_parent_targets(authed_client, db_session):
    parent, small, _ = _family(db_session, suffix="FT")
    other_parent, other_small, _ = _family(db_session, suffix="FT2")
    rs = _cabinet_row(db_session, parent, "EV004")

    bad = authed_client.put(f"/api/retention/samples/{rs.id}/variation",
                            json={"item_id": other_small.id}, headers=_HDR)
    assert bad.status_code == 400 and "boy değil" in bad.json()["detail"]

    par = authed_client.put(f"/api/retention/samples/{rs.id}/variation",
                            json={"item_id": other_parent.id}, headers=_HDR)
    assert par.status_code == 400
    db_session.expire_all()
    assert db_session.query(RetentionSample).get(rs.id).item_id == parent.id


def test_set_variation_blocked_for_production_sample(authed_client, db_session):
    """Üretim kaydı `-S` Inventory lotuna bağlı; ürünü değişirse çıkışta
    yanlış ürünün stoğu düşerdi."""
    rs, _ = _make_sample(authed_client, db_session, suffix="PB")
    assert rs.inventory_id is not None
    r = authed_client.put(f"/api/retention/samples/{rs.id}/variation",
                          json={"item_id": rs.item_id}, headers=_HDR)
    assert r.status_code == 400 and "Üretimden gelen" in r.json()["detail"]


def test_bulk_variation(authed_client, db_session):
    parent, small, big = _family(db_session, suffix="BV")
    a = _cabinet_row(db_session, parent, "EV005")
    b = _cabinet_row(db_session, parent, "EV006")
    other_parent, other_small, _ = _family(db_session, suffix="BV2")
    c = _cabinet_row(db_session, other_parent, "EV007")

    r = authed_client.put("/api/retention/samples/bulk-variation", headers=_HDR, json={
        "rows": [{"id": a.id, "item_id": small.id},
                 {"id": b.id, "item_id": big.id},
                 {"id": c.id, "item_id": small.id}]})     # yabancı aile → atlanır
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["updated"] == 2 and d["skipped"] == 1 and d["errors"]
    db_session.expire_all()
    assert db_session.query(RetentionSample).get(a.id).item_id == small.id
    assert db_session.query(RetentionSample).get(b.id).item_id == big.id
    assert db_session.query(RetentionSample).get(c.id).item_id == other_parent.id


def test_cabinets_product_breakdown(authed_client, db_session):
    parent, small, big = _family(db_session, suffix="CB")
    _cabinet_row(db_session, small, "EV006", qty=2)
    _cabinet_row(db_session, big, "EV002", qty=3)
    _cabinet_row(db_session, parent, "EV009", qty=1)      # boyu belirsiz
    d = authed_client.get("/api/retention/cabinets").json()
    cab = next(c for c in d["cabinets"] if c["brand"] == cabinet_of(parent.name))
    got = {(p["variation_name"], p["quantity"], p["is_parent"]) for p in cab["products"]}
    assert ("200ml", 2.0, False) in got
    assert ("500ml", 3.0, False) in got
    assert ("", 1.0, True) in got                         # ana ürüne yazılmış kayıt
    assert cab["unsized"] == 1 and d["totals"]["unsized"] == 1


def test_unsized_and_item_id_filters(authed_client, db_session):
    parent, small, _ = _family(db_session, suffix="UF")
    _cabinet_row(db_session, small, "EV006")
    unsized = _cabinet_row(db_session, parent, "EV002")

    rows = authed_client.get("/api/retention/samples?unsized=1").json()["rows"]
    assert [r["id"] for r in rows] == [unsized.id]

    rows = authed_client.get(f"/api/retention/samples?item_id={small.id}").json()["rows"]
    assert [r["lot_number"] for r in rows] == ["EV006"]


def test_variation_rbac(client, authed_client, db_session):
    """Boy düzeltmesi `retention.edit` ister.

    Tohumlanan dört kullanıcının hiçbirinde bu izin KAPALI değil (SuperAdmin /
    Manager / LabLead / LabTech hepsinde açık — dolabı fiilen laborant
    kullanıyor), o yüzden burada LabTech'in yazabildiği doğrulanır.  İznin
    gerçekten aranması `require_permission` bağımlılığıyla garanti; kapalı rol
    davranışı `test_delete_rbac`'te (destroy LabTech'te kapalı) kilitli.
    """
    parent, small, _ = _family(db_session, suffix="VR")
    rs = _cabinet_row(db_session, parent, "EV008")
    client.post("/api/login", json={"username": "meltem", "password": "minerva123"},
                headers=_HDR)
    assert client.put(f"/api/retention/samples/{rs.id}/variation",
                      json={"item_id": small.id}, headers=_HDR).status_code == 200
    db_session.expire_all()
    assert db_session.query(RetentionSample).get(rs.id).item_id == small.id


def test_variation_domain_isolation(authed_client, db_session):
    """Supplement panelinden kozmetik numunesine dokunulamaz."""
    parent, small, _ = _family(db_session, suffix="VD")
    rs = _cabinet_row(db_session, parent, "EV010")
    authed_client.post("/api/domain/switch", json={"domain": "supplement"}, headers=_HDR)
    r = authed_client.put(f"/api/retention/samples/{rs.id}/variation",
                          json={"item_id": small.id}, headers=_HDR)
    assert r.status_code == 404
    db_session.expire_all()
    assert db_session.query(RetentionSample).get(rs.id).item_id == parent.id
