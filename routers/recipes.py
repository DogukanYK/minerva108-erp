# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Recipes router — recipe CRUD plus BOM (bill-of-materials) cost calculation.
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional, List

from database import get_db, Item, Recipe, RecipeIngredient, to_tr
from core.auth import get_current_user
from core.permissions import _can_see_finance, require_permission
from core.domain import active_domain

router = APIRouter(prefix="/api", tags=["recipes"])


# ─── Schemas ────────────────────────────────────────────────────────────────

class RecipeIngredientSchema(BaseModel):
    item_id: int
    quantity: float = Field(..., gt=0, le=1_000_000)
    phase: Optional[str] = Field(None, max_length=8)   # Üretim föyü FAZ (A/B/D/E)


class RecipeCreateRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=150)
    target_item_id: int
    expected_yield: float = Field(..., gt=0, le=1_000_000)
    waste_percentage: Optional[float] = Field(0.0, ge=0, le=100)
    production_notes: Optional[str] = Field(None, max_length=5000)   # YAPILIŞI metni
    # 200 bileşen — gerçekçi tavan, saldırgan 100K bileşenli payload yollayamaz
    ingredients: List[RecipeIngredientSchema] = Field(..., min_length=1, max_length=200)


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
            "phase":         ing.phase or "",        # Phase 16 — üretim föyü FAZ
            "language":      item.language or "",    # Phase 15 — etiket dili
            "label_group":   item.label_group or "", # üretimde dil çözümü için
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
    domain: str = Depends(active_domain),
):
    rows = (db.query(Recipe)
            .filter(Recipe.is_active == True, Recipe.domain == domain)
            .order_by(Recipe.id.desc()).all())
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
            "target_item_id":      r.target_item_id,
            "target_item_name":    target.name if target else (r.description or r.name),
            "target_item_unit":    target.unit if target else (r.output_unit or ""),
            "target_item_barcode": (target.barcode or "") if target else "",   # Phase 9 — phone scan → recipe lookup
            "ingredient_count": len(r.ingredients),
            "total_cost":       costs["total_cost"],
            "unit_cost":        costs["unit_cost"],
            "created_at":       to_tr(r.created_at).strftime("%d.%m.%Y") if r.created_at else "",
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
            production_notes=(data.production_notes or "").strip() or None,
            domain=(target_item.domain or "cosmetics"),   # Faz 3 — hedef ürünün paneli
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
                phase=(ing.phase or "").strip().upper() or None,
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
        recipe.production_notes = (data.production_notes or "").strip() or None

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
                phase=(ing.phase or "").strip().upper() or None,
            ))

        db.commit()
        return {"id": recipe.id, "message": "Reçete güncellendi."}
    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Reçete güncellenemedi."})


# ─── İçindekiler Raporu ─────────────────────────────────────────────────────
# Reçete bileşimi ticari sırdır → recipes:view gate'i (Distributor'da yok).
# Rapor maliyetsizdir (yalnız içerik) — finance gating gerekmez.

class IngredientsReportRequest(BaseModel):
    # 1000 ürün — gerçekçi tavan (katalog ~500); saldırgan dev payload yollayamaz
    item_ids: List[int] = Field(..., min_length=1, max_length=1000)
    format: str = Field("xlsx", pattern="^(xlsx|pdf)$")


@router.get("/recipes/ingredients-report/products")
def ingredients_report_products(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("recipes", "view")),
    domain: str = Depends(active_domain),
):
    """Rapor seçim paneli: aktif paneldeki bitmiş ürünler (marka + reçete durumu)."""
    from core.ingredients_report import list_report_products
    return {"products": list_report_products(db, domain)}


@router.post("/recipes/ingredients-report/export")
def ingredients_report_export(
    data: IngredientsReportRequest,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("recipes", "view")),
    domain: str = Depends(active_domain),
):
    """Seçilen bitmiş ürünlerin içindekiler raporu — A4-yazdırılabilir Excel / antetli PDF."""
    import io

    from fastapi.responses import StreamingResponse

    from core.ingredients_report import assemble, build_workbook, render_pdf, report_filename

    rep = assemble(db, data.item_ids, domain)
    if not rep["products"] and not rep["recipeless"]:
        # Domain dışı / geçersiz id'ler assemble'da düşer — burada 400'e çevrilir.
        return JSONResponse(status_code=400, content={"detail": "Seçilen ürün bulunamadı."})
    try:
        if data.format == "pdf":
            content, media, ext = render_pdf(rep), "application/pdf", "pdf"
        else:
            content = build_workbook(rep)
            media, ext = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "Rapor üretilemedi."})
    return StreamingResponse(
        io.BytesIO(content), media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{report_filename(ext)}"'})


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
        "production_notes": recipe.production_notes or "",
        "total_cost":       total_cost,
        "unit_cost":        unit_cost,
        "ingredients":      ingredients,
    }
