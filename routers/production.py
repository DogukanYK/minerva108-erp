"""
Production router — manufacturing workflows + Quality Control (QA).
"""
from fastapi import APIRouter, Depends, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional

from database import (
    get_db, Item, Recipe, ProductionHistory, Inventory, Transaction,
)
from core.permissions import require_permission
from core.notifications import notify_low_stock

router = APIRouter(prefix="/api", tags=["production"])


# ─── Schemas ────────────────────────────────────────────────────────────────

class ProductionCreateRequest(BaseModel):
    recipe_id: int
    produced_quantity: float = Field(..., gt=0, le=1_000_000)


class QCActionRequest(BaseModel):
    notes:  str = Field(..., max_length=2000)
    status: str = Field(..., max_length=20)  # 'APPROVED' veya 'REJECTED'


class QCFormRequest(BaseModel):
    """Digital QC form — full checklist + lab results + decision."""
    status:      str  = Field(..., max_length=20)
    checklist:   dict = Field(...)            # { "q01": "Evet", ... } — endpoint validation yapıyor
    lab_ml:      Optional[float] = Field(None, ge=0, le=10_000)
    lab_density: Optional[float] = Field(None, ge=0, le=100)
    lab_color:   Optional[str]   = Field(None, max_length=50)
    notes:       Optional[str]   = Field("",   max_length=2000)


# ─── Production Endpoints ────────────────────────────────────────────────────

@router.get("/production")
def list_production_history(db: Session = Depends(get_db)):
    rows = db.query(ProductionHistory).order_by(ProductionHistory.id.desc()).limit(100).all()
    return [
        {
            "id": r.id,
            "recipe_name": r.recipe_name,
            "target_item_name": r.target_item_name,
            "produced_quantity": r.produced_quantity,
            "produced_at": r.produced_at.strftime("%d.%m.%Y %H:%M") if r.produced_at else "",
        }
        for r in rows
    ]


