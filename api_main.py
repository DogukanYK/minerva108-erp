from fastapi import FastAPI, Depends, Request, Response, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from database import get_db, init_db, User, Item, Supplier, Recipe, RecipeIngredient, ProductionHistory, Inventory, Transaction, Quotation, QuotationItem
from core.auth import (
    verify_password, create_access_token,
    set_auth_cookie, decode_token, get_current_user, require_role
)

app = FastAPI(title="Minerva108 ERP", version="1.0.0", docs_url="/api/docs")

# ─── Rate Limiter (P0 / brute-force protection) ────────────────────────────
# Uses client IP (via X-Forwarded-For when uvicorn is started with --proxy-headers).
# In-memory storage — fine for single-process uvicorn; if we ever scale to
# multiple workers, swap to Redis-backed storage.
limiter = Limiter(key_func=get_remote_address, default_limits=[])
app.state.limiter = limiter

@app.exception_handler(RateLimitExceeded)
def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """Friendly Turkish error when a client hits the rate limit."""
    retry_after = getattr(exc, "retry_after", 60)
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Çok fazla deneme. Lütfen biraz bekleyip tekrar deneyin.",
            "retry_after_seconds": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )

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

_CAN_DELETE       = ["SuperAdmin", "LabLead"]
_CAN_EDIT_RECIPES = ["SuperAdmin", "LabLead"]
_SUPERADMIN_ONLY  = ["SuperAdmin"]
_FINANCE_ROLES    = ["SuperAdmin", "Manager"]   # Costs / margins / quotations / currency rates

_VALID_ROLES = {"SuperAdmin", "Manager", "LabLead", "LabTech", "Staff"}


def _can_see_finance(payload_or_role) -> bool:
    """True if the user's role is allowed to view financial data."""
    role = payload_or_role.get("role") if isinstance(payload_or_role, dict) else payload_or_role
    return role in _FINANCE_ROLES


# ─── RBAC 2.0 — Granular Permission Matrix (Phase 4 / Task 2) ───────────────

# Single source of truth for the permission tree. Adding a new permission here
# automatically makes it appear in the admin UI matrix.
PERMISSION_CATEGORIES = {
    "items":      ["view", "create", "edit", "delete", "import"],
    "recipes":    ["view", "create", "edit", "delete"],
    "inventory":  ["view", "receive", "delete"],
    "production": ["view", "create"],
    "qc":         ["view", "approve"],
    "finance":    ["view"],            # Cost prices, recipe BOM costs, margins
    "b2b":        ["view", "create", "confirm"],
    "reports":    ["view"],
    "admin":      ["manage_users", "import_excel", "view_audit"],
}

# Default permission set per role — used when a user's `permissions` JSON is null.
# SuperAdmin is special-cased (always allowed) so doesn't need an entry.
_DEFAULT_PERMISSIONS = {
    "Manager": {
        "items":      {"view": True,  "create": True,  "edit": True,  "delete": False, "import": True},
        "recipes":    {"view": True,  "create": False, "edit": False, "delete": False},
        "inventory":  {"view": True,  "receive": True, "delete": False},
        "production": {"view": True,  "create": True},
        "qc":         {"view": True,  "approve": True},
        "finance":    {"view": True},
        "b2b":        {"view": True,  "create": True,  "confirm": True},
        "reports":    {"view": True},
        "admin":      {"manage_users": False, "import_excel": False, "view_audit": True},
    },
    "LabLead": {
        "items":      {"view": True,  "create": True,  "edit": True,  "delete": True,  "import": True},
        "recipes":    {"view": True,  "create": True,  "edit": True,  "delete": True},
        "inventory":  {"view": True,  "receive": True, "delete": True},
        "production": {"view": True,  "create": True},
        "qc":         {"view": True,  "approve": True},
        "finance":    {"view": False},
        "b2b":        {"view": False, "create": False, "confirm": False},
        "reports":    {"view": True},
        "admin":      {"manage_users": False, "import_excel": False, "view_audit": False},
    },
    "LabTech": {
        "items":      {"view": True,  "create": True,  "edit": False, "delete": False, "import": False},
        "recipes":    {"view": True,  "create": False, "edit": False, "delete": False},
        "inventory":  {"view": True,  "receive": True, "delete": False},
        "production": {"view": True,  "create": True},
        "qc":         {"view": True,  "approve": False},
        "finance":    {"view": False},
        "b2b":        {"view": False, "create": False, "confirm": False},
        "reports":    {"view": True},
        "admin":      {"manage_users": False, "import_excel": False, "view_audit": False},
    },
    "Staff": {
        # Read-only baseline
        "items":      {"view": True,  "create": False, "edit": False, "delete": False, "import": False},
        "recipes":    {"view": True,  "create": False, "edit": False, "delete": False},
        "inventory":  {"view": True,  "receive": False, "delete": False},
        "production": {"view": True,  "create": False},
        "qc":         {"view": True,  "approve": False},
        "finance":    {"view": False},
        "b2b":        {"view": False, "create": False, "confirm": False},
        "reports":    {"view": True},
        "admin":      {"manage_users": False, "import_excel": False, "view_audit": False},
    },
}


def _resolve_permissions(user: User) -> dict:
    """
    Compute the EFFECTIVE permission set for a user. Order of precedence:
      1) SuperAdmin → all True (no override possible)
      2) user.permissions JSON if set → that's the source of truth
      3) Otherwise, fall back to _DEFAULT_PERMISSIONS[role]
    """
    import json as _json
    if user.role == "SuperAdmin":
        return {cat: {act: True for act in acts} for cat, acts in PERMISSION_CATEGORIES.items()}
    if user.permissions:
        try:
            return _json.loads(user.permissions)
        except Exception:
            pass
    return _DEFAULT_PERMISSIONS.get(user.role, _DEFAULT_PERMISSIONS["Staff"])


def _has_permission(user: User, category: str, action: str) -> bool:
    perms = _resolve_permissions(user)
    return bool(perms.get(category, {}).get(action, False))


