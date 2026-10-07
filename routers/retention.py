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
import json
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import (get_db, to_tr, AppSetting, Inventory, Item,
                      RetentionSample, RetentionSampleCheck,
                      RetentionSampleMovement, Transaction)
from core.audit import log_admin_event
from core.brands import cabinet_of
from core.domain import active_domain
from core.permissions import require_permission
from core.retention import (CFG_EXTRA, CFG_SHELF_LIFE, CHECK_ITEM_KEYS,
                            CHECK_RESULTS, CHECK_STATES, CHECKOUT_REASONS,
                            DEFAULT_EXTRA_MONTHS, DEFAULT_SHELF_LIFE_MONTHS,
                            check_item_label, check_result_label,
                            check_state_label, expiry_state, location_label,
                            movement_label, reason_label, retention_until,
                            status_label)

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
    """Kısmi güncelleme — "gönderilmedi" ile "null gönderildi" AYRI şeydir.

    Alan gönderilmediyse dokunulmaz; `null`/`""` gönderildiyse TEMİZLENİR.
    Ayrım `model_fields_set` ile yapılır (pydantic v2).

    Bu ayrım olmadan eski davranış hatalıydı: yalnız `shelf` gönderen bir
    istek `slot`'u sessizce siliyordu (`data.slot` None → temizle).  Mevcut
    şablon dört alanı birden yolladığı için gizli kalmıştı; hızlı/kısmi
    konum girişiyle gerçek veri kaybına dönüşürdü.
    """
    shelf: Optional[str] = Field(None, max_length=20)
    slot: Optional[str] = Field(None, max_length=20)
    produced_at: Optional[date] = None
    retention_until: Optional[date] = None
    note: Optional[str] = Field(None, max_length=500)
    needs_review: Optional[bool] = None


class LocationRow(BaseModel):
    id: int
    shelf: Optional[str] = Field(None, max_length=20)
    slot: Optional[str] = Field(None, max_length=20)


class BulkLocationBody(BaseModel):
    rows: List[LocationRow] = Field(..., min_length=1, max_length=500)


class CheckItemRow(BaseModel):
    key: str = Field(..., max_length=40)
    state: str = Field(..., max_length=20)          # normal | degisim
    note: Optional[str] = Field(None, max_length=300)


class CheckCreateBody(BaseModel):
    checked_on: Optional[date] = None               # boş = bugün
    items: List[CheckItemRow] = Field(..., min_length=1, max_length=20)
    result: str = Field(..., max_length=20)         # uygun | uygun_degil
    result_note: Optional[str] = Field(None, max_length=1000)


class DeleteBody(BaseModel):
    reason: str = Field(..., min_length=3, max_length=300)


class VariationBody(BaseModel):
    item_id: int                                     # hedef varyasyon (boy)


class VariationRow(BaseModel):
    id: int
    item_id: int


class BulkVariationBody(BaseModel):
    rows: List[VariationRow] = Field(..., min_length=1, max_length=500)


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


def _check_view(c: RetentionSampleCheck) -> dict:
    """Kontrol kaydı görünümü.  `properties` bozuk JSON ise BOŞ liste —
    500'e düşmez (routers/sample_analysis.py:60-63 kalıbı)."""
    try:
        items = json.loads(c.properties or "[]")
        if not isinstance(items, list):
            items = []
    except (TypeError, ValueError):
        items = []
    return {
        "id": c.id,
        "checked_on": c.checked_on.isoformat() if c.checked_on else "",
        "checked_on_label": c.checked_on.strftime("%d.%m.%Y") if c.checked_on else "—",
        "result": c.result,
        "result_label": check_result_label(c.result),
        "result_note": c.result_note or "",
        "checked_by": c.checked_by or "",
        "created_at": to_tr(c.created_at).strftime("%d.%m.%Y %H:%M") if c.created_at else "",
        "items": [{
            "key": (it or {}).get("key", ""),
            "label": check_item_label((it or {}).get("key")),
            "state": (it or {}).get("state", ""),
            "state_label": check_state_label((it or {}).get("state")),
            "note": (it or {}).get("note") or "",
        } for it in items],
    }


