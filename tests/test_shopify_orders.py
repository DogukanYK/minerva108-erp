"""
Faz 2 — Shopify orders/paid webhook: stok düşümü + Paraşüt fatura otomasyonu.
TAMAMEN AĞSIZ (Paraşüt/Shopify HTTP monkeypatch'li).

  • Webhook güvenliği: HMAC doğru/yanlış/eksik, bilinmeyen mağaza, CSRF'siz geçiş
  • İdempotency: aynı sipariş 2x → stok BİR kez, tek Transaction
  • Ülke: TR dışı → skipped_export, stok DEĞİŞMEZ
  • Stok: barkod eşleşme, sku fallback, belirsiz barkod atlanır, negatife düşer
  • Paraşüt: contact→invoice→payment sırası, draft'ta legalize YOK, official'da var
  • Hata: fatura patlarsa failed + stok düşük kalır; retry stoğu TEKRAR DÜŞÜRMEZ
  • RBAC: /orders + /retry + /webhooks/setup admin.view
"""
import base64
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from core import parasut as P
from core import shopify as S
from database import Item, ShopifyOrder, Transaction

_H = {"Origin": "http://testserver"}
_SECRET = "test-webhook-secret"


def _env(monkeypatch, brand="MINERVA"):
    monkeypatch.setenv(f"SHOPIFY_{brand}_STORE", f"{brand.lower()}.myshopify.com")
    monkeypatch.setenv(f"SHOPIFY_{brand}_CLIENT_ID", "cid")
    monkeypatch.setenv(f"SHOPIFY_{brand}_CLIENT_SECRET", _SECRET)
    monkeypatch.setenv(f"SHOPIFY_{brand}_LOCATION", "111")


def _parasut_env(monkeypatch, mode="draft"):
    for k, v in {"CLIENT_ID": "pc", "CLIENT_SECRET": "ps", "COMPANY_ID": "115",
                 "EMAIL": "a@b.com", "PASSWORD": "x", "ACCOUNT_ID_IYZICO": "9"}.items():
        monkeypatch.setenv(f"PARASUT_{k}", v)
    monkeypatch.setenv("INVOICE_MODE", mode)


def _item(db, name, barcode, stock=10.0, sku=None, domain="cosmetics"):
    it = Item(name=name, sku=sku or f"sku-{name[:18]}", category="Bitmiş Ürün", unit="adet",
              barcode=barcode, current_stock=stock, domain=domain, is_active=True)
    db.add(it); db.commit(); db.refresh(it)
    return it


def _payload(order_id=5001, country="TR", lines=None, number="#1001", total="120.00"):
    return {
        "id": order_id, "name": number, "total_price": total, "currency": "TRY",
        "created_at": "2026-07-27T10:00:00+03:00", "email": "musteri@example.com",
        "billing_address": {"first_name": "Ayşe", "last_name": "Yılmaz", "country_code": country,
                            "address1": "Test Mah. 1", "city": "İstanbul", "phone": "0555"},
        "line_items": lines if lines is not None else [
            {"barcode": "BC-1", "sku": "SKU-1", "title": "Krem", "quantity": 2, "price": "60.00",
             "tax_lines": [{"rate": 0.20}], "discount_allocations": []}],
        "shipping_lines": [],
    }


def _post(client, payload, store="minerva", secret=_SECRET, bad_sig=False, no_sig=False):
    raw = json.dumps(payload).encode()
    headers = {}
    if not no_sig:
        digest = hmac.new(secret.encode(), raw, hashlib.sha256).digest()
        sig = base64.b64encode(digest).decode()
        headers["X-Shopify-Hmac-Sha256"] = "bozuk" if bad_sig else sig
    return client.post(f"/api/shopify/webhook/orders/{store}", content=raw,
                       headers={**headers, "Content-Type": "application/json"})


