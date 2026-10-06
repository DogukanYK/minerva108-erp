"""
Hedef seçmeli "Stoğa çevir" + çift sayım koruması — 06.10.2026.

Olaylar (prod, salt-okunur teşhis):
  • Naturalya'nın jojoba/portakal/lavanta numuneleri KRK GIDA ve İPEDA
    kartlarına girilmişti; çevirme stoğu yanlış tedarikçinin kartına yazdı,
    Songül Hanım 5 dk sonra açtığı Naturalya kartında 0 gördü.
  • 758 JOJOBA UÇUCU YAĞI: numune 10.09'da elle +20 sayılmış, 05.10 çevirmesi
    +20 daha ekledi (stok 40, lot 20).

Kapsam: POST /inventory/samples/{id}/convert gövdesi (target_item_id /
new_item / mode / acknowledge_counted / lot_number), GET convert-options,
gövdesiz eski çağrının aynen çalışması.
"""
import json
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from core.snapshots import compute_stock_at
from database import (AdminAuditLog, Inventory, Item, MaterialGroup, SampleAnalysis,
                      SampleAnalysisIngredient, Supplier, Transaction)

_HDR = {"Origin": "http://testserver"}


def _sup(db, name):
    s = Supplier(name=name)
    db.add(s); db.flush()
    return s.id


def _card(db, name, stock=0.0, unit="ml", category="Hammadde", supplier_id=None,
          group_id=None, domain="cosmetics", active=True):
    it = Item(name=name, category=category, unit=unit, current_stock=stock,
              supplier_id=supplier_id, material_group_id=group_id, domain=domain,
              is_active=active)
    db.add(it); db.flush()
    return it


def _group(db, name, domain="cosmetics"):
    g = MaterialGroup(name=name, domain=domain)
    db.add(g); db.flush()
    return g


def _sample(db, item_id, qty, lot="NUM-1", supplier_id=None, age_days=30, domain="cosmetics"):
    inv = Inventory(item_id=item_id, lot_number=lot, quantity=qty, status="APPROVED",
                    is_sample=True, location="Numune", supplier_id=supplier_id, domain=domain,
                    created_at=datetime.utcnow() - timedelta(days=age_days))
    db.add(inv); db.flush()
    return inv


def _analysis_row(db, item, inv, qty=1.0):
    sa = SampleAnalysis(document_no=f"NA-T-{inv.id}", bulk_name="Deneme", properties="[]",
                        domain="cosmetics")
    db.add(sa); db.flush()
    row = SampleAnalysisIngredient(analysis_id=sa.id, item_id=item.id, item_name=item.name,
                                   unit=item.unit, source="sample", inventory_id=inv.id,
                                   lot_number=inv.lot_number, quantity=qty, consumed_qty=qty)
    db.add(row); db.flush()
    return row


def _manual_adjust(db, item, delta, days_ago):
    """adjust_stock'un yazdığı biçimde elle sayım düzeltmesi."""
    db.add(Transaction(item_id=item.id, transaction_type="Adjustment", quantity=delta,
                       timestamp=datetime.utcnow() - timedelta(days=days_ago),
                       notes=f"Stok düzeltme — Eski: 0 ml → Yeni: {delta} ml (Δ +{delta}) | Sebep: DÜZELT",
                       performed_by="Betül"))
    item.current_stock = round((item.current_stock or 0) + delta, 6)


def _convert(c, inv_id, body=None):
    if body is None:
        return c.post(f"/api/inventory/samples/{inv_id}/convert", headers=_HDR)
    return c.post(f"/api/inventory/samples/{inv_id}/convert", json=body, headers=_HDR)


def _fresh(db, model, pk):
    db.expire_all()
    return db.query(model).filter(model.id == pk).first()


# ─── Eski çağrı + temel kontroller ──────────────────────────────────────────