def _size_map(db: Session, rows) -> dict:
    """{item_id: {variation_name, parent_id, parent_name, is_parent}} — sabit 3 sorgu.

    BOY AYRI BİR KOLON DEĞİL.  Bu sistemde boy, varyasyon `Item` satırının
    kendisidir (`parent_id` + `variation_name`); dolap kaydı yalnız `item_id`
    tutar.  Burada okunur, KOPYALANMAZ — ürün yeniden adlandırılsa da dolap
    doğru boyu göstermeye devam eder ve tek doğruluk kaynağı `Item` kalır.

    `is_parent` = kaydın işaret ettiği ürünün alt varyasyonu var demektir; yani
    o kaydın boyu BELİRSİZ (soyut ana ürüne yazılmış).  03.08.2026 sayımından
    gelen 21 kayıt bu durumda — el yazısı listede boy yazmıyordu.
    """
    ids = {r.item_id for r in rows if r.item_id}
    if not ids:
        return {}
    items = {i.id: i for i in db.query(Item).filter(Item.id.in_(ids)).all()}
    parent_ids = {i.parent_id for i in items.values() if i.parent_id}
    parents = ({p.id: p for p in db.query(Item).filter(Item.id.in_(parent_ids)).all()}
               if parent_ids else {})
    # Ailenin yaprakları — arayüzdeki "Boy" seçicisini besler.  Aile kökü
    # `parent_id or id`, böylece ana ürüne yazılmış kayıt da kardeşlerini görür.
    roots = {(i.parent_id or i.id) for i in items.values()}
    by_root: dict = {}
    for c in (db.query(Item)
              .filter(Item.parent_id.in_(roots),
                      Item.is_active == True)                  # noqa: E712
              .order_by(Item.name).all()):
        by_root.setdefault(c.parent_id, []).append(c)
    with_children = {row[0] for row in db.query(Item.parent_id)
                     .filter(Item.parent_id.in_(ids)).distinct().all()}
    out = {}
    for iid, it in items.items():
        parent = parents.get(it.parent_id)
        out[iid] = {
            "variation_name": it.variation_name or "",
            "parent_id": it.parent_id,
            "parent_name": parent.name if parent else "",
            "is_parent": iid in with_children,
            "variants": [{"item_id": v.id, "name": v.name,
                          "variation_name": v.variation_name or ""}
                         for v in by_root.get(it.parent_id or it.id, [])],
        }
    return out


def _view(r: RetentionSample, last_check: Optional[RetentionSampleCheck] = None,
          check_count: int = 0, size: Optional[dict] = None) -> dict:
    st = expiry_state(r.retention_until)
    size = size or {}
    return {
        "id": r.id,
        "item_id": r.item_id,
        "item_name": r.item_name or (r.item.name if r.item else ""),
        "lot_number": r.lot_number,
        "brand": r.brand,
        # Boy — `Item`'dan okunur (bkz. _size_map).  `is_parent` True ise bu
        # kaydın boyu seçilmemiştir ve arayüz uyarı rozeti gösterir.
        "variation_name": size.get("variation_name", ""),
        "parent_id": size.get("parent_id"),
        "parent_name": size.get("parent_name", ""),
        "is_parent": bool(size.get("is_parent")),
        "variants": size.get("variants", []),
        # Üretimden gelen kayıt `-S` Inventory lotuna bağlıdır; boyu
        # değiştirilemez (arayüz butonu da gizler).
        "inventory_bound": bool(r.inventory_id),
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
        "needs_review": bool(r.needs_review),
        # Son kontrol — denormalize kolon YOK; liste ucu tek gruplanmış
        # sorguyla doldurur, detay ucu tam listeyi verir.
        "last_check_on": (last_check.checked_on.strftime("%d.%m.%Y")
                          if last_check and last_check.checked_on else ""),
        "last_check_result": last_check.result if last_check else "",
        "last_check_result_label": (check_result_label(last_check.result)
                                    if last_check else ""),
        "check_count": check_count,
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
                    RetentionSample.domain == domain,
                    RetentionSample.is_active == True).first())      # noqa: E712