def require_permission(category: str, action: str):
    """
    FastAPI dependency factory. Raises 403 if the current user lacks the permission.
    Usage:
        @app.delete(...)
        def endpoint(_: dict = Depends(require_permission("items", "delete"))):
            ...
    """
    def _dep(payload: dict = Depends(get_current_user), db: Session = Depends(get_db)):
        user = db.query(User).filter(User.id == int(payload.get("sub", 0))).first()
        if not user or not user.is_active:
            return JSONResponse(status_code=401, content={"detail": "Yetkisiz."})
        if not _has_permission(user, category, action):
            from fastapi import HTTPException
            raise HTTPException(
                status_code=403,
                detail=f"Yetersiz yetki: {category}.{action}",
            )
        return payload
    return _dep


def _page_ctx(request: Request, payload: dict) -> dict:
    role = payload.get("role", "")
    return {
        "request":    request,
        "username":   payload.get("username"),
        "full_name":  payload.get("full_name"),
        "role":       role,
        "role_label": _ROLE_LABELS.get(role, role),
        "user_id":    int(payload.get("sub", 0)),   # current user ID — used by admin UI self-protection
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
    min_stock:      Optional[float] = 0.0
    cost_price:     Optional[float] = 0.0
    parent_id:      Optional[int]   = None    # Variation hierarchy — null for parents/standalones
    variation_name: Optional[str]   = None    # e.g. "200ml" — required if parent_id set


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
    name: Optional[str] = None          # arka planda hedef ürün adına eşitlenir; göndermek isteğe bağlı
    target_item_id: int
    expected_yield: float
    waste_percentage: Optional[float] = 0.0   # % fire oranı
    ingredients: List[RecipeIngredientSchema]


class ProductionCreateRequest(BaseModel):
    recipe_id: int
    produced_quantity: float


class QCActionRequest(BaseModel):
    notes: str
    status: str  # 'APPROVED' veya 'REJECTED'


class QCFormRequest(BaseModel):
    """Digital QC form — full checklist + lab results + decision."""
    status:      str                        # 'APPROVED' or 'REJECTED'
    checklist:   dict                       # { "q01": "Evet", "q02": "Hayır", … }
    lab_ml:      Optional[float] = None     # Ürün içi ML
    lab_density: Optional[float] = None     # Yoğunluk
    lab_color:   Optional[str]   = None     # Renk
    notes:       Optional[str]   = ""       # Serbest notlar


class AdminUserCreateRequest(BaseModel):
    username:  str
    full_name: str
    password:  str
    role:      str = "LabTech"


class AdminUserUpdateRequest(BaseModel):
    full_name: Optional[str]  = None
    role:      Optional[str]  = None
    is_active: Optional[bool] = None


class PasswordResetRequest(BaseModel):
    new_password: str


class StockReceiveRequest(BaseModel):
    item_id: int
    supplier_id: Optional[int] = None
    lot_number: str
    expiry_date: Optional[str] = None
    quantity: float
    location: Optional[str] = None


# ─── Auth Endpoints ──────────────────────────────────────────────────────────

@app.post("/api/login")
@limiter.limit("5/minute")     # Max 5 login attempts per IP per minute → blunts credential stuffing
def login(request: Request, data: LoginRequest, response: Response, db: Session = Depends(get_db)):
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

    finance_ok = _can_see_finance(current_user)
    return [
        {
            "id":              i.id,
            "name":            i.name,
            "category":        i.category,
            "unit":            i.unit,
            "min_stock_level": i.min_stock_level,
            "current_stock":   i.current_stock,
            # ── Finance-gated: zeroed for lab roles (defense in depth vs. DevTools snooping) ──
            "cost_price":      round(i.cost_price or 0.0, 4) if finance_ok else 0.0,
            "parent_id":       i.parent_id,
            "parent_name":     name_by_id.get(i.parent_id) if i.parent_id else None,
            "variation_name":  i.variation_name,
            "child_count":     child_count.get(i.id, 0),
            "is_parent":       child_count.get(i.id, 0) > 0,
            "is_variation":    i.parent_id is not None,
            "created_at":      i.created_at.strftime("%d.%m.%Y") if i.created_at else "",
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


@app.post("/api/items", status_code=201)
def create_item(data: ItemCreateRequest, db: Session = Depends(get_db), _: dict = Depends(require_role(_CAN_DELETE))):
    err = _validate_variation(db, data.parent_id, data.variation_name)
    if err: return err

    item = Item(
        name=data.name,
        category=data.category,
        unit=data.unit,
        min_stock_level=data.min_stock  or 0.0,
        cost_price=data.cost_price or 0.0,
        parent_id=data.parent_id,
        variation_name=(data.variation_name.strip() if data.parent_id and data.variation_name else None),
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

    # If converting to a variation, ensure this item itself has no children (would orphan them)
    if data.parent_id is not None and item.parent_id is None:
        has_children = db.query(Item).filter(Item.parent_id == item_id).count() > 0
        if has_children:
            return JSONResponse(status_code=400, content={
                "detail": "Bu ürünün varyasyonları mevcut. Önce varyasyonları silip sonra dönüştürebilirsiniz."
            })

    err = _validate_variation(db, data.parent_id, data.variation_name, self_id=item_id)
    if err: return err

    item.name            = data.name
    item.category        = data.category
    item.unit            = data.unit
    item.min_stock_level = data.min_stock  or 0.0
    item.cost_price      = data.cost_price or 0.0
    item.parent_id       = data.parent_id
    item.variation_name  = (data.variation_name.strip() if data.parent_id and data.variation_name else None)
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

def _calc_recipe_costs(recipe: Recipe, db: Session) -> dict:
    """
    Compute BOM cost for a recipe.

    Rules:
      - Hammadde (raw material): gross_qty = net × (1 + waste% / 100)  [additive fire]
      - Ambalaj (packaging):     gross_qty = net                        [fire exempt]
    Returns total_cost, unit_cost, and per-ingredient cost dicts.
    """
    waste_factor = 1.0 + (recipe.waste_percentage or 0.0) / 100.0
    total_cost   = 0.0
    ingredient_costs = []

    for ing in recipe.ingredients:
        item = db.query(Item).filter(Item.id == ing.item_id).first()
        if not item:
            continue
        is_ambalaj = (item.category == "Ambalaj")
        factor     = 1.0 if is_ambalaj else waste_factor
        gross_qty  = round(ing.quantity * factor, 6)
        cost_price = round(item.cost_price or 0.0, 4)
        line_cost  = round(gross_qty * cost_price, 4)
        total_cost += line_cost
        ingredient_costs.append({
            "item_id":       ing.item_id,
            "item_name":     item.name,
            "unit":          ing.unit or item.unit or "",
            "current_stock": item.current_stock,
            "quantity":      ing.quantity,          # net (recipe spec)
            "gross_qty":     gross_qty,              # actual stock consumption
            "is_ambalaj":    is_ambalaj,
            "cost_price":    cost_price,
            "line_cost":     line_cost,
        })

    total_cost = round(total_cost, 4)
    unit_cost  = round(total_cost / (recipe.output_quantity or 1.0), 6)
    return {
        "total_cost":       total_cost,
        "unit_cost":        unit_cost,
        "ingredient_costs": ingredient_costs,
    }


@app.get("/api/recipes")
def list_recipes(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    rows = db.query(Recipe).filter(Recipe.is_active == True).order_by(Recipe.id.desc()).all()
    finance_ok = _can_see_finance(current_user)
    result = []
    for r in rows:
        target = db.query(Item).filter(Item.id == r.target_item_id).first() if r.target_item_id else None
        costs  = _calc_recipe_costs(r, db) if finance_ok else {"total_cost": 0.0, "unit_cost": 0.0, "ingredient_costs": []}
        result.append({
            "id":               r.id,
            "name":             r.name,
            "description":      r.description,
            "expected_yield":   r.output_quantity,
            "waste_percentage": round(r.waste_percentage or 0.0, 2),
            "target_item_id":   r.target_item_id,
            "target_item_name": target.name if target else (r.description or r.name),
            "target_item_unit": target.unit if target else (r.output_unit or ""),
            "ingredient_count": len(r.ingredients),
            "total_cost":       costs["total_cost"],
            "unit_cost":        costs["unit_cost"],
            "created_at":       r.created_at.strftime("%d.%m.%Y") if r.created_at else "",
        })
    return result


@app.post("/api/recipes", status_code=201)
def create_recipe(data: RecipeCreateRequest, db: Session = Depends(get_db), _: dict = Depends(require_role(_CAN_EDIT_RECIPES))):
    target_item = db.query(Item).filter(Item.id == data.target_item_id).first()
    if not target_item:
        return JSONResponse(status_code=404, content={"detail": "Hedef ürün bulunamadı."})

    # Block abstract-parent recipes — only standalones or variations may have recipes
    has_children = db.query(Item).filter(Item.parent_id == target_item.id).count() > 0
    if has_children:
        return JSONResponse(status_code=400, content={
            "detail": (f"'{target_item.name}' bir ana üründür ve varyasyonları vardır. "
                       "Reçete varyasyonlara (örn: 200ml, 500ml) ayrı ayrı tanımlanmalıdır.")
        })

    try:
        recipe = Recipe(
            name=target_item.name,          # her zaman hedef ürünün adına eşitlenir
            output_quantity=data.expected_yield,
            output_unit=target_item.unit or "adet",
            target_item_id=data.target_item_id,
            waste_percentage=round(data.waste_percentage or 0.0, 4),
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


@app.put("/api/recipes/{recipe_id}")
def update_recipe(
    recipe_id: int,
    data: RecipeCreateRequest,
    db: Session = Depends(get_db),
    _: dict = Depends(require_role(_CAN_EDIT_RECIPES)),
):
    recipe = db.query(Recipe).filter(Recipe.id == recipe_id).first()
    if not recipe:
        return JSONResponse(status_code=404, content={"detail": "Reçete bulunamadı."})

    target_item = db.query(Item).filter(Item.id == data.target_item_id).first()
    if not target_item:
        return JSONResponse(status_code=404, content={"detail": "Hedef ürün bulunamadı."})

    # Block recipes on abstract parents (variations only)
    has_children = db.query(Item).filter(Item.parent_id == target_item.id).count() > 0
    if has_children:
        return JSONResponse(status_code=400, content={
            "detail": (f"'{target_item.name}' bir ana üründür. "
                       "Reçete varyasyonlara (örn: 200ml, 500ml) ayrı tanımlanmalıdır.")
        })

    try:
        # Update recipe-level fields
        recipe.name             = target_item.name
        recipe.output_quantity  = data.expected_yield
        recipe.output_unit      = target_item.unit or "adet"
        recipe.target_item_id   = data.target_item_id
        recipe.waste_percentage = round(data.waste_percentage or 0.0, 4)
        recipe.description      = f"Hedef: {target_item.name}"

        # Replace ingredients: delete old, add new
        db.query(RecipeIngredient).filter(
            RecipeIngredient.recipe_id == recipe_id
        ).delete()
        db.flush()

        for ing in data.ingredients:
            item = db.query(Item).filter(Item.id == ing.item_id).first()
            if not item:
                db.rollback()
                return JSONResponse(status_code=404, content={
                    "detail": f"Hammadde ID {ing.item_id} bulunamadı."
                })
            db.add(RecipeIngredient(
                recipe_id=recipe.id,
                item_id=ing.item_id,
                quantity=ing.quantity,
                unit=item.unit,
            ))

        db.commit()
        return {"id": recipe.id, "message": "Reçete güncellendi."}
    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Reçete güncellenemedi."})


@app.get("/api/recipes/{recipe_id}")
def get_recipe_detail(
    recipe_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    recipe = db.query(Recipe).filter(Recipe.id == recipe_id).first()
    if not recipe:
        return JSONResponse(status_code=404, content={"detail": "Reçete bulunamadı."})
    target = db.query(Item).filter(Item.id == recipe.target_item_id).first() if recipe.target_item_id else None
    costs  = _calc_recipe_costs(recipe, db)

    # ── Strip cost fields for non-finance users (defense in depth) ──
    if not _can_see_finance(current_user):
        ingredients_redacted = []
        for ic in costs["ingredient_costs"]:
            ic2 = dict(ic)
            ic2["cost_price"] = 0.0
            ic2["line_cost"]  = 0.0
            ingredients_redacted.append(ic2)
        total_cost = 0.0
        unit_cost  = 0.0
        ingredients = ingredients_redacted
    else:
        total_cost  = costs["total_cost"]
        unit_cost   = costs["unit_cost"]
        ingredients = costs["ingredient_costs"]

    return {
        "id":               recipe.id,
        "name":             recipe.name,
        "expected_yield":   recipe.output_quantity,
        "waste_percentage": round(recipe.waste_percentage or 0.0, 2),
        "target_item_id":   recipe.target_item_id,
        "target_item_name": target.name if target else recipe.description,
        "target_item_unit": target.unit if target else recipe.output_unit,
        "total_cost":       total_cost,
        "unit_cost":        unit_cost,
        "ingredients":      ingredients,
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
def start_production(
    data: ProductionCreateRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    recipe = db.query(Recipe).filter(Recipe.id == data.recipe_id).first()
    if not recipe:
        return JSONResponse(status_code=404, content={"detail": "Reçete bulunamadı."})
    if data.produced_quantity <= 0:
        return JSONResponse(status_code=400, content={"detail": "Üretim miktarı sıfırdan büyük olmalıdır."})

    actor = current_user.get("full_name") or current_user.get("username") or "—"
    multiplier = data.produced_quantity / recipe.output_quantity

    # ── Brüt Girdi Hesabı ──────────────────────────────────────────────────
    # Fire (waste) EKLEMELI çalışır: brüt_girdi = net_miktar × (1 + fire% / 100)
    # Örnek: 50 ml hammadde + %10 fire = 55 ml stoktan düşülür.
    # Ambalaj bileşenlerine fire uygulanmaz (reçete mantığıyla tutarlı).
    waste_factor = 1.0 + (recipe.waste_percentage or 0.0) / 100.0

    try:
        # ── Önce tüm brüt miktarları hesapla ve stok kontrolü yap ──────────
        ing_plan = []   # (ing, item, gross_qty) üçlüsü
        for ing in recipe.ingredients:
            item = db.query(Item).filter(Item.id == ing.item_id).with_for_update().first()
            if not item:
                db.rollback()
                return JSONResponse(status_code=404, content={"detail": f"Hammadde bulunamadı (ID: {ing.item_id})."})
            is_ambalaj = (item.category == "Ambalaj")
            factor     = 1.0 if is_ambalaj else waste_factor   # ambalaja fire uygulanmaz
            gross_qty  = round(ing.quantity * multiplier * factor, 6)
            ing_plan.append((ing, item, gross_qty, is_ambalaj))

        for ing, item, gross_qty, is_ambalaj in ing_plan:
            if item.current_stock < gross_qty:
                db.rollback()
                fire_note = "" if is_ambalaj else f" (%{recipe.waste_percentage or 0} fire dahil)"
                return JSONResponse(status_code=400, content={
                    "detail": f"'{item.name}' için yeterli stok yok. "
                              f"Gereken: {gross_qty} {item.unit}{fire_note}, "
                              f"Mevcut: {item.current_stock} {item.unit}"
                })

        # ── Lot numarası şimdiden üret — tüm transaction notlarına stamp atılır
        import datetime as _dt
        now = _dt.datetime.utcnow()
        produced_lot = f"PRD-{now.strftime('%Y%m%d-%H%M%S')}"

        # ── Stok düş + Output transaction kaydet ───────────────────────────
        for ing, item, gross_qty, is_ambalaj in ing_plan:
            item.current_stock = round(item.current_stock - gross_qty, 6)
            fire_note = (
                f" | %{recipe.waste_percentage or 0} fire dahil, brüt girdi"
                if not is_ambalaj and (recipe.waste_percentage or 0) > 0
                else ""
            )
            db.add(Transaction(
                item_id=ing.item_id,
                transaction_type="Output",
                quantity=gross_qty,
                notes=f"Üretim tüketimi — Reçete: {recipe.name}{fire_note} | Üretim Lot: {produced_lot}",
                performed_by=actor,
            ))

        # Üretilen lot'u QUARANTINE olarak inventory'e ekle (QC onayına kadar stok artmaz)
        if recipe.target_item_id:
            db.add(Inventory(
                item_id=recipe.target_item_id,
                lot_number=produced_lot,
                quantity=data.produced_quantity,
                status="QUARANTINE",
                received_by=actor,                  # Üretim çıktısını "alan" da üretici
            ))
            db.add(Transaction(
                item_id=recipe.target_item_id,
                lot_number=produced_lot,
                transaction_type="Input",
                quantity=data.produced_quantity,
                notes=f"Üretim çıktısı — Reçete: {recipe.name}, Lot: {produced_lot}",
                performed_by=actor,
            ))

        # Üretim kaydı
        db.add(ProductionHistory(
            recipe_id=recipe.id,
            recipe_name=recipe.name,
            target_item_id=recipe.target_item_id,
            target_item_name=recipe.target_item.name if recipe.target_item else recipe.description,
            produced_quantity=data.produced_quantity,
            produced_by=actor,                       # Audit
            lot_number=produced_lot if recipe.target_item_id else None,
        ))

        db.commit()
        return {
            "message": f"Üretim tamamlandı. {data.produced_quantity} birim QC onayına gönderildi.",
            "lot_number": produced_lot if recipe.target_item_id else None,
        }

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
            db.add(Inventory(
                item_id=data.item_id,
                supplier_id=data.supplier_id,
                lot_number=data.lot_number,
                expiry_date=data.expiry_date,
                quantity=data.quantity,
                location=data.location,
                status="APPROVED",
                received_by=actor,                      # Audit trail
            ))

        # ── Transaction kaydı (2.4) ─────────────────────────────────────────
        db.add(Transaction(
            item_id=data.item_id,
            lot_number=data.lot_number,
            transaction_type="Input",
            quantity=data.quantity,
            notes=f"Mal kabul — Lot: {data.lot_number}" + (f", Konum: {data.location}" if data.location else ""),
            performed_by=actor,                          # Audit trail
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
            "id":           r.id,
            "item_name":    r.item.name if r.item else "—",
            "item_id":      r.item_id,
            "item_unit":    r.item.unit if r.item else "",
            "lot_number":   r.lot_number,
            "quantity":     r.quantity,
            "expiry_date":  r.expiry_date or "—",
            "location":     r.location or "—",
            "status":       r.status,
            "created_at":   r.created_at.strftime("%d.%m.%Y") if r.created_at else "",
        }
        for r in rows
    ]


@app.post("/api/qc/process/{inventory_id}")
def process_qc(
    inventory_id: int,
    data: QCActionRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    if data.status not in ("APPROVED", "REJECTED"):
        return JSONResponse(status_code=400, content={"detail": "Geçersiz statü. 'APPROVED' veya 'REJECTED' olmalıdır."})

    inv = db.query(Inventory).filter(Inventory.id == inventory_id).first()
    if not inv:
        return JSONResponse(status_code=404, content={"detail": "Envanter kaydı bulunamadı."})
    if inv.status != "QUARANTINE":
        return JSONResponse(status_code=400, content={"detail": "Bu kayıt zaten karantinade değil."})

    actor = current_user.get("full_name") or current_user.get("username") or "—"

    try:
        inv.status         = data.status
        inv.qc_notes       = data.notes
        inv.qc_approved_by = actor
        inv.updated_at     = __import__("datetime").datetime.utcnow()

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
            performed_by=actor,
        ))

        db.commit()
        label = "Onaylandı" if data.status == "APPROVED" else "Reddedildi"
        return {"message": f"Lot #{inv.lot_number} başarıyla {label}."}
    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "QC işlemi sırasında hata oluştu."})


@app.post("/api/inventory/{inventory_id}/qc-approve")
def qc_approve_form(
    inventory_id: int,
    data: QCFormRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(["SuperAdmin", "Manager", "LabLead", "LabTech"])),
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

    inv = db.query(Inventory).filter(Inventory.id == inventory_id).first()
    if not inv:
        return JSONResponse(status_code=404, content={"detail": "Envanter kaydı bulunamadı."})
    if inv.status != "QUARANTINE":
        return JSONResponse(status_code=400, content={"detail": "Bu lot zaten işlenmiş — tekrar değiştirilemez."})

    if not data.checklist:
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
        inv.updated_at     = _dt.utcnow()

        label = "Onaylandı ✓" if data.status == "APPROVED" else "Reddedildi ✗"

        # APPROVED → add lot quantity to live stock
        if data.status == "APPROVED":
            approved_item = db.query(Item).filter(Item.id == inv.item_id).first()
            if approved_item:
                approved_item.current_stock = round(approved_item.current_stock + inv.quantity, 6)

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


# ─── Traceability / Audit Trail (Phase 2 / Task 9) ──────────────────────────

@app.get("/api/traceability/lot/{lot_number}")
def trace_lot(lot_number: str, db: Session = Depends(get_db), _: dict = Depends(get_current_user)):
    """
    Full genealogy tree for a lot. Resolves:
      • Lot identity (Inventory record + supplier)
      • Production record (if internally produced)
      • Ingredients consumed during that production (with their own supplier/expiry/received_by)
      • All transactions for this lot — chronological audit trail.
    """
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
            "created_at":     inv.created_at.strftime("%d.%m.%Y %H:%M") if inv.created_at else "",
            "updated_at":     inv.updated_at.strftime("%d.%m.%Y %H:%M") if inv.updated_at else "",
        }
    else:
        out["lot_info"] = None

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
            "produced_at":       prod.produced_at.strftime("%d.%m.%Y %H:%M") if prod.produced_at else "—",
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
            "timestamp":        t.timestamp.strftime("%d.%m.%Y %H:%M") if t.timestamp else "—",
            "performed_by":     t.performed_by or "—",
            "notes":            (t.notes or "")[:200],
        }
        for t in txs
    ]

    return out


def _import_dt():
    """Tiny helper — import datetime lazily without polluting module top level."""
    import datetime as _d
    return _d.datetime


@app.get("/api/traceability/expiring")
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


# ─── Currency Rates (Phase 3 / Task 2 — TCMB live feed) ─────────────────────

_rate_cache = {"data": None, "fetched_at": None}
_RATE_TTL_SECONDS = 3600   # 1 hour

# Last-known-good fallback (overwritten on first successful TCMB fetch)
_RATE_FALLBACK = {"USD": 34.50, "EUR": 37.20}


def _fetch_tcmb_rates():
    """
    Fetch today's USD/EUR forex selling rates from TCMB.
    Returns dict like {"USD": 34.61, "EUR": 37.25} on success, None on failure.
    Uses stdlib only — no extra deps.
    """
    import urllib.request
    import xml.etree.ElementTree as ET

    url = "https://www.tcmb.gov.tr/kurlar/today.xml"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Minerva108-ERP/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            xml_data = resp.read().decode("utf-8")

        root = ET.fromstring(xml_data)
        out = {}
        for currency in root.findall("Currency"):
            code = currency.get("CurrencyCode")
            if code in ("USD", "EUR"):
                node = currency.find("ForexSelling")
                if node is not None and node.text:
                    out[code] = float(node.text.strip())
        return out if {"USD", "EUR"}.issubset(out.keys()) else None
    except Exception:
        return None


@app.get("/api/currency-rates")
def get_currency_rates(_: dict = Depends(require_role(_FINANCE_ROLES))):
    """
    Live USD & EUR rates against TRY (TCMB ForexSelling).
    Cached 1 hour in-memory; falls back to last cache or hardcoded values on outage.

    Response:
      { source, USD, EUR, fetched_at, cached, stale?, warning? }
    All rates are TRY per 1 unit of foreign currency.
    e.g. usd_amount = try_amount / response.USD
    """
    from datetime import datetime as _dt
    now = _dt.utcnow()
    cached_data = _rate_cache.get("data")
    cached_at   = _rate_cache.get("fetched_at")

    # ── Serve fresh cache ───────────────────────────────────────────────
    if cached_data and cached_at and (now - cached_at).total_seconds() < _RATE_TTL_SECONDS:
        return {
            **cached_data,
            "fetched_at": cached_at.isoformat() + "Z",
            "cached": True,
        }

    # ── Try live fetch ──────────────────────────────────────────────────
    fresh = _fetch_tcmb_rates()
    if fresh and "USD" in fresh and "EUR" in fresh:
        data = {"source": "TCMB", "USD": fresh["USD"], "EUR": fresh["EUR"]}
        _rate_cache["data"]       = data
        _rate_cache["fetched_at"] = now
        return {**data, "fetched_at": now.isoformat() + "Z", "cached": False}

    # ── Fall back to stale cache if available ──────────────────────────
    if cached_data:
        return {
            **cached_data,
            "fetched_at": cached_at.isoformat() + "Z" if cached_at else None,
            "cached": True,
            "stale":  True,
            "warning": "TCMB'ye ulaşılamadı; önceki kur kullanılıyor.",
        }

    # ── Final hardcoded fallback ───────────────────────────────────────
    return {
        "source":     "fallback",
        "USD":        _RATE_FALLBACK["USD"],
        "EUR":        _RATE_FALLBACK["EUR"],
        "fetched_at": now.isoformat() + "Z",
        "cached":     False,
        "stale":      True,
        "warning":    "TCMB'ye ulaşılamadı; varsayılan kurlar kullanılıyor. Manuel doğrulama önerilir.",
    }


# ─── Quotations / Order Fulfillment (Phase 3 / Task 3) ──────────────────────

class QuotationLineRequest(BaseModel):
    item_id:            int
    quantity:           float
    unit_price_foreign: float
    unit_cost_try:      Optional[float] = None
    item_name_snapshot: Optional[str]   = None


class QuotationCreateRequest(BaseModel):
    quote_number:     str
    customer_name:    str
    customer_contact: Optional[str] = None
    customer_email:   Optional[str] = None
    customer_phone:   Optional[str] = None
    customer_address: Optional[str] = None
    customer_country: Optional[str] = None
    customer_vat:     Optional[str] = None

    currency:        str   = "TRY"   # USD / EUR / TRY
    exchange_rate:   Optional[float] = None
    subtotal_amount: float = 0.0
    tax_percentage:  float = 0.0
    tax_amount:      float = 0.0
    shipping_amount: float = 0.0
    total_amount:    float

    notes:      Optional[str] = None
    valid_days: int           = 30

    items: List[QuotationLineRequest]


def _serialize_quotation_summary(q: Quotation) -> dict:
    return {
        "id":               q.id,
        "quote_number":     q.quote_number,
        "customer_name":    q.customer_name,
        "customer_country": q.customer_country or "",
        "currency":         q.currency,
        "total_amount":     q.total_amount,
        "status":           q.status,
        "created_at":       q.created_at.strftime("%d.%m.%Y %H:%M") if q.created_at else "",
        "created_by":       q.created_by or "—",
        "confirmed_at":     q.confirmed_at.strftime("%d.%m.%Y %H:%M") if q.confirmed_at else None,
        "confirmed_by":     q.confirmed_by,
        "item_count":       len(q.items),
    }


def _serialize_quotation_full(q: Quotation) -> dict:
    return {
        **_serialize_quotation_summary(q),
        "customer_contact": q.customer_contact,
        "customer_email":   q.customer_email,
        "customer_phone":   q.customer_phone,
        "customer_address": q.customer_address,
        "customer_vat":     q.customer_vat,
        "exchange_rate":    q.exchange_rate,
        "subtotal_amount":  q.subtotal_amount,
        "tax_percentage":   q.tax_percentage,
        "tax_amount":       q.tax_amount,
        "shipping_amount":  q.shipping_amount,
        "notes":            q.notes,
        "valid_days":       q.valid_days,
        "items": [
            {
                "id":                 i.id,
                "item_id":            i.item_id,
                "item_name_snapshot": i.item_name_snapshot,
                "quantity":           i.quantity,
                "unit_price_foreign": i.unit_price_foreign,
                "unit_cost_try":      i.unit_cost_try,
                "line_total":         i.line_total,
            }
            for i in q.items
        ],
    }


@app.post("/api/quotations", status_code=201)
def create_quotation(
    data: QuotationCreateRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(_FINANCE_ROLES)),
):
    """Save a quotation as DRAFT. Validates quote_number uniqueness + at least one line."""
    actor = current_user.get("full_name") or current_user.get("username") or "—"

    if data.currency not in ("USD", "EUR", "TRY"):
        return JSONResponse(status_code=400, content={"detail": "Geçersiz para birimi (USD/EUR/TRY)."})
    if not data.items:
        return JSONResponse(status_code=400, content={"detail": "Teklifte en az bir kalem olmalıdır."})
    if not data.customer_name or not data.customer_name.strip():
        return JSONResponse(status_code=400, content={"detail": "Müşteri / şirket adı zorunludur."})

    if db.query(Quotation).filter(Quotation.quote_number == data.quote_number).first():
        return JSONResponse(status_code=400, content={
            "detail": f"Teklif numarası '{data.quote_number}' zaten kullanımda. Lütfen farklı bir numara giriniz."
        })

    try:
        q = Quotation(
            quote_number=data.quote_number.strip(),
            customer_name=data.customer_name.strip(),
            customer_contact=data.customer_contact,
            customer_email=data.customer_email,
            customer_phone=data.customer_phone,
            customer_address=data.customer_address,
            customer_country=data.customer_country,
            customer_vat=data.customer_vat,
            currency=data.currency,
            exchange_rate=data.exchange_rate,
            subtotal_amount=data.subtotal_amount,
            tax_percentage=data.tax_percentage,
            tax_amount=data.tax_amount,
            shipping_amount=data.shipping_amount,
            total_amount=data.total_amount,
            notes=data.notes,
            valid_days=data.valid_days,
            status="DRAFT",
            created_by=actor,
        )
        db.add(q)
        db.flush()  # Need q.id for items

        for line in data.items:
            line_total = round(line.quantity * line.unit_price_foreign, 4)
            db.add(QuotationItem(
                quotation_id=q.id,
                item_id=line.item_id,
                item_name_snapshot=line.item_name_snapshot,
                quantity=line.quantity,
                unit_price_foreign=line.unit_price_foreign,
                unit_cost_try=line.unit_cost_try,
                line_total=line_total,
            ))

        db.commit()
        db.refresh(q)
        return {
            "id":           q.id,
            "quote_number": q.quote_number,
            "message":      f"Teklif #{q.quote_number} taslak olarak kaydedildi.",
        }
    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Teklif kaydedilemedi."})


@app.get("/api/quotations")
def list_quotations(
    db: Session = Depends(get_db),
    _: dict = Depends(require_role(_FINANCE_ROLES)),
    limit:  int = 50,
    status: Optional[str] = None,
):
    q = db.query(Quotation).order_by(Quotation.id.desc())
    if status:
        q = q.filter(Quotation.status == status.upper())
    return [_serialize_quotation_summary(r) for r in q.limit(limit).all()]


@app.get("/api/quotations/{quote_id}")
def get_quotation(quote_id: int, db: Session = Depends(get_db), _: dict = Depends(require_role(_FINANCE_ROLES))):
    q = db.query(Quotation).filter(Quotation.id == quote_id).first()
    if not q:
        return JSONResponse(status_code=404, content={"detail": "Teklif bulunamadı."})
    return _serialize_quotation_full(q)


@app.post("/api/quotations/{quote_id}/confirm")
def confirm_quotation(
    quote_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(_FINANCE_ROLES)),
):
    """
    The CRITICAL endpoint. Flips DRAFT → CONFIRMED and:
      1) Pre-flight stock check across ALL lines (collects every issue, no partial state)
      2) Atomically deducts current_stock for each line
      3) Logs an Output Transaction for each deduction (audit trail)
      4) Stamps confirmed_at + confirmed_by
    Idempotent: re-confirming a CONFIRMED quote is rejected.
    """
    actor = current_user.get("full_name") or current_user.get("username") or "—"

    quote = db.query(Quotation).filter(Quotation.id == quote_id).first()
    if not quote:
        return JSONResponse(status_code=404, content={"detail": "Teklif bulunamadı."})
    if quote.status != "DRAFT":
        return JSONResponse(status_code=400, content={
            "detail": f"Bu teklif zaten {quote.status} durumunda — tekrar onaylanamaz."
        })
    if not quote.items:
        return JSONResponse(status_code=400, content={"detail": "Teklifte hiç kalem yok."})

    # ── Stage 1: pre-flight stock check (no mutation yet) ──────────────────
    issues = []
    item_lookup = {}
    for line in quote.items:
        item = db.query(Item).filter(Item.id == line.item_id).with_for_update().first()
        if not item or item.is_active is False:
            issues.append(f"Ürün artık mevcut değil veya pasif (ID: {line.item_id})")
            continue
        # Defense in depth: block abstract parents (shouldn't happen via UI, but…)
        if db.query(Item).filter(Item.parent_id == item.id).count() > 0:
            issues.append(f"'{item.name}' bir ana üründür — varyasyon olarak siparişe alınamaz")
            continue
        if (item.current_stock or 0) < line.quantity:
            issues.append(
                f"'{item.name}' yetersiz stok: gereken {line.quantity} {item.unit}, "
                f"mevcut {item.current_stock} {item.unit}"
            )
            continue
        item_lookup[line.item_id] = item

    if issues:
        db.rollback()
        return JSONResponse(status_code=400, content={
            "detail": "Stok kontrolü başarısız — sipariş onaylanamadı.",
            "issues": issues,
        })

    # ── Stage 2: atomic deduction + transaction logging ───────────────────
    try:
        from datetime import datetime as _dt
        for line in quote.items:
            item = item_lookup[line.item_id]
            item.current_stock = round((item.current_stock or 0) - line.quantity, 6)
            db.add(Transaction(
                item_id=line.item_id,
                transaction_type="Output",
                quantity=line.quantity,
                notes=f"Export Sale: Quotation #{quote.quote_number} → {quote.customer_name}",
                performed_by=actor,
            ))

        quote.status       = "CONFIRMED"
        quote.confirmed_at = _dt.utcnow()
        quote.confirmed_by = actor
        db.commit()

        return {
            "message":         f"Sipariş onaylandı — Teklif #{quote.quote_number} stoktan düşüldü.",
            "quote_number":    quote.quote_number,
            "items_deducted":  len(quote.items),
            "confirmed_by":    actor,
        }
    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Onay sırasında hata oluştu — değişiklikler geri alındı."})


@app.delete("/api/quotations/{quote_id}")
def delete_quotation(
    quote_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_role(_FINANCE_ROLES)),
):
    """Delete a draft. CONFIRMED quotes are immutable for audit and cannot be deleted."""
    quote = db.query(Quotation).filter(Quotation.id == quote_id).first()
    if not quote:
        return JSONResponse(status_code=404, content={"detail": "Teklif bulunamadı."})
    if quote.status != "DRAFT":
        return JSONResponse(status_code=400, content={
            "detail": "Onaylanmış teklifler silinemez (denetim izi gereği)."
        })
    db.delete(quote)
    db.commit()
    return {"message": f"Taslak #{quote.quote_number} silindi."}


