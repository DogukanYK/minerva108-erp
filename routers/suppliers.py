# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Tedarikçi satın alma durumu + tedarikçi başına fiyatlar — /api/suppliers/{id}/…
Malzeme bazlı tedarikçi tercihi — /api/material-prefs (`prefs_router`).
Tedarikçi birleştirme — /api/suppliers/{kaybeden}/merge-preview|merge-into/{kazanan}.

Lab (Songül Hanım, 07.10.2026) küçük miktarlı satış yapan firmaları
"bitirilecek — alma" işaretleyip yerine alınacak firmaları "tercih edilen"
yapar; malzeme başına sıralı yedek tedarikçi ve "bu malzemede alma" da
tutulur.  İş kuralları core/suppliers.py'de.  Tedarikçi kartı listesi / ekle /
düzenle / pasife al routers/inventory.py'de kalır (items.* yetkileri).

KURALLAR
  • Durum ve malzeme tercihi yazma `suppliers.status`; birleştirme
    `suppliers.merge`; okuma items.view | reports.view.
  • Her sorgu aktif panelle sınırlı — başka panelin firması / kartı / grubu 404.
  • phase_out'ta sebep zorunlu (400); pasif firmanın durumu değişmez (409).
  • Tercih: kartın aktif grubu varsa varsayılan olarak GRUBA yazılır
    (`scope: "item"` ile karta); aynı kapsam + tedarikçi ikinci kez 409
    `pref_exists`.
  • Audit: `supplier.status`, `material_pref.create|update|delete`,
    `supplier.merge` (sayılarla).