def test_bodiless_call_still_converts_on_own_card(authed_client: TestClient, db_session: Session):
    nat = _sup(db_session, "NATURALYA")
    card = _card(db_session, "JOJOBA YAĞI", stock=100)
    inv = _sample(db_session, card.id, 50, lot="MİNERVA", supplier_id=nat)
    db_session.commit()

    r = _convert(authed_client, inv.id)
    assert r.status_code == 200, r.text
    assert r.json()["item_id"] == card.id and r.json()["moved"] is False

    assert _fresh(db_session, Item, card.id).current_stock == 150
    row = _fresh(db_session, Inventory, inv.id)
    assert row.is_sample is False and row.location is None
    assert row.sample_converted_at is not None
    tx = db_session.query(Transaction).filter(Transaction.item_id == card.id).one()
    assert tx.transaction_type == "Input" and tx.quantity == 50
    # Önek KORUNUR (satın alma motoru bununla tanır) + tedarikçi yazılır
    assert tx.notes.startswith("Numune stoğa çevrildi — Lot: MİNERVA")
    assert "Tedarikçi: NATURALYA" in tx.notes and "Kart:" not in tx.notes


def test_zero_quantity_sample_rejected(authed_client: TestClient, db_session: Session):
    card = _card(db_session, "Tükenmiş Numune Kartı")
    inv = _sample(db_session, card.id, 0)
    db_session.commit()
    r = _convert(authed_client, inv.id)
    assert r.status_code == 400
    assert _fresh(db_session, Inventory, inv.id).is_sample is True


def test_inactive_source_without_target_rejected(authed_client: TestClient, db_session: Session):
    card = _card(db_session, "Pasif Kart", active=False)
    other = _card(db_session, "Aktif Kart")
    inv = _sample(db_session, card.id, 5)
    db_session.commit()
    assert _convert(authed_client, inv.id).status_code == 400
    # hedef verilince pasif karttaki numune kurtarılabilir
    r = _convert(authed_client, inv.id, {"target_item_id": other.id})
    assert r.status_code == 200, r.text
    assert _fresh(db_session, Item, other.id).current_stock == 5


# ─── Başka karta çevirme ────────────────────────────────────────────────────

def test_convert_into_group_card_moves_row_and_writes_input_there(authed_client: TestClient,
                                                                  db_session: Session):
    krk, nat = _sup(db_session, "KRK GIDA"), _sup(db_session, "NATURALYA")
    g = _group(db_session, "JOJOBA")
    source = _card(db_session, "JOJOBA YAĞI", stock=5710, unit="g", supplier_id=krk, group_id=g.id)
    target = _card(db_session, "JOJOBA YAĞI (NUMUNE)", stock=0, unit="gr", supplier_id=nat,
                   group_id=g.id)
    inv = _sample(db_session, source.id, 50, lot="MİNERVA", supplier_id=nat)
    sai = _analysis_row(db_session, source, inv)
    db_session.commit()

    r = _convert(authed_client, inv.id, {"target_item_id": target.id})
    assert r.status_code == 200, r.text
    assert r.json()["moved"] is True and r.json()["item_id"] == target.id

    row = _fresh(db_session, Inventory, inv.id)
    assert row.item_id == target.id and row.moved_from_item_id == source.id
    assert row.is_sample is False
    assert _fresh(db_session, Item, source.id).current_stock == 5710      # kaynak AYNEN
    assert _fresh(db_session, Item, target.id).current_stock == 50
    assert db_session.query(Transaction).filter(Transaction.item_id == source.id).count() == 0
    tx = db_session.query(Transaction).filter(Transaction.item_id == target.id).one()
    assert tx.transaction_type == "Input"
    assert "Kart: JOJOBA YAĞI → JOJOBA YAĞI (NUMUNE)" in tx.notes
    assert _fresh(db_session, SampleAnalysisIngredient, sai.id).item_id == target.id

    audit = (db_session.query(AdminAuditLog)
             .filter(AdminAuditLog.action == "inventory.sample_convert").one())
    det = json.loads(audit.details)
    assert (det["source_item_id"], det["target_item_id"], det["mode"]) == (source.id, target.id, "add")
    assert det["supplier_id"] == nat and det["quantity"] == 50