def _get_locked(db: Session, sample_id: int, domain: str) -> Optional[RetentionSample]:
    """Çıkış/imha için numune kaydı — FOR UPDATE, sıra kart → lot → kayıt
    (core/production_cancel.lock_rows ile aynı; ters sıra kilitlenme
    yaratırdı).  `is_active`/`status`/`quantity` KİLİTTEN SONRA okunur:
    eşzamanlı üretim iptali kaydı kapattıysa (pasif, 0 adet) bayat kopya
    üzerinden stok ikinci kez düşülmez — kayıt bulunamaz (404)."""
    r = _get(db, sample_id, domain)
    if r is None:
        return None
    (db.query(Item).filter(Item.id == r.item_id)
     .with_for_update().populate_existing().first())
    if r.inventory_id:
        (db.query(Inventory).filter(Inventory.id == r.inventory_id)
         .with_for_update().populate_existing().first())
    return (db.query(RetentionSample)
            .filter(RetentionSample.id == sample_id,
                    RetentionSample.domain == domain,
                    RetentionSample.is_active == True)               # noqa: E712
            .with_for_update().populate_existing().first())


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
    """Marka bazlı dolap özeti + her markanın içinde ÜRÜN×BOY kırılımı.

    Kırılım şart, çünkü aynı ürünün 200 ml'si ve 500 ml'si AYRI batch'lerde
    üretiliyor ve her boydan ayrı şahit numune saklanıyor — tek "marka" kovası
    ikisini tek rakamda topluyordu.
    """
    rows = (db.query(RetentionSample)
            .filter(RetentionSample.domain == domain,
                    RetentionSample.is_active == True,                # noqa: E712
                    RetentionSample.status == "stored").all())
    today = date.today()
    sizes = _size_map(db, rows)
    by_brand: dict = {}
    total = expired = due_soon = unsized = 0.0
    for r in rows:
        b = by_brand.setdefault(r.brand, {"brand": r.brand, "samples": 0,
                                          "quantity": 0.0, "expired": 0,
                                          "unsized": 0, "_products": {}})
        sz = sizes.get(r.item_id) or {}
        is_parent = bool(sz.get("is_parent"))
        p = b["_products"].setdefault(r.item_id, {
            "item_id": r.item_id,
            "name": sz.get("parent_name") or r.item_name or "",
            "variation_name": sz.get("variation_name", ""),
            "is_parent": is_parent,
            "samples": 0, "quantity": 0.0, "expired": 0,
        })
        b["samples"] += 1
        b["quantity"] += r.quantity or 0.0
        p["samples"] += 1
        p["quantity"] += r.quantity or 0.0
        total += r.quantity or 0.0
        if is_parent:                       # boyu seçilmemiş kayıt
            b["unsized"] += 1
            unsized += 1
        st = expiry_state(r.retention_until, today)
        if st == "expired":
            b["expired"] += 1
            p["expired"] += 1
            expired += 1
        elif st == "due_soon":
            due_soon += 1
    cabinets = []
    for b in sorted(by_brand.values(), key=lambda x: x["brand"]):
        prods = b.pop("_products")
        b["products"] = sorted(prods.values(),
                               key=lambda p: (p["name"], p["variation_name"]))
        cabinets.append(b)
    return {
        "cabinets": cabinets,
        "totals": {"quantity": total, "samples": len(rows),
                   "expired": int(expired), "due_soon": int(due_soon),
                   "unsized": int(unsized)},
        "reasons": [{"key": k, "label": reason_label(k)} for k in CHECKOUT_REASONS],
    }


def _filtered_query(db: Session, domain: str, *, brand=None, q=None, status=None,
                    expired=None, needs_review=None, unchecked=None,
                    item_id=None, unsized=None):
    """Liste + dışa aktarım (PDF/Excel) için TEK filtre yolu.

    Rapor ekrandakiyle birebir aynı kayıtları basmalı — iki uç filtreyi ayrı
    ayrı kursaydı biri değişince "ekranda 40, çıktıda 42" farkı sessizce
    doğardı.  Sıralama/limit çağırana aittir.
    """
    query = db.query(RetentionSample).filter(
        RetentionSample.domain == domain,
        RetentionSample.is_active == True)                            # noqa: E712
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
    if item_id:
        query = query.filter(RetentionSample.item_id == item_id)
    if unsized:
        # Boyu seçilmemişler: kayıt SOYUT ana ürüne yazılmış, yani o ürünün
        # alt varyasyonları var.  `unchecked` filtresindeki EXISTS kalıbı.
        query = query.filter(db.query(Item.id)
                             .filter(Item.parent_id == RetentionSample.item_id)
                             .exists())
    if needs_review:
        query = query.filter(RetentionSample.needs_review == True)    # noqa: E712
    if unchecked:
        # Hiç kontrol edilmemişler — KULLANICI TETİKLEMELİ bir mercek.
        # KPI şeridinde kalıcı sayaç yok; kullanıcı "sistem dürtmesin" dedi.
        query = query.filter(~db.query(RetentionSampleCheck)
                             .filter(RetentionSampleCheck.sample_id == RetentionSample.id)
                             .exists())
    return query