# ─── Excel Import Endpoints ──────────────────────────────────────────────────

_REQUIRED_COLS = {"Item_Name", "SKU", "Category", "Unit", "Stock", "Cost_Price", "Min_Stock_Level"}


@app.get("/api/import-items/template")
def download_import_template(_: dict = Depends(require_role(_CAN_DELETE))):
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


@app.post("/api/import-items")
async def import_items_from_excel(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: dict = Depends(require_role(_CAN_DELETE)),
):
    """Excel (.xlsx) dosyasından toplu ürün içe aktarma — SKU bazlı upsert."""
    import io, datetime as _dt

    # ── 1. Dosya uzantısı kontrolü ──────────────────────────────────────────
    if not file.filename.lower().endswith(".xlsx"):
        return JSONResponse(status_code=400, content={
            "detail": "Geçersiz dosya formatı. Yalnızca .xlsx dosyaları desteklenmektedir."
        })

    contents = await file.read()
    if len(contents) == 0:
        return JSONResponse(status_code=400, content={"detail": "Yüklenen dosya boş."})

    # ── 2. Pandas ile oku ───────────────────────────────────────────────────
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
                    ))
                    db.add(Transaction(
                        item_id=new_item.id, lot_number=lot,
                        transaction_type="Input", quantity=stock,
                        notes=f"Excel içe aktarım — SKU: {sku}",
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


# ─── Admin User Management Endpoints ────────────────────────────────────────

@app.get("/api/admin/users")
def admin_list_users(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(_SUPERADMIN_ONLY)),
):
    """Tüm kullanıcıları listele (şifre hash'i hariç)."""
    users = db.query(User).order_by(User.id.asc()).all()
    return [
        {
            "id":         u.id,
            "username":   u.username,
            "full_name":  u.full_name,
            "role":       u.role,
            "is_active":  u.is_active,
            "created_at": u.created_at.strftime("%d.%m.%Y") if u.created_at else "",
        }
        for u in users
    ]


@app.post("/api/admin/users", status_code=201)
def admin_create_user(
    data: AdminUserCreateRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(_SUPERADMIN_ONLY)),
):
    """Yeni kullanıcı hesabı oluştur."""
    if data.role not in _VALID_ROLES:
        return JSONResponse(status_code=422, content={"detail": f"Geçersiz rol: '{data.role}'."})
    if len(data.password) < 6:
        return JSONResponse(status_code=422, content={"detail": "Şifre en az 6 karakter olmalıdır."})

    existing = db.query(User).filter(User.username == data.username).first()
    if existing:
        return JSONResponse(status_code=409, content={
            "detail": f"'{data.username}' kullanıcı adı zaten kullanılıyor."
        })

    import bcrypt as _bcrypt
    pw_hash = _bcrypt.hashpw(data.password.encode(), _bcrypt.gensalt()).decode()
    user = User(
        username=data.username,
        full_name=data.full_name,
        password_hash=pw_hash,
        role=data.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "message": f"Kullanıcı '{data.username}' başarıyla oluşturuldu."}


