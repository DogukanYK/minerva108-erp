# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Influencer programı — panel API'si (`/api/influencer`) + halka açık başvuru /
claim uçları (`/basvuru`).  Faz 1 (plan B4): başvurular, creator'lar, iş
birliği Kanban'ı, ürün gönderimi (Delivery üzerinden — YENİ STOK YOLU YOK),
içerik takibi, dosyalar, ayarlar.  Faz 2 (affiliate/referral/payout/rapor)
BURADA DEĞİL.

Kalıplar: routers/reviews.py (dual router, token tek kullanım
`with_for_update`, @limiter.limit), routers/crm.py (log_admin_event, görev),
routers/delivery.py (`build_delivery` + `ship_core` çekirdekleri).
Cross-cutting — domain kolonu yok; yalnız Delivery (stok) aktif domain'e
yazılır (`active_domain` bağımlılığı, Item de o domain'den bulunur).

Yanıt sözleşmesi: listeler {items:[…], count}, tekil kayıt düz dict, mutasyon
{ok:true, id?, message?}.  Tarihler ISO string + *_label (dd.mm.yyyy) çifti.

TUZAK — @limiter.limit tek başına yeterli değil (bkz. reviews.submit_review):
nginx `limit_req zone=basvuru` + `client_max_body_size 60m` Faz 0'da canlıya
konmalı; bu dekoratör aynı URL'yi döven tek IP'ye karşı ikincil savunmadır.
"""
import io
import json
import re
import secrets
from datetime import date, datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database import (
    get_db, to_tr, AppSetting, Item, User, Delivery, CrmTask, CrmTag, CrmEntityTag,
    InfluencerTier, InfluencerBenchmark, InfluencerCreator, InfluencerAccount,
    InfluencerMetricSnapshot, InfluencerApplication, InfluencerAddress, InfluencerToken,
    InfluencerCampaign, InfluencerCollab, InfluencerCollabStageLog, InfluencerActivity,
    InfluencerFile, InfluencerShipment, InfluencerContent,
)
from core.audit import log_admin_event
from core.domain import active_domain
from core.limiter import limiter
from core.permissions import require_permission
from core.delivery_note import render_delivery_pdf, delivery_doc_filename, content_disposition
from core.drive import slugify, stored_path
from core.influencer_files import save_media
from core import influencer as ENG
from routers.delivery import (
    DeliveryCreate, DeliveryLine, DeliveryError, build_delivery, ship_core, _view as _delivery_view,
)

router = APIRouter(prefix="/api/influencer", tags=["influencer"])
public_router = APIRouter(prefix="/basvuru", tags=["influencer-public"])

STORE_KEYS = ("minerva", "serenida", "evanira")
PLATFORMS = ("instagram", "tiktok", "youtube")
MODELS = ("barter", "kod", "karma", "link")
TOKEN_PURPOSES = ("address", "insights", "agreement")
TOKEN_DEFAULT_DAYS = 14
CONSENT_VERSION = "v1"
MIN_AGE = 18
CONTENT_TYPES = ("reel", "story", "post", "tiktok", "short", "video", "ugc")
CONTENT_STATUSES = ("taslak", "revizyon", "onaylandi", "yayinlandi")
FILE_KINDS = ("insights_video", "screenshot", "agreement", "tax_doc", "draft", "other")
FILE_ENTITIES = ("creator", "collab", "content", "payout", "application")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ─── Genel yardımcılar ───────────────────────────────────────────────────────

def _actor(payload: dict) -> str:
    return payload.get("full_name") or payload.get("username") or "—"


def _uid(payload: dict) -> Optional[int]:
    try:
        return int(payload.get("sub", 0)) or None
    except (TypeError, ValueError):
        return None


def _err(msg: str, status: int = 400):
    return JSONResponse(status_code=status, content={"detail": msg})


def _iso(dt) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat()
    return dt.isoformat() if isinstance(dt, date) else str(dt)


def _lbl(dt) -> Optional[str]:
    """dd.mm.yyyy (datetime → TR saati)."""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return to_tr(dt).strftime("%d.%m.%Y %H:%M")
    return dt.strftime("%d.%m.%Y")


def _pair(out: dict, key: str, dt) -> None:
    out[key] = _iso(dt)
    out[key + "_label"] = _lbl(dt)


def _jload(s, default):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def _jdump(v) -> Optional[str]:
    return json.dumps(v, ensure_ascii=False) if v is not None else None


def _client_ip(request: Request) -> str:
    return (request.client.host if request and request.client else None) or "—"


def _parse_date(s) -> Optional[date]:
    if not s:
        return None
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _get_setting(db: Session, key: str) -> Optional[str]:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row else None


def _set_setting(db: Session, key: str, value: Optional[str]) -> None:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if value is None:
        if row:
            db.delete(row)
        return
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


def _cfg(db: Session) -> dict:
    """influencer.* ayarları — eksikler CFG_DEFAULTS'a düşer."""
    out = dict(ENG.CFG_DEFAULTS)
    rows = db.query(AppSetting).filter(AppSetting.key.like("influencer.%")).all()
    for r in rows:
        if r.key in out and r.value is not None:
            out[r.key] = r.value
    return out


def _float_setting(cfg: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(cfg.get(key) or default)
    except (TypeError, ValueError):
        return default


def _tier_rows(db: Session) -> list:
    rows = db.query(InfluencerTier).order_by(InfluencerTier.sort_order).all()
    return [_tier_out(t) for t in rows]


def _tier_out(t: InfluencerTier) -> dict:
    return {"key": t.key, "label": t.label, "min_followers": t.min_followers,
            "max_followers": t.max_followers, "commission_pct": t.commission_pct,
            "customer_discount_pct": t.customer_discount_pct, "max_gift_items": t.max_gift_items,
            "launch_access": bool(t.launch_access), "code_allowed": bool(t.code_allowed),
            "sort_order": t.sort_order, "is_active": bool(t.is_active),
            "updated_at": _iso(t.updated_at), "updated_by": t.updated_by}


def _benchmark_rows(db: Session) -> list:
    rows = db.query(InfluencerBenchmark).order_by(InfluencerBenchmark.platform,
                                                  InfluencerBenchmark.tier_key,
                                                  InfluencerBenchmark.metric).all()
    return [{"platform": b.platform, "tier_key": b.tier_key, "metric": b.metric,
             "low": b.low, "high": b.high} for b in rows]


def _tier_for_creator(db: Session, c: InfluencerCreator) -> Optional[str]:
    """Elle atanmış kademe varsa o; yoksa en yüksek takipçili hesaba göre."""
    if c.tier_override_key:
        return c.tier_override_key
    accs = db.query(InfluencerAccount).filter(InfluencerAccount.creator_id == c.id).all()
    followers = max([a.followers or 0 for a in accs] or [0])
    return ENG.tier_for(followers, _tier_rows(db) or None)


def _unique_slug(db: Session, base: str) -> str:
    base = (slugify(base) or "creator")[:48]
    slug, n = base, 1
    while db.query(InfluencerCreator.id).filter(InfluencerCreator.slug == slug).first():
        n += 1
        slug = f"{base}-{n}"
    return slug


def _handle_from_url(url: str) -> str:
    """https://instagram.com/@ayse/ → 'ayse'; boşsa ''."""
    s = (url or "").strip()
    s = re.sub(r"^https?://", "", s, flags=re.I)
    s = re.sub(r"[?#].*$", "", s)
    parts = [p for p in s.split("/") if p]
    if not parts:
        return ""
    if len(parts) == 1 and "." in parts[0]:   # yalnız domain
        return ""
    cand = parts[-1]
    for p in parts[1:]:
        if p.startswith("@"):
            cand = p
            break
    return cand.lstrip("@").lower()[:120]


def _tag_names(db: Session, entity_id: int) -> list:
    rows = (db.query(CrmTag.name)
            .join(CrmEntityTag, CrmEntityTag.tag_id == CrmTag.id)
            .filter(CrmEntityTag.entity == "influencer", CrmEntityTag.entity_id == entity_id)
            .order_by(CrmTag.name).all())
    return [r[0] for r in rows]


def _activity(db: Session, payload: Optional[dict], *, creator_id=None, collab_id=None,
              type_="system", subject=None, body=None) -> None:
    db.add(InfluencerActivity(
        creator_id=creator_id, collab_id=collab_id, type=type_, subject=subject, body=body,
        author_user_id=_uid(payload) if payload else None,
        author_name=_actor(payload) if payload else "sistem",
    ))


# ─── Serializer'lar ──────────────────────────────────────────────────────────

def _account_out(a: InfluencerAccount) -> dict:
    out = {
        "id": a.id, "creator_id": a.creator_id, "platform": a.platform, "handle": a.handle,
        "external_id": a.external_id, "followers": a.followers, "following": a.following,
        "posts_count": a.posts_count, "hidden_subscriber_count": bool(a.hidden_subscriber_count),
        "avg_views": a.avg_views, "avg_likes": a.avg_likes, "avg_comments": a.avg_comments,
        "er_follower": a.er_follower, "er_view": a.er_view, "view_per_follower": a.view_per_follower,
        "views_cv": a.views_cv, "audience_geo": _jload(a.audience_geo_json, None),
        "audience_age_gender": _jload(a.audience_age_gender_json, None),
        "metrics_source": a.metrics_source, "is_primary": bool(a.is_primary),
    }
    _pair(out, "metrics_at", a.metrics_at)
    return out


def _address_out(ad: InfluencerAddress) -> dict:
    out = {"id": ad.id, "creator_id": ad.creator_id, "label": ad.label,
           "recipient_name": ad.recipient_name, "line1": ad.line1, "line2": ad.line2,
           "district": ad.district, "city": ad.city, "postal_code": ad.postal_code,
           "country": ad.country, "phone": ad.phone, "is_default": bool(ad.is_default)}
    _pair(out, "verified_at", ad.verified_at)
    return out


def _creator_out(c: InfluencerCreator, db: Session, full: bool = False) -> dict:
    accounts = (db.query(InfluencerAccount).filter(InfluencerAccount.creator_id == c.id)
                .order_by(InfluencerAccount.is_primary.desc(), InfluencerAccount.id).all())
    out = {
        "id": c.id, "slug": c.slug, "full_name": c.full_name, "email": c.email, "phone": c.phone,
        "country": c.country, "city": c.city, "language": c.language, "birth_year": c.birth_year,
        "categories": _jload(c.categories_json, []), "skin_type": c.skin_type,
        "clothing_size": c.clothing_size, "allergies": c.allergies, "accepted_model": c.accepted_model,
        "tier_key": c.tier_key, "tier_override_key": c.tier_override_key,
        "effective_tier_key": c.tier_override_key or c.tier_key,
        "relationship_stage": c.relationship_stage,
        "relationship_label": ENG.RELATIONSHIP_LABELS.get(c.relationship_stage, c.relationship_stage),
        "authenticity_score": c.authenticity_score, "score": _jload(c.score_json, None),
        "verification_level": c.verification_level, "aqs": c.aqs, "fake_pct": c.fake_pct,
        "do_not_resend": bool(c.do_not_resend), "rating": c.rating,
        "owner_user_id": c.owner_user_id, "owner_name": c.owner_name, "source": c.source,
        "notes": c.notes, "is_active": bool(c.is_active), "created_by": c.created_by,
        "followers": max([a.followers or 0 for a in accounts] or [0]),
        "accounts": [_account_out(a) for a in accounts],
        "tags": _tag_names(db, c.id),
    }
    _pair(out, "created_at", c.created_at)
    _pair(out, "updated_at", c.updated_at)
    if full:
        out["addresses"] = [_address_out(a) for a in db.query(InfluencerAddress)
                            .filter(InfluencerAddress.creator_id == c.id)
                            .order_by(InfluencerAddress.is_default.desc(), InfluencerAddress.id).all()]
        out["collabs"] = [_collab_out(x, db) for x in db.query(InfluencerCollab)
                          .filter(InfluencerCollab.creator_id == c.id)
                          .order_by(InfluencerCollab.id.desc()).all()]
        out["activities"] = [_activity_out(a) for a in db.query(InfluencerActivity)
                             .filter(InfluencerActivity.creator_id == c.id)
                             .order_by(InfluencerActivity.created_at.desc()).limit(100).all()]
        out["files"] = [_file_out(f) for f in db.query(InfluencerFile)
                        .filter(InfluencerFile.entity == "creator", InfluencerFile.entity_id == c.id)
                        .order_by(InfluencerFile.id.desc()).all()]
        out["tokens"] = [_token_out(t) for t in db.query(InfluencerToken)
                         .filter(InfluencerToken.creator_id == c.id)
                         .order_by(InfluencerToken.id.desc()).limit(20).all()]
    return out


def _activity_out(a: InfluencerActivity) -> dict:
    out = {"id": a.id, "creator_id": a.creator_id, "collab_id": a.collab_id, "type": a.type,
           "subject": a.subject, "body": a.body, "author_name": a.author_name,
           "is_pinned": bool(a.is_pinned)}
    _pair(out, "created_at", a.created_at)
    return out


def _file_out(f: InfluencerFile) -> dict:
    out = {"id": f.id, "entity": f.entity, "entity_id": f.entity_id, "kind": f.kind,
           "original_name": f.original_name, "size_bytes": f.size_bytes,
           "content_type": f.content_type, "uploaded_by": f.uploaded_by,
           "url": f"/api/influencer/files/{f.id}"}
    _pair(out, "created_at", f.created_at)
    return out


def _token_out(t: InfluencerToken) -> dict:
    out = {"id": t.id, "token": t.token, "purpose": t.purpose, "creator_id": t.creator_id,
           "collab_id": t.collab_id, "url": f"/basvuru/t/{t.token}",
           "state": _token_state(t)}
    _pair(out, "expires_at", t.expires_at)
    _pair(out, "used_at", t.used_at)
    return out


def _token_state(t: Optional[InfluencerToken]) -> str:
    if not t:
        return "notfound"
    if t.used_at:
        return "used"
    if t.expires_at and t.expires_at < datetime.utcnow():
        return "expired"
    return "open"


def _application_out(a: InfluencerApplication) -> dict:
    out = {
        "id": a.id, "store_key": a.store_key, "full_name": a.full_name, "email": a.email,
        "phone": a.phone, "country": a.country, "city": a.city, "language": a.language,
        "birth_year": a.birth_year, "accounts": _jload(a.accounts_json, []),
        "form": _jload(a.form_json, {}), "status": a.status, "reject_reason": a.reject_reason,
        "reviewed_by": a.reviewed_by, "creator_id": a.creator_id,
        "consent_text_version": a.consent_text_version, "honeypot_hit": bool(a.honeypot_hit),
        "followers": max([int(x.get("followers_claimed") or x.get("followers") or 0)
                          for x in _jload(a.accounts_json, []) if isinstance(x, dict)] or [0]),
    }
    _pair(out, "created_at", a.created_at)
    _pair(out, "reviewed_at", a.reviewed_at)
    return out


def _collab_out(c: InfluencerCollab, db: Session, full: bool = False) -> dict:
    cr = c.creator
    out = {
        "id": c.id, "code": c.code, "creator_id": c.creator_id,
        "creator_name": cr.full_name if cr else None,
        "creator_slug": cr.slug if cr else None,
        "creator_tier_key": (cr.tier_override_key or cr.tier_key) if cr else None,
        "campaign_id": c.campaign_id, "campaign_name": c.campaign.name if c.campaign else None,
        "store_key": c.store_key, "market": c.market, "model": c.model,
        "tier_key_at_start": c.tier_key_at_start, "stage": c.stage,
        "stage_label": ENG.STAGE_LABELS.get(c.stage, c.stage),
        "days_in_stage": ((datetime.utcnow() - c.stage_changed_at).days if c.stage_changed_at else None),
        "usage_rights": c.usage_rights, "exclusivity_days": c.exclusivity_days,
        "guideline_version": c.guideline_version,
        "deliverables": _jload(c.deliverables_json, []), "cancel_reason": c.cancel_reason,
        "decision": c.decision, "decision_label": ENG.DECISION_LABELS.get(c.decision or "", None),
        "decision_suggested": _jload(c.decision_suggested_json, None),
        "owner_user_id": c.owner_user_id, "owner_name": c.owner_name, "notes": c.notes,
        "is_active": bool(c.is_active), "created_by": c.created_by,
    }
    for k in ("stage_changed_at", "offered_at", "accepted_at", "content_due_on",
              "guideline_ack_at", "no_post_flagged_at", "created_at", "updated_at"):
        _pair(out, k, getattr(c, k))
    if full:
        out["shipments"] = [_shipment_out(s, db) for s in db.query(InfluencerShipment)
                            .filter(InfluencerShipment.collab_id == c.id)
                            .order_by(InfluencerShipment.id).all()]
        out["contents"] = [_content_out(x) for x in db.query(InfluencerContent)
                           .filter(InfluencerContent.collab_id == c.id)
                           .order_by(InfluencerContent.id).all()]
        out["stage_log"] = [{"id": l.id, "from_stage": l.from_stage, "to_stage": l.to_stage,
                             "from_label": ENG.STAGE_LABELS.get(l.from_stage or "", l.from_stage),
                             "to_label": ENG.STAGE_LABELS.get(l.to_stage, l.to_stage),
                             "reason": l.reason, "actor": l.actor,
                             "at": _iso(l.at), "at_label": _lbl(l.at)}
                            for l in db.query(InfluencerCollabStageLog)
                            .filter(InfluencerCollabStageLog.collab_id == c.id)
                            .order_by(InfluencerCollabStageLog.at, InfluencerCollabStageLog.id).all()]
        out["activities"] = [_activity_out(a) for a in db.query(InfluencerActivity)
                             .filter(InfluencerActivity.collab_id == c.id)
                             .order_by(InfluencerActivity.created_at.desc()).limit(100).all()]
        out["allowed_stages"] = sorted(ENG.TRANSITIONS.get(c.stage, set()), key=ENG.STAGES.index)
    return out


def _shipment_out(s: InfluencerShipment, db: Session) -> dict:
    d = s.delivery
    out = {
        "id": s.id, "collab_id": s.collab_id, "delivery_id": s.delivery_id,
        "collab_code": s.collab.code if s.collab else None,
        "creator_id": s.collab.creator_id if s.collab else None,
        "creator_name": s.collab.creator.full_name if (s.collab and s.collab.creator) else None,
        "address_id": s.address_id, "cogs_total": s.cogs_total, "packaging_cost": s.packaging_cost,
        "shipping_cost": s.shipping_cost, "loaded_cost": s.loaded_cost,
        "handwritten_note": s.handwritten_note, "created_by": s.created_by,
        "reminder1_task_id": s.reminder1_task_id, "reminder2_task_id": s.reminder2_task_id,
        "document_no": d.document_no if d else None,
        "delivery_status": d.status if d else None,
        "tracking_no": d.tracking_no if d else None, "carrier": d.carrier if d else None,
        "recipient_name": d.recipient_name if d else None,
        "items": ([{"item_id": i.item_id, "item_name": i.item_name, "quantity": i.quantity,
                    "unit": i.unit} for i in d.items] if d else []),
        "document_url": f"/api/influencer/shipments/{s.id}/document",
    }
    for k in ("shipped_at", "delivered_at", "reminder1_due", "reminder2_due", "created_at"):
        _pair(out, k, getattr(s, k))
    return out


def _content_out(x: InfluencerContent) -> dict:
    out = {"id": x.id, "collab_id": x.collab_id, "platform": x.platform, "type": x.type,
           "url": x.url, "status": x.status, "draft_file_id": x.draft_file_id,
           "caption": x.caption, "review_note": x.review_note, "approved_by": x.approved_by,
           "metrics_d7": _jload(x.metrics_d7_json, None), "metrics_d30": _jload(x.metrics_d30_json, None),
           "compliance": _jload(x.compliance_json, None), "compliance_score": x.compliance_score,
           "created_by": x.created_by}
    for k in ("approved_at", "published_at", "created_at", "updated_at"):
        _pair(out, k, getattr(x, k))
    return out


def _campaign_out(c: InfluencerCampaign) -> dict:
    out = {"id": c.id, "name": c.name, "store_key": c.store_key, "market": c.market,
           "hashtags": c.hashtags, "brief_text": c.brief_text,
           "claim_sheet": _jload(c.claim_sheet_json, []),
           "default_deliverables": _jload(c.default_deliverables_json, []),
           "status": c.status, "is_active": bool(c.is_active), "created_by": c.created_by}
    for k in ("start_on", "end_on", "created_at", "updated_at"):
        _pair(out, k, getattr(c, k))
    return out


# ─── Aşama geçişi (tek kaynak) ───────────────────────────────────────────────

def _move_stage(db: Session, c: InfluencerCollab, to_stage: str, actor: str,
                reason: Optional[str] = None, strict: bool = True) -> bool:
    """TRANSITIONS doğrulamalı geçiş + stage_log.  strict=False: izinsiz geçişte
    sessizce False döner (yan etki olarak tetiklenen otomatik geçişler)."""
    if c.stage == to_stage:
        return False
    if not ENG.can_transition(c.stage, to_stage):
        if strict:
            raise HTTPException(status_code=400, detail=(
                f"Geçersiz geçiş: {ENG.STAGE_LABELS.get(c.stage, c.stage)} → "
                f"{ENG.STAGE_LABELS.get(to_stage, to_stage)}"))
        return False
    if to_stage == "iptal" and not (reason or "").strip():
        raise HTTPException(status_code=400, detail="İptal için gerekçe zorunludur.")
    db.add(InfluencerCollabStageLog(collab_id=c.id, from_stage=c.stage, to_stage=to_stage,
                                    reason=(reason or "").strip() or None, actor=actor))
    now = datetime.utcnow()
    c.stage = to_stage
    c.stage_changed_at = now
    c.updated_at = now
    if to_stage == "kabul_edildi" and not c.accepted_at:
        c.accepted_at = now
    if to_stage == "iptal":
        c.cancel_reason = (reason or "").strip()
    return True


def _next_code(db: Session) -> str:
    year = datetime.utcnow().year
    prefix = f"INF-{year}-"
    seq = db.query(func.count(InfluencerCollab.id)).filter(InfluencerCollab.code.like(prefix + "%")).scalar() or 0
    while True:
        seq += 1
        code = ENG.next_collab_code(seq, year)
        if not db.query(InfluencerCollab.id).filter(InfluencerCollab.code == code).first():
            return code


# ═══════════════════════════════════════════════════════════════════════════
#  BAŞVURULAR
# ═══════════════════════════════════════════════════════════════════════════

class AccountIn(BaseModel):
    platform: str = Field(..., max_length=20)
    url: Optional[str] = Field(None, max_length=500)
    handle: Optional[str] = Field(None, max_length=120)
    followers_claimed: Optional[int] = Field(None, ge=0)
    followers: Optional[int] = Field(None, ge=0)


class ApplicationManualIn(BaseModel):
    store_key: str = Field("minerva", max_length=20)
    full_name: str = Field(..., min_length=2, max_length=150)
    email: str = Field(..., max_length=150)
    phone: Optional[str] = Field(None, max_length=50)
    country: Optional[str] = Field(None, max_length=60)
    city: Optional[str] = Field(None, max_length=100)
    language: Optional[str] = Field("tr", max_length=10)
    birth_year: Optional[int] = Field(None, ge=1900, le=2100)
    accounts: List[AccountIn] = Field(default_factory=list)
    niches: List[str] = Field(default_factory=list)
    skin_type: Optional[str] = Field(None, max_length=40)
    recent_links: List[str] = Field(default_factory=list)
    why: Optional[str] = Field(None, max_length=2000)
    past_brands: Optional[str] = Field(None, max_length=1000)
    accepted_model: Optional[str] = Field("barter", max_length=20)
    notes: Optional[str] = Field(None, max_length=2000)


def _norm_accounts(accounts: List[AccountIn]) -> list:
    out = []
    for a in accounts or []:
        platform = (a.platform or "").strip().lower()
        if platform not in PLATFORMS:
            continue
        handle = (a.handle or "").strip().lstrip("@").lower() or _handle_from_url(a.url or "")
        followers = a.followers_claimed if a.followers_claimed is not None else a.followers
        out.append({"platform": platform, "handle": handle[:120], "url": (a.url or "").strip()[:500],
                    "followers_claimed": int(followers or 0)})
    return out


@router.get("/applications")
def list_applications(status: Optional[str] = None, store_key: Optional[str] = None,
                      q: Optional[str] = None, include_honeypot: int = 0,
                      db: Session = Depends(get_db),
                      _: dict = Depends(require_permission("influencer", "view"))):
    qry = db.query(InfluencerApplication)
    if not include_honeypot:
        qry = qry.filter(InfluencerApplication.honeypot_hit == False)  # noqa: E712
    if status:
        qry = qry.filter(InfluencerApplication.status == status)
    if store_key:
        qry = qry.filter(InfluencerApplication.store_key == store_key)
    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(or_(InfluencerApplication.full_name.ilike(like),
                             InfluencerApplication.email.ilike(like)))
    rows = qry.order_by(InfluencerApplication.created_at.desc()).limit(500).all()
    items = [_application_out(a) for a in rows]
    # Duplicate uyarısı — aynı e-postayla mevcut creator
    emails = {ENG.normalize_email(a.email) for a in rows if a.email}
    existing = {}
    if emails:
        for c in db.query(InfluencerCreator).filter(func.lower(InfluencerCreator.email).in_(emails)).all():
            existing[ENG.normalize_email(c.email)] = c.id
    for it in items:
        it["existing_creator_id"] = existing.get(ENG.normalize_email(it["email"]))
    return {"items": items, "count": len(items)}


@router.post("/applications/manual", status_code=201)
def create_application_manual(body: ApplicationManualIn, request: Request,
                              db: Session = Depends(get_db),
                              current_user: dict = Depends(require_permission("influencer", "edit"))):
    """Elle başvuru (DM'den gelen creator) — pending olarak açılır, aynı karar akışı."""
    email = (body.email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        return _err("Geçerli bir e-posta girin.")
    if body.store_key not in STORE_KEYS:
        return _err("Geçersiz mağaza.")
    app_row = InfluencerApplication(
        store_key=body.store_key, full_name=body.full_name.strip(), email=email,
        phone=(body.phone or "").strip() or None, country=(body.country or "").strip().upper()[:60] or None,
        city=(body.city or "").strip() or None, language=(body.language or "tr").lower()[:10],
        birth_year=body.birth_year, accounts_json=_jdump(_norm_accounts(body.accounts)),
        form_json=_jdump({"niches": body.niches, "skin_type": body.skin_type,
                          "recent_links": body.recent_links[:3], "why": body.why,
                          "past_brands": body.past_brands, "accepted_model": body.accepted_model,
                          "notes": body.notes, "source": "manual"}),
        status="pending", user_agent="manual",
    )
    db.add(app_row)
    db.commit()
    db.refresh(app_row)
    log_admin_event(db, request, actor=current_user, action="influencer.application.manual",
                    target_type="influencer_application", target_id=app_row.id,
                    target_name=app_row.full_name)
    return {"ok": True, "id": app_row.id, "message": "Başvuru eklendi."}


class DecideIn(BaseModel):
    decision: str = Field(..., max_length=12)        # approved | rejected | maybe
    reject_reason: Optional[str] = Field(None, max_length=2000)
    link_creator_id: Optional[int] = None
    owner_user_id: Optional[int] = None


def _creator_from_application(db: Session, a: InfluencerApplication, actor: str,
                              owner_user_id: Optional[int]) -> InfluencerCreator:
    form = _jload(a.form_json, {})
    c = InfluencerCreator(
        slug=_unique_slug(db, a.full_name), full_name=a.full_name, email=(a.email or "").lower(),
        phone=a.phone, country=a.country, city=a.city, language=a.language, birth_year=a.birth_year,
        categories_json=_jdump(form.get("niches") or []), skin_type=form.get("skin_type"),
        accepted_model=form.get("accepted_model") or "barter",
        relationship_stage="basvurdu", source="basvuru", created_by=actor,
        owner_user_id=owner_user_id, notes=form.get("notes"),
    )
    if owner_user_id:
        u = db.query(User).filter(User.id == owner_user_id).first()
        c.owner_name = u.full_name if u else None
    db.add(c)
    db.flush()
    _attach_accounts(db, c, _jload(a.accounts_json, []))
    c.tier_key = _tier_for_creator(db, c)
    return c


def _attach_accounts(db: Session, c: InfluencerCreator, accounts: list) -> None:
    first = not db.query(InfluencerAccount.id).filter(InfluencerAccount.creator_id == c.id).first()
    for acc in accounts or []:
        if not isinstance(acc, dict):
            continue
        platform, handle = acc.get("platform"), (acc.get("handle") or "").strip().lstrip("@").lower()
        if platform not in PLATFORMS or not handle:
            continue
        exists = (db.query(InfluencerAccount)
                  .filter(InfluencerAccount.platform == platform, InfluencerAccount.handle == handle).first())
        if exists:
            continue
        db.add(InfluencerAccount(creator_id=c.id, platform=platform, handle=handle,
                                 followers=int(acc.get("followers_claimed") or acc.get("followers") or 0) or None,
                                 metrics_source="manual", is_primary=first))
        first = False
    db.flush()


@router.post("/applications/{aid}/decide")
def decide_application(aid: int, body: DecideIn, request: Request, db: Session = Depends(get_db),
                       current_user: dict = Depends(require_permission("influencer", "approve"))):
    """Onayla / Belki / Reddet.  Onayda creator: `link_creator_id` → o karta;
    yoksa aynı e-postalı mevcut creator; o da yoksa yeni kart + hesaplar."""
    a = db.query(InfluencerApplication).filter(InfluencerApplication.id == aid).first()
    if not a:
        return _err("Başvuru bulunamadı.", 404)
    decision = (body.decision or "").strip().lower()
    if decision not in ("approved", "rejected", "maybe"):
        return _err("Karar approved / rejected / maybe olmalı.")
    if a.status == "approved" and a.creator_id:
        return _err("Başvuru zaten onaylanmış.")
    actor = _actor(current_user)
    creator = None
    if decision == "approved":
        if body.link_creator_id:
            creator = db.query(InfluencerCreator).filter(InfluencerCreator.id == body.link_creator_id).first()
            if not creator:
                return _err("Bağlanacak creator bulunamadı.", 404)
        else:
            em = ENG.normalize_email(a.email)
            if em:
                creator = db.query(InfluencerCreator).filter(func.lower(InfluencerCreator.email) == em).first()
        if creator:
            _attach_accounts(db, creator, _jload(a.accounts_json, []))
            if creator.relationship_stage == "havuz":
                creator.relationship_stage = "basvurdu"
            creator.tier_key = _tier_for_creator(db, creator)
            creator.updated_at = datetime.utcnow()
        else:
            creator = _creator_from_application(db, a, actor, body.owner_user_id)
        a.creator_id = creator.id
        _activity(db, current_user, creator_id=creator.id, subject="Başvuru onaylandı",
                  body=f"Başvuru #{a.id} ({a.store_key})")
    a.status = decision
    a.reject_reason = (body.reject_reason or "").strip() or None
    a.reviewed_by = actor
    a.reviewed_at = datetime.utcnow()
    db.commit()
    log_admin_event(db, request, actor=current_user, action=f"influencer.application.{decision}",
                    target_type="influencer_application", target_id=a.id, target_name=a.full_name,
                    details={"creator_id": a.creator_id, "reason": a.reject_reason})
    return {"ok": True, "id": a.id, "creator_id": a.creator_id, "status": a.status}


# ═══════════════════════════════════════════════════════════════════════════
#  CREATOR'LAR — literal uçlar {cid}'den ÖNCE
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/creators/lookup")
def lookup_creators(q: str = "", db: Session = Depends(get_db),
                    _: dict = Depends(require_permission("influencer", "view"))):
    """Hızlı arama (duplicate uyarısı / seçici): ad, e-posta, slug, hesap handle'ı."""
    s = (q or "").strip()
    if len(s) < 2:
        return {"items": [], "count": 0}
    like = f"%{s}%"
    handle_ids = [r[0] for r in db.query(InfluencerAccount.creator_id)
                  .filter(InfluencerAccount.handle.ilike(like)).limit(50).all()]
    rows = (db.query(InfluencerCreator)
            .filter(or_(InfluencerCreator.full_name.ilike(like), InfluencerCreator.email.ilike(like),
                        InfluencerCreator.slug.ilike(like), InfluencerCreator.id.in_(handle_ids or [0])))
            .order_by(InfluencerCreator.full_name).limit(20).all())
    items = [{"id": c.id, "full_name": c.full_name, "email": c.email, "slug": c.slug,
              "relationship_stage": c.relationship_stage, "tier_key": c.tier_override_key or c.tier_key,
              "do_not_resend": bool(c.do_not_resend)} for c in rows]
    return {"items": items, "count": len(items)}


@router.get("/creators/export")
def export_creators(db: Session = Depends(get_db),
                    _: dict = Depends(require_permission("influencer", "view"))):
    """Creator listesi Excel (openpyxl)."""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Creatorlar"
    ws.append(["ID", "Ad Soyad", "E-posta", "Telefon", "Ülke", "Şehir", "Kademe", "İlişki",
               "Skor", "Takipçi", "Hesaplar", "Tekrar Gönderme", "Kaynak", "Kayıt"])
    for c in db.query(InfluencerCreator).order_by(InfluencerCreator.full_name).all():
        accs = db.query(InfluencerAccount).filter(InfluencerAccount.creator_id == c.id).all()
        ws.append([c.id, c.full_name, c.email or "", c.phone or "", c.country or "", c.city or "",
                   c.tier_override_key or c.tier_key or "",
                   ENG.RELATIONSHIP_LABELS.get(c.relationship_stage, c.relationship_stage),
                   c.authenticity_score, max([a.followers or 0 for a in accs] or [0]),
                   ", ".join(f"{a.platform}:@{a.handle}" for a in accs),
                   "EVET" if c.do_not_resend else "", c.source or "", _lbl(c.created_at) or ""])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"influencer_creatorlar_{datetime.utcnow():%Y%m%d}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(fname)})


class CreatorIn(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=150)
    email: Optional[str] = Field(None, max_length=150)
    phone: Optional[str] = Field(None, max_length=50)
    country: Optional[str] = Field(None, max_length=60)
    city: Optional[str] = Field(None, max_length=100)
    language: Optional[str] = Field(None, max_length=10)
    birth_year: Optional[int] = Field(None, ge=1900, le=2100)
    categories: Optional[List[str]] = None
    skin_type: Optional[str] = Field(None, max_length=40)
    clothing_size: Optional[str] = Field(None, max_length=20)
    allergies: Optional[str] = Field(None, max_length=2000)
    accepted_model: Optional[str] = Field(None, max_length=20)
    tier_override_key: Optional[str] = Field(None, max_length=20)
    verification_level: Optional[str] = Field(None, max_length=4)
    fake_pct: Optional[float] = Field(None, ge=0, le=100)
    do_not_resend: Optional[bool] = None
    rating: Optional[int] = Field(None, ge=1, le=5)
    owner_user_id: Optional[int] = None
    source: Optional[str] = Field(None, max_length=50)
    notes: Optional[str] = Field(None, max_length=5000)
    is_active: Optional[bool] = None
    accounts: Optional[List[AccountIn]] = None       # yalnız create'te kullanılır


def _apply_creator(db: Session, c: InfluencerCreator, body: CreatorIn) -> None:
    c.full_name = body.full_name.strip()
    c.email = (body.email or "").strip().lower() or None
    c.phone = (body.phone or "").strip() or None
    c.country = (body.country or "").strip().upper()[:60] or None
    c.city = (body.city or "").strip() or None
    c.language = (body.language or "").strip().lower() or None
    c.birth_year = body.birth_year
    if body.categories is not None:
        c.categories_json = _jdump([str(x).strip() for x in body.categories if str(x).strip()])
    c.skin_type = body.skin_type
    c.clothing_size = body.clothing_size
    c.allergies = body.allergies
    c.accepted_model = body.accepted_model
    if body.tier_override_key is not None:
        key = body.tier_override_key.strip() or None
        if key and not db.query(InfluencerTier.id).filter(InfluencerTier.key == key).first():
            raise HTTPException(status_code=400, detail="Bilinmeyen kademe.")
        c.tier_override_key = key
    c.verification_level = body.verification_level
    c.fake_pct = body.fake_pct
    if body.do_not_resend is not None:
        c.do_not_resend = bool(body.do_not_resend)
    c.rating = body.rating
    if body.owner_user_id is not None:
        c.owner_user_id = body.owner_user_id or None
        u = db.query(User).filter(User.id == body.owner_user_id).first() if body.owner_user_id else None
        c.owner_name = u.full_name if u else None
    if body.source is not None:
        c.source = body.source
    c.notes = body.notes
    if body.is_active is not None:
        c.is_active = bool(body.is_active)
    c.updated_at = datetime.utcnow()


@router.get("/creators")
def list_creators(q: Optional[str] = None, stage: Optional[str] = None, tier: Optional[str] = None,
                  active: int = 1, db: Session = Depends(get_db),
                  _: dict = Depends(require_permission("influencer", "view"))):
    qry = db.query(InfluencerCreator)
    if active:
        qry = qry.filter(InfluencerCreator.is_active == True)  # noqa: E712
    if stage:
        qry = qry.filter(InfluencerCreator.relationship_stage == stage)
    if tier:
        qry = qry.filter(or_(InfluencerCreator.tier_override_key == tier,
                             (InfluencerCreator.tier_override_key.is_(None)) & (InfluencerCreator.tier_key == tier)))
    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(or_(InfluencerCreator.full_name.ilike(like), InfluencerCreator.email.ilike(like),
                             InfluencerCreator.slug.ilike(like)))
    rows = qry.order_by(InfluencerCreator.full_name).limit(1000).all()
    items = [_creator_out(c, db) for c in rows]
    return {"items": items, "count": len(items)}


@router.post("/creators", status_code=201)
def create_creator(body: CreatorIn, request: Request, db: Session = Depends(get_db),
                   current_user: dict = Depends(require_permission("influencer", "edit"))):
    if body.email:
        em = ENG.normalize_email(body.email)
        if not _EMAIL_RE.match(em):
            return _err("Geçerli bir e-posta girin.")
        if db.query(InfluencerCreator.id).filter(func.lower(InfluencerCreator.email) == em).first():
            return _err("Bu e-postayla kayıtlı bir creator zaten var.", 409)
    c = InfluencerCreator(slug=_unique_slug(db, body.full_name), full_name=body.full_name.strip(),
                          created_by=_actor(current_user), source=body.source or "manual")
    _apply_creator(db, c, body)
    db.add(c)
    db.flush()
    _attach_accounts(db, c, _norm_accounts(body.accounts or []))
    c.tier_key = _tier_for_creator(db, c)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.creator.create",
                    target_type="influencer_creator", target_id=c.id, target_name=c.full_name)
    return {"ok": True, "id": c.id, "slug": c.slug, "message": "Creator oluşturuldu."}


@router.get("/creators/{cid}")
def get_creator(cid: int, db: Session = Depends(get_db),
                _: dict = Depends(require_permission("influencer", "view"))):
    c = db.query(InfluencerCreator).filter(InfluencerCreator.id == cid).first()
    if not c:
        return _err("Creator bulunamadı.", 404)
    return _creator_out(c, db, full=True)


@router.put("/creators/{cid}")
def update_creator(cid: int, body: CreatorIn, request: Request, db: Session = Depends(get_db),
                   current_user: dict = Depends(require_permission("influencer", "edit"))):
    c = db.query(InfluencerCreator).filter(InfluencerCreator.id == cid).first()
    if not c:
        return _err("Creator bulunamadı.", 404)
    if body.email:
        em = ENG.normalize_email(body.email)
        other = db.query(InfluencerCreator.id).filter(func.lower(InfluencerCreator.email) == em,
                                                      InfluencerCreator.id != cid).first()
        if other:
            return _err("Bu e-posta başka bir creator'da kayıtlı.", 409)
    _apply_creator(db, c, body)
    c.tier_key = _tier_for_creator(db, c)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.creator.update",
                    target_type="influencer_creator", target_id=c.id, target_name=c.full_name)
    return {"ok": True, "id": c.id}


class StageIn(BaseModel):
    stage: str = Field(..., max_length=30)
    reason: Optional[str] = Field(None, max_length=2000)


@router.post("/creators/{cid}/stage")
def set_creator_stage(cid: int, body: StageIn, request: Request, db: Session = Depends(get_db),
                      current_user: dict = Depends(require_permission("influencer", "edit"))):
    c = db.query(InfluencerCreator).filter(InfluencerCreator.id == cid).first()
    if not c:
        return _err("Creator bulunamadı.", 404)
    if body.stage not in ENG.RELATIONSHIP_STAGES:
        return _err("Geçersiz ilişki aşaması.")
    old = c.relationship_stage
    c.relationship_stage = body.stage
    if body.stage == "kara_liste":
        c.do_not_resend = True
    c.updated_at = datetime.utcnow()
    _activity(db, current_user, creator_id=c.id, subject="İlişki aşaması değişti",
              body=f"{ENG.RELATIONSHIP_LABELS.get(old, old)} → {ENG.RELATIONSHIP_LABELS.get(body.stage)}"
                   + (f" — {body.reason}" if body.reason else ""))
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.creator.stage",
                    target_type="influencer_creator", target_id=c.id, target_name=c.full_name,
                    details={"from": old, "to": body.stage, "reason": body.reason})
    return {"ok": True, "id": c.id, "relationship_stage": c.relationship_stage}


