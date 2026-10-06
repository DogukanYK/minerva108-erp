"""
"Aynı malzeme" grupları — /api/material-groups + kancalar (06.10.2026).

Sözleşmeler:
  • CRUD: oluştur / aç / listele (?q=) / yeniden adlandır / üye ekle-çıkar /
    dağıt; ad AKTİF gruplarda panel içinde TR-katlanmış tekil (409).
  • Kart tek grupta: başka gruptaki kart `move` olmadan eklenmez (409);
    aktif üyesi 2'nin altına düşen grup dağılır.
  • Bitmiş Ürün / pasif kart / hammadde+ambalaj karışımı reddedilir; birim
    farkı engellemez, `warning` döner.
  • Panel izolasyonu, RBAC (okuma items.view, yazma items.edit), audit.
  • Öneriler + "farklı" kaydı; kopya popup'ında "ayrı kalsın" kartları
    gruplar; merge_items grubu kazanana devreder; yeni kart
    `join_group_of_item_id` ile kaynağın grubuna katılır.
"""
import json

import bcrypt
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from core.item_merge import merge_items
from database import (AdminAuditLog, AppSetting, DuplicateItemDecision, Item,
                      MaterialGroup, Supplier, User)

_HDR = {"Origin": "http://testserver"}
API = "/api/material-groups"


def _sup(db, name, domain="cosmetics"):
    s = Supplier(name=name, domain=domain)
    db.add(s); db.flush()
    return s.id


def _card(db, name, unit="g", category="Hammadde", domain="cosmetics", active=True,
          group_id=None, supplier_id=None, stock=0.0):
    it = Item(name=name, category=category, unit=unit, current_stock=stock, domain=domain,
              is_active=active, material_group_id=group_id, supplier_id=supplier_id)
    db.add(it); db.flush()
    return it.id


def _group(db, name, ids, domain="cosmetics"):
    g = MaterialGroup(name=name, domain=domain)
    db.add(g); db.flush()
    for i in ids:
        db.query(Item).get(i).material_group_id = g.id
    db.flush()
    return g.id


def _stearyl(db):
    ids = (_card(db, "SETİL STEARİL ALKOL", "g", supplier_id=_sup(db, "TATLIDİLİMLER"), stock=3944),
           _card(db, "CETYL STEARYL ALCOHOL", "adet", supplier_id=_sup(db, "YİĞİTOGLU KİMYA")),
           _card(db, "CETEARYL ALCOHOL", "adet", supplier_id=_sup(db, "VESER KİMYEVİ")))
    db.commit()
    return ids


def _gid(db, item_id):
    db.expire_all()
    return db.query(Item).get(item_id).material_group_id


def _audit(db, action):
    db.expire_all()
    return db.query(AdminAuditLog).filter(AdminAuditLog.action == action).all()


def _login(client, db, username, role):
    db.add(User(username=username, full_name=username, role=role, is_active=True,
                password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode()))
    db.commit()
    r = client.post("/api/login", json={"username": username, "password": "minerva123"},
                    headers=_HDR)
    assert r.status_code == 200, r.text
    return client


# ─── CRUD ───────────────────────────────────────────────────────────────────

def test_create_get_list_rename(authed_client: TestClient, db_session: Session):
    a, b, c = _stearyl(db_session)
    r = authed_client.post(API, json={"name": "Stearil alkol", "item_ids": [a, b, a],
                                      "note": "lab kararı"}, headers=_HDR)
    assert r.status_code == 201, r.text
    d = r.json()
    gid = d["id"]
    assert d["name"] == "Stearil alkol" and d["note"] == "lab kararı" and d["source"] == "manual"
    assert {m["item_id"] for m in d["members"]} == {a, b} and d["active_count"] == 2
    # birim farkı engellemez, uyarır
    assert d["unit_mismatch"] is True and d["units"] == ["adet", "g"] and "adet, g" in d["warning"]
    assert {m["supplier_name"] for m in d["members"]} == {"TATLIDİLİMLER", "YİĞİTOGLU KİMYA"}
    assert _gid(db_session, a) == gid and _gid(db_session, c) is None
    assert _audit(db_session, "material_group.create")[0].target_id == gid

    g = authed_client.get(f"{API}/{gid}").json()
    assert g["id"] == gid and len(g["members"]) == 2

    lst = authed_client.get(API).json()
    assert lst["count"] == 1 and lst["groups"][0]["id"] == gid
    # ?q= grup adı, üye adı ve tedarikçi adında TR-katlanmış arar
    for q in ("stearil", "CETYL", "yiğitoğlu"):
        assert authed_client.get(API, params={"q": q}).json()["count"] == 1, q
    assert authed_client.get(API, params={"q": "jojoba"}).json()["count"] == 0

    r = authed_client.put(f"{API}/{gid}", json={"name": "Setil stearil alkol"}, headers=_HDR)
    assert r.status_code == 200 and r.json()["name"] == "Setil stearil alkol"
    log = _audit(db_session, "material_group.rename")[0]
    assert json.loads(log.details)["ad"] == ["Stearil alkol", "Setil stearil alkol"]