def _mock_parasut(monkeypatch, calls):
    monkeypatch.setattr(P, "find_or_create_contact", lambda o: (calls.append("contact") or 11))
    monkeypatch.setattr(P, "find_or_create_product", lambda c, n, v: (calls.append("product") or 22))
    monkeypatch.setattr(P, "create_invoice", lambda o, l, c: (calls.append("invoice") or 33))
    monkeypatch.setattr(P, "add_payment", lambda i, a, date=None, description="": calls.append("payment"))
    def _legalize(o, i):
        calls.append("legalize")
        if P.invoice_mode() != "official":       # gerçek davranış: draft → NO-OP
            return {"mode": "draft", "doc_type": None, "job_id": None, "job_status": None}
        return {"mode": "official", "doc_type": "e_archive", "job_id": "j1", "job_status": "done"}
    monkeypatch.setattr(P, "legalize", _legalize)


# ─── Webhook güvenliği ────────────────────────────────────────────────────────

def test_webhook_valid_hmac_accepted(client: TestClient, db_session, monkeypatch):
    _env(monkeypatch)
    it = _item(db_session, "Minerva 108 Krem", "BC-1", stock=10)
    r = _post(client, _payload())
    assert r.status_code == 200 and r.json()["ok"] is True
    db_session.expire_all()
    assert db_session.query(Item).filter(Item.id == it.id).one().current_stock == 8


def test_webhook_bad_hmac_401(client: TestClient, db_session, monkeypatch):
    _env(monkeypatch)
    it = _item(db_session, "Minerva 108 Krem", "BC-1", stock=10)
    r = _post(client, _payload(), bad_sig=True)
    assert r.status_code == 401
    db_session.expire_all()
    assert db_session.query(Item).filter(Item.id == it.id).one().current_stock == 10
    assert db_session.query(ShopifyOrder).count() == 0


def test_webhook_missing_hmac_401(client: TestClient, monkeypatch):
    _env(monkeypatch)
    assert _post(client, _payload(), no_sig=True).status_code == 401


def test_webhook_unknown_store_404(client: TestClient, monkeypatch):
    _env(monkeypatch)
    assert _post(client, _payload(), store="acme").status_code == 404


def test_webhook_unconfigured_store_503(client: TestClient, monkeypatch):
    # hiç env yok → secret bulunamaz
    assert _post(client, _payload(), store="serenida").status_code == 503


# ─── İdempotency + ülke ───────────────────────────────────────────────────────

def test_duplicate_order_decrements_once(client: TestClient, db_session, monkeypatch):
    _env(monkeypatch)
    it = _item(db_session, "Minerva 108 Krem", "BC-1", stock=10)
    p = _payload()
    assert _post(client, p).status_code == 200
    assert _post(client, p).status_code == 200          # webhook redelivery
    db_session.expire_all()
    assert db_session.query(Item).filter(Item.id == it.id).one().current_stock == 8   # 1 kez
    assert db_session.query(Transaction).filter(Transaction.item_id == it.id,
                                                Transaction.transaction_type == "Output").count() == 1
    assert db_session.query(ShopifyOrder).count() == 1


def test_export_order_skipped(client: TestClient, db_session, monkeypatch):
    _env(monkeypatch)
    it = _item(db_session, "Minerva 108 Krem", "BC-1", stock=10)
    calls = []
    monkeypatch.setattr("core.notifications.notify_export_order_manual",
                        lambda no, c: calls.append((no, c)))
    r = _post(client, _payload(country="DE"))
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.query(Item).filter(Item.id == it.id).one().current_stock == 10   # DOKUNULMAZ
    row = db_session.query(ShopifyOrder).one()
    assert row.status == "skipped_export" and row.country == "DE"


# ─── Stok eşleme kuralları ────────────────────────────────────────────────────

def test_transaction_notes_and_actor(client: TestClient, db_session, monkeypatch):
    _env(monkeypatch)
    it = _item(db_session, "Minerva 108 Krem", "BC-1", stock=10)
    _post(client, _payload())
    db_session.expire_all()
    tx = db_session.query(Transaction).filter(Transaction.item_id == it.id).one()
    assert tx.transaction_type == "Output" and tx.quantity == 2
    assert tx.notes.startswith("Shopify #1001") and tx.performed_by == "Shopify"


