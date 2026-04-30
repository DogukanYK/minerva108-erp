"""
Recipes router — recipe CRUD plus BOM (bill-of-materials) cost calculation.
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List

from database import get_db, Item, Recipe, RecipeIngredient
from core.auth import get_current_user
from core.permissions import _can_see_finance, require_permission

router = APIRouter(prefix="/api", tags=["recipes"])


# ─── Schemas ────────────────────────────────────────────────────────────────

class RecipeIngredientSchema(BaseModel):
    item_id: int
    quantity: float


class RecipeCreateRequest(BaseModel):
    name: Optional[str] = None          # arka planda hedef ürün adına eşitlenir; göndermek isteğe bağlı
    target_item_id: int
    expected_yield: float
    waste_percentage: Optional[float] = 0.0   # % fire oranı
    ingredients: List[RecipeIngredientSchema]


# ─── BOM cost helper ────────────────────────────────────────────────────────

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


# ─── Endpoints ──────────────────────────────────────────────────────────────

@router.get("/recipes")
def list_recipes(
    include_ingredients: bool = False,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    rows = db.query(Recipe).filter(Recipe.is_active == True).order_by(Recipe.id.desc()).all()
    finance_ok = _can_see_finance(current_user)
    result = []
    for r in rows:
        target = db.query(Item).filter(Item.id == r.target_item_id).first() if r.target_item_id else None
        costs  = _calc_recipe_costs(r, db) if finance_ok else {"total_cost": 0.0, "unit_cost": 0.0, "ingredient_costs": []}
        row = {
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
        }
        if include_ingredients:
            row["ingredient_names"] = [
                (ing.item.name if ing.item else "") for ing in r.ingredients
            ]
        result.append(row)
    return result


@router.post("/recipes", status_code=201)
def create_recipe(data: RecipeCreateRequest, db: Session = Depends(get_db), _: dict = Depends(require_permission("recipes", "create"))):
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


@router.delete("/recipes/{recipe_id}")
def delete_recipe(recipe_id: int, db: Session = Depends(get_db), _: dict = Depends(require_permission("recipes", "delete"))):
    recipe = db.query(Recipe).filter(Recipe.id == recipe_id).first()
    if not recipe:
        return JSONResponse(status_code=404, content={"detail": "Reçete bulunamadı."})
    db.delete(recipe)   # cascade="all, delete-orphan" recipe_ingredients'ı siler
    db.commit()
    return {"message": "Reçete silindi."}


@router.put("/recipes/{recipe_id}")
def update_recipe(
    recipe_id: int,
    data: RecipeCreateRequest,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("recipes", "edit")),
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


@router.get("/recipes/{recipe_id}")
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