class AccountBody(BaseModel):
    platform: str = Field(..., max_length=20)
    handle: str = Field(..., min_length=1, max_length=120)
    external_id: Optional[str] = Field(None, max_length=80)
    followers: Optional[int] = Field(None, ge=0)
    following: Optional[int] = Field(None, ge=0)
    posts_count: Optional[int] = Field(None, ge=0)
    hidden_subscriber_count: Optional[bool] = None
    is_primary: Optional[bool] = None


@router.post("/creators/{cid}/accounts", status_code=201)
def add_account(cid: int, body: AccountBody, request: Request, db: Session = Depends(get_db),
                current_user: dict = Depends(require_permission("influencer", "edit"))):
    c = db.query(InfluencerCreator).filter(InfluencerCreator.id == cid).first()
    if not c:
        return _err("Creator bulunamadı.", 404)
    platform = body.platform.strip().lower()
    handle = body.handle.strip().lstrip("@").lower()
    if platform not in PLATFORMS:
        return _err("Platform instagram / tiktok / youtube olmalı.")
    if db.query(InfluencerAccount.id).filter(InfluencerAccount.platform == platform,
                                             InfluencerAccount.handle == handle).first():
        return _err("Bu hesap zaten kayıtlı.", 409)
    has_any = db.query(InfluencerAccount.id).filter(InfluencerAccount.creator_id == cid).first()
    a = InfluencerAccount(creator_id=cid, platform=platform, handle=handle, external_id=body.external_id,
                          followers=body.followers, following=body.following, posts_count=body.posts_count,
                          hidden_subscriber_count=bool(body.hidden_subscriber_count),
                          is_primary=bool(body.is_primary) or not has_any)
    db.add(a)
    db.flush()
    if a.is_primary:
        db.query(InfluencerAccount).filter(InfluencerAccount.creator_id == cid,
                                           InfluencerAccount.id != a.id).update({"is_primary": False})
    c.tier_key = _tier_for_creator(db, c)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.account.create",
                    target_type="influencer_account", target_id=a.id, target_name=f"{platform}:@{handle}")
    return {"ok": True, "id": a.id, "tier_key": c.tier_key}


