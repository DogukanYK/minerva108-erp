# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Reports router — dashboard stats + reporting data feeds.
"""
import calendar
import datetime as _dt
import io
import re
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from database import (
    get_db, Item, Supplier, Recipe, Transaction, ProductionHistory, Inventory,
    StockSnapshot,
)
from core.permissions import require_permission
from core.snapshots import compute_stock_at, snapshot_exists

router = APIRouter(prefix="/api", tags=["reports"])


# ─── Aylık Stok Raporu — kategori bazlı, ay sonu mevcut stok ───────────────

# UI'da görünen 4 kategori → DB'de Item.category + pkg_type kombinasyonu
def _category_filter(category_key: str):
    """Reports UI'daki kategori anahtarını Item filtresine çevir."""
    k = (category_key or "all").lower()
    if k == "hammadde":
        return (Item.category == "Hammadde",)
    if k == "ambalaj":   # etiket hariç
        return (Item.category == "Ambalaj",
                (Item.pkg_type.is_(None)) | (Item.pkg_type != "etiket"))
    if k == "etiket":
        return (Item.category == "Ambalaj", Item.pkg_type == "etiket")
    if k == "finished":
        return (Item.category == "Bitmiş Ürün",)
    return ()


def _month_window(year: int, month: int) -> tuple[_dt.datetime, _dt.datetime]:
    """Verilen yıl/ay için (ayın 1'i 00:00, ayın son günü 23:59:59.999999)."""
    if not (1 <= month <= 12) or year < 2000 or year > 2100:
        raise ValueError("Geçersiz yıl/ay")
    first = _dt.datetime(year, month, 1, 0, 0, 0)
    last_day = calendar.monthrange(year, month)[1]
    last  = _dt.datetime(year, month, last_day, 23, 59, 59, 999999)
    return first, last


def _snapshot_category_filter(category_key: str):
    """`_category_filter`'ın StockSnapshot satırı için karşılığı."""
    k = (category_key or "all").lower()
    if k == "hammadde":
        return (StockSnapshot.category == "Hammadde",)
    if k == "ambalaj":   # etiket hariç
        return (StockSnapshot.category == "Ambalaj",
                (StockSnapshot.pkg_type.is_(None)) | (StockSnapshot.pkg_type != "etiket"))
    if k == "etiket":
        return (StockSnapshot.category == "Ambalaj", StockSnapshot.pkg_type == "etiket")
    if k == "finished":
        return (StockSnapshot.category == "Bitmiş Ürün",)
    return ()


def _rows_from_snapshot(db: Session, year: int, month: int, category_key: str) -> list[dict]:
    """Dondurulmuş `stock_snapshot` satırlarından kategori bazlı rapor satırları."""
    rows = (
        db.query(StockSnapshot)
        .filter(StockSnapshot.year == year, StockSnapshot.month == month,
                *_snapshot_category_filter(category_key))
        .all()
    )
    out = []
    for s in rows:
        stock = float(s.stock or 0.0)
        if abs(stock) < 1e-9:
            continue
        out.append({
            "item_id":  s.item_id,
            "name":     s.item_name or "—",
            "unit":     s.unit or "",
            "pkg_type": s.pkg_type or "",
            "language": "",
            "stock":    round(stock, 4),
        })
    out.sort(key=lambda r: (r["name"] or "").lower())
    return out


def _rows_live(db: Session, eom: _dt.datetime, category_key: str) -> list[dict]:
    """Snapshot yoksa: `core.snapshots.compute_stock_at` ile canlı rekonstrüksiyon."""
    stocks, items = compute_stock_at(db, eom, *_category_filter(category_key))
    out = []
    for it in items:
        stock = float(stocks.get(it.id, 0.0))
        if abs(stock) < 1e-9:
            continue
        out.append({
            "item_id":  it.id,
            "name":     it.name,
            "unit":     it.unit or "",
            "pkg_type": it.pkg_type or "",
            "language": it.language or "",
            "stock":    round(stock, 4),
        })
    out.sort(key=lambda r: r["name"].lower())
    return out


