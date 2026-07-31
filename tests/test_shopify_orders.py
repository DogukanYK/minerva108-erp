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

import pytest
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
    monkeypatch.setattr(P, "add_payment",
                        lambda i, a, date=None, description="", store_key=None:
                        calls.append(f"payment:{store_key}"))
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
    # tahsilat siparişin geldiği mağazanın hesabına gider (marka bazlı iyzico)
    assert calls == ["contact", "invoice", "payment:minerva", "legalize"]
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


# ─── Retry job: yarım kalmış siparişleri toplama (sessiz boşluk kapatma) ─────

def _stuck_order(db, status="stock_done", age_min=60, attempts=0, invoice_id=None):
    """Belirtilen durumda, `age_min` dakika ÖNCE güncellenmiş bir sipariş kaydı."""
    from datetime import datetime, timedelta
    from database import ShopifyOrder
    import json as _json
    row = ShopifyOrder(
        store_key="minerva", shopify_order_id=7001, order_number="7001",
        status=status, stock_applied=True, attempts=attempts, total=120.0,
        currency="TRY", country="TR", customer_email="a@b.com",
        customer_name="Test", parasut_invoice_id=invoice_id,
        lines_json=_json.dumps({"lines": [{"barcode": "BC-1", "sku": "", "title": "Krem",
                                           "quantity": 2, "unit_price_incl": 60.0,
                                           "vat_rate": 20, "discount_amount": 0}],
                                "shipping_amount": 0, "order_date": "2026-07-27"}))
    db.add(row); db.commit()
    old = datetime.utcnow() - timedelta(minutes=age_min)
    db.query(ShopifyOrder).filter(ShopifyOrder.id == row.id).update({"updated_at": old})
    db.commit(); db.refresh(row)
    return row


def test_retry_job_picks_up_stuck_stock_done(db_session, monkeypatch):
    """Paraşüt kapalıyken gelip stock_done'da bekleyen sipariş, creds gelince
    kendiliğinden faturalanır (retry sadece 'failed'e bakmıyor)."""
    from core import scheduler as sch
    _parasut_env(monkeypatch, mode="draft")
    row = _stuck_order(db_session, status="stock_done", age_min=60)
    calls = []
    _mock_parasut(monkeypatch, calls)
    monkeypatch.setattr(sch, "SessionLocal", lambda: db_session)
    sch.parasut_invoice_retry()
    db_session.expire_all()
    from database import ShopifyOrder
    r = db_session.query(ShopifyOrder).filter(ShopifyOrder.id == row.id).one()
    assert r.status == "paid" and r.parasut_invoice_id == 33
    assert "invoice" in calls


def test_retry_job_skips_fresh_orders(db_session, monkeypatch):
    """15 dk'dan yeni kayda DOKUNMAZ — işlenmekte olan webhook'la yarışmasın."""
    from core import scheduler as sch
    _parasut_env(monkeypatch, mode="draft")
    row = _stuck_order(db_session, status="stock_done", age_min=2)     # taze
    calls = []
    _mock_parasut(monkeypatch, calls)
    monkeypatch.setattr(sch, "SessionLocal", lambda: db_session)
    sch.parasut_invoice_retry()
    db_session.expire_all()
    from database import ShopifyOrder
    assert db_session.query(ShopifyOrder).filter(ShopifyOrder.id == row.id).one().status == "stock_done"
    assert calls == []


def test_retry_job_draft_mode_leaves_paid_alone(db_session, monkeypatch):
    """DRAFT modda 'paid' uç durumdur — sonsuz yeniden deneme olmamalı."""
    from core import scheduler as sch
    _parasut_env(monkeypatch, mode="draft")
    row = _stuck_order(db_session, status="paid", age_min=60, invoice_id=33)
    calls = []
    _mock_parasut(monkeypatch, calls)
    monkeypatch.setattr(sch, "SessionLocal", lambda: db_session)
    sch.parasut_invoice_retry()
    db_session.expire_all()
    from database import ShopifyOrder
    r = db_session.query(ShopifyOrder).filter(ShopifyOrder.id == row.id).one()
    assert r.attempts == 0 and calls == []          # hiç dokunulmadı


