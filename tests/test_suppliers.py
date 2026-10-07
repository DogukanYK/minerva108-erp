"""Tedarikçi düzenleme (PUT /api/suppliers/{id}) — sayfada daha önce yalnız ekle/sil vardı."""
from fastapi.testclient import TestClient

ORIGIN = {"Origin": "http://testserver"}


def _create(c: TestClient, name="Argan Kimya") -> int:
    r = c.post("/api/suppliers", json={"name": name, "phone": "0212 000 00 00"}, headers=ORIGIN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _get(c: TestClient, sid: int) -> dict:
    return next(s for s in c.get("/api/suppliers").json() if s["id"] == sid)


def test_update_supplier_changes_fields(authed_client: TestClient):
    sid = _create(authed_client)
    r = authed_client.put(f"/api/suppliers/{sid}", headers=ORIGIN, json={
        "name": "  Argan Kimya A.Ş.  ", "contact_person": "Ayşe Yılmaz", "email": "ayse@argan.com",
        "phone": "0216 111 11 11", "notes": "30 gün vade"})
    assert r.status_code == 200, r.text
    s = _get(authed_client, sid)
    assert s["name"] == "Argan Kimya A.Ş."            # baş/son boşluk kırpılır
    assert (s["contact_person"], s["email"], s["phone"], s["notes"]) == \
        ("Ayşe Yılmaz", "ayse@argan.com", "0216 111 11 11", "30 gün vade")


def test_update_supplier_clears_optional_fields_and_rejects_blank_name(authed_client: TestClient):
    sid = _create(authed_client)
    r = authed_client.put(f"/api/suppliers/{sid}", headers=ORIGIN, json={"name": "Argan", "phone": "  "})
    assert r.status_code == 200
    assert _get(authed_client, sid)["phone"] is None
    r = authed_client.put(f"/api/suppliers/{sid}", headers=ORIGIN, json={"name": "   "})
    assert r.status_code == 400
    assert _get(authed_client, sid)["name"] == "Argan"


def test_update_supplier_other_domain_is_404(authed_client: TestClient):
    sid = _create(authed_client, "Kozmetik Tedarikçi")
    authed_client.post("/api/domain/switch", json={"domain": "supplement"}, headers=ORIGIN)
    r = authed_client.put(f"/api/suppliers/{sid}", headers=ORIGIN, json={"name": "Değişti"})
    assert r.status_code == 404
    authed_client.post("/api/domain/switch", json={"domain": "cosmetics"}, headers=ORIGIN)
    assert _get(authed_client, sid)["name"] == "Kozmetik Tedarikçi"


def test_update_supplier_unknown_is_404(authed_client: TestClient):
    r = authed_client.put("/api/suppliers/999999", headers=ORIGIN, json={"name": "X"})
    assert r.status_code == 404


def test_update_supplier_requires_items_edit(client: TestClient):
    # Önce SuperAdmin bir tedarikçi açar, sonra LabTech (items.edit kapalı) düzenlemeye çalışır.
    client.post("/api/login", json={"username": "dogukan", "password": "minerva123"}, headers=ORIGIN)
    sid = _create(client)
    client.post("/api/logout", headers=ORIGIN)
    client.post("/api/login", json={"username": "meltem", "password": "minerva123"}, headers=ORIGIN)
    r = client.put(f"/api/suppliers/{sid}", headers=ORIGIN, json={"name": "Yetkisiz"})
    assert r.status_code == 403


# ─── Satın alma durumu, yumuşak silme, adres (P1a — 07.10.2026) ─────────────

import json  # noqa: E402

from database import AdminAuditLog, Inventory, Item, Supplier, SupplierPrice  # noqa: E402


def _login(c: TestClient, username: str) -> TestClient:
    c.post("/api/logout", headers=ORIGIN)
    r = c.post("/api/login", json={"username": username, "password": "minerva123"}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    return c


def _audits(db, action):
    db.expire_all()
    return db.query(AdminAuditLog).filter(AdminAuditLog.action == action).order_by(AdminAuditLog.id).all()


def _switch(c: TestClient, domain: str):
    r = c.post("/api/domain/switch", json={"domain": domain}, headers=ORIGIN)
    assert r.status_code == 200, r.text


def test_list_has_status_and_address_fields(authed_client: TestClient):
    sid = _create(authed_client)
    s = _get(authed_client, sid)
    assert s["purchase_status"] == "normal" and s["status_label"] == "Normal"
    assert s["is_active"] is True and s["address"] is None
    assert s["status_reason"] is None and s["status_at"] == ""


def test_status_put_by_lablead_writes_stamp_and_audit(client: TestClient, db_session):
    _login(client, "songul")                                   # LabLead
    sid = _create(client, "Hammadde Sepeti")
    r = client.put(f"/api/suppliers/{sid}/status", headers=ORIGIN,
                   json={"status": "phase_out", "reason": "küçük miktar satıyor"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["purchase_status"] == "phase_out" and body["status_label"] == "Bitirilecek — alma"
    assert body["status_reason"] == "küçük miktar satıyor"
    assert body["status_by"] and body["status_at"]
    assert _get(client, sid)["purchase_status"] == "phase_out"
    a = _audits(db_session, "supplier.status")
    assert len(a) == 1
    d = json.loads(a[0].details)
    assert (d["eski"], d["yeni"], d["sebep"]) == ("normal", "phase_out", "küçük miktar satıyor")
    # Aynı değer tekrar → değişiklik yok, yeni audit yok
    client.put(f"/api/suppliers/{sid}/status", headers=ORIGIN,
               json={"status": "phase_out", "reason": "küçük miktar satıyor"})
    assert len(_audits(db_session, "supplier.status")) == 1
    # Normal'e dönüş: sebep temizlenir
    r = client.put(f"/api/suppliers/{sid}/status", headers=ORIGIN, json={"status": "normal"})
    assert r.status_code == 200 and r.json()["status_reason"] is None


def test_status_phase_out_requires_reason_and_rejects_unknown(authed_client: TestClient):
    sid = _create(authed_client)
    r = authed_client.put(f"/api/suppliers/{sid}/status", headers=ORIGIN,
                          json={"status": "phase_out", "reason": "   "})
    assert r.status_code == 400
    r = authed_client.put(f"/api/suppliers/{sid}/status", headers=ORIGIN, json={"status": "bad"})
    assert r.status_code == 400
    r = authed_client.put(f"/api/suppliers/{sid}/status", headers=ORIGIN, json={"status": "preferred"})
    assert r.status_code == 200 and r.json()["status_label"] == "Tercih edilen"


def test_status_put_forbidden_for_labtech(client: TestClient):
    _login(client, "dogukan")
    sid = _create(client)
    _login(client, "meltem")                                   # LabTech
    r = client.put(f"/api/suppliers/{sid}/status", headers=ORIGIN,
                   json={"status": "preferred"})
    assert r.status_code == 403


def test_status_other_domain_404_and_inactive_409(authed_client: TestClient):
    sid = _create(authed_client)
    _switch(authed_client, "supplement")
    r = authed_client.put(f"/api/suppliers/{sid}/status", headers=ORIGIN, json={"status": "preferred"})
    assert r.status_code == 404
    assert authed_client.get(f"/api/suppliers/{sid}/prices").status_code == 404
    assert authed_client.delete(f"/api/suppliers/{sid}", headers=ORIGIN).status_code == 404
    _switch(authed_client, "cosmetics")
    assert authed_client.delete(f"/api/suppliers/{sid}", headers=ORIGIN).status_code == 200
    r = authed_client.put(f"/api/suppliers/{sid}/status", headers=ORIGIN, json={"status": "preferred"})
    assert r.status_code == 409


def test_soft_delete_keeps_lot_and_card_links(authed_client: TestClient, db_session):
    sid = _create(authed_client, "Tatlıdilimler")
    it = Item(name="SETİL ALKOL", category="Hammadde", unit="g", current_stock=0,
              supplier_id=sid, domain="cosmetics")
    db_session.add(it); db_session.commit()
    r = authed_client.post("/api/inventory/receive", headers=ORIGIN, json={
        "item_id": it.id, "supplier_id": sid, "lot_number": "L-1", "quantity": 500})
    assert r.status_code == 201, r.text
    r = authed_client.delete(f"/api/suppliers/{sid}", headers=ORIGIN)
    assert r.status_code == 200, r.text
    db_session.expire_all()
    sup = db_session.get(Supplier, sid)
    assert sup is not None and sup.is_active is False           # satır duruyor
    inv = db_session.query(Inventory).filter(Inventory.item_id == it.id).one()
    assert inv.supplier_id == sid
    assert db_session.get(Item, it.id).supplier_id == sid
    # Liste pasifi gizler; include_inactive / include_ids gösterir
    assert sid not in [s["id"] for s in authed_client.get("/api/suppliers").json()]
    allr = authed_client.get("/api/suppliers?include_inactive=1").json()
    assert next(s for s in allr if s["id"] == sid)["is_active"] is False
    assert sid in [s["id"] for s in authed_client.get(f"/api/suppliers?include_ids={sid}").json()]
    # Kart listesinde ad kaybolmaz, pasif olduğu işaretlenir
    row = next(i for i in authed_client.get("/api/items").json() if i["id"] == it.id)
    assert row["supplier_name"] == "Tatlıdilimler" and row["supplier_active"] is False
    summ = next(i for i in authed_client.get("/api/inventory/summary").json() if i["item_id"] == it.id)
    assert summ["supplier_name"] == "Tatlıdilimler"
    a = _audits(db_session, "supplier.deactivate")
    assert len(a) == 1 and a[0].target_id == sid


def test_bulk_delete_soft_domain_scoped(authed_client: TestClient, db_session):
    a = _create(authed_client, "A Firma")
    b = _create(authed_client, "B Firma")
    _switch(authed_client, "supplement")
    c = _create(authed_client, "C Takviye")
    _switch(authed_client, "cosmetics")
    r = authed_client.post("/api/suppliers/bulk-delete", headers=ORIGIN,
                           json={"supplier_ids": [a, b, c]})
    assert r.status_code == 200 and r.json()["deactivated"] == 2
    db_session.expire_all()
    assert db_session.get(Supplier, a).is_active is False
    assert db_session.get(Supplier, b).is_active is False
    assert db_session.get(Supplier, c).is_active is True         # başka panel dokunulmadı
    assert {x.target_id for x in _audits(db_session, "supplier.deactivate")} == {a, b}


def test_activate_restores_supplier(authed_client: TestClient, db_session):
    sid = _create(authed_client)
    authed_client.delete(f"/api/suppliers/{sid}", headers=ORIGIN)
    r = authed_client.post(f"/api/suppliers/{sid}/activate", headers=ORIGIN)
    assert r.status_code == 200
    assert _get(authed_client, sid)["is_active"] is True
    assert len(_audits(db_session, "supplier.activate")) == 1


def test_address_roundtrip_and_partial_put_keeps_it(authed_client: TestClient, db_session):
    r = authed_client.post("/api/suppliers", headers=ORIGIN,
                           json={"name": "Naturalya", "address": " Kocaeli OSB 3. cad. "})
    sid = r.json()["id"]
    assert _get(authed_client, sid)["address"] == "Kocaeli OSB 3. cad."
    # Adres alanını göndermeyen (eski) istemci adresi silmez
    authed_client.put(f"/api/suppliers/{sid}", headers=ORIGIN, json={"name": "Naturalya"})
    assert _get(authed_client, sid)["address"] == "Kocaeli OSB 3. cad."
    authed_client.put(f"/api/suppliers/{sid}", headers=ORIGIN, json={"name": "Naturalya", "address": ""})
    assert _get(authed_client, sid)["address"] is None
    assert len(_audits(db_session, "supplier.create")) == 1
    upd = _audits(db_session, "supplier.update")
    assert len(upd) == 1 and "address" in json.loads(upd[0].details)["changes"]


def test_supplier_prices_endpoint_linked_and_key_matched(authed_client: TestClient, db_session):
    sid = _create(authed_client, "ULUDAĞ HERBAL")
    other = _create(authed_client, "NATURALYA")
    it = Item(name="ARGAN YAĞI", category="Hammadde", unit="g", current_stock=0, domain="cosmetics")
    db_session.add(it); db_session.commit()
    db_session.add_all([
        SupplierPrice(item_id=it.id, supplier_id=sid, supplier_name="ULUDAĞ HERBAL",
                      unit_price=20, currency="USD", price_unit="kg", domain="cosmetics"),
        SupplierPrice(item_id=it.id, supplier_id=None, supplier_name="ULUDAG HERBAL",
                      unit_price=19, currency="USD", price_unit="kg", domain="cosmetics"),
        SupplierPrice(item_id=it.id, supplier_id=other, supplier_name="NATURALYA",
                      unit_price=25, currency="USD", price_unit="kg", domain="cosmetics"),
    ])
    db_session.commit()
    r = authed_client.get(f"/api/suppliers/{sid}/prices")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["supplier"]["id"] == sid and body["total"] == 2
    assert sorted(p["matched"] for p in body["prices"]) == [False, True]
    assert {p["material"] for p in body["prices"]} == {"ARGAN YAĞI"}


def test_status_by_key_merges_duplicate_cards():
    from core.purchase_pricing import SupplierIndex
    from core.suppliers import status_by_key
    sups = [{"name": "TATLİDİLİMLER", "purchase_status": "phase_out"},
            {"name": "TATLIDİLİMLER", "purchase_status": "preferred"},
            {"name": "NATURALYA", "purchase_status": "preferred"},
            {"name": "KRK GIDA", "purchase_status": "normal", "is_active": False}]
    ix = SupplierIndex([s["name"] for s in sups])
    by_key, conflicts = status_by_key(sups, ix)
    assert by_key[ix.key("TATLİDİLİMLER")] == "phase_out"
    assert by_key[ix.key("NATURALYA")] == "preferred"
    assert ix.key("KRK GIDA") not in by_key                     # pasif sayılmaz
    assert len(conflicts) == 1 and set(conflicts[0]["names_by_status"]) == {"phase_out", "preferred"}


def test_supplier_ref_validated_on_receive_and_item_save(authed_client: TestClient, db_session):
    sid = _create(authed_client, "Aktif Firma")
    gone = _create(authed_client, "Eski Firma")
    _switch(authed_client, "supplement")
    foreign = _create(authed_client, "Takviye Firma")
    _switch(authed_client, "cosmetics")
    it = Item(name="GLİSERİN", category="Hammadde", unit="g", current_stock=0,
              supplier_id=gone, domain="cosmetics")
    db_session.add(it); db_session.commit()
    authed_client.delete(f"/api/suppliers/{gone}", headers=ORIGIN)
    base = {"item_id": it.id, "lot_number": "L-9", "quantity": 10}
    assert authed_client.post("/api/inventory/receive", headers=ORIGIN,
                              json={**base, "supplier_id": foreign}).status_code == 400
    assert authed_client.post("/api/inventory/receive", headers=ORIGIN,
                              json={**base, "supplier_id": gone}).status_code == 400
    card = {"name": "GLİSERİN", "category": "Hammadde", "unit": "g"}
    # Kartın DEĞİŞMEYEN pasif tedarikçisi kabul (sessizce düşmesin, kayıt da engellenmesin)
    r = authed_client.put(f"/api/items/{it.id}", headers=ORIGIN, json={**card, "supplier_id": gone})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.get(Item, it.id).supplier_id == gone
    # Başka panelin ya da yeni atanan pasif tedarikçi reddedilir
    r = authed_client.put(f"/api/items/{it.id}", headers=ORIGIN, json={**card, "supplier_id": foreign})
    assert r.status_code == 400
    r = authed_client.post("/api/items", headers=ORIGIN,
                           json={"name": "YENİ KART", "category": "Hammadde", "unit": "g",
                                 "supplier_id": gone})
    assert r.status_code == 400
    r = authed_client.put(f"/api/items/{it.id}", headers=ORIGIN, json={**card, "supplier_id": sid})
    assert r.status_code == 200


# ─── suppliers.* yetkileri + override'lı kullanıcı backfill'i ───────────────

def test_supplier_permission_defaults():
    from core.permissions import _DEFAULT_PERMISSIONS, PERMISSION_CATEGORIES
    assert PERMISSION_CATEGORIES["suppliers"] == ["status", "prices", "merge"]
    for role in ("Manager", "LabLead"):
        assert all(_DEFAULT_PERMISSIONS[role]["suppliers"].values())
    for role in ("LabTech", "Staff", "Distributor"):
        assert not any(_DEFAULT_PERMISSIONS[role]["suppliers"].values())


def test_supplier_permission_backfill_for_override_users(db_session):
    import bcrypt

    from database import AppSetting, User, _backfill_perm_suppliers
    pw = bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode()

    def _u(name, role, perms):
        u = User(username=name, password_hash=pw, full_name=name, role=role, is_active=True,
                 permissions=json.dumps(perms))
        db_session.add(u)
        return u

    mgr = _u("isik2", "Manager", {"items": {"view": True, "edit": False}})
    lt = _u("tech2", "LabTech", {"items": {"view": True}})
    keep = _u("mgr_keep", "Manager", {"suppliers": {"prices": False}})
    db_session.query(AppSetting).filter(AppSetting.key == "backfill.perm.suppliers.v1").delete()
    db_session.commit()

    _backfill_perm_suppliers()
    db_session.expire_all()
    perms = lambda u: json.loads(db_session.get(User, u.id).permissions)  # noqa: E731
    assert perms(mgr)["suppliers"] == {"status": True, "prices": True, "merge": True}
    assert perms(mgr)["items"] == {"view": True, "edit": False}       # başka kategoriye dokunmaz
    assert perms(lt)["suppliers"] == {"status": False, "prices": False, "merge": False}
    assert perms(keep)["suppliers"] == {"prices": False, "status": True, "merge": True}
    audits = (db_session.query(AdminAuditLog)
              .filter(AdminAuditLog.action == "permissions.backfill").all())
    names = {a.target_name for a in audits
             if "suppliers." in (a.details or "")}
    assert names == {"isik2", "tech2", "mgr_keep"}

    # Sentinel — ikinci koşu no-op (yönetici sonradan kapatsa da ezilmez)
    u = db_session.get(User, mgr.id)
    p = json.loads(u.permissions); p["suppliers"]["status"] = False
    u.permissions = json.dumps(p); db_session.commit()
    _backfill_perm_suppliers()
    db_session.expire_all()
    assert perms(mgr)["suppliers"]["status"] is False
