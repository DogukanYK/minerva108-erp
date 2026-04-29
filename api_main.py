from fastapi import FastAPI, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List

from database import get_db, init_db, User, Item, Supplier, Recipe, RecipeIngredient, ProductionHistory, Inventory, Transaction
from core.auth import (
    verify_password, create_access_token,
    set_auth_cookie, decode_token, get_current_user, require_role
)

app = FastAPI(title="Minerva108 ERP", version="1.0.0", docs_url="/api/docs")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def startup_event():
    init_db()


# ─── Role helpers ───────────────────────────────────────────────────────────

_ROLE_LABELS = {
    "SuperAdmin": "Süper Yönetici",
    "Manager":    "Yönetici",
    "LabLead":    "Lab Sorumlusu",
    "LabTech":    "Lab Teknisyeni",
}

_CAN_DELETE = ["SuperAdmin", "LabLead"]
_CAN_EDIT_RECIPES = ["SuperAdmin", "LabLead"]


def _page_ctx(request: Request, payload: dict) -> dict:
    role = payload.get("role", "")
    return {
        "request":    request,
        "username":   payload.get("username"),
        "full_name":  payload.get("full_name"),
        "role":       role,
        "role_label": _ROLE_LABELS.get(role, role),
    }


# ─── Schemas ────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: Optional[bool] = False


class ItemCreateRequest(BaseModel):
    name: str
    category: Optional[str] = None
    unit: str = "adet"
    min_stock: Optional[float] = 0.0


class BulkDeleteRequest(BaseModel):
    item_ids: List[int]


class SupplierCreateRequest(BaseModel):
    name: str
    contact_person: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None


class SupplierBulkDeleteRequest(BaseModel):
    supplier_ids: List[int]


class RecipeIngredientSchema(BaseModel):
    item_id: int
    quantity: float


class RecipeCreateRequest(BaseModel):
    name: str
    target_item_id: int
    expected_yield: float
    ingredients: List[RecipeIngredientSchema]


class ProductionCreateRequest(BaseModel):
    recipe_id: int
    produced_quantity: float


class QCActionRequest(BaseModel):
    notes: str
    status: str  # 'APPROVED' veya 'REJECTED'


class StockReceiveRequest(BaseModel):
    item_id: int
    supplier_id: Optional[int] = None
    lot_number: str
    expiry_date: Optional[str] = None
    quantity: float
    location: Optional[str] = None


# ─── Auth Endpoints ──────────────────────────────────────────────────────────