def test_retry_job_official_mode_finishes_paid(db_session, monkeypatch):
    """OFFICIAL modda 'paid' yarım kalmıştır → resmileştirme tamamlanır."""
    from core import scheduler as sch
    _parasut_env(monkeypatch, mode="official")
    row = _stuck_order(db_session, status="paid", age_min=60, invoice_id=33)
    calls = []
    _mock_parasut(monkeypatch, calls)
    monkeypatch.setattr(sch, "SessionLocal", lambda: db_session)
    sch.parasut_invoice_retry()
    db_session.expire_all()
    from database import ShopifyOrder
    r = db_session.query(ShopifyOrder).filter(ShopifyOrder.id == row.id).one()
    assert r.status == "legalized" and "legalize" in calls


def test_retry_job_respects_attempt_limit(db_session, monkeypatch):
    """5 denemeyi aşan kayıt artık toplanmaz (kalıcı hata — elle denenir)."""
    from core import scheduler as sch
    _parasut_env(monkeypatch, mode="draft")
    row = _stuck_order(db_session, status="failed", age_min=60, attempts=5)
    calls = []
    _mock_parasut(monkeypatch, calls)
    monkeypatch.setattr(sch, "SessionLocal", lambda: db_session)
    sch.parasut_invoice_retry()
    db_session.expire_all()
    assert calls == []


def test_retry_job_noop_when_parasut_unconfigured(db_session, monkeypatch):
    """Paraşüt yapılandırılmamışken job hiçbir şey yapmaz (dormant)."""
    from core import scheduler as sch
    for k in ("CLIENT_ID", "CLIENT_SECRET", "COMPANY_ID", "EMAIL", "PASSWORD"):
        monkeypatch.delenv(f"PARASUT_{k}", raising=False)
    row = _stuck_order(db_session, status="stock_done", age_min=60)
    calls = []
    _mock_parasut(monkeypatch, calls)
    monkeypatch.setattr(sch, "SessionLocal", lambda: db_session)
    sch.parasut_invoice_retry()
    db_session.expire_all()
    from database import ShopifyOrder
    assert db_session.query(ShopifyOrder).filter(ShopifyOrder.id == row.id).one().status == "stock_done"
    assert calls == []


# ─── Marka bazlı iyzico tahsilat hesabı ──────────────────────────────────────

def test_account_id_per_store(monkeypatch):
    """Her markanın kendi iyzico hesabı; yoksa genel hesaba düşer; o da yoksa None."""
    for k in ("ACCOUNT_ID_MINERVA", "ACCOUNT_ID_EVANIRA", "ACCOUNT_ID_IYZICO"):
        monkeypatch.delenv(f"PARASUT_{k}", raising=False)
    assert P.account_id("minerva") is None                    # hiçbiri yok
    monkeypatch.setenv("PARASUT_ACCOUNT_ID_IYZICO", "900")
    assert P.account_id("minerva") == 900                     # genel fallback
    monkeypatch.setenv("PARASUT_ACCOUNT_ID_MINERVA", "901")
    monkeypatch.setenv("PARASUT_ACCOUNT_ID_EVANIRA", "902")
    assert P.account_id("minerva") == 901                     # markaya özel kazanır
    assert P.account_id("evanira") == 902
    assert P.account_id("serenida") == 900                    # tanımsız marka → genel
    assert P.account_id() == 900


def test_payment_uses_store_account(db_session, monkeypatch):
    """Tahsilat, siparişin geldiği MAĞAZANIN hesabına işlenir."""
    _parasut_env(monkeypatch, mode="draft")
    monkeypatch.setenv("PARASUT_ACCOUNT_ID_MINERVA", "901")
    sent = {}
    monkeypatch.setattr(P, "_api", lambda m, p, json=None, params=None:
                        (sent.update({"path": p, "body": json}) or {"data": {"id": "1"}}))
    P.add_payment(55, 120.0, date="2026-07-27", store_key="minerva")
    assert sent["path"] == "/sales_invoices/55/payments"
    assert sent["body"]["data"]["attributes"]["account_id"] == 901
    assert sent["body"]["data"]["attributes"]["amount"] == 120.0


