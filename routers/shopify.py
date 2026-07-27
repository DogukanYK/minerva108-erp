# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Shopify stok senkron uçları — IMS → Shopify tek yön push (3 mağaza).

  GET  /api/shopify/status    durum + ayarlar (admin.view)
  POST /api/shopify/sync      elle senkron — CANLI push (admin.view)
  GET  /api/shopify/preview   dry-run — ne push edilecek + eşleşmeyen (yazmaz)
  PUT  /api/shopify/config    buffer/enabled + per-store toggle (admin.view)

Kommo /status+/sync kalıbı. Gerçek push zamanlayıcıyla da olur (core/scheduler.py).
Faz 2 (Shopify siparişi → IMS stok düşümü) public webhook stub'ı en altta — YOK.
"""
import base64
import hashlib
import hmac
import json
import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db, ShopifyOrder, to_tr
from core.audit import log_admin_event
from core.permissions import require_permission
from core import shopify as S

logger = logging.getLogger("minerva108.shopify")

router = APIRouter(prefix="/api/shopify", tags=["shopify"])
public_router = APIRouter(prefix="/api/shopify", tags=["shopify-public"])   # Faz 2 (stub)


class SyncRequest(BaseModel):
    brands: Optional[List[str]] = None          # None → yapılandırılmış hepsi


class ConfigRequest(BaseModel):
    buffer: Optional[int] = Field(None, ge=0, le=100000)
    enabled: Optional[bool] = None
    store_key: Optional[str] = Field(None, max_length=20)
    store_enabled: Optional[bool] = None


@router.get("/status")
def shopify_status(db: Session = Depends(get_db),
                   _: dict = Depends(require_permission("admin", "view"))):
    return S.status_summary(db)


@router.post("/sync")
def shopify_sync(data: SyncRequest, request: Request, db: Session = Depends(get_db),
                 current_user: dict = Depends(require_permission("admin", "view"))):
    """Elle senkron — seçili (veya tüm yapılandırılmış) mağazalara CANLI stok push."""
    if not S.any_configured():
        return JSONResponse(status_code=400, content={
            "detail": "Shopify yapılandırılmamış. Prod .env'de SHOPIFY_<MARKA>_STORE/TOKEN/LOCATION gerekli."})
    try:
        summary = S.run_shopify_sync(db, brands=data.brands, dry_run=False)
    except Exception as e:
        logger.exception("Shopify sync hatası")
        return JSONResponse(status_code=502, content={"detail": f"Senkron hatası: {e}"})
    counts = {s["store"]: {"matched": s["matched"], "pushed": s["pushed"]}
              for s in summary["per_store"]}
    log_admin_event(db, request, actor=current_user, action="shopify.sync",
                    target_type="integration", target_name="shopify", details=counts)
    return {"message": "Senkron tamamlandı.", **summary}


@router.get("/preview")
def shopify_preview(db: Session = Depends(get_db),
                    _: dict = Depends(require_permission("admin", "view"))):
    """Dry-run — Shopify'a YAZMAZ, state YAZMAZ. Ne push edileceğini + eşleşmeyeni gösterir."""
    if not S.any_configured():
        return JSONResponse(status_code=400, content={"detail": "Shopify yapılandırılmamış."})
    return S.run_shopify_sync(db, brands=None, dry_run=True)


@router.put("/config")
def shopify_config(data: ConfigRequest, request: Request, db: Session = Depends(get_db),
                   current_user: dict = Depends(require_permission("admin", "view"))):
    """Global buffer/enabled + per-store duraklatma."""
    S.set_config(db, buffer=data.buffer, enabled=data.enabled,
                 store_key_=data.store_key, store_enabled=data.store_enabled)
    log_admin_event(db, request, actor=current_user, action="shopify.config",
                    target_type="integration", target_name="shopify",
                    details={"buffer": data.buffer, "enabled": data.enabled,
                             "store_key": data.store_key, "store_enabled": data.store_enabled})
    return S.status_summary(db)


# ─── Faz 2 — Shopify siparişi → IMS stok düşümü + Paraşüt fatura ─────────────

@public_router.post("/webhook/orders/{store_key}")
async def shopify_order_webhook(store_key: str, request: Request,
                                background: BackgroundTasks):
    """PUBLIC — Shopify orders/paid webhook'u (HMAC imzalı, mağaza başına).

    Ham gövde üzerinden X-Shopify-Hmac-Sha256 doğrulanır (kommo webhook kalıbı);
    iş BackgroundTask'a atılır ve hızlı 200 dönülür (Shopify 5 sn bekler; 2xx
    almazsa yeniden gönderir — handler idempotenttir).
    """
    brand = S.brand_for_store_key((store_key or "").lower())
    if not brand:
        return JSONResponse(status_code=404, content={"detail": "Bilinmeyen mağaza."})
    secret = S.webhook_secret(brand)
    if not secret:
        return JSONResponse(status_code=503, content={"detail": "Mağaza yapılandırılmamış."})

    raw = await request.body()
    sent = request.headers.get("X-Shopify-Hmac-Sha256", "")
    digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    if not sent or not hmac.compare_digest(sent, expected):
        logger.warning("shopify webhook: HMAC uyuşmadı (%s)", store_key)
        return JSONResponse(status_code=401, content={"detail": "İmza doğrulanamadı."})

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(status_code=400, content={"detail": "Geçersiz gövde."})

    background.add_task(S.handle_order_paid, (store_key or "").lower(), payload)
    return {"ok": True}