def _row_views(db: Session, rows) -> list:
    """Kayıtları `_view` görünümüne çevir — liste ucu ve rapor ortak kullanır."""
    # Son kontrol özeti — TEK ek sorgu (N+1 yok, denormalize kolon yok).
    last, counts = {}, {}
    if rows:
        ids = [r.id for r in rows]
        for c in (db.query(RetentionSampleCheck)
                  .filter(RetentionSampleCheck.sample_id.in_(ids))
                  .order_by(RetentionSampleCheck.checked_on.asc(),
                            RetentionSampleCheck.id.asc()).all()):
            last[c.sample_id] = c                      # son yazan kazanır
            counts[c.sample_id] = counts.get(c.sample_id, 0) + 1
    sizes = _size_map(db, rows)
    return [_view(r, last.get(r.id), counts.get(r.id, 0), sizes.get(r.item_id))
            for r in rows]


def _ordered(query):
    return query.order_by(RetentionSample.retention_until.asc().nullslast(),
                          RetentionSample.id.desc())


@router.get("/retention/samples")
def list_samples(
    brand: Optional[str] = Query(None),
    q: Optional[str] = Query(None, max_length=100),
    status: Optional[str] = Query(None),
    expired: Optional[int] = Query(None),
    needs_review: Optional[int] = Query(None),
    unchecked: Optional[int] = Query(None),
    item_id: Optional[int] = Query(None),
    unsized: Optional[int] = Query(None),
    limit: int = Query(300, ge=1, le=1000),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("retention", "view")),
    domain: str = Depends(active_domain),
):
    query = _filtered_query(db, domain, brand=brand, q=q, status=status,
                            expired=expired, needs_review=needs_review,
                            unchecked=unchecked, item_id=item_id, unsized=unsized)
    rows = _ordered(query).limit(limit).all()
    return {"rows": _row_views(db, rows), "count": len(rows)}


@router.get("/retention/samples/export")
def export_samples(
    format: str = Query("pdf", pattern="^(pdf|xlsx)$"),
    brand: Optional[str] = Query(None),
    q: Optional[str] = Query(None, max_length=100),
    status: Optional[str] = Query(None),
    expired: Optional[int] = Query(None),
    needs_review: Optional[int] = Query(None),
    unchecked: Optional[int] = Query(None),
    item_id: Optional[int] = Query(None),
    unsized: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("retention", "view")),
    domain: str = Depends(active_domain),
):
    """Dolap raporu — ekrandaki filtrelerle PDF (yazdırma) ya da Excel.

    Filtreler `list_samples` ile AYNI yoldan (`_filtered_query`) geçer; tek
    fark `limit` yok — "dolaptaki her şeyi yazdır" talebi (Işık Hanım).
    Bu uç `/retention/samples/{sample_id}`'den ÖNCE tanımlı olmalı; yoksa
    "export" int'e çevrilemeyip 422 döner.
    """
    from core.delivery_note import content_disposition
    from core.domain import domain_label
    from core.retention_report import (build_report, filters_text,
                                       render_pdf, render_xlsx, report_filename)
    query = _filtered_query(db, domain, brand=brand, q=q, status=status,
                            expired=expired, needs_review=needs_review,
                            unchecked=unchecked, item_id=item_id, unsized=unsized)
    views = _row_views(db, _ordered(query).all())

    item_label = None
    if item_id:
        it = (db.query(Item)
              .filter(Item.id == item_id, Item.domain == domain).first())
        item_label = it.name if it else f"#{item_id}"
    report = build_report(
        views,
        filters=filters_text(brand=brand, q=q, status=status, expired=expired,
                             needs_review=needs_review, unchecked=unchecked,
                             item_label=item_label, unsized=unsized),
        generated_by=_actor(current_user),
        domain_label=domain_label(domain))
    try:
        if format == "pdf":
            content = render_pdf(report)
            media = "application/pdf"
        else:
            content = render_xlsx(report)
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    except Exception:
        return _err(500, "Rapor üretilemedi.")
    return Response(
        content=content, media_type=media,
        headers={"Content-Disposition": content_disposition(
            report_filename(format), inline=(format == "pdf"))})


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
    checks = sorted(r.checks, key=lambda c: (c.checked_on or date.min, c.id))
    out = _view(r, checks[-1] if checks else None, len(checks),
                _size_map(db, [r]).get(r.item_id))
    out["movements"] = [_movement_view(m) for m in r.movements]
    out["checks"] = [_check_view(c) for c in reversed(checks)]   # en yeni üstte
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
    r = _get_locked(db, sample_id, domain)
    if not r:
        db.rollback()
        return _err(404, "Numune kaydı bulunamadı.")
    qty = round(float(data.quantity), 4)
    problem = None
    if r.status != "stored":
        problem = f"Bu numune '{status_label(r.status)}' durumunda — çıkış yapılamaz."
    elif qty > (r.quantity or 0.0):
        problem = f"Dolapta {r.quantity:g} adet var — {qty:g} adet çıkarılamaz."
    elif data.reason not in CHECKOUT_REASONS:
        problem = "Geçersiz çıkış sebebi."
    if problem:
        db.rollback()                                    # kilitler bırakılsın
        return _err(400, problem)

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
    out = _view(r, size=_size_map(db, [r]).get(r.item_id))
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
    r = _get_locked(db, sample_id, domain)
    if not r:
        db.rollback()
        return _err(404, "Numune kaydı bulunamadı.")
    qty = round(float(data.quantity), 4) if data.quantity is not None else (r.quantity or 0.0)
    problem = None
    if r.status != "stored":
        problem = f"Bu numune '{status_label(r.status)}' durumunda — imha edilemez."
    elif qty <= 0:
        problem = "İmha edilecek adet yok."
    elif qty > (r.quantity or 0.0):
        problem = f"Dolapta {r.quantity:g} adet var — {qty:g} adet imha edilemez."
    if problem:
        db.rollback()                                    # kilitler bırakılsın
        return _err(400, problem)

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
    out = _view(r, size=_size_map(db, [r]).get(r.item_id))
    out["message"] = f"{qty:g} adet imha edildi."
    return out


