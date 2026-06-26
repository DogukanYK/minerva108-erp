"""
Proforma fatura + onay akışı + çift-dilli ad / belge dili testleri.

  • proforma oluştur → status=pending, STOK DÜŞMEZ, Transaction yok, kalemde fiyat/ağırlık
  • onay (SuperAdmin) → stok ONAYDA düşer + Transaction(Output) + status=approved
  • onay/red yalnız SuperAdmin (LabTech → 403)
  • red → status=rejected, stok değişmez, sebep saklanır
  • proforma PDF yalnız approved (pending → 403; approved → 200 %PDF)
  • belge dili TR + name_tr → kalem adı Türkçe snapshot; EN → İngilizce ad
  • proforma birim fiyatsız → 400
  • /api/items/import-names → name_tr toplu güncelle (SKU veya EN ad ile eşleşme)
"""
from io import BytesIO

from fastapi.testclient import TestClient

from database import Item, Transaction, Delivery

_H = {"Origin": "http://testserver"}


def _item(db, name="Argan Oil", name_tr="Argan Yağı", stock=10, sku=None):
    it = Item(name=name, name_tr=name_tr, sku=sku or f"sku-{name[:4]}-{stock}",
              category="Bitmiş Ürün", unit="adet", current_stock=stock, domain="cosmetics")
    db.add(it); db.commit(); db.refresh(it)
    return it


def _login(client, username="dogukan"):
    """Aynı client üzerinde rol değiştir (cookie tek; son login geçerli)."""
    r = client.post("/api/login", json={"username": username, "password": "minerva123"}, headers=_H)
    assert r.status_code == 200, r.text
    return client


def _proforma_payload(it, qty=5, price=4.5, **extra):
    p = {"recipient_name": "John Buyer", "recipient_org": "ACME LLC",
         "delivery_type": "proforma", "doc_lang": "EN", "currency": "USD",
         "customer_address": "5th Ave", "customer_country": "USA",
         "items": [{"item_id": it.id, "quantity": qty, "unit_price": price, "weight_ml": 100}]}
    p.update(extra)
    return p


def test_proforma_create_pending_no_stock(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10)
    r = authed_client.post("/api/delivery", headers=_H, json=_proforma_payload(it, qty=5))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending" and body["document_no"].startswith("PRF-")
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 10          # DÜŞMEDİ
    assert db_session.query(Transaction).filter(Transaction.item_id == it.id).count() == 0
    d = db_session.query(Delivery).first()
    assert d.status == "pending"
    assert d.items[0].unit_price == 4.5 and d.items[0].weight_ml == 100


def test_proforma_requires_unit_price(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10)
    payload = _proforma_payload(it)
    payload["items"][0].pop("unit_price")
    r = authed_client.post("/api/delivery", headers=_H, json=payload)
    assert r.status_code == 400
    assert db_session.query(Delivery).count() == 0


def test_proforma_approve_decrements_stock(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10)
    did = authed_client.post("/api/delivery", headers=_H, json=_proforma_payload(it, qty=4)).json()["id"]
    r = authed_client.post(f"/api/delivery/{did}/approve", headers=_H)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 6          # 10 - 4 ONAYDA
    tx = db_session.query(Transaction).filter(Transaction.item_id == it.id,
                                              Transaction.transaction_type == "Output").all()
    assert len(tx) == 1 and tx[0].quantity == 4
    d = db_session.query(Delivery).get(did)
    assert d.status == "approved" and d.approved_by and d.approved_at


def test_proforma_approve_requires_superadmin(client: TestClient, db_session):
    # authed/labtech aynı cookie'yi paylaşır → tek client'ta rol değiştir.
    it = _item(db_session, stock=10)
    _login(client, "dogukan")
    did = client.post("/api/delivery", headers=_H, json=_proforma_payload(it)).json()["id"]
    _login(client, "meltem")   # LabTech
    r = client.post(f"/api/delivery/{did}/approve", headers=_H)
    assert r.status_code == 403
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 10         # düşmedi


def test_proforma_reject_keeps_stock(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10)
    did = authed_client.post("/api/delivery", headers=_H, json=_proforma_payload(it, qty=4)).json()["id"]
    r = authed_client.post(f"/api/delivery/{did}/reject", headers=_H, json={"reason": "fiyat düşük"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    db_session.expire_all()
    assert db_session.query(Item).get(it.id).current_stock == 10
    d = db_session.query(Delivery).get(did)
    assert d.status == "rejected" and d.reject_reason == "fiyat düşük"


def test_proforma_pdf_only_after_approval(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10)
    did = authed_client.post("/api/delivery", headers=_H, json=_proforma_payload(it)).json()["id"]
    assert authed_client.get(f"/api/delivery/{did}/proforma?format=pdf").status_code == 403  # pending
    authed_client.post(f"/api/delivery/{did}/approve", headers=_H)
    doc = authed_client.get(f"/api/delivery/{did}/proforma?format=pdf")
    assert doc.status_code == 200 and doc.content[:4] == b"%PDF"


def test_pending_list_superadmin_only(client: TestClient, db_session):
    it = _item(db_session, stock=10)
    _login(client, "dogukan")
    client.post("/api/delivery", headers=_H, json=_proforma_payload(it))
    d = client.get("/api/delivery/pending").json()
    assert d["count"] == 1 and d["pending"][0]["delivery_type"] == "proforma"
    _login(client, "meltem")   # LabTech
    assert client.get("/api/delivery/pending").status_code == 403


def test_doc_lang_snapshots_turkish_name(authed_client: TestClient, db_session):
    it = _item(db_session, name="Argan Oil", name_tr="Argan Yağı", stock=10)
    r = authed_client.post("/api/delivery", headers=_H, json={
        "recipient_name": "Ali", "delivery_type": "hediye", "doc_lang": "TR",
        "items": [{"item_id": it.id, "quantity": 1}]})
    d = authed_client.get(f"/api/delivery/{r.json()['id']}").json()
    assert d["items"][0]["item_name"] == "Argan Yağı" and d["doc_lang"] == "TR"


def test_doc_lang_english_uses_name(authed_client: TestClient, db_session):
    it = _item(db_session, name="Argan Oil", name_tr="Argan Yağı", stock=10)
    r = authed_client.post("/api/delivery", headers=_H, json={
        "recipient_name": "Ali", "delivery_type": "hediye", "doc_lang": "EN",
        "items": [{"item_id": it.id, "quantity": 1}]})
    d = authed_client.get(f"/api/delivery/{r.json()['id']}").json()
    assert d["items"][0]["item_name"] == "Argan Oil"


def test_import_names_updates_name_tr(authed_client: TestClient, db_session):
    import openpyxl
    a = _item(db_session, name="Foot Cream", name_tr=None, stock=5, sku="FC-001")
    b = _item(db_session, name="Hand Balm", name_tr=None, stock=5, sku="HB-002")
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["SKU", "İngilizce Ad", "Türkçe Ad"])
    ws.append(["FC-001", "Foot Cream", "Ayak Kremi"])
    ws.append(["Hand Balm", "", "El Merhemi"])      # SKU yok → EN ad ile eşleş
    ws.append(["YOK-999", "Ghost", "Hayalet"])      # eşleşmez
    buf = BytesIO(); wb.save(buf)
    r = authed_client.post("/api/items/import-names", headers=_H,
                           files={"file": ("names.xlsx", buf.getvalue(),
                                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"] == 2 and body["unmatched_count"] == 1
    db_session.expire_all()
    assert db_session.query(Item).get(a.id).name_tr == "Ayak Kremi"
    assert db_session.query(Item).get(b.id).name_tr == "El Merhemi"
