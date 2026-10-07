# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Tedarikçi satın alma durumu + tedarikçi başına fiyatlar — /api/suppliers/{id}/…

Lab (Songül Hanım, 07.10.2026) küçük miktarlı satış yapan firmaları
"bitirilecek — alma" işaretleyip yerine alınacak firmaları "tercih edilen"
yapar.  İş kuralları core/suppliers.py'de.  Tedarikçi kartı listesi / ekle /
düzenle / pasife al routers/inventory.py'de kalır (items.* yetkileri).

KURALLAR
  • Durum yazma `suppliers.status`; okuma items.view | reports.view.
  • Her sorgu aktif panelle sınırlı — başka panelin firması 404.
  • phase_out'ta sebep zorunlu (400); pasif firmanın durumu değişmez (409).
  • Durum değişikliği audit `supplier.status` (eski → yeni, sebep).
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import suppliers as SUP
from core.audit import log_admin_event
from core.domain import active_domain
from core.permissions import require_any_permission, require_permission
from database import Item, Supplier, SupplierPrice, get_db

router = APIRouter(prefix="/api/suppliers", tags=["suppliers"])


class StatusBody(BaseModel):
    status: str = Field(..., max_length=16)
    reason: Optional[str] = Field(None, max_length=SUP.REASON_MAX)


def _supplier(db: Session, supplier_id: int, domain: str) -> Optional[Supplier]:
    return (db.query(Supplier)
            .filter(Supplier.id == supplier_id, Supplier.domain == domain).first())


@router.put("/{supplier_id}/status")
def set_supplier_status(
    supplier_id: int,
    data: StatusBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("suppliers", "status")),
    domain: str = Depends(active_domain),
):
    """Firmanın satın alma durumunu yaz: normal | preferred | phase_out.
    Sebep phase_out'ta zorunlu, diğerlerinde isteğe bağlı (normal'e dönünce
    boş sebep eskisini temizler).  `status_by` / `status_at` damgalanır.
    Yanıt GET /api/suppliers satırıyla aynı şekil."""
    sup = _supplier(db, supplier_id, domain)
    if sup is None:
        return JSONResponse(status_code=404, content={"detail": "Tedarikçi bulunamadı."})
    if sup.is_active is False:
        return JSONResponse(status_code=409, content={
            "detail": "Pasif tedarikçinin durumu değiştirilemez — önce etkinleştirin."})
    status = SUP.normalize_status(data.status) if (data.status or "").strip() else None
    if status is None:
        return JSONResponse(status_code=400, content={
            "detail": "Durum normal, preferred ya da phase_out olmalı."})
    reason = (data.reason or "").strip() or None
    if status == "phase_out" and not reason:
        return JSONResponse(status_code=400, content={
            "detail": "“Bitirilecek — alma” için sebep yazın (ör. küçük miktar satıyor)."})
    old_status = SUP.normalize_status(sup.purchase_status) or "normal"
    old_reason = sup.status_reason
    if old_status == status and old_reason == reason:
        return SUP.serialize_supplier(sup)                       # değişiklik yok → damga yok
    actor = ((current_user or {}).get("full_name") or (current_user or {}).get("username") or "—")
    sup.purchase_status = status
    sup.status_reason = reason
    sup.status_by = actor[:100]
    sup.status_at = datetime.utcnow()          # naive UTC (DB kuralı)
    db.commit()
    db.refresh(sup)
    log_admin_event(db, request, actor=current_user, action="supplier.status",
                    target_type="supplier", target_id=sup.id, target_name=sup.name,
                    details={"eski": old_status, "yeni": status,
                             "eski_sebep": old_reason, "sebep": reason, "domain": domain})
    return SUP.serialize_supplier(sup)


@router.get("/{supplier_id}/prices")
def supplier_prices(
    supplier_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_any_permission(("items", "view"), ("reports", "view"))),
    domain: str = Depends(active_domain),
):
    """Bir firmanın fiyat satırları (Tedarikçiler → "Fiyatlar").

    `supplier_id`'ye bağlı satırlar + Excel'den gelip tedarikçi kartına
    BAĞLANAMAMIŞ (supplier_id NULL) ama firma anahtarı
    (`core.purchase_pricing.SupplierIndex`) aynı olan satırlar — "ULUDAG
    HERBAL" listesi "ULUDAĞ HERBAL" kartında görünsün.  Bağsız satırlar
    `matched: false` ile işaretlenir.  Pasif firmanın fiyatları da okunur."""
    from core.purchase_pricing import SupplierIndex
    from core.supplier_prices import normalize, serialize_price
    sup = _supplier(db, supplier_id, domain)
    if sup is None:
        return JSONResponse(status_code=404, content={"detail": "Tedarikçi bulunamadı."})
    rows = (db.query(SupplierPrice)
            .filter(SupplierPrice.domain == domain, SupplierPrice.supplier_id == sup.id).all())
    loose = (db.query(SupplierPrice)
             .filter(SupplierPrice.domain == domain, SupplierPrice.supplier_id.is_(None),
                     SupplierPrice.supplier_name.isnot(None)).all())
    if loose:
        names = [n for (n,) in db.query(Supplier.name).filter(Supplier.domain == domain).all()]
        ix = SupplierIndex(names + [r.supplier_name for r in loose])
        k = ix.key(sup.name)
        if k:
            rows += [r for r in loose if ix.key(r.supplier_name) == k]
    items = {}
    ids = {r.item_id for r in rows}
    if ids:
        items = {it.id: it for it in db.query(Item).filter(Item.id.in_(ids)).all()}
    st = SUP.normalize_status(sup.purchase_status) or "normal"
    out = [serialize_price(r, items.get(r.item_id), supplier_status=st) for r in rows]
    out.sort(key=lambda p: (normalize(p.get("material")), p["unit_price"] is None,
                            p["unit_price"] or 0.0))
    return {"supplier": SUP.serialize_supplier(sup), "prices": out, "total": len(out)}