@router.put("/creators/{cid}/accounts/{aid}")
def update_account(cid: int, aid: int, body: AccountBody, request: Request, db: Session = Depends(get_db),
                   current_user: dict = Depends(require_permission("influencer", "edit"))):
    a = db.query(InfluencerAccount).filter(InfluencerAccount.id == aid, InfluencerAccount.creator_id == cid).first()
    if not a:
        return _err("Hesap bulunamadı.", 404)
    platform = body.platform.strip().lower()
    handle = body.handle.strip().lstrip("@").lower()
    if platform not in PLATFORMS:
        return _err("Platform instagram / tiktok / youtube olmalı.")
    dup = db.query(InfluencerAccount.id).filter(InfluencerAccount.platform == platform,
                                                InfluencerAccount.handle == handle,
                                                InfluencerAccount.id != aid).first()
    if dup:
        return _err("Bu hesap başka bir creator'da kayıtlı.", 409)
    a.platform, a.handle, a.external_id = platform, handle, body.external_id
    a.followers, a.following, a.posts_count = body.followers, body.following, body.posts_count
    if body.hidden_subscriber_count is not None:
        a.hidden_subscriber_count = bool(body.hidden_subscriber_count)
    if body.is_primary:
        db.query(InfluencerAccount).filter(InfluencerAccount.creator_id == cid,
                                           InfluencerAccount.id != aid).update({"is_primary": False})
        a.is_primary = True
    a.updated_at = datetime.utcnow()
    c = a.creator
    c.tier_key = _tier_for_creator(db, c)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.account.update",
                    target_type="influencer_account", target_id=a.id, target_name=f"{platform}:@{handle}")
    return {"ok": True, "id": a.id, "tier_key": c.tier_key}


