# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
CRM Faz C — kurumsal uçlar (ayrı router, crm.py'yi şişirmemek için).

  • /search           global arama (firma + kişi + fırsat)
  • /views            kaydedilmiş görünümler (kullanıcı başına filtre seti)
  • /reports/summary  satış hunisi, kazan/kaybet, tahmin, liderlik (C2)
  • /tags … /fields … /attachments … /bulk … /history  (C3–C4)
"""
import json
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, Request, Query, UploadFile, File
from fastapi.responses import JSONResponse, FileResponse
from sqlalchemy import or_, func
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from database import (
    get_db, User, AdminAuditLog,
    CrmCompany, CrmContact, CrmDeal, CrmSavedView,
    CrmTag, CrmEntityTag, CrmFieldDef, CrmFieldValue, CrmAttachment, CrmWaTemplate, to_tr,
)
from core.permissions import require_permission
from core.audit import log_admin_event
from core import crm as C
from core import crm_reports as R
from core import drive as D

router = APIRouter(prefix="/api/crm", tags=["crm-advanced"])

# entity (singular) → (model, geçerli)
_ENT = {"company": CrmCompany, "contact": CrmContact, "deal": CrmDeal}


def _actor(p: dict) -> str:
    return p.get("full_name") or p.get("username") or "—"


def _entity_exists(db: Session, entity: str, eid: int) -> bool:
    """Hedef kayıt gerçekten var mı — olmayan id'ye ek/etiket/alan yazılmasın."""
    model = _ENT.get(entity)
    return bool(model and db.query(model.id).filter(model.id == eid).first())


# ─── Global arama ────────────────────────────────────────────────────────────

@router.get("/search")
def crm_search(q: str = Query(..., min_length=1),
               db: Session = Depends(get_db),
               _: dict = Depends(require_permission("crm", "view"))):
    """Firma + kişi + fırsatta tek aramada en iyi eşleşmeler."""
    like = f"%{q.strip()}%"
    companies = (db.query(CrmCompany).filter(
        CrmCompany.is_active == True,  # noqa: E712
        or_(CrmCompany.name.ilike(like), CrmCompany.email.ilike(like),
            CrmCompany.phone.ilike(like), CrmCompany.city.ilike(like)))
        .order_by(CrmCompany.name).limit(6).all())
    contacts = (db.query(CrmContact).filter(
        CrmContact.is_active == True,  # noqa: E712
        or_(CrmContact.full_name.ilike(like), CrmContact.email.ilike(like),
            CrmContact.phone.ilike(like), CrmContact.mobile.ilike(like),
            CrmContact.whatsapp_number.ilike(like)))
        .order_by(CrmContact.full_name).limit(6).all())
    deals = (db.query(CrmDeal).filter(CrmDeal.title.ilike(like), CrmDeal.is_active == True)  # noqa: E712
             .order_by(CrmDeal.created_at.desc()).limit(6).all())
    cnames = {c.id: c.name for c in db.query(CrmCompany.id, CrmCompany.name).all()}
    return {
        "companies": [{"id": c.id, "label": c.name, "sub": c.city or c.sector or ""} for c in companies],
        "contacts": [{"id": c.id, "label": c.full_name,
                      "sub": " · ".join([x for x in [c.title, cnames.get(c.company_id)] if x])} for c in contacts],
        "deals": [{"id": d.id, "label": d.title,
                   "sub": cnames.get(d.company_id, "") or d.status} for d in deals],
    }


# ─── Kaydedilmiş görünümler ──────────────────────────────────────────────────

class ViewIn(BaseModel):
    entity:   str = Field(..., pattern="^(companies|contacts|deals)$")
    name:     str = Field(..., min_length=1, max_length=80)
    criteria: dict = Field(default_factory=dict)


@router.get("/views")
def list_views(entity: str = Query(...),
               db: Session = Depends(get_db),
               payload: dict = Depends(require_permission("crm", "view"))):
    uid = int(payload.get("sub", 0))
    rows = (db.query(CrmSavedView)
            .filter(CrmSavedView.user_id == uid, CrmSavedView.entity == entity)
            .order_by(CrmSavedView.name).all())
    out = []
    for v in rows:
        try:
            crit = json.loads(v.criteria or "{}")
        except Exception:
            crit = {}
        out.append({"id": v.id, "name": v.name, "criteria": crit})
    return out


@router.post("/views", status_code=201)
def create_view(data: ViewIn, db: Session = Depends(get_db),
                payload: dict = Depends(require_permission("crm", "view"))):
    uid = int(payload.get("sub", 0))
    v = CrmSavedView(user_id=uid, entity=data.entity, name=data.name.strip(),
                     criteria=json.dumps(data.criteria or {}, ensure_ascii=False))
    db.add(v); db.commit(); db.refresh(v)
    return {"id": v.id, "name": v.name, "criteria": data.criteria or {}}


@router.delete("/views/{vid}")
def delete_view(vid: int, db: Session = Depends(get_db),
                payload: dict = Depends(require_permission("crm", "view"))):
    uid = int(payload.get("sub", 0))
    v = db.query(CrmSavedView).filter(CrmSavedView.id == vid, CrmSavedView.user_id == uid).first()
    if not v:
        return JSONResponse(status_code=404, content={"detail": "Görünüm bulunamadı."})
    db.delete(v); db.commit()
    return {"message": "Silindi."}


# ─── Raporlar (C2) ───────────────────────────────────────────────────────────

@router.get("/reports/summary")
def reports_summary(db: Session = Depends(get_db),
                    _: dict = Depends(require_permission("crm", "view"))):
    return R.report_summary(db)


# ─── Etiketler (C3) ──────────────────────────────────────────────────────────

class TagIn(BaseModel):
    name:  str = Field(..., min_length=1, max_length=60)
    color: Optional[str] = Field(None, max_length=20)


class TagSet(BaseModel):
    tag_ids: List[int] = Field(default_factory=list)


@router.get("/tags")
def list_tags(db: Session = Depends(get_db), _: dict = Depends(require_permission("crm", "view"))):
    return [{"id": t.id, "name": t.name, "color": t.color or "#6b7280"}
            for t in db.query(CrmTag).order_by(CrmTag.name).all()]


@router.post("/tags", status_code=201)
def create_tag(data: TagIn, db: Session = Depends(get_db),
               _: dict = Depends(require_permission("crm", "edit"))):
    nm = data.name.strip()
    ex = db.query(CrmTag).filter(func.lower(CrmTag.name) == nm.lower()).first()
    if ex:
        return {"id": ex.id, "name": ex.name, "color": ex.color or "#6b7280"}
    t = CrmTag(name=nm[:60], color=(data.color or "#6b7280")[:20])
    db.add(t); db.commit(); db.refresh(t)
    return {"id": t.id, "name": t.name, "color": t.color}


@router.delete("/tags/{tid}")
def delete_tag(tid: int, db: Session = Depends(get_db),
               _: dict = Depends(require_permission("crm", "delete"))):
    t = db.query(CrmTag).filter(CrmTag.id == tid).first()
    if not t:
        return JSONResponse(status_code=404, content={"detail": "Etiket bulunamadı."})
    db.query(CrmEntityTag).filter(CrmEntityTag.tag_id == tid).delete()
    db.delete(t); db.commit()
    return {"message": "Etiket silindi."}


def _entity_tags(db: Session, entity: str, eid: int):
    rows = (db.query(CrmTag).join(CrmEntityTag, CrmEntityTag.tag_id == CrmTag.id)
            .filter(CrmEntityTag.entity == entity, CrmEntityTag.entity_id == eid)
            .order_by(CrmTag.name).all())
    return [{"id": t.id, "name": t.name, "color": t.color or "#6b7280"} for t in rows]


@router.get("/{entity}/{eid}/tags")
def get_entity_tags(entity: str, eid: int, db: Session = Depends(get_db),
                    _: dict = Depends(require_permission("crm", "view"))):
    if entity not in _ENT:
        return JSONResponse(status_code=400, content={"detail": "Geçersiz varlık."})
    return _entity_tags(db, entity, eid)


@router.put("/{entity}/{eid}/tags")
def set_entity_tags(entity: str, eid: int, data: TagSet, db: Session = Depends(get_db),
                    _: dict = Depends(require_permission("crm", "edit"))):
    if entity not in _ENT:
        return JSONResponse(status_code=400, content={"detail": "Geçersiz varlık."})
    if not _entity_exists(db, entity, eid):
        return JSONResponse(status_code=404, content={"detail": "Kayıt bulunamadı."})
    db.query(CrmEntityTag).filter(CrmEntityTag.entity == entity, CrmEntityTag.entity_id == eid).delete()
    valid = {tid for (tid,) in db.query(CrmTag.id).filter(CrmTag.id.in_(data.tag_ids or [])).all()}
    for tid in (data.tag_ids or []):
        if tid in valid:
            db.add(CrmEntityTag(entity=entity, entity_id=eid, tag_id=tid))
    db.commit()
    return _entity_tags(db, entity, eid)


# ─── WhatsApp mesaj şablonları ───────────────────────────────────────────────
# Tıkla-konuş (wa.me) linkine hazır metin ekler; {ad} → kişinin ilk adı.
# Listeleme crm.view (herkes kullanır); yönetim crm.edit.

class WaTemplateIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    body: str = Field(..., min_length=1, max_length=1000)


@router.get("/wa-templates")
def list_wa_templates(db: Session = Depends(get_db),
                      _: dict = Depends(require_permission("crm", "view"))):
    return [{"id": t.id, "name": t.name, "body": t.body}
            for t in db.query(CrmWaTemplate).order_by(CrmWaTemplate.name).all()]


@router.post("/wa-templates", status_code=201)
def create_wa_template(data: WaTemplateIn, request: Request, db: Session = Depends(get_db),
                       current_user: dict = Depends(require_permission("crm", "edit"))):
    t = CrmWaTemplate(name=data.name.strip()[:100], body=data.body.strip()[:1000],
                      created_by=_actor(current_user))
    db.add(t); db.commit(); db.refresh(t)
    log_admin_event(db, request, actor=current_user, action="crm.wa_template.create",
                    target_type="crm_wa_template", target_id=t.id, target_name=t.name)
    return {"id": t.id, "name": t.name, "body": t.body}


@router.delete("/wa-templates/{tid}")
def delete_wa_template(tid: int, request: Request, db: Session = Depends(get_db),
                       current_user: dict = Depends(require_permission("crm", "edit"))):
    t = db.query(CrmWaTemplate).filter(CrmWaTemplate.id == tid).first()
    if not t:
        return JSONResponse(status_code=404, content={"detail": "Şablon bulunamadı."})
    name = t.name
    db.delete(t); db.commit()
    log_admin_event(db, request, actor=current_user, action="crm.wa_template.delete",
                    target_type="crm_wa_template", target_id=tid, target_name=name)
    return {"message": "Şablon silindi."}


# ─── Özel alanlar (C3) ───────────────────────────────────────────────────────

class FieldIn(BaseModel):
    entity:     str = Field(..., pattern="^(company|contact|deal)$")
    label:      str = Field(..., min_length=1, max_length=80)
    field_type: str = Field("text", pattern="^(text|number|date|select)$")
    options:    Optional[List[str]] = None


class FieldValues(BaseModel):
    values: dict = Field(default_factory=dict)   # {field_id(str): value}


def _slug_key(label: str) -> str:
    import re
    tr = {"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u"}
    s = "".join(tr.get(ch, ch) for ch in label.lower())
    return (re.sub(r"[^a-z0-9]+", "_", s).strip("_") or "alan")[:40]


def _field_defs(db: Session, entity: str):
    return (db.query(CrmFieldDef)
            .filter(CrmFieldDef.entity == entity, CrmFieldDef.is_active == True)  # noqa: E712
            .order_by(CrmFieldDef.sort_order, CrmFieldDef.id).all())


@router.get("/fields")
def list_fields(entity: str = Query(...), db: Session = Depends(get_db),
                _: dict = Depends(require_permission("crm", "view"))):
    if entity not in _ENT:
        return JSONResponse(status_code=400, content={"detail": "Geçersiz varlık."})
    return [{"id": f.id, "key": f.key, "label": f.label, "field_type": f.field_type,
             "options": json.loads(f.options or "[]")} for f in _field_defs(db, entity)]


@router.post("/fields", status_code=201)
def create_field(data: FieldIn, db: Session = Depends(get_db),
                 _: dict = Depends(require_permission("admin", "view"))):
    maxo = db.query(func.count(CrmFieldDef.id)).filter(CrmFieldDef.entity == data.entity).scalar() or 0
    f = CrmFieldDef(entity=data.entity, key=_slug_key(data.label), label=data.label.strip()[:80],
                    field_type=data.field_type, options=json.dumps(data.options or [], ensure_ascii=False),
                    sort_order=maxo)
    db.add(f); db.commit(); db.refresh(f)
    return {"id": f.id, "label": f.label, "field_type": f.field_type}


@router.delete("/fields/{fid}")
def delete_field(fid: int, db: Session = Depends(get_db),
                 _: dict = Depends(require_permission("admin", "view"))):
    f = db.query(CrmFieldDef).filter(CrmFieldDef.id == fid).first()
    if not f:
        return JSONResponse(status_code=404, content={"detail": "Alan bulunamadı."})
    db.query(CrmFieldValue).filter(CrmFieldValue.field_id == fid).delete()
    db.delete(f); db.commit()
    return {"message": "Alan silindi."}


@router.get("/{entity}/{eid}/fields")
def get_entity_fields(entity: str, eid: int, db: Session = Depends(get_db),
                      _: dict = Depends(require_permission("crm", "view"))):
    if entity not in _ENT:
        return JSONResponse(status_code=400, content={"detail": "Geçersiz varlık."})
    vals = {v.field_id: v.value for v in db.query(CrmFieldValue)
            .filter(CrmFieldValue.entity == entity, CrmFieldValue.entity_id == eid).all()}
    return [{"id": f.id, "key": f.key, "label": f.label, "field_type": f.field_type,
             "options": json.loads(f.options or "[]"), "value": vals.get(f.id, "")}
            for f in _field_defs(db, entity)]


@router.put("/{entity}/{eid}/fields")
def set_entity_fields(entity: str, eid: int, data: FieldValues, db: Session = Depends(get_db),
                      _: dict = Depends(require_permission("crm", "edit"))):
    if entity not in _ENT:
        return JSONResponse(status_code=400, content={"detail": "Geçersiz varlık."})
    if not _entity_exists(db, entity, eid):
        return JSONResponse(status_code=404, content={"detail": "Kayıt bulunamadı."})
    own = {f.id for f in _field_defs(db, entity)}
    for fid_str, val in (data.values or {}).items():
        try:
            fid = int(fid_str)
        except (TypeError, ValueError):
            continue
        if fid not in own:
            continue
        ex = (db.query(CrmFieldValue)
              .filter(CrmFieldValue.field_id == fid, CrmFieldValue.entity == entity,
                      CrmFieldValue.entity_id == eid).first())
        if ex:
            ex.value = (val or None)
        else:
            db.add(CrmFieldValue(field_id=fid, entity=entity, entity_id=eid, value=(val or None)))
    db.commit()
    return {"message": "Kaydedildi."}


# ─── Dosya ekleri (C4) ───────────────────────────────────────────────────────

@router.get("/{entity}/{eid}/attachments")
def list_attachments(entity: str, eid: int, db: Session = Depends(get_db),
                     _: dict = Depends(require_permission("crm", "view"))):
    if entity not in _ENT:
        return JSONResponse(status_code=400, content={"detail": "Geçersiz varlık."})
    rows = (db.query(CrmAttachment)
            .filter(CrmAttachment.entity == entity, CrmAttachment.entity_id == eid)
            .order_by(CrmAttachment.id.desc()).all())
    return [{"id": a.id, "name": a.original_name, "size_human": D.humanize(a.size_bytes),
             "uploaded_by": a.uploaded_by or "—", "created_at": C.fmt_dt(a.created_at)} for a in rows]


@router.post("/{entity}/{eid}/attachments", status_code=201)
async def upload_attachment(entity: str, eid: int, file: UploadFile = File(...),
                            db: Session = Depends(get_db),
                            current_user: dict = Depends(require_permission("crm", "create"))):
    if entity not in _ENT:
        return JSONResponse(status_code=400, content={"detail": "Geçersiz varlık."})
    if not _entity_exists(db, entity, eid):
        return JSONResponse(status_code=404, content={"detail": "Kayıt bulunamadı."})
    try:
        stored, size, ctype = await D.save_upload(file)
    except ValueError as e:
        return JSONResponse(status_code=413, content={"detail": str(e)})
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "Yükleme hatası."})
    rec = CrmAttachment(entity=entity, entity_id=eid, original_name=(file.filename or "dosya")[:255],
                        stored_name=stored, size_bytes=size, content_type=ctype,
                        uploaded_by=_actor(current_user))
    db.add(rec); db.commit(); db.refresh(rec)
    return {"id": rec.id, "name": rec.original_name, "size_human": D.humanize(size)}