@app.put("/api/admin/users/{user_id}")
def admin_update_user(
    user_id: int,
    data: AdminUserUpdateRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(_SUPERADMIN_ONLY)),
):
    """Kullanıcının bilgilerini, rolünü veya aktiflik durumunu güncelle."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse(status_code=404, content={"detail": "Kullanıcı bulunamadı."})

    # Kendi hesabının rol veya aktiflik durumunu değiştirmeye izin verme
    if user_id == int(current_user.get("sub", -1)):
        if data.role is not None or data.is_active is not None:
            return JSONResponse(status_code=400, content={
                "detail": "Kendi hesabınızın rol veya aktiflik durumunu değiştiremezsiniz."
            })

    if data.role is not None:
        if data.role not in _VALID_ROLES:
            return JSONResponse(status_code=422, content={"detail": f"Geçersiz rol: '{data.role}'."})
        user.role = data.role

    if data.full_name is not None and data.full_name.strip():
        user.full_name = data.full_name.strip()

    if data.is_active is not None:
        user.is_active = data.is_active

    db.commit()
    return {"id": user.id, "message": f"Kullanıcı '{user.username}' güncellendi."}


@app.put("/api/admin/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: int,
    data: PasswordResetRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(_SUPERADMIN_ONLY)),
):
    """Kullanıcı şifresini zorla sıfırla."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse(status_code=404, content={"detail": "Kullanıcı bulunamadı."})
    if len(data.new_password) < 6:
        return JSONResponse(status_code=422, content={"detail": "Şifre en az 6 karakter olmalıdır."})

    import bcrypt as _bcrypt
    user.password_hash = _bcrypt.hashpw(data.new_password.encode(), _bcrypt.gensalt()).decode()
    db.commit()
    return {"message": f"'{user.username}' kullanıcısının şifresi başarıyla sıfırlandı."}