@app.post("/api/login")
def login(data: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = (
        db.query(User)
        .filter(User.username == data.username, User.is_active == True)
        .first()
    )
    if not user or not verify_password(data.password, user.password_hash):
        return JSONResponse(
            status_code=401,
            content={"detail": "Kullanıcı adı veya şifre hatalı."}
        )
    token = create_access_token(
        {"sub": str(user.id), "username": user.username, "full_name": user.full_name, "role": user.role},
        remember_me=data.remember_me,
    )
    set_auth_cookie(response, token, remember_me=data.remember_me)
    return {"message": "Giriş başarılı", "redirect": "/"}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("access_token")
    return {"message": "Çıkış yapıldı"}


# ─── Items Endpoints ─────────────────────────────────────────────────────────

@app.get("/api/items")
def list_items(db: Session = Depends(get_db)):
    items = db.query(Item).filter(Item.is_active == True).order_by(Item.id.desc()).all()
    return [
        {
            "id": i.id,
            "name": i.name,
            "category": i.category,
            "unit": i.unit,
            "min_stock_level": i.min_stock_level,
            "current_stock": i.current_stock,
            "created_at": i.created_at.strftime("%d.%m.%Y") if i.created_at else "",
        }
        for i in items
    ]


@app.post("/api/items", status_code=201)
def create_item(data: ItemCreateRequest, db: Session = Depends(get_db), _: dict = Depends(require_role(_CAN_DELETE))):
    item = Item(
        name=data.name,
        category=data.category,
        unit=data.unit,
        min_stock_level=data.min_stock or 0.0,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return {"id": item.id, "message": "Ürün başarıyla eklendi."}


@app.delete("/api/items/{item_id}")
def delete_item(item_id: int, db: Session = Depends(get_db), _: dict = Depends(require_role(_CAN_DELETE))):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        return JSONResponse(status_code=404, content={"detail": "Ürün bulunamadı."})
    db.delete(item)
    db.commit()
    return {"message": "Ürün silindi."}


@app.put("/api/items/{item_id}")
def update_item(item_id: int, data: ItemCreateRequest, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if not item:
        return JSONResponse(status_code=404, content={"detail": "Ürün bulunamadı."})
    item.name = data.name
    item.category = data.category
    item.unit = data.unit
    item.min_stock_level = data.min_stock or 0.0
    db.commit()
    return {"id": item.id, "message": "Ürün güncellendi."}


@app.post("/api/items/bulk-delete")
def bulk_delete_items(data: BulkDeleteRequest, db: Session = Depends(get_db), _: dict = Depends(require_role(_CAN_DELETE))):
    deleted = db.query(Item).filter(Item.id.in_(data.item_ids)).delete(synchronize_session=False)
    db.commit()
    return {"message": f"{deleted} ürün silindi."}


# ─── Suppliers Endpoints ─────────────────────────────────────────────────────

@app.get("/api/suppliers")
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
            "created_at": s.created_at.strftime("%d.%m.%Y") if s.created_at else "",
        }
        for s in rows
    ]


@app.post("/api/suppliers", status_code=201)
def create_supplier(data: SupplierCreateRequest, db: Session = Depends(get_db), _: dict = Depends(require_role(_CAN_DELETE))):
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


@app.delete("/api/suppliers/{supplier_id}")
def delete_supplier(supplier_id: int, db: Session = Depends(get_db), _: dict = Depends(require_role(_CAN_DELETE))):
    supplier = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if not supplier:
        return JSONResponse(status_code=404, content={"detail": "Tedarikçi bulunamadı."})
    db.delete(supplier)
    db.commit()
    return {"message": "Tedarikçi silindi."}


@app.post("/api/suppliers/bulk-delete")
def bulk_delete_suppliers(data: SupplierBulkDeleteRequest, db: Session = Depends(get_db), _: dict = Depends(require_role(_CAN_DELETE))):
    deleted = db.query(Supplier).filter(Supplier.id.in_(data.supplier_ids)).delete(synchronize_session=False)
    db.commit()
    return {"message": f"{deleted} tedarikçi silindi."}


# ─── Recipes Endpoints ───────────────────────────────────────────────────────

@app.get("/api/recipes")
def list_recipes(db: Session = Depends(get_db)):
    rows = db.query(Recipe).filter(Recipe.is_active == True).order_by(Recipe.id.desc()).all()
    result = []
    for r in rows:
        target = db.query(Item).filter(Item.id == r.output_quantity).first() if False else None
        # target_item stored via output_unit field repurposed — use dedicated join below
        ingredient_count = len(r.ingredients)
        result.append({
            "id": r.id,
            "name": r.name,
            "description": r.description,
            "expected_yield": r.output_quantity,
            "target_item_name": r.output_unit,
            "ingredient_count": ingredient_count,
            "created_at": r.created_at.strftime("%d.%m.%Y") if r.created_at else "",
        })
    return result


@app.post("/api/recipes", status_code=201)
def create_recipe(data: RecipeCreateRequest, db: Session = Depends(get_db), _: dict = Depends(require_role(_CAN_EDIT_RECIPES))):
    target_item = db.query(Item).filter(Item.id == data.target_item_id).first()
    if not target_item:
        return JSONResponse(status_code=404, content={"detail": "Hedef ürün bulunamadı."})
    try:
        recipe = Recipe(
            name=data.name,
            output_quantity=data.expected_yield,
            output_unit=target_item.unit or "adet",
            target_item_id=data.target_item_id,
            description=f"Hedef: {target_item.name}",
        )
        db.add(recipe)
        db.flush()  # recipe.id'yi al, commit etme
        for ing in data.ingredients:
            item = db.query(Item).filter(Item.id == ing.item_id).first()
            if not item:
                db.rollback()
                return JSONResponse(status_code=404, content={"detail": f"Hammadde ID {ing.item_id} bulunamadı."})
            db.add(RecipeIngredient(
                recipe_id=recipe.id,
                item_id=ing.item_id,
                quantity=ing.quantity,
                unit=item.unit,
            ))
        db.commit()
        db.refresh(recipe)
        return {"id": recipe.id, "message": "Reçete başarıyla oluşturuldu."}
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Reçete kaydedilemedi."})


@app.delete("/api/recipes/{recipe_id}")
def delete_recipe(recipe_id: int, db: Session = Depends(get_db), _: dict = Depends(require_role(_CAN_DELETE))):
    recipe = db.query(Recipe).filter(Recipe.id == recipe_id).first()
    if not recipe:
        return JSONResponse(status_code=404, content={"detail": "Reçete bulunamadı."})
    db.delete(recipe)   # cascade="all, delete-orphan" recipe_ingredients'ı siler
    db.commit()
    return {"message": "Reçete silindi."}


@app.get("/api/recipes/{recipe_id}")
def get_recipe_detail(recipe_id: int, db: Session = Depends(get_db)):
    recipe = db.query(Recipe).filter(Recipe.id == recipe_id).first()
    if not recipe:
        return JSONResponse(status_code=404, content={"detail": "Reçete bulunamadı."})
    target = db.query(Item).filter(Item.id == recipe.target_item_id).first() if recipe.target_item_id else None
    ingredients = []
    for ing in recipe.ingredients:
        item = db.query(Item).filter(Item.id == ing.item_id).first()
        ingredients.append({
            "item_id": ing.item_id,
            "item_name": item.name if item else "—",
            "quantity": ing.quantity,
            "unit": ing.unit or (item.unit if item else ""),
            "current_stock": item.current_stock if item else 0,
        })
    return {
        "id": recipe.id,
        "name": recipe.name,
        "expected_yield": recipe.output_quantity,
        "target_item_id": recipe.target_item_id,
        "target_item_name": target.name if target else recipe.description,
        "target_item_unit": target.unit if target else recipe.output_unit,
        "ingredients": ingredients,
    }


# ─── Production Endpoints ────────────────────────────────────────────────────

@app.get("/api/production")
def list_production_history(db: Session = Depends(get_db)):
    rows = db.query(ProductionHistory).order_by(ProductionHistory.id.desc()).limit(100).all()
    return [
        {
            "id": r.id,
            "recipe_name": r.recipe_name,
            "target_item_name": r.target_item_name,
            "produced_quantity": r.produced_quantity,
            "produced_at": r.produced_at.strftime("%d.%m.%Y %H:%M") if r.produced_at else "",
        }
        for r in rows
    ]


@app.post("/api/production", status_code=201)
def start_production(data: ProductionCreateRequest, db: Session = Depends(get_db)):
    recipe = db.query(Recipe).filter(Recipe.id == data.recipe_id).first()
    if not recipe:
        return JSONResponse(status_code=404, content={"detail": "Reçete bulunamadı."})
    if data.produced_quantity <= 0:
        return JSONResponse(status_code=400, content={"detail": "Üretim miktarı sıfırdan büyük olmalıdır."})

    multiplier = data.produced_quantity / recipe.output_quantity

    try:
        # Stok yeterliliğini kontrol et
        for ing in recipe.ingredients:
            item = db.query(Item).filter(Item.id == ing.item_id).with_for_update().first()
            if not item:
                db.rollback()
                return JSONResponse(status_code=404, content={"detail": f"Hammadde bulunamadı (ID: {ing.item_id})."})
            needed = round(ing.quantity * multiplier, 6)
            if item.current_stock < needed:
                db.rollback()
                return JSONResponse(status_code=400, content={
                    "detail": f"'{item.name}' için yeterli stok yok. "
                              f"Gereken: {needed} {item.unit}, Mevcut: {item.current_stock} {item.unit}"
                })

        # Hammadde stoklarını düş
        for ing in recipe.ingredients:
            item = db.query(Item).filter(Item.id == ing.item_id).first()
            item.current_stock = round(item.current_stock - (ing.quantity * multiplier), 6)

        # Üretilen lot'u QUARANTINE olarak inventory'e ekle (QC onayına kadar stok artmaz)
        if recipe.target_item_id:
            import datetime as _dt
            now = _dt.datetime.utcnow()
            lot_number = f"PRD-{now.strftime('%Y%m%d-%H%M%S')}"
            db.add(Inventory(
                item_id=recipe.target_item_id,
                lot_number=lot_number,
                quantity=data.produced_quantity,
                status="QUARANTINE",
            ))
            db.add(Transaction(
                item_id=recipe.target_item_id,
                lot_number=lot_number,
                transaction_type="Input",
                quantity=data.produced_quantity,
                notes=f"Üretim çıktısı — Reçete: {recipe.name}, Lot: {lot_number}",
            ))

        # Üretim kaydı
        db.add(ProductionHistory(
            recipe_id=recipe.id,
            recipe_name=recipe.name,
            target_item_id=recipe.target_item_id,
            target_item_name=recipe.target_item.name if recipe.target_item else recipe.description,
            produced_quantity=data.produced_quantity,
        ))

        db.commit()
        return {"message": f"Üretim tamamlandı. {data.produced_quantity} birim QC onayına gönderildi."}

    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Üretim sırasında hata oluştu, stoklar değiştirilmedi."})


# ─── Inventory / Receiving Endpoints ────────────────────────────────────────

@app.get("/api/inventory")
def list_inventory(db: Session = Depends(get_db)):
    rows = db.query(Inventory).order_by(Inventory.id.desc()).all()
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
            "created_at": r.created_at.strftime("%d.%m.%Y") if r.created_at else "",
        }
        for r in rows
    ]


