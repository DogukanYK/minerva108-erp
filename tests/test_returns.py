"""
Ürün iadesi — geri gelenlerin stoğa alınması testleri.

  • belgeye bağlı SAĞLAM iade → stok artar + Transaction(Input, 'İade (RET-') + restocked
  • HASARLI iade → stok DEĞİŞMEZ, yeni Transaction yazılmaz (fire izi kayıtta)
  • aşırı-iade guard: teslim 5 → 3 iade OK → 3 daha 400; remaining doğru
  • serbest iade (delivery_id yok) → stok artar
  • stoğu düşmemiş (preparing kargo) teslimata iade → 400
  • RBAC: inventory.adjust olmadan 403
  • iade belgesi PDF (%PDF) + belge no RET-YYYY-NNNNN
"""
from fastapi.testclient import TestClient

from database import Item, Transaction, ProductReturn

_H = {"Origin": "http://testserver"}


def _item(db, stock=10, barcode="869222200001"):
    it = Item(name=f"İade Ürün {barcode[-4:]}", sku=f"sku-r{barcode[-5:]}", category="Bitmiş Ürün",
              unit="adet", current_stock=stock, barcode=barcode, domain="cosmetics")
    db.add(it); db.commit(); db.refresh(it)
    return it


def _gift(ac, item_id, qty=5, name="Ayşe Yılmaz"):
    """Anında hediye teslimatı — stok hemen düşer; iade testlerinin kaynağı."""
    r = ac.post("/api/delivery", headers=_H, json={
        "recipient_name": name, "delivery_type": "hediye", "doc_lang": "TR",
        "items": [{"item_id": item_id, "quantity": qty}]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _remaining(ac, delivery_id):
    r = ac.get(f"/api/returns/delivery/{delivery_id}/remaining")
    assert r.status_code == 200, r.text
    return r.json()


def _input_txs(db, item_id):
    return db.query(Transaction).filter(Transaction.item_id == item_id,
                                        Transaction.transaction_type == "Input").all()


def test_linked_return_saglam_restocks(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10, barcode="869222200001")
    did = _gift(authed_client, it.id, 5)                      # 10 → 5
    rem = _remaining(authed_client, did)
    li = rem["items"][0]
    assert li["delivered"] == 5 and li["remaining"] == 5
    r = authed_client.post("/api/returns", headers=_H, json={
        "delivery_id": did, "channel": "toplanti",
        "items": [{"delivery_item_id": li["delivery_item_id"], "quantity": 3, "condition": "saglam"}]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["document_no"].startswith("RET-") and body["restocked_items"] == 1
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 8      # 5 + 3
    tx = _input_txs(db_session, it.id)
    assert len(tx) == 1 and tx[0].quantity == 3 and tx[0].notes.startswith("İade (RET-")
    ret = db_session.query(ProductReturn).first()
    assert ret.delivery_id == did and ret.items[0].restocked is True


def test_damaged_return_no_stock_no_transaction(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10, barcode="869222200002")
    did = _gift(authed_client, it.id, 4)                      # 10 → 6
    li = _remaining(authed_client, did)["items"][0]
    r = authed_client.post("/api/returns", headers=_H, json={
        "delivery_id": did,
        "items": [{"delivery_item_id": li["delivery_item_id"], "quantity": 2, "condition": "acilmis"}]})
    assert r.status_code == 201, r.text
    assert r.json()["damaged_items"] == 1 and r.json()["restocked_items"] == 0
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 6      # DEĞİŞMEDİ
    assert len(_input_txs(db_session, it.id)) == 0                   # Transaction YOK
    ret = db_session.query(ProductReturn).first()
    assert ret.items[0].restocked is False and ret.items[0].condition == "acilmis"
    # hasarlı da kalan iade hakkından düşer (aynı mal iki kez iade edilemez)
    assert _remaining(authed_client, did)["items"][0]["remaining"] == 2


def test_over_return_blocked(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10, barcode="869222200003")
    did = _gift(authed_client, it.id, 5)
    li = _remaining(authed_client, did)["items"][0]
    ok = authed_client.post("/api/returns", headers=_H, json={
        "delivery_id": did,
        "items": [{"delivery_item_id": li["delivery_item_id"], "quantity": 3, "condition": "saglam"}]})
    assert ok.status_code == 201
    over = authed_client.post("/api/returns", headers=_H, json={
        "delivery_id": did,
        "items": [{"delivery_item_id": li["delivery_item_id"], "quantity": 3, "condition": "saglam"}]})
    assert over.status_code == 400 and "Aşırı iade" in over.json()["detail"]
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 8      # yalnız ilk iade (5+3)
    assert _remaining(authed_client, did)["items"][0]["remaining"] == 2


def test_free_return_restocks(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10, barcode="869222200004")
    r = authed_client.post("/api/returns", headers=_H, json={
        "channel": "online", "returned_by": "Trendyol",
        "items": [{"item_id": it.id, "quantity": 2, "condition": "saglam"}]})
    assert r.status_code == 201, r.text
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 12
    ret = db_session.query(ProductReturn).first()
    assert ret.delivery_id is None and ret.channel == "online" and ret.returned_by == "Trendyol"


def test_return_on_undeducted_delivery_blocked(authed_client: TestClient, db_session):
    """Preparing kargo: stok hiç düşmedi → iade anlamsız, 400."""
    it = _item(db_session, stock=10, barcode="869222200005")
    did = authed_client.post("/api/delivery", headers=_H, json={
        "delivery_type": "kargo", "doc_lang": "TR",
        "items": [{"item_id": it.id, "quantity": 2}]}).json()["id"]
    li_probe = authed_client.get(f"/api/returns/delivery/{did}/remaining")
    assert li_probe.status_code == 400
    r = authed_client.post("/api/returns", headers=_H, json={
        "delivery_id": did, "items": [{"delivery_item_id": 1, "quantity": 1, "condition": "saglam"}]})
    assert r.status_code == 400
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 10


def test_return_requires_adjust_permission(labtech_client: TestClient):
    r = labtech_client.post("/api/returns", headers=_H, json={
        "items": [{"item_id": 1, "quantity": 1, "condition": "saglam"}]})
    assert r.status_code == 403


def test_return_document_pdf(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10, barcode="869222200006")
    did = _gift(authed_client, it.id, 2, "Burak Salan")
    li = _remaining(authed_client, did)["items"][0]
    rid = authed_client.post("/api/returns", headers=_H, json={
        "delivery_id": did, "returned_by": "Burak Salan",
        "items": [{"delivery_item_id": li["delivery_item_id"], "quantity": 1, "condition": "saglam"},
                  {"delivery_item_id": li["delivery_item_id"], "quantity": 1, "condition": "hasarli"}]
    }).json()["id"]
    r = authed_client.get(f"/api/returns/{rid}/document")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    # liste + belge no formatı
    lst = authed_client.get("/api/returns").json()
    assert lst["count"] == 1
    doc_no = lst["returns"][0]["document_no"]
    assert doc_no.startswith("RET-") and doc_no.split("-")[2].isdigit() and len(doc_no.split("-")[2]) == 5
