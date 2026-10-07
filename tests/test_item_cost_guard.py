"""
Kart kaydında maliyet (cost_price) koruması — 07.10.2026 hatası.

items.html finans dışı kullanıcıda maliyet alanını HİÇ göndermiyor; Pydantic
varsayılanı 0.0 olduğu için `update_item` (`data.cost_price or 0.0`) LabLead
min stoğu değiştirip kaydedince Manager'ın girdiği maliyeti 0'lıyordu.
Artık maliyet yalnız finans rolü alanı fiilen gönderdiyse yazılır; create'te
finans dışı kullanıcının cost_price'ı yok sayılır; "Geri al" maliyete yalnız
o düzenleme ona dokunduysa döner.  Ayrıca update_item aktif panelle sınırlı.
"""
from fastapi.testclient import TestClient

from database import Item

_H = {"Origin": "http://testserver"}


def _login(c: TestClient, username: str) -> TestClient:
    c.post("/api/logout", headers=_H)
    r = c.post("/api/login", json={"username": username, "password": "minerva123"}, headers=_H)
    assert r.status_code == 200, r.text
    return c


def _card(db, cost=12.5, domain="cosmetics") -> int:
    it = Item(name="ARGAN YAĞI", category="Hammadde", unit="g", current_stock=0,
              min_stock_level=100, cost_price=cost, domain=domain)
    db.add(it)
    db.commit()
    return it.id


def _cost(db, iid) -> float:
    db.expire_all()
    return db.get(Item, iid).cost_price


_BODY = {"name": "ARGAN YAĞI", "category": "Hammadde", "unit": "g"}


def test_lablead_save_without_cost_keeps_cost(client: TestClient, db_session):
    iid = _card(db_session)
    _login(client, "songul")
    r = client.put(f"/api/items/{iid}", headers=_H, json={**_BODY, "min_stock": 250})
    assert r.status_code == 200, r.text
    assert _cost(db_session, iid) == 12.5
    assert db_session.get(Item, iid).min_stock_level == 250


def test_lablead_sent_cost_is_ignored(client: TestClient, db_session):
    iid = _card(db_session)
    _login(client, "songul")
    r = client.put(f"/api/items/{iid}", headers=_H, json={**_BODY, "cost_price": 99})
    assert r.status_code == 200
    assert _cost(db_session, iid) == 12.5


def test_manager_cost_is_saved(client: TestClient, db_session):
    iid = _card(db_session)
    _login(client, "isik")                                     # Manager (finans)
    r = client.put(f"/api/items/{iid}", headers=_H, json={**_BODY, "cost_price": 8.0})
    assert r.status_code == 200
    assert _cost(db_session, iid) == 8.0
    # Manager de alanı göndermeden kaydederse maliyet korunur
    client.put(f"/api/items/{iid}", headers=_H, json={**_BODY, "min_stock": 5})
    assert _cost(db_session, iid) == 8.0


def test_lablead_create_ignores_cost(client: TestClient, db_session):
    _login(client, "songul")
    r = client.post("/api/items", headers=_H,
                    json={"name": "YENİ HAMMADDE", "category": "Hammadde", "unit": "g",
                          "cost_price": 99})
    assert r.status_code == 201, r.text
    assert _cost(db_session, r.json()["id"]) == 0.0


def test_undo_does_not_revert_cost_it_did_not_touch(client: TestClient, db_session):
    iid = _card(db_session, cost=5.0)
    _login(client, "songul")
    assert client.put(f"/api/items/{iid}", headers=_H,
                      json={**_BODY, "min_stock": 300}).status_code == 200
    _login(client, "isik")
    assert client.put(f"/api/items/{iid}", headers=_H,
                      json={**_BODY, "min_stock": 300, "cost_price": 8.0}).status_code == 200
    _login(client, "songul")
    r = client.post("/api/undo", headers=_H)
    assert r.status_code == 200, r.text
    assert _cost(db_session, iid) == 8.0                       # Manager'ın maliyeti kaldı
    assert db_session.get(Item, iid).min_stock_level == 100    # LabLead'in düzenlemesi geri alındı


def test_update_item_other_domain_404(authed_client: TestClient, db_session):
    iid = _card(db_session, domain="supplement")
    r = authed_client.put(f"/api/items/{iid}", headers=_H, json={**_BODY, "cost_price": 0})
    assert r.status_code == 404
    assert _cost(db_session, iid) == 12.5