@app.post("/api/inventory/receive", status_code=201)
def receive_stock(data: StockReceiveRequest, db: Session = Depends(get_db)):
    if data.quantity <= 0:
        return JSONResponse(status_code=400, content={"detail": "Miktar sıfırdan büyük olmalıdır."})

    item = db.query(Item).filter(Item.id == data.item_id).first()
    if not item:
        return JSONResponse(status_code=404, content={"detail": "Ürün bulunamadı."})

    try:
        # ── Upsert Logic (4.7) ──────────────────────────────────────────────
        existing = db.query(Inventory).filter(
            Inventory.item_id == data.item_id,
            Inventory.lot_number == data.lot_number,
        ).first()

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
        else:
            # Yeni satır — statü kesinlikle APPROVED
            db.add(Inventory(
                item_id=data.item_id,
                supplier_id=data.supplier_id,
                lot_number=data.lot_number,
                expiry_date=data.expiry_date,
                quantity=data.quantity,
                location=data.location,
                status="APPROVED",
            ))

        # ── Transaction kaydı (2.4) ─────────────────────────────────────────
        db.add(Transaction(
            item_id=data.item_id,
            lot_number=data.lot_number,
            transaction_type="Input",
            quantity=data.quantity,
            notes=f"Mal kabul — Lot: {data.lot_number}" + (f", Konum: {data.location}" if data.location else ""),
        ))

        # ── items.current_stock güncelle (üretim modülü ile uyum) ───────────
        item.current_stock = round(item.current_stock + data.quantity, 6)

        db.commit()
        return {"message": f"Mal kabul başarılı. {data.quantity} {item.unit} stoka eklendi."}

    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Mal kabul sırasında hata oluştu."})