# DİKKAT: bu route "/retention/samples/{sample_id}" PUT'unun ÜSTÜNDE kalmalı —
# altına taşınırsa "bulk-location" sample_id sanılıp int'e çevrilmeye
# çalışılır ve 422 döner (aynı tuzak /production/next-lot'ta yaşandı).
@router.put("/retention/samples/bulk-location")
def bulk_update_location(
    data: BulkLocationBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("retention", "edit")),
    domain: str = Depends(active_domain),
):
    """Çok sayıda numunenin raf/göz bilgisini TEK istekte yaz.

    126 satırı tek tek PUT etmek yerine bunun sebebi: (a) atomiklik — yarım
    kalmış ağ isteği yarı dolu bir dolap bırakmaz, (b) denetim defteri —
    N×PUT `admin_audit_log`'a 126 satır yazardı, bu tek satır yazar.

    STOĞA DOKUNMAZ, Transaction YAZMAZ.  `konum` hareketi yalnız FİİLEN
    değişen satırlar için yazılır (değişmeyen satır geçmişi kirletmez).
    Alan sözleşmesi PUT ile aynı: gönderilmeyen alan korunur, boş/null temizler.
    """
    sent_by_id = {row.id: row.model_fields_set for row in data.rows}
    ids = list(sent_by_id.keys())
    rows = (db.query(RetentionSample)
            .filter(RetentionSample.id.in_(ids),
                    RetentionSample.domain == domain,          # domain izolasyonu ŞART
                    RetentionSample.is_active == True).all())  # noqa: E712
    by_id = {r.id: r for r in rows}
    actor = _actor(current_user)
    updated = 0
    skipped = len(ids) - len(rows)          # domain dışı / silinmiş / yok
    try:
        for row in data.rows:
            r = by_id.get(row.id)
            if r is None:
                continue
            if r.status == "destroyed":     # imha edilmişe konum yazmak anlamsız
                skipped += 1
                continue
            sent = sent_by_id[row.id]
            new_shelf = ((row.shelf or "").strip() or None) if "shelf" in sent else r.shelf
            new_slot = ((row.slot or "").strip() or None) if "slot" in sent else r.slot
            if new_shelf == r.shelf and new_slot == r.slot:
                continue                    # değişmedi → hareket yazma
            r.shelf, r.slot = new_shelf, new_slot
            db.add(RetentionSampleMovement(
                sample_id=r.id, movement_type="konum", quantity=0,
                note=location_label(r.shelf, r.slot), performed_by=actor))
            updated += 1
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Konumlar kaydedilemedi.")

    if updated:
        log_admin_event(db, request, actor=current_user, action="retention.bulk_location",
                        target_type="retention_sample", target_id=None,
                        target_name=f"{updated} numune",
                        details={"updated": updated, "skipped": skipped})
    return {"updated": updated, "skipped": skipped,
            "message": f"{updated} satırın konumu kaydedildi"
                       + (f", {skipped} atlandı" if skipped else "")}