def test_name_unique_tr_folded(authed_client: TestClient, db_session: Session):
    a, b, c = _stearyl(db_session)
    d1, d2 = _card(db_session, "JOJOBA YAĞI"), _card(db_session, "JOJOBA YAGI")
    db_session.commit()
    assert authed_client.post(API, json={"name": "STEARİL ALKOL", "item_ids": [a, b]},
                              headers=_HDR).status_code == 201
    r = authed_client.post(API, json={"name": "stearil alkol", "item_ids": [d1, d2]}, headers=_HDR)
    assert r.status_code == 409 and r.json()["code"] == "name_conflict"
    r = authed_client.post(API, json={"name": "Jojoba", "item_ids": [d1, d2]}, headers=_HDR)
    gid2 = r.json()["id"]
    r = authed_client.put(f"{API}/{gid2}", json={"name": "Stearil Alkol"}, headers=_HDR)
    assert r.status_code == 409
    # dağılmış grubun adı serbest
    first = db_session.query(MaterialGroup).filter(MaterialGroup.name == "STEARİL ALKOL").one().id
    assert authed_client.delete(f"{API}/{first}", headers=_HDR).status_code == 200
    assert authed_client.put(f"{API}/{gid2}", json={"name": "Stearil Alkol"},
                             headers=_HDR).status_code == 200


def test_create_rejections(authed_client: TestClient, db_session: Session):
    a = _card(db_session, "GLİSERİN")
    b = _card(db_session, "GLYCERIN")
    off = _card(db_session, "GLYCERINE ESKİ", active=False)
    fin = _card(db_session, "GLİSERİNLİ KREM", category="Bitmiş Ürün", unit="adet")
    pkg = _card(db_session, "GLİSERİN ŞİŞESİ", category="Ambalaj", unit="adet")
    sup = _card(db_session, "GLİSERİN TAKVİYE", domain="supplement")
    db_session.commit()
    post = lambda ids: authed_client.post(API, json={"name": "Gliserin", "item_ids": ids},
                                          headers=_HDR)
    assert post([a, off]).status_code == 400
    assert post([a, fin]).status_code == 400
    assert post([a, pkg]).status_code == 400
    assert post([a, sup]).status_code == 404
    assert post([a, 999999]).status_code == 404
    assert post([a, a]).status_code == 400
    assert post([a]).status_code == 422
    assert db_session.query(MaterialGroup).count() == 0
    # Ambalaj kartları kendi aralarında gruplanabilir
    pkg2 = _card(db_session, "GLİSERİN ŞİŞESİ B", category="Ambalaj", unit="adet")
    db_session.commit()
    assert post([pkg, pkg2]).status_code == 201
    assert post([a, b]).status_code == 409                       # ad çakışması
    r = authed_client.post(API, json={"name": "Gliserin H", "item_ids": [a, b]}, headers=_HDR)
    assert r.status_code == 201 and r.json()["warning"] is None


