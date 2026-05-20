# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Push notification subscription management.

Endpoints
─────────
  GET  /api/notifications/vapid-public-key
      Public-by-design — the frontend reads this to pass into
      pushManager.subscribe(). Returns the configured key plus a
      `configured: bool` so the JS can degrade gracefully when push
      is disabled in dev.

  POST /api/notifications/subscribe
      Idempotent upsert. Body matches the browser's PushSubscription
      JSON shape verbatim. Re-subscribing replaces the previous record;
      a different user on the same device transparently reassigns the
      row to the new owner.

  DELETE /api/notifications/subscribe
      Drop the row when the user disables notifications client-side.
"""
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from database import get_db, PushSubscription
from core.auth import get_current_user

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


# ─── Schemas ────────────────────────────────────────────────────────────────

class _SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionRequest(BaseModel):
    endpoint: str
    keys:     _SubscriptionKeys
    expirationTime: Optional[float] = None   # browsers may send this; we ignore it


# ─── Endpoints ──────────────────────────────────────────────────────────────

@router.get("/vapid-public-key")
def get_vapid_public_key():
    """
    Returns the server's VAPID public key for use with pushManager.subscribe().
    `configured: false` means the operator hasn't set VAPID_PUBLIC_KEY yet —
    the frontend should hide its "Bildirimleri Aç" button in that case.
    """
    key = os.getenv("VAPID_PUBLIC_KEY", "")
    return {"key": key, "configured": bool(key)}


@router.post("/subscribe", status_code=201)
def subscribe(
    payload: PushSubscriptionRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Idempotent. Re-posting the same endpoint refreshes timestamps; a different
    user logging in on the same browser silently reassigns the row.
    """
    user_id = int(current_user.get("sub", 0))
    user_agent = (request.headers.get("user-agent") or "")[:255]

    existing = db.query(PushSubscription).filter(
        PushSubscription.endpoint == payload.endpoint
    ).first()

    if existing:
        reassigned = existing.user_id != user_id
        existing.user_id    = user_id
        existing.p256dh     = payload.keys.p256dh
        existing.auth       = payload.keys.auth
        existing.user_agent = user_agent
        # last_used_at auto-bumped via onupdate
        db.commit()
        return {
            "id":         existing.id,
            "reassigned": reassigned,
            "message":    "Abonelik güncellendi.",
        }

    sub = PushSubscription(
        user_id=user_id,
        endpoint=payload.endpoint,
        p256dh=payload.keys.p256dh,
        auth=payload.keys.auth,
        user_agent=user_agent,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return {"id": sub.id, "message": "Bildirimler etkinleştirildi."}


@router.delete("/subscribe")
def unsubscribe(
    payload: PushSubscriptionRequest,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    """User-initiated unsubscribe — silent no-op if endpoint already gone."""
    deleted = db.query(PushSubscription).filter(
        PushSubscription.endpoint == payload.endpoint
    ).delete(synchronize_session=False)
    db.commit()
    return {"deleted": deleted}