"""
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from core import suppliers as SUP
from core.audit import log_admin_event
from core.domain import active_domain
from core.permissions import require_any_permission, require_permission
from core.stock_lots import lot_kind
from database import Item, MaterialGroup, MaterialSupplierPref, Supplier, SupplierPrice, get_db

router = APIRouter(prefix="/api/suppliers", tags=["suppliers"])
prefs_router = APIRouter(prefix="/api/material-prefs", tags=["suppliers"])


class StatusBody(BaseModel):
    status: str = Field(..., max_length=16)
    reason: Optional[str] = Field(None, max_length=SUP.REASON_MAX)


def _supplier(db: Session, supplier_id: int, domain: str) -> Optional[Supplier]:
    return (db.query(Supplier)
            .filter(Supplier.id == supplier_id, Supplier.domain == domain).first())


@router.put("/{supplier_id}/status")
def set_supplier_status(
    supplier_id: int,
    data: StatusBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("suppliers", "status")),
    domain: str = Depends(active_domain),
):
    """Firmanın satın alma durumunu yaz: normal | preferred | phase_out.
    Sebep phase_out'ta zorunlu, diğerlerinde isteğe bağlı (normal'e dönünce
    boş sebep eskisini temizler).  `status_by` / `status_at` damgalanır.
    Yanıt GET /api/suppliers satırıyla aynı şekil."""
    sup = _supplier(db, supplier_id, domain)
    if sup is None:
        return JSONResponse(status_code=404, content={"detail": "Tedarikçi bulunamadı."})
    if sup.is_active is False:
        return JSONResponse(status_code=409, content={
            "detail": "Pasif tedarikçinin durumu değiştirilemez — önce etkinleştirin."})
    status = SUP.normalize_status(data.status) if (data.status or "").strip() else None
    if status is None:
        return JSONResponse(status_code=400, content={
            "detail": "Durum normal, preferred ya da phase_out olmalı."})
    reason = (data.reason or "").strip() or None
    if status == "phase_out" and not reason:
        return JSONResponse(status_code=400, content={
            "detail": "“Bitirilecek — alma” için sebep yazın (ör. küçük miktar satıyor)."})
    old_status = SUP.normalize_status(sup.purchase_status) or "normal"
    old_reason = sup.status_reason
    if old_status == status and old_reason == reason:
        return SUP.serialize_supplier(sup)                       # değişiklik yok → damga yok
    actor = ((current_user or {}).get("full_name") or (current_user or {}).get("username") or "—")
    sup.purchase_status = status
    sup.status_reason = reason
    sup.status_by = actor[:100]
    sup.status_at = datetime.utcnow()          # naive UTC (DB kuralı)
    db.commit()
    db.refresh(sup)
    log_admin_event(db, request, actor=current_user, action="supplier.status",
                    target_type="supplier", target_id=sup.id, target_name=sup.name,
                    details={"eski": old_status, "yeni": status,
                             "eski_sebep": old_reason, "sebep": reason, "domain": domain})
    return SUP.serialize_supplier(sup)


@router.get("/{supplier_id}/prices")
def supplier_prices(
    supplier_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_any_permission(("items", "view"), ("reports", "view"))),
    domain: str = Depends(active_domain),
):
    """Bir firmanın fiyat satırları (Tedarikçiler → "Fiyatlar").

    `supplier_id`'ye bağlı satırlar + Excel'den gelip tedarikçi kartına
    BAĞLANAMAMIŞ (supplier_id NULL) ama firma anahtarı
    (`core.purchase_pricing.SupplierIndex`) aynı olan satırlar — "ULUDAG
    HERBAL" listesi "ULUDAĞ HERBAL" kartında görünsün.  Bağsız satırlar
    `matched: false` ile işaretlenir.  Pasif firmanın fiyatları da okunur."""
    from core.purchase_pricing import SupplierIndex
    from core.supplier_prices import normalize, serialize_price
    sup = _supplier(db, supplier_id, domain)
    if sup is None:
        return JSONResponse(status_code=404, content={"detail": "Tedarikçi bulunamadı."})
    rows = (db.query(SupplierPrice)
            .filter(SupplierPrice.domain == domain, SupplierPrice.supplier_id == sup.id).all())
    loose = (db.query(SupplierPrice)
             .filter(SupplierPrice.domain == domain, SupplierPrice.supplier_id.is_(None),
                     SupplierPrice.supplier_name.isnot(None)).all())
    if loose:
        names = [n for (n,) in db.query(Supplier.name).filter(Supplier.domain == domain).all()]
        ix = SupplierIndex(names + [r.supplier_name for r in loose])
        k = ix.key(sup.name)
        if k:
            rows += [r for r in loose if ix.key(r.supplier_name) == k]
    items = {}
    ids = {r.item_id for r in rows}
    if ids:
        items = {it.id: it for it in db.query(Item).filter(Item.id.in_(ids)).all()}
    st = SUP.normalize_status(sup.purchase_status) or "normal"
    out = [serialize_price(r, items.get(r.item_id), supplier_status=st) for r in rows]
    out.sort(key=lambda p: (normalize(p.get("material")), p["unit_price"] is None,
                            p["unit_price"] or 0.0))
    return {"supplier": SUP.serialize_supplier(sup), "prices": out, "total": len(out)}


# ─── Tedarikçi birleştirme ──────────────────────────────────────────────────

def _merge_err(e: "SUP.SupplierMergeError") -> JSONResponse:
    return JSONResponse(status_code=e.status, content={"detail": e.detail, "code": e.code})


@router.get("/{loser_id}/merge-preview/{survivor_id}")
def supplier_merge_preview(
    loser_id: int,
    survivor_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("suppliers", "merge")),
    domain: str = Depends(active_domain),
):
    """Birleştirme önizlemesi — veri DEĞİŞMEZ.  Yanıt: `loser` / `survivor`
    (liste satırı şekli), `counts` {items, items_active, lots, prices,
    price_overlaps (kazananda aynı kart + birimde fiyatı olan satır — ikisi de
    kalır), order_flags, consumptions, prefs, prefs_dropped (kazananın aynı
    kapsamda tercihi var → kaybedeninki düşer)}, `copy_fields` (kazananda boş,
    kaybedende dolu iletişim alanları) + `copy_fields_text`, `status_warning`."""
    try:
        return SUP.merge_preview(db, loser_id, survivor_id, domain)
    except SUP.SupplierMergeError as e:
        return _merge_err(e)


@router.post("/{loser_id}/merge-into/{survivor_id}")
def supplier_merge(
    loser_id: int,
    survivor_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("suppliers", "merge")),
    domain: str = Depends(active_domain),
):
    """Kaybeden tedarikçi kartını kazanana birleştir (mükerrer firma kartları).
    Bütün bağlar taşınır, kaybeden PASİF kalır; ayrıntı core/suppliers.
    merge_suppliers.  Öncesinde prod'da `pg_dump`.  Audit `supplier.merge`."""
    actor = ((current_user or {}).get("full_name") or (current_user or {}).get("username") or "—")[:100]
    try:
        summary = SUP.merge_suppliers(db, loser_id, survivor_id, domain, actor)
        db.commit()
    except SUP.SupplierMergeError as e:
        db.rollback()
        return _merge_err(e)
    except (IntegrityError, OperationalError):  # eşzamanlı çakışan tercih (kısmi tekil indeks) / kilitlenme
        db.rollback()
        return _merge_err(SUP.SupplierMergeError(
            "Birleştirme sırasında aynı kayıtlar başka bir işlemle değişti — önizlemeyi yenileyip "
            "tekrar deneyin.", 409, "conflict"))
    log_admin_event(db, request, actor=current_user, action="supplier.merge",
                    target_type="supplier", target_id=summary["survivor_id"],
                    target_name=summary["survivor_name"],
                    details={"kaybeden_id": summary["loser_id"], "kaybeden": summary["loser_name"],
                             "sayilar": summary["counts"], "kopyalanan": summary["copied_fields"],
                             "uyarilar": summary["warnings"], "domain": domain})
    c = summary["counts"]
    msg = (f"«{summary['loser_name']}» → «{summary['survivor_name']}» birleştirildi: "
           f"{c['items']} kart, {c['lots']} lot, {c['prices']} fiyat, {c['order_flags']} sipariş işareti, "
           f"{c['consumptions']} üretim satırı, {c['prefs'] - c['prefs_dropped']} tercih taşındı.")
    return dict(summary, message=msg)


