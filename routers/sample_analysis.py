# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Numune Analizi router — FR.KK.01 numune analiz formunun dijital yaşam döngüsü.

Liste/detay/PDF `qc.view`; oluştur/düzenle/sil `qc.approve` (form iki onay imzası
taşıyan bağlayıcı bir KK belgesi — imza yetkisi olan roller yazar, analiz yapan
kişi serbest metin olarak kaydedilir). Belge no: NA-YYYY-NNNNN. Soft-delete.
Reçete bağı opsiyonel; recipe_name snapshot'ı reçete silinse de belgeyi okunur
tutar. Items sekmesindeki 'numune' lotlarıyla (Inventory.is_sample) İLGİSİZ.
"""
import io
import json
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db, to_tr, Recipe, SampleAnalysis
from core.audit import log_admin_event
from core.domain import active_domain
from core.permissions import require_permission
from core.sample_questions import FORM_CODE, result_label

router = APIRouter(prefix="/api", tags=["sample-analysis"])

_RESULTS = ("uygun", "uygun_degil")


class PropertyRow(BaseModel):
    key: str = Field("custom", max_length=40)
    label: str = Field(..., min_length=1, max_length=100)
    spec: Optional[str] = Field(None, max_length=300)
    found: Optional[str] = Field(None, max_length=300)


class SampleAnalysisCreate(BaseModel):
    bulk_name: str = Field(..., min_length=1, max_length=200)
    production_date: Optional[date] = None
    lot_number: Optional[str] = Field(None, max_length=100)
    recipe_id: Optional[int] = None
    formulation_notes: Optional[str] = Field(None, max_length=5000)
    properties: List[PropertyRow] = Field(..., min_length=1, max_length=30)
    analyst_name: Optional[str] = Field(None, max_length=100)
    result: Optional[str] = None                      # uygun | uygun_degil | None
    result_text: Optional[str] = Field(None, max_length=5000)
    notes: Optional[str] = Field(None, max_length=5000)
    approved_by: Optional[str] = Field(None, max_length=100)
    qa_representative: Optional[str] = Field(None, max_length=100)


def _view(r: SampleAnalysis) -> dict:
    try:
        props = json.loads(r.properties or "[]")
    except (TypeError, ValueError):
        props = []
    return {
        "id":                r.id,
        "document_no":       r.document_no,
        "bulk_name":         r.bulk_name,
        "production_date":   r.production_date.strftime("%d.%m.%Y") if r.production_date else "",
        "lot_number":        r.lot_number or "",
        "recipe_id":         r.recipe_id,
        "recipe_name":       r.recipe_name or "",
        "formulation_notes": r.formulation_notes or "",
        "properties":        props,
        "analyst_name":      r.analyst_name or "",
        "result":            r.result,
        "result_label":      result_label(r.result),
        "result_text":       r.result_text or "",
        "notes":             r.notes or "",
        "approved_by":       r.approved_by or "",
        "qa_representative": r.qa_representative or "",
        "form_code":         r.form_code or FORM_CODE,
        "created_by":        r.created_by or "",
        "created_at":        to_tr(r.created_at).strftime("%d.%m.%Y %H:%M") if r.created_at else "",
    }


def _get_active(db: Session, analysis_id: int, domain: str) -> Optional[SampleAnalysis]:
    return (db.query(SampleAnalysis)
            .filter(SampleAnalysis.id == analysis_id,
                    SampleAnalysis.domain == domain,
                    SampleAnalysis.is_active == True)      # noqa: E712
            .first())


def _apply(r: SampleAnalysis, data: SampleAnalysisCreate, db: Session, domain: str):
    """Ortak create/update alan aktarımı. Hata → (None, JSONResponse)."""
    result = (data.result or "").strip().lower() or None
    if result is not None and result not in _RESULTS:
        return None, JSONResponse(status_code=400,
                                  content={"detail": f"Geçersiz sonuç: {data.result} (uygun/uygun_degil/boş)."})
    recipe_name = None
    if data.recipe_id is not None:
        rec = (db.query(Recipe)
               .filter(Recipe.id == data.recipe_id, Recipe.domain == domain,
                       Recipe.is_active == True).first())  # noqa: E712
        if not rec:
            return None, JSONResponse(status_code=404, content={"detail": "Reçete bulunamadı."})
        recipe_name = rec.name
    r.bulk_name = data.bulk_name.strip()
    r.production_date = data.production_date
    r.lot_number = (data.lot_number or "").strip() or None
    r.recipe_id = data.recipe_id
    r.recipe_name = recipe_name
    r.formulation_notes = (data.formulation_notes or "").strip() or None
    r.properties = json.dumps([p.model_dump() for p in data.properties], ensure_ascii=False)
    r.analyst_name = (data.analyst_name or "").strip() or None
    r.result = result
    r.result_text = (data.result_text or "").strip() or None
    r.notes = (data.notes or "").strip() or None
    r.approved_by = (data.approved_by or "").strip() or None
    r.qa_representative = (data.qa_representative or "").strip() or None
    return r, None


@router.get("/sample-analysis")
def list_sample_analyses(
    limit: int = 100,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("qc", "view")),
    domain: str = Depends(active_domain),
):
    limit = max(1, min(int(limit or 100), 500))
    rows = (db.query(SampleAnalysis)
            .filter(SampleAnalysis.domain == domain,
                    SampleAnalysis.is_active == True)      # noqa: E712
            .order_by(SampleAnalysis.id.desc())
            .limit(limit).all())
    return {"analyses": [_view(r) for r in rows], "count": len(rows)}


@router.post("/sample-analysis", status_code=201)
def create_sample_analysis(
    data: SampleAnalysisCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("qc", "approve")),
    domain: str = Depends(active_domain),
):
    actor = current_user.get("full_name") or current_user.get("username") or "—"
    r = SampleAnalysis(form_code=FORM_CODE, created_by=actor, domain=domain)
    r, err = _apply(r, data, db, domain)
    if err:
        return err
    try:
        db.add(r)
        db.flush()
        r.document_no = f"NA-{datetime.utcnow().year}-{r.id:05d}"
        db.commit()
    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Kayıt oluşturulamadı."})
    log_admin_event(db, request, actor=current_user, action="sample_analysis.create",
                    target_type="sample_analysis", target_id=r.id, target_name=r.document_no,
                    details={"bulk": r.bulk_name, "lot": r.lot_number,
                             "sonuc": result_label(r.result)})
    return {"id": r.id, "document_no": r.document_no, "message": "Numune analiz formu kaydedildi."}


@router.get("/sample-analysis/{analysis_id}")
def get_sample_analysis(
    analysis_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("qc", "view")),
    domain: str = Depends(active_domain),
):
    r = _get_active(db, analysis_id, domain)
    if not r:
        return JSONResponse(status_code=404, content={"detail": "Kayıt bulunamadı."})
    return _view(r)


@router.put("/sample-analysis/{analysis_id}")
def update_sample_analysis(
    analysis_id: int,
    data: SampleAnalysisCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("qc", "approve")),
    domain: str = Depends(active_domain),
):
    r = _get_active(db, analysis_id, domain)
    if not r:
        return JSONResponse(status_code=404, content={"detail": "Kayıt bulunamadı."})
    r, err = _apply(r, data, db, domain)
    if err:
        return err
    try:
        db.commit()
    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Kayıt güncellenemedi."})
    log_admin_event(db, request, actor=current_user, action="sample_analysis.update",
                    target_type="sample_analysis", target_id=r.id, target_name=r.document_no,
                    details={"bulk": r.bulk_name, "sonuc": result_label(r.result)})
    return {"id": r.id, "message": "Numune analiz formu güncellendi."}


@router.delete("/sample-analysis/{analysis_id}")
def delete_sample_analysis(
    analysis_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("qc", "approve")),
    domain: str = Depends(active_domain),
):
    r = _get_active(db, analysis_id, domain)
    if not r:
        return JSONResponse(status_code=404, content={"detail": "Kayıt bulunamadı."})
    r.is_active = False
    db.commit()
    log_admin_event(db, request, actor=current_user, action="sample_analysis.delete",
                    target_type="sample_analysis", target_id=r.id, target_name=r.document_no,
                    details={"bulk": r.bulk_name})
    return {"message": "Numune analiz formu silindi."}


@router.get("/sample-analysis/{analysis_id}/pdf")
def sample_analysis_pdf(
    analysis_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("qc", "view")),
    domain: str = Depends(active_domain),
):
    r = _get_active(db, analysis_id, domain)
    if not r:
        return JSONResponse(status_code=404, content={"detail": "Kayıt bulunamadı."})
    from core.delivery_note import content_disposition
    from core.sample_report import render_sample_pdf, sample_doc_filename
    try:
        content = render_sample_pdf(_view(r))
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "Belge üretilemedi."})
    fname = sample_doc_filename(r.document_no, r.lot_number or r.bulk_name)
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition(fname)},
    )
