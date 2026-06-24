"""
Teslimat (hediye/numune çıkışı) testleri.

  • barkod-okut mantığı: aynı ürün çok kez → miktar birleşir, stok o kadar düşer
  • stok düşümü + immutable Transaction(Output)
  • yetersiz stok → net hata, stok değişmez
  • belge no üretimi (TES-YYYY-NNNNN) + imzalı PDF
  • RBAC: çıkış için inventory.adjust gerekir
"""
from fastapi.testclient import TestClient

from database import Item, Transaction, Delivery, DeliveryItem

_H = {"Origin": "http://testserver"}


def _item(db, name="Serenida Krem", stock=10, unit="adet", barcode="869000000001"):
    it = Item(name=name, sku=f"sku-{name[:6]}-{barcode[-4:]}", category="Bitmiş Ürün",
              unit=unit, current_stock=stock, barcode=barcode, domain="cosmetics")
    db.add(it); db.commit(); db.refresh(it)
    return it


def test_create_delivery_decrements_stock(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10)
    r = authed_client.post("/api/delivery", headers=_H, json={
        "recipient_name": "Ayşe Yılmaz", "delivery_type": "hediye", "method": "elden",
        "items": [{"item_id": it.id, "quantity": 3}],
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["document_no"].startswith("TES-") and body["item_count"] == 1

    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 7   # 10 - 3
    tx = db_session.query(Transaction).filter(Transaction.item_id == it.id,
                                              Transaction.transaction_type == "Output").all()
    assert len(tx) == 1 and tx[0].quantity == 3
    d = db_session.query(Delivery).first()
    assert d.recipient_name == "Ayşe Yılmaz" and len(d.items) == 1
    assert d.items[0].item_name == it.name and d.items[0].quantity == 3


def test_repeated_scans_accumulate(authed_client: TestClient, db_session):
    """Market kasası: aynı ürün 4 kez okutulursa stoktan 4 düşer."""
    it = _item(db_session, stock=10)
    r = authed_client.post("/api/delivery", headers=_H, json={
        "recipient_name": "Veli Demir",
        "items": [{"item_id": it.id, "quantity": 1}] * 4,   # 4 ayrı okutma
    })
    assert r.status_code == 201, r.text
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 6   # 10 - 4


def test_insufficient_stock_blocks_and_keeps_stock(authed_client: TestClient, db_session):
    it = _item(db_session, stock=2)
    r = authed_client.post("/api/delivery", headers=_H, json={
        "recipient_name": "Can Öz", "items": [{"item_id": it.id, "quantity": 5}],
    })
    assert r.status_code == 400
    assert "Yetersiz stok" in r.json()["detail"]
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 2   # değişmedi
    assert db_session.query(Delivery).count() == 0                # kayıt oluşmadı


def test_delivery_document_pdf(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10)
    did = authed_client.post("/api/delivery", headers=_H, json={
        "recipient_name": "Zeynep Ak", "items": [{"item_id": it.id, "quantity": 1}],
    }).json()["id"]
    doc = authed_client.get(f"/api/delivery/{did}/document?format=pdf")
    assert doc.status_code == 200
    assert doc.content[:4] == b"%PDF"
    assert "attachment" in doc.headers.get("content-disposition", "")


def test_delivery_list_and_detail(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10)
    did = authed_client.post("/api/delivery", headers=_H, json={
        "recipient_name": "Mehmet Su", "items": [{"item_id": it.id, "quantity": 2}],
    }).json()["id"]
    lst = authed_client.get("/api/delivery").json()
    assert lst["count"] == 1 and lst["deliveries"][0]["recipient_name"] == "Mehmet Su"
    det = authed_client.get(f"/api/delivery/{did}").json()
    assert det["items"][0]["quantity"] == 2


def test_dispatch_requires_adjust_permission(labtech_client: TestClient, db_session):
    it = _item(db_session, stock=10)
    r = labtech_client.post("/api/delivery", headers=_H, json={
        "recipient_name": "X Y", "items": [{"item_id": it.id, "quantity": 1}],
    })
    assert r.status_code == 403
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 10   # düşmedi