# ─── Malzeme bazlı tedarikçi tercihi — /api/material-prefs ─────────────────

class PrefCreateBody(BaseModel):
    item_id: Optional[int] = None
    material_group_id: Optional[int] = None
    # auto: kartın aktif grubu varsa gruba, yoksa karta · item: daima karta
    scope: Literal["auto", "item"] = "auto"
    supplier_id: int
    preference: Literal["preferred", "avoid"] = "preferred"
    rank: int = Field(1, ge=1, le=SUP.RANK_MAX)
    note: Optional[str] = Field(None, max_length=SUP.PREF_NOTE_MAX)


class PrefUpdateBody(BaseModel):
    preference: Optional[Literal["preferred", "avoid"]] = None
    rank: Optional[int] = Field(None, ge=1, le=SUP.RANK_MAX)
    note: Optional[str] = Field(None, max_length=SUP.PREF_NOTE_MAX)


def _perr(status: int, detail: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, **extra})


def _actor(user: dict) -> str:
    return ((user or {}).get("full_name") or (user or {}).get("username") or "—")[:100]


def _active_group(db: Session, gid: Optional[int], domain: str) -> Optional[MaterialGroup]:
    if not gid:
        return None
    return (db.query(MaterialGroup)
            .filter(MaterialGroup.id == gid, MaterialGroup.domain == domain,
                    MaterialGroup.is_active == True).first())                 # noqa: E712


def _domain_item(db: Session, item_id: int, domain: str) -> Optional[Item]:
    return db.query(Item).filter(Item.id == item_id, Item.domain == domain).first()


def _lookups(db: Session, rows) -> dict:
    sids = {r.supplier_id for r in rows}
    iids = {r.item_id for r in rows if r.item_id}
    gids = {r.material_group_id for r in rows if r.material_group_id}
    return {"suppliers": {s.id: s for s in db.query(Supplier).filter(Supplier.id.in_(sids)).all()} if sids else {},
            "items": {i.id: i for i in db.query(Item).filter(Item.id.in_(iids)).all()} if iids else {},
            "groups": {g.id: g for g in db.query(MaterialGroup).filter(MaterialGroup.id.in_(gids)).all()}
            if gids else {}}


def _ser(db: Session, rows) -> list:
    lk = _lookups(db, rows)
    out = [SUP.serialize_pref(r, **lk) for r in rows]
    out.sort(key=lambda p: (p["preference"] != "preferred", p["rank"], p["scope"] != "item",
                            (p["supplier_name"] or "").casefold(), p["id"]))
    return out


