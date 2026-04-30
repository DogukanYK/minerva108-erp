"""
Reports router — dashboard stats + reporting data feeds.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from database import (
    get_db, Item, Supplier, Recipe, Transaction, ProductionHistory, Inventory,
)
from core.permissions import require_permission

router = APIRouter(prefix="/api", tags=["reports"])


# ─── Reports Endpoints ───────────────────────────────────────────────────────

@router.get("/reports/top-usage")
def report_top_usage(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
):
    import datetime as _dt
    from sqlalchemy import func
    since = _dt.datetime.utcnow() - _dt.timedelta(days=30)
    rows = (
        db.query(
            Item.id,
            Item.name,
            Item.unit,
            func.sum(Transaction.quantity).label("total_used"),
        )
        .join(Transaction, Transaction.item_id == Item.id)
        .filter(
            Transaction.transaction_type == "Output",
            Transaction.timestamp >= since,
        )
        .group_by(Item.id)
        .order_by(func.sum(Transaction.quantity).desc())
        .limit(5)
        .all()
    )
    return [
        {"item_id": r.id, "name": r.name, "unit": r.unit, "total_used": round(float(r.total_used), 4)}
        for r in rows
    ]


@router.get("/reports/production-trends")
def report_production_trends(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
):
    import datetime as _dt
    from sqlalchemy import func
    today = _dt.date.today()
    since = _dt.datetime.combine(today - _dt.timedelta(days=6), _dt.time.min)
    rows = db.query(ProductionHistory).filter(ProductionHistory.produced_at >= since).all()
    # Gün gün topla
    day_map = {}
    for i in range(7):
        d = (today - _dt.timedelta(days=6 - i)).isoformat()
        day_map[d] = {"date": d, "total_quantity": 0.0, "count": 0}
    for r in rows:
        d = r.produced_at.date().isoformat()
        if d in day_map:
            day_map[d]["total_quantity"] = round(day_map[d]["total_quantity"] + r.produced_quantity, 4)
            day_map[d]["count"] += 1
    return list(day_map.values())


@router.get("/reports/low-stock-alert")
def report_low_stock_alert(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
):
    items = (
        db.query(Item)
        .filter(
            Item.is_active == True,
            Item.min_stock_level > 0,
            Item.current_stock <= Item.min_stock_level,
        )
        .order_by(Item.current_stock.asc())
        .all()
    )
    result = []
    for item in items:
        # Son tedarikçiyi transactions üzerinden bul
        last_inv = (
            db.query(Inventory)
            .filter(Inventory.item_id == item.id, Inventory.supplier_id != None)
            .order_by(Inventory.id.desc())
            .first()
        )
        supplier_name = last_inv.supplier.name if last_inv and last_inv.supplier else "—"
        result.append({
            "item_id": item.id,
            "name": item.name,
            "category": item.category or "—",
            "unit": item.unit,
            "current_stock": item.current_stock,
            "min_stock_level": item.min_stock_level,
            "deficit": round(item.min_stock_level - item.current_stock, 4),
            "last_supplier": supplier_name,
        })
    return result


# ─── Dashboard Stats ─────────────────────────────────────────────────────────

@router.get("/dashboard/stats")
def dashboard_stats(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
):
    total_items     = db.query(Item).filter(Item.is_active == True).count()
    total_suppliers = db.query(Supplier).filter(Supplier.is_active == True).count()
    total_recipes   = db.query(Recipe).filter(Recipe.is_active == True).count()

    # ── Critical stock: items at or below min level ────────────────────────
    critical_items_raw = (
        db.query(Item)
        .filter(
            Item.is_active == True,
            Item.min_stock_level > 0,
            Item.current_stock <= Item.min_stock_level,
        )
        .order_by(Item.current_stock.asc())
        .all()
    )
    critical_stock_list = [
        {
            "id":              i.id,
            "name":            i.name,
            "sku":             i.sku or "—",
            "category":        i.category or "—",
            "unit":            i.unit,
            "current_stock":   round(i.current_stock, 4),
            "min_stock_level": round(i.min_stock_level, 4),
            "deficit":         round(i.min_stock_level - i.current_stock, 4),
        }
        for i in critical_items_raw
    ]

    # ── Recent transactions (last 5) ───────────────────────────────────────
    recent_txs = (
        db.query(Transaction)
        .order_by(Transaction.id.desc())
        .limit(5)
        .all()
    )
    recent_transactions = [
        {
            "id":               t.id,
            "item_name":        t.item.name if t.item else "—",
            "transaction_type": t.transaction_type,
            "quantity":         t.quantity,
            "notes":            (t.notes or "")[:90],   # truncate for display
            "timestamp":        t.timestamp.strftime("%d.%m.%Y %H:%M") if t.timestamp else "",
        }
        for t in recent_txs
    ]

    # ── Recent production (last 5) ─────────────────────────────────────────
    recent_prod = (
        db.query(ProductionHistory)
        .order_by(ProductionHistory.id.desc())
        .limit(5)
        .all()
    )

    return {
        "total_items":          total_items,
        "total_suppliers":      total_suppliers,
        "total_recipes":        total_recipes,
        "critical_stock_count": len(critical_stock_list),
        "critical_stock_list":  critical_stock_list,
        "recent_transactions":  recent_transactions,
        "recent_productions": [
            {
                "recipe_name":       r.recipe_name,
                "target_item_name":  r.target_item_name,
                "produced_quantity": r.produced_quantity,
                "produced_at":       r.produced_at.strftime("%d.%m.%Y %H:%M") if r.produced_at else "",
            }
            for r in recent_prod
        ],
    }
