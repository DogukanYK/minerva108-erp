"""
Kargo / Sevkiyat — ertelenmiş stok (takip no atanınca düşer) testleri.

  • create kargo → stok DÜŞMEZ, status='preparing', KRG-YYYY-NNNNN
  • ship (takip no) → stok düşer + Transaction(Output) + status='shipped'
  • çift ship → 400, stok TEK kez düşer (idempotent — çift-düşüm guard'ı)
  • ship anında stok yetersiz → 400, hep-ya-hiç (stok değişmez, preparing kalır)
  • cancel (preparing) → stok değişmez; shipped sonrası cancel → 400
  • alıcı OPSİYONEL; boş takip no reddedilir
  • RBAC: inventory.adjust olmadan create + ship 403
  • bekleyenler listesi + hazırlık/master PDF 200 (%PDF)
"""
from fastapi.testclient import TestClient

from database import Item, Transaction, Delivery

_H = {"Origin": "http://testserver"}


def _item(db, stock=10, barcode="869111100001"):
    it = Item(name=f"Kargo Ürün {barcode[-4:]}", sku=f"sku-{barcode[-5:]}", category="Bitmiş Ürün",
              unit="adet", current_stock=stock, barcode=barcode, domain="cosmetics")
    db.add(it); db.commit(); db.refresh(it)
    return it


def _create_kargo(ac, item_id, qty=3, recipient=None):
    body = {"delivery_type": "kargo", "doc_lang": "TR", "items": [{"item_id": item_id, "quantity": qty}]}
    if recipient is not None:
        body["recipient_name"] = recipient
    return ac.post("/api/delivery", headers=_H, json=body)


def _outputs(db, item_id):
    return db.query(Transaction).filter(Transaction.item_id == item_id,
                                        Transaction.transaction_type == "Output").all()


def test_create_kargo_does_not_decrement_stock(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10, barcode="869111100001")
    r = _create_kargo(authed_client, it.id, 3, "Ayşe Yılmaz")
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["document_no"].startswith("KRG-")
    assert b["status"] == "preparing" and b["delivery_type"] == "kargo"
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 10      # DÜŞMEDİ
    assert len(_outputs(db_session, it.id)) == 0


def test_kargo_recipient_optional(authed_client: TestClient, db_session):
    it = _item(db_session, stock=5, barcode="869111100002")
    r = _create_kargo(authed_client, it.id, 1)                        # alıcı yok
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "preparing"


def test_ship_decrements_stock_and_sets_tracking(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10, barcode="869111100003")
    did = _create_kargo(authed_client, it.id, 4, "Veli").json()["id"]
    r = authed_client.post(f"/api/delivery/{did}/ship", headers=_H,
                           json={"tracking_no": "YT123", "carrier": "Yurtiçi"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "shipped" and r.json()["tracking_no"] == "YT123"
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 6       # 10 - 4
    tx = _outputs(db_session, it.id)
    assert len(tx) == 1 and tx[0].quantity == 4
    d = db_session.query(Delivery).get(did)
    assert d.status == "shipped" and d.tracking_no == "YT123" and d.carrier == "Yurtiçi"
    assert d.shipped_at is not None


def test_double_ship_is_idempotent(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10, barcode="869111100004")
    did = _create_kargo(authed_client, it.id, 3, "X").json()["id"]
    assert authed_client.post(f"/api/delivery/{did}/ship", headers=_H,
                              json={"tracking_no": "A1"}).status_code == 200
    r2 = authed_client.post(f"/api/delivery/{did}/ship", headers=_H, json={"tracking_no": "A2"})
    assert r2.status_code == 400                                      # zaten kargolandı
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 7       # TEK düşüş (10-3)
    assert len(_outputs(db_session, it.id)) == 1


def test_ship_insufficient_stock_all_or_nothing(authed_client: TestClient, db_session):
    it = _item(db_session, stock=2, barcode="869111100005")
    did = _create_kargo(authed_client, it.id, 5, "Y").json()["id"]    # create stok kontrol etmez
    r = authed_client.post(f"/api/delivery/{did}/ship", headers=_H, json={"tracking_no": "B1"})
    assert r.status_code == 400 and "yetersiz stok" in r.json()["detail"].lower()
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 2       # değişmedi
    assert db_session.query(Delivery).get(did).status == "preparing"  # hâlâ bekliyor


def test_cancel_preparing_keeps_stock(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10, barcode="869111100006")
    did = _create_kargo(authed_client, it.id, 3, "Z").json()["id"]
    r = authed_client.post(f"/api/delivery/{did}/cancel", headers=_H, json={"reason": "vazgeçildi"})
    assert r.status_code == 200 and r.json()["status"] == "canceled"
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 10
    assert db_session.query(Delivery).get(did).status == "canceled"


def test_cancel_after_ship_rejected(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10, barcode="869111100007")
    did = _create_kargo(authed_client, it.id, 2, "W").json()["id"]
    authed_client.post(f"/api/delivery/{did}/ship", headers=_H, json={"tracking_no": "C1"})
    r = authed_client.post(f"/api/delivery/{did}/cancel", headers=_H, json={"reason": "x"})
    assert r.status_code == 400


def test_ship_empty_tracking_rejected(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10, barcode="869111100008")
    did = _create_kargo(authed_client, it.id, 1, "Q").json()["id"]
    assert authed_client.post(f"/api/delivery/{did}/ship", headers=_H,
                              json={"tracking_no": "   "}).status_code in (400, 422)   # strip→boş
    assert authed_client.post(f"/api/delivery/{did}/ship", headers=_H,
                              json={"tracking_no": ""}).status_code == 422             # min_length
    db_session.expire_all()
    assert db_session.query(Delivery).get(did).status == "preparing"


def test_pending_shipments_and_pdfs(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10, barcode="869111100009")
    did = _create_kargo(authed_client, it.id, 3, "Master Test").json()["id"]
    pend = authed_client.get("/api/delivery/shipments").json()
    assert any(p["id"] == did for p in pend["pending"])
    r1 = authed_client.get(f"/api/delivery/{did}/packing-list")
    assert r1.status_code == 200 and r1.content[:4] == b"%PDF"
    r2 = authed_client.get("/api/delivery/shipments/packing-list")
    assert r2.status_code == 200 and r2.content[:4] == b"%PDF"


def test_delivery_document_blocked_until_shipped(authed_client: TestClient, db_session):
    """Teslim belgesi ('eksiksiz teslim alınmıştır') hazırlanıyor kargo için üretilmez."""
    it = _item(db_session, stock=10, barcode="869111100011")
    did = _create_kargo(authed_client, it.id, 2, "Doc Test").json()["id"]
    # preparing → 403
    assert authed_client.get(f"/api/delivery/{did}/document").status_code == 403
    authed_client.post(f"/api/delivery/{did}/ship", headers=_H, json={"tracking_no": "D1"})
    # shipped → 200 (belge artık geçerli)
    r = authed_client.get(f"/api/delivery/{did}/document")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"


def test_kargo_and_ship_require_adjust_permission(labtech_client: TestClient):
    # require_permission dependency → handler hiç çalışmadan 403 (id var olmasa da)
    assert labtech_client.post("/api/delivery/1/ship", headers=_H,
                               json={"tracking_no": "x"}).status_code == 403
    assert labtech_client.post("/api/delivery", headers=_H, json={
        "delivery_type": "kargo", "doc_lang": "TR",
        "items": [{"item_id": 1, "quantity": 1}]}).status_code == 403