@prefs_router.get("")
def list_material_prefs(
    item_id: Optional[int] = Query(None),
    group_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_any_permission(("items", "view"), ("reports", "view"))),
    domain: str = Depends(active_domain),
):
    """Bir kartın ya da grubun tedarikçi tercihleri.

    `?item_id=` → {item {id, name, group}, target {kind: group|item, id}
    (yeni tercihin varsayılan yazılacağı yer), prefs [grup + kart kapsamlı
    satırlar], effective {preferred [sıralı], avoid []} (kart kapsamı grubu
    ezer)}.  `?group_id=` → {group, prefs}.  İkisi birden ya da hiçbiri 400."""
    if (item_id is None) == (group_id is None):
        return _perr(400, "item_id ya da group_id verin (yalnız biri).")
    if group_id is not None:
        grp = _active_group(db, group_id, domain)
        if grp is None:
            return _perr(404, "Grup bulunamadı.")
        rows = (db.query(MaterialSupplierPref)
                .filter(MaterialSupplierPref.domain == domain,
                        MaterialSupplierPref.material_group_id == grp.id).all())
        return {"group": {"id": grp.id, "name": grp.name}, "prefs": _ser(db, rows)}
    it = _domain_item(db, item_id, domain)
    if it is None:
        return _perr(404, "Kart bulunamadı.")
    grp = _active_group(db, it.material_group_id, domain)
    q = db.query(MaterialSupplierPref).filter(MaterialSupplierPref.domain == domain)
    if grp is not None:
        q = q.filter((MaterialSupplierPref.item_id == it.id)
                     | (MaterialSupplierPref.material_group_id == grp.id))
    else:
        q = q.filter(MaterialSupplierPref.item_id == it.id)
    prefs = _ser(db, q.all())
    eff = SUP.effective_prefs([(p["supplier_id"], p["preference"], p["rank"], p["scope"] == "item")
                               for p in prefs])
    by_sup = {}
    for p in prefs:                             # kart kapsamı önce: aynı tedarikçide o geçerli
        if eff.get(p["supplier_id"]) == (p["preference"], p["rank"]):
            by_sup.setdefault(p["supplier_id"], p)
    live = list(by_sup.values())
    return {
        "item": {"id": it.id, "name": it.name,
                 "group": {"id": grp.id, "name": grp.name} if grp is not None else None},
        "target": {"kind": "group", "id": grp.id} if grp is not None else {"kind": "item", "id": it.id},
        "prefs": prefs,
        "effective": {
            "preferred": sorted((p for p in live if p["preference"] == "preferred"),
                                key=lambda p: (p["rank"], (p["supplier_name"] or "").casefold())),
            "avoid": sorted((p for p in live if p["preference"] == "avoid"),
                            key=lambda p: (p["supplier_name"] or "").casefold()),
        },
    }


