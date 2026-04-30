"""
Inventory router — items, suppliers, receiving, transactions, traceability,
and Excel imports (both classic templated import + smart auto-detect import).
"""
from fastapi import APIRouter, Depends, UploadFile, File, BackgroundTasks
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List

from database import (
    get_db, Item, Supplier, Inventory, Transaction,
)
from core.auth import get_current_user
from core.permissions import _can_see_finance, require_permission
from core.notifications import notify_low_stock

router = APIRouter(prefix="/api", tags=["inventory"])


# ─── Schemas ────────────────────────────────────────────────────────────────

class ItemCreateRequest(BaseModel):
    name: str
    category: Optional[str] = None
    unit: str = "adet"
    min_stock:      Optional[float] = 0.0
    cost_price:     Optional[float] = 0.0
    parent_id:      Optional[int]   = None    # Variation hierarchy — null for parents/standalones
    variation_name: Optional[str]   = None    # e.g. "200ml" — required if parent_id set


class BulkDeleteRequest(BaseModel):
    item_ids: List[int]


class SupplierCreateRequest(BaseModel):
    name: str
    contact_person: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None


class SupplierBulkDeleteRequest(BaseModel):
    supplier_ids: List[int]


class StockReceiveRequest(BaseModel):
    item_id: int
    supplier_id: Optional[int] = None
    lot_number: str
    expiry_date: Optional[str] = None
    quantity: float
    location: Optional[str] = None


# ─── Items Endpoints ─────────────────────────────────────────────────────────

@router.get("/items")
def list_items(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    items = db.query(Item).filter(Item.is_active == True).order_by(Item.id.desc()).all()

    # Resolve parent names + child counts in O(n) — avoids N+1 queries
    name_by_id = {i.id: i.name for i in items}
    child_count = {}
    for i in items:
        if i.parent_id:
            child_count[i.parent_id] = child_count.get(i.parent_id, 0) + 1

    finance_ok = _can_see_finance(current_user)
    return [
        {
            "id":              i.id,
            "name":            i.name,
            "category":        i.category,
            "unit":            i.unit,
            "min_stock_level": i.min_stock_level,
            "current_stock":   i.current_stock,
            # ── Finance-gated: zeroed for lab roles (defense in depth vs. DevTools snooping) ──
            "cost_price":      round(i.cost_price or 0.0, 4) if finance_ok else 0.0,
            "parent_id":       i.parent_id,
            "parent_name":     name_by_id.get(i.parent_id) if i.parent_id else None,
            "variation_name":  i.variation_name,
            "child_count":     child_count.get(i.id, 0),
            "is_parent":       child_count.get(i.id, 0) > 0,
            "is_variation":    i.parent_id is not None,
            "created_at":      i.created_at.strftime("%d.%m.%Y") if i.created_at else "",
        }
        for i in items
    ]


def _validate_variation(
    db: Session,
    parent_id: Optional[int],
    variation_name: Optional[str],
    self_id: Optional[int] = None,
) -> Optional[JSONResponse]:
    """
    Shared parent-child validation for both create_item and update_item.
    Returns a JSONResponse on validation failure, or None if OK.
    """
    if parent_id is None:
        return None  # Standalone or future-parent — nothing to validate

    if self_id is not None and parent_id == self_id:
        return JSONResponse(status_code=400, content={"detail": "Bir ürün kendisinin varyasyonu olamaz."})

    parent = db.query(Item).filter(Item.id == parent_id).first()
    if not parent:
        return JSONResponse(status_code=400, content={"detail": "Seçilen ana ürün bulunamadı."})

    if parent.parent_id is not None:
        return JSONResponse(status_code=400, content={
            "detail": "Varyasyonlar bir başka varyasyonun altına eklenemez (depth=1 sınırı)."
        })

    if not variation_name or not variation_name.strip():
        return JSONResponse(status_code=400, content={"detail": "Varyasyon adı zorunludur (örn: '200ml')."})

    return None


@router.post("/items", status_code=201)
def create_item(data: ItemCreateRequest, db: Session = Depends(get_db), _: dict = Depends(require_permission("items", "create"))):
    err = _validate_variation(db, data.parent_id, data.variation_name)
    if err: return err

    item = Item(
        name=data.name,
        category=data.category,
        unit=data.unit,
        min_stock_level=data.min_stock  or 0.0,
        cost_price=data.cost_price or 0.0,
        parent_id=data.parent_id,
        variation_name=(data.variation_name.strip() if data.parent_id and data.variation_name else None),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "message": "Ürün başarıyla eklendi."}


@router.delete("/items/{item_id}")
def delete_item(item_id: int, db: Session = Depends(get_db), _: dict = Depends(require_permission("items", "delete"))):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        return JSONResponse(status_code=404, content={"detail": "Ürün bulunamadı."})
    db.delete(item)
    db.commit()
    return {"message": "Ürün silindi."}


@router.put("/items/{item_id}")
def update_item(
    item_id: int,
    data: ItemCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("items", "edit")),
):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        return JSONResponse(status_code=404, content={"detail": "Ürün bulunamadı."})

    # If converting to a variation, ensure this item itself has no children (would orphan them)
    if data.parent_id is not None and item.parent_id is None:
        has_children = db.query(Item).filter(Item.parent_id == item_id).count() > 0
        if has_children:
            return JSONResponse(status_code=400, content={
                "detail": "Bu ürünün varyasyonları mevcut. Önce varyasyonları silip sonra dönüştürebilirsiniz."
            })

    err = _validate_variation(db, data.parent_id, data.variation_name, self_id=item_id)
    if err: return err

    item.name            = data.name
    item.category        = data.category
    item.unit            = data.unit
    item.min_stock_level = data.min_stock  or 0.0
    item.cost_price      = data.cost_price or 0.0
    item.parent_id       = data.parent_id
    item.variation_name  = (data.variation_name.strip() if data.parent_id and data.variation_name else None)
    db.commit()

    # ── Low-stock alert: if the edit (typically a min_stock_level bump) leaves
    #     the item at or below threshold, queue a notification on the response.
    if item.min_stock_level > 0 and item.current_stock <= item.min_stock_level:
        background_tasks.add_task(
            notify_low_stock,
            item.name, item.current_stock, item.min_stock_level, item.unit or "",
        )

    return {"id": item.id, "message": "Ürün güncellendi."}