@router.get("/attachments/{aid}/download")
def download_attachment(aid: int, db: Session = Depends(get_db),
                        _: dict = Depends(require_permission("crm", "view"))):
    a = db.query(CrmAttachment).filter(CrmAttachment.id == aid).first()
    if not a:
        return JSONResponse(status_code=404, content={"detail": "Dosya bulunamadı."})
    try:
        path = D.stored_path(a.stored_name)
    except ValueError:
        return JSONResponse(status_code=404, content={"detail": "Dosya bulunamadı."})
    if not path.is_file():
        return JSONResponse(status_code=404, content={"detail": "Dosya diskte yok."})
    return FileResponse(str(path), media_type="application/octet-stream",
                        filename=D.safe_download_name(a.original_name))


@router.delete("/attachments/{aid}")
def delete_attachment(aid: int, db: Session = Depends(get_db),
                      _: dict = Depends(require_permission("crm", "delete"))):
    a = db.query(CrmAttachment).filter(CrmAttachment.id == aid).first()
    if not a:
        return JSONResponse(status_code=404, content={"detail": "Dosya bulunamadı."})
    stored = a.stored_name
    db.delete(a); db.commit()
    D.delete_stored(stored)
    return {"message": "Dosya silindi."}


# ─── Toplu işlemler (C4) ─────────────────────────────────────────────────────