def test_sku_fallback_when_no_barcode(client: TestClient, db_session, monkeypatch):
    _env(monkeypatch)
    it = _item(db_session, "Minerva 108 Serum", "OTHER-BC", stock=7, sku="SKU-X")
    p = _payload(lines=[{"barcode": "", "sku": "SKU-X", "title": "Serum", "quantity": 3,
                         "price": "40.00", "tax_lines": [{"rate": 0.20}]}])
    _post(client, p)
    db_session.expire_all()
    assert db_session.query(Item).filter(Item.id == it.id).one().current_stock == 4


def test_ambiguous_barcode_skipped(client: TestClient, db_session, monkeypatch):
    _env(monkeypatch)
    a = _item(db_session, "Minerva 108 A", "DUP", stock=5, sku="s-a")
    b = _item(db_session, "Minerva 108 B", "DUP", stock=5, sku="s-b")
    p = _payload(lines=[{"barcode": "DUP", "sku": "", "title": "X", "quantity": 1,
                         "price": "10.00", "tax_lines": [{"rate": 0.20}]}])
    _post(client, p)
    db_session.expire_all()
    assert db_session.query(Item).filter(Item.id == a.id).one().current_stock == 5   # DOKUNULMAZ
    assert db_session.query(Item).filter(Item.id == b.id).one().current_stock == 5
    row = db_session.query(ShopifyOrder).one()
    assert "belirsiz" in (row.last_error or "")


def test_unmatched_item_reported_others_decrement(client: TestClient, db_session, monkeypatch):
    _env(monkeypatch)
    ok = _item(db_session, "Minerva 108 Krem", "BC-1", stock=10)
    p = _payload(lines=[
        {"barcode": "BC-1", "sku": "", "title": "Krem", "quantity": 1, "price": "60.00"},
        {"barcode": "YOK-BC", "sku": "", "title": "Bilinmeyen", "quantity": 5, "price": "10.00"}])
    _post(client, p)
    db_session.expire_all()
    assert db_session.query(Item).filter(Item.id == ok.id).one().current_stock == 9
    assert "eşleşmedi" in (db_session.query(ShopifyOrder).one().last_error or "")


def test_stock_can_go_negative(client: TestClient, db_session, monkeypatch):
    _env(monkeypatch)
    it = _item(db_session, "Minerva 108 Krem", "BC-1", stock=1)
    _post(client, _payload())                      # 2 adet satıldı, stok 1
    db_session.expire_all()
    assert db_session.query(Item).filter(Item.id == it.id).one().current_stock == -1


def test_supplement_domain_never_touched(client: TestClient, db_session, monkeypatch):
    _env(monkeypatch)
    sup = _item(db_session, "Minerva 108 Kapsül", "BC-SUP", stock=9, domain="supplement")
    p = _payload(lines=[{"barcode": "BC-SUP", "sku": "", "title": "Kapsül", "quantity": 2,
                         "price": "50.00"}])
    _post(client, p)
    db_session.expire_all()
    assert db_session.query(Item).filter(Item.id == sup.id).one().current_stock == 9


# ─── Paraşüt akışı ────────────────────────────────────────────────────────────

def test_parasut_draft_flow_no_legalize(client: TestClient, db_session, monkeypatch):
    _env(monkeypatch); _parasut_env(monkeypatch, mode="draft")
    _item(db_session, "Minerva 108 Krem", "BC-1", stock=10)
    calls = []
    _mock_parasut(monkeypatch, calls)
    _post(client, _payload())
    db_session.expire_all()
    row = db_session.query(ShopifyOrder).one()
    assert calls == ["contact", "invoice", "payment", "legalize"]
    assert row.parasut_invoice_id == 33 and row.status == "paid"     # draft → legalized DEĞİL