@router.post("/items/bulk-delete")
def bulk_delete_items(data: BulkDeleteRequest, db: Session = Depends(get_db), _: dict = Depends(require_permission("items", "delete"))):
    deleted = db.query(Item).filter(Item.id.in_(data.item_ids)).delete(synchronize_session=False)
    db.commit()
    return {"message": f"{deleted} ürün silindi."}


# ─── Suppliers Endpoints ─────────────────────────────────────────────────────

@router.get("/suppliers")
def list_suppliers(db: Session = Depends(get_db)):
    rows = db.query(Supplier).filter(Supplier.is_active == True).order_by(Supplier.id.desc()).all()
    return [
        {
            "id": s.id,
            "name": s.name,
            "contact_person": s.contact_person,
            "email": s.email,
            "phone": s.phone,
            "notes": s.notes,
            "created_at": s.created_at.strftime("%d.%m.%Y") if s.created_at else "",
        }
        for s in rows
    ]


@router.post("/suppliers", status_code=201)
def create_supplier(data: SupplierCreateRequest, db: Session = Depends(get_db), _: dict = Depends(require_permission("items", "create"))):
    supplier = Supplier(
        name=data.name,
        contact_person=data.contact_person,
        email=data.email,
        phone=data.phone,
        notes=data.notes,
    )
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    return {"id": supplier.id, "message": "Tedarikçi başarıyla eklendi."}


@router.delete("/suppliers/{supplier_id}")
def delete_supplier(supplier_id: int, db: Session = Depends(get_db), _: dict = Depends(require_permission("items", "delete"))):
    supplier = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not supplier:
        return JSONResponse(status_code=404, content={"detail": "Tedarikçi bulunamadı."})
    db.delete(supplier)
    db.commit()
    return {"message": "Tedarikçi silindi."}


@router.post("/suppliers/bulk-delete")
def bulk_delete_suppliers(data: SupplierBulkDeleteRequest, db: Session = Depends(get_db), _: dict = Depends(require_permission("items", "delete"))):
    deleted = db.query(Supplier).filter(Supplier.id.in_(data.supplier_ids)).delete(synchronize_session=False)
    db.commit()
    return {"message": f"{deleted} tedarikçi silindi."}


# ─── Inventory / Receiving Endpoints ────────────────────────────────────────

@router.get("/inventory")
def list_inventory(db: Session = Depends(get_db)):
    rows = db.query(Inventory).order_by(Inventory.id.desc()).all()
    return [
        {
            "id": r.id,
            "item_name": r.item.name if r.item else "—",
            "item_id": r.item_id,
            "supplier_name": r.supplier.name if r.supplier else "—",
            "lot_number": r.lot_number,
            "expiry_date": r.expiry_date or "—",
            "quantity": r.quantity,
            "location": r.location or "—",
            "status": r.status,
            "created_at": r.created_at.strftime("%d.%m.%Y") if r.created_at else "",
        }
        for r in rows
    ]