@router.delete("/creators/{cid}/accounts/{aid}")
def delete_account(cid: int, aid: int, request: Request, db: Session = Depends(get_db),
                   current_user: dict = Depends(require_permission("influencer", "edit"))):
    a = db.query(InfluencerAccount).filter(InfluencerAccount.id == aid, InfluencerAccount.creator_id == cid).first()
    if not a:
        return _err("Hesap bulunamadı.", 404)
    name = f"{a.platform}:@{a.handle}"
    db.delete(a)
    db.flush()
    c = db.query(InfluencerCreator).filter(InfluencerCreator.id == cid).first()
    if c:
        c.tier_key = _tier_for_creator(db, c)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.account.delete",
                    target_type="influencer_account", target_id=aid, target_name=name)
    return {"ok": True, "id": aid}


class PostIn(BaseModel):
    likes: Optional[float] = Field(None, ge=0)
    comments: Optional[float] = Field(None, ge=0)
    views: Optional[float] = Field(None, ge=0)
    saves: Optional[float] = Field(None, ge=0)
    shares: Optional[float] = Field(None, ge=0)


class MetricsIn(BaseModel):
    followers: Optional[int] = Field(None, ge=0)
    following: Optional[int] = Field(None, ge=0)
    posts_count: Optional[int] = Field(None, ge=0)
    posts: List[PostIn] = Field(default_factory=list, max_length=60)
    geo_target_pct: Optional[float] = Field(None, ge=0, le=100)
    comment_quality_pct: Optional[float] = Field(None, ge=0, le=100)
    fake_pct: Optional[float] = Field(None, ge=0, le=100)
    hidden_subs: Optional[bool] = None
    source: Optional[str] = Field("manual", max_length=20)


def _score_creator(db: Session, c: InfluencerCreator, a: InfluencerAccount, stats: dict,
                   geo_target_pct=None, comment_quality_pct=None, fake_pct=None,
                   hidden_subs: bool = False) -> dict:
    """Motoru çağır, creator skor alanlarını yaz.  Büyüme serisi snapshot'lardan."""
    tier = c.tier_override_key or ENG.tier_for(a.followers, _tier_rows(db) or None) or c.tier_key or "nano"
    if tier in ENG.MANUAL_TIER_KEYS:
        tier = ENG.tier_for(a.followers, _tier_rows(db) or None) or "makro"
    bm = ENG.benchmark_map(_benchmark_rows(db) or ENG.DEFAULT_BENCHMARKS)
    series = [(s.taken_at, s.followers) for s in
              db.query(InfluencerMetricSnapshot).filter(InfluencerMetricSnapshot.account_id == a.id,
                                                        InfluencerMetricSnapshot.followers.isnot(None))
              .order_by(InfluencerMetricSnapshot.taken_at).all()]
    geo = geo_target_pct
    if geo is None:
        geo = (_jload(a.audience_geo_json, {}) or {}).get("target_pct")
    fp = fake_pct if fake_pct is not None else c.fake_pct
    res = ENG.authenticity_score(stats, bm, a.platform, tier, geo_target_pct=geo,
                                 growth_series=series if len(series) >= 2 else None,
                                 comment_quality_pct=comment_quality_pct, fake_pct=fp,
                                 hidden_subs=bool(hidden_subs or a.hidden_subscriber_count))
    res["tier_key"] = tier
    c.authenticity_score = float(res["score"])
    c.score_json = _jdump({"score": res["score"], "components": res["components"], "flags": res["flags"],
                           "platform": a.platform, "tier_key": tier, "account_id": a.id,
                           "at": datetime.utcnow().isoformat()})
    c.updated_at = datetime.utcnow()
    return res


@router.post("/creators/{cid}/accounts/{aid}/metrics")
def enter_metrics(cid: int, aid: int, body: MetricsIn, request: Request, db: Session = Depends(get_db),
                  current_user: dict = Depends(require_permission("influencer", "edit"))):
    """Son N gönderi elle → post_stats → hesap özetleri + snapshot + özgünlük skoru."""
    a = db.query(InfluencerAccount).filter(InfluencerAccount.id == aid, InfluencerAccount.creator_id == cid).first()
    if not a:
        return _err("Hesap bulunamadı.", 404)
    c = a.creator
    if body.followers is not None:
        a.followers = body.followers
    if body.following is not None:
        a.following = body.following
    if body.posts_count is not None:
        a.posts_count = body.posts_count
    if body.hidden_subs is not None:
        a.hidden_subscriber_count = bool(body.hidden_subs)
    posts = [p.model_dump() for p in body.posts]
    stats = ENG.post_stats(posts, a.followers)
    a.avg_likes, a.avg_comments, a.avg_views = stats["avg_likes"], stats["avg_comments"], stats["avg_views"]
    a.er_follower, a.er_view = stats["er_follower"], stats["er_view"]
    a.view_per_follower, a.views_cv = stats["view_per_follower"], stats["views_cv"]
    if body.geo_target_pct is not None:
        geo = _jload(a.audience_geo_json, {}) or {}
        geo["target_pct"] = body.geo_target_pct
        a.audience_geo_json = _jdump(geo)
    a.metrics_source = (body.source or "manual")[:20]
    a.metrics_at = datetime.utcnow()
    a.updated_at = a.metrics_at
    if body.fake_pct is not None:
        c.fake_pct = body.fake_pct
    snap = InfluencerMetricSnapshot(account_id=a.id, source=a.metrics_source, followers=a.followers,
                                    posts_json=_jdump(posts), entered_by=_actor(current_user))
    db.add(snap)
    db.flush()
    res = _score_creator(db, c, a, stats, geo_target_pct=body.geo_target_pct,
                         comment_quality_pct=body.comment_quality_pct, fake_pct=body.fake_pct,
                         hidden_subs=bool(body.hidden_subs))
    snap.computed_json = _jdump({"stats": stats, "score": res["score"], "components": res["components"],
                                 "flags": res["flags"]})
    c.tier_key = _tier_for_creator(db, c)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.account.metrics",
                    target_type="influencer_account", target_id=a.id,
                    target_name=f"{a.platform}:@{a.handle}", details={"score": res["score"]})
    return {"ok": True, "id": a.id, "snapshot_id": snap.id, "stats": stats, "score": res["score"],
            "components": res["components"], "flags": res["flags"], "tier_key": c.tier_override_key or c.tier_key}


@router.post("/creators/{cid}/score")
def rescore_creator(cid: int, request: Request, db: Session = Depends(get_db),
                    current_user: dict = Depends(require_permission("influencer", "edit"))):
    """Son snapshot'tan skoru yeniden hesapla (benchmark/kademe değişince)."""
    c = db.query(InfluencerCreator).filter(InfluencerCreator.id == cid).first()
    if not c:
        return _err("Creator bulunamadı.", 404)
    a = (db.query(InfluencerAccount).filter(InfluencerAccount.creator_id == cid)
         .order_by(InfluencerAccount.is_primary.desc(), InfluencerAccount.metrics_at.desc().nullslast()).first())
    if not a:
        return _err("Skor için önce bir hesap ekleyin.")
    snap = (db.query(InfluencerMetricSnapshot).filter(InfluencerMetricSnapshot.account_id == a.id)
            .order_by(InfluencerMetricSnapshot.taken_at.desc()).first())
    stats = ENG.post_stats(_jload(snap.posts_json, []) if snap else [], a.followers)
    res = _score_creator(db, c, a, stats)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.creator.score",
                    target_type="influencer_creator", target_id=c.id, target_name=c.full_name,
                    details={"score": res["score"]})
    return {"ok": True, "id": c.id, "score": res["score"], "components": res["components"],
            "flags": res["flags"], "tier_key": res["tier_key"]}


class TokenIn(BaseModel):
    purpose: str = Field(..., max_length=20)        # address | insights | agreement
    collab_id: Optional[int] = None
    days: Optional[int] = Field(TOKEN_DEFAULT_DAYS, ge=1, le=90)


@router.post("/creators/{cid}/tokens", status_code=201)
def create_token(cid: int, body: TokenIn, request: Request, db: Session = Depends(get_db),
                 current_user: dict = Depends(require_permission("influencer", "edit"))):
    """Tek kullanımlık claim linki (adres / insights / kılavuz onayı)."""
    c = db.query(InfluencerCreator).filter(InfluencerCreator.id == cid).first()
    if not c:
        return _err("Creator bulunamadı.", 404)
    if body.purpose not in TOKEN_PURPOSES:
        return _err("purpose address / insights / agreement olmalı.")
    collab = None
    if body.collab_id:
        collab = db.query(InfluencerCollab).filter(InfluencerCollab.id == body.collab_id,
                                                   InfluencerCollab.creator_id == cid).first()
        if not collab:
            return _err("İş birliği bulunamadı.", 404)
    if body.purpose == "agreement" and not collab:
        return _err("Kılavuz onayı için collab_id zorunludur.")
    t = InfluencerToken(token=secrets.token_urlsafe(32), purpose=body.purpose, creator_id=cid,
                        collab_id=collab.id if collab else None,
                        expires_at=datetime.utcnow() + timedelta(days=body.days or TOKEN_DEFAULT_DAYS),
                        created_by=_actor(current_user))
    db.add(t)
    db.commit()
    db.refresh(t)
    log_admin_event(db, request, actor=current_user, action="influencer.token.create",
                    target_type="influencer_token", target_id=t.id, target_name=c.full_name,
                    details={"purpose": t.purpose, "collab_id": t.collab_id})
    out = _token_out(t)
    out.update({"ok": True, "id": t.id})
    return out


class TagsIn(BaseModel):
    tags: List[str] = Field(default_factory=list, max_length=30)