def test_one_group_per_card_and_move(authed_client: TestClient, db_session: Session):
    a, b, c = _stearyl(db_session)
    x = _card(db_session, "SETEARİL ALKOL")
    g1 = _group(db_session, "Grup 1", [a, b])
    g2 = _group(db_session, "Grup 2", [c, x])
    db_session.commit()
    r = authed_client.post(f"{API}/{g1}/members", json={"item_id": c}, headers=_HDR)
    assert r.status_code == 409 and r.json()["code"] == "in_other_group"
    assert r.json()["group"] == {"id": g2, "name": "Grup 2"}
    assert _gid(db_session, c) == g2
    # move: c Grup 1'e geçer, Grup 2'de tek aktif kart kalır → dağılır
    r = authed_client.post(f"{API}/{g1}/members", json={"item_id": c, "move": True}, headers=_HDR)
    assert r.status_code == 200, r.text
    assert r.json()["dissolved_groups"] == [g2] and r.json()["active_count"] == 3
    assert _gid(db_session, c) == g1 and _gid(db_session, x) is None
    assert db_session.query(MaterialGroup).get(g2).is_active is False
    # zaten üyeyse no-op
    r = authed_client.post(f"{API}/{g1}/members", json={"item_id": c}, headers=_HDR)
    assert r.status_code == 200 and "zaten" in r.json()["message"]
    # oluştururken de: başka gruptaki kart move olmadan alınmaz
    y = _card(db_session, "CETYL STEARYL ALC.")
    db_session.commit()
    r = authed_client.post(API, json={"name": "Yeni", "item_ids": [y, a]}, headers=_HDR)
    assert r.status_code == 409 and r.json()["code"] == "in_other_group"
    assert _audit(db_session, "material_group.add")[0].target_id == g1


def test_add_member_rules(authed_client: TestClient, db_session: Session):
    a, b, c = _stearyl(db_session)
    pkg = _card(db_session, "ŞİŞE", category="Ambalaj", unit="adet")
    fin = _card(db_session, "KREM", category="Bitmiş Ürün", unit="adet")
    off = _card(db_session, "ESKİ ALKOL", active=False)
    g = _group(db_session, "Stearil", [a, b])
    db_session.commit()
    add = lambda i: authed_client.post(f"{API}/{g}/members", json={"item_id": i}, headers=_HDR)
    assert add(pkg).status_code == 400
    assert add(fin).status_code == 400
    assert add(off).status_code == 400
    assert add(424242).status_code == 404
    assert authed_client.post(f"{API}/9999/members", json={"item_id": c},
                              headers=_HDR).status_code == 404
    r = add(c)
    assert r.status_code == 200 and r.json()["unit_mismatch"] is True and r.json()["warning"]


def test_remove_member_auto_dissolves(authed_client: TestClient, db_session: Session):
    a, b, c = _stearyl(db_session)
    g = _group(db_session, "Stearil", [a, b, c])
    db_session.commit()
    r = authed_client.delete(f"{API}/{g}/members/{c}", headers=_HDR)
    assert r.status_code == 200 and r.json()["dissolved"] is False and r.json()["active_count"] == 2
    assert authed_client.delete(f"{API}/{g}/members/{c}", headers=_HDR).status_code == 404
    r = authed_client.delete(f"{API}/{g}/members/{b}", headers=_HDR)
    assert r.status_code == 200 and r.json()["dissolved"] is True
    assert _gid(db_session, a) is None and _gid(db_session, b) is None
    assert db_session.query(MaterialGroup).get(g).is_active is False
    assert authed_client.get(f"{API}/{g}").status_code == 404
    assert _audit(db_session, "material_group.remove") and _audit(db_session, "material_group.dissolve")


def test_pasif_member_does_not_count(authed_client: TestClient, db_session: Session):
    a, b, c = _stearyl(db_session)
    g = _group(db_session, "Stearil", [a, b, c])
    db_session.query(Item).get(c).is_active = False
    db_session.commit()
    # aktif üye 2 → 1: dağılır (pasif üye sayılmaz)
    r = authed_client.delete(f"{API}/{g}/members/{b}", headers=_HDR)
    assert r.json()["dissolved"] is True and _gid(db_session, c) is None


def test_dissolve_endpoint(authed_client: TestClient, db_session: Session):
    a, b, c = _stearyl(db_session)
    g = _group(db_session, "Stearil", [a, b, c])
    db_session.commit()
    r = authed_client.delete(f"{API}/{g}", headers=_HDR)
    assert r.status_code == 200 and sorted(r.json()["item_ids"]) == sorted([a, b, c])
    assert all(_gid(db_session, i) is None for i in (a, b, c))
    assert authed_client.delete(f"{API}/{g}", headers=_HDR).status_code == 404
    assert _audit(db_session, "material_group.dissolve")[0].target_id == g


# ─── Panel izolasyonu + RBAC ────────────────────────────────────────────────