@router.post("/inventory/receive", status_code=201)
def receive_stock(
    data: StockReceiveRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    if data.quantity <= 0:
        return JSONResponse(status_code=400, content={"detail": "Miktar sıfırdan büyük olmalıdır."})

    item = db.query(Item).filter(Item.id == data.item_id).first()
    if not item:
        return JSONResponse(status_code=404, content={"detail": "Ürün bulunamadı."})

    actor = current_user.get("full_name") or current_user.get("username") or "—"

    try:
        # ── Upsert Logic (4.7) ──────────────────────────────────────────────
        existing = db.query(Inventory).filter(
            Inventory.item_id == data.item_id,
            Inventory.lot_number == data.lot_number,
        ).first()

        if existing:
            # Miktarı topla; location/supplier_id sadece yeni değer varsa güncelle (COALESCE)
            existing.quantity   += data.quantity
            existing.updated_at  = __import__("datetime").datetime.utcnow()
            if data.location:
                existing.location = data.location
            if data.supplier_id is not None:
                existing.supplier_id = data.supplier_id
            if data.expiry_date:
                existing.expiry_date = data.expiry_date
            # received_by sadece ilk kabul edende kalır (audit immutability)
        else:
            # Yeni satır — statü kesinlikle APPROVED
            db.add(Inventory(
                item_id=data.item_id,
                supplier_id=data.supplier_id,
                lot_number=data.lot_number,
                expiry_date=data.expiry_date,
                quantity=data.quantity,
                location=data.location,
                status="APPROVED",
                received_by=actor,                      # Audit trail
            ))

        # ── Transaction kaydı (2.4) ─────────────────────────────────────────
        db.add(Transaction(
            item_id=data.item_id,
            lot_number=data.lot_number,
            transaction_type="Input",
            quantity=data.quantity,
            notes=f"Mal kabul — Lot: {data.lot_number}" + (f", Konum: {data.location}" if data.location else ""),
            performed_by=actor,                          # Audit trail
        ))

        # ── items.current_stock güncelle (üretim modülü ile uyum) ───────────
        item.current_stock = round(item.current_stock + data.quantity, 6)

        db.commit()
        return {"message": f"Mal kabul başarılı. {data.quantity} {item.unit} stoka eklendi."}

    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Mal kabul sırasında hata oluştu."})


# ─── Inventory summary + transactions feed ──────────────────────────────────

@router.get("/inventory/summary")
def inventory_summary(db: Session = Depends(get_db)):
    """
    Mevcut stok özeti — `Item.current_stock` source-of-truth olarak
    kullanılır. Önceden APPROVED Inventory satırlarının quantity'lerini
    topluyorduk; ama production output'u eskiden QUARANTINE'de kalıyordu
    ve current_stock'a yansımıyordu. Phase 8 / Bug 4'le production direkt
    current_stock'u arttırıyor — bu endpoint de aynı kanonik değeri okur,
    böylece /stocks ve canlı önizleme birbirine uyumlu.
    """
    items = (
        db.query(Item)
        .filter(Item.is_active == True)
        .order_by(Item.category, Item.name)
        .all()
    )
    return [
        {
            "item_id":     i.id,
            "name":        i.name,
            "category":    i.category or "Diğer",
            "unit":        i.unit,
            "total_stock": round(float(i.current_stock or 0), 4),
        }
        for i in items
    ]


@router.get("/transactions")
def list_transactions(db: Session = Depends(get_db)):
    rows = (
        db.query(Transaction)
        .order_by(Transaction.id.desc())
        .limit(500)
        .all()
    )
    return [
        {
            "id": r.id,
            "item_name": r.item.name if r.item else "—",
            "lot_number": r.lot_number or "—",
            "transaction_type": r.transaction_type,
            "quantity": r.quantity,
            "notes": r.notes or "",
            "timestamp": r.timestamp.strftime("%d.%m.%Y %H:%M") if r.timestamp else "",
        }
        for r in rows
    ]


# ─── Traceability / Audit Trail (Phase 2 / Task 9) ──────────────────────────

def _import_dt():
    """Tiny helper — import datetime lazily without polluting module top level."""
    import datetime as _d
    return _d.datetime


@router.get("/traceability/lot/{lot_number}")
def trace_lot(lot_number: str, db: Session = Depends(get_db), _: dict = Depends(get_current_user)):
    """
    Full genealogy tree for a lot. Resolves:
      • Lot identity (Inventory record + supplier)
      • Production record (if internally produced)
      • Ingredients consumed during that production (with their own supplier/expiry/received_by)
      • All transactions for this lot — chronological audit trail.
    """
    # Imported here to avoid cross-router top-level dependency on production model
    from database import ProductionHistory

    inv  = db.query(Inventory).filter(Inventory.lot_number == lot_number).first()
    prod = db.query(ProductionHistory).filter(ProductionHistory.lot_number == lot_number).first()

    if not inv and not prod:
        return JSONResponse(status_code=404, content={"detail": f"Lot bulunamadı: {lot_number}"})

    out = {"lot_number": lot_number}

    # ── Lot bilgisi (Inventory) ──────────────────────────────────────────────
    if inv:
        item     = db.query(Item).filter(Item.id == inv.item_id).first()
        supplier = db.query(Supplier).filter(Supplier.id == inv.supplier_id).first() if inv.supplier_id else None
        out["lot_info"] = {
            "id":             inv.id,
            "item_id":        inv.item_id,
            "item_name":      item.name if item else "—",
            "item_category":  item.category if item else "—",
            "item_unit":      item.unit if item else "",
            "supplier_name":  supplier.name if supplier else "—",
            "supplier_phone": (supplier.phone if supplier else "") or "",
            "expiry_date":    inv.expiry_date or "—",
            "quantity":       inv.quantity,
            "location":       inv.location or "—",
            "status":         inv.status,
            "received_by":    inv.received_by or "—",
            "qc_approved_by": inv.qc_approved_by or "—",
            "qc_notes":       inv.qc_notes or "",
            "created_at":     inv.created_at.strftime("%d.%m.%Y %H:%M") if inv.created_at else "",
            "updated_at":     inv.updated_at.strftime("%d.%m.%Y %H:%M") if inv.updated_at else "",
        }
    else:
        out["lot_info"] = None

    # ── Üretim kaydı + tüketilen hammaddeler ────────────────────────────────
    if prod:
        # Production-time Output transactions are stamped with "Üretim Lot: {lot}" in notes.
        marker = f"Üretim Lot: {lot_number}"
        ing_outputs = (
            db.query(Transaction)
            .filter(
                Transaction.transaction_type == "Output",
                Transaction.notes.like(f"%{marker}%"),
            )
            .order_by(Transaction.id.asc())
            .all()
        )

        ingredients_consumed = []
        for tx in ing_outputs:
            tx_item = db.query(Item).filter(Item.id == tx.item_id).first()
            # Best-guess source lot: most-recent APPROVED Inventory for this item before production date
            tx_inv = (
                db.query(Inventory)
                .filter(
                    Inventory.item_id == tx.item_id,
                    Inventory.status == "APPROVED",
                    Inventory.created_at <= (prod.produced_at or _import_dt().utcnow()),
                )
                .order_by(Inventory.created_at.desc())
                .first()
            )
            tx_supplier = db.query(Supplier).filter(Supplier.id == tx_inv.supplier_id).first() if tx_inv and tx_inv.supplier_id else None
            ingredients_consumed.append({
                "item_id":       tx.item_id,
                "item_name":     tx_item.name if tx_item else "—",
                "item_category": tx_item.category if tx_item else "—",
                "quantity":      tx.quantity,
                "unit":          tx_item.unit if tx_item else "",
                "source_lot":    tx_inv.lot_number   if tx_inv else "—",
                "supplier_name": tx_supplier.name    if tx_supplier else "—",
                "expiry_date":   (tx_inv.expiry_date if tx_inv else None) or "—",
                "received_by":   (tx_inv.received_by if tx_inv else None) or "—",
                "performed_by":  tx.performed_by or "—",
            })

        out["production"] = {
            "id":                prod.id,
            "recipe_id":         prod.recipe_id,
            "recipe_name":       prod.recipe_name or "—",
            "target_item_id":    prod.target_item_id,
            "target_item_name":  prod.target_item_name or "—",
            "produced_quantity": prod.produced_quantity,
            "produced_at":       prod.produced_at.strftime("%d.%m.%Y %H:%M") if prod.produced_at else "—",
            "produced_by":       prod.produced_by or "—",
            "ingredients_consumed": ingredients_consumed,
        }
    else:
        out["production"] = None

    # ── İşlem geçmişi ───────────────────────────────────────────────────────
    txs = (
        db.query(Transaction)
        .filter(Transaction.lot_number == lot_number)
        .order_by(Transaction.id.asc())
        .all()
    )
    out["transactions"] = [
        {
            "id":               t.id,
            "transaction_type": t.transaction_type,
            "quantity":         t.quantity,
            "timestamp":        t.timestamp.strftime("%d.%m.%Y %H:%M") if t.timestamp else "—",
            "performed_by":     t.performed_by or "—",
            "notes":            (t.notes or "")[:200],
        }
        for t in txs
    ]

    return out


@router.get("/traceability/expiring")
def list_expiring(db: Session = Depends(get_db), _: dict = Depends(get_current_user)):
    """All APPROVED inventory lots expiring within the next 60 days, sorted most-urgent first."""
    from datetime import datetime as _dt, timedelta

    today  = _dt.utcnow().date()
    cutoff = today + timedelta(days=60)

    rows = (
        db.query(Inventory)
        .filter(
            Inventory.expiry_date.isnot(None),
            Inventory.expiry_date != "",
            Inventory.status == "APPROVED",
        )
        .all()
    )

    result = []
    for r in rows:
        try:
            exp_date = _dt.strptime(r.expiry_date, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        days_left = (exp_date - today).days
        if days_left < 0 or days_left > 60:
            continue
        item = db.query(Item).filter(Item.id == r.item_id).first()
        result.append({
            "id":            r.id,
            "lot_number":    r.lot_number,
            "item_name":     item.name if item else "—",
            "item_category": item.category if item else "—",
            "item_unit":     item.unit if item else "",
            "expiry_date":   r.expiry_date,
            "days_left":     days_left,
            "quantity":      r.quantity,
            "location":      r.location or "—",
            "received_by":   r.received_by or "—",
        })

    result.sort(key=lambda x: x["days_left"])
    return result


# ─── User-specific audit feed (Phase 8 / Bug 5) ─────────────────────────────
# Lets management drill into what a specific user has done over a time window.
# Gated on admin.view_audit so Manager (Işık Hanım) and SuperAdmin can see it
# while lab roles cannot.

@router.get("/traceability/audit-users")
def list_audit_users(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("admin", "view_audit")),
):
    """Lightweight user list — drives the dropdown on the traceability page."""
    from database import User
    users = (
        db.query(User)
        .filter(User.is_active == True)
        .order_by(User.full_name.asc())
        .all()
    )
    return [
        {"id": u.id, "username": u.username, "full_name": u.full_name, "role": u.role}
        for u in users
    ]


@router.get("/traceability/user-activity")
def user_activity(
    user_id: int,
    days:       Optional[int] = None,             # legacy preset support (last N days)
    date_from:  Optional[str] = None,             # YYYY-MM-DD inclusive (custom range)
    date_to:    Optional[str] = None,             # YYYY-MM-DD inclusive
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("admin", "view_audit")),
):
    """
    Audit trail for a specific user. Two modes:

      • Preset window:  days=30  → "last 30 days from now"
      • Custom range:   date_from=2025-01-01 & date_to=2025-12-31

    If both are sent, custom range wins. If neither, defaults to 30 days.
    Transaction.performed_by stores `actor` strings (full_name preferred,
    username fallback) so we match against both candidates of the target user.
    """
    from datetime import datetime as _dt, timedelta
    from database import User

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse(status_code=404, content={"detail": "Kullanıcı bulunamadı."})

    # ── Resolve the window ──────────────────────────────────────────────
    range_label = None
    if date_from or date_to:
        try:
            since = _dt.strptime(date_from, "%Y-%m-%d") if date_from else _dt(1970, 1, 1)
            until = _dt.strptime(date_to,   "%Y-%m-%d") + timedelta(days=1) if date_to else _dt.utcnow()
        except ValueError:
            return JSONResponse(status_code=422, content={
                "detail": "Geçersiz tarih formatı. YYYY-MM-DD bekleniyor."
            })
        range_label = f"{date_from or '∞'} → {date_to or 'bugün'}"
    else:
        n = max(1, min(int(days or 30), 1095))    # clamp 1 day .. 3 years
        since = _dt.utcnow() - timedelta(days=n)
        until = _dt.utcnow() + timedelta(days=1)
        range_label = f"son {n} gün"

    candidates = [c for c in (user.full_name, user.username) if c]

    rows = (
        db.query(Transaction)
        .filter(
            Transaction.performed_by.in_(candidates),
            Transaction.timestamp >= since,
            Transaction.timestamp <  until,
        )
        .order_by(Transaction.id.desc())
        .limit(500)
        .all()
    )
    return {
        "user": {
            "id":        user.id,
            "username":  user.username,
            "full_name": user.full_name,
            "role":      user.role,
        },
        "range":       range_label,
        "since":       since.isoformat() + "Z",
        "until":       until.isoformat() + "Z",
        "count":       len(rows),
        "transactions": [
            {
                "id":               t.id,
                "item_name":        t.item.name if t.item else "—",
                "lot_number":       t.lot_number or "—",
                "transaction_type": t.transaction_type,
                "quantity":         t.quantity,
                "notes":            (t.notes or "")[:200],
                "timestamp":        t.timestamp.strftime("%d.%m.%Y %H:%M") if t.timestamp else "—",
            }
            for t in rows
        ],
    }


