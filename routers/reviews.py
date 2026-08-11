# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Ürün yorumları — davet üretimi + (salt-okunur) liste.

Faz C1 kapsamı: yalnız auth'lu router.  Public yüzey (davet linkinin
tıklanıp form doldurulduğu `/yorum/{token}` sayfası ve ses yükleme ucu)
sonraki fazda `public_router` olarak eklenecek — bkz. routers/shopify.py
ve routers/kommo.py'deki aynı ikili router deseni (router + public_router,
ikisi de api_main.py'de ayrı ayrı include_router edilir).

Moderasyon (onay/red/yayından kaldırma/silme) ve Shopify'a itme (metaobject
+ Files) sonraki fazlarda eklenecek.  Bu dosyadaki liste uçları şimdiden
salt-okunur olarak var ki admin ekranı (templates/yorumlar.html) boş
kalmasın.
"""
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db, ProductReview, ReviewInvite
from core.audit import log_admin_event
from core.auth import get_current_user
from core.permissions import require_permission

router = APIRouter(prefix="/api/reviews", tags=["reviews"])

INVITE_EXPIRY_DAYS = 60


class InviteCreate(BaseModel):
    kind: str = Field(default="gifted", pattern="^(gifted|verified_buyer)$")
    store_key: str = Field(default="minerva")
    shopify_product_id: int = Field(gt=0)
    product_title: Optional[str] = None
    product_handle: Optional[str] = None
    recipient_name: Optional[str] = None
    recipient_email: Optional[str] = None
    locale: str = Field(default="tr")


def _invite_out(inv: ReviewInvite) -> dict:
    return {
        "id": inv.id,
        "token": inv.token,
        "kind": inv.kind,
        "store_key": inv.store_key,
        "shopify_product_id": inv.shopify_product_id,
        "product_title": inv.product_title,
        "product_handle": inv.product_handle,
        "recipient_name": inv.recipient_name,
        "recipient_email": inv.recipient_email,
        "locale": inv.locale,
        "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
        "used_at": inv.used_at.isoformat() if inv.used_at else None,
        "created_by": inv.created_by,
        "created_at": inv.created_at.isoformat() if inv.created_at else None,
        "is_expired": bool(inv.expires_at and inv.expires_at < datetime.utcnow() and not inv.used_at),
    }


def _review_out(r: ProductReview) -> dict:
    return {
        "id": r.id,
        "store_key": r.store_key,
        "shopify_product_id": r.shopify_product_id,
        "product_title": r.product_title,
        "source": r.source,
        "author_name": r.author_name,
        "rating": r.rating,
        "body": r.body,
        "locale": r.locale,
        "has_audio": bool(r.audio_stored_name),
        "audio_duration_s": r.audio_duration_s,
        "transcript": r.transcript,
        "status": r.status,
        "is_published": r.is_published,
        "moderation_note": r.moderation_note,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("/invites")
def list_invites(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reviews", "invite")),
):
    rows = (db.query(ReviewInvite)
            .order_by(ReviewInvite.created_at.desc())
            .limit(200)
            .all())
    return {"invites": [_invite_out(r) for r in rows]}


@router.post("/invites")
def create_invite(
    body: InviteCreate,
    request: Request,
    payload: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reviews", "invite")),
):
    """Hediye ürün daveti üretir.  v1'de yalnız 'gifted' — doğrulanmış-alıcı
    daveti sipariş webhook akışından otomatik üretilecek (ayrı bir iş kalemi,
    core.shopify._process_order'a bağlanacak; TR-dışı export kontrolünden
    ÖNCE üretilmeli, yoksa hiçbir AB müşterisi davet almaz)."""
    if body.kind != "gifted":
        raise HTTPException(
            status_code=400,
            detail="Bu uçtan yalnız 'gifted' (hediye ürün) daveti üretilebilir.",
        )

    token = secrets.token_urlsafe(32)
    inv = ReviewInvite(
        token=token,
        kind=body.kind,
        store_key=body.store_key,
        shopify_product_id=body.shopify_product_id,
        product_title=(body.product_title or "").strip() or None,
        product_handle=(body.product_handle or "").strip() or None,
        recipient_name=(body.recipient_name or "").strip() or None,
        recipient_email=(body.recipient_email or "").strip() or None,
        locale=(body.locale or "tr").strip() or "tr",
        expires_at=datetime.utcnow() + timedelta(days=INVITE_EXPIRY_DAYS),
        created_by=(payload.get("username") or payload.get("full_name") or None),
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)

    log_admin_event(
        db, request, actor=payload, action="review.invite",
        target_type="review_invite", target_id=inv.id,
        target_name=inv.product_title or str(inv.shopify_product_id),
        details={"kind": inv.kind, "store_key": inv.store_key},
    )
    return _invite_out(inv)


@router.get("/invites/{invite_id}/qr")
def invite_qr(
    invite_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reviews", "invite")),
):
    """Hediye kartına basılacak QR — yorum davet linkinin kendisini kodlar.

    İKİ ŞART birden (bkz. routers/pdks.py _qr_svg — aynı sahada yaşanan
    hata, aynı sabit): `make_qr()` (make() DEĞİL — kısa metinlerde Micro QR
    üretir, telefon kameraları okumaz) ve `omitsize=True` (yoksa viewBox
    yazılmaz, sembol kartta kırpılır).
    """
    import io as _io

    import segno

    inv = db.query(ReviewInvite).filter(ReviewInvite.id == invite_id).first()
    if not inv:
        raise HTTPException(status_code=404, detail="Davet bulunamadı.")

    # /yorum/{token} IMS'in kendisinde servis edilir (bkz. routers/drive.py'deki
    # /s/{token} deseni) — Shopify mağazasında DEĞİL. Base URL prod'da .env'den
    # gelir; yoksa canlı IMS domain'ine düşer.
    import os
    base = os.getenv("REVIEWS_BASE_URL", "https://ims.minerva108.com")
    url = f"{base.rstrip('/')}/yorum/{inv.token}"

    buf = _io.BytesIO()
    segno.make_qr(url, error="m").save(
        buf, kind="svg", xmldecl=False, scale=12, dark="#111827", border=2,
        omitsize=True,
    )
    return {"url": url, "svg": buf.getvalue().decode("utf-8")}


@router.get("")
def list_reviews(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reviews", "view")),
):
    """Salt-okunur liste.  Onay/red/yayın uçları sonraki fazda eklenecek."""
    q = db.query(ProductReview)
    if status:
        q = q.filter(ProductReview.status == status)
    rows = q.order_by(ProductReview.created_at.desc()).limit(200).all()
    return {"reviews": [_review_out(r) for r in rows]}