def test_target_rules_reject(authed_client: TestClient, db_session: Session):
    source = _card(db_session, "Kaynak Yağ", unit="ml")
    bad = {
        "birim":      _card(db_session, "Gram Kart", unit="g"),
        "pasif":      _card(db_session, "Pasif Hedef", active=False),
        "bitmis":     _card(db_session, "Krem", unit="ml", category="Bitmiş Ürün"),
        "ambalaj":    _card(db_session, "Şişe", unit="ml", category="Ambalaj"),
    }
    other_domain = _card(db_session, "Takviye Yağ", domain="supplement")
    inv = _sample(db_session, source.id, 5)
    db_session.commit()
    for key, card in bad.items():
        r = _convert(authed_client, inv.id, {"target_item_id": card.id})
        assert r.status_code == 400, key
    assert _convert(authed_client, inv.id, {"target_item_id": other_domain.id}).status_code == 404
    row = _fresh(db_session, Inventory, inv.id)
    assert row.is_sample is True and row.item_id == source.id            # dokunulmadı
    assert db_session.query(Transaction).count() == 0


def test_new_item_copies_supplier_unit_and_joins_group(authed_client: TestClient, db_session: Session):
    krk, nat = _sup(db_session, "KRK GIDA"), _sup(db_session, "NATURALYA")
    source = _card(db_session, "PORTAKAL UÇUCU YAĞ", stock=300, supplier_id=krk)
    inv = _sample(db_session, source.id, 10, lot="MİNERVA", supplier_id=nat)
    db_session.commit()

    opts = authed_client.get(f"/api/inventory/samples/{inv.id}/convert-options").json()
    name = opts["new_card"]["suggested_name"]
    assert name == "PORTAKAL UÇUCU YAĞ — NATURALYA"

    r = _convert(authed_client, inv.id, {"new_item": {"name": name}})
    assert r.status_code == 200, r.text
    new_id = r.json()["created_item_id"]
    new = _fresh(db_session, Item, new_id)
    assert (new.unit, new.category, new.supplier_id) == ("ml", "Hammadde", nat)
    assert new.current_stock == 10
    assert _fresh(db_session, Item, source.id).current_stock == 300
    src = _fresh(db_session, Item, source.id)
    assert src.material_group_id and src.material_group_id == new.material_group_id
    grp = db_session.query(MaterialGroup).filter(MaterialGroup.id == new.material_group_id).one()
    assert grp.source == "convert" and grp.name == "PORTAKAL UÇUCU YAĞ"
    assert _fresh(db_session, Inventory, inv.id).item_id == new_id


def test_new_item_name_conflict_409_then_force(authed_client: TestClient, db_session: Session):
    source = _card(db_session, "MELEZ LAVANTA UÇUCU YAĞ")
    existing = _card(db_session, "LAVANTA YAĞI NATURALYA")
    inv = _sample(db_session, source.id, 50)
    db_session.commit()
    r = _convert(authed_client, inv.id, {"new_item": {"name": "lavanta yagi naturalya"}})
    assert r.status_code == 409 and r.json()["code"] == "name_conflict"
    assert r.json()["existing"]["id"] == existing.id
    assert db_session.query(Item).count() == 2                           # kart açılmadı
    r = _convert(authed_client, inv.id, {"new_item": {"name": "lavanta yagi naturalya",
                                                      "force": True}})
    assert r.status_code == 200, r.text


# ─── Çift sayım koruması + link_only ────────────────────────────────────────

def test_guard_409_then_acknowledge_adds(authed_client: TestClient, db_session: Session):
    card = _card(db_session, "JOJOBA UÇUCU YAĞI (NUMUNE)")
    inv = _sample(db_session, card.id, 20, lot="NUMUNE", age_days=40)
    _manual_adjust(db_session, card, 20, days_ago=25)                   # 10.09 Betül +20
    db_session.commit()

    r = _convert(authed_client, inv.id)
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "maybe_already_counted"
    assert [a["qty"] for a in body["adjustments"]] == [20]
    assert _fresh(db_session, Item, card.id).current_stock == 20         # dokunulmadı

    r = _convert(authed_client, inv.id, {"acknowledge_counted": True})
    assert r.status_code == 200, r.text
    assert _fresh(db_session, Item, card.id).current_stock == 40
    audit = (db_session.query(AdminAuditLog)
             .filter(AdminAuditLog.action == "inventory.sample_convert").one())
    assert json.loads(audit.details)["guard_adjustment_ids"]