class BulkIn(BaseModel):
    entity: str = Field(..., pattern="^(companies|contacts|deals)$")
    ids:    List[int] = Field(..., min_length=1, max_length=1000)
    action: str = Field(..., pattern="^(assign|source|tag|delete)$")
    value:  Optional[str] = None        # assign→user_id, source→meta/kommo/manual, tag→tag_id


_PLURAL = {"companies": (CrmCompany, "company"), "contacts": (CrmContact, "contact"),
           "deals": (CrmDeal, "deal")}


@router.post("/bulk")
def bulk_action(data: BulkIn, request: Request, db: Session = Depends(get_db),
                current_user: dict = Depends(require_permission("crm", "edit"))):
    model, singular = _PLURAL[data.entity]
    rows = db.query(model).filter(model.id.in_(data.ids)).all()
    n = 0
    if data.action == "assign":
        uid = int(data.value) if data.value else None
        name = None
        if uid:
            u = db.query(User.full_name).filter(User.id == uid).first()
            name = u[0] if u else None
        for r in rows:
            r.owner_user_id = uid
            if hasattr(r, "owner_name"):
                r.owner_name = name
            n += 1
    elif data.action == "source":
        for r in rows:
            r.source = data.value or "manual"; n += 1
    elif data.action == "tag":
        tid = int(data.value) if data.value else None
        if not tid or not db.query(CrmTag.id).filter(CrmTag.id == tid).first():
            return JSONResponse(status_code=400, content={"detail": "Geçersiz etiket."})
        for r in rows:
            ex = (db.query(CrmEntityTag.id)
                  .filter(CrmEntityTag.entity == singular, CrmEntityTag.entity_id == r.id,
                          CrmEntityTag.tag_id == tid).first())
            if not ex:
                db.add(CrmEntityTag(entity=singular, entity_id=r.id, tag_id=tid))
            n += 1
    elif data.action == "delete":
        # crm.delete yetkisi gerekir
        from core.permissions import _has_permission
        u = db.query(User).filter(User.id == int(current_user.get("sub", 0))).first()
        if not (u and _has_permission(u, "crm", "delete")):
            return JSONResponse(status_code=403, content={"detail": "Silme yetkiniz yok."})
        for r in rows:
            r.is_active = False; n += 1
    db.commit()
    log_admin_event(db, request, actor=current_user, action=f"crm.bulk.{data.action}",
                    target_type="crm_" + data.entity, target_name=data.entity,
                    details={"count": n, "value": data.value})
    return {"message": f"{n} kayıt güncellendi.", "count": n}


# ─── Kayıt geçmişi (C4) ──────────────────────────────────────────────────────

@router.get("/{entity}/{eid}/history")
def entity_history(entity: str, eid: int, db: Session = Depends(get_db),
                   _: dict = Depends(require_permission("crm", "view"))):
    if entity not in _ENT:
        return JSONResponse(status_code=400, content={"detail": "Geçersiz varlık."})
    rows = (db.query(AdminAuditLog)
            .filter(AdminAuditLog.target_type == "crm_" + entity, AdminAuditLog.target_id == eid)
            .order_by(AdminAuditLog.timestamp.desc()).limit(50).all())
    out = []
    for r in rows:
        try:
            det = json.loads(r.details) if r.details else None
        except Exception:
            det = None
        out.append({"action": r.action, "actor": r.actor_name or "—",
                    "at": C.fmt_dt(r.timestamp), "details": det})
    return out
