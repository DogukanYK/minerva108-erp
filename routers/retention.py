# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Şahit numune dolabı router — dolap stoğu, çıkış, imha, konum.

Dolap = MARKA (ayrı tablo yok; `RetentionSample.brand` kanonik marka adıdır).
Kayıtlar üretimde otomatik açılır (`routers/production.py`), buradan
yönetilir.

STOK KURALI — dar ve net:
  • Numune dolapta durduğu sürece `Item.current_stock` içinde SAYILIR
    (bugünkü davranış korunuyor; kullanıcı kararı).
  • Konum/not düzenlemesi stoğa DOKUNMAZ, Transaction YAZMAZ.
  • Çıkış ve imha ise gerçek bir tüketimdir: `Inventory` `-S` lotu düşer,
    `Item.current_stock` düşer ve bir `Transaction(Output)` yazılır — yoksa
    imha edilmiş ürün sonsuza dek stokta görünürdü.

Transaction tipi SADECE Output (core/snapshots.py yalnız Input/Output/
Adjustment tanır — başka tip yazmak aylık stok rekonstrüksiyonunu bozar).
"""
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import (get_db, to_tr, AppSetting, Inventory, Item,
                      RetentionSample, RetentionSampleMovement, Transaction)
from core.audit import log_admin_event
from core.brands import cabinet_of
from core.domain import active_domain
from core.permissions import require_permission
from core.retention import (CFG_EXTRA, CFG_SHELF_LIFE, CHECKOUT_REASONS,
                            DEFAULT_EXTRA_MONTHS, DEFAULT_SHELF_LIFE_MONTHS,
                            expiry_state, location_label, movement_label,
                            reason_label, retention_until, status_label)

router = APIRouter(prefix="/api", tags=["retention"])


# ─── Gövdeler ────────────────────────────────────────────────────────────────

class CheckoutBody(BaseModel):
    quantity: float = Field(..., gt=0)
    reason: str = Field("diger", max_length=40)
    note: Optional[str] = Field(None, max_length=500)


class DestroyBody(BaseModel):
    quantity: Optional[float] = Field(None, gt=0)      # boş = kalanın tamamı
    note: Optional[str] = Field(None, max_length=500)


class SampleUpdateBody(BaseModel):
    shelf: Optional[str] = Field(None, max_length=20)
    slot: Optional[str] = Field(None, max_length=20)
    retention_until: Optional[date] = None
    note: Optional[str] = Field(None, max_length=500)


class SampleCreateBody(BaseModel):
    item_id: int
    lot_number: str = Field(..., min_length=1, max_length=100)
    quantity: float = Field(..., gt=0)
    shelf: Optional[str] = Field(None, max_length=20)
    slot: Optional[str] = Field(None, max_length=20)
    produced_at: Optional[date] = None
    retention_until: Optional[date] = None
    note: Optional[str] = Field(None, max_length=500)


# ─── Yardımcılar ─────────────────────────────────────────────────────────────

def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": msg})


def _actor(current_user: dict) -> str:
    return (current_user or {}).get("username") or "sistem"


def _cfg_months(db: Session) -> tuple:
    """(raf ömrü, ek süre) — AppSetting'ten, bozuk/eksikse varsayılan."""
    def _read(key, default):
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        try:
            return int(float(row.value)) if row and row.value not in (None, "") else default
        except (TypeError, ValueError):
            return default
    return _read(CFG_SHELF_LIFE, DEFAULT_SHELF_LIFE_MONTHS), _read(CFG_EXTRA, DEFAULT_EXTRA_MONTHS)