def test_payment_skipped_when_no_account(monkeypatch):
    """Hesap tanımsızsa tahsilat ATLANIR (fatura açık kalır), hata fırlatmaz."""
    _parasut_env(monkeypatch, mode="draft")
    monkeypatch.delenv("PARASUT_ACCOUNT_ID_IYZICO", raising=False)
    calls = []
    monkeypatch.setattr(P, "_api", lambda *a, **k: calls.append(1))
    P.add_payment(55, 120.0, store_key="minerva")
    assert calls == []


# ─── Tahsilat idempotansı (gerçek prod bug'ı regresyonu: #1008) ──────────────

def _payment_api(monkeypatch, invoice_att, on_post=None):
    """GET /sales_invoices/{id} → verilen attributes; POST payments → kaydeder."""
    posts = []

    def fake(method, path, json=None, params=None):
        if method == "GET":
            return {"data": {"id": "55", "attributes": invoice_att}}
        posts.append(json)
        if on_post:
            on_post()
        return {"data": {"id": "9"}}

    monkeypatch.setattr(P, "_api", fake)
    return posts


def test_payment_skipped_when_invoice_already_paid(monkeypatch):
    """Aynı webhook iki kez gelirse ikinci tahsilat HİÇ yazılmaz (çift tahsilat yok).
    (Yaşandı: #1008 → 400 'amount can not be bigger than remaining' → sipariş failed.)"""
    _parasut_env(monkeypatch, mode="draft")
    monkeypatch.setenv("PARASUT_ACCOUNT_ID_MINERVA", "901")
    posts = _payment_api(monkeypatch, {"net_total": 1450.0, "total_paid": 1450.0})
    P.add_payment(55, 1450.0, store_key="minerva")
    assert posts == []


def test_payment_clamped_to_remaining(monkeypatch):
    """Kalan tutardan büyük tahsilat, kalana kırpılır (KDV yuvarlama farkı)."""
    _parasut_env(monkeypatch, mode="draft")
    monkeypatch.setenv("PARASUT_ACCOUNT_ID_MINERVA", "901")
    posts = _payment_api(monkeypatch, {"net_total": 1449.98, "total_paid": 0})
    P.add_payment(55, 1450.0, store_key="minerva")
    assert posts[0]["data"]["attributes"]["amount"] == 1449.98


def test_payment_swallows_race_already_paid_error(monkeypatch):
    """Kontrolden SONRA araya tahsilat girerse Paraşüt'ün 400'ü hata sayılmaz."""
    _parasut_env(monkeypatch, mode="draft")
    monkeypatch.setenv("PARASUT_ACCOUNT_ID_MINERVA", "901")

    def boom():
        raise P.ParasutError("Paraşüt POST → 400: data->attributes->amount can not be "
                             "bigger than remaining of the SalesInvoice")

    _payment_api(monkeypatch, {"net_total": 1450.0, "total_paid": 0}, on_post=boom)
    P.add_payment(55, 1450.0, store_key="minerva")      # fırlatmamalı


def test_payment_still_raises_other_errors(monkeypatch):
    """Gerçek hatalar (yetki, geçersiz hesap) yutulmaz — sipariş failed kalmalı."""
    _parasut_env(monkeypatch, mode="draft")
    monkeypatch.setenv("PARASUT_ACCOUNT_ID_MINERVA", "901")

    def boom():
        raise P.ParasutError("Paraşüt POST → 422: account not found")

    _payment_api(monkeypatch, {"net_total": 1450.0, "total_paid": 0}, on_post=boom)
    with pytest.raises(P.ParasutError):
        P.add_payment(55, 1450.0, store_key="minerva")


def test_payment_proceeds_when_invoice_unreadable(monkeypatch):
    """Fatura okunamazsa (API hatası) tahsilat yine denenir — sessizce kaybolmaz."""
    _parasut_env(monkeypatch, mode="draft")
    monkeypatch.setenv("PARASUT_ACCOUNT_ID_MINERVA", "901")
    posts = []

    def fake(method, path, json=None, params=None):
        if method == "GET":
            raise P.ParasutError("Paraşüt GET → 503")
        posts.append(json)
        return {"data": {"id": "9"}}

    monkeypatch.setattr(P, "_api", fake)
    P.add_payment(55, 120.0, store_key="minerva")
    assert posts[0]["data"]["attributes"]["amount"] == 120.0