@router.post("/creators/{cid}/tags")
def set_tags(cid: int, body: TagsIn, request: Request, db: Session = Depends(get_db),
             current_user: dict = Depends(require_permission("influencer", "edit"))):
    """Etiket setini DEĞİŞTİRİR (CrmEntityTag entity='influencer')."""
    c = db.query(InfluencerCreator).filter(InfluencerCreator.id == cid).first()
    if not c:
        return _err("Creator bulunamadı.", 404)
    names = []
    for n in body.tags:
        n = (n or "").strip()[:60]
        if n and n.lower() not in [x.lower() for x in names]:
            names.append(n)
    db.query(CrmEntityTag).filter(CrmEntityTag.entity == "influencer", CrmEntityTag.entity_id == cid).delete()
    for n in names:
        tag = db.query(CrmTag).filter(func.lower(CrmTag.name) == n.lower()).first()
        if not tag:
            tag = CrmTag(name=n)
            db.add(tag)
            db.flush()
        db.add(CrmEntityTag(entity="influencer", entity_id=cid, tag_id=tag.id))
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.creator.tags",
                    target_type="influencer_creator", target_id=cid, target_name=c.full_name,
                    details={"tags": names})
    return {"ok": True, "id": cid, "tags": _tag_names(db, cid)}


class ActivityIn(BaseModel):
    type: str = Field("note", max_length=20)
    subject: Optional[str] = Field(None, max_length=200)
    body: Optional[str] = Field(None, max_length=5000)
    collab_id: Optional[int] = None


@router.post("/creators/{cid}/activities", status_code=201)
def add_activity(cid: int, body: ActivityIn, db: Session = Depends(get_db),
                 current_user: dict = Depends(require_permission("influencer", "edit"))):
    c = db.query(InfluencerCreator).filter(InfluencerCreator.id == cid).first()
    if not c:
        return _err("Creator bulunamadı.", 404)
    if not (body.subject or body.body):
        return _err("Not boş olamaz.")
    a = InfluencerActivity(creator_id=cid, collab_id=body.collab_id, type=(body.type or "note")[:20],
                           subject=body.subject, body=body.body, author_user_id=_uid(current_user),
                           author_name=_actor(current_user))
    db.add(a)
    db.commit()
    return {"ok": True, "id": a.id}


# ═══════════════════════════════════════════════════════════════════════════
#  KAMPANYALAR (basit CRUD — Kanban'da gruplama)
# ═══════════════════════════════════════════════════════════════════════════

class CampaignIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=150)
    store_key: str = Field("minerva", max_length=20)
    market: Optional[str] = Field("TR", max_length=10)
    start_on: Optional[str] = None
    end_on: Optional[str] = None
    hashtags: Optional[str] = Field(None, max_length=300)
    brief_text: Optional[str] = Field(None, max_length=10000)
    claim_sheet: Optional[List[str]] = None
    default_deliverables: Optional[List[dict]] = None
    status: Optional[str] = Field("taslak", max_length=20)


@router.get("/campaigns")
def list_campaigns(db: Session = Depends(get_db),
                   _: dict = Depends(require_permission("influencer", "view"))):
    rows = db.query(InfluencerCampaign).filter(InfluencerCampaign.is_active == True).order_by(  # noqa: E712
        InfluencerCampaign.id.desc()).all()
    return {"items": [_campaign_out(c) for c in rows], "count": len(rows)}


@router.post("/campaigns", status_code=201)
def create_campaign(body: CampaignIn, request: Request, db: Session = Depends(get_db),
                    current_user: dict = Depends(require_permission("influencer", "edit"))):
    if body.store_key not in STORE_KEYS:
        return _err("Geçersiz mağaza.")
    c = InfluencerCampaign(name=body.name.strip(), store_key=body.store_key, market=(body.market or "TR").upper(),
                           start_on=_parse_date(body.start_on), end_on=_parse_date(body.end_on),
                           hashtags=body.hashtags, brief_text=body.brief_text,
                           claim_sheet_json=_jdump(body.claim_sheet or []),
                           default_deliverables_json=_jdump(body.default_deliverables or []),
                           status=body.status or "taslak", created_by=_actor(current_user))
    db.add(c)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.campaign.create",
                    target_type="influencer_campaign", target_id=c.id, target_name=c.name)
    return {"ok": True, "id": c.id}


@router.put("/campaigns/{cid}")
def update_campaign(cid: int, body: CampaignIn, request: Request, db: Session = Depends(get_db),
                    current_user: dict = Depends(require_permission("influencer", "edit"))):
    c = db.query(InfluencerCampaign).filter(InfluencerCampaign.id == cid).first()
    if not c:
        return _err("Kampanya bulunamadı.", 404)
    c.name, c.store_key, c.market = body.name.strip(), body.store_key, (body.market or "TR").upper()
    c.start_on, c.end_on = _parse_date(body.start_on), _parse_date(body.end_on)
    c.hashtags, c.brief_text = body.hashtags, body.brief_text
    if body.claim_sheet is not None:
        c.claim_sheet_json = _jdump(body.claim_sheet)
    if body.default_deliverables is not None:
        c.default_deliverables_json = _jdump(body.default_deliverables)
    c.status = body.status or c.status
    c.updated_at = datetime.utcnow()
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.campaign.update",
                    target_type="influencer_campaign", target_id=c.id, target_name=c.name)
    return {"ok": True, "id": c.id}


# ═══════════════════════════════════════════════════════════════════════════
#  İŞ BİRLİKLERİ — /collabs/board literal ÖNCE
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/collabs/board")
def collab_board(store_key: Optional[str] = None, campaign_id: Optional[int] = None,
                 db: Session = Depends(get_db),
                 _: dict = Depends(require_permission("influencer", "view"))):
    """Kanban: her aşama bir kolon (terminaller dahil, iptal en sonda)."""
    qry = db.query(InfluencerCollab).filter(InfluencerCollab.is_active == True)  # noqa: E712
    if store_key:
        qry = qry.filter(InfluencerCollab.store_key == store_key)
    if campaign_id:
        qry = qry.filter(InfluencerCollab.campaign_id == campaign_id)
    rows = qry.order_by(InfluencerCollab.stage_changed_at.desc().nullslast()).limit(2000).all()
    cols = {s: [] for s in ENG.STAGES}
    for c in rows:
        cols.setdefault(c.stage, []).append(_collab_out(c, db))
    columns = [{"stage": s, "label": ENG.STAGE_LABELS.get(s, s), "terminal": s in ENG.TERMINAL_STAGES,
                "items": cols.get(s, []), "count": len(cols.get(s, []))} for s in ENG.STAGES]
    return {"columns": columns, "count": len(rows),
            "transitions": {k: sorted(v, key=ENG.STAGES.index) for k, v in ENG.TRANSITIONS.items()}}


class CollabIn(BaseModel):
    creator_id: int
    store_key: str = Field("minerva", max_length=20)
    market: Optional[str] = Field("TR", max_length=10)
    model: Optional[str] = Field("barter", max_length=10)
    campaign_id: Optional[int] = None
    content_due_on: Optional[str] = None
    usage_rights: Optional[str] = Field(None, max_length=60)
    exclusivity_days: Optional[int] = Field(0, ge=0, le=365)
    deliverables: Optional[List[dict]] = None
    owner_user_id: Optional[int] = None
    notes: Optional[str] = Field(None, max_length=5000)
    stage: Optional[str] = Field(None, max_length=30)   # yalnız create: başlangıç aşaması (varsayılan teklif_gonderildi)


@router.get("/collabs")
def list_collabs(creator_id: Optional[int] = None, stage: Optional[str] = None,
                 store_key: Optional[str] = None, active: int = 1, db: Session = Depends(get_db),
                 _: dict = Depends(require_permission("influencer", "view"))):
    qry = db.query(InfluencerCollab)
    if active:
        qry = qry.filter(InfluencerCollab.is_active == True)  # noqa: E712
    if creator_id:
        qry = qry.filter(InfluencerCollab.creator_id == creator_id)
    if stage:
        qry = qry.filter(InfluencerCollab.stage == stage)
    if store_key:
        qry = qry.filter(InfluencerCollab.store_key == store_key)
    rows = qry.order_by(InfluencerCollab.id.desc()).limit(1000).all()
    return {"items": [_collab_out(c, db) for c in rows], "count": len(rows)}


@router.post("/collabs", status_code=201)
def create_collab(body: CollabIn, request: Request, db: Session = Depends(get_db),
                  current_user: dict = Depends(require_permission("influencer", "edit"))):
    cr = db.query(InfluencerCreator).filter(InfluencerCreator.id == body.creator_id).first()
    if not cr:
        return _err("Creator bulunamadı.", 404)
    if body.store_key not in STORE_KEYS:
        return _err("Geçersiz mağaza.")
    if (body.model or "barter") not in MODELS:
        return _err("Model barter / kod / karma / link olmalı.")
    if body.campaign_id and not db.query(InfluencerCampaign.id).filter(InfluencerCampaign.id == body.campaign_id).first():
        return _err("Kampanya bulunamadı.", 404)
    stage = body.stage or "teklif_gonderildi"
    if stage not in ENG.STAGES or stage in ENG.TERMINAL_STAGES:
        return _err("Geçersiz başlangıç aşaması.")
    owner_id = body.owner_user_id or cr.owner_user_id or _uid(current_user)
    owner = db.query(User).filter(User.id == owner_id).first() if owner_id else None
    now = datetime.utcnow()
    c = InfluencerCollab(
        code=_next_code(db), creator_id=cr.id, campaign_id=body.campaign_id, store_key=body.store_key,
        market=(body.market or "TR").upper(), model=body.model or "barter",
        tier_key_at_start=cr.tier_override_key or cr.tier_key, stage=stage, stage_changed_at=now,
        offered_at=now, accepted_at=(now if stage != "teklif_gonderildi" else None),
        content_due_on=_parse_date(body.content_due_on), usage_rights=body.usage_rights,
        exclusivity_days=body.exclusivity_days or 0, deliverables_json=_jdump(body.deliverables or []),
        owner_user_id=owner.id if owner else None, owner_name=owner.full_name if owner else None,
        notes=body.notes, created_by=_actor(current_user),
    )
    db.add(c)
    db.flush()
    db.add(InfluencerCollabStageLog(collab_id=c.id, from_stage=None, to_stage=stage, actor=_actor(current_user)))
    _activity(db, current_user, creator_id=cr.id, collab_id=c.id, subject="İş birliği açıldı", body=c.code)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.collab.create",
                    target_type="influencer_collab", target_id=c.id, target_name=c.code,
                    details={"creator_id": cr.id, "store_key": c.store_key})
    return {"ok": True, "id": c.id, "code": c.code, "stage": c.stage}


@router.get("/collabs/{cid}")
def get_collab(cid: int, db: Session = Depends(get_db),
               _: dict = Depends(require_permission("influencer", "view"))):
    c = db.query(InfluencerCollab).filter(InfluencerCollab.id == cid).first()
    if not c:
        return _err("İş birliği bulunamadı.", 404)
    return _collab_out(c, db, full=True)


@router.put("/collabs/{cid}")
def update_collab(cid: int, body: CollabIn, request: Request, db: Session = Depends(get_db),
                  current_user: dict = Depends(require_permission("influencer", "edit"))):
    c = db.query(InfluencerCollab).filter(InfluencerCollab.id == cid).first()
    if not c:
        return _err("İş birliği bulunamadı.", 404)
    if body.store_key not in STORE_KEYS:
        return _err("Geçersiz mağaza.")
    if (body.model or "barter") not in MODELS:
        return _err("Model barter / kod / karma / link olmalı.")
    c.store_key, c.market, c.model = body.store_key, (body.market or "TR").upper(), body.model or "barter"
    c.campaign_id = body.campaign_id
    c.content_due_on = _parse_date(body.content_due_on)
    c.usage_rights, c.exclusivity_days = body.usage_rights, body.exclusivity_days or 0
    if body.deliverables is not None:
        c.deliverables_json = _jdump(body.deliverables)
    if body.owner_user_id is not None:
        owner = db.query(User).filter(User.id == body.owner_user_id).first() if body.owner_user_id else None
        c.owner_user_id = owner.id if owner else None
        c.owner_name = owner.full_name if owner else None
    c.notes = body.notes
    c.updated_at = datetime.utcnow()
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.collab.update",
                    target_type="influencer_collab", target_id=c.id, target_name=c.code)
    return {"ok": True, "id": c.id}


@router.post("/collabs/{cid}/stage")
def set_collab_stage(cid: int, body: StageIn, request: Request, db: Session = Depends(get_db),
                     current_user: dict = Depends(require_permission("influencer", "edit"))):
    """TRANSITIONS doğrulamalı; iptalde `reason` zorunlu; stage_log yazılır."""
    c = db.query(InfluencerCollab).filter(InfluencerCollab.id == cid).first()
    if not c:
        return _err("İş birliği bulunamadı.", 404)
    if body.stage not in ENG.STAGES:
        return _err("Bilinmeyen aşama.")
    old = c.stage
    _move_stage(db, c, body.stage, _actor(current_user), body.reason, strict=True)
    if body.stage == "iptal" and c.creator and c.creator.relationship_stage == "basvurdu":
        pass   # ilişki aşaması creator ekranından yönetilir
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.collab.stage",
                    target_type="influencer_collab", target_id=c.id, target_name=c.code,
                    details={"from": old, "to": c.stage, "reason": body.reason})
    return {"ok": True, "id": c.id, "stage": c.stage, "stage_label": ENG.STAGE_LABELS.get(c.stage, c.stage)}


class DecisionIn(BaseModel):
    decision: str = Field(..., max_length=20)     # birak | tekrarla | yukselt
    note: Optional[str] = Field(None, max_length=2000)