def _variation_target(db: Session, r: RetentionSample, target_item_id: int,
                      domain: str):
    """Hedef boy geçerli mi?  → (Item, None) ya da (None, hata metni).

    Kural: hedef, kaydın ürün AİLESİNE ait bir yaprak olmalı.  Aile kökü
    `parent_id or id`; böylece hem ana ürüne yazılmış kayıt bir varyasyona,
    hem de yanlış varyasyona yazılmış kayıt kardeşine taşınabilir.
    """
    cur = db.query(Item).filter(Item.id == r.item_id).first()
    if cur is None:
        return None, "Kaydın ürünü bulunamadı."
    tgt = (db.query(Item)
           .filter(Item.id == target_item_id, Item.domain == domain).first())
    if tgt is None:
        return None, "Hedef ürün bulunamadı."
    if (tgt.parent_id or tgt.id) != (cur.parent_id or cur.id):
        return None, f"«{tgt.name}» bu numunenin ürününe ait bir boy değil."
    if db.query(Item.id).filter(Item.parent_id == tgt.id).first() is not None:
        return None, f"«{tgt.name}» bir ana üründür — boy olarak seçilemez."
    return tgt, None


def _apply_variation(db: Session, r: RetentionSample, tgt: Item, actor: str) -> bool:
    """Kaydı hedef boya taşı.  Stoğa DOKUNMAZ, Transaction YAZMAZ.

    Yalnız hangi ürün satırına ait olduğu düzeltilir; adet dolapta aynı kalır.
    Sonraki bir çıkış/imha artık DOĞRU boyun stoğundan düşer — asıl kazanç bu.
    """
    if tgt.id == r.item_id:
        return False
    old = r.item_name or ""
    r.item_id = tgt.id
    r.item_name = tgt.name
    db.add(RetentionSampleMovement(
        sample_id=r.id, movement_type="duzeltme", quantity=0,
        note=f"Boy düzeltildi: {old} → {tgt.name}", performed_by=actor))
    return True


# DİKKAT: "/retention/samples/{sample_id}" PUT'unun ÜSTÜNDE kalmalı —
# altına taşınırsa "bulk-variation" sample_id sanılır ve 422 döner
# (aynı tuzak bulk-location ve /production/next-lot'ta yaşandı).
@router.put("/retention/samples/bulk-variation")
def bulk_update_variation(
    data: BulkVariationBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("retention", "edit")),
    domain: str = Depends(active_domain),
):
    """Çok sayıda numunenin boyunu TEK istekte düzelt (bulk-location kalıbı)."""
    ids = [row.id for row in data.rows]
    rows = (db.query(RetentionSample)
            .filter(RetentionSample.id.in_(ids),
                    RetentionSample.domain == domain,
                    RetentionSample.is_active == True).all())        # noqa: E712
    by_id = {r.id: r for r in rows}
    actor = _actor(current_user)
    updated, skipped, errors = 0, len(ids) - len(rows), []
    try:
        for row in data.rows:
            r = by_id.get(row.id)
            if r is None:
                continue
            if r.inventory_id:
                skipped += 1
                errors.append(f"#{r.id}: üretim kaydı, boyu değiştirilemez")
                continue
            tgt, err = _variation_target(db, r, row.item_id, domain)
            if err:
                skipped += 1
                errors.append(f"#{r.id}: {err}")
                continue
            if _apply_variation(db, r, tgt, actor):
                updated += 1
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Boylar kaydedilemedi.")

    if updated:
        log_admin_event(db, request, actor=current_user, action="retention.bulk_variation",
                        target_type="retention_sample", target_id=None,
                        target_name=f"{updated} numune",
                        details={"updated": updated, "skipped": skipped})
    return {"updated": updated, "skipped": skipped, "errors": errors[:20],
            "message": f"{updated} numunenin boyu kaydedildi"
                       + (f", {skipped} atlandı" if skipped else "")}