def _order_view(r: ShopifyOrder) -> dict:
    return {
        "id": r.id, "store_key": r.store_key, "order_number": r.order_number,
        "status": r.status, "step": r.step, "total": r.total, "currency": r.currency,
        "country": r.country, "customer_name": r.customer_name,
        "attempts": r.attempts, "last_error": r.last_error,
        "parasut_invoice_id": r.parasut_invoice_id,
        "parasut_doc_type": r.parasut_doc_type,
        "created_at": to_tr(r.created_at).strftime("%d.%m.%Y %H:%M") if r.created_at else None,
    }


@router.get("/orders")
def list_shopify_orders(limit: int = 50, db: Session = Depends(get_db),
                        _: dict = Depends(require_permission("admin", "view"))):
    """Son siparişler + fatura durumları (panel)."""
    limit = max(1, min(int(limit or 50), 200))
    rows = (db.query(ShopifyOrder).order_by(ShopifyOrder.id.desc()).limit(limit).all())
    from core import parasut as P
    return {"orders": [_order_view(r) for r in rows],
            "parasut_configured": P.is_configured(),
            "invoice_mode": P.invoice_mode()}


@router.post("/orders/{order_id}/retry")
def retry_shopify_order(order_id: int, request: Request, db: Session = Depends(get_db),
                        current_user: dict = Depends(require_permission("admin", "view"))):
    """Başarısız siparişi kaldığı ADIMDAN yeniden dener (stok tekrar düşmez)."""
    row = db.query(ShopifyOrder).filter(ShopifyOrder.id == order_id).first()
    if not row:
        return JSONResponse(status_code=404, content={"detail": "Sipariş bulunamadı."})
    try:
        res = S.retry_order(db, row)
    except Exception as e:
        logger.exception("retry failed")
        return JSONResponse(status_code=502, content={"detail": f"Yeniden deneme hatası: {e}"})
    log_admin_event(db, request, actor=current_user, action="shopify.order_retry",
                    target_type="shopify_order", target_id=row.id,
                    target_name=row.order_number, details=res)
    return {"message": "Yeniden denendi.", **res, "order": _order_view(row)}


_Q_WEBHOOKS = """
query { webhookSubscriptions(first: 50) { nodes { id topic
  endpoint { __typename ... on WebhookHttpEndpoint { callbackUrl } } } } }
"""

_M_WEBHOOK_CREATE = """
mutation($topic: WebhookSubscriptionTopic!, $sub: WebhookSubscriptionInput!) {
  webhookSubscriptionCreate(topic: $topic, webhookSubscription: $sub) {
    webhookSubscription { id }
    userErrors { field message }
  }
}
"""


@router.post("/webhooks/setup")
def setup_shopify_webhooks(request: Request, db: Session = Depends(get_db),
                           current_user: dict = Depends(require_permission("admin", "view"))):
    """Her yapılandırılmış mağazaya ORDERS_PAID aboneliği kurar (İDEMPOTENT).

    Zaten aynı callbackUrl ile abonelik varsa yeniden oluşturmaz.
    """
    base = (request.headers.get("x-forwarded-host") or request.url.hostname or "").strip()
    scheme = "https"
    results = []
    for brand in S._BRANDS:
        cfg = S.store_config(brand)
        sk = S.store_key(brand)
        if not cfg:
            results.append({"store": sk, "configured": False, "skipped": True})
            continue
        callback = f"{scheme}://{base}/api/shopify/webhook/orders/{sk}"
        try:
            with S._client(cfg) as client:
                data = S._graphql(client, _Q_WEBHOOKS, {})
                nodes = ((data.get("webhookSubscriptions") or {}).get("nodes")) or []
                exists = any(n.get("topic") == "ORDERS_PAID"
                             and ((n.get("endpoint") or {}).get("callbackUrl") == callback)
                             for n in nodes)
                if exists:
                    results.append({"store": sk, "configured": True, "created": False,
                                    "callback": callback, "message": "zaten kurulu"})
                    continue
                res = S._graphql(client, _M_WEBHOOK_CREATE,
                                 {"topic": "ORDERS_PAID",
                                  "sub": {"callbackUrl": callback, "format": "JSON"}})
                ue = ((res.get("webhookSubscriptionCreate") or {}).get("userErrors")) or []
                if ue:
                    results.append({"store": sk, "configured": True, "created": False,
                                    "callback": callback, "error": str(ue)[:250]})
                else:
                    results.append({"store": sk, "configured": True, "created": True,
                                    "callback": callback})
        except Exception as e:
            results.append({"store": sk, "configured": True, "created": False,
                            "error": str(e)[:250]})
    log_admin_event(db, request, actor=current_user, action="shopify.webhooks_setup",
                    target_type="integration", target_name="shopify",
                    details={"results": results})
    return {"message": "Webhook kurulumu tamamlandı.", "results": results}
