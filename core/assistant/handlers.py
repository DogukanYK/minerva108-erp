# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Minerva Asistan araç işleyicileri — katalog/stok/üretim/tedarikçi araçları (HEPSİ
salt-okunur; `search_help` DB'siz).  Operasyon araçları (teslimat, B2B, QC, fason,
pazaryerleri …) `handlers_ops.py`'de.  CRM ve PDKS araçları 13.10.2026 kullanıcı
kararıyla KALDIRILDI — asistan bu verileri hiç görmez.

Her işleyici `fn(ctx, db, args) -> dict`:
  • `ctx`  — tools.ToolContext (kullanıcı, panel, etkin yetkiler, can_finance)
  • `db`   — tools.run_tool'un açtığı kısa ömürlü SessionLocal (thread'de)
  • `args` — modelin JSON girdisi, şemaya göre temizlenmiş (eksik → None)
Dönen dict `tools.fit_payload` ile kompakt JSON'a çevrilir (≤ MAX_RESULT_CHARS,
aşarsa listeler kısaltılır + "truncated": true).  "Kayıt yok" gibi beklenen
hatalar `ToolError` ile döner (is_error=True, mesaj modele gider); izin
denetimi BURADA DEĞİL `tools.run_tool`'dadır (yetki yoksa DB'ye hiç
dokunulmaz).

DEĞİŞMEZLER
───────────
• Panel: domain kolonu olan her tablo `ctx.domain` ile süzülür (Drive cross-cutting,
  süzülmez).
• Finans alanları (`cost_price`, reçete maliyeti) YALNIZ `ctx.can_finance`
  ise ÜRETİLİR — "hesapla-sıfırla" yok; anahtar hiç konmaz.