@router.put("/retention/samples/{sample_id}/variation")
def set_variation(
    sample_id: int,
    data: VariationBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("retention", "edit")),
    domain: str = Depends(active_domain),
):
    """Tek numunenin boyunu seç/düzelt — stoğa DOKUNMAZ."""
    r = _get(db, sample_id, domain)
    if not r:
        return _err(404, "Numune kaydı bulunamadı.")
    if r.inventory_id:
        # Üretimden gelen kayıt `-S` Inventory lotuna bağlı; ürünü değiştirmek
        # o lotu sahipsiz bırakır ve çıkışta yanlış ürünün stoğu düşerdi.
        # Bu kayıtların boyu zaten `recipe.target_item_id`'den doğru geliyor.
        return _err(400, "Üretimden gelen numunenin boyu değiştirilemez.")
    tgt, err = _variation_target(db, r, data.item_id, domain)
    if err:
        return _err(400, err)
    changed = _apply_variation(db, r, tgt, _actor(current_user))
    try:
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Boy kaydedilemedi.")

    if changed:
        log_admin_event(db, request, actor=current_user, action="retention.variation",
                        target_type="retention_sample", target_id=r.id,
                        target_name=r.lot_number,
                        details={"item": r.item_name, "item_id": r.item_id})
    out = _view(r, size=_size_map(db, [r]).get(r.item_id))
    out["message"] = f"Boy güncellendi: {r.item_name}" if changed else "Boy zaten buydu."
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
    """Raf/göz, üretim/saklama tarihi, not, teyit — stoğa DOKUNMAZ, Transaction yazmaz.

    KISMİ güncelleme: yalnız GÖNDERİLEN alanlara dokunulur (`model_fields_set`).
    Gönderilmeyen alan korunur, `null`/`""` gönderilen alan temizlenir.
    """
    r = _get(db, sample_id, domain)
    if not r:
        return _err(404, "Numune kaydı bulunamadı.")
    sent = data.model_fields_set
    moved = False
    if "shelf" in sent or "slot" in sent:
        new_shelf = ((data.shelf or "").strip() or None) if "shelf" in sent else r.shelf
        new_slot = ((data.slot or "").strip() or None) if "slot" in sent else r.slot
        moved = (new_shelf != r.shelf) or (new_slot != r.slot)
        r.shelf, r.slot = new_shelf, new_slot
    if "produced_at" in sent:
        r.produced_at = (datetime.combine(data.produced_at, datetime.min.time())
                         if data.produced_at else None)
        # Üretim tarihi düzeltildi ve saklama sonu ayrıca verilmediyse yeniden hesapla
        if "retention_until" not in sent and r.produced_at:
            shelf_life, extra = _cfg_months(db)
            r.retention_until = retention_until(r.produced_at, shelf_life, extra)
    if "retention_until" in sent:
        r.retention_until = data.retention_until
    if "note" in sent:
        r.note = (data.note or "").strip() or None
    if "needs_review" in sent and data.needs_review is not None:
        r.needs_review = bool(data.needs_review)
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
    return _view(r, size=_size_map(db, [r]).get(r.item_id))


@router.post("/retention/samples/{sample_id}/checks", status_code=201)
def create_check(
    sample_id: int,
    data: CheckCreateBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("retention", "edit")),
    domain: str = Depends(active_domain),
):
    """Periyodik kontrol kaydı — GMP gözlemi.  APPEND-ONLY.

    Sonuç 'uygun değil' olsa bile stoğa/duruma DOKUNULMAZ: otomatik imha yok.
    Operatör görür, kararı kendisi verir ve gerekiyorsa ayrıca İmha basar.
    """
    r = _get(db, sample_id, domain)
    if not r:
        return _err(404, "Numune kaydı bulunamadı.")
    if r.status != "stored":
        return _err(400, f"'{status_label(r.status)}' durumundaki numune kontrol edilemez.")
    if data.result not in CHECK_RESULTS:
        return _err(400, "Geçersiz sonuç.")

    checked_on = data.checked_on or date.today()
    if checked_on > date.today():
        return _err(400, "Kontrol tarihi gelecekte olamaz.")

    seen, items = set(), []
    for it in data.items:
        key = (it.key or "").strip()
        if key not in CHECK_ITEM_KEYS:
            return _err(400, f"Bilinmeyen kontrol kalemi: {key}")
        if key in seen:
            return _err(400, f"Kalem iki kez gönderildi: {key}")
        if it.state not in CHECK_STATES:
            return _err(400, f"Geçersiz durum: {it.state}")
        seen.add(key)
        items.append({"key": key, "state": it.state,
                      "note": (it.note or "").strip() or ""})
    missing = [k for k in CHECK_ITEM_KEYS if k not in seen]
    if missing:
        return _err(400, "Tüm kalemler doldurulmalı — eksik: "
                         + ", ".join(check_item_label(k) for k in missing))

    actor = _actor(current_user)
    try:
        c = RetentionSampleCheck(
            sample_id=r.id, checked_on=checked_on,
            properties=json.dumps(items, ensure_ascii=False),   # TR karakter escape'lenmesin
            result=data.result,
            result_note=(data.result_note or "").strip() or None,
            checked_by=actor,
        )
        db.add(c)
        # Zaman çizelgesi izi — Detay ekranındaki hareket tablosu kontrolü de göstersin
        db.add(RetentionSampleMovement(
            sample_id=r.id, movement_type="kontrol", quantity=0,
            note=f"Periyodik kontrol — {check_result_label(data.result)}",
            performed_by=actor))
        db.commit()
        db.refresh(c)
    except Exception:
        db.rollback()
        return _err(500, "Kontrol kaydedilemedi.")

    log_admin_event(db, request, actor=current_user, action="retention.check",
                    target_type="retention_sample", target_id=r.id,
                    target_name=r.lot_number,
                    details={"result": data.result, "checked_on": checked_on.isoformat(),
                             "item": r.item_name})
    out = _check_view(c)
    out["message"] = f"Kontrol kaydedildi — {check_result_label(data.result)}."
    return out