# ─── Permission Matrix Endpoints (RBAC 2.0) ─────────────────────────────────

@app.get("/api/admin/permission-schema")
def admin_permission_schema(_: dict = Depends(require_role(_SUPERADMIN_ONLY))):
    """Returns the catalogue of categories × actions — drives the admin matrix UI."""
    return {"categories": PERMISSION_CATEGORIES}


@app.get("/api/admin/users/{user_id}/permissions")
def admin_get_user_permissions(
    user_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_role(_SUPERADMIN_ONLY)),
):
    """
    Returns the EFFECTIVE permission set for a user, plus a flag indicating
    whether they're using custom perms (`source: 'custom'`) or role defaults
    (`source: 'role-default'`). SuperAdmin always returns 'superadmin'.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse(status_code=404, content={"detail": "Kullanıcı bulunamadı."})

    if user.role == "SuperAdmin":
        source = "superadmin"
    elif user.permissions:
        source = "custom"
    else:
        source = "role-default"

    return {
        "user_id":     user.id,
        "username":    user.username,
        "full_name":   user.full_name,
        "role":        user.role,
        "source":      source,
        "permissions": _resolve_permissions(user),
    }


class PermissionsUpdateRequest(BaseModel):
    permissions: dict   # category → action → bool


@app.put("/api/admin/users/{user_id}/permissions")
def admin_update_user_permissions(
    user_id: int,
    data: PermissionsUpdateRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(_SUPERADMIN_ONLY)),
):
    """Persist a user-specific permission set as JSON. SuperAdmin perms are immutable."""
    import json as _json

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse(status_code=404, content={"detail": "Kullanıcı bulunamadı."})
    if user.role == "SuperAdmin":
        return JSONResponse(status_code=400, content={
            "detail": "SuperAdmin yetkileri değiştirilemez (her zaman tüm yetkilere sahiptir)."
        })

    # Sanitize: only accept categories/actions defined in PERMISSION_CATEGORIES
    cleaned = {}
    for cat, actions in PERMISSION_CATEGORIES.items():
        cleaned[cat] = {}
        for act in actions:
            cleaned[cat][act] = bool(data.permissions.get(cat, {}).get(act, False))

    user.permissions = _json.dumps(cleaned, ensure_ascii=False)
    db.commit()
    return {
        "message":     f"'{user.username}' yetkileri güncellendi.",
        "permissions": cleaned,
    }


@app.post("/api/admin/users/{user_id}/permissions/reset")
def admin_reset_user_permissions(
    user_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_role(_SUPERADMIN_ONLY)),
):
    """Clear custom permissions → user reverts to role defaults."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse(status_code=404, content={"detail": "Kullanıcı bulunamadı."})
    user.permissions = None
    db.commit()
    return {"message": f"'{user.username}' yetkileri rol varsayılanlarına döndürüldü."}


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


@app.post("/api/admin/smart-import")
async def smart_excel_import(
    file: UploadFile = File(...),
    commit: bool = False,
    _: dict = Depends(require_role(_SUPERADMIN_ONLY)),
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

    try:
        contents = await file.read()
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


@app.get("/traceability", response_class=HTMLResponse)
def traceability_page(request: Request):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("traceability.html", _page_ctx(request, payload))


@app.get("/quotations", response_class=HTMLResponse)
def quotations_page(request: Request):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    if not _can_see_finance(payload):
        return RedirectResponse(url="/", status_code=302)   # non-finance → dashboard
    return templates.TemplateResponse("quotations.html", _page_ctx(request, payload))


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    payload = _get_user_context(request)
    if not payload:
        return RedirectResponse(url="/login", status_code=302)
    if payload.get("role") != "SuperAdmin":
        return RedirectResponse(url="/", status_code=302)   # non-SuperAdmin → dashboard
    return templates.TemplateResponse("admin.html", _page_ctx(request, payload))
