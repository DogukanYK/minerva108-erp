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

import pytest  # noqa: E402

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
    assert all(_DEFAULT_PERMISSIONS["LabLead"]["suppliers"].values())
    # Manager: durum + elle fiyat açık, birleştirme KAPALI (items.delete gibi —
    # kaybeden kartı pasife alır; backfill kuralıyla aynı sonuç)
    assert _DEFAULT_PERMISSIONS["Manager"]["suppliers"] == {"status": True, "prices": True, "merge": False}
    assert _DEFAULT_PERMISSIONS["Manager"]["items"]["delete"] is False
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

    # Değer = rol varsayılanı VE override'daki ilgili izin (production.cancel
    # kalıbı): status/prices ← items.edit, merge ← items.edit + items.delete.
    mgr = _u("isik2", "Manager", {"items": {"view": True, "edit": False}})
    full = _u("isik3", "Manager", {"items": {"view": True, "edit": True, "delete": True}})
    nodel = _u("lead2", "LabLead", {"items": {"view": True, "edit": True, "delete": False}})
    lead = _u("lead3", "LabLead", {"items": {"view": True, "edit": True, "delete": True}})
    lt = _u("tech2", "LabTech", {"items": {"view": True, "edit": True, "delete": True}})
    keep = _u("mgr_keep", "Manager", {"suppliers": {"prices": False}})
    db_session.query(AppSetting).filter(AppSetting.key == "backfill.perm.suppliers.v1").delete()
    db_session.commit()

    _backfill_perm_suppliers()
    db_session.expire_all()
    perms = lambda u: json.loads(db_session.get(User, u.id).permissions)  # noqa: E731
    assert perms(mgr)["suppliers"] == {"status": False, "prices": False, "merge": False}
    assert perms(mgr)["items"] == {"view": True, "edit": False}       # başka kategoriye dokunmaz
    # Manager rol varsayılanında merge kapalı → items.delete açık olsa da False
    # (override'sız Manager ile aynı sonuç)
    assert perms(full)["suppliers"] == {"status": True, "prices": True, "merge": False}
    assert perms(lead)["suppliers"] == {"status": True, "prices": True, "merge": True}
    assert perms(nodel)["suppliers"] == {"status": True, "prices": True, "merge": False}
    # Rol varsayılanı False ise izin ne olursa olsun False
    assert perms(lt)["suppliers"] == {"status": False, "prices": False, "merge": False}
    # items anahtarı yok → kart düzenleme kapalı sayılır; var olan anahtara dokunulmaz
    assert perms(keep)["suppliers"] == {"prices": False, "status": False, "merge": False}
    audits = {a.target_name: json.loads(a.details) for a in
              db_session.query(AdminAuditLog)
              .filter(AdminAuditLog.action == "permissions.backfill").all()
              if "suppliers." in (a.details or "")}
    assert set(audits) == {"isik2", "isik3", "lead2", "lead3", "tech2", "mgr_keep"}
    # Koşul yüzünden False yazılanlar audit'te: hangi izin eksikti (rol
    # varsayılanı zaten False olan merge "gated" sayılmaz)
    assert audits["isik2"]["gated"] == {"suppliers.status": "items.edit",
                                        "suppliers.prices": "items.edit"}
    assert audits["lead3"]["gated"] == {}
    assert audits["lead2"]["gated"] == {"suppliers.merge": "items.edit+items.delete"}
    assert audits["isik3"]["gated"] == {}
    assert audits["tech2"]["gated"] == {}                            # rol zaten False

    # Sentinel — ikinci koşu no-op (yönetici sonradan kapatsa da ezilmez)
    u = db_session.get(User, full.id)
    p = json.loads(u.permissions); p["suppliers"]["status"] = False
    u.permissions = json.dumps(p); db_session.commit()
    _backfill_perm_suppliers()
    db_session.expire_all()
    assert perms(full)["suppliers"]["status"] is False


# ─── Tedarikçi birleştirme (P1b — 07.10.2026) ───────────────────────────────

from database import (MaterialGroup, MaterialSupplierPref, ProductionConsumption,  # noqa: E402
                      ProductionHistory, SampleAnalysis, SampleAnalysisIngredient, StockOrderFlag)