# ─── Ülke tespiti: çok kaynaklı (gerçek prod bug'ı regresyonu) ───────────────

def test_country_falls_back_to_shipping_and_customer():
    """billing.country_code BOŞ gelse bile TR siparişi ihracat sayılmamalı.
    (Yaşandı: draft order → complete akışında billing.country null geliyor.)"""
    # 1) billing boş, shipping'de kod var
    p = {"billing_address": {"city": "İstanbul"}, "shipping_address": {"country_code": "tr"}}
    assert S._country_code(p, p["billing_address"]) == "TR"
    # 2) hiçbirinde kod yok, shipping'de ülke ADI var
    p = {"billing_address": {"city": "İstanbul"}, "shipping_address": {"country": "Turkey"}}
    assert S._country_code(p, p["billing_address"]) == "TR"
    # 3) yalnız müşterinin varsayılan adresinde var
    p = {"billing_address": {"city": "İstanbul"},
         "customer": {"default_address": {"country_code": "TR"}}}
    assert S._country_code(p, p["billing_address"]) == "TR"
    # 4) billing önceliklidir
    p = {"billing_address": {"country_code": "DE"}, "shipping_address": {"country_code": "TR"}}
    assert S._country_code(p, p["billing_address"]) == "DE"
    # 5) hiçbir ülke bilgisi yok → boş (akış MANUEL'e düşürür, tahmin etmez)
    p = {"billing_address": {"city": "X"}}
    assert S._country_code(p, p["billing_address"]) == ""


def test_tr_order_with_empty_billing_country_is_processed(client, db_session, monkeypatch):
    """Uçtan uca: billing.country boş + shipping 'Turkey' → stok DÜŞER, atlanmaz."""
    _env(monkeypatch)
    it = _item(db_session, "Minerva 108 Krem", "BC-1", stock=10)
    p = _payload()
    p["billing_address"].pop("country_code", None)          # Shopify'ın boş bıraktığı hal
    p["shipping_address"] = {"country": "Turkey", "city": "İstanbul"}
    assert _post(client, p).status_code == 200
    db_session.expire_all()
    row = db_session.query(ShopifyOrder).one()
    assert row.country == "TR" and row.status != "skipped_export"
    assert db_session.query(Item).filter(Item.id == it.id).one().current_stock == 8


def test_variant_id_fallback_matches_stock(client, db_session, monkeypatch):
    """Shopify sipariş webhook'u BARKOD göndermez (sadece sku + variant_id).
    IMS'te sku de yoksa, variant_id → barkod haritasıyla eşleşip stok DÜŞMELİ.
    (Yaşandı: gerçek siparişte 'IMS'te eşleşmedi' → stok hiç düşmüyordu.)"""
    _env(monkeypatch)
    it = _item(db_session, "Minerva 108 Face Scrub", "8683829206413", stock=69, sku=None)
    monkeypatch.setattr(S, "fetch_variants", lambda b: [
        {"barcode": "8683829206413", "sku": "MIN-FACESCRB-60",
         "inventory_item_id": "gid://iv/1", "variant_id": "43396254662704",
         "product_status": "ACTIVE"}])
    S._VARIANT_BC_CACHE.clear()
    p = _payload(lines=[{"barcode": None, "sku": "MIN-FACESCRB-60", "variant_id": 43396254662704,
                         "title": "Face Scrub", "quantity": 1, "price": "1450.00",
                         "tax_lines": [{"rate": 0.20}]}])
    assert _post(client, p).status_code == 200
    db_session.expire_all()
    assert db_session.query(Item).filter(Item.id == it.id).one().current_stock == 68
    row = db_session.query(ShopifyOrder).one()
    assert row.stock_applied is True and "eşleşmedi" not in (row.last_error or "")
    S._VARIANT_BC_CACHE.clear()