@router.post("/collabs/{cid}/decision")
def set_collab_decision(cid: int, body: DecisionIn, request: Request, db: Session = Depends(get_db),
                        current_user: dict = Depends(require_permission("influencer", "approve"))):
    c = db.query(InfluencerCollab).filter(InfluencerCollab.id == cid).first()
    if not c:
        return _err("İş birliği bulunamadı.", 404)
    if body.decision not in ENG.DECISION_LABELS:
        return _err("Karar birak / tekrarla / yukselt olmalı.")
    c.decision = body.decision
    c.updated_at = datetime.utcnow()
    cr = c.creator
    if body.decision == "birak" and cr:
        cr.do_not_resend = True
    elif body.decision == "yukselt" and cr and cr.relationship_stage in ("havuz", "basvurdu", "seeded"):
        cr.relationship_stage = "affiliate"
    _activity(db, current_user, creator_id=c.creator_id, collab_id=c.id, subject="Karar",
              body=f"{ENG.DECISION_LABELS[body.decision]}" + (f" — {body.note}" if body.note else ""))
    if c.stage == "metrik_toplandi":
        _move_stage(db, c, "tamamlandi", _actor(current_user), strict=False)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.collab.decision",
                    target_type="influencer_collab", target_id=c.id, target_name=c.code,
                    details={"decision": body.decision, "note": body.note})
    return {"ok": True, "id": c.id, "decision": c.decision, "stage": c.stage}


# ═══════════════════════════════════════════════════════════════════════════
#  GÖNDERİM — stok yolu Delivery (kargo); burada stok hareketi YOK
# ═══════════════════════════════════════════════════════════════════════════

class ShipmentLine(BaseModel):
    item_id: int
    quantity: float = Field(..., gt=0)


class ShipmentIn(BaseModel):
    items: List[ShipmentLine]
    address_id: Optional[int] = None
    recipient_name: Optional[str] = Field(None, max_length=150)
    recipient_phone: Optional[str] = Field(None, max_length=40)
    handwritten_note: Optional[str] = Field(None, max_length=2000)
    note: Optional[str] = Field(None, max_length=2000)
    doc_lang: Optional[str] = Field(None, max_length=8)
    packaging_cost: Optional[float] = Field(None, ge=0)
    shipping_cost: Optional[float] = Field(None, ge=0)


@router.get("/shipments")
def list_shipments(collab_id: Optional[int] = None, status: Optional[str] = None,
                   db: Session = Depends(get_db),
                   _: dict = Depends(require_permission("influencer", "view"))):
    """Gönderim kuyruğu.  status: preparing | shipped | delivered (teslim edildi)."""
    qry = db.query(InfluencerShipment)
    if collab_id:
        qry = qry.filter(InfluencerShipment.collab_id == collab_id)
    rows = qry.order_by(InfluencerShipment.id.desc()).limit(1000).all()
    items = [_shipment_out(s, db) for s in rows]
    if status == "delivered":
        items = [x for x in items if x["delivered_at"]]
    elif status:
        items = [x for x in items if x["delivery_status"] == status and not x["delivered_at"]]
    return {"items": items, "count": len(items)}


@router.post("/collabs/{cid}/shipments", status_code=201)
def create_shipment(cid: int, body: ShipmentIn, request: Request, db: Session = Depends(get_db),
                    current_user: dict = Depends(require_permission("influencer", "ship")),
                    domain: str = Depends(active_domain)):
    """Delivery(kargo, status=preparing) + InfluencerShipment (COGS snapshot).
    Stok BU ADIMDA DÜŞMEZ — kargolamada (`/shipments/{sid}/ship`) düşer."""
    c = db.query(InfluencerCollab).filter(InfluencerCollab.id == cid).first()
    if not c:
        return _err("İş birliği bulunamadı.", 404)
    cr = c.creator
    if cr.do_not_resend:
        return _err("Bu creator 'tekrar gönderme' işaretli — gönderim engellendi.")
    if c.stage in ENG.TERMINAL_STAGES:
        return _err("Kapanmış iş birliğine gönderim yapılamaz.")
    if not body.items:
        return _err("En az bir ürün seçin.")
    addr = None
    if body.address_id:
        addr = db.query(InfluencerAddress).filter(InfluencerAddress.id == body.address_id,
                                                  InfluencerAddress.creator_id == cr.id).first()
        if not addr:
            return _err("Adres bulunamadı.", 404)
    else:
        addr = (db.query(InfluencerAddress).filter(InfluencerAddress.creator_id == cr.id)
                .order_by(InfluencerAddress.is_default.desc(), InfluencerAddress.id.desc()).first())
    rname = (body.recipient_name or (addr.recipient_name if addr else None) or cr.full_name).strip()
    rphone = (body.recipient_phone or (addr.phone if addr else None) or cr.phone or "").strip() or None
    doc_lang = (body.doc_lang or ("TR" if (c.market or "TR").upper() == "TR" else "EN")).upper()
    addr_txt = ""
    if addr:
        addr_txt = " ".join(x for x in [addr.line1, addr.line2, addr.district, addr.city,
                                        addr.postal_code, addr.country] if x)
    note_parts = [f"Influencer gönderimi {c.code}", ENG_SAMPLE_NOTE(doc_lang)]
    if addr_txt:
        note_parts.append("Adres: " + addr_txt)
    if body.note:
        note_parts.append(body.note.strip())
    data = DeliveryCreate(
        recipient_name=rname, recipient_org=None, recipient_phone=rphone, delivery_type="kargo",
        method="kargo", note=" | ".join(note_parts)[:2000], doc_lang=doc_lang,
        items=[DeliveryLine(item_id=ln.item_id, quantity=ln.quantity) for ln in body.items],
    )
    try:
        d = build_delivery(db, data, _actor(current_user), domain)
    except DeliveryError as e:
        db.rollback()
        return _err(e.detail, e.status_code)
    # COGS snapshot — Σ adet × Item.cost_price (cost_price sıfırsa uyarı döner)
    cogs, zero_cost = 0.0, []
    for li in d.items:
        it = db.query(Item).filter(Item.id == li.item_id).first()
        price = float((it.cost_price if it else 0) or 0)
        if price <= 0:
            zero_cost.append(li.item_name)
        cogs += price * float(li.quantity)
    cfg = _cfg(db)
    pack = body.packaging_cost if body.packaging_cost is not None else _float_setting(cfg, ENG.CFG_PACK_COST_TRY)
    ship = body.shipping_cost if body.shipping_cost is not None else _float_setting(cfg, ENG.CFG_SHIP_COST_TRY)
    s = InfluencerShipment(collab_id=c.id, delivery_id=d.id, address_id=addr.id if addr else None,
                           cogs_total=round(cogs, 2), packaging_cost=float(pack), shipping_cost=float(ship),
                           loaded_cost=ENG.loaded_gift_cost(cogs, pack, ship),
                           handwritten_note=body.handwritten_note, created_by=_actor(current_user))
    db.add(s)
    db.flush()
    _move_stage(db, c, "urun_hazirlaniyor", _actor(current_user), strict=False)
    _activity(db, current_user, creator_id=cr.id, collab_id=c.id, subject="Gönderim hazırlandı",
              body=f"{d.document_no} · yüklü maliyet {s.loaded_cost:.2f}")
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.shipment.create",
                    target_type="influencer_shipment", target_id=s.id, target_name=d.document_no,
                    details={"collab_id": c.id, "delivery_id": d.id, "cogs_total": s.cogs_total,
                             "loaded_cost": s.loaded_cost})
    out = {"ok": True, "id": s.id, "delivery_id": d.id, "document_no": d.document_no,
           "cogs_total": s.cogs_total, "loaded_cost": s.loaded_cost, "stage": c.stage,
           "warnings": ([f"Maliyet fiyatı sıfır: {', '.join(zero_cost)}"] if zero_cost else [])}
    return out


def ENG_SAMPLE_NOTE(doc_lang: str) -> str:
    from core.delivery_note import SAMPLE_NOTICE_EN, SAMPLE_NOTICE_TR
    return SAMPLE_NOTICE_EN if (doc_lang or "TR").upper() == "EN" else SAMPLE_NOTICE_TR


class ShipIn(BaseModel):
    tracking_no: str = Field(..., min_length=1, max_length=100)
    carrier: Optional[str] = Field(None, max_length=80)


@router.post("/shipments/{sid}/ship")
def ship_shipment(sid: int, body: ShipIn, request: Request, db: Session = Depends(get_db),
                  current_user: dict = Depends(require_permission("influencer", "ship"))):
    """Takip no → `ship_core` (stok BİR KEZ düşer, Output Transaction) → aşama kargoda.
    İkinci çağrı ship_core'dan 400 döner (Delivery satırı kilitli, status kontrolü)."""
    s = db.query(InfluencerShipment).filter(InfluencerShipment.id == sid).first()
    if not s or not s.delivery_id:
        return _err("Gönderim bulunamadı.", 404)
    d = db.query(Delivery).filter(Delivery.id == s.delivery_id).with_for_update().first()
    if not d:
        return _err("Teslimat kaydı bulunamadı.", 404)
    try:
        out = ship_core(db, d, body.tracking_no, body.carrier, _actor(current_user))
    except DeliveryError as e:
        db.rollback()
        return _err(e.detail, e.status_code)
    s.shipped_at = d.shipped_at or datetime.utcnow()
    c = s.collab
    _move_stage(db, c, "kargoda", _actor(current_user), strict=False)
    _activity(db, current_user, creator_id=c.creator_id, collab_id=c.id, subject="Kargolandı",
              body=f"{d.document_no} · {body.carrier or ''} {body.tracking_no}".strip())
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.shipment.ship",
                    target_type="influencer_shipment", target_id=s.id, target_name=d.document_no,
                    details={"tracking_no": out["tracking_no"], "carrier": out["carrier"]})
    out.update({"ok": True, "id": s.id, "stage": c.stage})
    return out


class DeliveredIn(BaseModel):
    delivered_on: Optional[str] = None      # 'YYYY-MM-DD' (varsayılan bugün)
    assigned_to_user_id: Optional[int] = None


@router.post("/shipments/{sid}/delivered")
def mark_delivered(sid: int, body: DeliveredIn, request: Request, db: Session = Depends(get_db),
                   current_user: dict = Depends(require_permission("influencer", "ship"))):
    """Elle 'ulaştı' → delivered_at + aşama icerik_bekleniyor + hatırlatma görevleri
    (CrmTask.influencer_collab_id; reminder_days ayarı, varsayılan 7,14)."""
    s = db.query(InfluencerShipment).filter(InfluencerShipment.id == sid).first()
    if not s:
        return _err("Gönderim bulunamadı.", 404)
    if s.delivered_at:
        return _err("Bu gönderim zaten teslim edildi olarak işaretli.")
    d = s.delivery
    if d and d.delivery_type == "kargo" and d.status not in ("shipped",):
        return _err("Önce kargolayın (takip no girin).")
    on = _parse_date(body.delivered_on) or datetime.utcnow().date()
    s.delivered_at = datetime.combine(on, datetime.min.time()) if body.delivered_on else datetime.utcnow()
    c = s.collab
    cr = c.creator
    actor = _actor(current_user)
    # kargoda → teslim_edildi → icerik_bekleniyor (elden teslim: urun_hazirlaniyor → teslim_edildi)
    _move_stage(db, c, "teslim_edildi", actor, strict=False)
    _move_stage(db, c, "icerik_bekleniyor", actor, strict=False)
    if cr.relationship_stage in ("havuz", "basvurdu"):
        cr.relationship_stage = "seeded"
    cfg = _cfg(db)
    days = ENG.parse_int_list(cfg.get(ENG.CFG_REMINDER_DAYS), default=[7, 14])
    dates = ENG.reminder_dates(on, days)
    assignee = body.assigned_to_user_id or c.owner_user_id or _uid(current_user)
    assignee_name = None
    if assignee:
        u = db.query(User).filter(User.id == assignee).first()
        assignee_name = u.full_name if u else None
    task_ids = []
    for i, dt in enumerate(dates[:2], start=1):
        t = CrmTask(title=f"İçerik hatırlatması {i} — {cr.full_name} ({c.code})",
                    notes=f"Ürün {on.strftime('%d.%m.%Y')} tarihinde ulaştı; içerik bekleniyor.",
                    due_at=datetime.combine(dt, datetime.min.time()) + timedelta(hours=6),   # 09:00 TR
                    assigned_to_user_id=assignee, assigned_to_name=assignee_name,
                    created_by=actor, influencer_collab_id=c.id)
        db.add(t)
        db.flush()
        task_ids.append(t.id)
    if dates:
        s.reminder1_due = dates[0]
        s.reminder1_task_id = task_ids[0] if task_ids else None
    if len(dates) > 1:
        s.reminder2_due = dates[1]
        s.reminder2_task_id = task_ids[1] if len(task_ids) > 1 else None
    _activity(db, current_user, creator_id=cr.id, collab_id=c.id, subject="Teslim edildi",
              body=f"{d.document_no if d else ''} · {on.strftime('%d.%m.%Y')}")
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.shipment.delivered",
                    target_type="influencer_shipment", target_id=s.id,
                    target_name=d.document_no if d else str(s.id),
                    details={"delivered_on": on.isoformat(), "task_ids": task_ids})
    return {"ok": True, "id": s.id, "stage": c.stage, "task_ids": task_ids,
            "reminder_dates": [x.isoformat() for x in dates]}


@router.get("/shipments/{sid}/document")
def shipment_document(sid: int, db: Session = Depends(get_db),
                      _: dict = Depends(require_permission("influencer", "view"))):
    """Şerhli teslim belgesi (NUMUNEDİR — PARAYLA SATILAMAZ / SAMPLE — NOT FOR SALE).
    Kargo belgesi yalnız kargolandıktan sonra (routers/delivery.delivery_document kalıbı)."""
    s = db.query(InfluencerShipment).filter(InfluencerShipment.id == sid).first()
    if not s or not s.delivery:
        return _err("Gönderim bulunamadı.", 404)
    d = s.delivery
    if d.delivery_type == "kargo" and d.status != "shipped":
        return _err("Teslim belgesi yalnızca kargolandıktan sonra üretilir.", 403)
    view = _delivery_view(d)
    view["sample_notice"] = True
    view["doc_lang"] = (d.doc_lang or "TR")
    try:
        content = render_delivery_pdf(view)
    except Exception:
        return _err("Belge üretilemedi.", 500)
    fname = delivery_doc_filename(d.document_no, d.recipient_name)
    return StreamingResponse(io.BytesIO(content), media_type="application/pdf",
                             headers={"Content-Disposition": content_disposition(fname)})