def _dup_pair(db, *, domain="cosmetics"):
    """Prod'daki mükerrer kart: TATLİDİLİMLER (kaybeden) / TATLIDİLİMLER (kazanan)
    + kaybedene bağlı her türden kayıt."""
    loser = Supplier(name="TATLİDİLİMLER", phone="0212 111 11 11", email="satis@tatli.com",
                     notes="1 kg satıyor", purchase_status="phase_out", status_reason="küçük miktar",
                     domain=domain)
    survivor = Supplier(name="TATLIDİLİMLER", contact_person="Ayşe", domain=domain)
    db.add_all([loser, survivor])
    db.flush()
    it = Item(name="SETİL STEARİL ALKOL", category="Hammadde", unit="g", supplier_id=loser.id, domain=domain)
    it2 = Item(name="GLİSERİN", category="Hammadde", unit="g", supplier_id=loser.id, domain=domain,
               is_active=False)
    db.add_all([it, it2])
    db.flush()
    grp = MaterialGroup(name="Setil", domain=domain)
    db.add(grp)
    db.flush()
    lot = Inventory(item_id=it.id, supplier_id=loser.id, lot_number="L1", quantity=5, domain=domain)
    ph = ProductionHistory(recipe_name="Krem", produced_quantity=1, domain=domain)
    db.add_all([lot, ph,
                SupplierPrice(item_id=it.id, supplier_id=loser.id, supplier_name="TATLİDİLİMLER",
                              unit_price=3.0, price_unit="kg", currency="USD", domain=domain),
                SupplierPrice(item_id=it.id, supplier_id=survivor.id, supplier_name="TATLIDİLİMLER",
                              unit_price=3.2, price_unit="kg", currency="USD", domain=domain),
                StockOrderFlag(item_id=it.id, supplier_id=loser.id, quantity=10, domain=domain),
                # aynı grup için iki firmanın da tercihi → kaybedeninki düşer
                MaterialSupplierPref(domain=domain, material_group_id=grp.id, supplier_id=loser.id,
                                     preference="avoid"),
                MaterialSupplierPref(domain=domain, material_group_id=grp.id, supplier_id=survivor.id,
                                     preference="preferred"),
                MaterialSupplierPref(domain=domain, item_id=it.id, supplier_id=loser.id,
                                     preference="preferred", rank=2)])
    db.flush()
    db.add(ProductionConsumption(production_id=ph.id, kind="raw", item_id=it.id, inventory_id=lot.id,
                                 lot_number="L1", supplier_id=loser.id, supplier_name="TATLİDİLİMLER",
                                 quantity=1, unit="g", domain=domain))
    an = SampleAnalysis(document_no="NA-1", bulk_name="Deneme", domain=domain)
    db.add(an)
    db.flush()
    db.add(SampleAnalysisIngredient(analysis_id=an.id, item_id=it.id, item_name=it.name,
                                    supplier_name="TATLİDİLİMLER"))
    db.commit()
    return loser.id, survivor.id, it.id, it2.id, grp.id


def test_merge_preview_counts_and_no_change(authed_client: TestClient, db_session):
    lid, sid, *_ = _dup_pair(db_session)
    r = authed_client.get(f"/api/suppliers/{lid}/merge-preview/{sid}")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["counts"] == {"items": 2, "items_active": 1, "lots": 1, "prices": 1, "price_overlaps": 1,
                           "order_flags": 1, "consumptions": 1, "prefs": 2, "prefs_dropped": 1}
    assert b["copy_fields"] == ["phone", "email", "notes"]
    assert "kendi durumunu (Normal) korur" in b["status_warning"]
    db_session.expire_all()
    assert db_session.get(Supplier, lid).is_active is True          # önizleme veri değiştirmez