def test_domain_isolation(authed_client: TestClient, db_session: Session):
    s1 = _card(db_session, "KAPSÜL A", domain="supplement")
    s2 = _card(db_session, "KAPSÜL B", domain="supplement")
    gs = _group(db_session, "Kapsül", [s1, s2], domain="supplement")
    k1, k2 = _card(db_session, "GLİSERİN"), _card(db_session, "GLYCERIN")
    db_session.commit()
    assert authed_client.get(API).json()["count"] == 0
    assert authed_client.get(f"{API}/{gs}").status_code == 404
    assert authed_client.put(f"{API}/{gs}", json={"name": "x"}, headers=_HDR).status_code == 404
    assert authed_client.delete(f"{API}/{gs}", headers=_HDR).status_code == 404
    assert authed_client.post(f"{API}/{gs}/members", json={"item_id": k1},
                              headers=_HDR).status_code == 404
    # aynı ad diğer panelde serbest
    assert authed_client.post(API, json={"name": "Kapsül", "item_ids": [k1, k2]},
                              headers=_HDR).status_code == 201
    authed_client.cookies.set("active_domain", "supplement")
    lst = authed_client.get(API).json()
    assert [g["id"] for g in lst["groups"]] == [gs]
    assert authed_client.post(f"{API}/{gs}/members", json={"item_id": k1},
                              headers=_HDR).status_code == 404


def test_rbac(client: TestClient, db_session: Session):
    a, b, c = _stearyl(db_session)
    g = _group(db_session, "Stearil", [a, b])
    db_session.commit()
    assert client.get(API).status_code == 401
    assert client.get(f"{API}/suggestions").status_code == 401
    # LabTech: items.view var, items.edit yok
    client.post("/api/login", json={"username": "meltem", "password": "minerva123"}, headers=_HDR)
    assert client.get(API).status_code == 200
    assert client.get(f"{API}/{g}").status_code == 200
    assert client.get(f"{API}/suggestions").status_code == 200
    assert client.post(API, json={"name": "X", "item_ids": [b, c]}, headers=_HDR).status_code == 403
    assert client.put(f"{API}/{g}", json={"name": "Y"}, headers=_HDR).status_code == 403
    assert client.post(f"{API}/{g}/members", json={"item_id": c}, headers=_HDR).status_code == 403
    assert client.delete(f"{API}/{g}/members/{b}", headers=_HDR).status_code == 403
    assert client.delete(f"{API}/{g}", headers=_HDR).status_code == 403
    assert client.post(f"{API}/suggestions/dismiss", json={"item_ids": [a, c]},
                       headers=_HDR).status_code == 403
    client.post("/api/logout", headers=_HDR)
    _login(client, db_session, "bayi_mg", "Distributor")
    assert client.get(API).status_code == 403
    client.post("/api/logout", headers=_HDR)
    # LabLead yazabilir
    client.post("/api/login", json={"username": "songul", "password": "minerva123"}, headers=_HDR)
    assert client.post(f"{API}/{g}/members", json={"item_id": c}, headers=_HDR).status_code == 200


# ─── Öneriler ───────────────────────────────────────────────────────────────

