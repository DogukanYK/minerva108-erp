# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Kodlu fason üretim router'ı — /api/outsourcing.

İş kuralları core/outsourcing.py'de; bu dosya yalnız yetki + panel + tek commit.
  • Her uç iç kullanıcıya açık (Distributor yetki verilse bile 403) ve aktif
    panelle (domain) sınırlı.
  • Motor commit ETMEZ; burada tek commit, hata hâlinde rollback ve
    `{"detail", "code"}` JSON'u döner (OutsourcingError.status).
  • Gerçek malzeme ↔ kod eşleştirmesi (kart adı, spec, kaynak lot) yalnız
    `outsourcing.mapping` yetkisiyle döner; dış belgeler yalnız kodları taşır.
  • Belge indirmesi de denetim kaydı yazar — dışarı neyin verildiği izlenir.
"""
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core import outsourcing as osrc
from core.delivery_note import content_disposition
from core.domain import active_domain
from core.outsourcing_documents import outsourcing_filename, render_packet_pdf
from core.permissions import _resolve_permissions, require_internal_user
from database import OutsourcingJob, User, get_db

router = APIRouter(prefix="/api/outsourcing", tags=["outsourcing"])


# ─── Gövdeler ────────────────────────────────────────────────────────────────

class PartnerBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    contact: Optional[str] = Field(None, max_length=2000)


class MaterialCodeBody(BaseModel):
    partner_id: int
    item_id: int
    specification: str = Field(..., min_length=3, max_length=4000)
    safety_instructions: str = Field("", max_length=4000)
    code: Optional[str] = Field(None, max_length=40)


class PreviewBody(BaseModel):
    partner_id: int
    recipe_id: int
    quantity: float
    label_language: str = Field("TR", max_length=8)


class JobBody(PreviewBody):
    external_product_name: str = Field(..., max_length=150)
    external_notes: str = Field(..., max_length=8000)
    approver_ids: Dict[str, int]
    material_code_ids: Dict[str, int] = Field(default_factory=dict)
    dispatch_quantities: Dict[str, float] = Field(default_factory=dict)


class JobUpdateBody(BaseModel):
    revision: int
    packet_hash: str = Field(..., max_length=64)
    quantity: Optional[float] = None
    label_language: Optional[str] = Field(None, max_length=8)
    external_product_name: Optional[str] = Field(None, max_length=150)
    external_notes: Optional[str] = Field(None, max_length=8000)
    material_code_ids: Optional[Dict[str, int]] = None
    dispatch_quantities: Optional[Dict[str, float]] = None


class ContainerBody(BaseModel):
    material_code_id: int
    quantity: float
    unit: Optional[str] = Field(None, max_length=20)
    inventory_id: Optional[int] = None


class ApproveBody(BaseModel):
    revision: int
    packet_hash: str = Field(..., max_length=64)


class DispatchBody(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=100)
    revision: int
    packet_hash: str = Field(..., max_length=64)
    container_ids: List[int] = Field(..., min_length=1, max_length=500)


class ConsumptionEntry(BaseModel):
    container_id: int
    unit: Optional[str] = Field(None, max_length=20)
    consumed_quantity: float = 0
    waste_quantity: float = 0
    reason: Optional[str] = Field(None, max_length=2000)


class ConsumptionBody(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=100)
    entries: List[ConsumptionEntry] = Field(..., min_length=1, max_length=500)


class ReturnEntry(BaseModel):
    container_id: int
    unit: Optional[str] = Field(None, max_length=20)
    quantity: float


class ReturnsBody(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=100)
    entries: List[ReturnEntry] = Field(..., min_length=1, max_length=500)


class ReceiptBody(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=100)
    quantity: float
    unit: Optional[str] = Field(None, max_length=20)
    sample_quantity: float = 0
    external_lot: str = Field(..., max_length=100)
    expiry_date: Optional[str] = Field(None, max_length=20)


class CloseBody(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=100)


class CancelBody(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=100)
    reason: str = Field(..., max_length=2000)


# ─── Yardımcılar ─────────────────────────────────────────────────────────────

def _perm(action):
    return require_internal_user(("outsourcing", action))


def _user_perms(db, actor) -> dict:
    user = db.query(User).filter(User.id == int(actor.get("sub", 0))).first()
    perms = _resolve_permissions(user) if user else {}
    return dict(perms.get("outsourcing") or {})


def _error(exc: "osrc.OutsourcingError"):
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail, "code": exc.code})


def _run(db, fn, *args):
    """Motoru çalıştır → tek commit; hata → rollback + anlamlı JSON."""
    try:
        result = fn(*args)
        db.commit()
        return result
    except osrc.OutsourcingError as exc:
        db.rollback()
        return _error(exc)
    except IntegrityError:
        db.rollback()
        return JSONResponse(status_code=409, content={
            "detail": "Aynı kayıt eşzamanlı oluşturuldu; sayfayı yenileyip yeniden deneyin.",
            "code": "concurrent_request"})


def _detail(db, job_or_response, actor):
    if isinstance(job_or_response, Response):
        return job_or_response
    db.refresh(job_or_response)
    return osrc.job_detail(db, job_or_response, user_id=int(actor.get("sub", 0)),
                           permissions=_user_perms(db, actor))


def _require(db, actor, action):
    if not _user_perms(db, actor).get(action):
        return JSONResponse(status_code=403, content={"detail": f"Yetersiz yetki: outsourcing.{action}",
                                                      "code": "forbidden"})
    return None


# ─── Okuma ───────────────────────────────────────────────────────────────────

@router.get("/bootstrap")
def get_bootstrap(db: Session = Depends(get_db), actor: dict = Depends(_perm("view")),
                  domain: str = Depends(active_domain)):
    return osrc.bootstrap(db, domain, _user_perms(db, actor))


@router.get("/jobs")
def get_jobs(db: Session = Depends(get_db), _: dict = Depends(_perm("view")),
             domain: str = Depends(active_domain)):
    return {"jobs": osrc.list_jobs(db, domain)}


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db), actor: dict = Depends(_perm("view")),
            domain: str = Depends(active_domain)):
    try:
        job = osrc._job(db, job_id, domain)
        return osrc.job_detail(db, job, user_id=int(actor.get("sub", 0)),
                               permissions=_user_perms(db, actor))
    except osrc.OutsourcingError as exc:
        return _error(exc)


@router.get("/jobs/{job_id}/documents/{document}")
def get_document(job_id: int, document: str, db: Session = Depends(get_db),
                 actor: dict = Depends(_perm("view")), domain: str = Depends(active_domain)):
    try:
        job = osrc._job(db, job_id, domain)
        packet = osrc.external_packet(db, job)
        pdf = render_packet_pdf(packet, document)
        filename = outsourcing_filename(packet, document)
    except osrc.OutsourcingError as exc:
        return _error(exc)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc), "code": "invalid_document"})
    osrc._audit(db, actor, "document", job.id, {"document": document, "revision": job.revision})
    db.commit()
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": content_disposition(filename),
        "Cache-Control": "no-store"})


# ─── Hazırlık ────────────────────────────────────────────────────────────────

@router.post("/partners")
def post_partner(body: PartnerBody, db: Session = Depends(get_db), actor: dict = Depends(_perm("manage")),
                 domain: str = Depends(active_domain)):
    row = _run(db, osrc.create_partner, db, domain, actor, body.model_dump())
    if isinstance(row, Response):
        return row
    return {"ok": True, "partner": {"id": row.id, "name": row.name, "contact": row.contact or ""}}


@router.post("/material-codes")
def post_material_code(body: MaterialCodeBody, db: Session = Depends(get_db),
                       actor: dict = Depends(_perm("mapping")), domain: str = Depends(active_domain)):
    row = _run(db, osrc.create_material_code, db, domain, actor, body.model_dump(exclude_none=True))
    if isinstance(row, Response):
        return row
    return {"ok": True, "material_code": {"id": row.id, "code": row.code, "partner_id": row.partner_id}}


@router.post("/preview")
def post_preview(body: PreviewBody, db: Session = Depends(get_db), actor: dict = Depends(_perm("manage")),
                 domain: str = Depends(active_domain)):
    try:
        result = osrc.preview(db, domain, body.model_dump())
    except osrc.OutsourcingError as exc:
        return _error(exc)
    finally:
        db.rollback()            # salt okuma — kilit/ara durum bırakma
    return osrc.public_preview(result, _user_perms(db, actor))


@router.post("/jobs")
def post_job(body: JobBody, db: Session = Depends(get_db), actor: dict = Depends(_perm("manage")),
             domain: str = Depends(active_domain)):
    return _detail(db, _run(db, osrc.create_job, db, domain, actor, body.model_dump()), actor)


@router.put("/jobs/{job_id}")
def put_job(job_id: int, body: JobUpdateBody, db: Session = Depends(get_db),
            actor: dict = Depends(_perm("manage")), domain: str = Depends(active_domain)):
    return _detail(db, _run(db, osrc.update_job, db, domain, actor, job_id,
                            body.model_dump(exclude_none=True)), actor)


@router.post("/jobs/{job_id}/containers")
def post_container(job_id: int, body: ContainerBody, db: Session = Depends(get_db),
                   actor: dict = Depends(_perm("manage")), domain: str = Depends(active_domain)):
    # Kaynak lot seçimi gerçek kartı/lotu görmeyi gerektirir → mapping de şart.
    denied = _require(db, actor, "mapping")
    if denied:
        return denied
    return _detail(db, _run(db, osrc.create_container, db, domain, actor, job_id,
                            body.model_dump(exclude_none=True)), actor)


@router.post("/jobs/{job_id}/containers/{container_id}/void")
def void_container(job_id: int, container_id: int, db: Session = Depends(get_db),
                   actor: dict = Depends(_perm("manage")), domain: str = Depends(active_domain)):
    denied = _require(db, actor, "mapping")
    if denied:
        return denied
    return _detail(db, _run(db, osrc.remove_container, db, domain, actor, job_id, container_id), actor)


# ─── Onay ve operasyon ───────────────────────────────────────────────────────

@router.post("/jobs/{job_id}/approve")
def post_approve(job_id: int, body: ApproveBody, db: Session = Depends(get_db),
                 actor: dict = Depends(_perm("approve")), domain: str = Depends(active_domain)):
    return _detail(db, _run(db, osrc.approve_job, db, domain, actor, job_id,
                            body.revision, body.packet_hash), actor)


@router.post("/jobs/{job_id}/dispatch")
def post_dispatch(job_id: int, body: DispatchBody, db: Session = Depends(get_db),
                  actor: dict = Depends(_perm("dispatch")), domain: str = Depends(active_domain)):
    return _detail(db, _run(db, osrc.dispatch, db, domain, actor, job_id, body.model_dump()), actor)


@router.post("/jobs/{job_id}/consumption")
def post_consumption(job_id: int, body: ConsumptionBody, db: Session = Depends(get_db),
                     actor: dict = Depends(_perm("record")), domain: str = Depends(active_domain)):
    return _detail(db, _run(db, osrc.record_consumption, db, domain, actor, job_id,
                            body.model_dump()), actor)


@router.post("/jobs/{job_id}/returns")
def post_returns(job_id: int, body: ReturnsBody, db: Session = Depends(get_db),
                 actor: dict = Depends(_perm("record")), domain: str = Depends(active_domain)):
    return _detail(db, _run(db, osrc.receive_returns, db, domain, actor, job_id, body.model_dump()), actor)


@router.post("/jobs/{job_id}/receipts")
def post_receipts(job_id: int, body: ReceiptBody, db: Session = Depends(get_db),
                  actor: dict = Depends(_perm("record")), domain: str = Depends(active_domain)):
    return _detail(db, _run(db, osrc.receive_finished, db, domain, actor, job_id, body.model_dump()), actor)


@router.post("/jobs/{job_id}/close")
def post_close(job_id: int, body: CloseBody, db: Session = Depends(get_db),
               actor: dict = Depends(_perm("record")), domain: str = Depends(active_domain)):
    return _detail(db, _run(db, osrc.close_job, db, domain, actor, job_id, body.model_dump()), actor)


@router.post("/jobs/{job_id}/cancel")
def post_cancel(job_id: int, body: CancelBody, db: Session = Depends(get_db),
                actor: dict = Depends(_perm("manage")), domain: str = Depends(active_domain)):
    return _detail(db, _run(db, osrc.cancel_job, db, domain, actor, job_id, body.model_dump()), actor)
