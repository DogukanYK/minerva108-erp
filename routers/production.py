# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Production router — manufacturing workflows + Quality Control (QA).
"""
import io
import logging
import re

from fastapi import APIRouter, Depends, BackgroundTasks, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Dict, List, Optional

from database import (
    to_tr, AppSetting,
    get_db, Item, Recipe, ProductionHistory, ProductionConsumption, Inventory, Transaction,
    RetentionSample, RetentionSampleMovement,
)
from core import lots
from core.audit import log_admin_event
from core import production_plan
from core.production_cancel import CancelError, apply_cancel, cancelled_view
from core.production_cancel import preview as cancel_preview
from core.brands import cabinet_location, cabinet_of
from core.permissions import require_internal_user, require_permission
from core.notifications import notify_low_stock
from core.domain import active_domain
from core.retention import (CFG_EXTRA, CFG_SHELF_LIFE, DEFAULT_EXTRA_MONTHS,
                            DEFAULT_SHELF_LIFE_MONTHS, retention_until)

logger = logging.getLogger("minerva108.production")


def retention_cfg_months(db: Session) -> tuple:
    """(raf ömrü, ek süre) — AppSetting'ten; bozuk/eksikse varsayılan.

    routers/retention.py'deki `_cfg_months` ile aynı okumayı yapar; ikisi de
    core/retention.py'deki sabitleri kullanır (tek kaynak).
    """
    def _read(key, default):
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        try:
            return int(float(row.value)) if row and row.value not in (None, "") else default
        except (TypeError, ValueError):
            return default
    return _read(CFG_SHELF_LIFE, DEFAULT_SHELF_LIFE_MONTHS), _read(CFG_EXTRA, DEFAULT_EXTRA_MONTHS)

router = APIRouter(prefix="/api", tags=["production"])


# ─── Schemas ────────────────────────────────────────────────────────────────

class SourceIn(BaseModel):
    """Reçete satırının bir kaynak kartı — core/production_plan doğrular
    (kart satırın seçeneği mi, miktar sonlu > 0 mı, toplam = brüt mü, lot o
    karta mı ait).  Burada yalnız tip; NaN/sonsuz da planlayıcıda 400 olur."""
    item_id: int
    quantity: float
    inventory_id: Optional[int] = None


class ProductionCreateRequest(BaseModel):
    recipe_id: int
    produced_quantity: float = Field(..., gt=0, le=1_000_000)
    # Etiket dili — 'TR' / 'EN'.  Reçetedeki dile özel etiketlerden bu dile
    # ait olan stoktan düşülür, diğeri atlanır.  Varsayılan TR.
    label_language: Optional[str] = Field("TR", max_length=8)
    # Şahit numune adedi — üretilen X adetten kaçı şahit numune dolabına
    # ayrılacak.  Varsayılan 2.  Kalan X-witness adet showroom'a gider.
    witness_quantity: Optional[float] = Field(0, ge=0)
    # Lot numarası — boş bırakılırsa sunucu üretir (MNR006).  Dolu gelirse
    # normalize edilir; AYNI ürün için kullanılmışsa 400 + öneri döner.
    lot_number: Optional[str] = Field(None, max_length=100)
    # Faz 2 — her hammadde için hangi lot/tedarikçiden tüketileceği seçimi:
    # {item_id: inventory_id}.  Seçilmeyen kalemler FIFO (en eski APPROVED lot)
    # ile düşer.  Seçilen lot yetersizse üretim NET HATA ile durur (sessizce
    # başka lottan düşmez).  Ambalaj/etiket bu seçimden muaftır (toplam stok).
    ingredient_lot_choices: Optional[dict] = None
    # P2 (08.10.2026) — "Hangi tedarikçiden?": {reçete kartı id: [{item_id,
    # quantity, inventory_id?}]}.  Reçete kartının "aynı malzeme" grubundaki
    # başka kartta stok varsa (needs_choice) ZORUNLU; yoksa 400
    # source_choice_required.  Bölmeye izin verir (iki karttan).  Anahtar
    # satır id'si DEĞİL kart id'si — reçete düzenlenince satırlar yeniden
    # yaratılıyor.  Ayrıntı: core/production_plan.py.
    ingredient_sources: Optional[Dict[str, List[SourceIn]]] = None


class ProductionCancelRequest(BaseModel):
    # Sebep zorunlu — denetim izi (PH.cancel_reason + her telafi kaydının notu).
    reason: str = Field(..., min_length=5, max_length=500)
    # Önizlemenin parmak izi — arada stok değiştiyse 409 preview_stale.
    fingerprint: str = Field(..., min_length=1, max_length=64)
    # Lot no'yu serbest bırak (aynı üründe tekrar kullanılabilsin).  Varsayılan
    # hayır — GMP: iptal edilen lot no yeniden verilmez.
    release_lot: bool = False


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
def list_production_history(db: Session = Depends(get_db), domain: str = Depends(active_domain),
                            _: dict = Depends(require_permission("production", "view"))):
    rows = (db.query(ProductionHistory)
            .filter(ProductionHistory.domain == domain)
            .order_by(ProductionHistory.id.desc()).limit(100).all())
    return [
        {
            "id": r.id,
            "recipe_name": r.recipe_name,
            "target_item_name": r.target_item_name,
            "produced_quantity": r.produced_quantity,
            "produced_at": to_tr(r.produced_at).strftime("%d.%m.%Y %H:%M") if r.produced_at else "",
            "lot_number": r.lot_number or "",
            # İptal edilmiş üretim listede KALIR (geçmiş silinmez) — UI üstü
            # çizili gösterir; iptal değilse None.
            "cancelled": cancelled_view(r),
        }
        for r in rows
    ]


def _lock_lot_for_qc(db: Session, inventory_id: int):
    """QC kararı için (lot, kart) — FOR UPDATE, sıra kart → lot (üretim
    iptaliyle aynı: core/production_cancel.lock_rows; ters sıra kilitlenme
    yaratırdı).  Durum KİLİTTEN SONRA okunur: eşzamanlı iptal lotu
    CANCELLED/0 yaptıysa bayat kopya üzerinden ikinci düşüm yapılmaz, istek
    "QC listesinde değil" ile döner.  Lot yoksa (None, None)."""
    peek = db.query(Inventory.item_id).filter(Inventory.id == inventory_id).first()
    if peek is None:
        return None, None
    item = (db.query(Item).filter(Item.id == peek[0])
            .with_for_update().populate_existing().first())
    inv = (db.query(Inventory).filter(Inventory.id == inventory_id)
           .with_for_update().populate_existing().first())
    if inv is not None and (item is None or inv.item_id != item.id):
        # Okuma ile kilit arasında lot başka karta taşındı — o kartı da kilitle.
        item = (db.query(Item).filter(Item.id == inv.item_id)
                .with_for_update().populate_existing().first())
    return inv, item


def _mark_retention_rejected(db: Session, inv: Inventory, actor: str, reason: str) -> None:
    """QC'de reddedilen şahit lotunun dolap kaydını kapat.

    Stok matematiği ÇAĞIRANA aittir (Adjustment orada yazılıyor) — burada
    yalnız dolap görünürlüğü düzeltilir, ikinci kez stok DÜŞÜLMEZ.  Yoksa
    reddedilmiş bir numune dolapta duruyormuş gibi görünürdü.
    """
    row = (db.query(RetentionSample)
           .filter(RetentionSample.inventory_id == inv.id,
                   RetentionSample.is_active == True).first())      # noqa: E712
    if not row:
        return
    qty = row.quantity or 0.0
    row.qc_status = "rejected"
    row.quantity = 0.0
    row.status = "destroyed"
    db.add(RetentionSampleMovement(
        sample_id=row.id, movement_type="duzeltme", quantity=qty,
        note=f"QC reddi — {reason}"[:500], performed_by=actor))


# ─── Lot numarası önerisi ───────────────────────────────────────────────────
# DİKKAT: bu route "/production/{prod_id}"nin ÜSTÜNDE kalmalı — altına
# taşınırsa "next-lot" prod_id sanılıp int'e çevrilmeye çalışılır → 422.

@router.get("/production/next-lot")
def suggest_next_lot(
    recipe_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("production", "create")),
    domain: str = Depends(active_domain),
):
    """Bu reçetenin hedef ürünü için bir sonraki lot numarası önerisi.

    Sayaç ürün bazlıdır: 'bu üründen en son MNR005'i ürettiniz, bu MNR006
    olmalı'.  Kullanıcı öneriyi düzenleyebilir (üretim POST'unda gönderilir).
    """
    recipe = (db.query(Recipe)
              .filter(Recipe.id == recipe_id, Recipe.domain == domain).first())
    if not recipe:
        return JSONResponse(status_code=404, content={"detail": "Reçete bulunamadı."})
    if not recipe.target_item_id:
        return {"lot_number": "", "message": "Bu reçetenin hedef ürünü yok — "
                                             "lot numarası üretimde otomatik atanır."}
    item = db.query(Item).filter(Item.id == recipe.target_item_id, Item.domain == domain).first()
    if not item:
        return JSONResponse(status_code=404, content={"detail": "Hedef ürün bulunamadı."})
    out = lots.suggest(db, item)
    out["cabinet"] = cabinet_of(item.name)
    return out


# ─── Üretim önizlemesi (P2 — "Hangi tedarikçiden?") ─────────────────────────
# DİKKAT: "/production/{prod_id}"nin ÜSTÜNDE kalsın (next-lot tuzağı).
# Hesap TEK KAYNAK: core/production_plan.plan() — başlatma da aynısını çağırır.

@router.post("/production/preview")
def production_preview(
    data: ProductionCreateRequest,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("production", "create")),
    domain: str = Depends(active_domain),
):
    """Üretim öncesi canlı önizleme — satırlar (net/brüt/fire), "aynı malzeme"
    grubundaki seçenek kartlar, önerilen bölme, seçilenin lot planı, kart
    başına toplam stok kapısı, engeller.  Hiçbir şey yazmaz, kilit almaz.
    Gövde başlatmayla aynı (`ingredient_sources` / `ingredient_lot_choices`);
    `can_start` False iken başlatma aynı gerekçeyle 400 döner."""
    recipe = (db.query(Recipe)
              .filter(Recipe.id == data.recipe_id, Recipe.domain == domain).first())
    if not recipe:
        return JSONResponse(status_code=404, content={"detail": "Reçete bulunamadı."})
    try:
        pl = production_plan.plan(db, recipe, data.produced_quantity, data.label_language,
                                  domain=domain, sources=data.ingredient_sources,
                                  lot_choices=data.ingredient_lot_choices, lock=False)
        return production_plan.preview_payload(pl)
    except production_plan.PlanError as e:
        return JSONResponse(status_code=e.status, content=e.payload())
    finally:
        db.rollback()                 # salt okur — açık transaction kalmasın


# ─── Üretim föyü (production sheet) ─────────────────────────────────────────

# İsim/varyasyondan ml değeri çeken yardımcı core/consumption.parse_ml'e taşındı
# (Satın Alma Planı da ürün boyunu aynı kuralla okuyor).  Bu satır RE-EXPORT
# SHIM'idir — eski ad buradan çağrılıyor.
from core.consumption import parse_ml as _parse_ml  # noqa: E402
from core.consumption import _kind as consumption_kind  # noqa: E402


def _sheet_rows_from_recipe(recipe: Recipe, prod: ProductionHistory, db: Session):
    """Föy satırları reçeteden (dökümü olmayan eski üretim) — net / brüt
    (fireli) / % bileşim; % hammadde net'i üzerinden (Excel föyündeki
    "% MİKTAR" mantığı).  Dönüş (satırlar, fire %)."""
    multiplier   = prod.produced_quantity / (recipe.output_quantity or 1.0)
    waste        = recipe.waste_percentage or 0.0
    waste_factor = 1.0 + waste / 100.0

    hammadde_qty_total = 0.0
    ings = []
    for ing in recipe.ingredients:
        item = db.query(Item).filter(Item.id == ing.item_id).first()
        if not item:
            continue
        is_amb = (item.category == "Ambalaj")
        if not is_amb:
            hammadde_qty_total += ing.quantity
        ings.append((ing, item, is_amb))

    rows = []
    for ing, item, is_amb in ings:
        factor = 1.0 if is_amb else waste_factor
        net    = round(ing.quantity * multiplier, 6)
        gross  = round(net * factor, 6)
        pct    = (round(ing.quantity / hammadde_qty_total * 100, 4)
                  if (not is_amb and hammadde_qty_total) else None)
        rows.append({
            "phase":          ing.phase or "",
            "item_name":      item.name,
            "recipe_item_id": item.id,
            "unit":           ing.unit or item.unit or "",
            "percent":        pct,
            "net":            net,
            "gross":          gross,
            "is_ambalaj":     is_amb,
            "substituted":    False,
            "sources":        [],
        })
    return rows, waste


def _sheet_rows_from_snapshot(snap, recipe: Optional[Recipe], db: Session):
    """Föy satırları tüketim dökümünden (production_consumptions) — üretimde
    GERÇEKTEN düşülen; reçete sonradan düzenlense/silinse de değişmez.

    Reçete kartı + faza (`recipe_item_id`, `phase`) göre gruplanır — aynı
    kart reçetede iki fazdaysa föyde eskisi gibi iki satır: brüt = Σ miktar,
    net = brüt / fire çarpanı, % bu netlerden.  Alt satırlar `sources`: hangi
    kart, tedarikçi, lot, miktar (P2 — bölünmüş üretimde iki kart).  Etiket
    satırı dil kardeşinin (fiilen düşülen) adıyla görünür; hammadde/ambalaj
    satırı reçetedeki kartın adıyla, kaynak kart farklıysa `substituted`.
    Dönüş (satırlar, fire %)."""
    groups: Dict[tuple, list] = {}
    for pc in snap:
        groups.setdefault((pc.recipe_item_id or pc.item_id, pc.phase or ""), []).append(pc)
    ids = {k[0] for k in groups} | {pc.item_id for pc in snap}
    names = {i: n for i, n in db.query(Item.id, Item.name).filter(Item.id.in_(ids)).all()}

    raw_net_total = 0.0
    built = []
    for (key, _phase), pcs in groups.items():
        kind = pcs[0].kind
        is_amb = kind != "raw"
        factor = float(pcs[0].factor or 1.0) or 1.0
        gross = round(sum(float(pc.quantity or 0.0) for pc in pcs), 6)
        net = round(gross / factor, 6)
        if not is_amb:
            raw_net_total += net
        distinct = list(dict.fromkeys(pc.item_id for pc in pcs))
        shown = distinct[0] if kind == "label" and len(distinct) == 1 else key
        built.append((key, pcs, kind, is_amb, factor, gross, net, shown))

    rows = []
    waste = None
    for key, pcs, kind, is_amb, factor, gross, net, shown in built:
        if kind == "raw" and waste is None:
            waste = round((factor - 1.0) * 100.0, 6)
        rows.append({
            "phase":          pcs[0].phase or "",
            "item_name":      names.get(shown) or "—",
            "recipe_item_id": key,
            "unit":           pcs[0].unit or "",
            "percent":        (round(net / raw_net_total * 100, 4)
                               if (not is_amb and raw_net_total) else None),
            "net":            net,
            "gross":          gross,
            "is_ambalaj":     is_amb,
            "substituted":    kind != "label" and any(pc.item_id != key for pc in pcs),
            "sources": [{
                "item_id":        pc.item_id,
                "item_name":      names.get(pc.item_id) or "—",
                "is_recipe_card": pc.item_id == key,
                "supplier_name":  pc.supplier_name or "",
                "lot_number":     pc.lot_number or "",
                "quantity":       round(float(pc.quantity or 0.0), 6),
                "unit":           pc.unit or "",
            } for pc in pcs],
        })
    if waste is None:
        waste = float(recipe.waste_percentage or 0.0) if recipe else 0.0
    return rows, waste


def _build_production_sheet(prod: ProductionHistory, db: Session) -> Optional[dict]:
    """
    Üretim föyü — net / brüt(fireli) / fire.

    Tüketim dökümü (P0 sonrası her üretim) varsa ONDAN kurulur: reçete
    sonradan düzenlense ya da silinse de üretimde gerçekten düşüleni ve hangi
    karttan/tedarikçiden/lottan düşüldüğünü (`sources`) gösterir.  Döküm
    yoksa (eski üretim) reçeteden yeniden hesaplanır; reçete de silinmişse
    None döner.
    """
    recipe = db.query(Recipe).filter(Recipe.id == prod.recipe_id).first() if prod.recipe_id else None
    snap = (db.query(ProductionConsumption)
            .filter(ProductionConsumption.production_id == prod.id,
                    ProductionConsumption.kind != "output")
            .order_by(ProductionConsumption.id.asc()).all())
    # Defterden kurulmuş döküm (source='ledger', eski üretimin iptalinde
    # kalıcılaşır) faz ve reçete kartı taşımaz — reçete duruyorsa föy ondan.
    live = [pc for pc in snap if (pc.source or "live") == "live"]
    if live:
        rows, waste = _sheet_rows_from_snapshot(live, recipe, db)
    elif recipe is not None:
        rows, waste = _sheet_rows_from_recipe(recipe, prod, db)
    elif snap:
        rows, waste = _sheet_rows_from_snapshot(snap, recipe, db)
    else:
        return None

    net_total = sum(r["net"] for r in rows)
    gross_total = sum(r["gross"] for r in rows)
    target_id = prod.target_item_id or (recipe.target_item_id if recipe else None)
    target = db.query(Item).filter(Item.id == target_id).first() if target_id else None
    bottle_ml = _parse_ml(
        getattr(target, "variation_name", None) if target else None,
        target.name if target else None,
        prod.target_item_name,
    )

    return {
        "id":               prod.id,
        "recipe_id":         prod.recipe_id,
        "recipe_name":       prod.recipe_name or (recipe.name if recipe else ""),
        "target_item_name":  prod.target_item_name or (target.name if target else ""),
        "produced_quantity": prod.produced_quantity,
        "produced_at":       to_tr(prod.produced_at).strftime("%d.%m.%Y %H:%M") if prod.produced_at else "",
        "produced_at_date":  to_tr(prod.produced_at).strftime("%d.%m.%Y") if prod.produced_at else "",
        "produced_by":       prod.produced_by or "",
        "lot_number":        prod.lot_number or "",
        "cancelled":         cancelled_view(prod),
        "bottle_ml":         bottle_ml,
        "waste_percentage":  round(waste, 2),
        "production_notes":  (recipe.production_notes if recipe else None) or "",
        # snapshot: üretimde düşülenden | recipe: reçeteden yeniden hesap
        "source":            "snapshot" if (live or recipe is None) else "recipe",
        "ingredients":       rows,
        "totals": {
            "net":   round(net_total, 4),
            "gross": round(gross_total, 4),
            "fire":  round(gross_total - net_total, 4),
        },
    }


# ─── Üretim iptali ──────────────────────────────────────────────────────────
# DİKKAT: "/production/{prod_id}"nin ÜSTÜNDE kalsın (next-lot tuzağı).
# Motor: core/production_cancel.py — hiçbir Transaction silinmez, telafi kaydı.

@router.get("/production/{prod_id}/cancel-preview")
def production_cancel_preview(
    prod_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("production", "cancel")),
    domain: str = Depends(active_domain),
):
    """İptal önizlemesi — stoğa iade edilecekler, stoktan düşülecekler,
    şahit numune, engeller, uyarılar ve POST'ta geri gönderilecek
    `fingerprint`.  Hiçbir şey yazmaz."""
    prod = (db.query(ProductionHistory)
            .filter(ProductionHistory.id == prod_id, ProductionHistory.domain == domain).first())
    if not prod:
        return JSONResponse(status_code=404, content={"detail": "Üretim kaydı bulunamadı."})
    return cancel_preview(db, prod)


@router.post("/production/{prod_id}/cancel")
def production_cancel(
    prod_id: int,
    data: ProductionCancelRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("production", "cancel")),
    domain: str = Depends(active_domain),
):
    """Üretimi iptal et — tüketilen her kalem stoğa (ve kaynak lotuna) iade,
    bitmiş ürün stoktan düşülür, şahit numune dolaptan kalkar.  İdempotent:
    zaten iptal edilmişse 200 ``already_cancelled`` (hiçbir şey yazılmaz)."""
    reason = (data.reason or "").strip()
    if len(reason) < 5:
        return JSONResponse(status_code=422, content={"detail": "İptal sebebi en az 5 karakter olmalı."})
    prod = (db.query(ProductionHistory)
            .filter(ProductionHistory.id == prod_id, ProductionHistory.domain == domain)
            .with_for_update().first())
    if not prod:
        return JSONResponse(status_code=404, content={"detail": "Üretim kaydı bulunamadı."})
    if prod.cancelled_at:
        out = {"already_cancelled": True, "production_id": prod.id,
               "cancelled": cancelled_view(prod)}
        db.rollback()
        return out

    actor = current_user.get("full_name") or current_user.get("username") or "—"
    try:
        summary = apply_cancel(db, prod, actor=actor, reason=reason,
                               release_lot=data.release_lot,
                               expected_fingerprint=data.fingerprint)
        db.commit()
    except CancelError as e:
        db.rollback()
        return JSONResponse(status_code=e.status, content=e.payload())
    except Exception:
        db.rollback()
        logger.exception("Üretim iptali başarısız (#%s)", prod_id)
        return JSONResponse(status_code=500, content={
            "detail": "İptal sırasında hata oluştu, stoklar değiştirilmedi."})

    # log_admin_event kendi commit'ini yapar — ana commit'ten SONRA.
    log_admin_event(db, request, current_user, action="production.cancel",
                    target_type="production", target_id=summary["production_id"],
                    target_name=f"{summary['lot'] or '—'} · {summary['recipe'] or '—'}"[:150],
                    details={"lot": summary["lot"], "recipe": summary["recipe"],
                             "qty": summary["qty"], "source": summary["source"],
                             "tx_ids": summary["tx_ids"],
                             "cancel_tx_ids": summary["cancel_tx_ids"],
                             "reason": reason, "release_lot": summary["release_lot"]})
    prod = db.query(ProductionHistory).filter(ProductionHistory.id == prod_id).first()
    return {
        "message": (f"Üretim iptal edildi — {summary['restored']} kalem stoğa iade edildi"
                    + (f", {summary['qty']:g} adet bitmiş ürün stoktan düşüldü."
                       if summary["lot"] and prod and prod.target_item_id else ".")),
        "already_cancelled": False,
        **{k: v for k, v in summary.items() if k != "recipe"},
        "cancelled": cancelled_view(prod) if prod else None,
    }


@router.get("/production/{prod_id}")
def production_detail(
    prod_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_internal_user(("production", "view"))),
    domain: str = Depends(active_domain),
):
    """Tek üretim kaydının föyü — canlı önizleme tarzı brüt/fireli döküm."""
    prod = db.query(ProductionHistory).filter(ProductionHistory.id == prod_id,
                                              ProductionHistory.domain == domain).first()
    if not prod:
        return JSONResponse(status_code=404, content={"detail": "Üretim kaydı bulunamadı."})
    sheet = _build_production_sheet(prod, db)
    if sheet is None:
        return JSONResponse(status_code=409, content={
            "detail": "Bu üretimin reçetesi silinmiş — föy yeniden hesaplanamıyor."
        })
    return sheet


@router.get("/production/{prod_id}/export")
def production_export(
    prod_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_internal_user(("production", "view"))),
    domain: str = Depends(active_domain),
):
    """Üretim föyünü .xlsx olarak indir — lab Excel formatının birebir aynısı."""
    prod = db.query(ProductionHistory).filter(ProductionHistory.id == prod_id,
                                              ProductionHistory.domain == domain).first()
    if not prod:
        return JSONResponse(status_code=404, content={"detail": "Üretim kaydı bulunamadı."})
    sheet = _build_production_sheet(prod, db)
    if sheet is None:
        return JSONResponse(status_code=409, content={"detail": "Reçete silinmiş — föy üretilemiyor."})

    import openpyxl
    from openpyxl.styles import Font, Alignment, Border, Side, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Üretim Föyü"

    bold      = Font(bold=True)
    big       = Font(bold=True, size=12)
    thin      = Side(style="thin", color="999999")
    box       = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_fill  = PatternFill("solid", fgColor="232E6E")
    hdr_font  = Font(bold=True, color="FFFFFF")
    wrap      = Alignment(wrap_text=True, vertical="top")

    t = sheet["totals"]
    waste = sheet["waste_percentage"]

    # ── Başlık bloğu ────────────────────────────────────────────────────────
    ws["A1"] = "YAPILMASI GEREKEN ADET:";          ws["B1"] = sheet["produced_quantity"]
    ws["C1"] = "ŞİŞE ML :";                         ws["D1"] = sheet["bottle_ml"] or ""
    ws["A2"] = "YAPILMASI GEREKEN MİKTAR (GR) :";   ws["B2"] = t["net"]
    ws["A3"] = f"% {waste:g} FİRE (GR) :";          ws["B3"] = t["fire"]
    ws["A4"] = "YAPILMASI GEREKEN TOPLAM MİKTAR:";  ws["B4"] = t["gross"]
    ws["D4"] = f"TARİH: {sheet['produced_at_date']}"
    for r in range(1, 5):
        ws[f"A{r}"].font = bold
        ws[f"C{r}"].font = bold
    ws["A5"] = sheet["target_item_name"]
    ws["A5"].font = big

    # ── Bileşen tablosu başlığı ─────────────────────────────────────────────
    hdr_row = 6
    headers = ["FAZ", "HAMMADDE İSİM", "% MİKTAR", f"MİKTAR (GR) — {t['gross']:g}"]
    for ci, h in enumerate(headers, start=1):
        c = ws.cell(row=hdr_row, column=ci, value=h)
        c.font = hdr_font; c.fill = hdr_fill; c.border = box
        c.alignment = Alignment(horizontal="center")

    # ── Satırlar ────────────────────────────────────────────────────────────
    row = hdr_row + 1
    for ing in sheet["ingredients"]:
        ws.cell(row=row, column=1, value=ing["phase"]).border = box
        ws.cell(row=row, column=2, value=ing["item_name"]).border = box
        pct_cell = ws.cell(row=row, column=3,
                           value=(round(ing["percent"], 3) if ing["percent"] is not None else ""))
        pct_cell.border = box
        amt_cell = ws.cell(row=row, column=4, value=round(ing["gross"], 4))
        amt_cell.border = box
        row += 1

    # ── TOPLAM satırı ───────────────────────────────────────────────────────
    ws.cell(row=row, column=2, value="TOPLAM").font = bold
    ws.cell(row=row, column=3, value=100).font = bold
    ws.cell(row=row, column=4, value=round(t["gross"], 4)).font = bold
    for ci in range(1, 5):
        ws.cell(row=row, column=ci).border = box
    row += 2

    # ── YAPILIŞI ────────────────────────────────────────────────────────────
    ws.cell(row=row, column=1, value="YAPILIŞI").font = bold
    row += 1
    notes_cell = ws.cell(row=row, column=1, value=sheet["production_notes"] or "—")
    notes_cell.alignment = wrap
    ws.merge_cells(start_row=row, start_column=1, end_row=row + 6, end_column=4)

    # ── Kolon genişlikleri ──────────────────────────────────────────────────
    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 38
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 22

    # ── "Tüketim Kaynakları" — hangi karttan / tedarikçiden / lottan ────────
    # Ana sayfa lab formatında KALIR; kaynak dökümü (P2: bölünmüş üretimde
    # iki kart) ayrı sayfada.  Yalnız döküm varsa (eski üretimde yok).
    src_rows = [(ing, s) for ing in sheet["ingredients"] for s in ing.get("sources") or []]
    if src_rows:
        ws2 = wb.create_sheet("Tüketim Kaynakları")
        heads = ["FAZ", "REÇETEDEKİ KALEM", "KULLANILAN KART", "TEDARİKÇİ", "KAYNAK LOT",
                 "MİKTAR", "BİRİM"]
        for ci, h in enumerate(heads, start=1):
            c = ws2.cell(row=1, column=ci, value=h)
            c.font = hdr_font; c.fill = hdr_fill; c.border = box
            c.alignment = Alignment(horizontal="center")
        for ri, (ing, s) in enumerate(src_rows, start=2):
            vals = [ing["phase"], ing["item_name"],
                    s["item_name"] + ("" if s["is_recipe_card"] else " (kaynak kart)"),
                    s["supplier_name"] or "—", s["lot_number"] or "—",
                    round(s["quantity"], 4), s["unit"]]
            for ci, v in enumerate(vals, start=1):
                c = ws2.cell(row=ri, column=ci, value=v)
                c.border = box
                if isinstance(v, str) and c.data_type == "f":
                    # "=" ile başlayan kart/tedarikçi adı formüle dönmesin
                    # (core/purchase_plan_xlsx._put_text ile aynı kalkan).
                    c.data_type = "s"
                    c.quotePrefix = True
        for col, w in zip("ABCDEFG", (8, 36, 40, 28, 16, 14, 8)):
            ws2.column_dimensions[col].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    safe_name = re.sub(r'[^A-Za-z0-9_-]+', '_', sheet["target_item_name"] or "uretim").strip("_")
    filename  = f"uretim_foyu_{prod.id}_{safe_name}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/production", status_code=201)
def start_production(
    data: ProductionCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("production", "create")),
    domain: str = Depends(active_domain),
):
    # Aktif panel — başka panelin reçetesi bu panelden üretilemez (eskiden
    # Recipe.id ile bulunuyordu, panel filtresi yoktu).
    recipe = (db.query(Recipe)
              .filter(Recipe.id == data.recipe_id, Recipe.domain == domain).first())
    if not recipe:
        return JSONResponse(status_code=404, content={"detail": "Reçete bulunamadı."})
    if data.produced_quantity <= 0:
        return JSONResponse(status_code=400, content={"detail": "Üretim miktarı sıfırdan büyük olmalıdır."})

    actor = current_user.get("full_name") or current_user.get("username") or "—"

    # ── Seçilen etiket dili ─────────────────────────────────────────────────
    sel_lang = "EN" if (data.label_language or "").strip().upper().startswith("EN") else "TR"
    sel_lang_label = "İngilizce" if sel_lang == "EN" else "Türkçe"

    try:
        # ── Plan: satırlar, kaynak kartlar, lot dağıtımı, kart başına stok ──
        # TEK KAYNAK core/production_plan.plan() — önizlemeyle aynı kod.
        # Brüt = net × (1 + fire%/100); ambalaj/etiket fire muaf; etiket
        # seçilen dilin kardeşine çözülür; aynı kart tek satırda birleşir.
        # lock=True: düşülecek kartlar + hedef (id sıralı) → lotları FOR UPDATE.
        # Üretim iptali de aynı kilit sırasını kullanır.  Hata varsa
        # HİÇBİR ŞEY yazılmaz.
        try:
            pl = production_plan.plan(db, recipe, data.produced_quantity, sel_lang,
                                      domain=domain, sources=data.ingredient_sources,
                                      lot_choices=data.ingredient_lot_choices, lock=True)
        except production_plan.PlanError as e:
            db.rollback()
            return JSONResponse(status_code=e.status, content={"detail": e.detail, "code": e.code})
        if pl.errors:
            first = pl.first_error()
            payload = production_plan.preview_payload(pl)
            db.rollback()
            return JSONResponse(status_code=400, content={
                "detail": first["detail"], "code": first["code"], "errors": pl.errors,
                "lines": payload["lines"], "per_card": payload["per_card"],
                "blockers": payload["blockers"],
            })
        label_warnings = list(pl.label_warnings)

        # ── Lot numarası şimdiden üret — tüm transaction notlarına stamp atılır
        #
        # Hedef satırı planlayıcıda kaynaklarla birlikte, lotlardan ÖNCE
        # kilitlendi.  Aynı transaction içinde yeniden almak yeni kilit
        # sırası yaratmaz.  populate_existing ile lot sayacı ve stok güncel
        # okunur; aynı ürünün eşzamanlı üretimleri numarayı paylaşamaz.
        import datetime as _dt
        now = _dt.datetime.utcnow()
        target_item_row = None
        if recipe.target_item_id:
            target_item_row = (db.query(Item)
                               .filter(Item.id == recipe.target_item_id, Item.domain == domain)
                               .with_for_update().populate_existing().first())
            if target_item_row is None:
                db.rollback()
                return JSONResponse(status_code=404, content={
                    "detail": "Hedef ürün bu panelde bulunamadı.", "code": "item_not_found"})

        if target_item_row is not None:
            requested_lot = lots.normalize_lot(data.lot_number)
            if requested_lot:
                if lots.is_taken(db, target_item_row.id, requested_lot):
                    db.rollback()
                    sug = lots.suggest(db, target_item_row)
                    return JSONResponse(status_code=400, content={
                        "detail": (f"{requested_lot} bu ürün için zaten kullanılmış — "
                                   f"önerilen: {sug['lot_number']}"),
                        "suggested_lot": sug["lot_number"],
                    })
                produced_lot = requested_lot
                # Elle ileri bir numara verildiyse sayaç oradan devam etsin.
                seq = lots.parse_sequence(produced_lot, lots.lot_prefix(target_item_row))
                if seq and seq > (target_item_row.lot_seq or 0):
                    target_item_row.lot_seq = seq
            else:
                seq = lots.next_sequence(db, target_item_row)
                produced_lot = lots.format_lot(lots.lot_prefix(target_item_row), seq)
                target_item_row.lot_seq = seq
        else:
            # Hedef ürünü olmayan reçete (yarı mamul denemesi) — eski biçim.
            produced_lot = f"PRD-{now.strftime('%Y%m%d-%H%M%S')}"

        # ── Üretim kaydı — lot kesinleşir kesinleşmez (tüketim satırları
        #    prod.id'ye bağlanacak).  Eskiden en sonda ekleniyordu.
        rec_domain = recipe.domain or "cosmetics"
        prod = ProductionHistory(
            recipe_id=recipe.id,
            recipe_name=recipe.name,
            target_item_id=recipe.target_item_id,
            target_item_name=recipe.target_item.name if recipe.target_item else recipe.description,
            produced_quantity=data.produced_quantity,
            produced_at=now,
            produced_by=actor,                       # Audit
            lot_number=produced_lot if recipe.target_item_id else None,
            witness_quantity=0.0,                    # aşağıda, kırpılmış değerle
            domain=rec_domain,                       # Faz 3 — reçetenin paneli
        )
        db.add(prod)
        db.flush()

        def _consumption(kind, tx, *, item_id, recipe_item_id=None, lot=None,
                         lot_number=None, quantity, factor=1.0, unit=None, phase=None):
            """Defter satırının dökümü (core/production_cancel bunu okur).
            İlişkiyle bağlanır → satır başına flush gerekmez."""
            sup = lot.supplier if (lot is not None and lot.supplier_id) else None
            if sup is None and kind != "output":
                card = pl.cards.get(item_id)
                sup = card.supplier if card is not None and card.supplier_id else None
            pc = ProductionConsumption(
                production_id=prod.id, kind=kind, recipe_item_id=recipe_item_id,
                item_id=item_id, lot_number=lot_number,
                supplier_id=sup.id if sup else None, supplier_name=sup.name if sup else None,
                quantity=quantity, factor=factor, unit=unit, phase=phase,
                source="live", domain=rec_domain)
            pc.transaction = tx
            if lot is not None:
                pc.inventory = lot
            db.add(pc)

        # ── Stok düş + Output transaction kaydet ───────────────────────────
        # Kart stoğu seçim (pick) başına TAM pay kadar düşer (current_stock
        # kaynak-of-truth).  Output'lar `pl.segments()` sırasıyla: reçete
        # satırı sırası, her tahsis (lot) ayrı Output — Transaction.lot_number
        # = KAYNAK lot.  Aynı kart reçetede iki satırdaysa (ör. su A ve C
        # fazında) her reçete satırı eskisi gibi kendi Output'unu/fazını alır.
        # Transaction.item_id = FİİLEN düşülen kart (etikette dil kardeşi,
        # kaynak seçiminde gruptaki diğer kart); döküm satırı reçetedeki kartı
        # `recipe_item_id`'de ayrıca taşır.
        # NOT SÖZLEŞMESİ: not "Üretim tüketimi — Reçete: {ad} | " ile başlar
        # ve "Üretim Lot: {lot}" ile BİTER — core/production_cancel eski
        # üretimi bu imzayla kurar, trace_lot damgaya düşer.  Kaynak kart
        # reçetedekinden farklıysa " | Kaynak kart: … (reçetede: …)" araya
        # ("Tedarikçi:"den önce) girer.
        for ln in pl.lines:
            for pick in ln.chosen:
                item = pl.cards[pick.item_id]
                item.current_stock = round(item.current_stock - pick.quantity, 6)
        waste_pct = recipe.waste_percentage or 0
        for seg in pl.segments():
            ln, item = seg.line, pl.cards[seg.pick.item_id]
            fire_note = (f" | %{waste_pct} fire dahil, brüt girdi"
                         if not ln.is_ambalaj and waste_pct > 0 else "")
            note = (f"Üretim tüketimi — Reçete: {recipe.name}{fire_note} | "
                    f"Dil: {sel_lang_label}")
            if ln.kind != "label" and item.id != ln.recipe_item_id:
                note += (f" | Kaynak kart: {item.name} "
                         f"(reçetede: {pl.names.get(ln.recipe_item_id, '—')})")
            lot = seg.lot
            if lot is not None:
                # Hammadde — seçilen/FIFO lot
                lot.quantity = round((lot.quantity or 0) - seg.quantity, 6)
                sup = lot.supplier.name if lot.supplier else "—"
                smp = " (numune)" if lot.is_sample else ""
                note += (f" | Tedarikçi: {sup}{smp} | Kaynak Lot: {lot.lot_number} | "
                         f"Üretim Lot: {produced_lot}")
            elif seg.uncovered:
                # Lot toplamı payı karşılamadı (eksik lot verisi) — artık toplam
                # stoktan; audit bütünlüğü için yine Output (toplam = pay).
                note += f" | (lot kaydı dışı, toplam stoktan) | Üretim Lot: {produced_lot}"
            else:
                # Ambalaj/etiket ya da hiç lotu olmayan hammadde — toplam stoktan
                note += f" | Üretim Lot: {produced_lot}"
            tx = Transaction(
                item_id=item.id, transaction_type="Output", quantity=seg.quantity,
                lot_number=lot.lot_number if lot is not None else None,
                notes=note, performed_by=actor,
            )
            db.add(tx)
            _consumption(consumption_kind(item), tx, item_id=item.id,
                         recipe_item_id=ln.recipe_item_id, lot=lot,
                         lot_number=lot.lot_number if lot is not None else None,
                         quantity=seg.quantity, factor=ln.factor, unit=item.unit,
                         phase=seg.phase)

        # ── Şahit numune ayrımı ─────────────────────────────────────────────
        # Üretilen X adetin Y'si "Şahit Numune Dolabı"na (marka bazlı), kalanı
        # "Showroom"a ayrılır.  Item.current_stock yine X kadar artar (hepsi
        # stoktadır, sadece konum farklı).  Witness=0 ise tek lot, eski davranış.
        target_item_obj = target_item_row      # yukarıda kilitlenmiş satır
        witness_qty = max(0.0, float(data.witness_quantity or 0))
        if witness_qty > data.produced_quantity:
            witness_qty = data.produced_quantity        # taşmayı kırp
        showroom_qty = round(data.produced_quantity - witness_qty, 6)
        prod.witness_quantity = witness_qty          # şahit numuneye ayrılan adet

        # Marka/dolap adı — TEK KAYNAK core/brands.py (eskiden burada hard-coded
        # bir harita vardı; üç ayrı marka implementasyonundan biriydi).
        target_name = target_item_obj.name if target_item_obj else ""
        brand = cabinet_of(target_name) if target_name else ""
        witness_location = cabinet_location(target_name) if target_name else "Şahit Numune Dolabı"

        # Üretilen lot APPROVED + qc_required=True olarak yaratılır:
        # → Stok hemen artar (patron şartı: üretim biter bitmez stoğa düşmeli)
        # → QC sayfası bu lotu görür ve inceler (qc_required=True flag'i ile)
        # → QC onaylarsa qc_required=False, status APPROVED kalır
        # → QC reddederse status=REJECTED + stok düşülür + Adjustment audit
        if recipe.target_item_id:
            finished_rows = []                     # [(Inventory, adet)] — output dökümü
            # 1) Showroom lot'u — kalan kısım
            if showroom_qty > 0:
                showroom_inv = Inventory(
                    item_id=recipe.target_item_id,
                    lot_number=produced_lot,
                    quantity=showroom_qty,
                    location="Showroom",
                    status="APPROVED",
                    received_by=actor,
                    qc_required=True,
                    domain=(recipe.domain or "cosmetics"),   # Faz 3 — reçetenin paneli
                )
                db.add(showroom_inv)
                finished_rows.append((showroom_inv, showroom_qty))
            # 2) Şahit numune lot'u — varsa.  Stok kaynağı burasıdır (adet
            #    current_stock içinde sayılmaya devam eder); RetentionSample
            #    bunun ÜSTÜNE dolap yönetimini ekler (raf/göz, saklama süresi,
            #    çıkış geçmişi) ve inventory_id ile bu satıra bağlanır.
            if witness_qty > 0:
                witness_inv = Inventory(
                    item_id=recipe.target_item_id,
                    lot_number=f"{produced_lot}-S",       # "-S" suffix = Şahit
                    quantity=witness_qty,
                    location=witness_location,
                    status="APPROVED",
                    received_by=actor,
                    qc_required=True,
                    domain=(recipe.domain or "cosmetics"),
                )
                db.add(witness_inv)
                finished_rows.append((witness_inv, witness_qty))
                db.flush()                                 # inventory_id gerekli
                shelf_life_m, extra_m = retention_cfg_months(db)
                retention_row = RetentionSample(
                    inventory_id=witness_inv.id,
                    production_history_id=prod.id,
                    item_id=recipe.target_item_id,
                    item_name=target_name,
                    lot_number=witness_inv.lot_number,
                    brand=brand or "Genel",
                    quantity=witness_qty,
                    initial_quantity=witness_qty,
                    unit=(target_item_obj.unit if target_item_obj else None),
                    produced_at=now,
                    retention_until=retention_until(now, shelf_life_m, extra_m),
                    status="stored",
                    source="production",
                    placed_by=actor,
                    domain=(recipe.domain or "cosmetics"),
                )
                db.add(retention_row)
                db.flush()
                db.add(RetentionSampleMovement(
                    sample_id=retention_row.id, movement_type="giris",
                    quantity=witness_qty, note=f"Üretim — Lot {produced_lot}",
                    performed_by=actor))
            # Tek toplam Input transaction'ı — audit'te bölünme not olarak yazılır
            split_note = (f" | Showroom: {showroom_qty}, Şahit: {witness_qty} ({brand})"
                          if witness_qty > 0 else "")
            input_tx = Transaction(
                item_id=recipe.target_item_id,
                lot_number=produced_lot,
                transaction_type="Input",
                quantity=data.produced_quantity,
                notes=(f"Üretim çıktısı — Reçete: {recipe.name} | "
                       f"Dil: {sel_lang_label} | Lot: {produced_lot}{split_note}"),
                performed_by=actor,
            )
            db.add(input_tx)
            # Her bitmiş satır (showroom, -S) ayrı output satırı; ikisi de
            # aynı tek Input'u taşır.
            for inv_row, qty_row in finished_rows:
                _consumption("output", input_tx, item_id=recipe.target_item_id,
                             lot=inv_row, lot_number=inv_row.lot_number, quantity=qty_row,
                             unit=(target_item_obj.unit if target_item_obj else None))
            # Item.current_stock = TOPLAM artar (witness de stokta sayılır —
            # dolaptaki numuneler satılabilir stoktan DÜŞMEZ, bilinçli karar).
            # Satır yukarıda lot sayacı için zaten kilitlendi, yeniden sorgulanmaz.
            if target_item_row:
                target_item_row.current_stock = round(
                    (target_item_row.current_stock or 0) + data.produced_quantity, 6
                )

        db.commit()

        # ── Low-stock alert: any consumed ingredient that crossed its threshold
        #     queues exactly one notification (one per ingredient line, not one
        #     per stock unit). Snapshots primitive values now; the BackgroundTask
        #     fires after the response is sent so the user sees no extra latency.
        #     Fiilen düşülen KART başına (bölünmüş satırda iki kart da bakılır).
        for item in pl.cards.values():
            if (item.min_stock_level or 0) > 0 and item.current_stock <= item.min_stock_level:
                background_tasks.add_task(
                    notify_low_stock,
                    item.name, item.current_stock, item.min_stock_level, item.unit or "",
                )

        # ── Stoktan düşülen kalem özeti — lab "ne düştü" diye sormasın ─────
        # Tüketilen REÇETE SATIRI sayısı (eskisi gibi; aynı kart iki satırdaysa
        # iki sayılır).  Hammadde / ambalaj ayrımı.
        hammadde_n = sum(len(ln.parts) for ln in pl.lines if not ln.is_ambalaj)
        ambalaj_n  = sum(len(ln.parts) for ln in pl.lines if ln.is_ambalaj)
        substituted = [
            {"recipe_item_id": ln.recipe_item_id,
             "recipe_item_name": pl.names.get(ln.recipe_item_id, ""),
             "sources": [{"item_id": p.item_id, "name": pl.names.get(p.item_id, ""),
                          "quantity": round(p.quantity, 6)} for p in ln.chosen]}
            for ln in pl.lines
            if ln.kind != "label" and any(p.item_id != ln.recipe_item_id for p in ln.chosen)
        ]

        msg = f"Üretim tamamlandı ({sel_lang_label}). {data.produced_quantity} birim stoğa eklendi."
        msg += f"  Stoktan düşülen: {hammadde_n} hammadde + {ambalaj_n} ambalaj/etiket kalemi."
        if ambalaj_n == 0:
            # En sık kafa karışıklığı: reçeteye ambalaj/etiket hiç eklenmemiş.
            msg += ("  ⚠ DİKKAT: Bu reçetede hiç ambalaj/etiket kalemi yok — "
                    "kavanoz, kapak, etiket stoktan DÜŞÜLMEDİ. Gerekiyorsa "
                    "Reçeteler sayfasından ambalaj bileşenlerini ekleyin.")
        if label_warnings:
            msg += (f"  ⚠ Şu kalemlerin {sel_lang_label} etiketi tanımlı değil, "
                    f"stoktan düşülmedi: {', '.join(label_warnings)}.")
        return {
            "message": msg,
            "production_id": prod.id,
            "lot_number": produced_lot if recipe.target_item_id else None,
            "label_language": sel_lang,
            "label_warnings": label_warnings,
            "consumed_hammadde": hammadde_n,
            "consumed_ambalaj":  ambalaj_n,
            # Reçete kartı yerine (ya da yanında) gruptaki başka karttan düşülen satırlar
            "substituted": substituted,
            "witness_quantity": witness_qty,
            "showroom_quantity": showroom_qty,
            "cabinet": brand or "",
        }

    except Exception:
        db.rollback()
        logger.exception("Üretim başlatılamadı (reçete %s, %s adet)",
                         data.recipe_id, data.produced_quantity)
        return JSONResponse(status_code=500, content={"detail": "Üretim sırasında hata oluştu, stoklar değiştirilmedi."})


# ─── QC Endpoints ────────────────────────────────────────────────────────────

@router.get("/qc/quarantine")
def list_quarantine(db: Session = Depends(get_db), domain: str = Depends(active_domain),
                    _: dict = Depends(require_permission("qc", "view"))):
    """
    QC sayfasının beslediği endpoint.  İki kaynaktan gelir:
      • status='QUARANTINE' — geleneksel mal kabul karantinası
      • qc_required=True   — üretim çıktıları (stok eklendi ama QC görmeli)
    Aktif panele (domain) göre süzülür.
    """
    from sqlalchemy import or_
    rows = (
        db.query(Inventory)
        .filter(
            Inventory.domain == domain,
            or_(
                Inventory.status == "QUARANTINE",
                Inventory.qc_required == True,
            )
        )
        .order_by(Inventory.id.desc())
        .all()
    )
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
            "qc_required":  r.qc_required,
            # 'source' = "Üretim" veya "Mal Kabul" — UI bunu rozetle gösterebilir
            "source":       "Üretim" if r.qc_required else "Mal Kabul",
            "created_at":   to_tr(r.created_at).strftime("%d.%m.%Y") if r.created_at else "",
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

    inv, item = _lock_lot_for_qc(db, inventory_id)
    if not inv:
        db.rollback()
        return JSONResponse(status_code=404, content={"detail": "Envanter kaydı bulunamadı."})
    # Hem mal kabul karantinası hem de üretim qc_required lot'ları işlenebilir
    if inv.status != "QUARANTINE" and not inv.qc_required:
        db.rollback()
        return JSONResponse(status_code=400, content={"detail": "Bu kayıt QC inceleme listesinde değil."})

    actor = current_user.get("full_name") or current_user.get("username") or "—"
    # production lot'u mu (qc_required+APPROVED) yoksa mal kabul karantinası mı?
    was_quarantine = (inv.status == "QUARANTINE")

    try:
        old_status         = inv.status
        inv.status         = data.status
        inv.qc_notes       = data.notes
        inv.qc_approved_by = actor
        inv.qc_required    = False     # QC karar verdi, artık listede çıkmasın
        inv.updated_at     = __import__("datetime").datetime.utcnow()

        # Stok değişikliği — karmaşık ama anlamlı:
        #   QUARANTINE + APPROVED → stok henüz eklenmemiş, ekle
        #   QUARANTINE + REJECTED → stok henüz eklenmemiş, hiçbir şey yapma
        #   qc_required (PROD)  + APPROVED → stok zaten üretimde eklendi, hiçbir şey yapma
        #   qc_required (PROD)  + REJECTED → stok üretimde eklendi, geri al + Adjustment audit
        if was_quarantine and data.status == "APPROVED" and item:
            item.current_stock = round((item.current_stock or 0) + inv.quantity, 6)
        elif not was_quarantine and data.status == "REJECTED" and item:
            item.current_stock = round((item.current_stock or 0) - inv.quantity, 6)
            db.add(Transaction(
                item_id=inv.item_id,
                lot_number=inv.lot_number,
                transaction_type="Adjustment",
                quantity=-inv.quantity,
                notes=(
                    f"Üretim QC reddi — Lot: {inv.lot_number}. "
                    f"Eklenen {inv.quantity} {item.unit or ''} stok geri alındı. "
                    f"Sebep: {data.notes}"
                ),
                performed_by=actor,
            ))
            _mark_retention_rejected(db, inv, actor, data.notes or "")

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

    inv, approved_item = _lock_lot_for_qc(db, inventory_id)
    if not inv:
        db.rollback()
        return JSONResponse(status_code=404, content={"detail": "Envanter kaydı bulunamadı."})
    # Hem mal kabul karantinası hem üretim qc_required lot'ları işlenebilir
    if inv.status != "QUARANTINE" and not inv.qc_required:
        db.rollback()
        return JSONResponse(status_code=400, content={"detail": "Bu lot zaten işlenmiş — tekrar değiştirilemez."})

    was_quarantine = (inv.status == "QUARANTINE")

    if not data.checklist:
        db.rollback()
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
        inv.qc_required    = False                  # QC karar verdi
        inv.updated_at     = _dt.utcnow()

        label = "Onaylandı ✓" if data.status == "APPROVED" else "Reddedildi ✗"

        # Stok ayarlaması — process_qc ile aynı mantık:
        #   QUARANTINE + APPROVED → stok henüz yok, ekle
        #   QUARANTINE + REJECTED → stok henüz yok, hiçbir şey
        #   qc_required (PROD) + APPROVED → stok zaten var, hiçbir şey
        #   qc_required (PROD) + REJECTED → stok geri al + Adjustment audit
        if was_quarantine and data.status == "APPROVED" and approved_item:
            approved_item.current_stock = round((approved_item.current_stock or 0) + inv.quantity, 6)
        elif not was_quarantine and data.status == "REJECTED" and approved_item:
            approved_item.current_stock = round((approved_item.current_stock or 0) - inv.quantity, 6)
            db.add(Transaction(
                item_id=inv.item_id,
                lot_number=inv.lot_number,
                transaction_type="Adjustment",
                quantity=-inv.quantity,
                notes=(
                    f"Üretim QC reddi — Lot: {inv.lot_number}. "
                    f"Eklenen {inv.quantity} {approved_item.unit or ''} stok geri alındı."
                ),
                performed_by=actor,
            ))
            _mark_retention_rejected(db, inv, actor, data.notes or "")

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


@router.get("/qc/{inventory_id}/form/export")
def export_qc_form(
    inventory_id: int,
    format: str = "pdf",
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("qc", "view")),
):
    """
    Bir lot'un kayıtlı QC formunu PDF veya Excel olarak indir.
    İzlenebilirlik sayfasından (lot detayı → Kalite Kontrol Formu) çağrılır.
    """
    fmt = (format or "pdf").lower()
    if fmt not in ("pdf", "excel", "xlsx"):
        return JSONResponse(status_code=400, content={"detail": "Geçersiz format (pdf veya excel)."})

    inv = db.query(Inventory).filter(Inventory.id == inventory_id).first()
    if not inv:
        return JSONResponse(status_code=404, content={"detail": "Envanter kaydı bulunamadı."})

    from core.qc_report import (
        parse_qc_form, render_qc_pdf, render_qc_excel, qc_export_filename)
    item = db.query(Item).filter(Item.id == inv.item_id).first()
    view = parse_qc_form(inv, item)
    if not view:
        return JSONResponse(status_code=404, content={"detail": "Bu lot için QC formu bulunamadı."})

    try:
        if fmt == "pdf":
            content, media, ext = render_qc_pdf(view), "application/pdf", "pdf"
        else:
            content = render_qc_excel(view)
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ext = "xlsx"
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "QC formu üretilemedi."})

    return StreamingResponse(
        io.BytesIO(content),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{qc_export_filename(view, ext)}"'},
    )