def test_suggestions_endpoint_and_dismiss(authed_client: TestClient, db_session: Session):
    a, b, c = _stearyl(db_session)
    j1 = _card(db_session, "JOJOBA YAĞI", supplier_id=_sup(db_session, "KRK GIDA"))
    j2 = _card(db_session, "JOJOBA YAĞI — NATURALYA", supplier_id=_sup(db_session, "NATURALYA"))
    _card(db_session, "E VİTAMİNİ"); _card(db_session, "C VİTAMİNİ")
    _card(db_session, "GLİSERİN TAKVİYE", domain="supplement")
    _card(db_session, "GLYCERIN TAKVİYE", domain="supplement")
    db_session.commit()
    d = authed_client.get(f"{API}/suggestions").json()
    by = {tuple(s["item_ids"]): s for s in d["suggestions"]}
    assert set(by) == {tuple(sorted((a, b, c))), (j1, j2)} and d["count"] == 2
    st = by[tuple(sorted((a, b, c)))]
    assert st["unit_mismatch"] is True and st["kind"] == "same_key" and st["action"] == "create"
    # "— NATURALYA" son eki paneldeki tedarikçi anahtarıyla atıldı → aynı anahtar
    assert by[(j1, j2)]["kind"] == "same_key"
    assert {i["supplier_name"] for i in by[(j1, j2)]["items"]} == {"KRK GIDA", "NATURALYA"}

    # kabul → grup kurulunca öneri kaybolur
    authed_client.post(API, json={"name": st["title"], "item_ids": st["item_ids"]}, headers=_HDR)
    # "farklı" → kaydedilir, öneri kaybolur
    r = authed_client.post(f"{API}/suggestions/dismiss", json={"item_ids": [j2, j1]}, headers=_HDR)
    assert r.status_code == 200 and r.json()["added_pairs"] == 1
    assert authed_client.get(f"{API}/suggestions").json()["count"] == 0
    row = db_session.query(AppSetting).filter(
        AppSetting.key == "material_groups.dismissed.cosmetics").one()
    assert json.loads(row.value) == [sorted([j1, j2])]
    assert _audit(db_session, "material_group.dismiss")
    # tekrar → yeni çift yok; başka paneldeki kart reddedilir
    r = authed_client.post(f"{API}/suggestions/dismiss", json={"item_ids": [j1, j2]}, headers=_HDR)
    assert r.json()["added_pairs"] == 0
    sup_id = db_session.query(Item).filter(Item.name == "GLİSERİN TAKVİYE").one().id
    assert authed_client.post(f"{API}/suggestions/dismiss", json={"item_ids": [j1, sup_id]},
                              headers=_HDR).status_code == 404
    # supplement paneli kendi önerisini görür, cosmetics kaydı ona sızmaz
    authed_client.cookies.set("active_domain", "supplement")
    assert authed_client.get(f"{API}/suggestions").json()["count"] == 1


def test_suggestions_include_pending_decisions(authed_client: TestClient, db_session: Session):
    a = _card(db_session, "LAURYL GLUCOSIDE")
    b = _card(db_session, "LAURİL GLUKOZİT KOPYA")
    db_session.add(DuplicateItemDecision(cluster_key="lg", title="Lauryl glucoside",
                                         item_ids=json.dumps([a, b])))
    db_session.commit()
    s = authed_client.get(f"{API}/suggestions").json()["suggestions"]
    assert len(s) == 1 and s[0]["kind"] == "pending_decision" and s[0]["item_ids"] == [a, b]
    dec = db_session.query(DuplicateItemDecision).one()
    assert s[0]["decision_id"] == dec.id and s[0]["title"] == "Lauryl glucoside"


# ─── Kancalar ───────────────────────────────────────────────────────────────

def test_dup_keep_groups_cards(authed_client: TestClient, db_session: Session):
    a, b, c = _stearyl(db_session)
    off = _card(db_session, "ESKİ STEARİL", active=False)
    dec = DuplicateItemDecision(cluster_key="st", title="Setil stearil alkol",
                                item_ids=json.dumps([a, b, c, off]))
    db_session.add(dec); db_session.commit()
    r = authed_client.post(f"/api/items/dup-decisions/{dec.id}", json={"action": "keep"},
                           headers=_HDR)
    assert r.status_code == 200, r.text
    mg = r.json()["material_group"]
    assert mg["name"] == "Setil stearil alkol" and "aynı malzeme grubunda" in r.json()["message"]
    grp = db_session.query(MaterialGroup).get(mg["id"])
    assert grp.source == "dup_kept" and grp.source_ref == dec.id
    assert {_gid(db_session, i) for i in (a, b, c)} == {mg["id"]} and _gid(db_session, off) is None
    log = _audit(db_session, "items.dup_keep")[0]
    assert json.loads(log.details)["ayni_malzeme_grubu"] == [mg["id"]]