def _view(r: RetentionSample) -> dict:
    st = expiry_state(r.retention_until)
    return {
        "id": r.id,
        "item_id": r.item_id,
        "item_name": r.item_name or (r.item.name if r.item else ""),
        "lot_number": r.lot_number,
        "brand": r.brand,
        "shelf": r.shelf or "",
        "slot": r.slot or "",
        "location_label": location_label(r.shelf, r.slot),
        "quantity": r.quantity,
        "initial_quantity": r.initial_quantity,
        "unit": r.unit or "adet",
        "produced_at": to_tr(r.produced_at).strftime("%d.%m.%Y") if r.produced_at else "",
        "retention_until": r.retention_until.isoformat() if r.retention_until else "",
        "retention_until_label": r.retention_until.strftime("%d.%m.%Y") if r.retention_until else "—",
        "expiry_state": st,
        "status": r.status,
        "status_label": status_label(r.status),
        "qc_status": r.qc_status or "",
        "source": r.source,
        "placed_by": r.placed_by or "",
        "note": r.note or "",
    }


def _movement_view(m: RetentionSampleMovement) -> dict:
    return {
        "id": m.id,
        "type": m.movement_type,
        "type_label": movement_label(m.movement_type),
        "quantity": m.quantity,
        "reason": m.reason or "",
        "reason_label": reason_label(m.reason) if m.reason else "",
        "note": m.note or "",
        "performed_by": m.performed_by or "",
        "created_at": to_tr(m.created_at).strftime("%d.%m.%Y %H:%M") if m.created_at else "",
    }


def _get(db: Session, sample_id: int, domain: str) -> Optional[RetentionSample]:
    return (db.query(RetentionSample)
            .filter(RetentionSample.id == sample_id,
                    RetentionSample.domain == domain).first())


def _consume(db: Session, sample: RetentionSample, qty: float, note: str,
             actor: str) -> None:
    """Dolaptan fiilen çıkan adedi stoktan da düş.

    Çağıran, miktar doğrulamasını YAPMIŞ olmalı.  Commit çağırana aittir —
    böylece hareket + stok tek işlemde yazılır (yarım kalma olmaz).
    """
    if sample.inventory_id:
        inv = (db.query(Inventory)
               .filter(Inventory.id == sample.inventory_id)
               .with_for_update().first())
        if inv:
            inv.quantity = max(0.0, (inv.quantity or 0.0) - qty)

    item = db.query(Item).filter(Item.id == sample.item_id).with_for_update().first()
    if item:
        item.current_stock = (item.current_stock or 0.0) - qty
        db.add(Transaction(
            item_id=item.id,
            lot_number=sample.lot_number,
            transaction_type="Output",          # izinli üç tipten biri
            quantity=qty,
            notes=note[:500],
            performed_by=actor,
        ))


# ─── Uçlar ───────────────────────────────────────────────────────────────────

@router.get("/retention/cabinets")
def list_cabinets(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("retention", "view")),
    domain: str = Depends(active_domain),
):
    """Marka bazlı dolap özeti — sekme çubuğu + KPI şeridi."""
    rows = (db.query(RetentionSample)
            .filter(RetentionSample.domain == domain,
                    RetentionSample.status == "stored").all())
    today = date.today()
    by_brand: dict = {}
    total = expired = due_soon = 0.0
    for r in rows:
        b = by_brand.setdefault(r.brand, {"brand": r.brand, "samples": 0,
                                          "quantity": 0.0, "expired": 0})
        b["samples"] += 1
        b["quantity"] += r.quantity or 0.0
        total += r.quantity or 0.0
        st = expiry_state(r.retention_until, today)
        if st == "expired":
            b["expired"] += 1
            expired += 1
        elif st == "due_soon":
            due_soon += 1
    return {
        "cabinets": sorted(by_brand.values(), key=lambda x: x["brand"]),
        "totals": {"quantity": total, "samples": len(rows),
                   "expired": int(expired), "due_soon": int(due_soon)},
        "reasons": [{"key": k, "label": reason_label(k)} for k in CHECKOUT_REASONS],
    }


