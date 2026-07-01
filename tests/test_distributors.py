"""
Distribütör hesap + fiyat yönetimi (personel API) testleri.
  • RBAC: distributors:* yetkisi gerekir (LabTech → 403)
  • create: User(role=Distributor) + Distributor profili; unique username; şifre gücü
  • prices: set/get; fiyat=0/boş → katalogdan çıkar
"""
from fastapi.testclient import TestClient

from database import User, Distributor, DistributorPrice, Item

_H = {"Origin": "http://testserver"}


def _item(db, name="Serum", sku="SR-1", domain="cosmetics", stock=10.0):
    it = Item(name=name, sku=sku, category="Bitmiş Ürün", unit="adet",
              current_stock=stock, cost_price=20.0, selling_price=0.0,
              domain=domain, is_active=True)
    db.add(it); db.commit(); db.refresh(it)
    return it


def test_list_requires_distributors_view(labtech_client: TestClient):
    assert labtech_client.get("/api/distributors").status_code == 403


def test_create_requires_perm(labtech_client: TestClient):
    r = labtech_client.post("/api/distributors",
                            json={"username": "d1", "password": "Distpass123", "company_name": "Bayi"},
                            headers=_H)
    assert r.status_code == 403


def test_create_distributor(authed_client: TestClient, db_session):
    r = authed_client.post("/api/distributors", headers=_H, json={
        "username": "bayi1", "password": "Distpass123", "company_name": "Bayi A.Ş.",
        "currency": "USD", "contact_name": "Ali", "country": "TR",
    })
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    d = db_session.query(Distributor).filter(Distributor.id == did).one()
    assert d.company_name == "Bayi A.Ş." and d.currency == "USD"
    u = db_session.query(User).filter(User.id == d.user_id).one()
    assert u.role == "Distributor" and u.username == "bayi1"


def test_create_duplicate_username(authed_client: TestClient):
    body = {"username": "dogukan", "password": "Distpass123", "company_name": "X"}
    assert authed_client.post("/api/distributors", json=body, headers=_H).status_code == 409


def test_create_weak_password(authed_client: TestClient):
    r = authed_client.post("/api/distributors", headers=_H,
                           json={"username": "bayi2", "password": "123", "company_name": "X"})
    assert r.status_code == 422


def test_prices_set_get_and_remove(authed_client: TestClient, db_session):
    it = _item(db_session)
    r = authed_client.post("/api/distributors", headers=_H,
                           json={"username": "bayi3", "password": "Distpass123", "company_name": "Bayi 3"})
    did = r.json()["id"]

    # fiyat tanımla
    r = authed_client.put(f"/api/distributors/{did}/prices", headers=_H,
                          json={"prices": [{"item_id": it.id, "unit_price": 42.5}]})
    assert r.status_code == 200, r.text
    assert db_session.query(DistributorPrice).filter_by(distributor_id=did, item_id=it.id).one().unit_price == 42.5

    # get → ürün listesinde fiyat görünür
    r = authed_client.get(f"/api/distributors/{did}/prices")
    assert r.status_code == 200
    rows = {x["item_id"]: x for x in r.json()["items"]}
    assert rows[it.id]["unit_price"] == 42.5

    # fiyat=0 → sil (katalogdan çıkar)
    r = authed_client.put(f"/api/distributors/{did}/prices", headers=_H,
                          json={"prices": [{"item_id": it.id, "unit_price": 0}]})
    assert r.status_code == 200
    assert db_session.query(DistributorPrice).filter_by(distributor_id=did, item_id=it.id).first() is None


def test_deactivate_blocks_login(authed_client: TestClient, client: TestClient, db_session):
    r = authed_client.post("/api/distributors", headers=_H,
                           json={"username": "bayi4", "password": "Distpass123", "company_name": "Bayi 4"})
    did = r.json()["id"]
    # pasifleştir → bağlı user da is_active=False
    authed_client.put(f"/api/distributors/{did}", headers=_H, json={"is_active": False})
    d = db_session.query(Distributor).filter(Distributor.id == did).one()
    u = db_session.query(User).filter(User.id == d.user_id).one()
    assert u.is_active is False
    # login denenince başarısız (aktif olmayan kullanıcı)
    r = client.post("/api/login", json={"username": "bayi4", "password": "Distpass123"}, headers=_H)
    assert r.status_code == 401