# ─── Excel Import Endpoints (classic templated upload) ──────────────────────

_REQUIRED_COLS = {"Item_Name", "SKU", "Category", "Unit", "Stock", "Cost_Price", "Min_Stock_Level"}


@router.get("/import-items/template")
def download_import_template(_: dict = Depends(require_permission("items", "import"))):
    """Doldurulabilir örnek Excel şablonunu indir."""
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Ürün Şablonu"

    headers = list(_REQUIRED_COLS)
    # Sabit sıralama
    headers = ["Item_Name", "SKU", "Category", "Unit", "Stock", "Cost_Price", "Min_Stock_Level"]
    ws.append(headers)

    # Örnek satırlar
    ws.append(["Argan Yağı",      "ARG-001",     "Hammadde",    "kg",   50,  25.50, 10])
    ws.append(["Cam Şişe 100ml",  "CAM-100",     "Ambalaj",     "adet", 200,  3.75, 50])
    ws.append(["Vitamin C Serum", "VIT-SRM-001", "Bitmiş Ürün", "adet",  0,  45.00, 20])

    # Header stili — lacivert/altın
    hdr_font  = Font(bold=True, color="FFFFFF", size=11)
    hdr_fill  = PatternFill("solid", fgColor="2C2C73")
    hdr_align = Alignment(horizontal="center", vertical="center")
    thin_border = Border(
        bottom=Side(style="thin", color="B8965A"),
        right=Side(style="thin",  color="E5E7EB"),
    )
    for cell in ws[1]:
        cell.font   = hdr_font
        cell.fill   = hdr_fill
        cell.alignment = hdr_align
        cell.border = thin_border

    # Zebra satırları
    alt_fill = PatternFill("solid", fgColor="F5F0E8")
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        for cell in row:
            if cell.row % 2 == 0:
                cell.fill = alt_fill
            cell.alignment = Alignment(horizontal="left", vertical="center")

    # Otomatik sütun genişliği
    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col) + 4
        ws.column_dimensions[col[0].column_letter].width = min(max_len, 30)

    ws.row_dimensions[1].height = 22

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=minerva108_urun_sablonu.xlsx"},
    )