@router.get("/retention/samples")
def list_samples(
    brand: Optional[str] = Query(None),
    q: Optional[str] = Query(None, max_length=100),
    status: Optional[str] = Query(None),
    expired: Optional[int] = Query(None),
    limit: int = Query(300, ge=1, le=1000),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("retention", "view")),
    domain: str = Depends(active_domain),
):
    query = db.query(RetentionSample).filter(RetentionSample.domain == domain)
    if brand:
        query = query.filter(RetentionSample.brand == brand)
    if status:
        query = query.filter(RetentionSample.status == status)
    else:
        query = query.filter(RetentionSample.status != "destroyed")
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(RetentionSample.item_name.ilike(like),
                                 RetentionSample.lot_number.ilike(like)))
    if expired:
        query = query.filter(RetentionSample.retention_until.isnot(None),
                             RetentionSample.retention_until < date.today(),
                             RetentionSample.status == "stored")
    rows = (query.order_by(RetentionSample.retention_until.asc().nullslast(),
                           RetentionSample.id.desc()).limit(limit).all())
    return {"rows": [_view(r) for r in rows], "count": len(rows)}


@router.get("/retention/samples/{sample_id}")
def get_sample(
    sample_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("retention", "view")),
    domain: str = Depends(active_domain),
):
    r = _get(db, sample_id, domain)
    if not r:
        return _err(404, "Numune kaydı bulunamadı.")
    out = _view(r)
    out["movements"] = [_movement_view(m) for m in r.movements]
    return out


@router.post("/retention/samples/{sample_id}/checkout")
def checkout_sample(
    sample_id: int,
    data: CheckoutBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("retention", "checkout")),
    domain: str = Depends(active_domain),
):
    """Dolaptan numune al — stoktan da düşer (fiilen tüketildi)."""
    r = _get(db, sample_id, domain)
    if not r:
        return _err(404, "Numune kaydı bulunamadı.")
    if r.status != "stored":
        return _err(400, f"Bu numune '{status_label(r.status)}' durumunda — çıkış yapılamaz.")
    qty = round(float(data.quantity), 4)
    if qty > (r.quantity or 0.0):
        return _err(400, f"Dolapta {r.quantity:g} adet var — {qty:g} adet çıkarılamaz.")
    if data.reason not in CHECKOUT_REASONS:
        return _err(400, "Geçersiz çıkış sebebi.")

    actor = _actor(current_user)
    note = f"Şahit numune çıkışı — {reason_label(data.reason)}"
    if data.note:
        note += f" | {data.note.strip()}"
    try:
        _consume(db, r, qty, f"{note} | Dolap: {r.brand}", actor)
        r.quantity = round((r.quantity or 0.0) - qty, 4)
        if r.quantity <= 0:
            r.quantity = 0.0
            r.status = "depleted"
        db.add(RetentionSampleMovement(
            sample_id=r.id, movement_type="cikis", quantity=qty,
            reason=data.reason, note=(data.note or "").strip() or None,
            performed_by=actor))
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Çıkış kaydedilemedi.")

    log_admin_event(db, request, actor=current_user, action="retention.checkout",
                    target_type="retention_sample", target_id=r.id,
                    target_name=r.lot_number,
                    details={"item": r.item_name, "quantity": qty,
                             "reason": data.reason, "brand": r.brand})
    out = _view(r)
    out["message"] = f"{qty:g} adet çıkış kaydedildi — stoktan da düşüldü."
    return out


@router.post("/retention/samples/{sample_id}/destroy")
def destroy_sample(
    sample_id: int,
    data: DestroyBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("retention", "destroy")),
    domain: str = Depends(active_domain),
):
    """Saklama süresi dolan numuneyi imha et — kalan adet stoktan düşer."""
    r = _get(db, sample_id, domain)
    if not r:
        return _err(404, "Numune kaydı bulunamadı.")
    if r.status != "stored":
        return _err(400, f"Bu numune '{status_label(r.status)}' durumunda — imha edilemez.")
    qty = round(float(data.quantity), 4) if data.quantity is not None else (r.quantity or 0.0)
    if qty <= 0:
        return _err(400, "İmha edilecek adet yok.")
    if qty > (r.quantity or 0.0):
        return _err(400, f"Dolapta {r.quantity:g} adet var — {qty:g} adet imha edilemez.")

    actor = _actor(current_user)
    note = "Şahit numune imhası"
    if data.note:
        note += f" | {data.note.strip()}"
    try:
        _consume(db, r, qty, f"{note} | Dolap: {r.brand}", actor)
        r.quantity = round((r.quantity or 0.0) - qty, 4)
        if r.quantity <= 0:
            r.quantity = 0.0
            r.status = "destroyed"
        db.add(RetentionSampleMovement(
            sample_id=r.id, movement_type="imha", quantity=qty,
            note=(data.note or "").strip() or None, performed_by=actor))
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "İmha kaydedilemedi.")

    log_admin_event(db, request, actor=current_user, action="retention.destroy",
                    target_type="retention_sample", target_id=r.id,
                    target_name=r.lot_number,
                    details={"item": r.item_name, "quantity": qty, "brand": r.brand})
    out = _view(r)
    out["message"] = f"{qty:g} adet imha edildi."
    return out