def test_dup_keep_joins_existing_group(authed_client: TestClient, db_session: Session):
    a, b, c = _stearyl(db_session)
    x = _card(db_session, "DİĞER")
    g = _group(db_session, "Elle kurulmuş", [a, x])
    dec = DuplicateItemDecision(cluster_key="st2", title="Stearil", item_ids=json.dumps([a, b, c]))
    db_session.add(dec); db_session.commit()
    r = authed_client.post(f"/api/items/dup-decisions/{dec.id}", json={"action": "keep"},
                           headers=_HDR)
    assert r.json()["material_group"] == {"id": g, "name": "Elle kurulmuş"}
    assert {_gid(db_session, i) for i in (a, b, c, x)} == {g}
    assert db_session.query(MaterialGroup).count() == 1
    # iki ayrı gruba dağılmış küme → dokunulmaz
    y, z = _card(db_session, "Y"), _card(db_session, "Z")
    g2 = _group(db_session, "Başka", [y, z])
    w = _card(db_session, "W")
    dec2 = DuplicateItemDecision(cluster_key="st3", title="Karışık", item_ids=json.dumps([b, y, w]))
    db_session.add(dec2); db_session.commit()
    r = authed_client.post(f"/api/items/dup-decisions/{dec2.id}", json={"action": "keep"},
                           headers=_HDR)
    assert r.status_code == 200 and r.json()["material_group"] is None
    assert _gid(db_session, w) is None and _gid(db_session, y) == g2


def test_merge_transfers_group(db_session: Session):
    loser = _card(db_session, "JOJOBA YAGI")
    survivor = _card(db_session, "JOJOBA YAĞI")
    other = _card(db_session, "JOJOBA YAĞI — NATURALYA")
    g = _group(db_session, "Jojoba", [loser, other])
    db_session.commit()
    s = merge_items(db_session, loser, survivor, "test")
    db_session.commit()
    assert s["material_group_id"] == g
    assert _gid(db_session, survivor) == g and _gid(db_session, other) == g
    assert _gid(db_session, loser) == g                       # pasif üye (iz)
    assert db_session.query(MaterialGroup).get(g).is_active is True


def test_merge_within_group_dissolves_pair(db_session: Session):
    loser, survivor = _card(db_session, "A KOPYA"), _card(db_session, "A ASIL")
    g = _group(db_session, "A", [loser, survivor])
    db_session.commit()
    s = merge_items(db_session, loser, survivor, "test")
    db_session.commit()
    assert s["material_group_id"] is None
    assert db_session.query(MaterialGroup).get(g).is_active is False
    assert _gid(db_session, survivor) is None


def test_merge_keeps_survivor_group(db_session: Session):
    loser, l2 = _card(db_session, "L"), _card(db_session, "L2")
    survivor, s2 = _card(db_session, "S"), _card(db_session, "S2")
    gl = _group(db_session, "GL", [loser, l2])
    gs = _group(db_session, "GS", [survivor, s2])
    db_session.commit()
    merge_items(db_session, loser, survivor, "test")
    db_session.commit()
    assert _gid(db_session, survivor) == gs
    # kaybedenin grubunda tek aktif kart kaldı → dağıldı
    assert db_session.query(MaterialGroup).get(gl).is_active is False and _gid(db_session, l2) is None


def test_create_item_joins_source_group(authed_client: TestClient, db_session: Session):
    nat = _sup(db_session, "NATURALYA")
    src = _card(db_session, "JOJOBA YAĞI", supplier_id=_sup(db_session, "KRK GIDA"))
    db_session.commit()
    body = {"name": "JOJOBA YAĞI — NATURALYA", "category": "Hammadde", "unit": "g",
            "supplier_id": nat, "join_group_of_item_id": src}
    r = authed_client.post("/api/items", json=body, headers=_HDR)
    assert r.status_code == 201, r.text
    new_id, mg = r.json()["id"], r.json()["material_group"]
    assert mg and _gid(db_session, src) == mg["id"] == _gid(db_session, new_id)
    assert r.json()["warning"] is None
    grp = db_session.query(MaterialGroup).get(mg["id"])
    assert grp.name == "JOJOBA YAĞI" and grp.source == "convert"
    assert _audit(db_session, "material_group.add")
    # kaynağın grubu varsa ona katılır; birim farkı uyarır
    body2 = dict(body, name="JOJOBA YAĞI — PERA", unit="adet", supplier_id=None)
    r = authed_client.post("/api/items", json=body2, headers=_HDR)
    assert r.json()["material_group"]["id"] == mg["id"] and r.json()["warning"]
    # alan yoksa eski davranış
    r = authed_client.post("/api/items", json={"name": "BAĞIMSIZ", "category": "Hammadde",
                                               "unit": "g"}, headers=_HDR)
    assert r.status_code == 201 and r.json()["material_group"] is None
    assert _gid(db_session, r.json()["id"]) is None