• Tedarikçi alış fiyatları sistemdeki kuralla aynı (items.view|reports.view
  görür; 05.10.2026 kararı; `SUPPLIER_PRICE_PERMS`) — supplier_detail'de
  finans şartı aranmaz ama `prices`/`prices_total` YALNIZ bu izinlerden biri
  varsa üretilir (yalnız inventory.view taşıyan hesap iletişimi görür,
  `/api/suppliers/{id}/prices`'ta 403 aldığı fiyatları asistandan da alamaz).
• Bilinçli karar (12.10.2026): item_detail / find_lot / production_summary
  any-of kümeleri onaylı plan tablosundaki gibi kalır (items|inventory|reports,
  inventory|production|retention, production|reports).  Bunlar sistemin tek
  tek uçlarından (örn. `/api/inventory/by-item` inventory|reports,
  `trace_lot` inventory) daha geniştir; varsayılan rollerin hepsinde ilgili
  view'lar açık olduğundan fark yalnız override hesaplarını etkiler.  Lot
  aracında yine de alt listeler izne göre üretilir: `inventory` yalnız
  inventory.view, `retention` yalnız retention.view, `production` yalnız
  production.view — kullanıcı sistemde göremediği alt veriyi asistandan görmez.
• Çıktı id + sayı + kısa etiket; düzyazı yok; linkler yalnız iç yollar.
• Araç sonucundaki metinler (ad, not) modele VERİ olarak gider — sistem
  istemi bunların talimat olmadığını söyler; burada ek kaçış yapılmaz.
"""
import datetime as _dt
import difflib
from typing import TYPE_CHECKING, Dict, Optional, Tuple
from urllib.parse import quote

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from database import (DriveFile, DriveFolder, Inventory, Item, MaterialGroup, MaterialSupplierPref, ProductionHistory, Recipe,
                      RetentionSample, StockOrderFlag, Supplier, SupplierPrice, to_tr, tr_now)
from core import drive as D
from core import group_stock, material_groups, stock_gaps
from core.assistant import help_docs, prompt
from core.brands import cabinet_of
from core.items_report import tr_key
from core.lots import expiry_today, normalize_lot, parse_expiry
from core.material_groups import material_key
from core.production_history_report import GROUPS, build_report, date_in_range
from core.purchase_pricing import supplier_key
from core.retention import location_label
from core.supplier_prices import net_unit_price, normalize, vat_label
from core.suppliers import STATUS_LABELS, normalize_status

if TYPE_CHECKING:   # döngüsel import yok: ToolContext tools.py'de
    from core.assistant.tools import ToolContext

# Liste tavanları — çıktı ≈ ≤2K token kalsın (tools.MAX_RESULT_CHARS ikinci emniyet)
ITEMS_MAX = 8
LOTS_MAX = 20
RECIPES_MAX = 8
ROWS_MAX = 25
PRODUCTIONS_MAX = 10
SUPPLIERS_MAX = 8
PRICES_MAX = 20
GAPS_MAX = 20
EXPIRING_MAX = 20
FILES_MAX = 10
FOLDERS_MAX = 5
NOTES_MAX = 1500
EXPIRING_DEFAULT_DAYS = 60
EXPIRING_MAX_DAYS = 180

CATEGORY_CHOICES = ("all", "Hammadde", "Ambalaj", "Bitmiş Ürün")
GAP_STATUSES = ("sifir", "kritik", "numune", "all")
_NOT_HAMMADDE = ("Ambalaj", "Bitmiş Ürün")
NO_SHELF = "raf bilgisi girilmemiş"


class ToolError(Exception):
    """Beklenen, modele anlatılacak hata ("kayıt bulunamadı", "lot boş")."""


# ─── Yardımcılar ─────────────────────────────────────────────────────────────

def _q(s) -> str:
    return quote(str(s or ""), safe="")


def _r(x, nd: int = 4):
    return round(float(x or 0.0), nd)


def _dt_text(dt) -> str:
    return to_tr(dt).strftime("%d.%m.%Y %H:%M") if dt else ""


def _date_text(d) -> Optional[str]:
    return d.isoformat() if d else None


def _text(v) -> str:
    return str(v or "").strip()


def _int_arg(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = args.get(key)
    if v is None or v == "":
        return default
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _jaccard(a, b) -> float:
    sa, sb = set(a or ()), set(b or ())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _category_filter(category: Optional[str]):
    """find_item kategori süzgeci — Ürünler sekmesiyle aynı negasyon
    (stock_gaps.category_filter)."""
    c = (category or "all")
    if c == "Ambalaj":
        return (Item.category == "Ambalaj",)
    if c == "Bitmiş Ürün":
        return (Item.category == "Bitmiş Ürün",)
    if c == "Hammadde":
        return (or_(Item.category.notin_(_NOT_HAMMADDE), Item.category.is_(None)),)
    return ()


# ─── find_item ───────────────────────────────────────────────────────────────

def score_item(q_norm: str, q_key: Tuple[str, ...], name_norm: str, name_tr_norm: str,
               sku_norm: str, barcode_norm: str, item_key: Tuple[str, ...]) -> float:
    """SAF puanlayıcı (birim test edilebilir).
    sku/barkod tam eşleşme 100 → katlanmış alt-dizgi 80 (+ kısa ad önde) →
    material_key Jaccard ≥ 0.5 → 60·J → difflib ≥ 0.8 → 50·r; eşleşmezse 0."""
    if q_norm and (q_norm == sku_norm or q_norm == barcode_norm):
        return 100.0
    for n in (name_norm, name_tr_norm):
        if n and q_norm and q_norm in n:
            return 80.0 + max(0.0, 1.0 - len(n) / 200.0)
    j = _jaccard(q_key, item_key)
    if j >= 0.5:
        return 60.0 * j
    best = 0.0
    for n in (name_norm, name_tr_norm):
        r = _ratio(q_norm, n)
        if r >= 0.8:
            best = max(best, 50.0 * r)
    return best


def find_item(ctx: "ToolContext", db: Session, args: dict) -> dict:
    q = _text(args.get("q"))
    if not q:
        raise ToolError("Arama metni boş")
    category = args.get("category") or "all"
    if category not in CATEGORY_CHOICES:
        category = "all"
    q_norm = normalize(q)
    q_key = material_key(q)
    rows = (db.query(Item, Supplier.name, MaterialGroup.name)
            .outerjoin(Supplier, Supplier.id == Item.supplier_id)
            .outerjoin(MaterialGroup, MaterialGroup.id == Item.material_group_id)
            .filter(Item.is_active == True, Item.domain == ctx.domain)   # noqa: E712
            .filter(*_category_filter(category))
            .all())
    scored = []
    for it, sup_name, grp_name in rows:
        s = score_item(q_norm, q_key, normalize(it.name), normalize(it.name_tr),
                       normalize(it.sku), normalize(it.barcode), material_key(it.name))
        if s > 0:
            scored.append((s, tr_key(it.name), it, sup_name, grp_name))
    scored.sort(key=lambda t: (-t[0], t[1]))
    items = [{
        "id": it.id,
        "name": it.name,
        "name_tr": it.name_tr or "",
        "sku": it.sku or "",
        "unit": it.unit or "",
        "category": it.category or "",
        "stock": _r(it.current_stock),
        "min": _r(it.min_stock_level),
        "supplier": sup_name or "",
        "group": grp_name or "",
        "link": f"/items?q={_q(it.name)}",
    } for _, _, it, sup_name, grp_name in scored[:ITEMS_MAX]]
    return {"items": items, "count": len(scored), "more": len(scored) > ITEMS_MAX,
            "link": f"/items?q={_q(q)}"}


# ─── item_detail ─────────────────────────────────────────────────────────────

def item_detail(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """routers/inventory.inventory_by_item mantığı (lot/konum/numune/grup) +
    açık sipariş bayrağı; `cost_price` YALNIZ finans."""
    item_id = _int_arg(args, "item_id", 0, 0, 2 ** 31)
    item = db.query(Item).filter(Item.id == item_id, Item.domain == ctx.domain).first()
    if item is None:
        raise ToolError("Bu panelde böyle bir kart yok")
    rows = (db.query(Inventory).options(joinedload(Inventory.supplier))
            .filter(Inventory.item_id == item.id, Inventory.domain == ctx.domain)
            .order_by(Inventory.id.desc()).all())
    by_location: Dict[str, float] = {}
    lots = []
    non_sample = sample = 0.0
    for r in rows:
        loc = (r.location or "").strip() or "(Konum belirtilmemiş)"
        qty = float(r.quantity or 0.0)
        by_location[loc] = round(by_location.get(loc, 0.0) + qty, 4)
        if r.is_sample:
            sample += qty
        else:
            non_sample += qty
        lots.append({
            "lot": r.lot_number, "qty": _r(qty), "location": loc, "status": r.status or "",
            "is_sample": bool(r.is_sample),
            "supplier": r.supplier.name if r.supplier else "",
            "expiry": r.expiry_date or "", "received": _dt_text(r.created_at),
        })
    grp = material_groups.group_payload(db, item.material_group_id)
    group = None
    if grp:
        group = {"id": grp["id"], "name": grp["name"],
                 "members": [{"id": m["item_id"], "name": m["name"], "stock": m["stock"],
                              "supplier": m.get("supplier_name") or ""}
                             for m in grp["members"] if m["is_active"] and m["item_id"] != item.id]}
        # Grup TOPLAMI (core.group_stock — Stoklar/Aylık rapordaki sayı; g↔kg,
        # ml↔l çevrilir, başka birim ailesi `aside`).  `rule_active`: grup
        # toplamı eksik/kritik kararına giriyor mu (üretimde kaynak seçimi ayarı).
        gs = group_stock.index_by_item(group_stock.load_groups(
            db, ctx.domain, group_ids=[item.material_group_id])).get(item.id)
        if gs is not None:
            group.update({
                "group_total": _r(gs.total), "group_unit": gs.base_unit,
                "group_total_text": gs.total_text, "breakdown": gs.breakdown_text,
                "aside": gs.aside_text, "counted": item.id in gs.counted_ids,
                "group_min_text": (group_stock.amount_short(gs.min_stock, gs.base_unit)
                                   if gs.min_stock > 0 else ""),
                "rule_active": group_stock.group_stock_enabled(db),
            })
    flag = (db.query(StockOrderFlag).options(joinedload(StockOrderFlag.supplier))
            .filter(StockOrderFlag.item_id == item.id, StockOrderFlag.domain == ctx.domain,
                    StockOrderFlag.closed_at.is_(None))
            .order_by(StockOrderFlag.id.desc()).first())
    open_order = None
    if flag is not None:
        open_order = {"qty": _r(flag.quantity) if flag.quantity is not None else None,
                      "unit": flag.unit or "", "expected_date": _date_text(flag.expected_date),
                      "supplier": flag.supplier.name if flag.supplier else "",
                      "ordered_at": _dt_text(flag.ordered_at)}
    out = {
        "id": item.id, "name": item.name, "name_tr": item.name_tr or "", "sku": item.sku or "",
        "unit": item.unit or "", "category": item.category or "",
        "current_stock": _r(item.current_stock), "lot_total": _r(non_sample),
        "sample_total": _r(sample), "min_stock_level": _r(item.min_stock_level),
        "supplier": item.supplier.name if item.supplier else "",
        "by_location": [{"location": k, "total": v}
                        for k, v in sorted(by_location.items(), key=lambda kv: -kv[1])],
        "lots": lots[:LOTS_MAX], "lots_total": len(lots),
        "group": group, "open_order": open_order,
        "link": f"/items?q={_q(item.name)}",
    }
    if ctx.can_finance:
        out["cost_price"] = _r(item.cost_price)
    return out


# ─── find_lot ────────────────────────────────────────────────────────────────

def find_lot(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Lot no ÜRÜN BAZLIDIR (SR005 birden çok üründe olabilir) → ürün başına
    envanter + üretim + şahit numunesi; `ambiguous` birden çok üründe.
    trace_lot'un aksine panel süzgeci VAR.  Alt listeler izne göre: `inventory`
    yalnız inventory.view, `production` yalnız production.view, `retention`
    yalnız retention.view ile okunur (yoksa boş liste / null — sistemde
    göremediği alt veriyi asistandan görmesin)."""
    lot = normalize_lot(args.get("lot_number"))
    if not lot:
        raise ToolError("Lot numarası boş")
    variants = [lot, lot + "-S"] if not lot.endswith("-S") else [lot, lot[:-2]]
    inv_rows = (db.query(Inventory).options(joinedload(Inventory.supplier))
                .filter(Inventory.domain == ctx.domain, Inventory.lot_number.in_(variants))
                .order_by(Inventory.id.asc()).all()) if ctx.can("inventory", "view") else []
    prods = (db.query(ProductionHistory)
             .filter(ProductionHistory.domain == ctx.domain,
                     ProductionHistory.lot_number.in_(variants))
             .order_by(ProductionHistory.cancelled_at.isnot(None), ProductionHistory.id.desc())
             .all()) if ctx.can("production", "view") else []
    rets = (db.query(RetentionSample)
            .filter(RetentionSample.domain == ctx.domain, RetentionSample.is_active == True,  # noqa: E712
                    RetentionSample.lot_number.in_(variants))
            .order_by(RetentionSample.id.asc()).all()) if ctx.can("retention", "view") else []
    item_ids = ({r.item_id for r in inv_rows}
                | {p.target_item_id for p in prods if p.target_item_id}
                | {r.item_id for r in rets})
    if not item_ids:
        raise ToolError(f"Lot bulunamadı: {lot}")
    items = {i.id: i for i in db.query(Item).filter(Item.id.in_(item_ids)).all()}
    products = []
    for iid in sorted(item_ids, key=lambda i: tr_key(items[i].name) if i in items else ""):
        it = items.get(iid)
        prod = next((p for p in prods if p.target_item_id == iid), None)
        products.append({
            "item_id": iid,
            "item_name": it.name if it else "—",
            "unit": (it.unit if it else "") or "",
            "inventory": [{
                "lot": r.lot_number, "qty": _r(r.quantity), "location": r.location or "—",
                "status": r.status or "", "expiry": r.expiry_date or "",
                "is_sample": bool(r.is_sample),
                "supplier": r.supplier.name if r.supplier else "",
            } for r in inv_rows if r.item_id == iid],
            "production": ({
                "id": prod.id, "recipe": prod.recipe_name or "",
                "produced_at": _dt_text(prod.produced_at), "qty": _r(prod.produced_quantity),
                "by": prod.produced_by or "", "cancelled": bool(prod.cancelled_at),
                "cancel_reason": (prod.cancel_reason or "") if prod.cancelled_at else "",
            } if prod else None),
            "retention": [{
                "cabinet": r.brand or cabinet_of(it.name if it else ""),
                "shelf_slot_label": (location_label(r.shelf, r.slot)
                                     if location_label(r.shelf, r.slot) != "—" else NO_SHELF),
                "qty": _r(r.quantity), "unit": r.unit or "",
                "retention_until": _date_text(r.retention_until), "status": r.status or "",
            } for r in rets if r.item_id == iid],
        })
    return {"lot": lot, "products": products, "ambiguous": len(products) > 1,
            "link": f"/traceability?lot={_q(lot)}"}


# ─── find_recipe / recipe_detail ─────────────────────────────────────────────

def find_recipe(ctx: "ToolContext", db: Session, args: dict) -> dict:
    q = _text(args.get("q"))
    if not q:
        raise ToolError("Arama metni boş")
    qn = normalize(q)
    rows = (db.query(Recipe).options(joinedload(Recipe.target_item), joinedload(Recipe.ingredients))
            .filter(Recipe.is_active == True, Recipe.domain == ctx.domain)   # noqa: E712
            .all())
    scored = []
    for r in rows:
        tname = r.target_item.name if r.target_item else (r.description or "")
        names = [normalize(r.name), normalize(tname)]
        s = 0.0
        if any(qn and qn in n for n in names if n):
            s = 80.0
        else:
            best = max((_ratio(qn, n) for n in names if n), default=0.0)
            if best >= 0.8:
                s = 50.0 * best
        if s > 0:
            scored.append((s, tr_key(r.name), r, tname))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return {
        "recipes": [{
            "id": r.id, "name": r.name, "target_item": tname,
            "target_item_id": r.target_item_id,
            "output_qty": _r(r.output_quantity), "unit": r.output_unit or "",
            "ingredients_count": len(r.ingredients or []),
            "link": f"/recipes?q={_q(r.name)}",
        } for _, _, r, tname in scored[:RECIPES_MAX]],
        "count": len(scored), "more": len(scored) > RECIPES_MAX,
        "link": f"/recipes?q={_q(q)}",
    }


def recipe_detail(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Reçete + bileşenler (faz, miktar, kart stoğu) + yapılış notu.  Maliyet
    YALNIZ finans: `routers.recipes._calc_recipe_costs` ancak o zaman çağrılır
    (hesapla-sıfırla yok)."""
    rid = _int_arg(args, "recipe_id", 0, 0, 2 ** 31)
    r = (db.query(Recipe).options(joinedload(Recipe.target_item), joinedload(Recipe.ingredients))
         .filter(Recipe.id == rid, Recipe.domain == ctx.domain).first())
    if r is None:
        raise ToolError("Bu panelde böyle bir reçete yok")
    item_ids = [ing.item_id for ing in r.ingredients]
    items = ({i.id: i for i in db.query(Item).filter(Item.id.in_(item_ids)).all()}
             if item_ids else {})
    ingredients = []
    for ing in sorted(r.ingredients, key=lambda x: ((x.phase or "~"), x.id)):
        it = items.get(ing.item_id)
        ingredients.append({
            "item_id": ing.item_id, "name": it.name if it else "—",
            "qty": _r(ing.quantity, 6), "unit": ing.unit or (it.unit if it else "") or "",
            "phase": ing.phase or "", "category": (it.category if it else "") or "",
            "stock": _r(it.current_stock) if it else None,
            "stock_unit": (it.unit if it else "") or "",
        })
    tname = r.target_item.name if r.target_item else (r.description or "")
    notes = (r.production_notes or "").strip()
    out = {
        "id": r.id, "name": r.name, "target_item": tname, "target_item_id": r.target_item_id,
        "expected_yield": _r(r.output_quantity), "unit": r.output_unit or "",
        "waste_pct": _r(r.waste_percentage, 2), "is_active": bool(r.is_active),
        "ingredients": ingredients,
        "production_notes": notes[:NOTES_MAX], "notes_truncated": len(notes) > NOTES_MAX,
        "link": f"/recipes?q={_q(r.name)}",
    }
    if ctx.can_finance:
        from routers.recipes import _calc_recipe_costs   # tembel: router modülü
        costs = _calc_recipe_costs(r, db)
        by_item = {c["item_id"]: c for c in costs["ingredient_costs"]}
        for ing in out["ingredients"]:
            c = by_item.get(ing["item_id"])
            if c is not None:
                ing["cost_price"] = c["cost_price"]
                ing["line_cost"] = c["line_cost"]
        out["total_cost"] = costs["total_cost"]
        out["unit_cost"] = costs["unit_cost"]
    return out


# ─── production_summary / recent_productions ─────────────────────────────────

def _parse_iso(v) -> Optional[_dt.date]:
    s = _text(v)
    if not s:
        return None
    try:
        return _dt.date.fromisoformat(s[:10])
    except ValueError:
        raise ToolError(f"Tarih YYYY-AA-GG olmalı: {s}")


def production_summary(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """core.production_history_report.build_report — varsayılan dönem içinde
    bulunulan TR ayı (ayın 1'i → bugün); iptal edilen üretim sayılmaz."""
    today = tr_now().date()
    start, end = _parse_iso(args.get("start")), _parse_iso(args.get("end"))
    if start is None and end is None:
        start, end = today.replace(day=1), today
    elif start is None:
        start = end.replace(day=1)
    elif end is None:
        end = today if today >= start else start
    if not (date_in_range(start) and date_in_range(end)):
        raise ToolError("Tarih 2000–2100 aralığında olmalı")
    if start > end:
        raise ToolError("Başlangıç bitişten sonra olamaz")
    group = args.get("group") or "product"
    if group not in GROUPS:
        raise ToolError("group product|brand|category olmalı")
    rep = build_report(db, ctx.domain, start, end, q=_text(args.get("q")) or None, group=group)
    rows = []
    for r in rep["rows"][:ROWS_MAX]:
        row = {"name": r["name"], "total": r["total"], "count": r["count"]}
        if group == "product":
            row["brand"] = r.get("brand") or ""
            row["category"] = r.get("category") or ""
        rows.append(row)
    return {
        "start": rep["start"], "end": rep["end"], "group": group,
        "rows": rows, "rows_total": len(rep["rows"]),
        "totals": {k: rep["totals"][k] for k in ("quantity", "count", "products")},
        "first_record": rep.get("first_record"),
        "link": "/reports#production-history",
    }


def recent_productions(ctx: "ToolContext", db: Session, args: dict) -> dict:
    limit = _int_arg(args, "limit", 5, 1, PRODUCTIONS_MAX)
    rows = (db.query(ProductionHistory, Item.unit)
            .outerjoin(Item, Item.id == ProductionHistory.target_item_id)
            .filter(ProductionHistory.domain == ctx.domain, ProductionHistory.cancelled_at.is_(None))
            .order_by(ProductionHistory.id.desc()).limit(limit).all())
    return {
        "productions": [{
            "id": p.id, "recipe": p.recipe_name or "", "target_item": p.target_item_name or "",
            "qty": _r(p.produced_quantity), "unit": unit or "", "lot": p.lot_number or "",
            "produced_at": _dt_text(p.produced_at), "by": p.produced_by or "",
            "link": f"/traceability?lot={_q(p.lot_number)}" if p.lot_number else "/production",
        } for p, unit in rows],
        "link": "/production",
    }


# ─── find_supplier / supplier_detail ─────────────────────────────────────────

def _supplier_row(s: Supplier) -> dict:
    st = normalize_status(s.purchase_status) or "normal"
    return {
        "id": s.id, "name": s.name, "phone": s.phone or "", "email": s.email or "",
        "contact_person": s.contact_person or "",
        "status": st, "status_label": STATUS_LABELS[st], "status_reason": s.status_reason or "",
        "is_active": bool(s.is_active) if s.is_active is not None else True,
        "link": f"/suppliers?q={_q(s.name)}",
    }


def find_supplier(ctx: "ToolContext", db: Session, args: dict) -> dict:
    q = _text(args.get("q"))
    if not q:
        raise ToolError("Arama metni boş")
    qn, qk = normalize(q), supplier_key(q)
    rows = db.query(Supplier).filter(Supplier.domain == ctx.domain).all()
    scored = []
    for s in rows:
        n = normalize(s.name)
        if qn and qn in n:
            sc = 80.0
        elif qk and qk == supplier_key(s.name):
            sc = 70.0
        else:
            r = _ratio(qn, n)
            sc = 50.0 * r if r >= 0.8 else 0.0
        if sc > 0:
            active = bool(s.is_active) if s.is_active is not None else True
            scored.append((-sc, 0 if active else 1, tr_key(s.name), s))
    scored.sort(key=lambda t: t[:3])
    return {"suppliers": [_supplier_row(s) for _, _, _, s in scored[:SUPPLIERS_MAX]],
            "count": len(scored), "more": len(scored) > SUPPLIERS_MAX,
            "link": f"/suppliers?q={_q(q)}"}


#: `prices` alanının kapısı — routers/suppliers.py `GET /{id}/prices` ile aynı.
SUPPLIER_PRICE_PERMS = (("items", "view"), ("reports", "view"))


def supplier_detail(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Firma kartı + fiyat listesi (sistem kuralı: fiyatlar items.view|reports.view
    ile açık — yoksa `prices` anahtarı HİÇ konmaz, finans alanı kalıbı) +
    malzeme bazlı tercihler + kart sayısı."""
    sid = _int_arg(args, "supplier_id", 0, 0, 2 ** 31)
    s = db.query(Supplier).filter(Supplier.id == sid, Supplier.domain == ctx.domain).first()
    if s is None:
        raise ToolError("Bu panelde böyle bir tedarikçi yok")
    out = _supplier_row(s)
    out["address"] = s.address or ""
    out["notes"] = (s.notes or "")[:300]
    out["items_count"] = int(db.query(func.count(Item.id))
                             .filter(Item.supplier_id == s.id, Item.is_active == True,   # noqa: E712
                                     Item.domain == ctx.domain).scalar() or 0)
    if ctx.can_any(*SUPPLIER_PRICE_PERMS):
        price_rows = (db.query(SupplierPrice, Item.name, Item.unit)
                      .join(Item, Item.id == SupplierPrice.item_id)
                      .filter(SupplierPrice.supplier_id == s.id, SupplierPrice.domain == ctx.domain)
                      .order_by(SupplierPrice.updated_at.desc(), SupplierPrice.id.desc()).all())
        out["prices"] = [{
            "item_id": sp.item_id, "item": name, "unit": unit or "",
            "unit_price": sp.unit_price,
            "net_unit_price": _r(net_unit_price(sp), 4) if sp.unit_price is not None else None,
            "currency": sp.currency or "", "price_unit": sp.price_unit or "",
            "vat": vat_label(sp) or ("KDV hariç" if sp.vat_included is False else ""),
            "package_size": sp.package_size, "quoted_at": _date_text(sp.quoted_at),
            "source": sp.source or "",
        } for sp, name, unit in price_rows[:PRICES_MAX]]
        out["prices_total"] = len(price_rows)
    prefs = (db.query(MaterialSupplierPref, MaterialGroup.name, Item.name)
             .outerjoin(MaterialGroup, MaterialGroup.id == MaterialSupplierPref.material_group_id)
             .outerjoin(Item, Item.id == MaterialSupplierPref.item_id)
             .filter(MaterialSupplierPref.supplier_id == s.id,
                     MaterialSupplierPref.domain == ctx.domain)
             .order_by(MaterialSupplierPref.preference, MaterialSupplierPref.rank).all())
    out["prefs"] = [{"material": gname or iname or "—", "preference": p.preference, "rank": p.rank}
                    for p, gname, iname in prefs]
    return out


# ─── stock_gaps / expiring_lots ──────────────────────────────────────────────

def stock_gaps_tool(ctx: "ToolContext", db: Session, args: dict) -> dict:
    status = args.get("status") or "all"
    if status not in GAP_STATUSES:
        raise ToolError("status sifir|kritik|numune|all olmalı")
    rep = stock_gaps.assemble(db, ctx.domain, category="hammadde",
                              statuses=None if status == "all" else [status],
                              q=_text(args.get("q")) or None)
    def _ordered(r):
        o = r.get("order")
        return ({"qty": o["quantity"], "unit": o["unit"], "expected_date": o["expected_date"],
                 "supplier": o["supplier_name"] or ""} if o else None)

    def _row(r):
        out = {
            "item_id": r["item_id"], "name": r["name"], "status": r["status"],
            "status_label": r["status_label"], "current_stock": r["current_stock"],
            "min_stock_level": r["min_stock_level"], "unit": r["unit"],
            "sample_qty": r["sample_qty"], "last_supplier": r.get("last_supplier") or "",
            "ordered": _ordered(r),
        }
        if r.get("kind") == "group":
            # "Aynı malzeme" grubu TEK satır: miktarlar grubun taban biriminde
            # toplam, kartlar `members` altında (sipariş bayrağı kart başına).
            out.update({
                "kind": "group", "group_id": r["group_id"], "total_text": r["current_text"],
                "min_text": r["min_text"], "breakdown": r["breakdown_text"],
                "ordered_cards": r.get("ordered_count", 0),
                "members": [{"item_id": m["item_id"], "name": m["name"],
                             "stock_text": m.get("stock_text") or "", "unit": m["unit"],
                             "supplier": m.get("supplier_name") or "", "ordered": _ordered(m)}
                            for m in r.get("members") or []],
            })
        return out

    rows = [_row(r) for r in rep["rows"][:GAPS_MAX]]
    sm = rep["summary"]
    out = {"summary": {k: sm.get(k, 0) for k in ("sifir", "kritik", "numune", "open_orders", "total")},
           "rows": rows, "rows_total": len(rep["rows"]), "link": "/reports#stock-gaps"}
    if rep.get("group_rule"):
        out["group_rule"] = ("Aynı malzeme grupları toplam stokla değerlendirildi; "
                             "grubu yeterli kartlar listede yok.")
    return out


def expiring_lots(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """routers/inventory.list_expiring mantığı, pencere `days` (vars. 60, ≤180)."""
    days = _int_arg(args, "days", EXPIRING_DEFAULT_DAYS, 1, EXPIRING_MAX_DAYS)
    today = expiry_today()
    rows = (db.query(Inventory)
            .filter(Inventory.expiry_date.isnot(None), Inventory.expiry_date != "",
                    Inventory.status == "APPROVED", Inventory.domain == ctx.domain)
            .all())
    found = []
    for r in rows:
        d = parse_expiry(r.expiry_date)
        if d is None:
            continue
        left = (d - today).days
        if left < 0 or left > days:
            continue
        found.append((left, r))
    found.sort(key=lambda t: (t[0], t[1].id))
    item_ids = {r.item_id for _, r in found}
    items = ({i.id: i for i in db.query(Item).filter(Item.id.in_(item_ids)).all()}
             if item_ids else {})
    return {
        "days": days,
        "lots": [{
            "item_id": r.item_id, "item": items[r.item_id].name if r.item_id in items else "—",
            "unit": (items[r.item_id].unit if r.item_id in items else "") or "",
            "lot": r.lot_number, "expiry": r.expiry_date, "days_left": left,
            "qty": _r(r.quantity), "location": r.location or "—",
        } for left, r in found[:EXPIRING_MAX]],
        "count": len(found), "link": "/traceability",
    }


# ─── drive_search ────────────────────────────────────────────────────────────

def drive_search(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Yalnız AD + klasör yolu (içerik okunmaz, indirme linki üretilmez)."""
    term = _text(args.get("q"))
    if len(term) < 2:
        raise ToolError("En az 2 karakter gir")
    like = f"%{term}%"

    def _path(folder_id):
        return " / ".join(p["name"] for p in D.folder_path(db, folder_id)) or "Kök"

    folders = (db.query(DriveFolder).filter(DriveFolder.name.ilike(like))
               .order_by(DriveFolder.name).limit(FOLDERS_MAX + 1).all())
    files = (db.query(DriveFile).filter(DriveFile.original_name.ilike(like))
             .order_by(DriveFile.original_name).limit(FILES_MAX + 1).all())
    return {
        "files": [{"id": f.id, "name": f.original_name, "path": _path(f.folder_id),
                   "size": D.humanize(f.size_bytes)} for f in files[:FILES_MAX]],
        "folders": [{"id": f.id, "name": f.name, "path": _path(f.parent_id)}
                    for f in folders[:FOLDERS_MAX]],
        "more": len(files) > FILES_MAX or len(folders) > FOLDERS_MAX,
        "link": f"/drive?q={_q(term)}",
    }


# ─── search_help ─────────────────────────────────────────────────────────────

def search_help(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Nasıl-yapılır rehberinde arama (`help_docs.search`) — DB'ye DOKUNMAZ
    (`db` yok sayılır), kullanıcı metninden dosya yolu kurmaz; konular import
    anında bir kez okunmuş `prompt.HELP_TOPICS`'tir.  Sonuç `MAX_RESULT_CHARS`'a
    işleyicide sığdırılır (birinci konunun metni kırpılmaz)."""
    # döngüsel import: tools bu modülü import eder — sınır çağrı anında okunur
    from core.assistant.tools import MAX_RESULT_CHARS
    q = _text(args.get("q"))
    if len(q) < 2:
        raise ToolError("En az 2 karakter gir")
    return help_docs.search(prompt.HELP_TOPICS, q, limit=MAX_RESULT_CHARS)
