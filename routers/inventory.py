# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Inventory router — items, suppliers, receiving, transactions, traceability,
and Excel imports (both classic templated import + smart auto-detect import).
"""
import re

from fastapi import APIRouter, Depends, UploadFile, File, BackgroundTasks
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session, joinedload
from pydantic import BaseModel, Field
from typing import Optional, List


# ── Etiket dil/grup yardımcıları ────────────────────────────────────────────
# Migration (d579c1629598) ile aynı mantık — isimden dil belirteçlerini at,
# normalize et → label_group anahtarı.  TR ve EN kardeşleri aynı anahtarda
# buluşur, üretimde dil seçilince doğru kardeşe inilir.
_LANG_TOKEN_RE = re.compile(r'\(\s*(?:eng?|ing|tur|tr|t[üu]rk(?:[çc]e)?|english)\s*\)', re.IGNORECASE)


def _label_group_key(name: str) -> str:
    s = _LANG_TOKEN_RE.sub(' ', name or '')
    s = re.sub(r'\s+', ' ', s.lower()).strip()
    return s


def _norm_language(val: Optional[str]) -> Optional[str]:
    """Kullanıcı girdisini 'TR' / 'EN' / None'a normalize et."""
    v = (val or '').strip().upper()
    if v in ('TR', 'TUR', 'TÜRKÇE', 'TURKCE'):    return 'TR'
    if v in ('EN', 'ENG', 'İNG', 'ING', 'ENGLISH'): return 'EN'
    return None

from database import (
    to_tr,
    get_db, Item, Supplier, Inventory, Transaction,
    Recipe, RecipeIngredient,
)
from core.auth import get_current_user
from core.permissions import _can_see_finance, require_permission
from core.notifications import notify_low_stock
from core.undo import record as record_undoable

router = APIRouter(prefix="/api", tags=["inventory"])


# ─── Schemas ────────────────────────────────────────────────────────────────

class ItemCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    category: Optional[str] = Field(None, max_length=50)
    unit: str = Field("adet", max_length=20)
    min_stock:      Optional[float] = 0.0
    cost_price:     Optional[float] = 0.0
    parent_id:      Optional[int]   = None
    variation_name: Optional[str]   = Field(None, max_length=100)
    barcode:        Optional[str]   = Field(None, max_length=64)
    pkg_type:       Optional[str]   = Field(None, max_length=20)
    # Etiket dili — 'TR' / 'EN' / None (dilsiz).  Sadece etiketlerde anlamlı.
    language:       Optional[str]   = Field(None, max_length=8)
    # Varsayılan tedarikçi — mal kabulde bu ürün seçilince oto-doldurulur
    supplier_id:    Optional[int]   = None


class BulkDeleteRequest(BaseModel):
    # max_items 1000: tek istekte 1000 ürün silmek operasyonel kapsamımızdan büyük
    item_ids: List[int] = Field(..., min_length=1, max_length=1000)


class SupplierCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    contact_person: Optional[str] = Field(None, max_length=100)
    email: Optional[str] = Field(None, max_length=150)
    phone: Optional[str] = Field(None, max_length=30)
    notes: Optional[str] = Field(None, max_length=2000)


class SupplierBulkDeleteRequest(BaseModel):
    supplier_ids: List[int] = Field(..., min_length=1, max_length=500)


class StockReceiveRequest(BaseModel):
    item_id: int
    supplier_id: Optional[int] = None
    lot_number: str = Field(..., min_length=1, max_length=100)
    expiry_date: Optional[str] = Field(None, max_length=20)
    quantity: float
    location: Optional[str] = Field(None, max_length=100)


