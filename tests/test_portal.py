"""
Distribütör sipariş portalı (dağıtıcı tarafı) testleri.
  • RBAC: Distributor yalnız /api/portal/*; ERP uçları 403
  • katalog: yalnız fiyatlı ürün + var/yok (maliyet/kesin stok yok)
  • sipariş: SUNUCU fiyatıyla PENDING Quotation; fiyatsız ürün 400
  • personel onay → CONFIRMED + stok düşer + Transaction; red → REJECTED (stok sabit)
  • distribütör yalnız kendi siparişini görür
"""
from fastapi.testclient import TestClient

from database import Item, Distributor, DistributorPrice, Quotation, Transaction

_H = {"Origin": "http://testserver"}


def _item(db, name, sku, stock=50.0, domain="cosmetics"):
    it = Item(name=name, sku=sku, category="Bitmiş Ürün", unit="adet",
              current_stock=stock, cost_price=20.0, domain=domain, is_active=True)
    db.add(it); db.commit(); db.refresh(it)
    return it


def _make_dist(authed_client, username, company, item_id=None, price=100.0):
    """authed_client (SuperAdmin) ile distribütör + (opsiyonel) fiyat kur."""
    r = authed_client.post("/api/distributors", headers=_H, json={
        "username": username, "password": "Distpass123", "company_name": company, "currency": "TRY"})
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    if item_id is not None:
        r = authed_client.put(f"/api/distributors/{did}/prices", headers=_H,
                              json={"prices": [{"item_id": item_id, "unit_price": price}]})
        assert r.status_code == 200, r.text
    return did


def _login(client, username, password="Distpass123"):
    r = client.post("/api/login", json={"username": username, "password": password}, headers=_H)
    assert r.status_code == 200, r.text


def test_products_only_priced_with_availability(authed_client: TestClient, db_session):
    sold = _item(db_session, "Serum", "SR-1", stock=5)
    _item(db_session, "Krem (fiyatsız)", "KR-1", stock=5)      # fiyatı yok → görünmez
    out = _item(db_session, "Tükenen", "TK-1", stock=0)
    _make_dist(authed_client, "bayi1", "Bayi 1", item_id=sold.id, price=120.0)
    # tükenen ürüne de fiyat ver (var/yok testi)
    d = db_session.query(Distributor).filter(Distributor.company_name == "Bayi 1").one()
    db_session.add(DistributorPrice(distributor_id=d.id, item_id=out.id, unit_price=80.0)); db_session.commit()

    _login(authed_client, "bayi1")   # aynı client'ta re-login
    r = authed_client.get("/api/portal/products")
    assert r.status_code == 200
    prods = {p["item_id"]: p for p in r.json()["products"]}
    assert set(prods.keys()) == {sold.id, out.id}     # fiyatsız ürün yok
    assert prods[sold.id]["unit_price"] == 120.0 and prods[sold.id]["available"] is True
    assert prods[out.id]["available"] is False        # stok 0 → yok
    assert "cost_price" not in prods[sold.id] and "current_stock" not in prods[sold.id]


def test_distributor_cannot_access_erp(authed_client: TestClient, db_session):
    _make_dist(authed_client, "bayi2", "Bayi 2")
    _login(authed_client, "bayi2")
    assert authed_client.get("/api/items").status_code == 403
    assert authed_client.get("/api/distributors").status_code == 403
    assert authed_client.get("/api/quotations").status_code == 403


def test_order_uses_server_price_and_is_pending(authed_client: TestClient, db_session):
    it = _item(db_session, "Serum", "SR-1", stock=50)
    did = _make_dist(authed_client, "bayi3", "Bayi 3", item_id=it.id, price=100.0)
    _login(authed_client, "bayi3")
    r = authed_client.post("/api/portal/orders", headers=_H,
                           json={"items": [{"item_id": it.id, "quantity": 3}]})
    assert r.status_code == 201, r.text
    order_no = r.json()["order_number"]
    assert order_no.startswith("SIP-")
    q = db_session.query(Quotation).filter(Quotation.quote_number == order_no).one()
    assert q.status == "PENDING" and q.distributor_id == did
    assert q.total_amount == 300.0          # 3 × sunucu fiyatı 100 (istemci fiyat gönderemez)
    assert q.items[0].unit_price_foreign == 100.0
    # stok henüz düşmedi (onay bekliyor)
    db_session.expire_all()
    assert db_session.query(Item).filter(Item.id == it.id).one().current_stock == 50


def test_order_rejects_unpriced_item(authed_client: TestClient, db_session):
    it = _item(db_session, "Serum", "SR-1")
    other = _item(db_session, "Başka", "BS-1")
    _make_dist(authed_client, "bayi4", "Bayi 4", item_id=it.id, price=100.0)
    _login(authed_client, "bayi4")
    r = authed_client.post("/api/portal/orders", headers=_H,
                           json={"items": [{"item_id": other.id, "quantity": 1}]})
    assert r.status_code == 400


def test_staff_approve_deducts_stock(authed_client: TestClient, db_session):
    it = _item(db_session, "Serum", "SR-1", stock=50)
    _make_dist(authed_client, "bayi5", "Bayi 5", item_id=it.id, price=100.0)
    _login(authed_client, "bayi5")
    oid = authed_client.post("/api/portal/orders", headers=_H,
                             json={"items": [{"item_id": it.id, "quantity": 4}]}).json()["id"]
    # personel (SuperAdmin) onaylar
    _login(authed_client, "dogukan", "minerva123")
    r = authed_client.post(f"/api/quotations/{oid}/confirm", headers=_H)
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.query(Item).filter(Item.id == it.id).one().current_stock == 46
    assert db_session.query(Quotation).filter(Quotation.id == oid).one().status == "CONFIRMED"
    assert db_session.query(Transaction).filter(Transaction.item_id == it.id,
                                                Transaction.transaction_type == "Output").count() == 1


def test_staff_reject_keeps_stock(authed_client: TestClient, db_session):
    it = _item(db_session, "Serum", "SR-1", stock=50)
    _make_dist(authed_client, "bayi6", "Bayi 6", item_id=it.id, price=100.0)
    _login(authed_client, "bayi6")
    oid = authed_client.post("/api/portal/orders", headers=_H,
                             json={"items": [{"item_id": it.id, "quantity": 4}]}).json()["id"]
    _login(authed_client, "dogukan", "minerva123")
    r = authed_client.post(f"/api/quotations/{oid}/reject", headers=_H, json={"reason": "Stok yetersiz"})
    assert r.status_code == 200, r.text
    db_session.expire_all()
    q = db_session.query(Quotation).filter(Quotation.id == oid).one()
    assert q.status == "REJECTED" and q.reject_reason == "Stok yetersiz"
    assert db_session.query(Item).filter(Item.id == it.id).one().current_stock == 50


def test_distributor_sees_only_own_orders(authed_client: TestClient, db_session):
    it = _item(db_session, "Serum", "SR-1", stock=50)
    _make_dist(authed_client, "bayiA", "Bayi A", item_id=it.id, price=100.0)
    _make_dist(authed_client, "bayiB", "Bayi B", item_id=it.id, price=90.0)
    _login(authed_client, "bayiA")
    authed_client.post("/api/portal/orders", headers=_H, json={"items": [{"item_id": it.id, "quantity": 1}]})
    _login(authed_client, "bayiB")
    r = authed_client.get("/api/portal/orders")
    assert r.status_code == 200 and r.json() == []      # B, A'nın siparişini görmez
