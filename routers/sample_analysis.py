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
tutar.

Bileşen satırları (`ingredients`): her satır hammadde + kaynak (numune lotu /
stok / henüz gelmedi) + miktar.  Kullanılan miktar KAYNAĞINDAN DÜŞÜLÜR — motor
core/sample_trial_stock.py (numune lotu: yalnız Inventory.quantity; stok:
stock_lots.consume).  PUT yalnız farkı uygular, DELETE hepsini iade eder.
Yetki bilinçli olarak yalnız `qc.approve` (inventory.adjust EK ŞART DEĞİL):
deneme miktarları küçük, her hareket performed_by'lı Transaction/audit bırakır;
admin'e gitme sürtünmesi 24.08.2026 kopya-kart kaosunu doğurmuştu.
"""
import io
import json
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, selectinload

from database import get_db, to_tr, Recipe, SampleAnalysis
from core.audit import log_admin_event
from core.domain import active_domain
from core.permissions import require_permission
from core.sample_questions import FORM_CODE, result_label, source_label, mode_label
from core.sample_trial_stock import TrialStockError, release_all, sync_ingredients

router = APIRouter(prefix="/api", tags=["sample-analysis"])

_RESULTS = ("uygun", "uygun_degil")
_MODES = ("existing", "new")


class PropertyRow(BaseModel):
    key: str = Field("custom", max_length=40)
    label: str = Field(..., min_length=1, max_length=100)
    spec: Optional[str] = Field(None, max_length=300)
    found: Optional[str] = Field(None, max_length=300)


class IngredientRow(BaseModel):
    row_id: Optional[int] = None                     # mevcut satır id (PUT eşlemesi)
    item_id: int
    source: str = Field("pending", max_length=10)    # sample | stock | pending
    inventory_id: Optional[int] = None               # yalnız source == sample
    quantity: Optional[float] = Field(None, ge=0)
    unit: Optional[str] = Field(None, max_length=20)
    note: Optional[str] = Field(None, max_length=300)


class SampleAnalysisCreate(BaseModel):
    bulk_name: str = Field(..., min_length=1, max_length=200)
    production_date: Optional[date] = None
    lot_number: Optional[str] = Field(None, max_length=100)
    recipe_id: Optional[int] = None
    mode: Optional[str] = None                        # existing | new | None → türet
    ingredients: List[IngredientRow] = Field(default_factory=list, max_length=100)
    formulation_notes: Optional[str] = Field(None, max_length=5000)
    properties: List[PropertyRow] = Field(..., min_length=1, max_length=30)
    analyst_name: Optional[str] = Field(None, max_length=100)
    result: Optional[str] = None                      # uygun | uygun_degil | None
    result_text: Optional[str] = Field(None, max_length=5000)
    notes: Optional[str] = Field(None, max_length=5000)
    approved_by: Optional[str] = Field(None, max_length=100)
    qa_representative: Optional[str] = Field(None, max_length=100)


def _view_mode(r: SampleAnalysis) -> str:
    return r.mode or ("existing" if r.recipe_id else "new")


def _view(r: SampleAnalysis) -> dict:
    try:
        props = json.loads(r.properties or "[]")
    except (TypeError, ValueError):
        props = []
    mode = _view_mode(r)
    return {
        "id":                r.id,
        "document_no":       r.document_no,
        "bulk_name":         r.bulk_name,
        "production_date":   r.production_date.strftime("%d.%m.%Y") if r.production_date else "",
        "lot_number":        r.lot_number or "",
        "recipe_id":         r.recipe_id,
        "recipe_name":       r.recipe_name or "",
        "mode":              mode,
        "mode_label":        mode_label(mode),
        "ingredients":       [{
            "row_id":        g.id,
            "item_id":       g.item_id,
            "item_name":     g.item_name,
            "unit":          g.unit or "",
            "source":        g.source,
            "source_label":  source_label(g.source),
            "inventory_id":  g.inventory_id,
            "lot_number":    g.lot_number or "",
            "supplier_name": g.supplier_name or "",
            "quantity":      round(float(g.quantity or 0), 6),
            "consumed_qty":  round(float(g.consumed_qty or 0), 6),
            "note":          g.note or "",
        } for g in (r.ingredients or [])],
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
            .options(selectinload(SampleAnalysis.ingredients))
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
    mode = (data.mode or "").strip().lower() or ("existing" if data.recipe_id else "new")
    if mode not in _MODES:
        return None, JSONResponse(status_code=400,
                                  content={"detail": f"Geçersiz çalışma türü: {data.mode} (existing/new)."})
    recipe_id = data.recipe_id if mode == "existing" else None   # yeni reçete → bağ yok
    if mode == "existing" and recipe_id is None:
        return None, JSONResponse(status_code=400,
                                  content={"detail": "Mevcut reçete çalışması için bir reçete seçin."})
    recipe_name = None
    if recipe_id is not None:
        rec = (db.query(Recipe)
               .filter(Recipe.id == recipe_id, Recipe.domain == domain,
                       Recipe.is_active == True).first())  # noqa: E712
        if not rec:
            return None, JSONResponse(status_code=404, content={"detail": "Reçete bulunamadı."})
        recipe_name = rec.name
    r.bulk_name = data.bulk_name.strip()
    r.production_date = data.production_date
    r.lot_number = (data.lot_number or "").strip() or None
    r.mode = mode
    r.recipe_id = recipe_id
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
            .options(selectinload(SampleAnalysis.ingredients))
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
        moves = sync_ingredients(db, r, data.ingredients, domain=domain, actor=actor)
        db.commit()
    except TrialStockError as e:
        db.rollback()
        return JSONResponse(status_code=e.status, content={"detail": e.detail})
    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Kayıt oluşturulamadı."})
    log_admin_event(db, request, actor=current_user, action="sample_analysis.create",
                    target_type="sample_analysis", target_id=r.id, target_name=r.document_no,
                    details={"bulk": r.bulk_name, "lot": r.lot_number, "mod": r.mode,
                             "sonuc": result_label(r.result), "satir": len(r.ingredients),
                             "dusum": moves["consumed"], "iade": moves["released"]})
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
    actor = current_user.get("full_name") or current_user.get("username") or "—"
    r, err = _apply(r, data, db, domain)
    if err:
        db.rollback()
        return err
    try:
        moves = sync_ingredients(db, r, data.ingredients, domain=domain, actor=actor)
        db.commit()
    except TrialStockError as e:
        db.rollback()          # alan değişiklikleri de geri alınır — tek işlem, hep ya hiç
        return JSONResponse(status_code=e.status, content={"detail": e.detail})
    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Kayıt güncellenemedi."})
    log_admin_event(db, request, actor=current_user, action="sample_analysis.update",
                    target_type="sample_analysis", target_id=r.id, target_name=r.document_no,
                    details={"bulk": r.bulk_name, "mod": r.mode, "sonuc": result_label(r.result),
                             "satir": len(r.ingredients),
                             "dusum": moves["consumed"], "iade": moves["released"]})
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
    actor = current_user.get("full_name") or current_user.get("username") or "—"
    try:
        released = release_all(db, r, actor=actor, reason="form silindi")
        r.is_active = False
        db.commit()
    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Form silinemedi."})
    log_admin_event(db, request, actor=current_user, action="sample_analysis.delete",
                    target_type="sample_analysis", target_id=r.id, target_name=r.document_no,
                    details={"bulk": r.bulk_name, "iade": released})
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