def test_parasut_official_sets_legalized(client: TestClient, db_session, monkeypatch):
    _env(monkeypatch); _parasut_env(monkeypatch, mode="official")
    _item(db_session, "Minerva 108 Krem", "BC-1", stock=10)
    calls = []
    _mock_parasut(monkeypatch, calls)
    _post(client, _payload())
    db_session.expire_all()
    row = db_session.query(ShopifyOrder).one()
    assert row.status == "legalized" and row.parasut_doc_type == "e_archive"


def test_invoice_failure_keeps_stock_and_marks_failed(client: TestClient, db_session, monkeypatch):
    _env(monkeypatch); _parasut_env(monkeypatch)
    it = _item(db_session, "Minerva 108 Krem", "BC-1", stock=10)
    monkeypatch.setattr(P, "find_or_create_contact", lambda o: 11)
    monkeypatch.setattr(P, "create_invoice",
                        lambda o, l, c: (_ for _ in ()).throw(P.ParasutError("422 vergi no")))
    _post(client, _payload())
    db_session.expire_all()
    row = db_session.query(ShopifyOrder).one()
    assert row.status == "failed" and "422" in (row.last_error or "")
    assert db_session.query(Item).filter(Item.id == it.id).one().current_stock == 8   # stok DÜŞTÜ


def test_retry_does_not_double_decrement(client: TestClient, db_session, monkeypatch):
    _env(monkeypatch); _parasut_env(monkeypatch)
    it = _item(db_session, "Minerva 108 Krem", "BC-1", stock=10)
    monkeypatch.setattr(P, "find_or_create_contact", lambda o: 11)
    monkeypatch.setattr(P, "create_invoice",
                        lambda o, l, c: (_ for _ in ()).throw(P.ParasutError("geçici")))
    _post(client, _payload())
    db_session.expire_all()
    row = db_session.query(ShopifyOrder).one()
    assert row.status == "failed"
    # düzelt ve yeniden dene
    calls = []
    _mock_parasut(monkeypatch, calls)
    S.retry_order(db_session, row)
    db_session.expire_all()
    assert db_session.query(Item).filter(Item.id == it.id).one().current_stock == 8   # TEKRAR DÜŞMEDİ
    assert db_session.query(Transaction).filter(Transaction.item_id == it.id).count() == 1
    assert db_session.query(ShopifyOrder).one().parasut_invoice_id == 33


def test_parasut_token_password_grant_cached(monkeypatch):
    _parasut_env(monkeypatch)
    import httpx
    calls = []

    class _R:
        status_code = 200
        def json(self): return {"access_token": "tok", "expires_in": 7200}

    monkeypatch.setattr(httpx, "post", lambda url, **kw: (calls.append((url, kw.get("data", {}))) or _R()))
    P._TOKEN_CACHE.clear()
    assert P._get_access_token() == "tok"
    assert calls[0][1]["grant_type"] == "password"
    assert P._get_access_token() == "tok" and len(calls) == 1        # cache
    P._TOKEN_CACHE.clear()


# ─── RBAC ─────────────────────────────────────────────────────────────────────

def test_orders_endpoints_require_admin(labtech_client: TestClient):
    assert labtech_client.get("/api/shopify/orders").status_code == 403
    assert labtech_client.post("/api/shopify/orders/1/retry", headers=_H).status_code == 403
    assert labtech_client.post("/api/shopify/webhooks/setup", headers=_H).status_code == 403


def test_orders_list_ok_for_admin(authed_client: TestClient, db_session, monkeypatch):
    _env(monkeypatch)
    _item(db_session, "Minerva 108 Krem", "BC-1", stock=10)
    _post(authed_client, _payload())
    d = authed_client.get("/api/shopify/orders").json()
    assert d["orders"] and d["orders"][0]["order_number"] == "1001"   # '#' tekilleştirilir
    assert d["invoice_mode"] in ("draft", "official")