@router.post("/import-items")
async def import_items_from_excel(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("items", "import")),
):
    """Excel (.xlsx) dosyasından toplu ürün içe aktarma — SKU bazlı upsert."""
    import io, datetime as _dt

    # ── 1. Dosya uzantısı kontrolü ──────────────────────────────────────────
    if not file.filename.lower().endswith(".xlsx"):
        return JSONResponse(status_code=400, content={
            "detail": "Geçersiz dosya formatı. Yalnızca .xlsx dosyaları desteklenmektedir."
        })

    contents = await file.read()
    if len(contents) == 0:
        return JSONResponse(status_code=400, content={"detail": "Yüklenen dosya boş."})

    # ── 2. Pandas ile oku ───────────────────────────────────────────────────
    try:
        import pandas as pd
        df = pd.read_excel(io.BytesIO(contents), dtype=str)   # hepsini str oku, sonra cast
        df.columns = [str(c).strip() for c in df.columns]     # boşluk temizle
    except Exception as exc:
        return JSONResponse(status_code=400, content={
            "detail": f"Excel dosyası okunamadı. Dosya bozuk olabilir. ({exc})"
        })

    # ── 3. Zorunlu sütun kontrolü ───────────────────────────────────────────
    missing_cols = _REQUIRED_COLS - set(df.columns)
    if missing_cols:
        return JSONResponse(status_code=422, content={
            "detail": f"Eksik sütunlar: {', '.join(sorted(missing_cols))}. "
                      "Lütfen örnek şablonu indirip kullanın."
        })

    # ── 4. Satır satır upsert ───────────────────────────────────────────────
    created, updated = 0, 0
    errors = []

    for idx, row in df.iterrows():
        row_num = int(idx) + 2  # Excel satır no (header = 1)
        try:
            item_name = str(row.get("Item_Name", "") or "").strip()
            sku       = str(row.get("SKU",       "") or "").strip()

            if not item_name:
                errors.append(f"Satır {row_num}: 'Item_Name' boş bırakılamaz — satır atlandı.")
                continue
            if not sku:
                errors.append(f"Satır {row_num}: 'SKU' boş bırakılamaz — satır atlandı.")
                continue

            def _float(val, default=0.0):
                try:    return float(str(val).replace(",", "."))
                except: return default

            category   = str(row.get("Category",        "") or "").strip() or None
            unit       = str(row.get("Unit",            "") or "adet").strip() or "adet"
            stock      = _float(row.get("Stock"),       0.0)
            cost_price = _float(row.get("Cost_Price"),  0.0)
            min_stock  = _float(row.get("Min_Stock_Level"), 0.0)

            existing = db.query(Item).filter(Item.sku == sku).first()

            if existing:
                # ── GÜNCELLE ────────────────────────────────────────────
                existing.name           = item_name
                existing.category       = category
                existing.unit           = unit
                existing.cost_price     = cost_price
                existing.min_stock_level = min_stock
                if stock >= 0:
                    existing.current_stock = round(stock, 6)
                updated += 1

            else:
                # ── OLUŞTUR ─────────────────────────────────────────────
                new_item = Item(
                    name=item_name, sku=sku, category=category,
                    unit=unit, current_stock=round(stock, 6),
                    cost_price=cost_price, min_stock_level=min_stock,
                )
                db.add(new_item)
                db.flush()   # ID'yi al

                if stock > 0:
                    lot = f"IMP-{_dt.datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{new_item.id}"
                    db.add(Inventory(
                        item_id=new_item.id, lot_number=lot,
                        quantity=stock, status="APPROVED",
                    ))
                    db.add(Transaction(
                        item_id=new_item.id, lot_number=lot,
                        transaction_type="Input", quantity=stock,
                        notes=f"Excel içe aktarım — SKU: {sku}",
                    ))
                created += 1

        except Exception as exc:
            db.rollback()
            errors.append(f"Satır {row_num}: İşlem hatası — {str(exc)[:100]}")

    # ── 5. Commit ────────────────────────────────────────────────────────────
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        return JSONResponse(status_code=500, content={
            "detail": f"Veritabanına kayıt sırasında hata oluştu: {str(exc)[:120]}"
        })

    suffix = f" {len(errors)} satırda hata oluştu." if errors else ""
    return {
        "created": created,
        "updated": updated,
        "error_count": len(errors),
        "errors": errors,
        "message": f"{created} yeni ürün eklendi, {updated} ürün güncellendi.{suffix}",
    }