class StockAdjustRequest(BaseModel):
    """
    Manual stock correction. The user supplies the *target* quantity that
    Item.current_stock should hold; backend computes the delta and writes
    an immutable Transaction(type='Adjustment') so the change is auditable.
    Reason is mandatory — no silent corrections.
    """
    item_id: int
    new_quantity: float
    reason: str = Field(..., min_length=3, max_length=200)
    lot_number: Optional[str] = Field(None, max_length=100)


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

    # Supplier lookup — listing'de supplier_name göstermek için
    supplier_name_by_id = {
        s.id: s.name for s in db.query(Supplier).filter(Supplier.is_active == True).all()
    }

    finance_ok = _can_see_finance(current_user)
    return [
        {
            "id":              i.id,
            "name":            i.name,
            "category":        i.category,
            "pkg_type":        i.pkg_type or "",                # Phase 10 — Ambalaj alt-tipi
            "unit":            i.unit,
            "min_stock_level": i.min_stock_level,
            "current_stock":   i.current_stock,
            # ── Finance-gated: zeroed for lab roles (defense in depth vs. DevTools snooping) ──
            "cost_price":      round(i.cost_price or 0.0, 4) if finance_ok else 0.0,
            "parent_id":       i.parent_id,
            "parent_name":     name_by_id.get(i.parent_id) if i.parent_id else None,
            "variation_name":  i.variation_name,
            "barcode":         i.barcode or "",                 # Phase 9
            "language":        i.language or "",                 # Phase 15 — etiket dili
            "label_group":     i.label_group or "",
            "child_count":     child_count.get(i.id, 0),
            "is_parent":       child_count.get(i.id, 0) > 0,
            "is_variation":    i.parent_id is not None,
            "supplier_id":     i.supplier_id,
            "supplier_name":   supplier_name_by_id.get(i.supplier_id) if i.supplier_id else None,
            "created_at":      to_tr(i.created_at).strftime("%d.%m.%Y") if i.created_at else "",
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

    pkg = (data.pkg_type.strip() if data.pkg_type and data.pkg_type.strip() else None)
    is_label = (data.category == "Ambalaj" and pkg == "etiket")
    lang     = _norm_language(data.language) if is_label else None
    # label_group sadece etiketlerde — isimden türetilir (TR/EN kardeşleri buluşsun)
    grp      = _label_group_key(data.name) if is_label else None

    item = Item(
        name=data.name,
        category=data.category,
        unit=data.unit,
        min_stock_level=data.min_stock  or 0.0,
        cost_price=data.cost_price or 0.0,
        parent_id=data.parent_id,
        variation_name=(data.variation_name.strip() if data.parent_id and data.variation_name else None),
        barcode=(data.barcode.strip() if data.barcode and data.barcode.strip() else None),
        pkg_type=pkg,
        language=lang,
        label_group=grp,
        supplier_id=data.supplier_id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "message": "Ürün başarıyla eklendi."}


def _item_delete_blockers(db: Session, item_id: int) -> list[str]:
    """
    HARD-delete'in mantıksal/şemasal olarak imkansız olduğu gerekçeleri döner.

    Audit kayıtları (transaction/inventory) ARTIK BURADA YOK — onlar için
    soft-delete devreye girer (bkz. delete_item).  Sadece şu durumlar HARD
    blok: silinirse veri orphan/corrupt olur.

    Kapsam:
      1) RecipeIngredient — bu ürünü malzeme olarak kullanan reçete varsa,
         silinirse reçete bozulur (NOT NULL constraint).
      2) Recipe.target_item_id — bu ürünün çıktı olduğu reçete var (silinirse
         reçete bir hedefe işaret edemez; nullable ama mantıksal kayıp).
      3) Item.parent_id — varyasyonlar parent'a bağlı, silinirse orphan.
    """
    blockers: list[str] = []

    # 1) Reçete malzemesi
    using = (
        db.query(Recipe.name)
        .join(RecipeIngredient, RecipeIngredient.recipe_id == Recipe.id)
        .filter(RecipeIngredient.item_id == item_id)
        .distinct()
        .limit(6)
        .all()
    )
    if using:
        names = ", ".join(r[0] for r in using[:5])
        more  = " …" if len(using) > 5 else ""
        blockers.append(f"şu reçetelerde malzeme: {names}{more}")

    # 2) Reçete hedefi
    targets = db.query(Recipe.name).filter(Recipe.target_item_id == item_id).limit(6).all()
    if targets:
        names = ", ".join(r[0] for r in targets[:5])
        more  = " …" if len(targets) > 5 else ""
        blockers.append(f"şu reçetenin hedef ürünü: {names}{more}")

    # 3) Varyasyonlar
    kids = db.query(Item.name).filter(Item.parent_id == item_id).limit(6).all()
    if kids:
        names = ", ".join(r[0] for r in kids[:5])
        more  = " …" if len(kids) > 5 else ""
        blockers.append(f"varyasyonu mevcut: {names}{more}")

    return blockers


def _item_has_audit(db: Session, item_id: int) -> bool:
    """Transaction veya inventory satırı varsa True — hard-delete yerine
    soft-delete kullanılır."""
    if db.query(Transaction.id).filter(Transaction.item_id == item_id).first():
        return True
    if db.query(Inventory.id).filter(Inventory.item_id == item_id).first():
        return True
    return False


@router.delete("/items/{item_id}")
def delete_item(item_id: int, db: Session = Depends(get_db), _: dict = Depends(require_permission("items", "delete"))):
    """
    Ürün silme — iki davranış birden:

    1) **Hard delete** — hiç bağ yoksa (audit / recipe / variation) satır
       DB'den kalkar.  Yeni oluşturulmuş hatalı ürünler için ideal.

    2) **Soft delete** — transaction veya inventory kaydı varsa
       `is_active=False` yapılır.  Ürün /api/items listesinden kaybolur
       (zaten is_active=True filtresi var), ama audit kayıtları + lot
       geçmişi DB'de okunur kalır (rapor / raporlama / restore için).

    3) **Block** — sadece reçete bağı veya varyasyon orphan riski olursa.
       Bu gerçekten silinmemeli; aksi halde reçeteler bozulur.

    Soft-delete kullanıcıya "Ürün arşivlendi" mesajı ile bildirilir; toast
    aynı yeşil tonda, lab fark etmez ama biz audit'i koruruz.
    """
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        return JSONResponse(status_code=404, content={"detail": "Ürün bulunamadı."})

    blockers = _item_delete_blockers(db, item_id)
    if blockers:
        msg = "Bu ürün silinemez — " + "; ".join(blockers) + "."
        return JSONResponse(status_code=400, content={"detail": msg})

    if _item_has_audit(db, item_id):
        # Soft-delete: kayıtlar korunsun
        item.is_active = False
        db.commit()
        return {
            "message": f"'{item.name}' arşivlendi (geçmiş kayıtlar korundu).",
            "soft_deleted": True,
        }

    # Hard-delete: hiç bağ yok
    db.delete(item)
    db.commit()
    return {"message": "Ürün silindi.", "soft_deleted": False}


@router.put("/items/{item_id}")
def update_item(
    item_id: int,
    data: ItemCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("items", "edit")),
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

    # ── Undo için BEFORE snapshot (mutation öncesi mevcut değerleri yakala) ──
    before_snapshot = {
        "name":            item.name,
        "category":        item.category,
        "unit":            item.unit,
        "min_stock_level": float(item.min_stock_level or 0.0),
        "cost_price":      float(item.cost_price or 0.0),
        "parent_id":       item.parent_id,
        "variation_name":  item.variation_name,
        "barcode":         item.barcode,
        "pkg_type":        item.pkg_type,
        "language":        item.language,
        "label_group":     item.label_group,
        "supplier_id":     item.supplier_id,
    }

    item.name            = data.name
    item.category        = data.category
    item.unit            = data.unit
    item.min_stock_level = data.min_stock  or 0.0
    item.cost_price      = data.cost_price or 0.0
    item.parent_id       = data.parent_id
    item.variation_name  = (data.variation_name.strip() if data.parent_id and data.variation_name else None)
    item.barcode         = (data.barcode.strip() if data.barcode and data.barcode.strip() else None)
    item.pkg_type        = (data.pkg_type.strip().lower() if data.pkg_type and data.pkg_type.strip() else None)
    item.supplier_id     = data.supplier_id
    # Etiket dil/grup — sadece category=Ambalaj + pkg_type=etiket kombinasyonunda
    _is_label = (item.category == "Ambalaj" and item.pkg_type == "etiket")
    item.language    = _norm_language(data.language) if _is_label else None
    item.label_group = _label_group_key(data.name)   if _is_label else None

    # ── Undo entry ───────────────────────────────────────────────────────
    try:
        uid = int(current_user.get("sub", 0))
        if uid:
            record_undoable(
                db,
                user_id=uid,
                action_type="item_edit",
                target_table="items",
                target_id=item.id,
                payload={"item_id": item.id, "before": before_snapshot},
                description=f"Ürün düzenlendi: {item.name}",
            )
    except Exception:
        pass

    db.commit()

    # ── Low-stock alert: if the edit (typically a min_stock_level bump) leaves
    #     the item at or below threshold, queue a notification on the response.
    if item.min_stock_level > 0 and item.current_stock <= item.min_stock_level:
        background_tasks.add_task(
            notify_low_stock,
            item.name, item.current_stock, item.min_stock_level, item.unit or "",
        )

    return {"id": item.id, "message": "Ürün güncellendi."}


@router.get("/items/by-barcode/{barcode}")
def get_item_by_barcode(
    barcode: str,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    """
    Server-side resolver — useful when the client doesn't have the full item
    list cached (e.g. mobile receiving flow on a slow connection). Frontend
    pages that already loaded /api/items can match locally without this call.
    Returns 404 when no active item carries the given barcode.
    """
    code = (barcode or "").strip()
    if not code:
        return JSONResponse(status_code=400, content={"detail": "Barkod boş."})
    item = (
        db.query(Item)
        .filter(Item.barcode == code, Item.is_active == True)
        .first()
    )
    if not item:
        return JSONResponse(status_code=404, content={"detail": f"Bu barkoda sahip ürün yok: {code}"})
    return {
        "id":              item.id,
        "name":            item.name,
        "category":        item.category,
        "unit":            item.unit,
        "current_stock":   item.current_stock,
        "barcode":         item.barcode,
        "parent_id":       item.parent_id,
        "variation_name":  item.variation_name,
    }


@router.post("/items/bulk-delete")
def bulk_delete_items(data: BulkDeleteRequest, db: Session = Depends(get_db), _: dict = Depends(require_permission("items", "delete"))):
    """
    Toplu silme — single-delete'le aynı 3 davranış:
      • Reçete/varyasyon bağı → block, batch iptal
      • Audit kaydı → soft-delete (is_active=False)
      • Bağsız → hard-delete

    Hard-blok'lar mevcutsa hiçbir şey silinmez (yarım iş kalmasın).
    Aksi halde her ürün uygun yola gönderilir; kullanıcıya kaç hard +
    kaç soft yapıldığı raporlanır.
    """
    blocked:  list[str] = []   # "ürün adı (gerekçe)"
    soft_ids: list[int] = []
    hard_ids: list[int] = []

    for iid in data.item_ids:
        it = db.query(Item).filter(Item.id == iid).first()
        if not it:
            continue
        reasons = _item_delete_blockers(db, iid)
        if reasons:
            blocked.append(f"{it.name} — {'; '.join(reasons)}")
            continue
        if _item_has_audit(db, iid):
            soft_ids.append(iid)
        else:
            hard_ids.append(iid)

    if blocked:
        msg = (
            f"{len(blocked)} ürün silinemediği için toplu silme iptal edildi:\n• "
            + "\n• ".join(blocked[:10])
            + ("\n…" if len(blocked) > 10 else "")
        )
        return JSONResponse(status_code=400, content={"detail": msg})

    hard_count = 0
    soft_count = 0
    if hard_ids:
        hard_count = db.query(Item).filter(Item.id.in_(hard_ids)).delete(synchronize_session=False)
    if soft_ids:
        soft_count = (
            db.query(Item).filter(Item.id.in_(soft_ids))
            .update({Item.is_active: False}, synchronize_session=False)
        )
    db.commit()

    parts = []
    if hard_count: parts.append(f"{hard_count} ürün silindi")
    if soft_count: parts.append(f"{soft_count} ürün arşivlendi (geçmiş korundu)")
    return {"message": ", ".join(parts) + ".", "hard": hard_count, "soft": soft_count}


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
            "created_at": to_tr(s.created_at).strftime("%d.%m.%Y") if s.created_at else "",
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
    # joinedload — item + supplier ilişkileri tek query'de gelir (N+1 önler)
    rows = (
        db.query(Inventory)
        .options(joinedload(Inventory.item), joinedload(Inventory.supplier))
        .order_by(Inventory.id.desc())
        .all()
    )
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
            "created_at": to_tr(r.created_at).strftime("%d.%m.%Y") if r.created_at else "",
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

        new_inventory: Optional[Inventory] = None
        stock_before = float(item.current_stock or 0.0)

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
            new_inventory = Inventory(
                item_id=data.item_id,
                supplier_id=data.supplier_id,
                lot_number=data.lot_number,
                expiry_date=data.expiry_date,
                quantity=data.quantity,
                location=data.location,
                status="APPROVED",
                received_by=actor,                      # Audit trail
            )
            db.add(new_inventory)

        # ── Transaction kaydı (2.4) ─────────────────────────────────────────
        tx = Transaction(
            item_id=data.item_id,
            lot_number=data.lot_number,
            transaction_type="Input",
            quantity=data.quantity,
            notes=f"Mal kabul — Lot: {data.lot_number}" + (f", Konum: {data.location}" if data.location else ""),
            performed_by=actor,                          # Audit trail
        )
        db.add(tx)

        # ── items.current_stock güncelle (üretim modülü ile uyum) ───────────
        item.current_stock = round(item.current_stock + data.quantity, 6)
        stock_after = float(item.current_stock)

        # ── Undo log — sadece yeni lot (upsert değil) için ─────────────────
        # Upsert durumunda undo karmaşık (mevcut lot'tan subtract); şimdilik skip.
        if new_inventory is not None:
            try:
                db.flush()
                uid = int(current_user.get("sub", 0))
                if uid:
                    record_undoable(
                        db,
                        user_id=uid,
                        action_type="inventory_receive",
                        target_table="inventory",
                        target_id=new_inventory.id,
                        payload={
                            "inventory_id":      new_inventory.id,
                            "transaction_id":    tx.id,
                            "item_id":           item.id,
                            "received_quantity": float(data.quantity),
                            "item_stock_before": stock_before,
                            "item_stock_after":  stock_after,
                            "lot_number":        data.lot_number,
                        },
                        description=(
                            f"Lot kabul: {data.quantity} {item.unit or ''} "
                            f"{item.name} (Lot {data.lot_number})"
                        ),
                    )
            except Exception:
                pass

        db.commit()
        return {"message": f"Mal kabul başarılı. {data.quantity} {item.unit} stoka eklendi."}

    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Mal kabul sırasında hata oluştu."})


# ─── Stock Adjustment (manual correction) ──────────────────────────────────
# Black-box invariant: stock changes outside of receiving/production/QC must
# still leave a fingerprint. This endpoint never deletes or rewrites history;
# it appends an immutable Transaction(type='Adjustment') and updates the
# canonical Item.current_stock to the requested value. The reason string is
# required so audit reviewers can see *why* the correction happened.

@router.post("/inventory/adjust", status_code=201)
def adjust_stock(
    data: StockAdjustRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("inventory", "adjust")),
):
    if data.new_quantity < 0:
        return JSONResponse(status_code=400, content={"detail": "Stok miktarı negatif olamaz."})

    reason = (data.reason or "").strip()
    if len(reason) < 3:
        return JSONResponse(status_code=422, content={"detail": "Sebep en az 3 karakter olmalıdır."})

    item = db.query(Item).filter(Item.id == data.item_id).with_for_update().first()
    if not item:
        return JSONResponse(status_code=404, content={"detail": "Ürün bulunamadı."})

    actor    = current_user.get("full_name") or current_user.get("username") or "—"
    old_qty  = float(item.current_stock or 0)
    new_qty  = float(data.new_quantity)
    delta    = round(new_qty - old_qty, 6)

    if abs(delta) < 1e-9:
        return JSONResponse(status_code=400, content={
            "detail": f"Yeni miktar mevcut stokla aynı ({old_qty} {item.unit or ''}). Düzeltme gerekmez."
        })

    try:
        # ── Item.current_stock güncelle ────────────────────────────────────
        item.current_stock = round(new_qty, 6)

        # ── Lot-level adjustment opsiyonel: belirli bir lot'un quantity'sini
        #     hedef değere düşür/yükselt. Yoksa sadece item-level düzeltme.
        lot_note = ""
        if data.lot_number:
            inv = (
                db.query(Inventory)
                .filter(Inventory.item_id == data.item_id, Inventory.lot_number == data.lot_number)
                .with_for_update()
                .first()
            )
            if inv:
                # Bu lot için yeni miktar mantıklı mı kontrol etmiyoruz — kullanıcı
                # zaten gerekçeyi girdi. Sadece lot satırını da güncelleyelim.
                inv.quantity = round(new_qty, 6)
                inv.updated_at = __import__("datetime").datetime.utcnow()
                lot_note = f" | Lot: {data.lot_number}"

        # ── Immutable audit kaydı (delta hem +/- olabilir) ─────────────────
        sign = "+" if delta > 0 else ""
        tx = Transaction(
            item_id=item.id,
            lot_number=data.lot_number,
            transaction_type="Adjustment",
            quantity=delta,            # signed delta (-8.0 veya +3.0)
            notes=(
                f"Stok düzeltme — Eski: {old_qty} {item.unit or ''} → "
                f"Yeni: {new_qty} {item.unit or ''} "
                f"(Δ {sign}{delta}){lot_note} | Sebep: {reason[:200]}"
            ),
            performed_by=actor,
        )
        db.add(tx)
        db.flush()    # tx.id'i undo payload'a koyabilmek için

        # ── Undo log — Ctrl+Z için ─────────────────────────────────────────
        try:
            uid = int(current_user.get("sub", 0))
            if uid:
                record_undoable(
                    db,
                    user_id=uid,
                    action_type="stock_adjust",
                    target_table="items",
                    target_id=item.id,
                    payload={
                        "item_id":        item.id,
                        "before_stock":   old_qty,
                        "after_stock":    new_qty,
                        "transaction_id": tx.id,
                    },
                    description=f"Stok düzeltildi: {old_qty} → {new_qty} {item.unit or ''} ({item.name})",
                )
        except Exception:
            pass  # Undo başarısız olsa ana mutation hâlâ commit edilir.

        db.commit()
        return {
            "message": f"Stok düzeltildi: {old_qty} → {new_qty} {item.unit or ''} (Δ {sign}{delta}).",
            "item_id":    item.id,
            "old_qty":    old_qty,
            "new_qty":    new_qty,
            "delta":      delta,
        }

    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Stok düzeltme sırasında hata oluştu."})


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
    # Supplier lookup — listing'de supplier_name göstermek için tek query
    supplier_name_by_id = {
        s.id: s.name for s in db.query(Supplier).filter(Supplier.is_active == True).all()
    }
    return [
        {
            "item_id":       i.id,
            "name":          i.name,
            "category":      i.category or "Diğer",
            "unit":          i.unit,
            "pkg_type":      i.pkg_type or "",        # /stocks Etiket vs Ambalaj ayırımı için
            "total_stock":   round(float(i.current_stock or 0), 4),
            "supplier_id":   i.supplier_id,
            "supplier_name": supplier_name_by_id.get(i.supplier_id) if i.supplier_id else None,
        }
        for i in items
    ]


@router.get("/inventory/by-item/{item_id}")
def inventory_by_item(
    item_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    """
    Tek bir ürünün lot bazlı envanter dökümü — Stoklar sayfasında satıra
    tıklayınca açılan detay.  Hangi lot nerede (Showroom / Şahit Numune
    Dolabı / diğer konum), ne kadarı, hangi durumda.

    Lokasyon kırılımı `by_location` alanında özetlenir — üretimde şahit
    numune ayrımı yapılan ürünlerde "kaçı showroom, kaçı şahit" tek bakışta
    görülür.
    """
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        return JSONResponse(status_code=404, content={"detail": "Ürün bulunamadı."})

    rows = (
        db.query(Inventory)
        .filter(Inventory.item_id == item_id)
        .order_by(Inventory.id.desc())
        .all()
    )

    lots = []
    by_location: dict[str, float] = {}
    for r in rows:
        loc = (r.location or "").strip() or "(Konum belirtilmemiş)"
        qty = float(r.quantity or 0)
        by_location[loc] = round(by_location.get(loc, 0.0) + qty, 4)
        lots.append({
            "lot_number":  r.lot_number,
            "location":    loc,
            "quantity":    round(qty, 4),
            "status":      r.status or "",
            "qc_required": bool(r.qc_required),
            "expiry_date": r.expiry_date or "",
            "received_by": r.received_by or "",
            "created_at":  to_tr(r.created_at).strftime("%d.%m.%Y %H:%M") if r.created_at else "",
        })

    return {
        "item_id":       item.id,
        "item_name":     item.name,
        "category":      item.category or "",
        "unit":          item.unit or "",
        # Item.current_stock source-of-truth; lot toplamı bundan sapabilir
        # (ör. üretim çıktısı doğrudan current_stock'a yazılır).
        "current_stock": round(float(item.current_stock or 0), 4),
        "lot_total":     round(sum(l["quantity"] for l in lots), 4),
        "by_location":   [
            {"location": loc, "total": tot}
            for loc, tot in sorted(by_location.items(), key=lambda x: -x[1])
        ],
        "lots":          lots,
    }


@router.get("/transactions")
def list_transactions(db: Session = Depends(get_db)):
    # 500 satır × N+1 ürün lookup'ı yerine joinedload ile tek query
    rows = (
        db.query(Transaction)
        .options(joinedload(Transaction.item))
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
            "timestamp": to_tr(r.timestamp).strftime("%d.%m.%Y %H:%M") if r.timestamp else "",
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
            "created_at":     to_tr(inv.created_at).strftime("%d.%m.%Y %H:%M") if inv.created_at else "",
            "updated_at":     to_tr(inv.updated_at).strftime("%d.%m.%Y %H:%M") if inv.updated_at else "",
        }
        # ── QC formu (varsa) — soru metinleriyle etiketlenmiş okunur görünüm ──
        from core.qc_report import parse_qc_form
        out["qc_form"] = parse_qc_form(inv, item)
    else:
        out["lot_info"] = None
        out["qc_form"]  = None

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
            "produced_at":       to_tr(prod.produced_at).strftime("%d.%m.%Y %H:%M") if prod.produced_at else "—",
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
            "timestamp":        to_tr(t.timestamp).strftime("%d.%m.%Y %H:%M") if t.timestamp else "—",
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
    """
    Lightweight user list — drives the dropdown on the traceability page.
    Each entry includes `tx_count` so the UI can show "(N işlem)" hints,
    avoiding the "Songül seçildi → boş" surprise.

    Match is case-insensitive and tries both full_name and username because
    the legacy code wrote actor names in mixed casings (e.g. "Doğukan
    YALÇINKAYA" vs "Doğukan Yalçınkaya").
    """
    from sqlalchemy import func
    from database import User

    users = (
        db.query(User)
        .filter(User.is_active == True)
        .order_by(User.full_name.asc())
        .all()
    )

    # Tek seferde tüm performed_by sayımlarını al — N+1 yok
    counts_raw = (
        db.query(func.lower(Transaction.performed_by), func.count(Transaction.id))
        .filter(Transaction.performed_by.isnot(None))
        .group_by(func.lower(Transaction.performed_by))
        .all()
    )
    counts = {k: v for k, v in counts_raw}   # {lowercase_name: count}

    out = []
    for u in users:
        candidates = [c.lower() for c in (u.full_name, u.username) if c]
        tx_count = sum(counts.get(c, 0) for c in candidates)
        out.append({
            "id":        u.id,
            "username":  u.username,
            "full_name": u.full_name,
            "role":      u.role,
            "tx_count":  tx_count,
        })
    return out


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

    # Case-insensitive eşleşme — eski veriler "Doğukan YALÇINKAYA" gibi
    # büyük harf, yeni veriler "Doğukan Yalçınkaya" olabilir; ikisini de yakala.
    from sqlalchemy import func
    candidates_lower = [c.lower() for c in (user.full_name, user.username) if c]

    rows = (
        db.query(Transaction)
        .filter(
            func.lower(Transaction.performed_by).in_(candidates_lower),
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
                "timestamp":        to_tr(t.timestamp).strftime("%d.%m.%Y %H:%M") if t.timestamp else "—",
            }
            for t in rows
        ],
    }


# ─── Excel Import Endpoints (classic templated upload) ──────────────────────

# ─── Excel upload guards ────────────────────────────────────────────────────
# Lab dosyaları büyük olabiliyor (Sayfa10 + sayfa-sayfa hammadde sayımları),
# 100 MB tavanı pratik; üstüne çıkarsa OOM riskine girer.
MAX_EXCEL_BYTES = 100 * 1024 * 1024     # 100 MB

# XLSX = ZIP container; ZIP header magic bytes "PK\x03\x04" (veya bazen
# "PK\x05\x06" boş arşiv).  Saldırgan .xlsx uzantılı text/JS/HTML gönderirse
# openpyxl açmaya çalışmadan reddederiz.
_XLSX_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


async def _read_xlsx_safely(file) -> bytes:
    """
    Upload'ı validate edip bytes döner.  Hata olursa HTTPException raise eder.
    Sıra: uzantı → boyut (Content-Length spoof'a karşı stream-based) → magic bytes.
    """
    from fastapi import HTTPException

    # 1) Uzantı (hızlı reddetme — yine de zorunlu değil ama UX'i iyi)
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Yalnızca .xlsx dosyaları desteklenir.")

    # 2) Stream-based read — saldırgan Content-Length'i yalan söylese bile
    #     biz okurken anlık byte sayısını sayıyoruz, MAX_EXCEL_BYTES'i geçince keseriz
    contents = bytearray()
    while True:
        chunk = await file.read(1024 * 1024)   # 1 MB
        if not chunk:
            break
        contents.extend(chunk)
        if len(contents) > MAX_EXCEL_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Dosya çok büyük (max {MAX_EXCEL_BYTES // (1024*1024)} MB)."
            )

    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Yüklenen dosya boş.")

    # 3) Magic bytes — uzantısı .xlsx ama içeriği farklıysa reddet
    if not any(bytes(contents[:4]).startswith(m) for m in _XLSX_MAGIC):
        raise HTTPException(
            status_code=400,
            detail="Dosya gerçekten bir Excel (.xlsx) değil. Header doğrulaması başarısız."
        )

    return bytes(contents)


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
    current_user: dict = Depends(get_current_user),
    _: dict = Depends(require_permission("items", "import")),
):
    """Excel (.xlsx) dosyasından toplu ürün içe aktarma — SKU bazlı upsert."""
    import io, datetime as _dt
    actor = current_user.get("full_name") or current_user.get("username") or "Excel Import"

    # Uzantı + boyut + magic-bytes doğrulamayla güvenli okuma
    contents = await _read_xlsx_safely(file)

    # ── Pandas ile oku ──────────────────────────────────────────────────────
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
                        received_by=actor,
                    ))
                    db.add(Transaction(
                        item_id=new_item.id, lot_number=lot,
                        transaction_type="Input", quantity=stock,
                        notes=f"Excel içe aktarım — SKU: {sku}",
                        performed_by=actor,
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

    # Uzantı + boyut + magic-bytes doğrulama (HTTPException döner — global handler yakalar)
    contents = await _read_xlsx_safely(file)
    try:
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
