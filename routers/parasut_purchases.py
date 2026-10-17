# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Paraşüt alış faturası fiyat onay kuyruğu — /api/supplier-prices/parasut-queue
(core/parasut_purchases.py).  Görüntüleme `reports.view` + aktif panel
(/fiyatlar sayfasıyla aynı kapı); onay / atlama `suppliers.prices`.
Paraşüt'e hiçbir şey yazılmaz; onay yalnız `supplier_prices`'a yazar.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from core import parasut_purchases as PP
from core.audit import log_admin_event
from core.domain import active_domain
from core.permissions import require_permission
from database import get_db

router = APIRouter(prefix="/api/supplier-prices/parasut-queue", tags=["supplier-prices"])


def _actor(current_user: dict) -> str:
    return ((current_user or {}).get("full_name") or (current_user or {}).get("username") or "—")[:100]


def _busy(db: Session, e: OperationalError) -> JSONResponse:
    """Kilit çakışması (kilitlenme / kilit alınamadı) → geri al, 409 `busy`;
    başka veritabanı hatası olduğu gibi yükselir."""
    db.rollback()
    if not PP.is_lock_busy(e):
        raise e
    return JSONResponse(status_code=409, content={"detail": PP.BUSY_TEXT, "code": "busy"})


class ApproveBody(BaseModel):
    item_id: int
    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = Field(None, max_length=150)
    package_qty: float
    price_unit: str = Field(..., max_length=8)


class SkipBody(BaseModel):
    reason: Optional[str] = Field(None, max_length=255)


@router.get("")
def parasut_queue(
    limit: int = Query(PP.QUEUE_LIMIT, ge=1, le=500),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    """Bekleyen fatura satırları (gider ipucu olmayanlar önce, yeni fatura
    önce) + kart önerisi / adaylar (aktif panel), IMS firma eşleşmesi,
    hatırlanan ambalaj, net önizleme; `count` toplam bekleyen."""
    return PP.queue_view(db, domain, limit)


@router.post("/{line_id}/approve")
def parasut_queue_approve(
    line_id: int,
    body: ApproveBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("suppliers", "prices")),
    domain: str = Depends(active_domain),
):
    """Satırı karta + firmaya + ambalaja bağlayıp `supplier_prices`'a yazar
    (`source='parasut'`).  409 `manual_exists` (aynı firmanın elle fiyatı) /
    `not_pending` / `busy` (kilit çakışması — eşzamanlı toplu onay; tekrar
    denenir); 404 kart / tedarikçi / satır; 400 doğrulama."""
    try:
        res = PP.approve_line(db, line_id, item_id=body.item_id, supplier_id=body.supplier_id,
                              supplier_name=body.supplier_name, package_qty=body.package_qty,
                              price_unit=body.price_unit, domain=domain, actor=_actor(current_user))
        db.commit()
    except PP.ApproveError as e:
        db.rollback()
        return JSONResponse(status_code=e.http, content={"detail": str(e), "code": e.code})
    except OperationalError as e:
        return _busy(db, e)
    log_admin_event(db, request, actor=current_user, action="supplier_prices.parasut_approve",
                    target_type="supplier_price", target_id=res["supplier_price_id"],
                    target_name=f"{res['audit']['item']} — {res['audit']['supplier_name']}"[:150],
                    details=res["audit"])
    return {"message": ("Mevcut daha yeni fatura fiyatı korundu." if res["kept_newer"] else "Fiyat yazıldı."),
            **{k: res[k] for k in ("line_id", "supplier_price_id", "kept_newer", "net_unit_price", "price")}}


@router.post("/{line_id}/skip")
def parasut_queue_skip(
    line_id: int,
    request: Request,
    body: Optional[SkipBody] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("suppliers", "prices")),
    domain: str = Depends(active_domain),
):
    """Satırı atlar (fiyat yazılmaz; gider kalemi / ilgisiz satır)."""
    try:
        res = PP.skip_line(db, line_id, actor=_actor(current_user), reason=(body.reason if body else None))
        db.commit()
    except PP.ApproveError as e:
        db.rollback()
        return JSONResponse(status_code=e.http, content={"detail": str(e), "code": e.code})
    except OperationalError as e:
        return _busy(db, e)
    log_admin_event(db, request, actor=current_user, action="supplier_prices.parasut_skip",
                    target_type="parasut_purchase_line", target_id=res["line_id"],
                    details={"domain": domain, "reason": (body.reason if body else None)})
    return {"message": "Satır atlandı.", **res}
