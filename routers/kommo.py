# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Kommo entegrasyonu uçları — tek yön ayna (Kommo → bizim CRM).

  GET  /status            durum (crm.view)
  POST /import            tam toplu içe aktarma (admin.view)
  POST /sync              delta senkron (admin.view)
  POST /webhook/{secret}  PUBLIC — Kommo olay push'u (secret ile korunur)

Webhook auth YOK (Kommo imza göndermez) → tahmin-edilemez secret URL + işi
arka planda yap, 2 sn içinde hızlı 200 dön (Kommo şartı).
"""
import hmac
import logging

from fastapi import APIRouter, Depends, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from database import get_db
from core.permissions import require_permission
from core.audit import log_admin_event
from core import kommo as K

logger = logging.getLogger("minerva108.kommo")

router = APIRouter(prefix="/api/crm/integrations/kommo", tags=["crm-kommo"])
public_router = APIRouter(prefix="/api/crm/integrations/kommo", tags=["crm-kommo-public"])


@router.get("/status")
def kommo_status(db: Session = Depends(get_db),
                 _: dict = Depends(require_permission("crm", "view"))):
    return K.status_summary(db)


@router.post("/import")
def kommo_import(request: Request, db: Session = Depends(get_db),
                 current_user: dict = Depends(require_permission("admin", "view"))):
    """Tam toplu içe aktarma — Kommo'daki tüm firma/kişi/fırsatları çeker."""
    if not K.is_configured():
        return JSONResponse(status_code=400, content={
            "detail": "Kommo yapılandırılmamış. .env'de KOMMO_SUBDOMAIN ve KOMMO_TOKEN gerekli."})
    try:
        counts = K.run_sync(db, since_epoch=None)
    except Exception as e:
        logger.exception("Kommo import hatası")
        return JSONResponse(status_code=502, content={"detail": f"Kommo'dan veri alınamadı: {e}"})
    log_admin_event(db, request, actor=current_user, action="crm.kommo.import",
                    target_type="integration", target_name="kommo", details=counts)
    return {"message": "İçe aktarma tamamlandı.", "counts": counts}


@router.post("/sync")
def kommo_sync(request: Request, db: Session = Depends(get_db),
               current_user: dict = Depends(require_permission("admin", "view"))):
    """Delta senkron — son senkrondan beri Kommo'da güncellenenleri çeker."""
    if not K.is_configured():
        return JSONResponse(status_code=400, content={"detail": "Kommo yapılandırılmamış."})
    from database import CrmIntegrationState
    since = None
    s = db.query(CrmIntegrationState).filter(CrmIntegrationState.provider == "kommo").first()
    if s and s.cursor:
        since = int(s.cursor)
    try:
        counts = K.run_sync(db, since_epoch=since)
    except Exception as e:
        logger.exception("Kommo sync hatası")
        return JSONResponse(status_code=502, content={"detail": f"Senkron hatası: {e}"})
    log_admin_event(db, request, actor=current_user, action="crm.kommo.sync",
                    target_type="integration", target_name="kommo", details=counts)
    return {"message": "Senkron tamamlandı.", "counts": counts}


@public_router.post("/webhook/{secret}")
async def kommo_webhook(secret: str, request: Request, background: BackgroundTasks):
    """PUBLIC — Kommo olay push'u.  Secret eşleşmezse 403.  İşi arka planda yap,
    hızlı 200 dön (Kommo 2 sn içinde 2xx bekler)."""
    configured = K.webhook_secret()
    if not configured or not hmac.compare_digest(secret, configured):
        return JSONResponse(status_code=403, content={"detail": "Geçersiz webhook anahtarı."})
    try:
        form = await request.form()
        payload = {k: v for k, v in form.items()}
    except Exception:
        payload = {}
    background.add_task(K.handle_webhook, payload)
    return {"ok": True}