def test_merge_moves_every_fk_and_deactivates_loser(authed_client: TestClient, db_session):
    lid, sid, iid, iid2, gid = _dup_pair(db_session)
    r = authed_client.post(f"/api/suppliers/{lid}/merge-into/{sid}", headers=ORIGIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["counts"]["items"] == 2 and body["warnings"] and "birleştirildi" in body["message"]
    db_session.expire_all()
    loser, survivor = db_session.get(Supplier, lid), db_session.get(Supplier, sid)
    assert loser.is_active is False and "«TATLIDİLİMLER»" in loser.notes and loser.notes.startswith("1 kg")
    assert survivor.purchase_status == "normal"                       # kazanan durumunu korur
    assert (survivor.phone, survivor.email, survivor.contact_person) == \
        ("0212 111 11 11", "satis@tatli.com", "Ayşe")                 # boş alanlar dolduruldu
    assert db_session.get(Item, iid).supplier_id == sid and db_session.get(Item, iid2).supplier_id == sid
    assert db_session.query(Inventory).filter(Inventory.supplier_id == lid).count() == 0
    prices = db_session.query(SupplierPrice).filter(SupplierPrice.item_id == iid).all()
    assert {p.supplier_id for p in prices} == {sid}
    assert {p.supplier_name for p in prices} == {"TATLİDİLİMLER", "TATLIDİLİMLER"}   # metin kalır
    assert db_session.query(StockOrderFlag).one().supplier_id == sid
    pc = db_session.query(ProductionConsumption).one()
    assert (pc.supplier_id, pc.supplier_name) == (sid, "TATLİDİLİMLER")
    prefs = db_session.query(MaterialSupplierPref).order_by(MaterialSupplierPref.id).all()
    assert [(p.material_group_id, p.item_id, p.supplier_id, p.preference) for p in prefs] == [
        (gid, None, sid, "preferred"), (None, iid, sid, "preferred")]     # grup çakışması: kazananınki
    assert db_session.query(SampleAnalysisIngredient).one().supplier_name == "TATLİDİLİMLER"   # anlık ad
    a = _audits(db_session, "supplier.merge")
    assert len(a) == 1 and a[0].target_id == sid
    d = json.loads(a[0].details)
    assert d["kaybeden_id"] == lid and d["sayilar"]["lots"] == 1
    # ikinci kez: kaybeden artık pasif → 409
    r = authed_client.post(f"/api/suppliers/{lid}/merge-into/{sid}", headers=ORIGIN)
    assert r.status_code == 409
    # birleşmeden sonra çakışan (kart, firma, birim) iki satır: yalnız fiyatı
    # düzeltmek engellenmez; birimi/firmayı çakışmaya çevirmek yine 409
    keep = next(p for p in prices if p.supplier_name == "TATLIDİLİMLER")
    other = next(p for p in prices if p.id != keep.id)
    r = authed_client.put(f"/api/supplier-prices/{keep.id}", json={"unit_price": 3.5}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert r.json()["unit_price"] == 3.5
    r = authed_client.put(f"/api/supplier-prices/{other.id}", json={"price_unit": "kg", "note": "eski"},
                          headers=ORIGIN)
    assert r.status_code == 200, r.text
    db_session.add(SupplierPrice(item_id=iid, supplier_id=sid, supplier_name="TATLIDİLİMLER", unit_price=1.0,
                                 price_unit="l", currency="USD", domain="cosmetics"))
    db_session.commit()
    r = authed_client.put(f"/api/supplier-prices/{other.id}", json={"price_unit": "l"}, headers=ORIGIN)
    assert r.status_code == 409 and r.json()["code"] == "price_exists"


def test_merged_name_is_alias_moved_and_reimported_prices_stay_active(authed_client: TestClient, db_session):
    """KRK GIDA(HAYAT) → KRK GIDA (iki adın firma anahtarı FARKLI).  Taşınan
    fiyat (eski ad metinde) Raporlar panelinde / Excel'de ve planda aktif ve
    en ucuz; liste eski yazımla yeniden içe aktarılınca satır kazanana
    bağlanır (serbest metin kalıp "pasif tedarikçi" olmaz).  Kaybeden
    yeniden açılınca takma ad bağı düşer."""
    from datetime import datetime

    from core.consumption import IngredientRec, ItemRec, RecipeRec
    from core.purchase_plan import PlanInputs, compute
    from core.purchase_plan_models import PlanRequest
    from core.purchase_pricing import attach, load_price_inputs
    from core.supplier_prices import import_prices, prices_for_items
    loser = Supplier(name="KRK GIDA(HAYAT)", domain="cosmetics")
    survivor = Supplier(name="KRK GIDA", domain="cosmetics")
    other = Supplier(name="BEFCHEM", domain="cosmetics")
    item = Item(name="SETİL STEARİL ALKOL", category="Hammadde", unit="g", domain="cosmetics")
    db_session.add_all([loser, survivor, other, item])
    db_session.flush()
    lid, sid, iid = loser.id, survivor.id, item.id
    db_session.add_all([
        SupplierPrice(item_id=iid, supplier_id=lid, supplier_name="KRK GIDA(HAYAT)", unit_price=4.0,
                      price_unit="kg", currency="USD", domain="cosmetics"),
        SupplierPrice(item_id=iid, supplier_id=other.id, supplier_name="BEFCHEM", unit_price=8.0,
                      price_unit="kg", currency="USD", domain="cosmetics")])
    db_session.commit()
    assert authed_client.post(f"/api/suppliers/{lid}/merge-into/{sid}", headers=ORIGIN).status_code == 200
    db_session.expire_all()
    assert db_session.get(Supplier, lid).merged_into_id == sid

    def panel():
        return [(p["supplier_name"], p["inactive"]) for p in prices_for_items(db_session, [iid], "cosmetics")[iid]]

    def chosen():
        inp = PlanInputs(items={iid: ItemRec(id=iid, name="SETİL STEARİL ALKOL", category="Hammadde", unit="g",
                                             pkg_type=None, current_stock=0.0)},
                         recipes={1: RecipeRec(id=1, name="Krem",
                                               ingredients=(IngredientRec(item_id=iid, quantity=10),))},
                         stock_as_of=datetime(2026, 10, 8, 9, 0))
        res = attach(compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 1000}])),
                     load_price_inputs(db_session, [iid], "cosmetics"), None)
        return next(m for m in res["materials"] if iid in m["member_ids"])["supplier"]

    assert panel() == [("KRK GIDA(HAYAT)", False), ("BEFCHEM", False)]
    r = authed_client.get("/api/supplier-prices")
    g = next(x for x in r.json()["items"] if x["item_id"] == iid)
    assert [(s["supplier_name"], s["supplier_inactive"]) for s in g["suppliers"]] == panel()
    assert chosen() == "KRK GIDA(HAYAT)"
    # Işık Hanım'ın listesi eski yazımla yeniden geldi → kazanana bağlanır
    summ = import_prices(db_session, [{"material": "SETİL STEARİL ALKOL", "suppliers": [
        {"name": "KRK GIDA(HAYAT)", "package": 25, "price": 3.5},
        {"name": "BEFCHEM", "package": 25, "price": 8.0}]}], "cosmetics")
    db_session.commit()
    assert summ["unmatched_suppliers"] == []
    krk = db_session.query(SupplierPrice).filter(SupplierPrice.item_id == iid,
                                                 SupplierPrice.supplier_name == "KRK GIDA(HAYAT)").one()
    assert krk.supplier_id == sid
    assert panel() == [("KRK GIDA(HAYAT)", False), ("BEFCHEM", False)] and chosen() == "KRK GIDA(HAYAT)"
    # Kaybeden yeniden açıldı → kendi firması; takma ad bağı temizlenir
    assert authed_client.post(f"/api/suppliers/{lid}/activate", headers=ORIGIN).status_code == 200
    db_session.expire_all()
    assert db_session.get(Supplier, lid).merged_into_id is None


