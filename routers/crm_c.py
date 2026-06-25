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
from typing import Optional, List

from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import JSONResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from database import (
    get_db, CrmCompany, CrmContact, CrmDeal, CrmSavedView,
)
from core.permissions import require_permission
from core import crm as C
from core import crm_reports as R

router = APIRouter(prefix="/api/crm", tags=["crm-advanced"])


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
    deals = (db.query(CrmDeal).filter(CrmDeal.title.ilike(like))
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
