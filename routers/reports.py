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
from typing import Optional, List

from fastapi import APIRouter, Depends, Query, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from database import (
    to_tr,
    get_db, Item, Supplier, Recipe, Transaction, ProductionHistory, Inventory,
    StockSnapshot, SupplierPrice, StockOrderFlag,
)
from core.permissions import require_permission
from core.auth import require_role
from core.snapshots import compute_stock_at, snapshot_exists
from core.domain import active_domain
from core.audit import log_admin_event

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


def _rows_from_snapshot(db: Session, year: int, month: int, category_key: str,
                        domain: str = "cosmetics") -> list[dict]:
    """Dondurulmuş `stock_snapshot` satırlarından kategori bazlı rapor satırları."""
    rows = (
        db.query(StockSnapshot)
        .filter(StockSnapshot.year == year, StockSnapshot.month == month,
                StockSnapshot.domain == domain,
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


def _rows_live(db: Session, eom: _dt.datetime, category_key: str,
               domain: str = "cosmetics") -> list[dict]:
    """Snapshot yoksa: `core.snapshots.compute_stock_at` ile canlı rekonstrüksiyon."""
    stocks, items = compute_stock_at(db, eom, Item.domain == domain, *_category_filter(category_key))
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
    domain: str = Depends(active_domain),
):
    """
    Verilen ay sonunda mevcut stok dökümü.  Tarihçe rekonstrüksiyonu —
    Transaction'lardaki signed delta'ların kümülatif toplamı.  Aktif panele
    (domain) göre süzülür.

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
    use_snapshot = snapshot_exists(db, year, month, domain)

    result = []
    for key, label in sections:
        rows = (_rows_from_snapshot(db, year, month, key, domain)
                if use_snapshot else
                _rows_live(db, eom, key, domain))
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
        "generated_at": to_tr(now).strftime("%d.%m.%Y %H:%M"),
        # Stok verisinin hangi ana kadar olduğu (ay sonu ya da bugün).
        "as_of":        to_tr(eom).strftime("%d.%m.%Y %H:%M"),
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
    domain: str = Depends(active_domain),
):
    """Aylık stok raporu .xlsx — her kategori ayrı sheet."""
    data = report_monthly_stock(year, month, category, db, _, domain)
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
    domain: str = Depends(active_domain),
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
            Item.domain == domain,
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
    domain: str = Depends(active_domain),
):
    import datetime as _dt
    from sqlalchemy import func
    today = _dt.date.today()
    since = _dt.datetime.combine(today - _dt.timedelta(days=6), _dt.time.min)
    rows = (db.query(ProductionHistory)
            .filter(ProductionHistory.produced_at >= since,
                    ProductionHistory.domain == domain).all())
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


class _OrderFlagIn(BaseModel):
    supplier_id: Optional[int] = None
    quantity: Optional[float] = Field(None, gt=0)
    expected_date: Optional[_dt.date] = None
    note: Optional[str] = Field(None, max_length=300)


@router.get("/reports/stock-gaps")
def stock_gaps_report(
    category: str = Query("hammadde", pattern="^(hammadde|ambalaj|all)$"),
    status: Optional[str] = None,
    include_undefined_min: int = Query(0, ge=0, le=1),
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    """Eksik Hammaddeler — sıfır / kritik altı / sadece numune hammaddeler.

    core/stock_gaps.py TEK KAYNAK. `current_stock<=0 and min_stock_level=0`
    (min. tanımsız) hiçbir alarma girmiyordu — `include_undefined_min=1` ile
    görünür kılınır."""
    from core.stock_gaps import assemble
    statuses = [s.strip() for s in status.split(",") if s.strip()] if status else None
    return assemble(db, domain, category=category, statuses=statuses,
                    include_undefined_min=bool(include_undefined_min), q=q)


@router.post("/reports/stock-gaps/{item_id}/order", status_code=201)
def stock_gaps_order(
    item_id: int,
    data: _OrderFlagIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("inventory", "adjust")),
    domain: str = Depends(active_domain),
):
    """'Sipariş verildi' işaretle — ürün başına en fazla bir açık bayrak
    (yeniden işaretleme eskisini 'manual' kapatır). Gerçek mal kabulünde
    (routers/inventory.py receive_stock / samples/convert) otomatik kapanır."""
    from core.stock_gaps import open_flag
    item = db.query(Item).filter(Item.id == item_id).with_for_update().first()
    if not item or not item.is_active or item.domain != domain:
        return JSONResponse(status_code=404, content={"detail": "Ürün bulunamadı."})
    if data.supplier_id is not None:
        sup = db.query(Supplier).filter(Supplier.id == data.supplier_id).first()
        if not sup or sup.domain != domain:
            return JSONResponse(status_code=400, content={"detail": "Tedarikçi bu panelde değil."})
    actor = current_user.get("full_name") or current_user.get("username") or "—"
    flag = open_flag(db, item, supplier_id=data.supplier_id, quantity=data.quantity,
                     expected_date=data.expected_date, note=data.note, actor=actor, domain=domain)
    log_admin_event(db, request, actor=current_user, action="stock_gap.order",
                    target_type="item", target_id=item.id, target_name=item.name,
                    details={"supplier_id": data.supplier_id, "quantity": data.quantity,
                             "expected_date": str(data.expected_date) if data.expected_date else None})
    db.commit()
    return {"id": flag.id, "message": "Sipariş işaretlendi."}


@router.delete("/reports/stock-gaps/{item_id}/order")
def stock_gaps_order_cancel(
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("inventory", "adjust")),
    domain: str = Depends(active_domain),
):
    from core.stock_gaps import close_open_flags
    item = db.query(Item).filter(Item.id == item_id, Item.domain == domain).first()
    if not item:
        return JSONResponse(status_code=404, content={"detail": "Ürün bulunamadı."})
    actor = current_user.get("full_name") or current_user.get("username") or "—"
    closed = close_open_flags(db, item_id, "manual", actor)
    if not closed:
        return JSONResponse(status_code=404, content={"detail": "Açık sipariş bulunamadı."})
    log_admin_event(db, request, actor=current_user, action="stock_gap.order_cancel",
                    target_type="item", target_id=item.id, target_name=item.name)
    db.commit()
    return {"message": "Sipariş işareti kaldırıldı."}


@router.get("/reports/stock-gaps/export")
def stock_gaps_export(
    format: str = Query("xlsx", pattern="^(xlsx|pdf)$"),
    category: str = Query("hammadde", pattern="^(hammadde|ambalaj|all)$"),
    status: Optional[str] = None,
    include_undefined_min: int = Query(0, ge=0, le=1),
    q: Optional[str] = None,
    ids: Optional[str] = None,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    """Ekrandaki süzgeçlerle ya da SEÇİLEN kartlarla (`ids`) dışa aktar.

    `ids` verilirse kategori/durum/arama yok sayılır — lab yalnız istediği
    uçucu yağları, yalnız istediği ambalajları ya da ikisini birlikte tek
    Excel'de alabilsin diye (10.09.2026 talebi)."""
    from core.stock_gaps import assemble, build_workbook, render_pdf, export_filename
    from core.supplier_prices import prices_for_items
    from core.delivery_note import content_disposition
    statuses = [s.strip() for s in status.split(",") if s.strip()] if status else None
    picked = None
    if ids:
        try:
            picked = [int(x) for x in ids.split(",") if x.strip()][:2000]
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Geçersiz ürün seçimi."})
        if not picked:
            return JSONResponse(status_code=400, content={"detail": "Seçili ürün yok."})
    report = assemble(db, domain, category=category, statuses=statuses,
                      include_undefined_min=bool(include_undefined_min), q=q,
                      item_ids=picked)
    try:
        if format == "pdf":
            content = render_pdf(report)
            media = "application/pdf"
        else:
            order_ids = [r["item_id"] for r in report["rows"] if r.get("order")]
            prices = prices_for_items(db, order_ids, domain)
            content = build_workbook(report, prices=prices)
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "Rapor üretilemedi."})
    fname = export_filename("secim" if picked else category, format)
    return StreamingResponse(
        io.BytesIO(content), media_type=media,
        headers={"Content-Disposition": content_disposition(fname, inline=(format == "pdf"))},
    )