def test_create_item_join_rejections(authed_client: TestClient, db_session: Session):
    off = _card(db_session, "PASİF KAYNAK", active=False)
    other = _card(db_session, "TAKVİYE KAYNAK", domain="supplement")
    raw = _card(db_session, "HAMMADDE KAYNAK")
    db_session.commit()
    before = db_session.query(Item).count()
    for src, cat in ((off, "Hammadde"), (other, "Hammadde"), (424242, "Hammadde"),
                     (raw, "Ambalaj"), (raw, "Bitmiş Ürün")):
        r = authed_client.post("/api/items", json={"name": f"YENİ {src} {cat}", "category": cat,
                                                   "unit": "g", "join_group_of_item_id": src},
                               headers=_HDR)
        assert r.status_code == 400, (src, cat, r.text)
    db_session.expire_all()
    assert db_session.query(Item).count() == before


def test_convert_options_uses_material_key(authed_client: TestClient, db_session: Session):
    """Numune çevirme adayları TR/EN eşanlamlı adı da tanır (P1'in basit ad
    içerme kuralı CETYL STEARYL ALCOHOL'ü SETİL STEARİL ALKOL'e bağlamıyordu)."""
    from database import Inventory
    nat = _sup(db_session, "NATURALYA")
    src = _card(db_session, "SETİL STEARİL ALKOL", "g")
    en = _card(db_session, "CETYL STEARYL ALCOHOL", "g", supplier_id=nat)
    _card(db_session, "CETEARYL ALCOHOL", "adet")                     # birim ailesi farklı
    _card(db_session, "STEARİK ASİT", "g")                            # farklı malzeme
    inv = Inventory(item_id=src, lot_number="N1", quantity=50, is_sample=True,
                    supplier_id=nat, status="APPROVED", domain="cosmetics")
    db_session.add(inv); db_session.commit()
    d = authed_client.get(f"/api/inventory/samples/{inv.id}/convert-options").json()
    assert [c["item_id"] for c in d["candidates"]] == [en]
    assert d["candidates"][0]["reason"] == "Numunenin tedarikçisi · benzer ad"


def test_labtech_new_card_joins_group_by_design(labtech_client: TestClient, db_session: Session):
    """Bilinçli istisna: numune girişinin "bu tedarikçi için yeni kart aç" akışı
    items.create ile (LabTech) yeni kartı kaynağın grubuna katar; var olan
    kartları gruba ekleme (/members) items.edit ister."""
    nat = _sup(db_session, "NATURALYA")
    a, b, c = _stearyl(db_session)
    g = _group(db_session, "Stearil", [a, b])
    db_session.commit()
    r = labtech_client.post("/api/items", json={"name": "CETYL STEARYL ALCOHOL — NATURALYA",
                                                "category": "Hammadde", "unit": "g",
                                                "supplier_id": nat, "join_group_of_item_id": a},
                            headers=_HDR)
    assert r.status_code == 201, r.text
    assert r.json()["material_group"]["id"] == g == _gid(db_session, r.json()["id"])
    assert labtech_client.post(f"{API}/{g}/members", json={"item_id": c},
                               headers=_HDR).status_code == 403
    assert _gid(db_session, c) is None


# ─── Kart silme / tür değişikliği grubu budar ──────────────────────────────

def test_delete_item_prunes_group(authed_client: TestClient, db_session: Session):
    from database import Inventory
    a, b, c = _stearyl(db_session)
    g = _group(db_session, "Stearil", [a, b])
    db_session.add(Inventory(item_id=b, lot_number="YK-1", quantity=1, status="APPROVED",
                             domain="cosmetics"))                       # hareketi var → arşiv
    db_session.commit()
    r = authed_client.delete(f"/api/items/{b}", headers=_HDR)
    assert r.status_code == 200 and r.json()["soft_deleted"] is True
    db_session.expire_all()
    assert db_session.query(MaterialGroup).get(g).is_active is False
    assert _gid(db_session, a) is None and _gid(db_session, b) is None
    ev = _audit(db_session, "material_group.dissolve")
    assert len(ev) == 1 and ev[0].target_id == g and json.loads(ev[0].details)["sebep"] == "kart arşivlendi"
    # ad yeniden kullanılabilir
    assert authed_client.post(API, json={"name": "Stearil", "item_ids": [a, c]},
                              headers=_HDR).status_code == 201
    # hard-delete (bağsız kart) de budar; 3 üyeli grupta 2 aktif kalırsa grup durur
    x, y, z = _card(db_session, "X1"), _card(db_session, "X2"), _card(db_session, "X3")
    g2 = _group(db_session, "Üçlü", [x, y, z])
    db_session.commit()
    r = authed_client.delete(f"/api/items/{z}", headers=_HDR)
    assert r.status_code == 200 and r.json()["soft_deleted"] is False
    db_session.expire_all()
    assert db_session.query(MaterialGroup).get(g2).is_active is True
    assert authed_client.delete(f"/api/items/{y}", headers=_HDR).status_code == 200
    db_session.expire_all()
    assert db_session.query(MaterialGroup).get(g2).is_active is False and _gid(db_session, x) is None


