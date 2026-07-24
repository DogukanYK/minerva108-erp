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
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
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


# ─── Faz 2 — Shopify siparişi → IMS stok düşümü (STUB, uygulanmadı) ───────────
# Shopify canlı satışa geçince eklenecek. Tasarım (additif — şema değişmez):
#   @public_router.post("/webhook/orders/{store_key}")  # PUBLIC
#   - Ham gövdeyi oku; X-Shopify-Hmac-Sha256'yı
#       hmac.new(SHOPIFY_<MARKA>_WEBHOOK_SECRET.encode(), raw, sha256).digest() → base64
#       ile compare_digest → uyuşmazsa 403 (kommo_webhook kalıbı).
#   - Hızlı 200 dön; işi BackgroundTasks'a at.
#   - Handler: her kalemin barcode/sku → Item; Item.current_stock -= qty +
#       Transaction(transaction_type="Output", notes="Shopify #<order>", performed_by="Shopify").
#   - İdempotency: Faz-2'de shopify_order_id dedup (yeni kolon/tablo — additif).
#   - Env: SHOPIFY_<MARKA>_WEBHOOK_SECRET (rezerve).