@router.post("/production", status_code=201)
def start_production(
    data: ProductionCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("production", "create")),
):
    recipe = db.query(Recipe).filter(Recipe.id == data.recipe_id).first()
    if not recipe:
        return JSONResponse(status_code=404, content={"detail": "Reçete bulunamadı."})
    if data.produced_quantity <= 0:
        return JSONResponse(status_code=400, content={"detail": "Üretim miktarı sıfırdan büyük olmalıdır."})

    actor = current_user.get("full_name") or current_user.get("username") or "—"
    multiplier = data.produced_quantity / recipe.output_quantity

    # ── Brüt Girdi Hesabı ──────────────────────────────────────────────────
    # Fire (waste) EKLEMELI çalışır: brüt_girdi = net_miktar × (1 + fire% / 100)
    # Örnek: 50 ml hammadde + %10 fire = 55 ml stoktan düşülür.
    # Ambalaj bileşenlerine fire uygulanmaz (reçete mantığıyla tutarlı).
    waste_factor = 1.0 + (recipe.waste_percentage or 0.0) / 100.0

    try:
        # ── Önce tüm brüt miktarları hesapla ve stok kontrolü yap ──────────
        ing_plan = []   # (ing, item, gross_qty) üçlüsü
        for ing in recipe.ingredients:
            item = db.query(Item).filter(Item.id == ing.item_id).with_for_update().first()
            if not item:
                db.rollback()
                return JSONResponse(status_code=404, content={"detail": f"Hammadde bulunamadı (ID: {ing.item_id})."})
            is_ambalaj = (item.category == "Ambalaj")
            factor     = 1.0 if is_ambalaj else waste_factor   # ambalaja fire uygulanmaz
            gross_qty  = round(ing.quantity * multiplier * factor, 6)
            ing_plan.append((ing, item, gross_qty, is_ambalaj))

        for ing, item, gross_qty, is_ambalaj in ing_plan:
            if item.current_stock < gross_qty:
                db.rollback()
                fire_note = "" if is_ambalaj else f" (%{recipe.waste_percentage or 0} fire dahil)"
                return JSONResponse(status_code=400, content={
                    "detail": f"'{item.name}' için yeterli stok yok. "
                              f"Gereken: {gross_qty} {item.unit}{fire_note}, "
                              f"Mevcut: {item.current_stock} {item.unit}"
                })

        # ── Lot numarası şimdiden üret — tüm transaction notlarına stamp atılır
        import datetime as _dt
        now = _dt.datetime.utcnow()
        produced_lot = f"PRD-{now.strftime('%Y%m%d-%H%M%S')}"

        # ── Stok düş + Output transaction kaydet ───────────────────────────
        for ing, item, gross_qty, is_ambalaj in ing_plan:
            item.current_stock = round(item.current_stock - gross_qty, 6)
            fire_note = (
                f" | %{recipe.waste_percentage or 0} fire dahil, brüt girdi"
                if not is_ambalaj and (recipe.waste_percentage or 0) > 0
                else ""
            )
            db.add(Transaction(
                item_id=ing.item_id,
                transaction_type="Output",
                quantity=gross_qty,
                notes=f"Üretim tüketimi — Reçete: {recipe.name}{fire_note} | Üretim Lot: {produced_lot}",
                performed_by=actor,
            ))

        # Üretilen lot doğrudan APPROVED — patron QC quarantine flow'u istemiyor:
        # üretim biter bitmez stok artmalı (Phase 8 / Bug 4 Logic Fix). QC ekibi
        # geriye dönük inceleme yapmak isterse history üstünden lot detayına bakabilir.
        if recipe.target_item_id:
            db.add(Inventory(
                item_id=recipe.target_item_id,
                lot_number=produced_lot,
                quantity=data.produced_quantity,
                status="APPROVED",
                received_by=actor,                  # Üretim çıktısını "alan" da üretici
                qc_approved_by=actor,                # Self-approve at production time
            ))
            db.add(Transaction(
                item_id=recipe.target_item_id,
                lot_number=produced_lot,
                transaction_type="Input",
                quantity=data.produced_quantity,
                notes=f"Üretim çıktısı — Reçete: {recipe.name}, Lot: {produced_lot}",
                performed_by=actor,
            ))
            # ── Stoğu anında artır — ledger ile current_stock arasındaki sync gap'i kapanır.
            target_item_row = db.query(Item).filter(Item.id == recipe.target_item_id).with_for_update().first()
            if target_item_row:
                target_item_row.current_stock = round(
                    (target_item_row.current_stock or 0) + data.produced_quantity, 6
                )

        # Üretim kaydı
        db.add(ProductionHistory(
            recipe_id=recipe.id,
            recipe_name=recipe.name,
            target_item_id=recipe.target_item_id,
            target_item_name=recipe.target_item.name if recipe.target_item else recipe.description,
            produced_quantity=data.produced_quantity,
            produced_by=actor,                       # Audit
            lot_number=produced_lot if recipe.target_item_id else None,
        ))

        db.commit()

        # ── Low-stock alert: any consumed ingredient that crossed its threshold
        #     queues exactly one notification (one per ingredient line, not one
        #     per stock unit). Snapshots primitive values now; the BackgroundTask
        #     fires after the response is sent so the user sees no extra latency.
        for _ing, item, _gross, _amb in ing_plan:
            if item.min_stock_level > 0 and item.current_stock <= item.min_stock_level:
                background_tasks.add_task(
                    notify_low_stock,
                    item.name, item.current_stock, item.min_stock_level, item.unit or "",
                )

        return {
            "message": f"Üretim tamamlandı. {data.produced_quantity} birim stoğa eklendi.",
            "lot_number": produced_lot if recipe.target_item_id else None,
        }

    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Üretim sırasında hata oluştu, stoklar değiştirilmedi."})


# ─── QC Endpoints ────────────────────────────────────────────────────────────

@router.get("/qc/quarantine")
def list_quarantine(db: Session = Depends(get_db)):
    rows = db.query(Inventory).filter(Inventory.status == "QUARANTINE").order_by(Inventory.id.desc()).all()
    return [
        {
            "id":           r.id,
            "item_name":    r.item.name if r.item else "—",
            "item_id":      r.item_id,
            "item_unit":    r.item.unit if r.item else "",
            "lot_number":   r.lot_number,
            "quantity":     r.quantity,
            "expiry_date":  r.expiry_date or "—",
            "location":     r.location or "—",
            "status":       r.status,
            "created_at":   r.created_at.strftime("%d.%m.%Y") if r.created_at else "",
        }
        for r in rows
    ]