# ─── Smart Excel Importer (Phase 4 / Task 1) ────────────────────────────────

def _parse_qty_unit(raw):
    """
    Parse messy quantity strings from the Hammadde sheets.
      '4.276GR'  → (4276, 'g')      (Turkish dot = thousands separator)
      '1 KG'     → (1000, 'g')
      '90GR'     → (90, 'g')
      '162ML'    → (162, 'ml')
      '21KG'     → (21000, 'g')
      ''/None    → (0, 'adet')
    """
    import re
    if raw is None:
        return (0.0, "adet")
    s = str(raw).strip().upper()
    if not s or s in ("—", "-", "NAN"):
        return (0.0, "adet")

    # Detect unit suffix
    m = re.search(r'(KG|ML|GR|G)\b\s*$', s)
    if m:
        unit_str = m.group(1)
        s = s[:m.start()].strip()
    else:
        unit_str = "GR"

    # Comma → dot for decimals (Turkish comma)
    s = s.replace(",", ".")
    # Treat XX.XXX (3 trailing digits after dot) as a thousands separator
    if re.match(r'^\d+\.\d{3}$', s):
        s = s.replace(".", "")

    try:
        qty = float(s)
    except ValueError:
        qty = 0.0

    if unit_str == "KG":
        return (qty * 1000, "g")
    if unit_str == "ML":
        return (qty, "ml")
    return (qty, "g")


