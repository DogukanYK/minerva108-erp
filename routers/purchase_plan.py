# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Satın Alma Planı router'ı — `/api/purchase-plan/*` (sayfa: `/satin-alma`).

"Şu ürünlerden şu adetlerde üreteceğiz → hangi malzemeden ne kadar, kimden,
kaça alacağız?"  Hesap `core/purchase_plan.py` (motor) + `core/purchase_
pricing.py` (fiyat/tedarikçi) + çıktılar `core/purchase_plan_pdf.py` /
`core/purchase_plan_xlsx.py`.  Bu dosya yalnız HTTP katmanı: doğrulama,
domain kapsamı, senaryo CRUD'u ve audit.

Kurallar:
  • Her uç `reports.view` + `active_domain`.  Fiyatlar raporu gören herkese
    açık (kullanıcı kararı 05.10.2026) — rol bazlı fiyat gizleme YOK.
  • Panel dışı reçete/kalem id'si → 400 "Bu panelde değil: …" (motor
    `PlanInputError`).  Hiçbir satırın reçetesi yoksa → 400.
  • Kur (TCMB) YALNIZ rapor para biriminden farklı fiyatlı teklif varsa
    çekilir — USD fiyat listesi + USD rapor ağ isteği yapmaz.
  • Senaryolar (`purchase_plans`): ad aktif kayıtlarda panel içinde tekil
    (TR-katlanmış karşılaştırma + kısmi tekil indeks); düzenleme/silme sahibi,
    SuperAdmin ya da Manager (rol DB'den CANLI okunur, JWT'deki eski rol
    değil).  Silme yumuşak.  create/update/delete/export audit'li.
"""
import io
import json
import logging
import re
from datetime import date, datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.audit import log_admin_event
from core.domain import active_domain
from core.limiter import limiter
from core.permissions import require_permission
from core.purchase_plan_models import PlanRequest
from database import PurchasePlan, User, get_db, to_tr, tr_now

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/purchase-plan", tags=["purchase-plan"])

SCENARIO_VERSION = 1
EDITOR_ROLES = ("SuperAdmin", "Manager")
# Rapor (önizleme JSON'u) içinde satırları malzeme dict'i olan bölümler —
# yanıtta bu bölümlerin satırları `row_keys` ile verilir (aynı dict
# `materials[]`'ta zaten var; iki kez taşınıp yanıt şişmesin).
_MATERIAL_SECTIONS = {"raw_priced", "raw_unpriced", "pkg_priced", "pkg_unpriced", "labels", "sufficient"}
_NAME_CAUTIONS = {"estimated"}       # PDF ile aynı: adın yanına, "Not:" satırına değil


def _err(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


def _actor(current_user: dict) -> str:
    return (current_user.get("full_name") or current_user.get("username") or "—")[:80]


def _uid(current_user: dict) -> int:
    try:
        return int(current_user.get("sub", 0))
    except (TypeError, ValueError):
        return 0


def _tr(dt) -> Optional[str]:
    return to_tr(dt).strftime("%d.%m.%Y %H:%M") if dt else None


# ─── Ürün listesi ───────────────────────────────────────────────────────────

def _brand(target_name: Optional[str], recipe_name: Optional[str]):
    """(anahtar, etiket) — `core.brands.product_brand` ile aynı öncelik."""
    from core.brands import brand_key, brand_label, is_known_brand
    for nm in (target_name, recipe_name):
        if nm and is_known_brand(nm):
            return brand_key(nm), brand_label(nm)
    nm = target_name or recipe_name or ""
    return brand_key(nm), brand_label(nm)


def products_payload(db: Session, domain: str) -> dict:
    """Seçim listesi: paneldeki AKTİF reçeteler (`r:<id>`) + reçetesi olmayan
    somut bitmiş ürünler (`i:<id>`, `recipeless`).  Soyut varyasyon ebeveyni
    (başka kartın `parent_id`'si) reçete taşıyamaz → listelenmez.

    Aynı ürüne birden fazla aktif reçete varsa hepsi listelenir; motorun
    `recipe_by_target` kuralıyla (en yeni) seçileni `primary=True`.  "Geçmiş
    üretimden doldur" adedi yalnız primary satıra yazar (çift sayılmasın).
    """
    from core.consumption import _is_lang_label, _kind, item_rec, load_recipe_recs, parse_ml
    from core.ingredients_report import _abstract_parent_ids
    from core.purchase_plan import CFG_DEFAULT_EXCLUDED, default_excluded_key, resolve_default_excluded
    from database import AppSetting, Item, Recipe

    rec_rows = (db.query(Recipe.id, Recipe.target_item_id, Recipe.created_at)
                .filter(Recipe.domain == domain, Recipe.is_active == True)   # noqa: E712
                .all())
    newest: Dict[int, tuple] = {}
    primary_of: Dict[int, int] = {}
    for r in rec_rows:
        if not r.target_item_id:
            continue
        key = (r.created_at or datetime.min, r.id)
        if r.target_item_id not in newest or key > newest[r.target_item_id]:
            newest[r.target_item_id] = key
            primary_of[r.target_item_id] = r.id
    recs, ritems, sibs = load_recipe_recs(db, [r.id for r in rec_rows], domain)

    products: List[dict] = []
    targeted = set()
    for r in recs:
        tgt = ritems.get(r.target_item_id) if r.target_item_id else None
        if tgt:
            targeted.add(tgt.id)
        tname = tgt.name if tgt else None
        bkey, blabel = _brand(tname, r.name)
        has_pkg, n_labels, langs = False, 0, None
        for ing in r.ingredients:
            it = ritems.get(ing.item_id)
            if it is None:
                continue
            k = _kind(it)
            if k == "packaging":
                has_pkg = True
            elif k == "label" or _is_lang_label(it):
                n_labels += 1
                if _is_lang_label(it):
                    av = set((sibs.get(it.label_group) or {}).keys()) | {it.language}
                    langs = av if langs is None else (langs & av)
        products.append({
            "key": f"r:{r.id}", "recipe_id": r.id, "recipe_name": r.name or None,
            "target_item_id": tgt.id if tgt else None,
            "name": (tname or r.name or f"Reçete #{r.id}").strip(),
            "brand": blabel, "brand_key": bkey,
            "finished_stock": round(float(tgt.current_stock or 0.0), 3) if tgt else None,
            "unit": (tgt.unit if tgt else None) or "adet",
            "size_ml": parse_ml(tname, r.name),
            "output_quantity": r.output_quantity,
            "has_packaging": has_pkg, "labels": n_labels,
            "label_langs": sorted(langs) if langs is not None else [],
            "recipeless": False,
            "primary": (tgt is None) or primary_of.get(tgt.id) == r.id,
            "target_active": bool(tgt.is_active) if tgt else None,
        })

    parents = _abstract_parent_ids(db)
    for it in (db.query(Item)
               .filter(Item.domain == domain, Item.is_active == True,     # noqa: E712
                       Item.category == "Bitmiş Ürün").all()):
        if it.id in targeted or it.id in parents:
            continue
        bkey, blabel = _brand(it.name, None)
        products.append({
            "key": f"i:{it.id}", "recipe_id": None, "recipe_name": None,
            "target_item_id": it.id, "name": (it.name or f"Ürün #{it.id}").strip(),
            "brand": blabel, "brand_key": bkey,
            "finished_stock": round(float(it.current_stock or 0.0), 3),
            "unit": it.unit or "adet", "size_ml": parse_ml(it.name),
            "output_quantity": None, "has_packaging": False, "labels": 0, "label_langs": [],
            "recipeless": True, "primary": True, "target_active": True,
        })

    from core.supplier_prices import normalize as fold
    products.sort(key=lambda p: (fold(p["brand"]), fold(p["name"]), p["key"]))
    brands: Dict[str, dict] = {}
    for p in products:
        b = brands.setdefault(p["brand_key"], {"key": p["brand_key"], "label": p["brand"], "count": 0})
        b["count"] += 1
    brand_list = sorted(brands.values(), key=lambda b: (-b["count"], fold(b["label"])))

    # Varsayılan hariçler — motorla AYNI çözüm (panel anahtarı → genel → ad kuralı)
    items = {it.id: item_rec(it) for it in db.query(Item).filter(Item.domain == domain).all()}
    cfg = {r.key: r.value for r in db.query(AppSetting).filter(
        AppSetting.key.in_((default_excluded_key(domain), CFG_DEFAULT_EXCLUDED))).all()}
    ex_ids = resolve_default_excluded(cfg.get(default_excluded_key(domain)), cfg.get(CFG_DEFAULT_EXCLUDED),
                                      items, domain)
    return {
        "brands": brand_list,
        "products": products,
        "defaults": {"excluded_items": [{"id": i, "name": items[i].name, "unit": items[i].unit}
                                        for i in ex_ids if i in items]},
        "stock_as_of": _tr(datetime.utcnow()),
    }


@router.get("/products")
def plan_products(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    """Ürün seçim listesi + marka çipleri + varsayılan hariç kalemler."""
    return products_payload(db, domain)


# ─── Rapor ──────────────────────────────────────────────────────────────────

def _rates_if_needed(pin, currency: str):
    """Rapor para biriminden farklı FİYATLI teklif varsa bugünün kuru; yoksa None
    (ağ isteği yok).  Kur alınamazsa None → teklif "çevrilemedi" notuyla
    fiyatsız sayılır (attach)."""
    cur = (currency or "USD").upper()
    need = any((o.currency or "USD").upper() != cur
               for lst in pin.offers.values() for o in lst
               if o.unit_price is not None and o.unit_price > 0)
    if not need:
        return None
    try:
        from core.fx import today_rates
        return today_rates()
    except Exception:
        log.exception("Satın alma planı: kur alınamadı")
        return None


def build_report(db: Session, req: PlanRequest, domain: str) -> dict:
    """load_inputs → compute → load_price_inputs → attach.  `PlanInputError`
    (kullanıcıya gösterilebilir) yukarı fırlar; router 400'e çevirir."""
    from core import purchase_plan as pp
    from core import purchase_pricing as pricing

    inputs = pp.load_inputs(db, req, domain)
    res = pp.compute(inputs, req)
    prods = res.get("products") or []
    if not prods or all(any(c["code"] == "recipeless_product" for c in p["cautions"]) for p in prods):
        raise pp.PlanInputError(
            "Seçilen ürünlerin hiçbirinin reçetesi yok; hesaplanacak malzeme bulunamadı. "
            "Reçetesiz ürüne ⚙ ayarlarından başka bir ürünün reçetesini seçebilirsiniz.")
    ids, alt_ids = set(), set()
    for m in (res.get("materials") or []) + (res.get("held") or []):
        ids.update(i for i in (m.get("member_ids") or []) if i is not None)
        alt_ids.update(a["item_id"] for a in ((m.get("material_group") or {}).get("alts") or []))
    pin = pricing.load_price_inputs(db, ids, domain, extra_item_ids=alt_ids - ids)
    cur = req.options.currency
    pricing.attach(res, pin, _rates_if_needed(pin, cur), currency=cur,
                   round_to_package=req.options.round_to_package)
    return res


def _sup_lines(m: dict, cur: str) -> List[dict]:
    """Tedarikçi hücresi — PDF `_sup_cell` ile aynı sıra/metin, yapısal:
    [{t: metin, s: 'b' kalın | 'g' gri | 'r' kırmızı}]."""
    from core.purchase_plan import _amount_near
    from core.purchase_plan_pdf import PKG_SUFFIX, _qty
    from core.purchase_pricing import (CURRENCY_SYMBOL, UNIT_TEXT, money, price_text, relation_texts,
                                       unpriced_offer_text)
    sym = CURRENCY_SYMBOL.get(cur, cur)
    out: List[dict] = []
    offers = m.get("offers") or []
    priced = [o for o in offers if o.get("price") is not None]
    if m.get("group") == "list" and priced:
        for i, o in enumerate(priced):
            unit = UNIT_TEXT.get(o.get("price_unit"), o.get("price_unit") or "")
            t = f"{o['name']} — {price_text(o['price'])} {sym}/{unit}"
            if o.get("package"):
                pu = o.get("orig_price_unit") or o.get("price_unit") or "kg"
                t += f" · {_qty(o['package'])} {PKG_SUFFIX.get(pu, pu)} ambalaj"
            if o.get("orig_currency") and o["orig_currency"] != cur and o.get("orig_price") is not None:
                osym = CURRENCY_SYMBOL.get(o["orig_currency"], o["orig_currency"])
                ou = UNIT_TEXT.get(o.get("orig_price_unit"), o.get("orig_price_unit") or unit)
                t += f" ({price_text(o['orig_price'])} {osym}/{ou})"
            out.append({"t": t, "s": "b"} if i == 0 else {"t": t + " (daha pahalı)", "s": "g"})
    else:
        out.append({"t": "Fiyat yok — teklif alınacak", "s": "r"})
    for o in offers:
        if o.get("price") is None:
            out.append({"t": unpriced_offer_text(o), "s": "g"})
    # İlişkiler (stok kartı / son alım / numune · Sipariş: · Aynı malzeme:) — PDF ile tek kaynak
    out += [{"t": t, "s": "g"} for t in relation_texts(m)]
    if m.get("pkg_buy") is not None:
        out.append({"t": f"Ambalaj katına yuvarlanırsa: {_amount_near(m['pkg_buy'], m['price_unit'])}"
                         f" · {money(m.get('pkg_amount'), cur)}", "s": "g"})
    return out


def _preview_view(res: dict) -> dict:
    """Önizleme yanıtı: rapor + `sections` (malzeme bölümlerinde `row_keys`)
    + malzeme başına sunucuda biçimlenmiş `ui` (tedarikçi hücresi, tutar) —
    TR sayı biçimi tek kaynakta (Python) kalsın, JS yeniden yazmasın."""
    from core.purchase_pricing import firms_view, money, sections
    cur = (res.get("pricing") or {}).get("currency") or "USD"
    for m in (res.get("materials") or []) + (res.get("held") or []):
        m["ui"] = {"sup": _sup_lines(m, cur),
                   "amount": money(m["amount"], cur) if m.get("amount") is not None else None,
                   "name_notes": [c["text"] for c in m.get("cautions") or [] if c["code"] in _NAME_CAUTIONS],
                   # "Tedarikçiler (N)" açılır listesi: firma dökümü + aynı malzemenin diğer kartları
                   "firms": firms_view(m, cur)}
    secs = []
    for s in sections(res):
        d = {k: v for k, v in s.items() if k != "rows"}
        if s["key"] in _MATERIAL_SECTIONS:
            d["row_keys"] = [m["key"] for m in s["rows"]]
        elif s["key"] == "products":
            d["row_nos"] = [p["no"] for p in s["rows"]]
        else:
            d["rows"] = s["rows"]
        if s.get("total") is not None:
            d["total_text"] = money(s["total"], cur)
        secs.append(d)
    res["sections"] = secs
    return res


def _touch_scenario(db: Session, scenario_id: Optional[int], domain: str) -> None:
    """`last_run_at` damgası — updated_at'e (onupdate) DOKUNMADAN."""
    if not scenario_id:
        return
    try:
        (db.query(PurchasePlan)
         .filter(PurchasePlan.id == scenario_id, PurchasePlan.domain == domain,
                 PurchasePlan.is_active == True)                       # noqa: E712
         .update({PurchasePlan.last_run_at: datetime.utcnow(),
                  PurchasePlan.updated_at: PurchasePlan.updated_at}, synchronize_session=False))
        db.commit()
    except Exception:
        db.rollback()


@router.post("/preview")
def plan_preview(
    req: PlanRequest,
    scenario_id: Optional[int] = Query(None, ge=1),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    """PlanRequest → rapor JSON + bölümler (UI önizlemesi)."""
    from core.purchase_plan import PlanInputError
    try:
        res = build_report(db, req, domain)
    except PlanInputError as e:
        return _err(400, str(e))
    _touch_scenario(db, scenario_id, domain)
    return _preview_view(res)


def _slug(title: Optional[str]) -> str:
    """Dosya adı parçası: TR-katlanmış ASCII ('Rusya Siparişi' → 'rusya_siparisi')."""
    from core.supplier_prices import normalize
    s = re.sub(r"[^a-z0-9]+", "_", normalize(title or "")).strip("_")
    return s[:50].strip("_")


@router.post("/export")
@limiter.limit("20/minute")
def plan_export(
    request: Request,
    req: PlanRequest,
    format: str = Query("pdf", pattern="^(pdf|xlsx)$"),
    scenario_id: Optional[int] = Query(None, ge=1),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    """Aynı rapor PDF (yatay A4) ya da formüllü Excel olarak."""
    from core.purchase_plan import PlanInputError
    from core.delivery_note import content_disposition
    try:
        res = build_report(db, req, domain)
    except PlanInputError as e:
        return _err(400, str(e))
    try:
        if format == "pdf":
            from core.purchase_plan_pdf import render_pdf
            content, media = render_pdf(res), "application/pdf"
        else:
            from core.purchase_plan_xlsx import build_workbook
            content = build_workbook(res)
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    except Exception:
        log.exception("Satın alma planı çıktısı üretilemedi")
        return _err(500, "Rapor üretilemedi.")
    title = res["meta"].get("title") or "Satın Alma Planı"
    pr = res.get("pricing") or {}
    log_admin_event(db, request, actor=current_user, action="purchase_plan.export",
                    target_type="purchase_plan", target_id=scenario_id, target_name=title[:200],
                    details={"format": format, "domain": domain, "lines": len(req.lines),
                             "materials": len(res.get("materials") or []),
                             "currency": pr.get("currency"), "total": (pr.get("totals") or {}).get("all")})
    db.commit()
    _touch_scenario(db, scenario_id, domain)
    # Başlık girilmediyse varsayılan "Satın Alma Planı" önekle aynı → tekrar yazılmaz
    slug = _slug(req.title)
    fname = f"satin_alma_plani_{slug + '_' if slug else ''}{tr_now():%Y%m%d}.{format}"
    return StreamingResponse(io.BytesIO(content), media_type=media,
                             headers={"Content-Disposition": content_disposition(fname, inline=(format == "pdf"))})


# ─── Geçmiş üretimden doldur ────────────────────────────────────────────────

@router.get("/history-fill")
def plan_history_fill(
    start: Optional[date] = Query(None),
    end: Optional[date] = Query(None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    """{target_item_id: üretilen toplam} — tarihler TR-yerel gün (varsayılan
    son 12 ay; `core.production_history_report` ile aynı kurallar)."""
    from core import production_history_report as phr
    if not (phr.date_in_range(start) and phr.date_in_range(end)):
        return _err(400, f"Tarihler {phr.MIN_DATE:%d.%m.%Y} – {phr.MAX_DATE:%d.%m.%Y} aralığında olmalı.")
    d_start, d_end = phr.default_range()
    start = start or (d_start if end is None else end - (d_end - d_start))
    end = end or d_end
    if start > end:
        return _err(400, "Başlangıç tarihi bitiş tarihinden sonra olamaz.")
    if (end - start).days > 3660:
        return _err(400, "Tarih aralığı en fazla 10 yıl olabilir.")
    return {str(k): v for k, v in phr.history_quantities(db, domain, start, end).items()}


# ─── Taslak kart denetimi ───────────────────────────────────────────────────

_ITEM_REFS_MAX = 1000


@router.get("/item-refs")
def plan_item_refs(
    ids: str = Query("", max_length=12000),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    """Tarayıcı taslağındaki kart id'lerinden aktif OLMAYANLAR — `GET
    /scenarios/{id}` → `missing[]` ile aynı biçim (`kind/id/name/status`).

    Taslak eskiden istemcide `/api/items` listesine bakılarak denetleniyordu:
    o liste yalnız AKTİF kartları verir (pasif kart "silinmiş" sanılıp
    önizlemeden düşüyordu — bekletilen pasif kart alınacaklara karışıyordu)
    ve sessionStorage önbelleği panele göre ayrılmadığı için panel geçişinden
    hemen sonra öteki panelin listesini görebiliyordu.  Karar artık sunucuda:
    'not_found' (silinmiş / bu panelde değil → önizlemeye gönderilmez),
    'inactive' (pasif kart → hesap yapılır, sunucu uyarır)."""
    from database import Item
    want = list(dict.fromkeys(int(x) for x in re.findall(r"\d{1,9}", ids or "")))
    if len(want) > _ITEM_REFS_MAX:
        return _err(400, f"En fazla {_ITEM_REFS_MAX} kart denetlenebilir.")
    found = {i.id: i for i in (db.query(Item.id, Item.name, Item.is_active)
                               .filter(Item.id.in_(want), Item.domain == domain).all() if want else [])}
    out: List[dict] = []
    for iid in want:
        it = found.get(iid)
        if it is not None and it.is_active:
            continue
        out.append({"kind": "item", "id": iid, "name": it.name if it else None,
                    "status": "not_found" if it is None else "inactive"})
    return {"missing": out}


# ─── Senaryolar ─────────────────────────────────────────────────────────────

def _clean_name(v: str) -> str:
    return " ".join((v or "").split())


class ScenarioIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    config: PlanRequest

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        v = _clean_name(v)
        if not v:
            raise ValueError("Senaryo adı boş olamaz")
        return v


class ScenarioUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=150)
    config: Optional[PlanRequest] = None

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if v is None:
            return v
        v = _clean_name(v)
        if not v:
            raise ValueError("Senaryo adı boş olamaz")
        return v


def _dump_config(cfg: PlanRequest) -> str:
    return json.dumps({"version": SCENARIO_VERSION, **cfg.model_dump(mode="json")}, ensure_ascii=False)


def _parse_config(raw: Optional[str]):
    """(PlanRequest|None, ham dict, sürüm, hata metni|None).  Bozuk/uyumsuz
    config senaryoyu açılamaz yapmaz — ham hâli + hata döner."""
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return None, {}, None, "Senaryo yapılandırması okunamadı (bozuk JSON)."
    if not isinstance(data, dict):
        return None, {}, None, "Senaryo yapılandırması okunamadı."
    version = data.pop("version", None)
    try:
        return PlanRequest.model_validate(data), data, version, None
    except ValidationError as e:
        first = (e.errors() or [{}])[0]
        return None, data, version, f"Senaryo yapılandırması geçersiz: {first.get('msg', 'doğrulama hatası')}"


def _editor_role(db: Session, current_user: dict) -> bool:
    u = db.query(User).filter(User.id == _uid(current_user)).first()
    return bool(u and u.is_active and u.role in EDITOR_ROLES)


def _can_edit(p: PurchasePlan, uid: int, is_editor: bool) -> bool:
    return is_editor or (p.created_by_id is not None and p.created_by_id == uid)


def _name_taken(db: Session, domain: str, name: str, exclude_id: Optional[int] = None) -> bool:
    from core.supplier_prices import normalize
    key = normalize(name)
    q = db.query(PurchasePlan.id, PurchasePlan.name).filter(
        PurchasePlan.domain == domain, PurchasePlan.is_active == True)   # noqa: E712
    return any(normalize(n) == key and i != exclude_id for i, n in q.all())


def _scenario_row(p: PurchasePlan, can_edit: bool, lines: Optional[int] = None) -> dict:
    return {
        "id": p.id, "name": p.name, "domain": p.domain,
        "created_by": p.created_by, "created_by_id": p.created_by_id, "updated_by": p.updated_by,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        "last_run_at": p.last_run_at.isoformat() if p.last_run_at else None,
        "created_at_tr": _tr(p.created_at), "updated_at_tr": _tr(p.updated_at),
        "last_run_at_tr": _tr(p.last_run_at),
        "lines": lines, "can_edit": can_edit,
    }


def _get_scenario(db: Session, scenario_id: int, domain: str) -> Optional[PurchasePlan]:
    return (db.query(PurchasePlan)
            .filter(PurchasePlan.id == scenario_id, PurchasePlan.domain == domain,
                    PurchasePlan.is_active == True)                      # noqa: E712
            .first())


def missing_refs(db: Session, req: PlanRequest, domain: str) -> List[dict]:
    """Senaryonun baktığı ama artık panelde olmayan / pasif reçete ve kartlar.

    status: 'not_found' (silinmiş ya da bu panelde değil — önizlemeye
    gönderilemez) | 'inactive' (reçete pasif → gönderilemez; kart pasif →
    hesap yine yapılır, uyarılır).  UI bunları İŞARETLER, sessizce düşürmez.
    """
    from database import Item, Recipe
    rec_ids = {ln.recipe_id for ln in req.lines if ln.recipe_id is not None}
    o = req.options
    item_refs: List[tuple] = []          # (alan, satır no|None, item_id)
    for no, ln in enumerate(req.lines, 1):
        if ln.target_item_id is not None:
            item_refs.append(("target", no, ln.target_item_id))
        for ex in ln.extra_packaging:
            if ex.item_id is not None:
                item_refs.append(("extra_packaging", no, ex.item_id))
    for iid in o.excluded_item_ids or []:
        item_refs.append(("excluded", None, iid))
    for h in o.held_items:
        item_refs.append(("held", None, h.item_id))
    for iid in o.item_notes:
        item_refs.append(("item_notes", None, int(iid)))
    for pair in o.manual_merges:
        for iid in pair:
            item_refs.append(("manual_merges", None, iid))
    recs = {r.id: r for r in (db.query(Recipe.id, Recipe.name, Recipe.is_active)
                              .filter(Recipe.id.in_(rec_ids), Recipe.domain == domain).all()
                              if rec_ids else [])}
    ids = {i for _, _, i in item_refs}
    its = {i.id: i for i in (db.query(Item.id, Item.name, Item.is_active)
                             .filter(Item.id.in_(ids), Item.domain == domain).all() if ids else [])}
    # satır alanları iyelik ekiyle ("3. satırın ürünü"), seçenek alanları yalın
    field_text = {"target": "ürünü", "extra_packaging": "ek ambalajı", "excluded": "Hariç kalem",
                  "held": "Bekletilen kalem", "item_notes": "Kalem notu", "manual_merges": "Elle birleştirme"}
    out: List[dict] = []
    for no, ln in enumerate(req.lines, 1):
        rid = ln.recipe_id
        if rid is None:
            continue
        r = recs.get(rid)
        if r is None or not r.is_active:
            st = "not_found" if r is None else "inactive"
            out.append({"kind": "recipe", "field": "recipe", "line": no, "id": rid,
                        "name": r.name if r else None, "status": st,
                        "text": f"{no}. satırın reçetesi (#{rid}" + (f" {r.name}" if r else "") + ") "
                                + ("bulunamadı — silinmiş ya da bu panelde değil." if r is None else "pasif.")})
    seen = set()
    for fld, no, iid in item_refs:
        it = its.get(iid)
        if it is not None and it.is_active:
            continue
        k = (fld, no, iid)
        if k in seen:
            continue
        seen.add(k)
        st = "not_found" if it is None else "inactive"
        where = f"{no}. satırın {field_text[fld]}" if no else field_text[fld]
        out.append({"kind": "item", "field": fld, "line": no, "id": iid,
                    "name": it.name if it else None, "status": st,
                    "text": f"{where} (#{iid}" + (f" {it.name}" if it else "") + ") "
                            + ("bulunamadı — silinmiş ya da bu panelde değil." if it is None else "pasif kart.")})
    return out


@router.get("/scenarios")
def list_scenarios(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    uid, is_editor = _uid(current_user), _editor_role(db, current_user)
    rows = (db.query(PurchasePlan)
            .filter(PurchasePlan.domain == domain, PurchasePlan.is_active == True)   # noqa: E712
            .order_by(PurchasePlan.updated_at.desc(), PurchasePlan.id.desc()).all())
    out = []
    for p in rows:
        try:
            n = len((json.loads(p.config or "{}") or {}).get("lines") or [])
        except (TypeError, ValueError, AttributeError):
            n = None
        out.append(_scenario_row(p, _can_edit(p, uid, is_editor), n))
    return {"scenarios": out}


@router.post("/scenarios", status_code=201)
def create_scenario(
    data: ScenarioIn,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    if _name_taken(db, domain, data.name):
        return _err(409, f"“{data.name}” adlı bir senaryo zaten var.")
    actor = _actor(current_user)
    p = PurchasePlan(name=data.name, config=_dump_config(data.config), domain=domain,
                     created_by_id=_uid(current_user) or None, created_by=actor, updated_by=actor)
    db.add(p)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return _err(409, f"“{data.name}” adlı bir senaryo zaten var.")
    log_admin_event(db, request, actor=current_user, action="purchase_plan.create",
                    target_type="purchase_plan", target_id=p.id, target_name=p.name,
                    details={"domain": domain, "lines": len(data.config.lines)})
    db.commit()
    db.refresh(p)
    return _scenario_row(p, True, len(data.config.lines))


@router.get("/scenarios/{scenario_id}")
def get_scenario(
    scenario_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    p = _get_scenario(db, scenario_id, domain)
    if not p:
        return _err(404, "Senaryo bulunamadı.")
    req, raw, version, error = _parse_config(p.config)
    d = _scenario_row(p, _can_edit(p, _uid(current_user), _editor_role(db, current_user)),
                      len(req.lines) if req else None)
    d.update({"version": version, "config": req.model_dump(mode="json") if req else raw,
              "config_error": error, "missing": missing_refs(db, req, domain) if req else []})
    return d


@router.put("/scenarios/{scenario_id}")
def update_scenario(
    scenario_id: int,
    data: ScenarioUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    p = _get_scenario(db, scenario_id, domain)
    if not p:
        return _err(404, "Senaryo bulunamadı.")
    if not _can_edit(p, _uid(current_user), _editor_role(db, current_user)):
        return _err(403, "Bu senaryoyu yalnız sahibi, SuperAdmin ya da Manager değiştirebilir.")
    if data.name is None and data.config is None:
        return _err(400, "Değiştirilecek alan yok.")
    details: dict = {"domain": domain}
    if data.name is not None and data.name != p.name:
        if _name_taken(db, domain, data.name, exclude_id=p.id):
            return _err(409, f"“{data.name}” adlı bir senaryo zaten var.")
        details["renamed_from"] = p.name
        p.name = data.name
    if data.config is not None:
        p.config = _dump_config(data.config)
        details["lines"] = len(data.config.lines)
    p.updated_by = _actor(current_user)
    p.updated_at = datetime.utcnow()
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return _err(409, f"“{data.name}” adlı bir senaryo zaten var.")
    log_admin_event(db, request, actor=current_user, action="purchase_plan.update",
                    target_type="purchase_plan", target_id=p.id, target_name=p.name, details=details)
    db.commit()
    db.refresh(p)
    req, _raw, _v, _e = _parse_config(p.config)
    return _scenario_row(p, True, len(req.lines) if req else None)


@router.delete("/scenarios/{scenario_id}")
def delete_scenario(
    scenario_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("reports", "view")),
    domain: str = Depends(active_domain),
):
    p = _get_scenario(db, scenario_id, domain)
    if not p:
        return _err(404, "Senaryo bulunamadı.")
    if not _can_edit(p, _uid(current_user), _editor_role(db, current_user)):
        return _err(403, "Bu senaryoyu yalnız sahibi, SuperAdmin ya da Manager silebilir.")
    p.is_active = False
    p.updated_by = _actor(current_user)
    log_admin_event(db, request, actor=current_user, action="purchase_plan.delete",
                    target_type="purchase_plan", target_id=p.id, target_name=p.name,
                    details={"domain": domain})
    db.commit()
    return {"message": "Senaryo silindi.", "id": p.id}