def test_guard_ignores_adjustment_before_sample_arrived(authed_client: TestClient, db_session: Session):
    card = _card(db_session, "Eski Sayımlı Kart")
    _manual_adjust(db_session, card, 20, days_ago=60)                   # numuneden ÖNCE
    inv = _sample(db_session, card.id, 20, age_days=30)
    db_session.commit()
    r = _convert(authed_client, inv.id)
    assert r.status_code == 200, r.text
    assert _fresh(db_session, Item, card.id).current_stock == 40


def test_guard_net_sum_covers_sample(authed_client: TestClient, db_session: Session):
    """Tek kalem ±%5'te değil ama numuneden sonraki düzeltmelerin toplamı karşılıyor."""
    card = _card(db_session, "Parça Sayımlı Kart")
    inv = _sample(db_session, card.id, 50, age_days=30)
    _manual_adjust(db_session, card, 30, days_ago=20)
    _manual_adjust(db_session, card, 25, days_ago=10)
    db_session.commit()
    r = _convert(authed_client, inv.id)
    assert r.status_code == 409 and r.json()["code"] == "maybe_already_counted"


def test_link_only_keeps_ledger_and_reconstruction(authed_client: TestClient, db_session: Session):
    card = _card(db_session, "PORTAKAL UÇUCU YAĞI (NUMUNE)")
    inv = _sample(db_session, card.id, 20, lot="NUMUNE", age_days=40)
    _manual_adjust(db_session, card, 20, days_ago=25)
    db_session.commit()
    eoms = [datetime.utcnow() - timedelta(days=d) for d in (50, 30, 20, 1)] + \
           [datetime.utcnow() + timedelta(days=1)]
    before = [compute_stock_at(db_session, e)[0].get(card.id) for e in eoms]
    tx_before = db_session.query(Transaction).count()

    r = _convert(authed_client, inv.id, {"mode": "link_only"})
    assert r.status_code == 200, r.text
    assert r.json()["warning"] is None                                  # lot 20 = stok 20

    row = _fresh(db_session, Inventory, inv.id)
    assert row.is_sample is False and row.location is None and row.sample_converted_at
    assert _fresh(db_session, Item, card.id).current_stock == 20
    assert db_session.query(Transaction).count() == tx_before
    after = [compute_stock_at(db_session, e)[0].get(card.id) for e in eoms]
    assert before == after
    assert db_session.query(AdminAuditLog).filter(
        AdminAuditLog.action == "inventory.sample_link").count() == 1


def test_link_only_warns_when_lots_exceed_stock(authed_client: TestClient, db_session: Session):
    card = _card(db_session, "Sayılmamış Kart", stock=0)
    inv = _sample(db_session, card.id, 20)
    db_session.commit()
    r = _convert(authed_client, inv.id, {"mode": "link_only"})
    assert r.status_code == 200, r.text
    assert r.json()["warning"]


def test_link_only_only_on_own_card(authed_client: TestClient, db_session: Session):
    source, target = _card(db_session, "Kaynak Bağ"), _card(db_session, "Hedef Bağ")
    inv = _sample(db_session, source.id, 5)
    db_session.commit()
    r = _convert(authed_client, inv.id, {"mode": "link_only", "target_item_id": target.id})
    assert r.status_code == 400
    assert _fresh(db_session, Inventory, inv.id).is_sample is True


# ─── Lot no çakışması ───────────────────────────────────────────────────────