def _detect_excel_schema(workbook) -> str:
    """
    Sniff the workbook to identify which schema it follows. Returns one of:
      'hammadde' — multi-sheet raw-material catalogue with HAMMADDE/GR/MARKASI columns
      'etiket'   — packaging/label inventory (ÜRÜN İSMİ + ML + ETİKET SAYISI)
      'standard' — the existing minimal Item_Name/SKU/Category template
      'unknown'
    """
    keywords_hammadde = {"HAMMADDE", "MARKASI"}
    keywords_etiket   = {"ETİKET", "ÜRÜN İSİM", "ÜRÜN İSMİ"}
    keywords_standard = {"ITEM_NAME", "SKU"}

    for sheet in workbook.sheetnames:
        ws = workbook[sheet]
        # Scan first 5 rows
        for row in ws.iter_rows(min_row=1, max_row=5, values_only=True):
            cells = [str(c).strip().upper() for c in row if c is not None]
            joined = " | ".join(cells)
            if any(k in joined for k in keywords_hammadde):
                return "hammadde"
            if any(k in joined for k in keywords_etiket):
                return "etiket"
            if any(k in joined for k in keywords_standard):
                return "standard"
    return "unknown"


def _parse_hammadde_workbook(workbook) -> list:
    """
    Walk every sheet in a Hammadde workbook. Returns list of dicts:
      { sheet, name, quantity, unit, supplier, raw_qty }
    Skips header rows ('KONTROL TARİHİ' or starts with 'DOLAP') and blank rows.
    """
    out = []
    for sheet_name in workbook.sheetnames:
        ws = workbook[sheet_name]
        for row in ws.iter_rows(min_row=1, values_only=True):
            row_norm = [c if c is not None else "" for c in row]
            if len(row_norm) < 4:
                continue

            col_a = str(row_norm[0]).strip()
            col_b = str(row_norm[1]).strip()
            col_c = row_norm[2]
            col_d = str(row_norm[3]).strip() if len(row_norm) > 3 else ""

            # Skip blanks and section headers
            if not col_b:
                continue
            joined_left = f"{col_a} {col_b}".upper()
            if "KONTROL TARİHİ" in joined_left:        # Header row (multiple sheets repeat it)
                continue
            if "DOLAP" in joined_left and "RAF" in joined_left:   # Cabinet/shelf section header — anywhere in left two cols
                continue
            if "DOLAP" in joined_left and "ÜST RAF" in joined_left:
                continue
            # Skip rows where col_b is itself the column header
            if col_b.upper().strip() in ("HAMMADDE İSİM", "HAMMADDE İSIM"):
                continue
            # Skip if col_b looks like a section header by itself
            if col_b.upper().strip().startswith("DOLAP"):
                continue

            qty, unit = _parse_qty_unit(col_c)
            out.append({
                "sheet":    sheet_name,
                "name":     col_b,
                "quantity": qty,
                "unit":     unit,
                "supplier": col_d if col_d and col_d not in ("—", "-", "NAN") else None,
                "raw_qty":  str(col_c) if col_c is not None else "",
            })
    return out