# ─── Dashboard Stats ─────────────────────────────────────────────────────────

@router.get("/dashboard/stats")
def dashboard_stats(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    total_items     = db.query(Item).filter(Item.is_active == True, Item.domain == domain).count()
    total_suppliers = db.query(Supplier).filter(Supplier.is_active == True, Supplier.domain == domain).count()
    total_recipes   = db.query(Recipe).filter(Recipe.is_active == True, Recipe.domain == domain).count()
    zero_stock_raw_count = (
        db.query(Item)
        .filter(Item.is_active == True, Item.domain == domain,
                Item.current_stock <= 0, Item.category.notin_(("Ambalaj", "Bitmiş Ürün")))
        .count()
    )

    # ── Critical stock: items at or below min level ────────────────────────
    critical_items_raw = (
        db.query(Item)
        .filter(
            Item.is_active == True,
            Item.min_stock_level > 0,
            Item.current_stock <= Item.min_stock_level,
            Item.domain == domain,
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
        .join(Item, Item.id == Transaction.item_id)
        .filter(Item.domain == domain)
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
            "timestamp":        to_tr(t.timestamp).strftime("%d.%m.%Y %H:%M") if t.timestamp else "",
        }
        for t in recent_txs
    ]

    # ── Recent production (last 5) ─────────────────────────────────────────
    recent_prod = (
        db.query(ProductionHistory)
        .filter(ProductionHistory.domain == domain)
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
        "zero_stock_raw_count": zero_stock_raw_count,
        "recent_transactions":  recent_transactions,
        "recent_productions": [
            {
                "recipe_name":       r.recipe_name,
                "target_item_name":  r.target_item_name,
                "produced_quantity": r.produced_quantity,
                "produced_at":       to_tr(r.produced_at).strftime("%d.%m.%Y %H:%M") if r.produced_at else "",
            }
            for r in recent_prod
        ],
    }


# ─── Üretim Stok Analizi (what-if simülasyonu) ──────────────────────────────
# Lab ekibi: seçilen ürünlerden N'er adet üretsek malzeme yeter mi? Excel iner.

class _PlanRequest(BaseModel):
    recipe_ids: List[int] = Field(..., min_length=1, max_length=500)
    quantity:   float     = Field(..., gt=0, le=1_000_000)
    language:   Optional[str] = "TR"


@router.get("/reports/production-plan/products")
def production_plan_products(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    """Aktif panelde reçetesi olan ürünler + reçetesi olmayan bitmiş ürünler (uyarı için)."""
    from core.production_sim import list_plan_products, list_recipeless_products
    return {"products": list_plan_products(db, domain),
            "recipeless": list_recipeless_products(db, domain)}


@router.post("/reports/production-plan")
def production_plan(
    data: _PlanRequest,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    """Seçilen ürünlerden quantity'şer adet üretim senaryosu — JSON rapor."""
    from core.production_sim import simulate
    return simulate(db, data.recipe_ids, data.quantity, data.language or "TR", domain)


@router.post("/reports/production-plan/export")
def production_plan_export(
    data: _PlanRequest,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    """Aynı senaryoyu Excel olarak indir (Satın Alma tedarikçi/fiyatla + Reçetesiz Ürünler sayfası)."""
    from core.production_sim import simulate, build_workbook, list_recipeless_products
    from core.supplier_prices import prices_for_items
    rep = simulate(db, data.recipe_ids, data.quantity, data.language or "TR", domain)
    if not rep["materials"]:
        return JSONResponse(status_code=400, content={"detail": "Seçilen ürünlerde malzeme bulunamadı."})
    purchase_ids = [m["item_id"] for m in rep["purchase"] if m.get("item_id")]
    prices = prices_for_items(db, purchase_ids, domain)
    recipeless = list_recipeless_products(db, domain)
    try:
        content = build_workbook(rep, title_suffix=f"{rep['summary']['products']} ürün × {int(data.quantity)}",
                                 prices=prices, recipeless=recipeless)
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "Excel üretilemedi."})
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="uretim_stok_analizi.xlsx"'},
    )


# ─── Üretim geçmişi (ürün / marka / ürün grubu bazında) ─────────────────────

@router.get("/reports/production-history")
def production_history_report(
    start:  Optional[_dt.date] = Query(None),
    end:    Optional[_dt.date] = Query(None),
    brand:  Optional[str] = Query(None, max_length=300),
    q:      Optional[str] = Query(None, max_length=100),
    group:  str = Query("product", regex="^(product|brand|category)$"),
    format: str = Query("json", regex="^(json|xlsx)$"),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    """ProductionHistory toplamları — tarihler TR-yerel gün (varsayılan son 12 ay).

    `brand` virgüllü olabilir; `format=xlsx` → 'Özet' + 'Aylık' sayfalı Excel.
    """
    from core import production_history_report as phr
    # Varsayılan başlangıç hesabından (end − 364 gün) ÖNCE: uç tarih OverflowError → 500
    if not (phr.date_in_range(start) and phr.date_in_range(end)):
        return JSONResponse(status_code=400, content={
            "detail": f"Tarihler {phr.MIN_DATE:%d.%m.%Y} – {phr.MAX_DATE:%d.%m.%Y} aralığında olmalı."})
    d_start, d_end = phr.default_range()
    start = start or (d_start if end is None else end - (d_end - d_start))
    end = end or d_end
    if start > end:
        return JSONResponse(status_code=400, content={"detail": "Başlangıç tarihi bitiş tarihinden sonra olamaz."})
    if (end - start).days > 3660:
        return JSONResponse(status_code=400, content={"detail": "Tarih aralığı en fazla 10 yıl olabilir."})
    rep = phr.build_report(db, domain, start, end, brand=brand, q=q, group=group)
    rep["note"] = phr.since_note(rep["first_record"])
    if format == "json":
        return rep
    try:
        content = phr.build_workbook(rep)
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "Excel üretilemedi."})
    fname = f"uretim_gecmisi_{start.isoformat()}_{end.isoformat()}_{group}.xlsx"
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ─── Tedarikçi fiyat listesi (satın alma raporunu besler) ───────────────────
_FINANCE = ["SuperAdmin", "Manager"]