# ─── QC Endpoints ────────────────────────────────────────────────────────────

@app.get("/api/qc/quarantine")
def list_quarantine(db: Session = Depends(get_db)):
    rows = db.query(Inventory).filter(Inventory.status == "QUARANTINE").order_by(Inventory.id.desc()).all()
    return [
        {
            "id": r.id,
            "item_name": r.item.name if r.item else "—",
            "item_id": r.item_id,
            "lot_number": r.lot_number,
            "quantity": r.quantity,
            "expiry_date": r.expiry_date or "—",
            "location": r.location or "—",
            "status": r.status,
            "created_at": r.created_at.strftime("%d.%m.%Y") if r.created_at else "",
        }
        for r in rows
    ]


@app.post("/api/qc/process/{inventory_id}")
def process_qc(inventory_id: int, data: QCActionRequest, db: Session = Depends(get_db)):
    if data.status not in ("APPROVED", "REJECTED"):
        return JSONResponse(status_code=400, content={"detail": "Geçersiz statü. 'APPROVED' veya 'REJECTED' olmalıdır."})

    inv = db.query(Inventory).filter(Inventory.id == inventory_id).first()
    if not inv:
        return JSONResponse(status_code=404, content={"detail": "Envanter kaydı bulunamadı."})
    if inv.status != "QUARANTINE":
        return JSONResponse(status_code=400, content={"detail": "Bu kayıt zaten karantinade değil."})

    try:
        inv.status = data.status
        inv.qc_notes = data.notes
        inv.updated_at = __import__("datetime").datetime.utcnow()

        # APPROVED ise items.current_stock'u artır; REJECTED ise stok değişmez
        if data.status == "APPROVED":
            approved_item = db.query(Item).filter(Item.id == inv.item_id).first()
            if approved_item:
                approved_item.current_stock = round(approved_item.current_stock + inv.quantity, 6)

        tx_type = "QC Approval" if data.status == "APPROVED" else "QC Rejection"
        db.add(Transaction(
            item_id=inv.item_id,
            lot_number=inv.lot_number,
            transaction_type=tx_type,
            quantity=inv.quantity,
            notes=f"{tx_type} — Lot: {inv.lot_number}. Not: {data.notes}",
        ))

        db.commit()
        label = "Onaylandı" if data.status == "APPROVED" else "Reddedildi"
        return {"message": f"Lot #{inv.lot_number} başarıyla {label}."}
    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "QC işlemi sırasında hata oluştu."})