@pytest.mark.parametrize("call", ["receive", "pref", "price"])
def test_write_to_loser_during_merge_waits_and_is_rejected(authed_client: TestClient, db_session, call):
    """Birleştirme kaybedeni kilitliyken gelen mal kabul / tercih / fiyat
    yazımı beklemeli ve commit sonrası pasif kaybedeni görüp reddetmeli —
    kontrol kilitsiz okusaydı lot / tercih / fiyat pasif kaybedene bağlı kalırdı."""
    import threading
    from core.suppliers import merge_suppliers
    lid, sid, *_ = _dup_pair(db_session)
    free = Item(name="KAOLİN", category="Hammadde", unit="g", domain="cosmetics")
    db_session.add(free)
    db_session.commit()
    fid = free.id
    if call == "receive":
        do = lambda: authed_client.post("/api/inventory/receive", headers=ORIGIN, json={  # noqa: E731
            "item_id": fid, "supplier_id": lid, "lot_number": "YR-1", "quantity": 5})
        expect = 400
    elif call == "pref":
        do = lambda: authed_client.post("/api/material-prefs", headers=ORIGIN, json={  # noqa: E731
            "item_id": fid, "supplier_id": lid, "preference": "preferred", "rank": 1})
        expect = 409
    else:
        do = lambda: authed_client.post("/api/supplier-prices", headers=ORIGIN, json={  # noqa: E731
            "item_id": fid, "supplier_id": lid, "unit_price": 2.0, "currency": "USD", "price_unit": "kg"})
        expect = 400
    merge_suppliers(db_session, lid, sid, "cosmetics", "test")        # kilitli, commit yok
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("r", do()))
    th.start()
    th.join(timeout=1.5)
    assert th.is_alive(), "yazım birleştirmenin kilidini beklemeliydi"
    db_session.commit()
    th.join(timeout=20)
    assert not th.is_alive()
    assert out["r"].status_code == expect, out["r"].text
    assert "pasif" in out["r"].json()["detail"]
    db_session.expire_all()
    assert db_session.query(Inventory).filter(Inventory.supplier_id == lid).count() == 0
    assert db_session.query(MaterialSupplierPref).filter(MaterialSupplierPref.supplier_id == lid).count() == 0
    assert db_session.query(SupplierPrice).filter(SupplierPrice.supplier_id == lid).count() == 0