@router.get("/supplier-prices")
def list_supplier_prices(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    """Malzemeye göre gruplu tedarikçi fiyat listesi (görüntüleme paneli)."""
    rows = db.query(SupplierPrice).filter(SupplierPrice.domain == domain).all()
    item_ids = {r.item_id for r in rows}
    meta = {}
    if item_ids:
        for it in db.query(Item).filter(Item.id.in_(item_ids)).all():
            meta[it.id] = {"name": it.name, "unit": it.unit or "", "category": it.category or ""}
    groups: dict = {}
    for r in rows:
        g = groups.setdefault(r.item_id, {
            "item_id": r.item_id,
            "material": meta.get(r.item_id, {}).get("name", "—"),
            "unit": meta.get(r.item_id, {}).get("unit", ""),
            "category": meta.get(r.item_id, {}).get("category", ""),
            "suppliers": [],
        })
        g["suppliers"].append({
            "id": r.id,
            "supplier_name": r.supplier_name or (r.supplier.name if r.supplier else "—"),
            "package_size": r.package_size,
            "unit_price": r.unit_price,
            "currency": r.currency,
            "price_unit": r.price_unit,
            "source_label": r.source_label,
            "quoted_at": r.quoted_at.isoformat() if r.quoted_at else None,
            "matched": r.supplier_id is not None,
        })
    out = sorted(groups.values(), key=lambda g: (g["material"] or "").lower())
    for g in out:
        g["suppliers"].sort(key=lambda s: (s["unit_price"] is None, s["unit_price"] or 0.0))
    return {"items": out, "total_items": len(out), "total_prices": len(rows)}


@router.post("/supplier-prices/import")
async def import_supplier_prices(
    request: Request,
    file: UploadFile = File(...),
    currency: str = Form("USD"),
    price_unit: str = Form("kg"),
    label: Optional[str] = Form(None),
    quoted_at: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(_FINANCE)),
    domain: str = Depends(active_domain),
):
    """Işık Hanım'ın 'Stok Son Durum' Excel'ini içe aktarır (malzeme başına eski satırları değiştirir).

    Form alanları fiyat temelini belirler: `currency` (USD|EUR|TRY, vars. USD),
    `price_unit` (kg|l, vars. kg — listenin ağırlık/hacim temeli; adet birimli
    malzemeler her zaman 'adet', 'adet' seçeneği bu yüzden yok), `label` (kaynak etiketi, boşsa dosya adı), `quoted_at`
    (YYYY-MM-DD liste/teklif tarihi).  Audit: `supplier_prices.import`.
    """
    from core import supplier_prices as SP
    if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
        return JSONResponse(status_code=400, content={"detail": "Lütfen .xlsx dosyası yükleyin."})
    currency = (currency or "USD").strip().upper()
    if currency not in SP.CURRENCIES:
        return JSONResponse(status_code=400, content={"detail": "Para birimi USD, EUR ya da TRY olmalı."})
    price_unit = (price_unit or "kg").strip().lower()
    if price_unit not in SP.LIST_PRICE_UNITS:
        return JSONResponse(status_code=400, content={
            "detail": "Fiyat birimi kg ya da l olmalı (adet birimli malzemeler zaten adet başına yazılır)."})
    label = SP.clean_name(label)
    if label and len(label) > SP.SOURCE_LABEL_MAX:
        return JSONResponse(status_code=400, content={
            "detail": f"Etiket en fazla {SP.SOURCE_LABEL_MAX} karakter olabilir."})
    quoted = None
    if quoted_at and quoted_at.strip():
        try:
            quoted = _dt.date.fromisoformat(quoted_at.strip())
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Geçersiz liste tarihi."})
    source_label = label or SP.clean_name(file.filename)
    data = await file.read()
    try:
        rows = SP.parse_stok_son_durum(data)
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "Excel okunamadı — beklenen 'Stok Son Durum' düzeni mi?"})
    if not rows:
        return JSONResponse(status_code=400, content={"detail": "Dosyada veri satırı bulunamadı."})
    summary = SP.import_prices(db, rows, domain, currency=currency, price_unit=price_unit,
                               source_label=source_label, quoted_at=quoted)
    log_admin_event(db, request, actor=current_user, action="supplier_prices.import",
                    target_type="supplier_prices", target_name=summary["source_label"],
                    details={"domain": domain, "filename": file.filename,
                             "currency": summary["currency"], "price_unit": summary["price_unit"],
                             "quoted_at": summary["quoted_at"],
                             "items_updated": summary["items_updated"],
                             "prices_inserted": summary["prices_inserted"],
                             "unmatched_materials": len(summary["unmatched_materials"]),
                             "unmatched_suppliers": len(summary["unmatched_suppliers"])})
    return summary


@router.delete("/supplier-prices/{price_id}")
def delete_supplier_price(
    price_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_role(_FINANCE)),
    domain: str = Depends(active_domain),
):
    """Tek bir tedarikçi fiyat satırını siler."""
    row = db.query(SupplierPrice).filter(
        SupplierPrice.id == price_id, SupplierPrice.domain == domain
    ).first()
    if not row:
        return JSONResponse(status_code=404, content={"detail": "Kayıt bulunamadı."})
    db.delete(row)
    db.commit()
    return {"ok": True}