# ─── Ledger Endpoints ────────────────────────────────────────────────────────

@app.get("/api/inventory/summary")
def inventory_summary(db: Session = Depends(get_db)):
    from sqlalchemy import func
    rows = (
        db.query(
            Item.id,
            Item.name,
            Item.category,
            Item.unit,
            func.coalesce(func.sum(Inventory.quantity), 0).label("total_stock"),
        )
        .outerjoin(Inventory, (Inventory.item_id == Item.id) & (Inventory.status == "APPROVED"))
        .filter(Item.is_active == True)
        .group_by(Item.id)
        .order_by(Item.category, Item.name)
        .all()
    )
    return [
        {
            "item_id": r.id,
            "name": r.name,
            "category": r.category or "Diğer",
            "unit": r.unit,
            "total_stock": round(float(r.total_stock), 4),
        }
        for r in rows
    ]


@app.get("/api/transactions")
def list_transactions(db: Session = Depends(get_db)):
    rows = (
        db.query(Transaction)
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
            "timestamp": r.timestamp.strftime("%d.%m.%Y %H:%M") if r.timestamp else "",
        }
        for r in rows
    ]


# ─── Reports Endpoints ───────────────────────────────────────────────────────

@app.get("/api/reports/top-usage")
def report_top_usage(db: Session = Depends(get_db)):
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


@app.get("/api/reports/production-trends")
def report_production_trends(db: Session = Depends(get_db)):
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


@app.get("/api/reports/low-stock-alert")
def report_low_stock_alert(db: Session = Depends(get_db)):
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

@app.get("/api/dashboard/stats")
def dashboard_stats(db: Session = Depends(get_db)):
    total_items      = db.query(Item).filter(Item.is_active == True).count()
    total_suppliers  = db.query(Supplier).filter(Supplier.is_active == True).count()
    total_recipes    = db.query(Recipe).filter(Recipe.is_active == True).count()
    critical_stock   = db.query(Item).filter(
        Item.is_active == True,
        Item.min_stock_level > 0,
        Item.current_stock <= Item.min_stock_level,
    ).count()
    recent = (
        db.query(ProductionHistory)
        .order_by(ProductionHistory.id.desc())
        .limit(5).all()
    )
    return {
        "total_items": total_items,
        "total_suppliers": total_suppliers,
        "total_recipes": total_recipes,
        "critical_stock_count": critical_stock,
        "recent_productions": [
            {
                "recipe_name": r.recipe_name,
                "target_item_name": r.target_item_name,
                "produced_quantity": r.produced_quantity,
                "produced_at": r.produced_at.strftime("%d.%m.%Y %H:%M") if r.produced_at else "",
            }
            for r in recent
        ],
    }


# ─── Page Routes ─────────────────────────────────────────────────────────────

def _get_user_context(request: Request) -> Optional[dict]:
    token = request.cookies.get("access_token")
    if not token:
        return None
    return decode_token(token)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    token = request.cookies.get("access_token")
    if token and decode_token(token):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("index.html", _page_ctx(request, payload))


@app.get("/items", response_class=HTMLResponse)
def items_page(request: Request):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("items.html", _page_ctx(request, payload))


@app.get("/suppliers", response_class=HTMLResponse)
def suppliers_page(request: Request):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("suppliers.html", _page_ctx(request, payload))


@app.get("/recipes", response_class=HTMLResponse)
def recipes_page(request: Request):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("recipes.html", _page_ctx(request, payload))


@app.get("/production", response_class=HTMLResponse)
def production_page(request: Request):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("production.html", _page_ctx(request, payload))


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("reports.html", _page_ctx(request, payload))


@app.get("/ledger", response_class=HTMLResponse)
def ledger_page(request: Request):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("ledger.html", _page_ctx(request, payload))


@app.get("/qc", response_class=HTMLResponse)
def qc_page(request: Request):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("qc.html", _page_ctx(request, payload))


@app.get("/receiving", response_class=HTMLResponse)
def receiving_page(request: Request):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("receiving.html", _page_ctx(request, payload))