@pytest.mark.parametrize("legacy", [False, True])
def test_undo_of_earlier_item_edit_does_not_relink_merged_loser(client: TestClient, db_session, legacy):
    """Birleştirmeden ÖNCE yapılmış kart düzenlemesinin "Geri al"ı kartı pasif
    kaybedene geri bağlamaz (yeni kayıtta `after_supplier_id`, eski kayıtta
    kaybedenin pasifliği yakalar); diğer alanlar geri alınır.  Etkin eski
    tedarikçiye dönüş normal çalışır."""
    from database import UndoLog
    lid, sid, iid, *_ = _dup_pair(db_session)
    body = {"name": "SETİL STEARİL ALKOL", "category": "Hammadde", "unit": "g"}
    _login(client, "songul")
    r = client.put(f"/api/items/{iid}", headers=ORIGIN, json={**body, "min_stock": 300, "supplier_id": lid})
    assert r.status_code == 200, r.text
    assert client.post(f"/api/suppliers/{lid}/merge-into/{sid}", headers=ORIGIN).status_code == 200
    if legacy:                                   # P1b öncesi kayıt: after_supplier_id yok
        db_session.expire_all()
        e = db_session.query(UndoLog).order_by(UndoLog.id.desc()).first()
        e.payload = {k: v for k, v in e.payload.items() if k != "after_supplier_id"}
        db_session.commit()
    r = client.post("/api/undo", headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert "Tedarikçi bağı değiştirilmedi" in r.json()["message"]
    db_session.expire_all()
    it = db_session.get(Item, iid)
    assert it.supplier_id == sid and it.min_stock_level == 0          # bağ kaldı, min stok geri alındı
    # etkin eski tedarikçiye dönüş: geri alınır
    other = Supplier(name="NATURALYA", domain="cosmetics")
    db_session.add(other)
    db_session.commit()
    r = client.put(f"/api/items/{iid}", headers=ORIGIN, json={**body, "supplier_id": other.id})
    assert r.status_code == 200, r.text
    r = client.post("/api/undo", headers=ORIGIN)
    assert r.status_code == 200 and "Tedarikçi bağı" not in r.json()["message"]
    db_session.expire_all()
    assert db_session.get(Item, iid).supplier_id == sid


def test_merge_guards_same_other_domain_and_permission(client: TestClient, db_session):
    lid, sid, *_ = _dup_pair(db_session)
    other = Supplier(name="Takviye Firma", domain="supplement")
    db_session.add(other)
    db_session.commit()
    _login(client, "dogukan")
    assert client.post(f"/api/suppliers/{lid}/merge-into/{lid}", headers=ORIGIN).status_code == 400
    assert client.post(f"/api/suppliers/{lid}/merge-into/{other.id}", headers=ORIGIN).status_code == 404
    assert client.get(f"/api/suppliers/{lid}/merge-preview/{other.id}").status_code == 404
    _switch(client, "supplement")
    assert client.post(f"/api/suppliers/{lid}/merge-into/{sid}", headers=ORIGIN).status_code == 404
    _switch(client, "cosmetics")
    _login(client, "meltem")                                          # LabTech: suppliers.merge yok
    assert client.get(f"/api/suppliers/{lid}/merge-preview/{sid}").status_code == 403
    assert client.post(f"/api/suppliers/{lid}/merge-into/{sid}", headers=ORIGIN).status_code == 403
    _login(client, "songul")                                          # LabLead: var
    assert client.post(f"/api/suppliers/{lid}/merge-into/{sid}", headers=ORIGIN).status_code == 200
