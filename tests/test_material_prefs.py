"""
Malzeme bazlı tedarikçi tercihi (/api/material-prefs) + kart birleştirme /
grup dağılması kancaları + mal kabul ipucu (/api/inventory/receive-options).

Lab (Songül Hanım, 07.10.2026): "önce küçük satıcıların elimizdekini bitirip
yerine alışveriş yapacağımız tedarikçileri ekleyelim" — malzeme başına sıralı
tercih ve "bu malzemede alma".  İzole DB ile koşun:
    MINERVA_TEST_DB=minerva_test2 .venv/bin/pytest tests/test_material_prefs.py -q
"""
import json

from fastapi.testclient import TestClient

from database import (AdminAuditLog, Item, MaterialGroup, MaterialSupplierPref, Supplier)

ORIGIN = {"Origin": "http://testserver"}


def _login(c: TestClient, username: str) -> TestClient:
    c.post("/api/logout", headers=ORIGIN)
    r = c.post("/api/login", json={"username": username, "password": "minerva123"}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    return c


def _switch(c: TestClient, domain: str):
    assert c.post("/api/domain/switch", json={"domain": domain}, headers=ORIGIN).status_code == 200


def _audits(db, action):
    db.expire_all()
    return db.query(AdminAuditLog).filter(AdminAuditLog.action == action).order_by(AdminAuditLog.id).all()


def _seed(db, domain="cosmetics"):
    """Jojoba: iki tedarikçi kartı aynı grupta + grupsuz ayrı kart; üç firma."""
    krk = Supplier(name="KRK GIDA", domain=domain)
    sepet = Supplier(name="HAMMADDE SEPETİ", domain=domain, purchase_status="phase_out",
                     status_reason="1 kg satıyor")
    nat = Supplier(name="NATURALYA", domain=domain)
    db.add_all([krk, sepet, nat])
    db.flush()
    grp = MaterialGroup(name="Jojoba", domain=domain)
    db.add(grp)
    db.flush()
    a = Item(name="JOJOBA YAĞI", category="Hammadde", unit="g", supplier_id=krk.id,
             material_group_id=grp.id, domain=domain, current_stock=100)
    b = Item(name="JOJOBA YAĞI — HAMMADDE SEPETİ", category="Hammadde", unit="g", supplier_id=sepet.id,
             material_group_id=grp.id, domain=domain, current_stock=500)
    solo = Item(name="KAOLİN KİLİ", category="Hammadde", unit="g", domain=domain)
    fin = Item(name="Krem 50 ml", category="Bitmiş Ürün", unit="adet", domain=domain)
    db.add_all([a, b, solo, fin])
    db.commit()
    return {"krk": krk.id, "sepet": sepet.id, "nat": nat.id, "grp": grp.id, "a": a.id, "b": b.id,
            "solo": solo.id, "fin": fin.id}


# ─── CRUD ───────────────────────────────────────────────────────────────────

def test_create_defaults_to_group_scope_and_lists_effective(client: TestClient, db_session):
    ids = _seed(db_session)
    _login(client, "songul")                                         # LabLead: suppliers.status
    r = client.post("/api/material-prefs", headers=ORIGIN, json={
        "item_id": ids["a"], "supplier_id": ids["nat"], "preference": "preferred", "rank": 1,
        "note": "büyük ambalaj"})
    assert r.status_code == 201, r.text
    row = r.json()
    assert (row["scope"], row["material_group_id"], row["item_id"]) == ("group", ids["grp"], None)
    assert row["supplier_name"] == "NATURALYA" and row["preference_label"] == "Tercih edilen"
    # kart kapsamı (scope=item) aynı firmanın grup satırını kartta ezer
    r = client.post("/api/material-prefs", headers=ORIGIN, json={
        "item_id": ids["a"], "scope": "item", "supplier_id": ids["nat"], "preference": "avoid"})
    assert r.status_code == 201 and r.json()["scope"] == "item"
    client.post("/api/material-prefs", headers=ORIGIN, json={
        "material_group_id": ids["grp"], "supplier_id": ids["krk"], "preference": "preferred", "rank": 2})
    body = client.get(f"/api/material-prefs?item_id={ids['a']}").json()
    assert body["target"] == {"kind": "group", "id": ids["grp"]}
    assert body["item"]["group"] == {"id": ids["grp"], "name": "Jojoba"}
    assert len(body["prefs"]) == 3
    assert [p["supplier_name"] for p in body["effective"]["preferred"]] == ["KRK GIDA"]
    assert [(p["supplier_name"], p["scope"]) for p in body["effective"]["avoid"]] == [("NATURALYA", "item")]
    # grubun öbür kartında kart satırı yok → grup tercihi geçerli
    other = client.get(f"/api/material-prefs?item_id={ids['b']}").json()
    assert [p["supplier_name"] for p in other["effective"]["preferred"]] == ["NATURALYA", "KRK GIDA"]
    grp = client.get(f"/api/material-prefs?group_id={ids['grp']}").json()
    assert {p["supplier_name"] for p in grp["prefs"]} == {"NATURALYA", "KRK GIDA"}
    a = _audits(db_session, "material_pref.create")
    assert len(a) == 3 and json.loads(a[0].details)["kapsam"] == "group"


def test_create_validations(client: TestClient, db_session):
    ids = _seed(db_session)
    _login(client, "dogukan")
    post = lambda body: client.post("/api/material-prefs", headers=ORIGIN, json=body)   # noqa: E731
    assert post({"supplier_id": ids["nat"]}).status_code == 400                       # hedef yok
    assert post({"item_id": ids["a"], "material_group_id": ids["grp"],
                 "supplier_id": ids["nat"]}).status_code == 400                       # iki hedef
    assert post({"item_id": ids["fin"], "supplier_id": ids["nat"]}).status_code == 400   # bitmiş ürün
    assert post({"item_id": ids["solo"], "supplier_id": 999999}).status_code == 404
    assert post({"item_id": ids["solo"], "supplier_id": ids["nat"], "preference": "x"}).status_code == 422
    r = post({"item_id": ids["solo"], "supplier_id": ids["nat"]})
    assert r.status_code == 201 and r.json()["scope"] == "item"                       # grupsuz → karta
    dup = post({"item_id": ids["solo"], "supplier_id": ids["nat"], "preference": "avoid"})
    assert dup.status_code == 409 and dup.json()["code"] == "pref_exists" and dup.json()["id"] == r.json()["id"]
    db_session.get(Supplier, ids["krk"]).is_active = False
    db_session.commit()
    assert post({"item_id": ids["solo"], "supplier_id": ids["krk"]}).status_code == 409   # pasif firma


def test_update_delete_audit_and_rbac(client: TestClient, db_session):
    ids = _seed(db_session)
    _login(client, "dogukan")
    pid = client.post("/api/material-prefs", headers=ORIGIN, json={
        "item_id": ids["solo"], "supplier_id": ids["nat"]}).json()["id"]
    _login(client, "meltem")                                         # LabTech: okur, yazamaz
    assert client.get(f"/api/material-prefs?item_id={ids['solo']}").status_code == 200
    assert client.post("/api/material-prefs", headers=ORIGIN, json={
        "item_id": ids["solo"], "supplier_id": ids["krk"]}).status_code == 403
    assert client.put(f"/api/material-prefs/{pid}", headers=ORIGIN, json={"rank": 2}).status_code == 403
    assert client.delete(f"/api/material-prefs/{pid}", headers=ORIGIN).status_code == 403
    _login(client, "isik")                                           # Manager
    r = client.put(f"/api/material-prefs/{pid}", headers=ORIGIN, json={"rank": 3, "note": "  yedek "})
    assert r.status_code == 200 and (r.json()["rank"], r.json()["note"]) == (3, "yedek")
    a = _audits(db_session, "material_pref.update")
    assert json.loads(a[0].details)["changes"]["rank"] == {"eski": 1, "yeni": 3}
    _switch(client, "supplement")
    assert client.put(f"/api/material-prefs/{pid}", headers=ORIGIN, json={"rank": 1}).status_code == 404
    assert client.get(f"/api/material-prefs?item_id={ids['solo']}").status_code == 404
    assert client.delete(f"/api/material-prefs/{pid}", headers=ORIGIN).status_code == 404
    _switch(client, "cosmetics")
    assert client.delete(f"/api/material-prefs/{pid}", headers=ORIGIN).status_code == 200
    assert db_session.query(MaterialSupplierPref).count() == 0
    d = json.loads(_audits(db_session, "material_pref.delete")[0].details)
    assert d["eski"]["supplier_name"] == "NATURALYA" and d["eski"]["rank"] == 3


# ─── Kancalar: kart birleştirme + grup dağılması ────────────────────────────

def test_merge_items_moves_item_prefs_and_dedupes(db_session):
    from core.item_merge import merge_items
    ids = _seed(db_session)
    dup = Item(name="KAOLIN KILI", category="Hammadde", unit="g", domain="cosmetics")
    db_session.add(dup)
    db_session.flush()
    db_session.add_all([
        MaterialSupplierPref(domain="cosmetics", item_id=dup.id, supplier_id=ids["nat"], preference="avoid"),
        MaterialSupplierPref(domain="cosmetics", item_id=dup.id, supplier_id=ids["krk"], preference="preferred"),
        MaterialSupplierPref(domain="cosmetics", item_id=ids["solo"], supplier_id=ids["nat"],
                             preference="preferred")])
    db_session.commit()
    summary = merge_items(db_session, dup.id, ids["solo"], actor="test")
    db_session.commit()
    assert (summary["material_prefs_moved"], summary["material_prefs_dropped"]) == (1, 1)
    rows = {(p.item_id, p.supplier_id): p.preference for p in db_session.query(MaterialSupplierPref).all()}
    assert rows == {(ids["solo"], ids["nat"]): "preferred",           # kazananınki esas
                    (ids["solo"], ids["krk"]): "preferred"}


def test_group_dissolve_turns_group_prefs_into_item_prefs(client: TestClient, db_session):
    ids = _seed(db_session)
    db_session.add_all([
        MaterialSupplierPref(domain="cosmetics", material_group_id=ids["grp"], supplier_id=ids["nat"],
                             preference="preferred", rank=1, note="lab"),
        MaterialSupplierPref(domain="cosmetics", material_group_id=ids["grp"], supplier_id=ids["krk"],
                             preference="avoid"),
        # kartın kendi kararı aynı firmada esas kalır
        MaterialSupplierPref(domain="cosmetics", item_id=ids["a"], supplier_id=ids["krk"],
                             preference="preferred", rank=2)])
    db_session.commit()
    _login(client, "dogukan")
    r = client.delete(f"/api/material-groups/{ids['grp']}/members/{ids['b']}", headers=ORIGIN)
    assert r.status_code == 200 and r.json()["dissolved"] is True
    db_session.expire_all()
    rows = {(p.material_group_id, p.item_id, p.supplier_id, p.preference, p.rank, p.note)
            for p in db_session.query(MaterialSupplierPref).all()}
    assert rows == {(None, ids["a"], ids["nat"], "preferred", 1, "lab"),
                    (None, ids["a"], ids["krk"], "preferred", 2, None),          # çıkarılan karta yazılmaz
                    # grubun satırları pasif grupta iz olarak kalır (okuyucular aktif grubu okur)
                    (ids["grp"], None, ids["nat"], "preferred", 1, "lab"),
                    (ids["grp"], None, ids["krk"], "avoid", 1, None)}
    r = client.get(f"/api/material-prefs?item_id={ids['a']}", headers=ORIGIN)
    assert r.status_code == 200
    assert {p["scope"] for p in r.json()["prefs"]} == {"item"}


def test_group_move_carries_dissolved_group_prefs_to_new_group(client: TestClient, db_session):
    """"Taşı" ile kartları yeni gruba geçen eski grup dağılır; lab'ın o
    gruptaki kararları (tercih + "alma") yeni gruba geçer, audit'e sayısı
    yazılır.  Yeni grubun aynı firmadaki kendi kararı esas kalır."""
    ids = _seed(db_session)
    db_session.add_all([
        MaterialSupplierPref(domain="cosmetics", material_group_id=ids["grp"], supplier_id=ids["nat"],
                             preference="preferred", rank=1, note="lab"),
        MaterialSupplierPref(domain="cosmetics", material_group_id=ids["grp"], supplier_id=ids["krk"],
                             preference="avoid")])
    db_session.commit()
    _login(client, "dogukan")
    r = client.post("/api/material-groups", headers=ORIGIN,
                    json={"name": "Jojoba tüm", "item_ids": [ids["a"], ids["b"], ids["solo"]], "move": True})
    assert r.status_code == 201, r.text
    new_id = r.json()["id"]
    assert r.json()["dissolved_groups"] == [ids["grp"]]
    db_session.expire_all()
    got = {(p.supplier_id, p.preference, p.rank, p.note) for p in db_session.query(MaterialSupplierPref)
           .filter(MaterialSupplierPref.material_group_id == new_id).all()}
    assert got == {(ids["nat"], "preferred", 1, "lab"), (ids["krk"], "avoid", 1, None)}
    eff = client.get(f"/api/material-prefs?item_id={ids['solo']}", headers=ORIGIN).json()["effective"]
    assert [p["supplier_id"] for p in eff["preferred"]] == [ids["nat"]]
    assert [p["supplier_id"] for p in eff["avoid"]] == [ids["krk"]]
    assert json.loads(_audits(db_session, "material_group.create")[-1].details)["tercihler_tasindi"] == 2

    # add_member + move: hedef grubun aynı firmadaki kararı esas, eksikler gelir
    d = Item(name="JOJOBA X", category="Hammadde", unit="g", domain="cosmetics")
    e = Item(name="JOJOBA Y", category="Hammadde", unit="g", domain="cosmetics")
    db_session.add_all([d, e])
    db_session.flush()
    g2 = MaterialGroup(name="Jojoba eski", domain="cosmetics")
    db_session.add(g2)
    db_session.flush()
    d.material_group_id = e.material_group_id = g2.id
    db_session.add_all([
        MaterialSupplierPref(domain="cosmetics", material_group_id=g2.id, supplier_id=ids["krk"],
                             preference="preferred", rank=3),
        MaterialSupplierPref(domain="cosmetics", material_group_id=g2.id, supplier_id=ids["sepet"],
                             preference="avoid")])
    db_session.commit()
    r = client.post(f"/api/material-groups/{new_id}/members", headers=ORIGIN,
                    json={"item_id": d.id, "move": True})
    assert r.status_code == 200, r.text
    assert r.json()["dissolved_groups"] == [g2.id]
    db_session.expire_all()
    got = {(p.supplier_id, p.preference) for p in db_session.query(MaterialSupplierPref)
           .filter(MaterialSupplierPref.material_group_id == new_id).all()}
    assert got == {(ids["nat"], "preferred"), (ids["krk"], "avoid"), (ids["sepet"], "avoid")}
    assert json.loads(_audits(db_session, "material_group.add")[-1].details)["tercihler_tasindi"] == 1
    # grupta kalan tek kart (e) eski grubun kararlarını kart tercihi olarak alır
    assert {(p.supplier_id, p.preference) for p in db_session.query(MaterialSupplierPref)
            .filter(MaterialSupplierPref.item_id == e.id).all()} == {(ids["krk"], "preferred"),
                                                                    (ids["sepet"], "avoid")}


# ─── Mal kabul ipucu ────────────────────────────────────────────────────────

def test_receive_options_mismatch_candidates_and_phase_out(client: TestClient, db_session):
    ids = _seed(db_session)
    _login(client, "meltem")                                         # LabTech: inventory.receive var
    r = client.get(f"/api/inventory/receive-options?item_id={ids['a']}&supplier_id={ids['sepet']}")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["mismatch"] is True and b["card_supplier"]["name"] == "KRK GIDA"
    assert b["supplier"]["status"] == "phase_out" and b["supplier"]["status_reason"] == "1 kg satıyor"
    assert b["supplier"]["status_label"] == "Bitirilecek — alma"
    assert b["supplier_card"]["item_id"] == ids["b"]                 # bu tedarikçinin kartı
    assert b["candidates"][0]["item_id"] == ids["b"]
    assert b["candidates"][0]["reason"] == "Aynı malzeme grubu · seçilen tedarikçi"
    assert b["new_card"]["suggested_name"] == "JOJOBA YAĞI — HAMMADDE SEPETİ"
    assert b["new_card"]["join_group_of_item_id"] == ids["a"]
    assert b["new_card"]["conflict"] is not None                     # o adda kart zaten var
    # kartın kendi tedarikçisi → ipucu yok
    same = client.get(f"/api/inventory/receive-options?item_id={ids['a']}&supplier_id={ids['krk']}").json()
    assert same["mismatch"] is False and same["supplier"]["status"] == "normal"
    # yeni firma: kartı yok → supplier_card yok, yeni kart önerisi
    new = client.get(f"/api/inventory/receive-options?item_id={ids['a']}&supplier_id={ids['nat']}").json()
    assert new["mismatch"] is True and new["supplier_card"] is None
    assert new["new_card"]["suggested_name"] == "JOJOBA YAĞI — NATURALYA" and new["new_card"]["conflict"] is None


def test_receive_options_duplicate_firm_card_is_not_mismatch(client: TestClient, db_session):
    ids = _seed(db_session)
    twin = Supplier(name="KRK  GIDA", domain="cosmetics", purchase_status="normal")
    db_session.add(twin)
    db_session.commit()
    _login(client, "dogukan")
    b = client.get(f"/api/inventory/receive-options?item_id={ids['a']}&supplier_id={twin.id}").json()
    assert b["mismatch"] is False


def test_receive_options_domain_and_permission(client: TestClient, db_session):
    import bcrypt

    from database import User
    ids = _seed(db_session)
    db_session.add(User(username="depo", full_name="Depo", role="Staff",
                        password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode()))
    db_session.commit()
    _login(client, "dogukan")
    _switch(client, "supplement")
    assert client.get(f"/api/inventory/receive-options?item_id={ids['a']}"
                      f"&supplier_id={ids['krk']}").status_code == 404
    _switch(client, "cosmetics")
    _login(client, "depo")                                           # Staff: inventory.receive yok
    assert client.get(f"/api/inventory/receive-options?item_id={ids['a']}"
                      f"&supplier_id={ids['krk']}").status_code == 403


def test_item_with_pref_or_price_is_soft_deleted_not_500(authed_client: TestClient, db_session):
    from database import SupplierPrice
    ids = _seed(db_session)
    priced = Item(name="BOŞ KART", category="Hammadde", unit="g", domain="cosmetics")
    db_session.add(priced)
    db_session.flush()
    db_session.add_all([
        MaterialSupplierPref(domain="cosmetics", item_id=ids["solo"], supplier_id=ids["nat"],
                             preference="preferred"),
        SupplierPrice(item_id=priced.id, supplier_name="X", unit_price=1.0, price_unit="kg",
                      currency="USD", domain="cosmetics")])
    db_session.commit()
    for iid in (ids["solo"], priced.id):
        r = authed_client.delete(f"/api/items/{iid}", headers=ORIGIN)
        assert r.status_code == 200 and r.json()["soft_deleted"] is True, r.text
    db_session.expire_all()
    assert db_session.query(MaterialSupplierPref).count() == 1          # tercih kaydı korundu