def test_lot_collision_409_then_new_lot_number(authed_client: TestClient, db_session: Session):
    a, b = _sup(db_session, "Tedarikçi A"), _sup(db_session, "Tedarikçi B")
    card = _card(db_session, "Çakışan Kart", stock=30)
    normal = Inventory(item_id=card.id, lot_number="L1", quantity=30, status="APPROVED",
                       supplier_id=a, domain="cosmetics")
    db_session.add(normal)
    inv = _sample(db_session, card.id, 8, lot="L1", supplier_id=b)
    db_session.commit()

    r = _convert(authed_client, inv.id)
    assert r.status_code == 409
    assert r.json()["code"] == "lot_collision" and r.json()["existing"]["supplier_name"] == "Tedarikçi A"

    r = _convert(authed_client, inv.id, {"lot_number": "L1-B"})
    assert r.status_code == 200, r.text
    row = _fresh(db_session, Inventory, inv.id)
    assert (row.lot_number, row.is_sample, row.supplier_id) == ("L1-B", False, b)
    assert _fresh(db_session, Inventory, normal.id).quantity == 30
    tx = db_session.query(Transaction).filter(Transaction.item_id == card.id).one()
    assert tx.lot_number == "L1-B"


def test_merge_into_same_supplier_lot_redirects_analysis_rows(authed_client: TestClient,
                                                             db_session: Session):
    a = _sup(db_session, "Tedarikçi A")
    source = _card(db_session, "Birleşen Kaynak")
    target = _card(db_session, "Birleşen Hedef", stock=10)
    normal = Inventory(item_id=target.id, lot_number="L7", quantity=10, status="APPROVED",
                       supplier_id=None, domain="cosmetics")
    db_session.add(normal)
    inv = _sample(db_session, source.id, 4, lot="L7", supplier_id=a)
    sai = _analysis_row(db_session, source, inv)
    db_session.commit()

    r = _convert(authed_client, inv.id, {"target_item_id": target.id})
    assert r.status_code == 200, r.text
    assert r.json()["inventory_id"] == normal.id
    assert _fresh(db_session, Inventory, inv.id) is None                 # numune satırı silindi
    survivor = _fresh(db_session, Inventory, normal.id)
    assert survivor.quantity == 14 and survivor.supplier_id == a         # COALESCE
    link = _fresh(db_session, SampleAnalysisIngredient, sai.id)
    assert (link.inventory_id, link.item_id) == (normal.id, target.id)   # NULL DEĞİL
    assert _fresh(db_session, Item, target.id).current_stock == 14


# ─── convert-options ────────────────────────────────────────────────────────

def test_convert_options_ranking_and_filters(authed_client: TestClient, db_session: Session):
    krk, nat, ipe = _sup(db_session, "KRK GIDA"), _sup(db_session, "NATURALYA"), _sup(db_session, "İPEDA")
    g = _group(db_session, "JOJOBA")
    source = _card(db_session, "JOJOBA YAĞI", stock=5710, unit="g", supplier_id=krk, group_id=g.id)
    t0 = _card(db_session, "JOJOBA NATURALYA", unit="gr", supplier_id=nat, group_id=g.id)
    t1 = _card(db_session, "Jojoba Altın", unit="g", supplier_id=ipe, group_id=g.id)
    t2 = _card(db_session, "JOJOBA YAĞI (NUMUNE)", unit="g", supplier_id=nat)
    t3 = _card(db_session, "Jojoba Yağı Organik", unit="g", supplier_id=ipe)
    _card(db_session, "JOJOBA YAĞI LİTRE", unit="ml", supplier_id=nat)            # birim farkı
    _card(db_session, "JOJOBA YAĞI KREM", unit="g", category="Bitmiş Ürün")        # bitmiş ürün
    _card(db_session, "JOJOBA YAĞI ESKİ", unit="g", active=False)                  # pasif
    _card(db_session, "JOJOBA YAĞI TAKVİYE", unit="g", domain="supplement")       # başka panel
    _card(db_session, "BADEM YAĞI", unit="g", supplier_id=nat)                     # ilgisiz
    inv = _sample(db_session, source.id, 50, lot="MİNERVA", supplier_id=nat)
    db_session.commit()

    r = authed_client.get(f"/api/inventory/samples/{inv.id}/convert-options")
    assert r.status_code == 200, r.text
    d = r.json()
    assert [c["item_id"] for c in d["candidates"]] == [t0.id, t1.id, t2.id, t3.id]
    assert d["candidates"][0]["reason"].startswith("Aynı malzeme grubu")
    assert d["sample"]["supplier_name"] == "NATURALYA" and d["sample"]["qty"] == 50
    assert d["source"]["group"] == {"id": g.id, "name": "JOJOBA"}
    assert d["source"]["supplier_name"] == "KRK GIDA"
    assert d["guard"]["maybe_already_counted"] is False
    assert d["new_card"]["supplier_id"] == nat and d["new_card"]["unit"] == "g"