@router.post("/admin/smart-import")
async def smart_excel_import(
    file: UploadFile = File(...),
    commit: bool = False,
    _: dict = Depends(require_permission("admin", "import_excel")),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Smart Excel importer.
      - Auto-detects schema (Hammadde / Etiket / Standard)
      - Always returns a dry-run preview (commit=false default)
      - When commit=true: creates Items, Suppliers, Inventory + Transaction logs
      - Existing items are upserted (stock added, supplier filled if missing)
    """
    import io
    import openpyxl
    from datetime import datetime as _dt

    actor = current_user.get("full_name") or current_user.get("username") or "Excel Bulk Import"

    try:
        contents = await file.read()
        wb = openpyxl.load_workbook(io.BytesIO(contents), data_only=True, read_only=False)
    except Exception as e:
        return JSONResponse(status_code=400, content={"detail": f"Excel dosyası okunamadı: {e}"})

    schema = _detect_excel_schema(wb)
    if schema == "unknown":
        return JSONResponse(status_code=422, content={
            "detail": "Dosya yapısı tanınamadı. Hammadde, Etiket veya Standart formatlardan biri olmalı.",
            "detected_schema": schema,
        })

    # ── Currently Hammadde is the production-ready parser; others return preview only.
    if schema != "hammadde":
        return JSONResponse(status_code=422, content={
            "detail": f"Bu sürümde sadece Hammadde formatı işlenebiliyor. Algılanan: '{schema}'. "
                      "Etiket dosyaları için ayrı içe aktarım akışı yakında eklenecektir.",
            "detected_schema": schema,
        })

    parsed = _parse_hammadde_workbook(wb)
    if not parsed:
        return JSONResponse(status_code=422, content={"detail": "Sayfalardan veri okunamadı."})

    # ── Pre-flight analysis: classify each row as create vs update ──────────
    name_index = {i.name.strip().upper(): i for i in db.query(Item).filter(Item.is_active == True).all()}
    supp_index = {s.name.strip().upper(): s for s in db.query(Supplier).filter(Supplier.is_active == True).all()}

    plan = {
        "schema":               schema,
        "sheets_processed":     len(wb.sheetnames),
        "rows_parsed":          len(parsed),
        "items_to_create":      0,
        "items_to_update":      0,
        "suppliers_to_create":  0,
        "warnings":             [],
        "preview":              [],
    }

    new_supplier_keys = set()
    for row in parsed:
        key = row["name"].strip().upper()
        existing = name_index.get(key)
        action = "update" if existing else "create"
        if action == "create":
            plan["items_to_create"] += 1
        else:
            plan["items_to_update"] += 1

        if row["supplier"]:
            sk = row["supplier"].strip().upper()
            if sk not in supp_index and sk not in new_supplier_keys:
                new_supplier_keys.add(sk)
                plan["suppliers_to_create"] += 1

        if row["quantity"] <= 0:
            plan["warnings"].append(f"Sıfır miktar: '{row['name']}' (sayfa: {row['sheet']}, ham değer: '{row['raw_qty']}')")

        if len(plan["preview"]) < 25:   # Cap preview for response size
            plan["preview"].append({
                "sheet":    row["sheet"],
                "name":     row["name"],
                "quantity": row["quantity"],
                "unit":     row["unit"],
                "supplier": row["supplier"] or "—",
                "action":   action,
            })

    # ── If dry run, stop here ──
    if not commit:
        plan["committed"] = False
        return plan

    # ── COMMIT: idempotent upserts ──────────────────────────────────────────
    items_created    = 0
    items_updated    = 0
    suppliers_added  = 0
    inv_rows_created = 0
    txs_logged       = 0

    try:
        for row in parsed:
            key = row["name"].strip().upper()
            supplier_obj = None
            if row["supplier"]:
                sk = row["supplier"].strip().upper()
                supplier_obj = supp_index.get(sk)
                if not supplier_obj:
                    supplier_obj = Supplier(name=row["supplier"], is_active=True)
                    db.add(supplier_obj)
                    db.flush()
                    supp_index[sk] = supplier_obj
                    suppliers_added += 1

            existing = name_index.get(key)
            if existing:
                # Add stock to existing item
                existing.current_stock = round((existing.current_stock or 0) + row["quantity"], 6)
                if supplier_obj and not existing.supplier_id:
                    existing.supplier_id = supplier_obj.id
                item = existing
                items_updated += 1
            else:
                item = Item(
                    name=row["name"],
                    category="Hammadde",
                    unit=row["unit"],
                    current_stock=row["quantity"],
                    cost_price=0.0,
                    supplier_id=supplier_obj.id if supplier_obj else None,
                    is_active=True,
                )
                db.add(item)
                db.flush()
                name_index[key] = item
                items_created += 1

            # Inventory + Transaction (only when there's actual stock)
            if row["quantity"] > 0:
                lot_no = f"XLS-{_dt.utcnow().strftime('%Y%m%d')}-{item.id}"
                db.add(Inventory(
                    item_id=item.id,
                    supplier_id=supplier_obj.id if supplier_obj else None,
                    lot_number=lot_no,
                    quantity=row["quantity"],
                    status="APPROVED",
                    received_by=actor,
                ))
                inv_rows_created += 1

                db.add(Transaction(
                    item_id=item.id,
                    lot_number=lot_no,
                    transaction_type="Input",
                    quantity=row["quantity"],
                    notes=f"Excel Bulk Import — Sheet: {row['sheet']} · Raw: '{row['raw_qty']}'",
                    performed_by=actor,
                ))
                txs_logged += 1

        db.commit()
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": f"İçe aktarım sırasında hata: {e}"})

    plan.update({
        "committed":       True,
        "items_created":   items_created,
        "items_updated":   items_updated,
        "suppliers_added": suppliers_added,
        "inventory_rows":  inv_rows_created,
        "transactions":    txs_logged,
    })
    return plan
