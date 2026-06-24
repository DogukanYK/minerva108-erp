# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
CRM — Müşteri İlişkileri Yönetimi API'si.

Cross-cutting (domain'siz): tek birleşik CRM, Kozmetik/Supplement ayrımına tabi
değil.  Erişim paylaşımlı — tüm CRM kullanıcıları tüm kayıt/aktiviteleri görür;
owner alanları sorumluluk içindir.  Yetki: require_permission("crm", …).

Uçlar:
  • /companies  firma kartları (soft-delete)
  • /contacts   kişi kartları (firmaya bağlı)
  • /deals + /pipeline  satış fırsatları + Kanban
  • /activities paylaşımlı zaman çizelgesi (not/arama/toplantı/e-posta/whatsapp)
  • /tasks      hatırlatma/görev (günlük scheduler push'u besler)
  • /stages /users /dashboard  yardımcı uçlar
"""
from datetime import datetime, timedelta
from typing import Optional, List

from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from database import (
    get_db, User, TR_OFFSET,
    CrmCompany, CrmContact, CrmStage, CrmDeal, CrmActivity, CrmTask, Quotation,
)
from core.permissions import require_permission, _has_permission
from core.audit import log_admin_event
from core import crm as C

router = APIRouter(prefix="/api/crm", tags=["crm"])

_ACTIVITY_TYPES = {"note", "call", "meeting", "email", "whatsapp"}


# ─── Yardımcılar ─────────────────────────────────────────────────────────────

def _actor(payload: dict) -> str:
    return payload.get("full_name") or payload.get("username") or "—"


def _user_name(db: Session, user_id: Optional[int]) -> Optional[str]:
    """user_id → full_name; yoksa None."""
    if not user_id:
        return None
    u = db.query(User.full_name).filter(User.id == user_id).first()
    return u[0] if u else None


def _now() -> datetime:
    return datetime.utcnow()


def _today_end_utc() -> datetime:
    """Bugünün (TR) gün sonunun UTC karşılığı — 'bugün vadeli' filtresi için."""
    tr_today = (datetime.utcnow() + TR_OFFSET).date()
    tr_end = datetime(tr_today.year, tr_today.month, tr_today.day, 23, 59, 59)
    return tr_end - TR_OFFSET


def _source_filter(query, model, source: Optional[str]):
    """Kaynak filtresi: meta / kommo / manual (manual = NULL veya 'manual').
    Boş/all → filtre yok."""
    if not source or source == "all":
        return query
    if source == "manual":
        return query.filter(or_(model.source.is_(None), model.source == "manual"))
    return query.filter(model.source == source)


# ─── Pydantic şemaları ───────────────────────────────────────────────────────

class CompanyIn(BaseModel):
    name:       str = Field(..., min_length=1, max_length=200)
    sector:     Optional[str] = Field(None, max_length=100)
    website:    Optional[str] = Field(None, max_length=200)
    phone:      Optional[str] = Field(None, max_length=50)
    email:      Optional[str] = Field(None, max_length=150)
    address:    Optional[str] = None
    city:       Optional[str] = Field(None, max_length=100)
    country:    Optional[str] = Field(None, max_length=100)
    tax_office: Optional[str] = Field(None, max_length=120)
    tax_no:     Optional[str] = Field(None, max_length=50)
    notes:      Optional[str] = None
    owner_user_id: Optional[int] = None


class ContactIn(BaseModel):
    full_name:       str = Field(..., min_length=1, max_length=150)
    company_id:      Optional[int] = None
    title:           Optional[str] = Field(None, max_length=100)
    phone:           Optional[str] = Field(None, max_length=50)
    mobile:          Optional[str] = Field(None, max_length=50)
    email:           Optional[str] = Field(None, max_length=150)
    whatsapp_number: Optional[str] = Field(None, max_length=50)
    source:          Optional[str] = Field(None, max_length=50)
    notes:           Optional[str] = None
    owner_user_id:   Optional[int] = None


class DealIn(BaseModel):
    title:             str = Field(..., min_length=1, max_length=200)
    company_id:        Optional[int] = None
    contact_id:        Optional[int] = None
    stage_id:          Optional[int] = None
    value:             Optional[float] = 0.0
    currency:          str = Field("TRY", max_length=3)
    probability:       Optional[int] = Field(0, ge=0, le=100)
    expected_close_at: Optional[str] = None      # TR-local 'YYYY-MM-DDTHH:MM'
    owner_user_id:     Optional[int] = None
    quotation_id:      Optional[int] = None


class DealMove(BaseModel):
    stage_id:   int
    sort_order: Optional[int] = 0


class DealClose(BaseModel):
    result:      str = Field(..., pattern="^(won|lost)$")
    lost_reason: Optional[str] = None


class ActivityIn(BaseModel):
    type:       str = Field("note")
    subject:    Optional[str] = Field(None, max_length=200)
    body:       Optional[str] = None
    company_id: Optional[int] = None
    contact_id: Optional[int] = None
    deal_id:    Optional[int] = None


class TaskIn(BaseModel):
    title:               str = Field(..., min_length=1, max_length=200)
    notes:               Optional[str] = None
    due_at:              Optional[str] = None    # TR-local
    company_id:          Optional[int] = None
    contact_id:          Optional[int] = None
    deal_id:             Optional[int] = None
    assigned_to_user_id: Optional[int] = None


# ─── Yardımcı uçlar ──────────────────────────────────────────────────────────

@router.get("/users")
def list_users(db: Session = Depends(get_db), _: dict = Depends(require_permission("crm", "view"))):
    """Sahip/atanan seçimleri için SADECE CRM erişimi olan aktif kullanıcılar.
    Lab/IMS'te olup CRM yetkisi olmayanlar listeye düşmez — atama/sorumlu
    yalnızca CRM kullanıcılarına yapılır."""
    rows = db.query(User).filter(User.is_active == True).order_by(User.full_name).all()  # noqa: E712
    return [{"id": u.id, "full_name": u.full_name, "username": u.username}
            for u in rows if _has_permission(u, "crm", "view")]


@router.get("/stages")
def list_stages(db: Session = Depends(get_db), _: dict = Depends(require_permission("crm", "view"))):
    rows = (db.query(CrmStage).filter(CrmStage.is_active == True)  # noqa: E712
            .order_by(CrmStage.sort_order, CrmStage.id).all())
    return [C.serialize_stage(s) for s in rows]


# ─── Firmalar ────────────────────────────────────────────────────────────────

@router.get("/companies")
def list_companies(
    q: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    include_inactive: bool = Query(False),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("crm", "view")),
):
    query = db.query(CrmCompany)
    if not include_inactive:
        query = query.filter(CrmCompany.is_active == True)  # noqa: E712
    query = _source_filter(query, CrmCompany, source)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(
            CrmCompany.name.ilike(like), CrmCompany.email.ilike(like),
            CrmCompany.phone.ilike(like), CrmCompany.city.ilike(like),
        ))
    companies = query.order_by(CrmCompany.name).all()
    ids = [c.id for c in companies]
    # contact + open-deal sayıları (toplu, N+1 yok)
    contact_counts, deal_counts = {}, {}
    if ids:
        for cid, n in (db.query(CrmContact.company_id, func.count(CrmContact.id))
                       .filter(CrmContact.company_id.in_(ids), CrmContact.is_active == True)  # noqa: E712
                       .group_by(CrmContact.company_id).all()):
            contact_counts[cid] = n
        for cid, n in (db.query(CrmDeal.company_id, func.count(CrmDeal.id))
                       .filter(CrmDeal.company_id.in_(ids), CrmDeal.status == "open")
                       .group_by(CrmDeal.company_id).all()):
            deal_counts[cid] = n
    return [C.serialize_company(c, contact_count=contact_counts.get(c.id, 0),
                                open_deal_count=deal_counts.get(c.id, 0)) for c in companies]


@router.post("/companies", status_code=201)
def create_company(
    data: CompanyIn, request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("crm", "create")),
):
    c = CrmCompany(
        name=data.name.strip(), sector=data.sector, website=data.website,
        phone=data.phone, email=data.email, address=data.address,
        city=data.city, country=data.country, tax_office=data.tax_office,
        tax_no=data.tax_no, notes=data.notes,
        owner_user_id=data.owner_user_id, owner_name=_user_name(db, data.owner_user_id),
        source="manual",
        created_by=_actor(current_user),
    )
    db.add(c); db.commit(); db.refresh(c)
    log_admin_event(db, request, actor=current_user, action="crm.company.create",
                    target_type="crm_company", target_id=c.id, target_name=c.name)
    return C.serialize_company(c)


@router.get("/companies/{cid}")
def company_detail(cid: int, db: Session = Depends(get_db),
                   _: dict = Depends(require_permission("crm", "view"))):
    c = db.query(CrmCompany).filter(CrmCompany.id == cid).first()
    if not c:
        return JSONResponse(status_code=404, content={"detail": "Firma bulunamadı."})
    contacts = (db.query(CrmContact).filter(CrmContact.company_id == cid, CrmContact.is_active == True)  # noqa: E712
                .order_by(CrmContact.full_name).all())
    deals = (db.query(CrmDeal).filter(CrmDeal.company_id == cid)
             .order_by(CrmDeal.status, CrmDeal.created_at.desc()).all())
    stage_names = {s.id: s.name for s in db.query(CrmStage).all()}
    activities = (db.query(CrmActivity).filter(CrmActivity.company_id == cid)
                  .order_by(CrmActivity.is_pinned.desc(), CrmActivity.created_at.desc()).limit(100).all())
    tasks = (db.query(CrmTask).filter(CrmTask.company_id == cid)
             .order_by(CrmTask.status, CrmTask.due_at).all())
    now = _now()
    return {
        "company": C.serialize_company(c),
        "contacts": [C.serialize_contact(ct, company_name=c.name) for ct in contacts],
        "deals": [C.serialize_deal(d, company_name=c.name, stage_name=stage_names.get(d.stage_id, "")) for d in deals],
        "activities": [C.serialize_activity(a) for a in activities],
        "tasks": [C.serialize_task(t, overdue=(t.status == "open" and t.due_at and t.due_at < now)) for t in tasks],
    }


@router.put("/companies/{cid}")
def update_company(cid: int, data: CompanyIn, request: Request,
                   db: Session = Depends(get_db),
                   current_user: dict = Depends(require_permission("crm", "edit"))):
    c = db.query(CrmCompany).filter(CrmCompany.id == cid).first()
    if not c:
        return JSONResponse(status_code=404, content={"detail": "Firma bulunamadı."})
    c.name = data.name.strip(); c.sector = data.sector; c.website = data.website
    c.phone = data.phone; c.email = data.email; c.address = data.address
    c.city = data.city; c.country = data.country; c.tax_office = data.tax_office
    c.tax_no = data.tax_no; c.notes = data.notes
    c.owner_user_id = data.owner_user_id; c.owner_name = _user_name(db, data.owner_user_id)
    db.commit(); db.refresh(c)
    log_admin_event(db, request, actor=current_user, action="crm.company.update",
                    target_type="crm_company", target_id=c.id, target_name=c.name)
    return C.serialize_company(c)


@router.delete("/companies/{cid}")
def delete_company(cid: int, request: Request, db: Session = Depends(get_db),
                   current_user: dict = Depends(require_permission("crm", "delete"))):
    c = db.query(CrmCompany).filter(CrmCompany.id == cid).first()
    if not c:
        return JSONResponse(status_code=404, content={"detail": "Firma bulunamadı."})
    c.is_active = False   # soft-delete
    db.commit()
    log_admin_event(db, request, actor=current_user, action="crm.company.delete",
                    target_type="crm_company", target_id=c.id, target_name=c.name)
    return {"message": "Firma arşivlendi."}


# ─── Kişiler ─────────────────────────────────────────────────────────────────

@router.get("/contacts")
def list_contacts(
    q: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    company_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("crm", "view")),
):
    query = db.query(CrmContact).filter(CrmContact.is_active == True)  # noqa: E712
    query = _source_filter(query, CrmContact, source)
    if company_id:
        query = query.filter(CrmContact.company_id == company_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(
            CrmContact.full_name.ilike(like), CrmContact.email.ilike(like),
            CrmContact.phone.ilike(like), CrmContact.mobile.ilike(like),
            CrmContact.whatsapp_number.ilike(like),
        ))
    contacts = query.order_by(CrmContact.full_name).all()
    names = {c.id: c.name for c in db.query(CrmCompany).all()}
    return [C.serialize_contact(ct, company_name=names.get(ct.company_id, "")) for ct in contacts]


@router.post("/contacts", status_code=201)
def create_contact(data: ContactIn, request: Request, db: Session = Depends(get_db),
                   current_user: dict = Depends(require_permission("crm", "create"))):
    if data.company_id and not db.query(CrmCompany.id).filter(CrmCompany.id == data.company_id).first():
        return JSONResponse(status_code=400, content={"detail": "Geçersiz firma."})
    ct = CrmContact(
        full_name=data.full_name.strip(), company_id=data.company_id,
        title=data.title, phone=data.phone, mobile=data.mobile, email=data.email,
        whatsapp_number=data.whatsapp_number, source=data.source or "manual", notes=data.notes,
        owner_user_id=data.owner_user_id, owner_name=_user_name(db, data.owner_user_id),
        created_by=_actor(current_user),
    )
    db.add(ct); db.commit(); db.refresh(ct)
    log_admin_event(db, request, actor=current_user, action="crm.contact.create",
                    target_type="crm_contact", target_id=ct.id, target_name=ct.full_name)
    cname = db.query(CrmCompany.name).filter(CrmCompany.id == ct.company_id).scalar() if ct.company_id else ""
    return C.serialize_contact(ct, company_name=cname or "")


@router.get("/contacts/{cid}")
def contact_detail(cid: int, db: Session = Depends(get_db),
                   _: dict = Depends(require_permission("crm", "view"))):
    ct = db.query(CrmContact).filter(CrmContact.id == cid).first()
    if not ct:
        return JSONResponse(status_code=404, content={"detail": "Kişi bulunamadı."})
    cname = db.query(CrmCompany.name).filter(CrmCompany.id == ct.company_id).scalar() if ct.company_id else ""
    activities = (db.query(CrmActivity).filter(CrmActivity.contact_id == cid)
                  .order_by(CrmActivity.is_pinned.desc(), CrmActivity.created_at.desc()).limit(100).all())
    tasks = (db.query(CrmTask).filter(CrmTask.contact_id == cid)
             .order_by(CrmTask.status, CrmTask.due_at).all())
    now = _now()
    return {
        "contact": C.serialize_contact(ct, company_name=cname or ""),
        "activities": [C.serialize_activity(a) for a in activities],
        "tasks": [C.serialize_task(t, overdue=(t.status == "open" and t.due_at and t.due_at < now)) for t in tasks],
    }


@router.put("/contacts/{cid}")
def update_contact(cid: int, data: ContactIn, request: Request, db: Session = Depends(get_db),
                   current_user: dict = Depends(require_permission("crm", "edit"))):
    ct = db.query(CrmContact).filter(CrmContact.id == cid).first()
    if not ct:
        return JSONResponse(status_code=404, content={"detail": "Kişi bulunamadı."})
    ct.full_name = data.full_name.strip(); ct.company_id = data.company_id
    ct.title = data.title; ct.phone = data.phone; ct.mobile = data.mobile
    ct.email = data.email; ct.whatsapp_number = data.whatsapp_number
    ct.source = data.source; ct.notes = data.notes
    ct.owner_user_id = data.owner_user_id; ct.owner_name = _user_name(db, data.owner_user_id)
    db.commit(); db.refresh(ct)
    log_admin_event(db, request, actor=current_user, action="crm.contact.update",
                    target_type="crm_contact", target_id=ct.id, target_name=ct.full_name)
    cname = db.query(CrmCompany.name).filter(CrmCompany.id == ct.company_id).scalar() if ct.company_id else ""
    return C.serialize_contact(ct, company_name=cname or "")


@router.delete("/contacts/{cid}")
def delete_contact(cid: int, request: Request, db: Session = Depends(get_db),
                   current_user: dict = Depends(require_permission("crm", "delete"))):
    ct = db.query(CrmContact).filter(CrmContact.id == cid).first()
    if not ct:
        return JSONResponse(status_code=404, content={"detail": "Kişi bulunamadı."})
    ct.is_active = False
    db.commit()
    log_admin_event(db, request, actor=current_user, action="crm.contact.delete",
                    target_type="crm_contact", target_id=ct.id, target_name=ct.full_name)
    return {"message": "Kişi arşivlendi."}


# ─── Fırsatlar / Pipeline ────────────────────────────────────────────────────

def _deal_names(db: Session):
    """Toplu ad çözümü — şirket/kişi/aşama adları (Kanban serileştirme için)."""
    companies = {c.id: c.name for c in db.query(CrmCompany.id, CrmCompany.name).all()}
    contacts = {c.id: c.full_name for c in db.query(CrmContact.id, CrmContact.full_name).all()}
    stages = {s.id: s.name for s in db.query(CrmStage.id, CrmStage.name).all()}
    return companies, contacts, stages


@router.get("/pipeline")
def pipeline(source: Optional[str] = Query(None),
             db: Session = Depends(get_db),
             _: dict = Depends(require_permission("crm", "view"))):
    """Kanban verisi — aşamalar + her aşamadaki açık fırsatlar + aşama toplamları."""
    stages = (db.query(CrmStage).filter(CrmStage.is_active == True)  # noqa: E712
              .order_by(CrmStage.sort_order, CrmStage.id).all())
    dq = _source_filter(db.query(CrmDeal).filter(CrmDeal.status == "open"), CrmDeal, source)
    deals = dq.order_by(CrmDeal.sort_order, CrmDeal.created_at.desc()).all()
    companies, contacts, stage_names = _deal_names(db)
    by_stage, totals = {}, {}
    for s in stages:
        by_stage[s.id] = []; totals[s.id] = 0.0
    for d in deals:
        row = C.serialize_deal(d, company_name=companies.get(d.company_id, ""),
                               contact_name=contacts.get(d.contact_id, ""),
                               stage_name=stage_names.get(d.stage_id, ""))
        by_stage.setdefault(d.stage_id, []).append(row)
        totals[d.stage_id] = totals.get(d.stage_id, 0.0) + (d.value or 0.0)
    return {
        "stages": [C.serialize_stage(s) for s in stages],
        "deals_by_stage": by_stage,
        "totals": totals,
    }


@router.get("/deals")
def list_deals(
    status: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("crm", "view")),
):
    query = db.query(CrmDeal)
    if status in ("open", "won", "lost"):
        query = query.filter(CrmDeal.status == status)
    query = _source_filter(query, CrmDeal, source)
    deals = query.order_by(CrmDeal.created_at.desc()).all()
    companies, contacts, stage_names = _deal_names(db)
    return [C.serialize_deal(d, company_name=companies.get(d.company_id, ""),
                             contact_name=contacts.get(d.contact_id, ""),
                             stage_name=stage_names.get(d.stage_id, "")) for d in deals]


@router.post("/deals", status_code=201)
def create_deal(data: DealIn, request: Request, db: Session = Depends(get_db),
                current_user: dict = Depends(require_permission("crm", "create"))):
    # aşama verilmemişse ilk (en düşük sort_order) aşamayı kullan
    stage_id = data.stage_id
    if not stage_id:
        first = (db.query(CrmStage).filter(CrmStage.is_active == True)  # noqa: E712
                 .order_by(CrmStage.sort_order, CrmStage.id).first())
        stage_id = first.id if first else None
    d = CrmDeal(
        title=data.title.strip(), company_id=data.company_id, contact_id=data.contact_id,
        stage_id=stage_id, value=data.value or 0.0, currency=(data.currency or "TRY")[:3],
        probability=data.probability or 0,
        expected_close_at=C.parse_tr_to_utc(data.expected_close_at),
        owner_user_id=data.owner_user_id, owner_name=_user_name(db, data.owner_user_id),
        quotation_id=data.quotation_id, source="manual", created_by=_actor(current_user),
    )
    db.add(d); db.commit(); db.refresh(d)
    log_admin_event(db, request, actor=current_user, action="crm.deal.create",
                    target_type="crm_deal", target_id=d.id, target_name=d.title)
    companies, contacts, stage_names = _deal_names(db)
    return C.serialize_deal(d, company_name=companies.get(d.company_id, ""),
                            contact_name=contacts.get(d.contact_id, ""),
                            stage_name=stage_names.get(d.stage_id, ""))


@router.get("/deals/{did}")
def deal_detail(did: int, db: Session = Depends(get_db),
                _: dict = Depends(require_permission("crm", "view"))):
    d = db.query(CrmDeal).filter(CrmDeal.id == did).first()
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Fırsat bulunamadı."})
    companies, contacts, stage_names = _deal_names(db)
    activities = (db.query(CrmActivity).filter(CrmActivity.deal_id == did)
                  .order_by(CrmActivity.is_pinned.desc(), CrmActivity.created_at.desc()).limit(100).all())
    tasks = (db.query(CrmTask).filter(CrmTask.deal_id == did)
             .order_by(CrmTask.status, CrmTask.due_at).all())
    now = _now()
    return {
        "deal": C.serialize_deal(d, company_name=companies.get(d.company_id, ""),
                                 contact_name=contacts.get(d.contact_id, ""),
                                 stage_name=stage_names.get(d.stage_id, "")),
        "activities": [C.serialize_activity(a) for a in activities],
        "tasks": [C.serialize_task(t, overdue=(t.status == "open" and t.due_at and t.due_at < now)) for t in tasks],
    }


@router.put("/deals/{did}")
def update_deal(did: int, data: DealIn, request: Request, db: Session = Depends(get_db),
                current_user: dict = Depends(require_permission("crm", "edit"))):
    d = db.query(CrmDeal).filter(CrmDeal.id == did).first()
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Fırsat bulunamadı."})
    d.title = data.title.strip(); d.company_id = data.company_id; d.contact_id = data.contact_id
    if data.stage_id:
        d.stage_id = data.stage_id
    d.value = data.value or 0.0; d.currency = (data.currency or "TRY")[:3]
    d.probability = data.probability or 0
    d.expected_close_at = C.parse_tr_to_utc(data.expected_close_at)
    d.owner_user_id = data.owner_user_id; d.owner_name = _user_name(db, data.owner_user_id)
    d.quotation_id = data.quotation_id
    db.commit(); db.refresh(d)
    log_admin_event(db, request, actor=current_user, action="crm.deal.update",
                    target_type="crm_deal", target_id=d.id, target_name=d.title)
    companies, contacts, stage_names = _deal_names(db)
    return C.serialize_deal(d, company_name=companies.get(d.company_id, ""),
                            contact_name=contacts.get(d.contact_id, ""),
                            stage_name=stage_names.get(d.stage_id, ""))


@router.post("/deals/{did}/move")
def move_deal(did: int, data: DealMove, db: Session = Depends(get_db),
              _: dict = Depends(require_permission("crm", "edit"))):
    """Kanban sürükle-bırak — aşama + sıra güncelle.  Hedef aşama kazanıldı/
    kaybedildi ise fırsatın durumunu da otomatik kapatır."""
    d = db.query(CrmDeal).filter(CrmDeal.id == did).first()
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Fırsat bulunamadı."})
    stage = db.query(CrmStage).filter(CrmStage.id == data.stage_id).first()
    if not stage:
        return JSONResponse(status_code=400, content={"detail": "Geçersiz aşama."})
    d.stage_id = stage.id
    d.sort_order = data.sort_order or 0
    if stage.is_won:
        d.status = "won"; d.won_at = d.won_at or _now(); d.closed_at = _now()
    elif stage.is_lost:
        d.status = "lost"; d.closed_at = _now()
    else:
        d.status = "open"; d.won_at = None; d.closed_at = None
    db.commit()
    return {"message": "Taşındı.", "status": d.status}


@router.post("/deals/{did}/close")
def close_deal(did: int, data: DealClose, request: Request, db: Session = Depends(get_db),
               current_user: dict = Depends(require_permission("crm", "edit"))):
    d = db.query(CrmDeal).filter(CrmDeal.id == did).first()
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Fırsat bulunamadı."})
    now = _now()
    if data.result == "won":
        d.status = "won"; d.won_at = now; d.closed_at = now; d.probability = 100
        won = db.query(CrmStage).filter(CrmStage.is_won == True).order_by(CrmStage.sort_order).first()  # noqa: E712
        if won:
            d.stage_id = won.id
    else:
        d.status = "lost"; d.closed_at = now; d.lost_reason = data.lost_reason
        lost = db.query(CrmStage).filter(CrmStage.is_lost == True).order_by(CrmStage.sort_order).first()  # noqa: E712
        if lost:
            d.stage_id = lost.id
    db.commit()
    log_admin_event(db, request, actor=current_user, action=f"crm.deal.{data.result}",
                    target_type="crm_deal", target_id=d.id, target_name=d.title)
    return {"message": "Kazanıldı." if data.result == "won" else "Kaybedildi.", "status": d.status}


@router.delete("/deals/{did}")
def delete_deal(did: int, request: Request, db: Session = Depends(get_db),
                current_user: dict = Depends(require_permission("crm", "delete"))):
    d = db.query(CrmDeal).filter(CrmDeal.id == did).first()
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Fırsat bulunamadı."})
    title = d.title
    db.delete(d)   # aktiviteler/görevler CASCADE/SET NULL
    db.commit()
    log_admin_event(db, request, actor=current_user, action="crm.deal.delete",
                    target_type="crm_deal", target_id=did, target_name=title)
    return {"message": "Fırsat silindi."}


# ─── Aktiviteler (paylaşımlı zaman çizelgesi) ────────────────────────────────

@router.get("/activities")
def list_activities(
    company_id: Optional[int] = Query(None),
    contact_id: Optional[int] = Query(None),
    deal_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("crm", "view")),
):
    query = db.query(CrmActivity)
    if company_id:
        query = query.filter(CrmActivity.company_id == company_id)
    if contact_id:
        query = query.filter(CrmActivity.contact_id == contact_id)
    if deal_id:
        query = query.filter(CrmActivity.deal_id == deal_id)
    rows = (query.order_by(CrmActivity.is_pinned.desc(), CrmActivity.created_at.desc())
            .limit(limit).all())
    return [C.serialize_activity(a) for a in rows]


@router.post("/activities", status_code=201)
def create_activity(data: ActivityIn, db: Session = Depends(get_db),
                    current_user: dict = Depends(require_permission("crm", "create"))):
    if not (data.company_id or data.contact_id or data.deal_id):
        return JSONResponse(status_code=400, content={"detail": "Bir firma, kişi veya fırsat seçilmeli."})
    a = CrmActivity(
        type=data.type if data.type in _ACTIVITY_TYPES else "note",
        subject=data.subject, body=data.body,
        company_id=data.company_id, contact_id=data.contact_id, deal_id=data.deal_id,
        author_user_id=int(current_user.get("sub", 0)) or None,
        author_name=_actor(current_user),
    )
    db.add(a); db.commit(); db.refresh(a)
    return C.serialize_activity(a)


@router.post("/activities/{aid}/pin")
def toggle_pin(aid: int, db: Session = Depends(get_db),
               _: dict = Depends(require_permission("crm", "edit"))):
    a = db.query(CrmActivity).filter(CrmActivity.id == aid).first()
    if not a:
        return JSONResponse(status_code=404, content={"detail": "Kayıt bulunamadı."})
    a.is_pinned = not a.is_pinned
    db.commit()
    return {"is_pinned": bool(a.is_pinned)}


@router.delete("/activities/{aid}")
def delete_activity(aid: int, db: Session = Depends(get_db),
                    _: dict = Depends(require_permission("crm", "delete"))):
    a = db.query(CrmActivity).filter(CrmActivity.id == aid).first()
    if not a:
        return JSONResponse(status_code=404, content={"detail": "Kayıt bulunamadı."})
    db.delete(a); db.commit()
    return {"message": "Silindi."}


# ─── Görevler / Hatırlatmalar ────────────────────────────────────────────────

@router.get("/tasks")
def list_tasks(
    scope: str = Query("all"),         # all | mine | today | overdue | open
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    payload: dict = Depends(require_permission("crm", "view")),
):
    query = db.query(CrmTask)
    now = _now()
    if status in ("open", "done"):
        query = query.filter(CrmTask.status == status)
    if scope == "mine":
        query = query.filter(CrmTask.assigned_to_user_id == int(payload.get("sub", 0)))
    elif scope == "open":
        query = query.filter(CrmTask.status == "open")
    elif scope == "overdue":
        query = query.filter(CrmTask.status == "open", CrmTask.due_at.isnot(None), CrmTask.due_at < now)
    elif scope == "today":
        query = query.filter(CrmTask.status == "open", CrmTask.due_at.isnot(None),
                             CrmTask.due_at <= _today_end_utc())
    rows = query.order_by(CrmTask.status, CrmTask.due_at.is_(None), CrmTask.due_at).all()
    return [C.serialize_task(t, overdue=(t.status == "open" and t.due_at and t.due_at < now)) for t in rows]


@router.post("/tasks", status_code=201)
def create_task(data: TaskIn, db: Session = Depends(get_db),
                current_user: dict = Depends(require_permission("crm", "create"))):
    assignee = data.assigned_to_user_id or int(current_user.get("sub", 0)) or None
    t = CrmTask(
        title=data.title.strip(), notes=data.notes,
        due_at=C.parse_tr_to_utc(data.due_at),
        company_id=data.company_id, contact_id=data.contact_id, deal_id=data.deal_id,
        assigned_to_user_id=assignee, assigned_to_name=_user_name(db, assignee),
        created_by=_actor(current_user),
    )
    db.add(t); db.commit(); db.refresh(t)
    return C.serialize_task(t)


@router.put("/tasks/{tid}")
def update_task(tid: int, data: TaskIn, db: Session = Depends(get_db),
                _: dict = Depends(require_permission("crm", "edit"))):
    t = db.query(CrmTask).filter(CrmTask.id == tid).first()
    if not t:
        return JSONResponse(status_code=404, content={"detail": "Görev bulunamadı."})
    t.title = data.title.strip(); t.notes = data.notes
    t.due_at = C.parse_tr_to_utc(data.due_at)
    t.company_id = data.company_id; t.contact_id = data.contact_id; t.deal_id = data.deal_id
    t.assigned_to_user_id = data.assigned_to_user_id
    t.assigned_to_name = _user_name(db, data.assigned_to_user_id)
    t.reminder_sent = False   # vade değişmiş olabilir → yeniden hatırlat
    db.commit(); db.refresh(t)
    return C.serialize_task(t)


@router.post("/tasks/{tid}/complete")
def complete_task(tid: int, db: Session = Depends(get_db),
                  _: dict = Depends(require_permission("crm", "edit"))):
    t = db.query(CrmTask).filter(CrmTask.id == tid).first()
    if not t:
        return JSONResponse(status_code=404, content={"detail": "Görev bulunamadı."})
    if t.status == "open":
        t.status = "done"; t.completed_at = _now()
    else:
        t.status = "open"; t.completed_at = None
    db.commit()
    return {"status": t.status}


@router.delete("/tasks/{tid}")
def delete_task(tid: int, db: Session = Depends(get_db),
                _: dict = Depends(require_permission("crm", "delete"))):
    t = db.query(CrmTask).filter(CrmTask.id == tid).first()
    if not t:
        return JSONResponse(status_code=404, content={"detail": "Görev bulunamadı."})
    db.delete(t); db.commit()
    return {"message": "Silindi."}


# ─── Pano (Dashboard) ────────────────────────────────────────────────────────

@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db),
              payload: dict = Depends(require_permission("crm", "view"))):
    now = _now()
    uid = int(payload.get("sub", 0))
    # Ay başı (UTC) — kazanılan fırsatlar bu aydan sayılır
    month_start = datetime(now.year, now.month, 1)

    companies_total = db.query(func.count(CrmCompany.id)).filter(CrmCompany.is_active == True).scalar() or 0  # noqa: E712
    contacts_total = db.query(func.count(CrmContact.id)).filter(CrmContact.is_active == True).scalar() or 0  # noqa: E712
    open_deals = db.query(func.count(CrmDeal.id)).filter(CrmDeal.status == "open").scalar() or 0
    open_value = db.query(func.coalesce(func.sum(CrmDeal.value), 0.0)).filter(CrmDeal.status == "open").scalar() or 0.0
    won_month = (db.query(func.count(CrmDeal.id))
                 .filter(CrmDeal.status == "won", CrmDeal.won_at >= month_start).scalar() or 0)
    won_month_value = (db.query(func.coalesce(func.sum(CrmDeal.value), 0.0))
                       .filter(CrmDeal.status == "won", CrmDeal.won_at >= month_start).scalar() or 0.0)

    # Aşamaya göre açık pipeline değeri
    stages = (db.query(CrmStage).filter(CrmStage.is_active == True)  # noqa: E712
              .order_by(CrmStage.sort_order, CrmStage.id).all())
    stage_value = dict(db.query(CrmDeal.stage_id, func.coalesce(func.sum(CrmDeal.value), 0.0))
                       .filter(CrmDeal.status == "open").group_by(CrmDeal.stage_id).all())
    stage_count = dict(db.query(CrmDeal.stage_id, func.count(CrmDeal.id))
                       .filter(CrmDeal.status == "open").group_by(CrmDeal.stage_id).all())
    pipeline_by_stage = [{
        "stage": s.name, "stage_id": s.id,
        "value": float(stage_value.get(s.id, 0.0)), "count": int(stage_count.get(s.id, 0)),
    } for s in stages if not (s.is_won or s.is_lost)]

    # Görevler — benim bugünküler/geciken + tüm geciken
    my_today = (db.query(CrmTask).filter(
        CrmTask.status == "open", CrmTask.assigned_to_user_id == uid,
        CrmTask.due_at.isnot(None), CrmTask.due_at <= _today_end_utc()
    ).order_by(CrmTask.due_at).all())
    overdue_all = (db.query(func.count(CrmTask.id)).filter(
        CrmTask.status == "open", CrmTask.due_at.isnot(None), CrmTask.due_at < now).scalar() or 0)

    recent = (db.query(CrmActivity).order_by(CrmActivity.created_at.desc()).limit(12).all())

    return {
        "counts": {
            "companies": companies_total, "contacts": contacts_total,
            "open_deals": open_deals, "open_value": float(open_value),
            "won_month": won_month, "won_month_value": float(won_month_value),
            "overdue_tasks": overdue_all,
        },
        "pipeline_by_stage": pipeline_by_stage,
        "my_tasks": [C.serialize_task(t, overdue=(t.due_at and t.due_at < now)) for t in my_today],
        "recent_activities": [C.serialize_activity(a) for a in recent],
    }