def test_convert_options_requires_receive_permission(client: TestClient, db_session: Session):
    import bcrypt
    from database import User
    card = _card(db_session, "Yetki Kartı")
    inv = _sample(db_session, card.id, 5)
    db_session.add(User(username="rafci2", full_name="Rafçı", role="Staff", is_active=True,
                        password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode()))
    db_session.commit()
    assert client.post("/api/login", json={"username": "rafci2", "password": "minerva123"},
                       headers=_HDR).status_code == 200
    assert client.get(f"/api/inventory/samples/{inv.id}/convert-options").status_code == 403


# ─── İnceleme bulguları (06.10): kaynak/ilk kart koruması, analiz uyarısı ───

def test_guard_on_source_card_when_converting_elsewhere(authed_client: TestClient,
                                                        db_session: Session):
    """Numune A'da elle +20 sayılmış; B'ye (ya da yeni karta) çevrilirse aynı
    20 ml iki kartta sayılırdı (A 20 lotsuz, B +20)."""
    a = _card(db_session, "PORTAKAL UÇUCU YAĞI (NUMUNE)")
    b = _card(db_session, "PORTAKAL UÇUCU YAĞ", stock=100)
    inv = _sample(db_session, a.id, 20, lot="NUMUNE", age_days=40)
    _manual_adjust(db_session, a, 20, days_ago=25)
    db_session.commit()

    opts = authed_client.get(f"/api/inventory/samples/{inv.id}/convert-options").json()
    assert [(g["on"], g["item_id"]) for g in opts["guards"]] == [("source", a.id)]

    for body in ({"target_item_id": b.id}, {"new_item": {"name": "PORTAKAL — DOĞASA"}}):
        r = _convert(authed_client, inv.id, body)
        assert r.status_code == 409, (body, r.text)
        d = r.json()
        assert d["code"] == "maybe_already_counted"
        assert (d["on"], d["item_id"]) == ("source", a.id)
        assert "yalnız lotu bağla" in d["detail"]
    assert _fresh(db_session, Item, a.id).current_stock == 20
    assert _fresh(db_session, Item, b.id).current_stock == 100
    assert db_session.query(Item).count() == 2                           # yeni kart açılmadı
    assert _fresh(db_session, Inventory, inv.id).item_id == a.id

    # Doğru yol: önce kendi kartında bağla, sonra normal lotu taşı → bir kez sayılır
    r = _convert(authed_client, inv.id, {"mode": "link_only"})
    assert r.status_code == 200, r.text
    r = authed_client.post(f"/api/inventory/lots/{inv.id}/move", headers=_HDR,
                           json={"target_item_id": b.id, "reason": "numune B malı"})
    assert r.status_code == 200, r.text
    assert _fresh(db_session, Item, a.id).current_stock == 0
    assert _fresh(db_session, Item, b.id).current_stock == 120


def test_acknowledged_ids_cover_only_seen_guards(authed_client: TestClient, db_session: Session):
    a = _card(db_session, "Kaynak Sayımlı")
    b = _card(db_session, "Hedef Sayımlı", stock=50)
    inv = _sample(db_session, a.id, 20, age_days=40)
    _manual_adjust(db_session, a, 20, days_ago=25)
    _manual_adjust(db_session, b, 20, days_ago=20)
    db_session.commit()

    # Arayüz yalnız kaynağın bulgusunu görmüştü — hedefinki görülmemiş: yine 409
    r = _convert(authed_client, inv.id, {"target_item_id": b.id, "acknowledged_item_ids": [a.id]})
    assert r.status_code == 409
    d = r.json()
    assert (d["on"], d["item_id"]) == ("target", b.id)
    assert {(g["on"], g["item_id"]) for g in d["guards"]} == {("target", b.id), ("source", a.id)}

    r = _convert(authed_client, inv.id, {"target_item_id": b.id,
                                         "acknowledged_item_ids": [a.id, b.id]})
    assert r.status_code == 200, r.text
    assert _fresh(db_session, Item, b.id).current_stock == 90
    det = json.loads(db_session.query(AdminAuditLog)
                     .filter(AdminAuditLog.action == "inventory.sample_convert").one().details)
    assert set(det["guard_item_ids"]) == {a.id, b.id} and len(det["guard_adjustment_ids"]) == 2


