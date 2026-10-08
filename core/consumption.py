# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Ortak tüketim motoru — "bu reçeteden N adet üretirsek hangi kalemden ne kadar
düşer?" sorusunun TEK KAYNAĞI.

Bu modülden önce aynı kural iki yerde ayrı ayrı yazılıydı: gerçek üretim
(`routers/production.py::start_production`) ve what-if simülasyonu
(`core/production_sim.simulate`).  Satın Alma Planı üçüncü kopya olacaktı;
bunun yerine simülasyon ve plan bu motoru kullanır.  P2'den (08.10.2026)
beri `start_production` da satırlarını bu motordan alır
(`core/production_plan.plan` — kaynak kart seçimi, lot dağıtımı, kart
başına stok kapısı onun işi).  `tests/test_consumption.py`'deki parite testi
gerçek `POST /api/production` çıktısını yine kilitler.

Kurallar (start_production ile birebir):
  • hammadde brüt = ing.quantity × qty / output × (1 + fire%/100)
  • category='Ambalaj' (pkg_type='etiket' etiketler dahil) fire MUAF, faktör 1.0
  • Dil etiketli kalem (language + label_group) reçete başına GRUP BAŞINA bir
    kez sayılır (reçetede TR+EN kardeşin ikisi de varsa ikincisi atlanır)
  • Etiket seçilen dildeki AKTİF kardeşe çözülür; dili zaten seçilen dilse
    kalemin kendisi kullanılır; kardeş yoksa kalem atlanır + kayda geçer
    (üretim de durmaz, uyarıyla atlar)
  • Reçetede silinmiş/bulunamayan kalem atlanır (üretim burada 404 döner;
    hesap aracı durmaz, `missing_item_ids`'e yazar)

Üretimde olmayan, planlama için eklenen anahtarlar:
  • label_mode="exclude" → etiket tüketimi tamamen düşer
  • label_mode="new"     → etiket tüketimi düşer, etiket grupları
    `label_groups`'ta raporlanır (yeni basılacak etiket satırları için)
  • scale                → YALNIZ hammaddeyi çarpar (ambalaj/etiket adedi
    ürün başına sabittir)
  • include_packaging=False → ambalaj VE etiket düşer (label_groups da boş)

Çekirdek fonksiyon SAFTIR (DB'siz) — girdiler küçük donmuş dataclass'lar;
DB'den toplu yükleme `load_recipe_recs()` ile yapılır.
"""
import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

LABEL_MODES = ("TR", "EN", "exclude", "new")


@dataclass(frozen=True)
class ItemRec:
    id: int
    name: str
    category: str = ""
    unit: str = ""
    pkg_type: Optional[str] = None
    language: Optional[str] = None
    label_group: Optional[str] = None
    current_stock: float = 0.0
    is_active: bool = True
    domain: str = "cosmetics"


@dataclass(frozen=True)
class IngredientRec:
    item_id: int
    quantity: float
    unit: Optional[str] = None
    phase: Optional[str] = None


@dataclass(frozen=True)
class RecipeRec:
    id: int
    name: str
    output_quantity: float = 1.0
    waste_percentage: float = 0.0
    target_item_id: Optional[int] = None
    ingredients: Tuple[IngredientRec, ...] = ()
    domain: str = "cosmetics"


@dataclass(frozen=True)
class ConsumptionLine:
    """Tüketilen TEK kalem (aynı kalem reçetede iki satırsa toplanır, `rows`=2).

    item_id      — gerçekten tüketilen kalem (etiket kardeşe çözülmüş olabilir)
    source_item_id — reçetedeki orijinal kalem (çözülmediyse item_id ile aynı)
    kind         — raw | packaging | label
    net_per_unit — 1 ürün için firesiz miktar (scale dahil)
    per_unit     — 1 ürün için brüt miktar (= net_per_unit × factor)
    gross        — qty ürün için brüt miktar
    factor       — fire çarpanı (ambalaj/etiket 1.0)
    """
    item_id: int
    kind: str
    net_per_unit: float
    per_unit: float
    gross: float
    factor: float
    label_group: Optional[str] = None
    source_item_id: Optional[int] = None
    rows: int = 1


@dataclass(frozen=True)
class Expansion:
    lines: Tuple[ConsumptionLine, ...] = ()
    skipped_labels: Tuple[dict, ...] = ()     # seçilen dilde kardeşi olmayan etiketler
    label_groups: Tuple[dict, ...] = ()       # yalnız label_mode="new"
    missing_item_ids: Tuple[int, ...] = ()    # reçetede olup kalem kartı bulunamayan


def _is_lang_label(it: ItemRec) -> bool:
    """Üretimdeki koşulun aynısı: `item.language and item.label_group`."""
    return bool(it.language and it.label_group)


def _kind(it: ItemRec) -> str:
    if it.category != "Ambalaj":
        return "raw"
    if it.pkg_type == "etiket" or _is_lang_label(it):
        return "label"
    return "packaging"


def _is_label_like(it: ItemRec) -> bool:
    """exclude/new modlarında düşecek kalem: dil etiketi ya da ambalaj-etiket."""
    return _is_lang_label(it) or _kind(it) == "label"


def expand_recipe(recipe: RecipeRec, qty: float, items: Dict[int, ItemRec],
                  label_siblings: Dict[str, Dict[str, ItemRec]], *,
                  label_mode: str = "TR", scale: float = 1.0,
                  include_packaging: bool = True) -> Expansion:
    """Bir reçeteden `qty` adet üretimin kalem bazında tüketimi.

    `items`          — {item_id: ItemRec}; reçete kalemleri (+ çözülen kardeşler)
    `label_siblings` — {label_group: {language: ItemRec}}; yalnız AKTİF kardeşler
    """
    if label_mode not in LABEL_MODES:
        raise ValueError(f"Geçersiz label_mode: {label_mode!r}")
    qty = float(qty or 0.0)
    output = recipe.output_quantity or 1.0
    mult = qty / output
    waste_factor = 1.0 + (recipe.waste_percentage or 0.0) / 100.0
    scale = 1.0 if scale is None else float(scale)

    acc: Dict[int, dict] = {}          # item_id → birikmiş satır (ilk görülme sırası korunur)
    processed_groups = set()           # aynı label_group iki kez sayılmasın
    skipped, groups, missing = [], [], []

    for ing in recipe.ingredients:
        it = items.get(ing.item_id)
        if it is None:
            missing.append(ing.item_id)
            continue
        source_id = it.id
        label_like = _is_label_like(it)

        # Ambalaj/etiket istenmiyorsa dil çözümüne bile girmeden düş (aksi
        # halde var olmayan kardeş "atlandı" diye raporlanırdı).
        if not include_packaging and (label_like or it.category == "Ambalaj"):
            continue

        if label_like and label_mode in ("exclude", "new"):
            if label_mode == "new":
                gkey = it.label_group or f"item:{it.id}"
                if gkey in processed_groups:
                    continue
                processed_groups.add(gkey)
                langs = sorted((label_siblings.get(it.label_group) or {}).keys()) if it.label_group else []
                per_unit = ing.quantity / output
                groups.append({
                    "label_group": it.label_group,
                    "item_id": it.id,
                    "name": it.name,
                    "languages": langs or ([it.language] if it.language else []),
                    "per_unit": per_unit,
                    "gross": ing.quantity * mult,
                })
            continue

        # ── Etiket dil çözümü (start_production ile birebir) ─────────────────
        if _is_lang_label(it):
            if it.label_group in processed_groups:
                continue
            processed_groups.add(it.label_group)
            if it.language != label_mode:
                sib = (label_siblings.get(it.label_group) or {}).get(label_mode)
                if sib is None:
                    skipped.append({"item_id": it.id, "label": it.name,
                                    "label_group": it.label_group, "language": label_mode})
                    continue
                it = sib

        kind = _kind(it)
        is_amb = (it.category == "Ambalaj")
        factor = 1.0 if is_amb else waste_factor        # ambalaja fire uygulanmaz
        s = scale if kind == "raw" else 1.0              # ölçek YALNIZ hammaddeye
        net_per_unit = ing.quantity / output * s
        per_unit = net_per_unit * factor
        gross = ing.quantity * mult * factor * s

        d = acc.get(it.id)
        if d is None:
            acc[it.id] = {"item_id": it.id, "kind": kind, "net_per_unit": net_per_unit,
                          "per_unit": per_unit, "gross": gross, "factor": factor,
                          "label_group": it.label_group, "source_item_id": source_id,
                          "rows": 1}
        else:
            d["net_per_unit"] += net_per_unit
            d["per_unit"] += per_unit
            d["gross"] += gross
            d["rows"] += 1

    return Expansion(
        lines=tuple(ConsumptionLine(**d) for d in acc.values()),
        skipped_labels=tuple(skipped),
        label_groups=tuple(groups),
        missing_item_ids=tuple(missing),
    )


# ─── DB yükleyici ───────────────────────────────────────────────────────────

def item_rec(it) -> ItemRec:
    """ORM Item → ItemRec."""
    return ItemRec(
        id=it.id, name=it.name or "", category=it.category or "", unit=it.unit or "",
        pkg_type=it.pkg_type, language=it.language, label_group=it.label_group,
        current_stock=float(it.current_stock or 0.0),
        is_active=bool(it.is_active) if it.is_active is not None else True,
        domain=it.domain or "cosmetics",
    )


def load_recipe_recs(db, recipe_ids, domain: str, *, active_only: bool = True):
    """Reçeteleri + kalemlerini + etiket kardeşlerini 4 sorguda yükler.

    Döner: (recipes: [RecipeRec] id sırasıyla, items: {id: ItemRec},
            label_siblings: {label_group: {language: ItemRec}})

    `items` reçete kalemlerini, hedef ürünleri ve kardeş etiketleri içerir.
    Kalemler ve etiket kardeşleri aynı panelden yüklenir; aynı label_group
    iki panelde kullanılsa bile başka panelin etiketi tüketilemez.
    Kardeşlerde aynı grup+dil için birden fazla aktif kart varsa en küçük id
    seçilir (üretimin `.first()`'ü ile aynı pratik sonuç, ama deterministik).
    """
    from database import Item, Recipe, RecipeIngredient

    ids = [int(i) for i in (recipe_ids or [])]
    if not ids:
        return [], {}, {}
    q = db.query(Recipe).filter(Recipe.id.in_(ids), Recipe.domain == domain)
    if active_only:
        q = q.filter(Recipe.is_active == True)   # noqa: E712
    recipes = q.order_by(Recipe.id.asc()).all()
    if not recipes:
        return [], {}, {}
    rids = [r.id for r in recipes]

    ing_rows = (db.query(RecipeIngredient)
                .filter(RecipeIngredient.recipe_id.in_(rids))
                .order_by(RecipeIngredient.id.asc())
                .all())
    by_recipe: Dict[int, list] = {}
    for ing in ing_rows:
        by_recipe.setdefault(ing.recipe_id, []).append(
            IngredientRec(item_id=ing.item_id, quantity=float(ing.quantity or 0.0),
                          unit=ing.unit, phase=ing.phase))

    item_ids = {ing.item_id for ing in ing_rows}
    item_ids |= {r.target_item_id for r in recipes if r.target_item_id}
    items: Dict[int, ItemRec] = {}
    if item_ids:
        for it in db.query(Item).filter(Item.id.in_(item_ids), Item.domain == domain).all():
            items[it.id] = item_rec(it)

    label_siblings: Dict[str, Dict[str, ItemRec]] = {}
    groups = {it.label_group for it in items.values() if _is_lang_label(it)}
    if groups:
        sibs = (db.query(Item)
                .filter(Item.label_group.in_(groups), Item.language.isnot(None),
                        Item.domain == domain,
                        Item.is_active == True)   # noqa: E712
                .order_by(Item.id.asc())
                .all())
        for s in sibs:
            rec = items.get(s.id) or item_rec(s)
            items.setdefault(s.id, rec)
            label_siblings.setdefault(s.label_group, {}).setdefault(s.language, rec)

    out = [RecipeRec(
        id=r.id, name=r.name or "", output_quantity=float(r.output_quantity or 1.0),
        waste_percentage=float(r.waste_percentage or 0.0), target_item_id=r.target_item_id,
        ingredients=tuple(by_recipe.get(r.id, ())), domain=r.domain or "cosmetics",
    ) for r in recipes]
    return out, items, label_siblings


# ─── Yardımcılar ────────────────────────────────────────────────────────────

_ML_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*ml", re.IGNORECASE)


def parse_ml(*texts) -> Optional[float]:
    """İsim/varyasyondan ml değeri çek — '50ml', '100 ML' → 50 / 100.

    İlk eşleşen metin kazanır.  Eskiden `routers/production.py::_parse_ml`
    (üretim föyü şişe boyu); orada aynı adla re-export edilir.
    """
    for t in texts:
        m = _ML_RE.search(str(t or ""))
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                pass
    return None