@router.get("/reports/monthly-stock")
def report_monthly_stock(
    year:     int = Query(..., ge=2000, le=2100),
    month:    int = Query(..., ge=1,    le=12),
    category: str = Query("all", regex="^(all|hammadde|ambalaj|etiket|finished)$"),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
):
    """
    Verilen ay sonunda mevcut stok dökümü.  Tarihçe rekonstrüksiyonu —
    Transaction'lardaki signed delta'ların kümülatif toplamı.

    category: all | hammadde | ambalaj | etiket | finished
    """
    try:
        _, eom_raw = _month_window(year, month)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})

    # Veri kesim tarihi = ayın sonu VEYA şimdi (hangisi önce).  Mevcut ay
    # seçilince ayın sonu henüz GELMEMİŞTİR; gelecekteki bir tarihe kadar
    # rekonstrüksiyon anlamsız — bugüne kadar olan stok gösterilir.
    now = _dt.datetime.utcnow()
    eom = min(eom_raw, now)

    sections = (
        [("hammadde", "Hammadde"),
         ("ambalaj",  "Ambalaj"),
         ("etiket",   "Etiket"),
         ("finished", "Bitmiş Ürün")]
        if category == "all" else
        [(category, {"hammadde": "Hammadde", "ambalaj": "Ambalaj",
                     "etiket": "Etiket", "finished": "Bitmiş Ürün"}[category])]
    )

    # Geçmiş bir ay için dondurulmuş snapshot varsa onu kullan — kesin ve
    # transaction değişikliklerinden etkilenmez.  Yoksa canlı rekonstrüksiyon.
    use_snapshot = snapshot_exists(db, year, month)

    result = []
    for key, label in sections:
        rows = (_rows_from_snapshot(db, year, month, key)
                if use_snapshot else
                _rows_live(db, eom, key))
        result.append({
            "category": key,
            "label":    label,
            "rows":     rows,
            "row_count": len(rows),
            "total":    round(sum(r["stock"] for r in rows), 4),
        })

    return {
        "year":         year,
        "month":        month,
        # Verinin kaynağı: dondurulmuş snapshot mu, canlı hesap mı?
        "source":       "snapshot" if use_snapshot else "canlı",
        # Raporun fiilen oluşturulduğu an — "Rapor tarihi" olarak gösterilir.
        "generated_at": now.strftime("%d.%m.%Y %H:%M"),
        # Stok verisinin hangi ana kadar olduğu (ay sonu ya da bugün).
        "as_of":        eom.strftime("%d.%m.%Y %H:%M"),
        "sections":     result,
        "category":     category,
    }


@router.get("/reports/monthly-stock/export")
def report_monthly_stock_export(
    year:     int = Query(..., ge=2000, le=2100),
    month:    int = Query(..., ge=1,    le=12),
    category: str = Query("all", regex="^(all|hammadde|ambalaj|etiket|finished)$"),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
):
    """Aylık stok raporu .xlsx — her kategori ayrı sheet."""
    data = report_monthly_stock(year, month, category, db, _)
    if isinstance(data, JSONResponse):
        return data

    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    bold  = Font(bold=True, color="FFFFFF")
    fill  = PatternFill("solid", fgColor="232E6E")
    box   = Border(*([Side(style="thin", color="999999")] * 4))
    title_font = Font(bold=True, size=12)

    month_label = ["", "Ocak","Şubat","Mart","Nisan","Mayıs","Haziran",
                   "Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"][month]

    for sec in data["sections"]:
        ws = wb.create_sheet(title=sec["label"][:31])
        ws["A1"] = f"{sec['label']} — {month_label} {year} stok durumu"
        ws["A1"].font = title_font
        ws["A2"] = (f"Rapor tarihi: {data['generated_at']}  ·  "
                    f"Stok kesim: {data['as_of']} itibarıyla  ·  "
                    f"{sec['row_count']} kalem  ·  toplam {sec['total']}")
        ws["A2"].font = Font(italic=True, color="6b7280")

        headers = ["#", "Ürün Adı", "Birim", "Alt-Tip", "Dil", "Stok"]
        for ci, h in enumerate(headers, 1):
            c = ws.cell(row=4, column=ci, value=h)
            c.font = bold; c.fill = fill; c.border = box
            c.alignment = Alignment(horizontal="center")

        for ri, r in enumerate(sec["rows"], start=5):
            ws.cell(row=ri, column=1, value=ri - 4).border = box
            ws.cell(row=ri, column=2, value=r["name"]).border = box
            ws.cell(row=ri, column=3, value=r["unit"]).border = box
            ws.cell(row=ri, column=4, value=r["pkg_type"]).border = box
            ws.cell(row=ri, column=5, value=r["language"]).border = box
            ws.cell(row=ri, column=6, value=r["stock"]).border = box

        # TOPLAM satırı
        total_row = 5 + sec["row_count"]
        ws.cell(row=total_row, column=2, value="TOPLAM").font = Font(bold=True)
        ws.cell(row=total_row, column=6, value=sec["total"]).font = Font(bold=True)

        ws.column_dimensions["A"].width = 5
        ws.column_dimensions["B"].width = 50
        ws.column_dimensions["C"].width = 10
        ws.column_dimensions["D"].width = 12
        ws.column_dimensions["E"].width = 8
        ws.column_dimensions["F"].width = 14

    if not wb.worksheets:
        wb.create_sheet(title="Boş")["A1"] = "Bu ay için kayıt yok."

    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    fname = f"stok_raporu_{year}-{month:02d}_{category}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


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