@router.put("/retention/samples/{sample_id}")
def update_sample(
    sample_id: int,
    data: SampleUpdateBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("retention", "edit")),
    domain: str = Depends(active_domain),
):
    """Raf/göz, saklama sonu, not — stoğa DOKUNMAZ, Transaction yazmaz."""
    r = _get(db, sample_id, domain)
    if not r:
        return _err(404, "Numune kaydı bulunamadı.")
    moved = False
    if data.shelf is not None or data.slot is not None:
        new_shelf = (data.shelf or "").strip() or None
        new_slot = (data.slot or "").strip() or None
        moved = (new_shelf != r.shelf) or (new_slot != r.slot)
        r.shelf, r.slot = new_shelf, new_slot
    if data.retention_until is not None:
        r.retention_until = data.retention_until
    if data.note is not None:
        r.note = data.note.strip() or None
    try:
        if moved:
            db.add(RetentionSampleMovement(
                sample_id=r.id, movement_type="konum", quantity=0,
                note=location_label(r.shelf, r.slot), performed_by=_actor(current_user)))
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Kayıt güncellenemedi.")

    log_admin_event(db, request, actor=current_user, action="retention.update",
                    target_type="retention_sample", target_id=r.id,
                    target_name=r.lot_number,
                    details={"shelf": r.shelf, "slot": r.slot})
    return _view(r)


@router.post("/retention/samples", status_code=201)
def create_sample(
    data: SampleCreateBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("retention", "edit")),
    domain: str = Depends(active_domain),
):
    """Elle dolap kaydı — geçmiş/kayıt dışı numuneler için.

    Stoğa DOKUNMAZ: adet zaten `Item.current_stock` içindedir (dolaptaki
    numuneler stokta sayılır).  Yalnız dolap görünürlüğü ekler.
    """
    item = (db.query(Item)
            .filter(Item.id == data.item_id, Item.domain == domain).first())
    if not item:
        return _err(404, "Ürün bulunamadı.")
    shelf_life, extra = _cfg_months(db)
    produced = (datetime.combine(data.produced_at, datetime.min.time())
                if data.produced_at else datetime.utcnow())
    r = RetentionSample(
        item_id=item.id, item_name=item.name,
        lot_number=data.lot_number.strip().upper(),
        brand=cabinet_of(item.name), shelf=(data.shelf or "").strip() or None,
        slot=(data.slot or "").strip() or None,
        quantity=data.quantity, initial_quantity=data.quantity, unit=item.unit,
        produced_at=produced,
        retention_until=data.retention_until or retention_until(produced, shelf_life, extra),
        status="stored", source="manual", placed_by=_actor(current_user),
        note=(data.note or "").strip() or None, domain=domain,
    )
    try:
        db.add(r)
        db.flush()
        db.add(RetentionSampleMovement(
            sample_id=r.id, movement_type="giris", quantity=data.quantity,
            note="Elle eklendi", performed_by=_actor(current_user)))
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Kayıt oluşturulamadı.")

    log_admin_event(db, request, actor=current_user, action="retention.create",
                    target_type="retention_sample", target_id=r.id,
                    target_name=r.lot_number,
                    details={"item": r.item_name, "quantity": data.quantity,
                             "brand": r.brand})
    return _view(r)
