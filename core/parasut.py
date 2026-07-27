# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Paraşüt API v4 motoru — Shopify siparişinden otomatik resmi fatura.

Akış (core/shopify.handle_order_paid çağırır):
  find_or_create_contact → find_or_create_product (kalem başına) → create_invoice
  → add_payment (iyzico kasası) → legalize (INVOICE_MODE=official ise e-Arşiv /
  mükellefse e-Fatura; draft ise NO-OP).

Auth: password grant (PARASUT_EMAIL/PASSWORD) — token 2 saat, in-process cache,
refresh zinciri YOK (kendini onarır). RATE LİMİT 10 istek/10 sn → her çağrı
arası ~1.05s (_RATE_SLEEP). JSON:API (data{type,attributes,relationships}).

RESMİ BELGE İLKESİ: asla tahmin etme — Paraşüt hata gövdesi ParasutError ile
aynen yukarı taşınır (panele/bildirime düşer). Tüm tarihler TR-lokal (to_tr).
Env yoksa modül import edilebilir kalır; is_configured() False → akış atlanır.
"""
import logging
import os
import time
from datetime import datetime
from typing import List, Optional

import httpx

from database import to_tr

logger = logging.getLogger("minerva108.parasut")

_BASE = "https://api.parasut.com"
_RATE_SLEEP = 1.05          # 10 istek / 10 sn limiti — güvenli aralık
_TIMEOUT = 25.0

# Nihai tüketici (TCKN'siz B2C) — GİB e-Arşiv konvansiyonu
FINAL_CONSUMER_TCKN = "11111111111"


class ParasutError(RuntimeError):
    """Paraşüt API hatası — gövde mesajı aynen taşınır (panel/bildirim için)."""


# ─── Yapılandırma (runtime — import-time DEĞİL) ──────────────────────────────

def _env(key: str) -> str:
    return (os.getenv(f"PARASUT_{key}", "") or "").strip()


def is_configured() -> bool:
    return all(_env(k) for k in
               ("CLIENT_ID", "CLIENT_SECRET", "COMPANY_ID", "EMAIL", "PASSWORD"))


def company_id() -> str:
    return _env("COMPANY_ID")


def account_id(store_key: Optional[str] = None) -> Optional[int]:
    """Tahsilatın işleneceği Paraşüt kasa/banka hesabı.

    Her markanın KENDİ iyzico hesabı var → mağaza bazlı:
        PARASUT_ACCOUNT_ID_MINERVA / _EVANIRA / _SERENIDA
    Mağazaya özel tanım yoksa genel PARASUT_ACCOUNT_ID_IYZICO'ya düşer;
    o da yoksa None (tahsilat kaydı atlanır, fatura açık kalır).
    """
    for key in ([f"ACCOUNT_ID_{store_key.upper()}"] if store_key else []) + ["ACCOUNT_ID_IYZICO"]:
        try:
            return int(_env(key))
        except (TypeError, ValueError):
            continue
    return None


def default_vat() -> float:
    try:
        return float(_env("DEFAULT_VAT") or 20)
    except ValueError:
        return 20.0


def invoice_mode() -> str:
    """draft (vars): resmileştirme atlanır · official: e-belge kesilir."""
    m = (os.getenv("INVOICE_MODE", "") or "draft").strip().lower()
    return m if m in ("draft", "official") else "draft"


# ─── OAuth token (password grant, in-process cache) ──────────────────────────

_TOKEN_CACHE: dict = {}     # "token" → (access_token, expiry_epoch)


def _get_access_token() -> str:
    cached = _TOKEN_CACHE.get("token")
    if cached and cached[1] > time.time() + 300:
        return cached[0]
    r = httpx.post(
        f"{_BASE}/oauth/token",
        data={"grant_type": "password",
              "client_id": _env("CLIENT_ID"),
              "client_secret": _env("CLIENT_SECRET"),
              "username": _env("EMAIL"),
              "password": _env("PASSWORD"),
              "redirect_uri": "urn:ietf:wg:oauth:2.0:oob"},
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        raise ParasutError(f"Paraşüt girişi başarısız ({r.status_code}): {r.text[:200]}")
    body = r.json()
    token = body.get("access_token")
    if not token:
        raise ParasutError(f"access_token alınamadı: {str(body)[:200]}")
    _TOKEN_CACHE["token"] = (token, time.time() + int(body.get("expires_in", 7200)))
    return token


def _api(method: str, path: str, json: Optional[dict] = None,
         params: Optional[dict] = None) -> dict:
    """Rate-limited JSON:API çağrısı. path company-relative ('/contacts' gibi)."""
    token = _get_access_token()
    url = f"{_BASE}/v4/{company_id()}{path}"
    r = httpx.request(method, url, json=json, params=params, timeout=_TIMEOUT,
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"})
    time.sleep(_RATE_SLEEP)
    if r.status_code == 204:
        return {}
    if r.status_code >= 400:
        raise ParasutError(f"Paraşüt {method} {path} → {r.status_code}: {r.text[:400]}")
    try:
        return r.json()
    except ValueError:
        return {}


# ─── Contact (müşteri) ────────────────────────────────────────────────────────

def find_or_create_contact(order: dict) -> int:
    """Shopify sipariş özetinden Paraşüt müşteri id'si (bul-yoksa-yarat).

    Arama sırası: VKN/TCKN → e-posta. B2C TCKN'siz → nihai tüketici (11111111111).
    """
    vkn = (order.get("vkn_tckn") or "").strip()
    email = (order.get("customer_email") or "").strip()
    name = (order.get("customer_name") or "").strip() or "Shopify Müşterisi"

    if vkn:
        data = _api("GET", "/contacts", params={"filter[tax_number]": vkn, "page[size]": 1})
        rows = data.get("data") or []
        if rows:
            return int(rows[0]["id"])
    if email:
        data = _api("GET", "/contacts", params={"filter[email]": email, "page[size]": 1})
        rows = data.get("data") or []
        if rows:
            return int(rows[0]["id"])

    is_company = bool(vkn) and len(vkn) == 10          # 10 hane VKN = tüzel kişi
    attributes = {
        "name": name[:100],
        "email": email or None,
        "contact_type": "company" if is_company else "person",
        "account_type": "customer",
        "tax_number": vkn or FINAL_CONSUMER_TCKN,
        "tax_office": (order.get("tax_office") or "").strip() or None,
        "city": (order.get("city") or "").strip() or None,
        "district": (order.get("district") or "").strip() or None,
        "address": (order.get("address") or "").strip() or None,
        "phone": (order.get("phone") or "").strip() or None,
    }
    body = {"data": {"type": "contacts",
                     "attributes": {k: v for k, v in attributes.items() if v is not None}}}
    data = _api("POST", "/contacts", json=body)
    return int(data["data"]["id"])


# ─── Product (Paraşüt ürün kaydı — fatura kalemi için ZORUNLU) ───────────────

def find_or_create_product(code: str, name: str, vat_rate: float) -> int:
    """code (barkod/sku) ile ara; yoksa yarat. Fatura detayı product id ister."""
    code = (code or "").strip()
    if code:
        data = _api("GET", "/products", params={"filter[code]": code, "page[size]": 1})
        rows = data.get("data") or []
        if rows:
            return int(rows[0]["id"])
    body = {"data": {"type": "products", "attributes": {
        "name": (name or code or "Shopify Ürünü")[:150],
        "code": code or None,
        "vat_rate": vat_rate,
        "unit": "adet",
    }}}
    body["data"]["attributes"] = {k: v for k, v in body["data"]["attributes"].items()
                                  if v is not None}
    data = _api("POST", "/products", json=body)
    return int(data["data"]["id"])


# ─── Satış faturası ───────────────────────────────────────────────────────────

def create_invoice(order: dict, lines: List[dict], contact_id: int) -> int:
    """Satış faturası oluşturur → invoice id.

    lines: [{code, title, quantity, unit_price_incl (KDV DAHİL), vat_rate,
             discount_amount (KDV dahil, satıra dağıtılmış)}]
    Shopify TR mağazalarında fiyatlar KDV DAHİL → unit_price = incl / (1+vat/100).
    Kargo bedeli varsa 'Kargo Bedeli' generic ürünüyle ayrı satır (order['shipping']).
    """
    details = []
    for ln in lines:
        vat = float(ln.get("vat_rate") or default_vat())
        unit_incl = float(ln["unit_price_incl"])
        unit_excl = round(unit_incl / (1.0 + vat / 100.0), 4)
        pid = find_or_create_product(ln.get("code") or "", ln.get("title") or "", vat)
        att = {"quantity": float(ln["quantity"]),
               "unit_price": unit_excl,
               "vat_rate": vat,
               "description": (ln.get("title") or "")[:250]}
        disc = float(ln.get("discount_amount") or 0)
        if disc > 0:
            att["discount_type"] = "amount"
            # indirim de KDV dahil geldi → hariç tabana çevir
            att["discount_value"] = round(disc / (1.0 + vat / 100.0), 4)
        details.append({
            "type": "sales_invoice_details",
            "attributes": att,
            "relationships": {"product": {"data": {"id": str(pid), "type": "products"}}},
        })

    shipping = float(order.get("shipping_amount") or 0)
    if shipping > 0:
        vat = default_vat()
        pid = find_or_create_product("SHOPIFY-KARGO", "Kargo Bedeli", vat)
        details.append({
            "type": "sales_invoice_details",
            "attributes": {"quantity": 1.0,
                           "unit_price": round(shipping / (1.0 + vat / 100.0), 4),
                           "vat_rate": vat,
                           "description": "Kargo Bedeli"},
            "relationships": {"product": {"data": {"id": str(pid), "type": "products"}}},
        })

    today_tr = to_tr(datetime.utcnow()).strftime("%Y-%m-%d")
    order_date = (order.get("order_date") or today_tr)[:10]
    attributes = {
        "item_type": "invoice",
        "issue_date": today_tr,
        "due_date": today_tr,
        "currency": "TRL",
        "order_no": order.get("order_number") or None,
        "order_date": order_date,
        "billing_address": (order.get("address") or "")[:250] or None,
        "billing_phone": (order.get("phone") or "")[:30] or None,
        "description": f"Shopify {order.get('store_label') or ''} #{order.get('order_number') or ''}".strip(),
        "shipment_included": False,
    }
    body = {"data": {
        "type": "sales_invoices",
        "attributes": {k: v for k, v in attributes.items() if v is not None},
        "relationships": {
            "contact": {"data": {"id": str(contact_id), "type": "contacts"}},
            "details": {"data": details},
        },
    }}
    data = _api("POST", "/sales_invoices", json=body)
    return int(data["data"]["id"])


def add_payment(invoice_id: int, amount: float, date: Optional[str] = None,
                description: str = "iyzico tahsilatı (Shopify)",
                store_key: Optional[str] = None) -> None:
    """Faturayı ilgili markanın iyzico hesabına ödendi işler.

    Hesap `account_id(store_key)` ile çözülür (marka bazlı → genel fallback).
    Hiç tanımlı değilse sessizce atlar — fatura açık kalır (muhasebeci kapatır).
    """
    acc = account_id(store_key)
    if not acc:
        logger.warning("Paraşüt tahsilat hesabı tanımsız (%s) — tahsilat kaydı atlandı",
                       store_key or "genel")
        return
    body = {"data": {"type": "payments", "attributes": {
        "account_id": acc,
        "date": (date or to_tr(datetime.utcnow()).strftime("%Y-%m-%d"))[:10],
        "amount": round(float(amount), 2),
        "description": description[:250],
    }}}
    _api("POST", f"/sales_invoices/{invoice_id}/payments", json=body)


# ─── Resmileştirme (e-Arşiv / e-Fatura) ──────────────────────────────────────

def _is_einvoice_user(vkn: str) -> Optional[dict]:
    """VKN mükellef mi? → ilk gelen kutusu (dict) veya None."""
    if not vkn or vkn == FINAL_CONSUMER_TCKN:
        return None
    data = _api("GET", "/e_invoice_inboxes", params={"filter[vkn]": vkn, "page[size]": 1})
    rows = data.get("data") or []
    return rows[0] if rows else None


def poll_trackable_job(job_id: str, max_wait: float = 60.0) -> str:
    """trackable job durumu: done/error/pending/running. max_wait sonunda son durum."""
    waited = 0.0
    while True:
        data = _api("GET", f"/trackable_jobs/{job_id}")
        status = ((data.get("data") or {}).get("attributes") or {}).get("status") or "pending"
        if status in ("done", "error") or waited >= max_wait:
            if status == "error":
                errs = ((data.get("data") or {}).get("attributes") or {}).get("errors")
                raise ParasutError(f"e-belge işi hata verdi: {str(errs)[:300]}")
            return status
        time.sleep(2.0)
        waited += 2.0 + _RATE_SLEEP


def legalize(order: dict, invoice_id: int) -> dict:
    """Resmi belge keser (INVOICE_MODE=official ise; draft → NO-OP).

    Döner: {mode, doc_type, job_id, job_status}. VKN mükellefse e-Fatura,
    değilse e-Arşiv (İNTERNET SATIŞI zorunlu alanlarıyla).
    """
    if invoice_mode() != "official":
        return {"mode": "draft", "doc_type": None, "job_id": None, "job_status": None}

    vkn = (order.get("vkn_tckn") or "").strip()
    inbox = _is_einvoice_user(vkn)
    payment_date = (order.get("order_date") or
                    to_tr(datetime.utcnow()).strftime("%Y-%m-%d"))[:10]

    if inbox:
        # e-Fatura (mükellef)
        body = {"data": {"type": "e_invoices", "attributes": {
            "to": ((inbox.get("attributes") or {}).get("e_invoice_address")
                   or (inbox.get("attributes") or {}).get("address")),
            "scenario": "basic",
            "note": order.get("note") or None,
        }, "relationships": {
            "invoice": {"data": {"id": str(invoice_id), "type": "sales_invoices"}},
        }}}
        body["data"]["attributes"] = {k: v for k, v in body["data"]["attributes"].items()
                                      if v is not None}
        data = _api("POST", "/e_invoices", json=body)
        doc_type = "e_invoice"
    else:
        # e-Arşiv — internet satışı alanları ZORUNLU
        body = {"data": {"type": "e_archives", "attributes": {
            "internet_sale": {
                "url": order.get("store_url") or "",
                "payment_type": "KREDIKARTI",
                "payment_platform": "iyzico",
                "payment_date": payment_date,
            },
            "shipment": {
                "title": order.get("cargo_company") or "Kargo",
                "date": payment_date,
            },
        }, "relationships": {
            "sales_invoice": {"data": {"id": str(invoice_id), "type": "sales_invoices"}},
        }}}
        data = _api("POST", "/e_archives", json=body)
        doc_type = "e_archive"

    job_id = str(((data.get("data") or {}).get("id")) or "")
    job_status = None
    if job_id:
        job_status = poll_trackable_job(job_id)
    return {"mode": "official", "doc_type": doc_type,
            "job_id": job_id or None, "job_status": job_status}