# ═══════════════════════════════════════════════════════════════════════════
#  İÇERİK
# ═══════════════════════════════════════════════════════════════════════════

class ContentIn(BaseModel):
    platform: Optional[str] = Field(None, max_length=20)
    type: Optional[str] = Field(None, max_length=20)
    url: Optional[str] = Field(None, max_length=500)
    caption: Optional[str] = Field(None, max_length=10000)
    draft_file_id: Optional[int] = None


@router.get("/contents")
def list_contents(collab_id: Optional[int] = None, status: Optional[str] = None,
                  db: Session = Depends(get_db),
                  _: dict = Depends(require_permission("influencer", "view"))):
    qry = db.query(InfluencerContent)
    if collab_id:
        qry = qry.filter(InfluencerContent.collab_id == collab_id)
    if status:
        qry = qry.filter(InfluencerContent.status == status)
    rows = qry.order_by(InfluencerContent.id.desc()).limit(1000).all()
    return {"items": [_content_out(x) for x in rows], "count": len(rows)}


@router.post("/collabs/{cid}/contents", status_code=201)
def create_content(cid: int, body: ContentIn, request: Request, db: Session = Depends(get_db),
                   current_user: dict = Depends(require_permission("influencer", "edit"))):
    """Taslak içerik kaydı; aşama icerik_bekleniyor ise → taslak_incelemede."""
    c = db.query(InfluencerCollab).filter(InfluencerCollab.id == cid).first()
    if not c:
        return _err("İş birliği bulunamadı.", 404)
    if body.type and body.type not in CONTENT_TYPES:
        return _err("İçerik türü geçersiz.")
    if body.draft_file_id and not db.query(InfluencerFile.id).filter(InfluencerFile.id == body.draft_file_id).first():
        return _err("Taslak dosyası bulunamadı.", 404)
    x = InfluencerContent(collab_id=c.id, platform=(body.platform or "").lower() or None, type=body.type,
                          url=body.url, caption=body.caption, draft_file_id=body.draft_file_id,
                          status="taslak", created_by=_actor(current_user))
    db.add(x)
    db.flush()
    if c.stage in ("icerik_bekleniyor", "revizyon"):
        _move_stage(db, c, "taslak_incelemede", _actor(current_user), strict=False)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.content.create",
                    target_type="influencer_content", target_id=x.id, target_name=c.code)
    return {"ok": True, "id": x.id, "stage": c.stage}


class ReviewIn(BaseModel):
    action: str = Field(..., max_length=10)      # approve | revise
    note: Optional[str] = Field(None, max_length=2000)


@router.post("/contents/{cid}/review")
def review_content(cid: int, body: ReviewIn, request: Request, db: Session = Depends(get_db),
                   current_user: dict = Depends(require_permission("influencer", "approve"))):
    x = db.query(InfluencerContent).filter(InfluencerContent.id == cid).first()
    if not x:
        return _err("İçerik bulunamadı.", 404)
    if body.action not in ("approve", "revise"):
        return _err("action approve / revise olmalı.")
    if body.action == "revise" and not (body.note or "").strip():
        return _err("Revizyon için not zorunludur.")
    c = db.query(InfluencerCollab).filter(InfluencerCollab.id == x.collab_id).first()
    x.review_note = (body.note or "").strip() or None
    x.updated_at = datetime.utcnow()
    if body.action == "approve":
        x.status = "onaylandi"
        x.approved_by = _actor(current_user)
        x.approved_at = datetime.utcnow()
        if c:
            _move_stage(db, c, "onaylandi", _actor(current_user), strict=False)
    else:
        x.status = "revizyon"
        if c:
            _move_stage(db, c, "revizyon", _actor(current_user), body.note, strict=False)
    db.commit()
    log_admin_event(db, request, actor=current_user, action=f"influencer.content.{body.action}",
                    target_type="influencer_content", target_id=x.id, target_name=c.code if c else None,
                    details={"note": body.note})
    return {"ok": True, "id": x.id, "status": x.status, "stage": c.stage if c else None}


class PublishedIn(BaseModel):
    url: Optional[str] = Field(None, max_length=500)
    caption: Optional[str] = Field(None, max_length=10000)
    published_on: Optional[str] = None


@router.post("/contents/{cid}/published")
def content_published(cid: int, body: PublishedIn, request: Request, db: Session = Depends(get_db),
                      current_user: dict = Depends(require_permission("influencer", "edit"))):
    """Yayınlandı → compliance_check (pazar etiketi + marka + yasak kelime) → aşama yayinlandi."""
    x = db.query(InfluencerContent).filter(InfluencerContent.id == cid).first()
    if not x:
        return _err("İçerik bulunamadı.", 404)
    c = db.query(InfluencerCollab).filter(InfluencerCollab.id == x.collab_id).first()
    if body.url:
        x.url = body.url.strip()
    if body.caption is not None:
        x.caption = body.caption
    cfg = _cfg(db)
    comp = ENG.compliance_check(x.caption or "", c.market if c else "TR",
                                ENG.parse_handles(cfg.get(ENG.CFG_BRAND_HANDLES)),
                                cfg.get(ENG.CFG_FORBIDDEN_WORDS) or "")
    x.compliance_json = _jdump(comp)
    x.compliance_score = float(comp["score"])
    x.status = "yayinlandi"
    on = _parse_date(body.published_on)
    x.published_at = datetime.combine(on, datetime.min.time()) if on else datetime.utcnow()
    x.updated_at = datetime.utcnow()
    if c:
        _move_stage(db, c, "yayinlandi", _actor(current_user), strict=False)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.content.published",
                    target_type="influencer_content", target_id=x.id, target_name=c.code if c else None,
                    details={"compliance_score": comp["score"], "hits": comp["forbidden_hits"]})
    return {"ok": True, "id": x.id, "status": x.status, "compliance": comp,
            "compliance_score": comp["score"], "stage": c.stage if c else None}


class ContentMetricsIn(BaseModel):
    window: str = Field("d7", max_length=4)      # d7 | d30
    metrics: dict = Field(default_factory=dict)  # {views, likes, comments, saves, shares, reach, link_clicks}


@router.post("/contents/{cid}/metrics")
def content_metrics(cid: int, body: ContentMetricsIn, request: Request, db: Session = Depends(get_db),
                    current_user: dict = Depends(require_permission("influencer", "edit"))):
    x = db.query(InfluencerContent).filter(InfluencerContent.id == cid).first()
    if not x:
        return _err("İçerik bulunamadı.", 404)
    if body.window not in ("d7", "d30"):
        return _err("window d7 / d30 olmalı.")
    clean = {}
    for k, v in (body.metrics or {}).items():
        try:
            clean[str(k)[:30]] = float(v)
        except (TypeError, ValueError):
            continue
    clean["at"] = datetime.utcnow().isoformat()
    if body.window == "d7":
        x.metrics_d7_json = _jdump(clean)
    else:
        x.metrics_d30_json = _jdump(clean)
    x.updated_at = datetime.utcnow()
    c = db.query(InfluencerCollab).filter(InfluencerCollab.id == x.collab_id).first()
    if body.window == "d30" and c:
        _move_stage(db, c, "metrik_toplandi", _actor(current_user), strict=False)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.content.metrics",
                    target_type="influencer_content", target_id=x.id, target_name=c.code if c else None,
                    details={"window": body.window})
    return {"ok": True, "id": x.id, "window": body.window, "stage": c.stage if c else None}


# ═══════════════════════════════════════════════════════════════════════════
#  DOSYALAR
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/files/upload", status_code=201)
async def upload_file(request: Request, file: UploadFile = File(...), entity: str = Form(...),
                      entity_id: int = Form(...), kind: str = Form("other"),
                      db: Session = Depends(get_db),
                      current_user: dict = Depends(require_permission("influencer", "edit"))):
    if entity not in FILE_ENTITIES:
        return _err("entity creator / collab / content / payout / application olmalı.")
    if kind not in FILE_KINDS:
        return _err("kind geçersiz.")
    try:
        stored, size, mime = await save_media(file)
    except ValueError as e:
        return _err(str(e))
    f = InfluencerFile(entity=entity, entity_id=entity_id, kind=kind,
                       original_name=(file.filename or stored)[:255], stored_name=stored,
                       size_bytes=size, content_type=mime, uploaded_by=_actor(current_user))
    db.add(f)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.file.upload",
                    target_type="influencer_file", target_id=f.id, target_name=f.original_name,
                    details={"entity": entity, "entity_id": entity_id, "kind": kind, "size": size})
    out = _file_out(f)
    out["ok"] = True
    return out


@router.get("/files")
def list_files(entity: str, entity_id: int, db: Session = Depends(get_db),
               _: dict = Depends(require_permission("influencer", "view"))):
    rows = (db.query(InfluencerFile).filter(InfluencerFile.entity == entity, InfluencerFile.entity_id == entity_id)
            .order_by(InfluencerFile.id.desc()).all())
    return {"items": [_file_out(f) for f in rows], "count": len(rows)}


@router.get("/files/{fid}")
def get_file(fid: int, db: Session = Depends(get_db),
             _: dict = Depends(require_permission("influencer", "view"))):
    """İndirme — Drive kalıbı: attachment + octet-stream (inline render yok → XSS-güvenli)."""
    f = db.query(InfluencerFile).filter(InfluencerFile.id == fid).first()
    if not f:
        return _err("Dosya bulunamadı.", 404)
    try:
        p = stored_path(f.stored_name)
    except ValueError:
        return _err("Dosya bulunamadı.", 404)
    if not p.exists():
        return _err("Dosya diskte yok.", 404)
    return FileResponse(str(p), media_type="application/octet-stream",
                        headers={"Content-Disposition": content_disposition(f.original_name)})


# ═══════════════════════════════════════════════════════════════════════════
#  AYARLAR
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/settings")
def get_settings(db: Session = Depends(get_db),
                 _: dict = Depends(require_permission("influencer", "settings"))):
    return {"settings": _cfg(db), "defaults": dict(ENG.CFG_DEFAULTS), "keys": list(ENG.CFG_KEYS),
            "tiers": _tier_rows(db), "benchmarks": _benchmark_rows(db),
            "benchmark_metrics": list(ENG.BENCHMARK_METRICS), "platforms": list(PLATFORMS),
            "label_templates": dict(ENG.LABEL_TEMPLATES), "consent_version": CONSENT_VERSION}


class SettingsIn(BaseModel):
    settings: dict = Field(default_factory=dict)


@router.put("/settings")
def put_settings(body: SettingsIn, request: Request, db: Session = Depends(get_db),
                 current_user: dict = Depends(require_permission("influencer", "settings"))):
    changed = {}
    for k, v in (body.settings or {}).items():
        if k not in ENG.CFG_KEYS:
            return _err(f"Bilinmeyen ayar: {k}")
        val = None if v is None else str(v).strip()
        if k == ENG.CFG_FORBIDDEN_WORDS and val:
            try:
                re.compile(val)
            except re.error:
                return _err("Yasak kelime deseni geçerli bir regex değil.")
        if k in (ENG.CFG_REMINDER_DAYS,) and val and not ENG.parse_int_list(val):
            return _err("Hatırlatma günleri '7,14' biçiminde olmalı.")
        _set_setting(db, k, val)
        changed[k] = val
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.settings.update",
                    target_type="app_setting", details=changed)
    return {"ok": True, "settings": _cfg(db)}


class TierIn(BaseModel):
    label: Optional[str] = Field(None, max_length=60)
    min_followers: Optional[int] = Field(None, ge=0)
    max_followers: Optional[int] = Field(None, ge=0)
    commission_pct: Optional[float] = Field(None, ge=0, le=100)
    customer_discount_pct: Optional[float] = Field(None, ge=0, le=100)
    max_gift_items: Optional[int] = Field(None, ge=0)
    launch_access: Optional[bool] = None
    code_allowed: Optional[bool] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None
    clear_max: Optional[bool] = False    # max_followers'ı NULL'a çek


@router.put("/settings/tiers/{key}")
def put_tier(key: str, body: TierIn, request: Request, db: Session = Depends(get_db),
             current_user: dict = Depends(require_permission("influencer", "settings"))):
    t = db.query(InfluencerTier).filter(InfluencerTier.key == key).first()
    if not t:
        return _err("Kademe bulunamadı.", 404)
    for f in ("label", "min_followers", "commission_pct", "customer_discount_pct",
              "max_gift_items", "launch_access", "code_allowed", "sort_order", "is_active"):
        v = getattr(body, f)
        if v is not None:
            setattr(t, f, v)
    if body.clear_max:
        t.max_followers = None
    elif body.max_followers is not None:
        t.max_followers = body.max_followers
    if t.max_followers is not None and t.max_followers < t.min_followers:
        return _err("Üst sınır alt sınırdan küçük olamaz.")
    t.updated_at = datetime.utcnow()
    t.updated_by = _actor(current_user)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.tier.update",
                    target_type="influencer_tier", target_id=t.id, target_name=t.key)
    return {"ok": True, "tier": _tier_out(t)}


class BenchmarkRow(BaseModel):
    platform: str = Field(..., max_length=20)
    tier_key: str = Field(..., max_length=20)
    metric: str = Field(..., max_length=30)
    low: float
    high: float


class BenchmarksIn(BaseModel):
    items: List[BenchmarkRow] = Field(..., max_length=500)