@prefs_router.post("", status_code=201)
def create_material_pref(
    data: PrefCreateBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("suppliers", "status")),
    domain: str = Depends(active_domain),
):
    """Tercih ekle.  Hedef: `material_group_id` (grup) YA DA `item_id` (kart;
    `scope: auto` + kartın aktif grubu varsa gruba yazılır).  Tedarikçi aktif
    ve bu panelde olmalı (kartı olmayan yeni firma da olur); Bitmiş Ürün
    kartına tercih yazılmaz.  Aynı kapsam + tedarikçi varsa 409
    {code: 'pref_exists', id}."""
    if (data.item_id is None) == (data.material_group_id is None):
        return _perr(400, "item_id ya da material_group_id verin (yalnız biri).")
    # FOR SHARE: eşzamanlı tedarikçi birleştirmesini bekle (pasif kaybedene tercih yazılmasın)
    sup = (db.query(Supplier)
           .filter(Supplier.id == data.supplier_id, Supplier.domain == domain)
           .with_for_update(read=True).populate_existing().first())
    if sup is None:
        return _perr(404, "Tedarikçi bulunamadı.")
    if sup.is_active is False:
        return _perr(409, f"«{sup.name}» pasif — önce etkinleştirin.")
    grp, it = None, None
    if data.material_group_id is not None:
        grp = _active_group(db, data.material_group_id, domain)
        if grp is None:
            return _perr(404, "Grup bulunamadı.")
    else:
        it = _domain_item(db, data.item_id, domain)
        if it is None:
            return _perr(404, "Kart bulunamadı.")
        if it.is_active is False:
            return _perr(409, f"«{it.name}» kartı pasif.")
        if lot_kind(it.category) == "finished":
            return _perr(400, "Bitmiş ürün kartına tedarikçi tercihi yazılmaz.")
        if data.scope == "auto":
            grp = _active_group(db, it.material_group_id, domain)
            if grp is not None:
                it = None
    q = db.query(MaterialSupplierPref).filter(MaterialSupplierPref.supplier_id == sup.id)
    q = (q.filter(MaterialSupplierPref.material_group_id == grp.id, MaterialSupplierPref.item_id.is_(None))
         if grp is not None else
         q.filter(MaterialSupplierPref.item_id == it.id, MaterialSupplierPref.material_group_id.is_(None)))
    dup = q.first()
    if dup is not None:
        return _perr(409, f"«{sup.name}» için bu malzemede zaten bir tercih var — onu düzenleyin.",
                     code="pref_exists", id=dup.id)
    pref = MaterialSupplierPref(
        domain=domain, material_group_id=grp.id if grp is not None else None,
        item_id=it.id if it is not None else None, supplier_id=sup.id,
        preference=data.preference, rank=data.rank if data.preference == "preferred" else 1,
        note=(data.note or "").strip() or None, created_by=_actor(current_user))
    db.add(pref)
    try:
        db.commit()
    except IntegrityError:                     # eşzamanlı ikinci kayıt — kısmi tekil indeks
        db.rollback()
        return _perr(409, f"«{sup.name}» için bu malzemede zaten bir tercih var.", code="pref_exists")
    db.refresh(pref)
    row = _ser(db, [pref])[0]
    log_admin_event(db, request, actor=current_user, action="material_pref.create",
                    target_type="material_pref", target_id=pref.id,
                    target_name=(row["group_name"] or row["item_name"] or "")[:150],
                    details={"kapsam": row["scope"], "grup_id": pref.material_group_id,
                             "kart_id": pref.item_id, "tedarikci_id": sup.id, "tedarikci": sup.name,
                             "tercih": pref.preference, "sira": pref.rank, "not": pref.note,
                             "domain": domain})
    return row


def _pref(db: Session, pref_id: int, domain: str) -> Optional[MaterialSupplierPref]:
    return (db.query(MaterialSupplierPref)
            .filter(MaterialSupplierPref.id == pref_id, MaterialSupplierPref.domain == domain).first())


@prefs_router.put("/{pref_id}")
def update_material_pref(
    pref_id: int,
    data: PrefUpdateBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("suppliers", "status")),
    domain: str = Depends(active_domain),
):
    """Tercih / sıra / not değiştir (kapsam ve tedarikçi değişmez — silip
    yeniden ekleyin).  Yalnız gövdede gelen alanlar yazılır."""
    pref = _pref(db, pref_id, domain)
    if pref is None:
        return _perr(404, "Tercih bulunamadı.")
    changes = {}
    new = {}
    if data.preference is not None:
        new["preference"] = data.preference
    if data.rank is not None:
        new["rank"] = data.rank
    if "note" in data.model_fields_set:
        new["note"] = (data.note or "").strip() or None
    for col, val in new.items():
        old = getattr(pref, col)
        if old != val:
            changes[col] = {"eski": old, "yeni": val}
            setattr(pref, col, val)
    if changes:
        pref.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(pref)
    row = _ser(db, [pref])[0]
    if changes:
        log_admin_event(db, request, actor=current_user, action="material_pref.update",
                        target_type="material_pref", target_id=pref.id,
                        target_name=(row["group_name"] or row["item_name"] or "")[:150],
                        details={"tedarikci": row["supplier_name"], "changes": changes, "domain": domain})
    return row


@prefs_router.delete("/{pref_id}")
def delete_material_pref(
    pref_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("suppliers", "status")),
    domain: str = Depends(active_domain),
):
    """Tercihi kaldır (satır silinir; audit eski değerleri tutar)."""
    pref = _pref(db, pref_id, domain)
    if pref is None:
        return _perr(404, "Tercih bulunamadı.")
    row = _ser(db, [pref])[0]
    db.delete(pref)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="material_pref.delete",
                    target_type="material_pref", target_id=pref_id,
                    target_name=(row["group_name"] or row["item_name"] or "")[:150],
                    details={"eski": {k: row[k] for k in ("scope", "material_group_id", "item_id",
                                                          "supplier_id", "supplier_name", "preference",
                                                          "rank", "note")},
                             "domain": domain})
    return {"message": "Tercih kaldırıldı.", "id": pref_id}
