# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Eksik Hammaddeler raporu — TEK KAYNAK (core/production_sim.py kalıbı).

Sistemde sıfır stoğu listeleyen hiçbir ekran yoktu: tüm uyarılar (dashboard,
eski /api/reports/low-stock-alert, push bildirimleri) tek koşula bağlıydı —
`min_stock_level > 0 and current_stock <= min_stock_level`. Min. stoğu
tanımsız (0, DB varsayılanı) bir hammadde stoğu bitse bile hiçbir yerde
görünmüyordu (09.09.2026 tespiti — bkz. CLAUDE.md "Eksik Hammaddeler").

  classify(current, min, sample_qty) -> 'sifir'|'numune'|'kritik'|None
  assemble(db, domain, ...)          -> rapor dict'i (JSON + Excel + PDF ortak)
  close_open_flags / open_flag       -> "sipariş verildi" bayrağı yaşam döngüsü
  build_workbook / render_pdf        -> dışa aktarma

DEĞİŞMEZLER
───────────
`Item.current_stock` TEK OTORİTE — `Inventory` toplanmaz (stok_lots.py'deki
aynı kural). Numune miktarı yalnız `Inventory.is_sample=True, quantity>0`
lotlarından; numune STOK DEĞİLDİR (24.08.2026 kuralı), bu yüzden ayrı bir
durum (`numune`) — `current_stock<=0` olsa da bir şey elde var demektir.
Sıfır stok + min>0 HER ZAMAN 'sifir' kalır, asla 'kritik' değildir (rank
sırası bunu garanti eder) — "elimizde hiç yok" ile "azaldı" karıştırılmaz.
"""
from datetime import date, datetime
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from database import Inventory, Item, Recipe, RecipeIngredient, StockOrderFlag, Supplier, Transaction, to_tr
from core.items_report import tr_key
from core.supplier_prices import normalize

STATUS_RANK = {"sifir": 0, "numune": 1, "kritik": 2, "min_tanimsiz": 3, "yeterli": 4}
STATUS_LABELS = {
    "sifir": "Sıfır stok",
    "numune": "Sadece numune",
    "kritik": "Kritik altı",
    "min_tanimsiz": "Min. tanımsız",
    "yeterli": "Yeterli",
}
CATEGORIES = ("hammadde", "ambalaj", "all")
_NOT_HAMMADDE = ("Ambalaj", "Bitmiş Ürün")
EPS = 1e-9


def category_filter(category: str):
    """UI kategori anahtarını Item filtre koşullarına çevir (Ürünler sekmesiyle
    aynı negasyon — templates/items.html TABS['hammadde'])."""
    k = (category or "hammadde").lower()
    if k == "ambalaj":
        return (Item.category == "Ambalaj",)
    if k == "all":
        return ()
    return (Item.category.notin_(_NOT_HAMMADDE) | Item.category.is_(None),)


def classify(current: float, min_level: float, sample_qty: float) -> Optional[str]:
    current = float(current or 0.0)
    min_level = float(min_level or 0.0)
    sample_qty = float(sample_qty or 0.0)
    if current <= EPS:
        return "numune" if sample_qty > EPS else "sifir"
    if min_level > EPS and current <= min_level + EPS:
        return "kritik"
    return None


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status or "—")


# ─── Rapor derleme ────────────────────────────────────────────────────────────

def assemble(db: Session, domain: str, *, category: str = "hammadde",
             statuses: Optional[List[str]] = None, include_undefined_min: bool = False,
             q: Optional[str] = None, item_ids: Optional[List[int]] = None) -> dict:
    """Rapor derle.

    `item_ids` verilirse SEÇİM MODU: kategori/durum/arama süzgeçleri yok sayılır,
    tam olarak seçilen kartlar döner (kullanıcı hammadde + ambalajı birlikte tek
    Excel'e alabilsin diye).  Seçilen bir kalem bu arada stoklanmışsa satır
    düşmez, `yeterli` durumuyla görünür — "12 seçtim, 12 satır aldım".
    """
    category = (category or "hammadde").lower()
    if category not in CATEGORIES:
        category = "hammadde"
    picked = [int(i) for i in item_ids] if item_ids else None

    # (1) aday ürünler — aktif + domain + kategori (seçim modunda: yalnız seçilenler)
    q_items = (
        db.query(Item)
        .options(joinedload(Item.supplier))
        .filter(Item.is_active == True, Item.domain == domain)  # noqa: E712
    )
    q_items = (q_items.filter(Item.id.in_(picked)) if picked
               else q_items.filter(*category_filter(category)))
    items = q_items.all()
    if category == "all" and not picked:
        # Konteyner (parent) kartlar doğası gereği stoksuzdur — "sıfır" gibi
        # görünüp 25 kap satırıyla raporu boğmasın.
        parent_ids = {i.parent_id for i in items if i.parent_id}
        items = [i for i in items if i.id not in parent_ids]

    # (2) numune toplamı — item_id → sample_qty
    sample_rows = (
        db.query(Inventory.item_id, func.sum(Inventory.quantity))
        .filter(Inventory.is_sample == True, Inventory.quantity > 0,  # noqa: E712
                Inventory.domain == domain)
        .group_by(Inventory.item_id)
        .all()
    )
    sample_qty_by_item = {iid: float(qty or 0) for iid, qty in sample_rows}

    # Python'da sınıflandır — min tanımsız olanları da say (durum filtresinden ÖNCE)
    min_undefined = 0
    classified = []   # (item, status)
    for it in items:
        sq = sample_qty_by_item.get(it.id, 0.0)
        st = classify(it.current_stock, it.min_stock_level, sq)
        if st is None:
            undefined_min = ((it.min_stock_level or 0.0) <= EPS and (it.current_stock or 0.0) > EPS)
            if undefined_min:
                min_undefined += 1
            if picked:
                classified.append((it, "min_tanimsiz" if undefined_min else "yeterli"))
            elif undefined_min and include_undefined_min:
                classified.append((it, "min_tanimsiz"))
            continue
        classified.append((it, st))

    if statuses and not picked:
        wanted = {s for s in statuses if s in STATUS_RANK}
        classified = [(it, st) for it, st in classified if st in wanted]

    ids = [it.id for it, _ in classified]

    # (3) numune lot detayı — yalnız aday satırlardan 'numune' olanlar
    sample_lots_by_item: Dict[int, list] = {}
    numune_ids = [it.id for it, st in classified if st == "numune"]
    if numune_ids:
        lots = (
            db.query(Inventory)
            .options(joinedload(Inventory.supplier))
            .filter(Inventory.is_sample == True, Inventory.quantity > 0,  # noqa: E712
                    Inventory.item_id.in_(numune_ids), Inventory.domain == domain)
            .order_by(Inventory.created_at.asc())
            .all()
        )
        for l in lots:
            sample_lots_by_item.setdefault(l.item_id, []).append({
                "lot_number": l.lot_number,
                "quantity": round(float(l.quantity or 0), 4),
                "supplier_name": l.supplier.name if l.supplier else None,
                "expiry_date": l.expiry_date or None,
            })

    # (4) son hareket — item_id → en son Transaction.timestamp
    last_movement_by_item: Dict[int, datetime] = {}
    if ids:
        rows = (
            db.query(Transaction.item_id, func.max(Transaction.timestamp))
            .filter(Transaction.item_id.in_(ids))
            .group_by(Transaction.item_id)
            .all()
        )
        last_movement_by_item = {iid: ts for iid, ts in rows}

    # (5) son tedarikçi — item_id → en son supplier'lı Inventory satırı (N+1 yerine tek sorgu)
    last_supplier_by_item: Dict[int, str] = {}
    if ids:
        sub = (
            db.query(Inventory.item_id, func.max(Inventory.id).label("max_id"))
            .filter(Inventory.item_id.in_(ids), Inventory.supplier_id.isnot(None))
            .group_by(Inventory.item_id)
            .subquery()
        )
        rows = (
            db.query(Inventory.item_id, Supplier.name)
            .join(sub, Inventory.id == sub.c.max_id)
            .join(Supplier, Supplier.id == Inventory.supplier_id)
            .all()
        )
        last_supplier_by_item = {iid: name for iid, name in rows}

    # (6) reçete etkisi — item_id → [{id, name}], yalnız aktif + domain reçeteler
    recipes_by_item: Dict[int, list] = {}
    if ids:
        rows = (
            db.query(RecipeIngredient.item_id, Recipe.id, Recipe.name)
            .join(Recipe, Recipe.id == RecipeIngredient.recipe_id)
            .filter(Recipe.is_active == True, Recipe.domain == domain,  # noqa: E712
                    RecipeIngredient.item_id.in_(ids))
            .distinct()
            .all()
        )
        for iid, rid, rname in rows:
            recipes_by_item.setdefault(iid, []).append({"id": rid, "name": rname})
        for iid in recipes_by_item:
            recipes_by_item[iid].sort(key=lambda r: tr_key(r["name"]))

    # (7) açık sipariş bayrakları
    open_flags_by_item: Dict[int, StockOrderFlag] = {}
    open_orders_total = 0
    if ids:
        flags = (
            db.query(StockOrderFlag)
            .options(joinedload(StockOrderFlag.supplier))
            .filter(StockOrderFlag.closed_at.is_(None), StockOrderFlag.domain == domain,
                    StockOrderFlag.item_id.in_(ids))
            .all()
        )
        for f in flags:
            open_flags_by_item[f.item_id] = f
        open_orders_total = len(flags)

    qn = normalize(q) if (q and not picked) else None
    rows = []
    for it, st in classified:
        sup_name = it.supplier.name if it.supplier else None
        last_sup = last_supplier_by_item.get(it.id) or sup_name
        if qn:
            hay = normalize(f"{it.name} {it.name_tr or ''} {sup_name or ''} {last_sup or ''}")
            if qn not in hay:
                continue
        flag = open_flags_by_item.get(it.id)
        min_level = float(it.min_stock_level or 0.0)
        current = float(it.current_stock or 0.0)
        lm = last_movement_by_item.get(it.id)
        rows.append({
            "item_id": it.id,
            "name": it.name,
            "name_tr": it.name_tr or "",
            "category": it.category or "—",
            "unit": it.unit or "",
            "current_stock": round(current, 4),
            "min_stock_level": round(min_level, 4),
            "deficit": round(min_level - current, 4) if min_level > EPS else None,
            "status": st,
            "status_label": status_label(st),
            "sample_qty": round(sample_qty_by_item.get(it.id, 0.0), 4),
            "sample_lots": sample_lots_by_item.get(it.id, []),
            "supplier_id": it.supplier_id,
            "supplier_name": sup_name,
            "last_supplier": last_sup,
            "last_movement": to_tr(lm).strftime("%d.%m.%Y %H:%M") if lm else None,
            "recipes": recipes_by_item.get(it.id, []),
            "recipe_count": len(recipes_by_item.get(it.id, [])),
            "order": _flag_view(flag) if flag else None,
        })

    rows.sort(key=lambda r: (STATUS_RANK.get(r["status"], 9), tr_key(r["name"])))

    summary = {
        "sifir": sum(1 for _, st in classified if st == "sifir"),
        "kritik": sum(1 for _, st in classified if st == "kritik"),
        "numune": sum(1 for _, st in classified if st == "numune"),
        "min_undefined": min_undefined,
        "total": len(rows),
        "open_orders": open_orders_total,
    }

    return {
        "generated_at": to_tr(datetime.utcnow()).strftime("%d.%m.%Y %H:%M"),
        "category": category,
        "summary": summary,
        "rows": rows,
    }


def _flag_view(f: StockOrderFlag) -> dict:
    return {
        "id": f.id,
        "supplier_id": f.supplier_id,
        "supplier_name": f.supplier.name if f.supplier else None,
        "quantity": round(float(f.quantity), 4) if f.quantity is not None else None,
        "unit": f.unit or "",
        "expected_date": f.expected_date.strftime("%Y-%m-%d") if f.expected_date else None,
        "note": f.note or "",
        "ordered_by": f.ordered_by or "",
        "ordered_at": to_tr(f.ordered_at).strftime("%d.%m.%Y %H:%M") if f.ordered_at else None,
    }


# ─── Sipariş bayrağı yaşam döngüsü ────────────────────────────────────────────

def close_open_flags(db: Session, item_id: int, reason: str, actor: Optional[str] = None) -> int:
    """Ürünün açık bayraklarını kapat — COMMIT YAPMAZ, çağıran kendi işleminde eder.

    `reason='received'`: gerçek mal kabul (routers/inventory.py receive_stock
    numune-olmayan dal + samples/convert). `reason='manual'`: kullanıcı elle
    kapattı veya yeniden işaretledi."""
    open_flags = (
        db.query(StockOrderFlag)
        .filter(StockOrderFlag.item_id == item_id, StockOrderFlag.closed_at.is_(None))
        .with_for_update()
        .all()
    )
    now = datetime.utcnow()
    for f in open_flags:
        f.closed_at = now
        f.closed_reason = reason
    return len(open_flags)


def open_flag(db: Session, item: Item, *, supplier_id: Optional[int], quantity: Optional[float],
             expected_date: Optional[date], note: Optional[str], actor: str, domain: str) -> StockOrderFlag:
    """Yeni sipariş bayrağı aç — varsa eskisini 'manual' ile kapatır. flush eder, COMMIT ÇAĞIRANDA."""
    close_open_flags(db, item.id, "manual", actor)
    f = StockOrderFlag(
        item_id=item.id, supplier_id=supplier_id, quantity=quantity, unit=item.unit,
        expected_date=expected_date, note=(note or "").strip()[:300] or None,
        ordered_by=actor, domain=domain,
    )
    db.add(f)
    db.flush()
    return f


# ─── Dışa aktarma ─────────────────────────────────────────────────────────────

def export_filename(category: str, ext: str) -> str:
    return f"eksik_hammaddeler_{category}_{datetime.utcnow():%Y%m%d}.{ext}"


def build_workbook(report: dict, prices: Optional[dict] = None) -> bytes:
    """Rapordan 3-sayfalı .xlsx üretir (Eksikler / Sipariş Listesi / Reçete Etkisi).

    `prices` verilirse ({item_id: [{supplier_name, package_size, unit_price}, …]},
    core.supplier_prices.prices_for_items), Sipariş Listesi'nde her satıra 3
    tedarikçiye kadar [Tedarikçi · Alınabilecek Miktar · Birim Fiyat] eklenir
    (core.production_sim.build_workbook ile aynı kalıp)."""
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    s = report["summary"]
    prices = prices or {}
    wb = Workbook()
    NAVY = PatternFill("solid", fgColor="232E6E"); HF = Font(bold=True, color="FFFFFF")
    RED = Font(color="B91C1C", bold=True); GRAY = Font(color="9CA3AF")
    TITLE = Font(bold=True, size=13, color="232E6E"); BLD = Font(bold=True)
    thin = Border(*([Side(style="thin", color="DDDDDD")] * 4))

    def hrow(ws, headers, row=1):
        for ci, h in enumerate(headers, 1):
            c = ws.cell(row, ci, h); c.fill = NAVY; c.font = HF
            c.alignment = Alignment(horizontal="left", vertical="center")

    def widths(ws, ws_widths):
        for i, w in enumerate(ws_widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w

    # Eksikler
    ws = wb.active; ws.title = "Eksikler"
    ws["A1"] = f"Minerva 108 — Eksik Hammaddeler ({report.get('category', 'hammadde')})"
    ws["A1"].font = TITLE
    meta = [
        ("Tarih", report["generated_at"]),
        ("Sıfır stok", s["sifir"]), ("Kritik altı", s["kritik"]),
        ("Sadece numune", s["numune"]), ("Min. tanımsız", s["min_undefined"]),
        ("Açık sipariş", s["open_orders"]),
    ]
    for i, (k, v) in enumerate(meta, 3):
        ws.cell(i, 1, k).font = BLD; ws.cell(i, 2, v)
    hstart = 3 + len(meta) + 1
    hrow2 = ["Hammadde", "Türkçe Ad", "Kategori", "Durum", "Mevcut", "Min", "Eksik", "Birim",
             "Numune", "Reçete Sayısı", "Son Hareket", "Tedarikçi", "Sipariş"]
    for ci, h in enumerate(hrow2, 1):
        c = ws.cell(hstart, ci, h); c.fill = NAVY; c.font = HF
    for ri, r in enumerate(report["rows"], hstart + 1):
        order_txt = (f"{r['order']['supplier_name'] or '—'} · {r['order']['quantity'] or ''} {r['order']['unit']}"
                     if r.get("order") else "")
        vals = [r["name"], r["name_tr"], r["category"], r["status_label"], r["current_stock"],
                r["min_stock_level"], r["deficit"] if r["deficit"] is not None else "", r["unit"],
                r["sample_qty"], r["recipe_count"], r["last_movement"] or "—",
                r["last_supplier"] or "—", order_txt]
        for ci, v in enumerate(vals, 1):
            c = ws.cell(ri, ci, v); c.border = thin
            if r["status"] in ("sifir",) and ci == 4:
                c.font = RED
    ws.freeze_panes = f"A{hstart + 1}"
    widths(ws, [30, 24, 12, 14, 10, 10, 10, 8, 10, 12, 16, 20, 26])

    # Sipariş Listesi — yalnız açık siparişli satırlar, tedarikçiye göre gruplu
    ws = wb.create_sheet("Sipariş Listesi")
    ordered = [r for r in report["rows"] if r.get("order")]
    show_sup = bool(prices)
    SUP_SLOTS = 3
    base = ["Hammadde", "Miktar", "Birim", "Beklenen", "Not", "Sipariş Veren"]
    headers = list(base)
    if show_sup:
        for n in range(1, SUP_SLOTS + 1):
            headers += [f"TEDARİKÇİ-{n}", "Alınabilecek Miktar", "Birim Fiyat"]
    if not ordered:
        ws["A1"] = "Açık sipariş yok."; ws["A1"].font = BLD
    else:
        by_supplier: Dict[str, list] = {}
        for r in ordered:
            by_supplier.setdefault(r["order"]["supplier_name"] or "Tedarikçi belirtilmedi", []).append(r)
        ri = 1
        for sup_name in sorted(by_supplier, key=tr_key):
            c = ws.cell(ri, 1, sup_name); c.font = HF; c.fill = NAVY
            ws.merge_cells(start_row=ri, start_column=1, end_row=ri, end_column=len(headers))
            ri += 1
            hrow(ws, headers, row=ri)
            ri += 1
            for r in by_supplier[sup_name]:
                o = r["order"]
                vals = [r["name"], o["quantity"] or "", o["unit"], o["expected_date"] or "",
                        o["note"] or "", o["ordered_by"] or ""]
                gray_cols = set()
                fmt_cols = {}                            # fiyat hücresi → "#,##0.00## ₺/kg"
                if show_sup:
                    from core.supplier_prices import price_cell_format, vat_label
                    plist = prices.get(r["item_id"], [])
                    for n in range(SUP_SLOTS):
                        if n < len(plist):
                            p = plist[n]
                            name = p["supplier_name"]
                            if p.get("vat_included"):    # brüt fiyat — sıralama netle yapıldı
                                name = f"{name} ({vat_label(p)})"
                            if p.get("inactive"):        # pasif firma: sonda, gri, etiketli
                                name = f"{name} (pasif tedarikçi)"
                                gray_cols.update(range(len(vals) + 1, len(vals) + 4))
                            if p.get("unit_price"):
                                fmt_cols[len(vals) + 3] = price_cell_format(p)
                            vals += [name, p["package_size"] or "", p["unit_price"] or ""]
                        else:
                            vals += ["", "", ""]
                for ci, v in enumerate(vals, 1):
                    c = ws.cell(ri, ci, v); c.border = thin
                    if ci in gray_cols:
                        c.font = GRAY
                    if ci in fmt_cols:
                        c.number_format = fmt_cols[ci]
                ri += 1
            ri += 1
    widths(ws, [30, 10, 8, 12, 30, 16] + ([16, 16, 12] * SUP_SLOTS if show_sup else []))

    # Reçete Etkisi
    ws = wb.create_sheet("Reçete Etkisi")
    hrow(ws, ["Hammadde", "Durum", "Reçete"])
    ri = 2
    for r in report["rows"]:
        if not r["recipes"]:
            continue
        for rec in r["recipes"]:
            vals = [r["name"], r["status_label"], rec["name"]]
            for ci, v in enumerate(vals, 1):
                ws.cell(ri, ci, v).border = thin
            ri += 1
    ws.freeze_panes = "A2"; widths(ws, [30, 14, 46])

    buf = BytesIO(); wb.save(buf)
    return buf.getvalue()


def _story(report: dict, s: float = 1.0):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer

    from core.monthly_report import _register_fonts
    from core.shipment_note import _grid

    font, font_b = _register_fonts()
    NAVY = colors.HexColor("#232E6E")
    MUTED = colors.HexColor("#6b7280")
    st = {
        "font": font, "font_b": font_b, "NAVY": NAVY,
        "LIGHT": colors.HexColor("#f5f0e8"), "GREY": colors.HexColor("#e5e7eb"),
        "cell": ParagraphStyle("cell", fontName=font, fontSize=7.6 * s, textColor=colors.HexColor("#374151"), leading=10 * s),
        "hcell": ParagraphStyle("hcell", fontName=font_b, fontSize=7.6 * s, textColor=colors.white, leading=10 * s),
    }
    h1 = ParagraphStyle("h1", fontName=font_b, fontSize=15 * s, textColor=NAVY, spaceAfter=2 * s, leading=18 * s)
    meta = ParagraphStyle("meta", fontName=font, fontSize=8 * s, textColor=MUTED, leading=11 * s)

    sm = report["summary"]
    cat_label = {"hammadde": "Hammadde", "ambalaj": "Ambalaj", "all": "Tümü"}.get(report.get("category"), "Hammadde")
    story = [
        Paragraph("EKSİK HAMMADDELER", h1),
        Paragraph(f"{report['generated_at']}  ·  Kapsam: {cat_label}  ·  "
                 f"Sıfır: {sm['sifir']}  ·  Kritik altı: {sm['kritik']}  ·  "
                 f"Sadece numune: {sm['numune']}  ·  Min. tanımsız: {sm['min_undefined']}  ·  "
                 f"Açık sipariş: {sm['open_orders']}", meta),
        Spacer(1, 8 * s),
    ]
    headers = ["HAMMADDE", "DURUM", "MEVCUT / MİN", "EKSİK", "NUMUNE", "REÇETE", "SİPARİŞ"]
    W = 174.0
    widths = [W * f * mm for f in (0.27, 0.13, 0.16, 0.11, 0.10, 0.09, 0.14)]
    rows = []
    for r in report["rows"]:
        cur_min = f"{r['current_stock']:g} / {r['min_stock_level']:g} {r['unit']}"
        deficit = f"{r['deficit']:g} {r['unit']}" if r["deficit"] is not None else "—"
        numune = f"{r['sample_qty']:g} {r['unit']}" if r["sample_qty"] else "—"
        order = (f"{r['order']['supplier_name'] or '—'} · {r['order']['quantity'] or ''} {r['order']['unit']}"
                 if r.get("order") else "—")
        rows.append([r["name"], r["status_label"], cur_min, deficit, numune,
                    str(r["recipe_count"]) if r["recipe_count"] else "—", order])
    story.append(_grid(st, headers, rows, widths, s))
    return story


def render_pdf(report: dict) -> bytes:
    from reportlab.lib.units import mm
    from core.delivery_note import render_autofit, merge_letterhead

    content = render_autofit(
        lambda s: _story(report, s),
        margins=(20 * mm, 20 * mm, 48 * mm, 40 * mm),
        doc_kwargs={"title": "Eksik Hammaddeler", "author": "Minerva 108"})
    return merge_letterhead(content)
