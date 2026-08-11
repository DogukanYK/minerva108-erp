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

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db, ProductReview, ReviewInvite
from core.audit import log_admin_event
from core.auth import get_current_user
from core.limiter import limiter
from core.permissions import require_permission
from core.reviews import save_audio

router = APIRouter(prefix="/api/reviews", tags=["reviews"])
public_router = APIRouter(prefix="/yorum", tags=["reviews-public"])

# Kendi Jinja2Templates örneği — api_main.templates'i import ETMEZ (api_main
# zaten bu router'ı import ediyor; ters yönde import döngüsel bağımlılık
# yaratırdı). Jinja2Templates hafif bir sarmalayıcı, ikinci bir örnek
# yaratmanın hiçbir maliyeti yok.
_templates = Jinja2Templates(directory="templates")

INVITE_EXPIRY_DAYS = 60
CONSENT_TEXT_VERSION = "v1"
MAX_BODY_CHARS = 1200
MAX_NAME_CHARS = 60


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


# ─── Public — davet linkinin formu (auth YOK) ───────────────────────────────
# GET /yorum/{token} sayfa rotası api_main.py'de (share_page ile aynı desen:
# state'i burada değil orada hesaplayıp templates/yorum.html'e basar).  Bu
# router yalnız gönderim POST'unu taşır — iş mantığı (dosya doğrulama,
# tek-kullanım kilidi, DB yazımı) burada, sayfa render'ı orada.


def load_invite_for_page(token: str, db: Session) -> dict:
    """GET /yorum/{token} (api_main.py) için state hesaplar — share_page'in
    aynı state-makinesi deseni: notfound / expired / used / open."""
    inv = db.query(ReviewInvite).filter(ReviewInvite.token == token).first()
    if not inv:
        return {"state": "notfound"}
    if inv.used_at:
        return {"state": "used", "invite": inv}
    if inv.expires_at and inv.expires_at < datetime.utcnow():
        return {"state": "expired", "invite": inv}
    return {"state": "open", "invite": inv}


@public_router.post("/{token}/gonder")
@limiter.limit("5/hour")
async def submit_review(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
    rating: int = Form(...),
    author_name: str = Form(...),
    body: Optional[str] = Form(None),
    consent: Optional[str] = Form(None),
    audio: Optional[UploadFile] = File(None),
):
    """Davet linkinin formu — auth YOK, herkese açık.  CSRFMiddleware bunu
    zaten muaf tutuyor (anonim gönderende access_token cookie'si yok, bkz.
    api_main.CSRFMiddleware).  Tek kullanımlık kilit: invite satırı
    `with_for_update()` ile kilitlenir ve `used_at`, review INSERT'iyle AYNI
    transaction'da yazılır — iki eşzamanlı gönderim aynı davetten iki yorum
    üretemez (core.shopify._decrement_stock aynı deseni kullanır).

    TUZAK — @limiter.limit("5/hour") TEK BAŞINA yeterli değil: slowapi
    varsayılanı `key_style="url"`dir, yani sayaç route ŞABLONUNA
    (`/yorum/{token}/gonder`) değil isteğin GERÇEK yoluna göre tutulur —
    her token kendi bağımsız sayacına sahiptir.  Farklı (hatta var olmayan)
    token'larla gelen bir sel bu dekoratörle YAKALANMAZ.  Asıl, URL'den
    bağımsız IP bazlı koruma nginx `limit_req zone=review` katmanındadır
    (bkz. CLAUDE.md — routers/product_images.py'nin aynı dersi: tek süreçli
    uvicorn'da auth'suz bir uç bombalanınca tüm ERP etkilenir).  Bu
    dekoratör yalnız aynı linki tekrar tekrar deneyen birine karşı ikincil
    bir savunma — nginx katmanı OLMADAN bu uç gerçekten korunmuyor demektir.
    """
    inv = (db.query(ReviewInvite)
           .filter(ReviewInvite.token == token)
           .with_for_update()
           .first())

    def _error(msg: str, status_code: int = 400):
        ctx = load_invite_for_page(token, db)
        ctx.update({"request": request, "token": token, "error": msg,
                    "form_rating": rating, "form_name": author_name, "form_body": body})
        return _templates.TemplateResponse("yorum.html", ctx, status_code=status_code)

    if not inv:
        return _error("Bu davet bağlantısı geçersiz.", 404)
    if inv.used_at:
        return _error("Bu davet daha önce kullanılmış.", 410)
    if inv.expires_at and inv.expires_at < datetime.utcnow():
        return _error("Bu davet bağlantısının süresi dolmuş.", 410)

    if rating < 1 or rating > 5:
        return _error("Geçerli bir puan seçin (1-5 yıldız).")
    author_name = (author_name or "").strip()[:MAX_NAME_CHARS]
    if not author_name:
        return _error("Adınızı yazın.")
    body = (body or "").strip()[:MAX_BODY_CHARS] or None
    consent_given = (consent or "").lower() in ("on", "true", "1", "yes")
    if not consent_given:
        return _error("Devam etmek için onay kutusunu işaretleyin.")

    audio_stored_name = audio_mime = None
    audio_bytes = None
    if audio is not None and getattr(audio, "filename", None):
        try:
            audio_stored_name, audio_bytes, audio_mime = await save_audio(audio)
        except ValueError as e:
            return _error(str(e))

    if not body and not audio_stored_name:
        return _error("Yazılı bir yorum ya da sesli not eklemelisiniz.")

    review = ProductReview(
        invite_id=inv.id,
        store_key=inv.store_key,
        shopify_product_id=inv.shopify_product_id,
        product_title=inv.product_title,
        source=inv.kind,
        author_name=author_name,
        author_email=inv.recipient_email,
        rating=rating,
        body=body,
        locale=inv.locale or "tr",
        audio_stored_name=audio_stored_name,
        audio_mime=audio_mime,
        audio_bytes=audio_bytes,
        consent_voice=bool(audio_stored_name),
        consent_text_version=CONSENT_TEXT_VERSION,
        consent_ip=(request.client.host if request.client else None),
        consent_at=datetime.utcnow(),
        status="pending",
    )
    inv.used_at = datetime.utcnow()
    db.add(review)
    db.commit()

    return RedirectResponse(url=f"/yorum/{token}", status_code=303)