@router.delete("/retention/samples/{sample_id}")
def delete_sample(
    sample_id: int,
    data: DeleteBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("retention", "destroy")),
    domain: str = Depends(active_domain),
):
    """YANLIŞ GİRİLEN kaydı gizle (soft-delete).

    İMHA DEĞİLDİR — stoğa, `Inventory`'ye ve Transaction defterine HİÇ
    dokunmaz.  Ürün fiilen yok edildiyse `/destroy` kullanılır (o stoktan düşer).

    Guard'lar:
      • `source='production'` → silinemez.  Üretimin açtığı kayıt veri giriş
        hatası olamaz; yanlışsa yolu İmha ya da QC reddidir.  Ayrıca o kaydın
        `inventory_id`'si var, silmek `-S` lotunu görünmez yapardı.
      • Çıkış/imha hareketi görmüş kayıt → silinemez.  O adetler stok defterine
        yazılmıştır; kaydı gizlemek defteri yetim bırakır.
    """
    r = _get(db, sample_id, domain)
    if not r:
        return _err(404, "Numune kaydı bulunamadı.")
    if r.source == "production":
        return _err(400, "Üretimden gelen kayıt silinemez — fiilen imha edildiyse "
                         "'İmha' işlemini kullanın.")
    blocking = [m.movement_type for m in r.movements if m.movement_type in ("cikis", "imha")]
    if blocking:
        return _err(400, "Bu kayıtta çıkış/imha hareketi var — stok defterine "
                         "yazılmış, silinemez.")

    actor = _actor(current_user)
    reason = data.reason.strip()
    try:
        r.is_active = False
        db.add(RetentionSampleMovement(
            sample_id=r.id, movement_type="duzeltme", quantity=0,
            note=f"Kayıt silindi — {reason}"[:500], performed_by=actor))
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Kayıt silinemedi.")

    log_admin_event(db, request, actor=current_user, action="retention.delete",
                    target_type="retention_sample", target_id=r.id,
                    target_name=r.lot_number,
                    details={"item": r.item_name, "reason": reason, "brand": r.brand})
    return {"message": "Kayıt silindi — stok değişmedi."}


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
    # Ana ürün SOYUTTUR — fiziksel numunesi olamaz.  03.08.2026 sayımından
    # gelen 21 kayıt tam olarak bu yüzden boyu belirsiz kaldı (el yazısı
    # listede boy yazmıyordu); tekrarını burada kesiyoruz.
    if db.query(Item.id).filter(Item.parent_id == item.id).first() is not None:
        return _err(400, f"«{item.name}» bir ana üründür — numunenin boyunu "
                         f"seçin (ör. 200 ml / 500 ml).")
    lot = data.lot_number.strip().upper()
    # Çift kayıt guard'ı — elle 126 kayıt girilen bir dolapta aynı numuneyi
    # iki kez girmek gerçek bir hata; sessizce ikinci satır açmak yanıltır.
    dup = (db.query(RetentionSample)
           .filter(RetentionSample.item_id == item.id,
                   RetentionSample.lot_number == lot,
                   RetentionSample.status == "stored",
                   RetentionSample.is_active == True).first())      # noqa: E712
    if dup:
        return _err(400, f"Bu ürün için {lot} lotu dolapta zaten kayıtlı "
                         f"(#{dup.id}, {dup.quantity:g} adet).")
    shelf_life, extra = _cfg_months(db)
    produced = (datetime.combine(data.produced_at, datetime.min.time())
                if data.produced_at else datetime.utcnow())
    r = RetentionSample(
        item_id=item.id, item_name=item.name,
        lot_number=lot,
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
    return _view(r, size=_size_map(db, [r]).get(r.item_id))