def test_acknowledge_counted_true_still_overrides_all(authed_client: TestClient, db_session: Session):
    a = _card(db_session, "Eski Ack Kaynak")
    b = _card(db_session, "Eski Ack Hedef")
    inv = _sample(db_session, a.id, 20, age_days=40)
    _manual_adjust(db_session, a, 20, days_ago=25)
    db_session.commit()
    r = _convert(authed_client, inv.id, {"target_item_id": b.id, "acknowledge_counted": True})
    assert r.status_code == 200, r.text
    assert _fresh(db_session, Item, b.id).current_stock == 20


def test_guard_on_origin_card_after_sample_was_moved(authed_client: TestClient, db_session: Session):
    """Numune X'te elle sayılmış, sonra "Karta taşı" ile A'ya gitmiş (deftersiz):
    A'da çevirmek X'teki sayımı görmüyordu."""
    x = _card(db_session, "LAVANTA UÇUCU YAĞI")
    a = _card(db_session, "LAVANTA — NATURALYA")
    inv = _sample(db_session, x.id, 20, age_days=40)
    _manual_adjust(db_session, x, 20, days_ago=25)
    db_session.commit()
    r = authed_client.post(f"/api/inventory/lots/{inv.id}/move", headers=_HDR,
                           json={"target_item_id": a.id, "reason": "doğru tedarikçi"})
    assert r.status_code == 200, r.text

    opts = authed_client.get(f"/api/inventory/samples/{inv.id}/convert-options").json()
    assert [(g["on"], g["item_id"]) for g in opts["guards"]] == [("origin", x.id)]
    r = _convert(authed_client, inv.id)
    assert r.status_code == 409
    assert (r.json()["on"], r.json()["item_id"]) == ("origin", x.id)
    assert "geri taşıyıp" in r.json()["detail"]
    assert _fresh(db_session, Item, a.id).current_stock == 0


def test_link_only_warns_when_analysis_consumed_sample(authed_client: TestClient,
                                                       db_session: Session):
    """Analiz numuneden 5 düşmüş (lot 15), kart elle 20 sayılmış: bağlama
    stoğu değiştirmez ama uyarır — analiz silinirse iade +5 stoğa da yazılır."""
    card = _card(db_session, "Analizli Numune Kartı")
    inv = _sample(db_session, card.id, 20, lot="NUMUNE", age_days=40)
    _manual_adjust(db_session, card, 20, days_ago=25)
    _analysis_row(db_session, card, inv, qty=5.0)
    inv.quantity = 15
    db_session.commit()

    opts = authed_client.get(f"/api/inventory/samples/{inv.id}/convert-options").json()
    assert opts["sample"]["analysis_used"] == 5
    assert opts["sample"]["analysis_docs"] == [f"NA-T-{inv.id}"]

    tx_before = db_session.query(Transaction).count()
    r = _convert(authed_client, inv.id, {"mode": "link_only"})
    assert r.status_code == 200, r.text
    w = r.json()["warning"] or ""
    assert "Numune Analizi" in w and f"NA-T-{inv.id}" in w
    assert _fresh(db_session, Item, card.id).current_stock == 20
    assert db_session.query(Transaction).count() == tx_before
    det = json.loads(db_session.query(AdminAuditLog)
                     .filter(AdminAuditLog.action == "inventory.sample_link").one().details)
    assert det["analysis_used"] == 5