def test_bulk_delete_prunes_groups(authed_client: TestClient, db_session: Session):
    from database import Inventory
    a, b, c = _stearyl(db_session)
    g = _group(db_session, "Stearil", [a, b, c])
    x, y = _card(db_session, "Y1"), _card(db_session, "Y2")
    g2 = _group(db_session, "İkili", [x, y])
    db_session.add(Inventory(item_id=b, lot_number="L", quantity=1, status="APPROVED",
                             domain="cosmetics"))
    db_session.commit()
    r = authed_client.post("/api/items/bulk-delete", json={"item_ids": [b, c, y]}, headers=_HDR)
    assert r.status_code == 200, r.text
    assert (r.json()["soft"], r.json()["hard"]) == (1, 2)
    db_session.expire_all()
    assert db_session.query(MaterialGroup).get(g).is_active is False      # tek aktif (a) kaldı
    assert db_session.query(MaterialGroup).get(g2).is_active is False
    assert _gid(db_session, a) is None and _gid(db_session, x) is None
    assert {e.target_id for e in _audit(db_session, "material_group.dissolve")} == {g, g2}


def test_update_item_kind_change_blocked_in_group(authed_client: TestClient, db_session: Session):
    a, b, _c = _stearyl(db_session)
    _group(db_session, "Stearil", [a, b])
    db_session.commit()
    body = {"name": "CETYL STEARYL ALCOHOL", "category": "Ambalaj", "unit": "adet"}
    r = authed_client.put(f"/api/items/{b}", json=body, headers=_HDR)
    assert r.status_code == 400 and "gruptan çıkarın" in r.json()["detail"]
    db_session.expire_all()
    assert db_session.query(Item).get(b).category == "Hammadde"
    # aynı tür içinde (Hammadde → Kimyasal) değişiklik serbest; grupsuz kart da serbest
    r = authed_client.put(f"/api/items/{b}", json=dict(body, category="Kimyasal"), headers=_HDR)
    assert r.status_code == 200, r.text
    r = authed_client.put(f"/api/items/{_c}", json=dict(body, name="CETEARYL ALCOHOL"), headers=_HDR)
    assert r.status_code == 200, r.text


def test_dup_merge_audits_dissolved_group(authed_client: TestClient, db_session: Session):
    """Kopya popup'ında birleştirme kaybedenin grubunu tek karta indirip
    dağıtırsa iz kalır: dup_merge detayında + material_group.dissolve."""
    loser, l2 = _card(db_session, "L KOPYA"), _card(db_session, "L2")
    survivor, s2 = _card(db_session, "L ASIL"), _card(db_session, "S2")
    gl = _group(db_session, "GL", [loser, l2])
    _group(db_session, "GS", [survivor, s2])
    dec = DuplicateItemDecision(cluster_key="m1", title="L", item_ids=json.dumps([loser, survivor]))
    db_session.add(dec); db_session.commit()
    r = authed_client.post(f"/api/items/dup-decisions/{dec.id}",
                           json={"action": "merge", "target_item_id": survivor}, headers=_HDR)
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.query(MaterialGroup).get(gl).is_active is False and _gid(db_session, l2) is None
    ev = _audit(db_session, "material_group.dissolve")
    assert len(ev) == 1 and ev[0].target_id == gl and ev[0].target_name == "GL"
    d = json.loads(ev[0].details)
    assert (d["sebep"], d["kaybeden"], d["kazanan"]) == ("kart birleştirme", loser, survivor)
    assert json.loads(_audit(db_session, "items.dup_merge")[0].details)["dagilan_gruplar"] == [gl]