@router.post("/qc/process/{inventory_id}")
def process_qc(
    inventory_id: int,
    data: QCActionRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("qc", "approve")),
):
    if data.status not in ("APPROVED", "REJECTED"):
        return JSONResponse(status_code=400, content={"detail": "Geçersiz statü. 'APPROVED' veya 'REJECTED' olmalıdır."})

    inv = db.query(Inventory).filter(Inventory.id == inventory_id).first()
    if not inv:
        return JSONResponse(status_code=404, content={"detail": "Envanter kaydı bulunamadı."})
    if inv.status != "QUARANTINE":
        return JSONResponse(status_code=400, content={"detail": "Bu kayıt zaten karantinade değil."})

    actor = current_user.get("full_name") or current_user.get("username") or "—"

    try:
        inv.status         = data.status
        inv.qc_notes       = data.notes
        inv.qc_approved_by = actor
        inv.updated_at     = __import__("datetime").datetime.utcnow()

        # APPROVED ise items.current_stock'u artır; REJECTED ise stok değişmez
        if data.status == "APPROVED":
            approved_item = db.query(Item).filter(Item.id == inv.item_id).first()
            if approved_item:
                approved_item.current_stock = round(approved_item.current_stock + inv.quantity, 6)

        tx_type = "QC Approval" if data.status == "APPROVED" else "QC Rejection"
        db.add(Transaction(
            item_id=inv.item_id,
            lot_number=inv.lot_number,
            transaction_type=tx_type,
            quantity=inv.quantity,
            notes=f"{tx_type} — Lot: {inv.lot_number}. Not: {data.notes}",
            performed_by=actor,
        ))

        db.commit()
        label = "Onaylandı" if data.status == "APPROVED" else "Reddedildi"
        return {"message": f"Lot #{inv.lot_number} başarıyla {label}."}
    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "QC işlemi sırasında hata oluştu."})


@router.post("/inventory/{inventory_id}/qc-approve")
def qc_approve_form(
    inventory_id: int,
    data: QCFormRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("qc", "approve")),
):
    """
    Digital QC form endpoint — the ONLY approved path to change a lot from
    QUARANTINE to APPROVED or REJECTED.  Stores the full form JSON in
    inventory.qc_form_data so the audit trail is permanent.
    """
    import json
    from datetime import datetime as _dt

    if data.status not in ("APPROVED", "REJECTED"):
        return JSONResponse(status_code=400, content={"detail": "Geçersiz statü. 'APPROVED' veya 'REJECTED' olmalıdır."})

    inv = db.query(Inventory).filter(Inventory.id == inventory_id).first()
    if not inv:
        return JSONResponse(status_code=404, content={"detail": "Envanter kaydı bulunamadı."})
    if inv.status != "QUARANTINE":
        return JSONResponse(status_code=400, content={"detail": "Bu lot zaten işlenmiş — tekrar değiştirilemez."})

    if not data.checklist:
        return JSONResponse(status_code=422, content={"detail": "Kontrol listesi boş gönderilemez."})

    # ── Serialize full form payload for permanent audit ────────────────────
    form_payload = {
        "checklist":    data.checklist,
        "lab_ml":       data.lab_ml,
        "lab_density":  data.lab_density,
        "lab_color":    data.lab_color,
        "notes":        data.notes or "",
        "status":       data.status,
        "submitted_at": _dt.utcnow().isoformat(),
    }

    actor = current_user.get("full_name") or current_user.get("username") or "—"

    try:
        inv.status         = data.status
        inv.qc_notes       = data.notes or ""
        inv.qc_form_data   = json.dumps(form_payload, ensure_ascii=False)
        inv.qc_approved_by = actor                  # Audit: who QC'd
        inv.updated_at     = _dt.utcnow()

        label = "Onaylandı ✓" if data.status == "APPROVED" else "Reddedildi ✗"

        # APPROVED → add lot quantity to live stock
        if data.status == "APPROVED":
            approved_item = db.query(Item).filter(Item.id == inv.item_id).first()
            if approved_item:
                approved_item.current_stock = round(approved_item.current_stock + inv.quantity, 6)

        note_text = f"QC Form — {label} — Lot: {inv.lot_number}"
        if data.notes:
            note_text += f" | Not: {data.notes[:120]}"

        db.add(Transaction(
            item_id=inv.item_id,
            lot_number=inv.lot_number,
            transaction_type="QC Approval" if data.status == "APPROVED" else "QC Rejection",
            quantity=inv.quantity,
            notes=note_text,
            performed_by=actor,                      # Audit
        ))

        db.commit()
        return {"message": f"Lot #{inv.lot_number} QC formu kaydedildi — {label}."}

    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "QC işlemi sırasında hata oluştu."})