@router.put("/settings/benchmarks")
def put_benchmarks(body: BenchmarksIn, request: Request, db: Session = Depends(get_db),
                   current_user: dict = Depends(require_permission("influencer", "settings"))):
    """Toplu upsert — (platform, tier_key, metric) anahtarıyla."""
    n = 0
    for r in body.items:
        if r.platform not in PLATFORMS or r.metric not in ENG.BENCHMARK_METRICS:
            return _err(f"Geçersiz satır: {r.platform}/{r.tier_key}/{r.metric}")
        if r.high < r.low:
            return _err(f"{r.platform}/{r.tier_key}/{r.metric}: üst sınır alt sınırdan küçük.")
        b = db.query(InfluencerBenchmark).filter(InfluencerBenchmark.platform == r.platform,
                                                 InfluencerBenchmark.tier_key == r.tier_key,
                                                 InfluencerBenchmark.metric == r.metric).first()
        if not b:
            b = InfluencerBenchmark(platform=r.platform, tier_key=r.tier_key, metric=r.metric)
            db.add(b)
        b.low, b.high = float(r.low), float(r.high)
        b.updated_at = datetime.utcnow()
        b.updated_by = _actor(current_user)
        n += 1
    db.commit()
    log_admin_event(db, request, actor=current_user, action="influencer.benchmarks.update",
                    target_type="influencer_benchmark", details={"count": n})
    return {"ok": True, "count": n, "benchmarks": _benchmark_rows(db)}


# ═══════════════════════════════════════════════════════════════════════════
#  PUBLIC — /basvuru (auth YOK; CSRF muaf — anonimde access_token cookie yok)
# ═══════════════════════════════════════════════════════════════════════════

class PublicAccountIn(BaseModel):
    platform: str = Field(..., max_length=20)
    url: str = Field(..., max_length=500)
    followers_claimed: Optional[int] = Field(0, ge=0)


class ApplicationIn(BaseModel):
    store_key: str = Field("minerva", max_length=20)
    lang: Optional[str] = Field("tr", max_length=5)
    full_name: str = Field(..., min_length=2, max_length=150)
    email: str = Field(..., max_length=150)
    phone: Optional[str] = Field(None, max_length=50)
    country: Optional[str] = Field(None, max_length=60)
    city: Optional[str] = Field(None, max_length=100)
    birth_year: Optional[int] = None
    accounts: List[PublicAccountIn] = Field(default_factory=list, max_length=6)
    niches: List[str] = Field(default_factory=list, max_length=20)
    skin_type: Optional[str] = Field(None, max_length=40)
    recent_links: List[str] = Field(default_factory=list, max_length=6)
    why: Optional[str] = Field(None, max_length=2000)
    past_brands: Optional[str] = Field(None, max_length=1000)
    accepted_model: Optional[str] = Field("barter", max_length=20)
    consent_ack: bool = False
    consent_version: Optional[str] = Field(CONSENT_VERSION, max_length=20)
    website: Optional[str] = Field(None, max_length=300)     # honeypot


def _msg(lang: str, tr: str, en: str) -> str:
    return en if (lang or "tr").lower().startswith("en") else tr


@public_router.post("/gonder", status_code=201)
@limiter.limit("5/hour")
async def submit_application(body: ApplicationIn, request: Request, db: Session = Depends(get_db)):
    """Halka açık başvuru — honeypot doluysa 200 + sessiz (kaydetme); 18 altı red."""
    lang = (body.lang or "tr").lower()
    if (body.website or "").strip():
        return JSONResponse(status_code=200, content={"ok": True, "message": _msg(lang, "Başvurun alındı.", "Application received.")})
    if body.store_key not in STORE_KEYS:
        return _err(_msg(lang, "Geçersiz mağaza.", "Invalid store."))
    email = ENG.normalize_email(body.email)
    if not _EMAIL_RE.match(email):
        return _err(_msg(lang, "Geçerli bir e-posta girin.", "Enter a valid e-mail."))
    if body.birth_year is None:
        return _err(_msg(lang, "Doğum yılı zorunludur.", "Birth year is required."))
    year_now = datetime.utcnow().year
    if body.birth_year < 1900 or body.birth_year > year_now:
        return _err(_msg(lang, "Geçersiz doğum yılı.", "Invalid birth year."))
    if year_now - body.birth_year < MIN_AGE:
        return _err(_msg(lang, "Programa yalnızca 18 yaş ve üzeri başvurabilir.",
                         "You must be 18 or older to apply."))
    if not body.consent_ack:
        return _err(_msg(lang, "Devam etmek için KVKK onay kutusunu işaretleyin.",
                         "Please accept the privacy notice to continue."))
    accounts = []
    for a in body.accounts:
        platform = (a.platform or "").strip().lower()
        if platform not in PLATFORMS:
            continue
        handle = _handle_from_url(a.url)
        if not handle:
            continue
        accounts.append({"platform": platform, "handle": handle, "url": a.url.strip()[:500],
                         "followers_claimed": int(a.followers_claimed or 0)})
    if not accounts:
        return _err(_msg(lang, "En az bir sosyal medya hesabı girin.", "Add at least one social account."))
    if (body.accepted_model or "barter") not in MODELS:
        return _err(_msg(lang, "Geçersiz iş birliği modeli.", "Invalid collaboration model."))
    links = [l.strip()[:500] for l in body.recent_links if (l or "").strip()][:3]
    a = InfluencerApplication(
        store_key=body.store_key, full_name=body.full_name.strip()[:150], email=email,
        phone=(body.phone or "").strip()[:50] or None, country=(body.country or "").strip().upper()[:60] or None,
        city=(body.city or "").strip()[:100] or None, language=lang[:10], birth_year=body.birth_year,
        accounts_json=_jdump(accounts),
        form_json=_jdump({"niches": [str(x).strip()[:60] for x in body.niches if str(x).strip()],
                          "skin_type": body.skin_type, "recent_links": links, "why": body.why,
                          "past_brands": body.past_brands, "accepted_model": body.accepted_model or "barter",
                          "source": "form"}),
        status="pending", consent_text_version=(body.consent_version or CONSENT_VERSION)[:20],
        consent_at=datetime.utcnow(), consent_ip=_client_ip(request)[:64],
        user_agent=(request.headers.get("user-agent", "") or "")[:300],
    )
    db.add(a)
    db.commit()
    return JSONResponse(status_code=201, content={
        "ok": True, "id": a.id,
        "message": _msg(lang, "Başvurun alındı — ekibimiz inceleyip e-posta ile dönecek.",
                        "Application received — our team will review it and reply by e-mail.")})


def load_token_for_page(db: Session, token: str) -> dict:
    """GET /basvuru/t/{token} sayfa context'i — state: open|notfound|expired|used."""
    t = db.query(InfluencerToken).filter(InfluencerToken.token == token).first()
    ctx = {"state": _token_state(t), "token": token, "purpose": None, "creator_name": None,
           "market": "TR", "label_template": None, "claim_sheet": [], "consent_version": CONSENT_VERSION,
           "collab_code": None, "store_key": None, "lang": "tr"}
    if not t:
        return ctx
    cr = db.query(InfluencerCreator).filter(InfluencerCreator.id == t.creator_id).first() if t.creator_id else None
    collab = db.query(InfluencerCollab).filter(InfluencerCollab.id == t.collab_id).first() if t.collab_id else None
    market = (collab.market if collab and collab.market else "TR").upper()
    store = collab.store_key if collab else "minerva"
    brand_handles = ENG.parse_handles(_cfg(db).get(ENG.CFG_BRAND_HANDLES))
    brand = next((h for h in brand_handles if store in h), brand_handles[0] if brand_handles else store)
    claim_sheet = []
    if collab and collab.campaign and collab.campaign.claim_sheet_json:
        claim_sheet = _jload(collab.campaign.claim_sheet_json, [])
    ctx.update({"purpose": t.purpose, "creator_name": cr.full_name if cr else None, "market": market,
                "label_template": ENG.label_template(market, brand), "claim_sheet": claim_sheet,
                "collab_code": collab.code if collab else None, "store_key": store,
                "lang": (cr.language or ("tr" if market == "TR" else "en")) if cr else ("tr" if market == "TR" else "en"),
                "guideline_version": _cfg(db).get(ENG.CFG_GUIDELINE_VERSION)})
    return ctx


def _lock_token(db: Session, token: str, purpose: str):
    """with_for_update ile kilitle; (token, hata_yanıtı) döner — tek kullanım garantisi."""
    t = db.query(InfluencerToken).filter(InfluencerToken.token == token).with_for_update().first()
    if not t:
        return None, _err("Bu bağlantı geçersiz.", 404)
    if t.used_at:
        return None, _err("Bu bağlantı daha önce kullanılmış.", 410)
    if t.expires_at and t.expires_at < datetime.utcnow():
        return None, _err("Bu bağlantının süresi dolmuş.", 410)
    if t.purpose != purpose:
        return None, _err("Bu bağlantı bu işlem için değil.", 400)
    return t, None


class AddressIn(BaseModel):
    recipient: str = Field(..., min_length=2, max_length=150)
    line1: str = Field(..., min_length=3, max_length=200)
    line2: Optional[str] = Field(None, max_length=200)
    district: Optional[str] = Field(None, max_length=100)
    city: str = Field(..., min_length=2, max_length=100)
    postal_code: Optional[str] = Field(None, max_length=20)
    country: Optional[str] = Field("TR", max_length=60)
    phone: Optional[str] = Field(None, max_length=50)


@public_router.post("/t/{token}/adres")
@limiter.limit("10/hour")
def claim_address(token: str, body: AddressIn, request: Request, db: Session = Depends(get_db)):
    t, err = _lock_token(db, token, "address")
    if err:
        return err
    now = datetime.utcnow()
    db.query(InfluencerAddress).filter(InfluencerAddress.creator_id == t.creator_id).update({"is_default": False})
    ad = InfluencerAddress(creator_id=t.creator_id, label="claim", recipient_name=body.recipient.strip(),
                           line1=body.line1.strip(), line2=(body.line2 or "").strip() or None,
                           district=(body.district or "").strip() or None, city=body.city.strip(),
                           postal_code=(body.postal_code or "").strip() or None,
                           country=(body.country or "TR").strip().upper()[:60], phone=(body.phone or "").strip() or None,
                           is_default=True, verified_at=now, verified_ip=_client_ip(request)[:64])
    db.add(ad)
    db.flush()
    t.used_at = now
    if t.collab_id:
        c = db.query(InfluencerCollab).filter(InfluencerCollab.id == t.collab_id).first()
        if c:
            _move_stage(db, c, "urun_hazirlaniyor", "creator", strict=False)
    _activity(db, None, creator_id=t.creator_id, collab_id=t.collab_id, subject="Adres alındı",
              body=f"{ad.city} / {ad.country}")
    db.commit()
    return {"ok": True, "id": ad.id, "message": "Adresin alındı, teşekkürler."}


@public_router.post("/t/{token}/insights")
@limiter.limit("10/hour")
async def claim_insights(token: str, request: Request, file: UploadFile = File(...),
                         reach_30d: Optional[int] = Form(None), engaged_30d: Optional[int] = Form(None),
                         followers: Optional[int] = Form(None), geo_target_pct: Optional[int] = Form(None),
                         db: Session = Depends(get_db)):
    """Insights ekran kaydı (≤60 MB video) + 4 elle sayı."""
    t, err = _lock_token(db, token, "insights")
    if err:
        return err
    try:
        stored, size, mime = await save_media(file)
    except ValueError as e:
        return _err(str(e))
    f = InfluencerFile(entity="creator", entity_id=t.creator_id, kind="insights_video",
                       original_name=(file.filename or stored)[:255], stored_name=stored, size_bytes=size,
                       content_type=mime, uploaded_by="creator")
    db.add(f)
    db.flush()
    nums = {"reach_30d": reach_30d, "engaged_30d": engaged_30d, "followers": followers,
            "geo_target_pct": geo_target_pct, "file_id": f.id}
    acc = (db.query(InfluencerAccount).filter(InfluencerAccount.creator_id == t.creator_id)
           .order_by(InfluencerAccount.is_primary.desc(), InfluencerAccount.id).first())
    if acc:
        if followers:
            acc.followers = int(followers)
        if geo_target_pct is not None:
            geo = _jload(acc.audience_geo_json, {}) or {}
            geo["target_pct"] = int(geo_target_pct)
            acc.audience_geo_json = _jdump(geo)
        acc.updated_at = datetime.utcnow()
    _activity(db, None, creator_id=t.creator_id, collab_id=t.collab_id, subject="Insights beyanı",
              body=_jdump(nums))
    t.used_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "file_id": f.id, "message": "Kaydın alındı, teşekkürler."}


class AckIn(BaseModel):
    ack: bool = False
    version: Optional[str] = Field(None, max_length=20)


@public_router.post("/t/{token}/onay")
@limiter.limit("10/hour")
def claim_agreement(token: str, body: AckIn, request: Request, db: Session = Depends(get_db)):
    """'Kılavuzu okudum, etiketi ilk satıra yazacağım' onayı → guideline_ack_*."""
    t, err = _lock_token(db, token, "agreement")
    if err:
        return err
    if not body.ack:
        return _err("Devam etmek için onay kutusunu işaretleyin.")
    if not t.collab_id:
        return _err("Bu bağlantı bir iş birliğine bağlı değil.")
    c = db.query(InfluencerCollab).filter(InfluencerCollab.id == t.collab_id).first()
    if not c:
        return _err("İş birliği bulunamadı.", 404)
    now = datetime.utcnow()
    c.guideline_ack_at = now
    c.guideline_version = (body.version or _cfg(db).get(ENG.CFG_GUIDELINE_VERSION) or "")[:20] or None
    c.guideline_ack_ip = _client_ip(request)[:64]
    c.updated_at = now
    _move_stage(db, c, "kabul_edildi", "creator", strict=False)
    t.used_at = now
    _activity(db, None, creator_id=t.creator_id, collab_id=c.id, subject="Kılavuz onayı",
              body=f"sürüm {c.guideline_version}")
    db.commit()
    return {"ok": True, "message": "Onayın kaydedildi, teşekkürler."}
