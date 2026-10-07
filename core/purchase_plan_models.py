# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Satın Alma Planı — istek modeli (pydantic).

Aynı model İKİ yerde kullanılır: `POST /api/purchase-plan/preview|export`
gövdesi ve kaydedilen senaryonun `config`'i (`purchase_plans.config`, sürümlü
JSON).  Bu yüzden alanlar geriye uyumlu genişletilmeli: yeni alan DAİMA
varsayılanlı eklenir, eski senaryo aynen açılabilsin.

Hesap motoru `core/purchase_plan.py`; router bu modülü doğrudan import eder
(motorun DB yükleyicisini çekmeden).

NOT (Python 3.9 yerel venv): `int | None` yazımı pydantic'in annotation
değerlendirmesinde 3.9'da patlar → `Optional[...]` kullanılır.
"""
from typing import Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_LINES = 500
MAX_EXTRA_PACKAGING = 20


class ExtraPkgIn(BaseModel):
    """Reçetede olmayan (ya da reçete ambalajı yerine kabul edilen) ambalaj.

    `item_id` → mevcut kart; `new_name` → sistemde kartı olmayan yeni kalem
    (sentetik `new:<slug>` anahtarı, stok 0).  İkisinden biri zorunlu.
    """
    item_id: Optional[int] = None
    new_name: Optional[str] = Field(None, max_length=150)
    pkg_type: Optional[str] = Field(None, max_length=20)
    per_unit: float = Field(1.0, gt=0, le=100)
    note: Optional[str] = Field(None, max_length=300)

    @model_validator(mode="after")
    def _one_of(self):
        if self.item_id is None and not (self.new_name or "").strip():
            raise ValueError("Ek ambalaj için kalem ya da yeni kalem adı gerekli")
        return self


class PlanLineIn(BaseModel):
    """Plan satırı = bir ürün × adet.  `recipe_id` ya da `target_item_id` zorunlu.

    • recipe_id verilirse o reçete kullanılır (başka ürünün reçetesiyle tahmin
      için: recipe_id + target_item_id birlikte → `estimated`).
    • yalnız target_item_id → hedefin en yeni aktif reçetesi; reçetesi yoksa
      satır "reçetesiz ürün" olarak raporlanır (tüketim yok).
    """
    recipe_id: Optional[int] = None
    target_item_id: Optional[int] = None
    qty: float = Field(..., gt=0, le=10_000_000)
    scale: float = Field(1.0, gt=0, le=1000)
    use_recipe_packaging: bool = True
    extra_packaging: List[ExtraPkgIn] = Field(default_factory=list, max_length=MAX_EXTRA_PACKAGING)
    display_name: Optional[str] = Field(None, max_length=200)
    label_faces: Optional[int] = Field(None, ge=1, le=6)
    note: Optional[str] = Field(None, max_length=500)

    @model_validator(mode="after")
    def _recipe_or_target(self):
        if self.recipe_id is None and self.target_item_id is None:
            raise ValueError("Satırda reçete ya da ürün seçilmeli")
        return self


class ManualLineIn(BaseModel):
    """Sistemde kartı olmayan serbest satır (koli, palet, ara levha…)."""
    section: Literal["label", "shipping", "other"] = "shipping"
    name: str = Field(..., min_length=1, max_length=200)
    qty: Optional[float] = Field(None, ge=0, le=100_000_000)
    qty_text: Optional[str] = Field(None, max_length=80)
    unit: str = Field("adet", max_length=20)
    note: Optional[str] = Field(None, max_length=300)


class WastePctIn(BaseModel):
    """Alım firesi % — üretim firesinin (reçete) ÜSTÜNE, tür bazında."""
    raw: float = Field(0.0, ge=0, le=100)
    packaging: float = Field(0.0, ge=0, le=100)
    label: float = Field(0.0, ge=0, le=100)


class HeldItemIn(BaseModel):
    """Bekletilen kalem — listeye/toplama girmez, "alınırsa" tutarıyla notta görünür."""
    item_id: int
    reason: str = Field("", max_length=300)


class PlanOptionsIn(BaseModel):
    stock_mode: Literal["net", "gross"] = "net"
    subtract_open_orders: bool = False
    subtract_finished_stock: bool = False
    extra_waste_pct: WastePctIn = Field(default_factory=WastePctIn)
    label_mode: Literal["TR", "EN", "exclude", "new"] = "TR"
    new_label_title: str = Field("Yeni etiket", max_length=80)
    merge_duplicates: bool = True
    manual_merges: List[Tuple[int, int]] = Field(default_factory=list, max_length=200)
    # None → AppSetting 'purchase_plan.default_excluded.<domain>' / genel anahtar
    # (yoksa DİSTİLE SU / SAF SU) — bkz. purchase_plan.resolve_default_excluded
    excluded_item_ids: Optional[List[int]] = Field(None, max_length=500)
    held_items: List[HeldItemIn] = Field(default_factory=list, max_length=200)
    safe_rounding: bool = True
    round_to_package: bool = False
    currency: Literal["USD", "EUR", "TRY"] = "USD"
    lookalike_check: bool = True
    # Tedarikçi tercihleri (07.10.2026) — eski senaryolarda alan yok → açık.
    # count_phase_out_stock: aynı "aynı malzeme" grubunda "bitirilecek" (ya da
    # bu malzemede "alma") tedarikçinin kartındaki stok önce kullanılır,
    # ihtiyaçtan düşülür (net mod).  respect_supplier_status: en iyi teklif
    # seçiminde bitirilecek / alma / atlanacak firmalar seçilmez, malzeme
    # tercihi + "tercih edilen" firma en ucuzdan önce gelir; False → yalnız
    # en ucuz (eski davranış).
    count_phase_out_stock: bool = True
    respect_supplier_status: bool = True
    item_notes: Dict[int, str] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list, max_length=50)
    checklist_owner: Optional[str] = Field(None, max_length=120)

    @field_validator("item_notes")
    @classmethod
    def _item_notes_size(cls, v):
        if len(v) > 500:
            raise ValueError("En fazla 500 kalem notu")
        for txt in v.values():
            if txt is not None and len(txt) > 500:
                raise ValueError("Kalem notu en fazla 500 karakter")
        return v

    @field_validator("notes")
    @classmethod
    def _notes_size(cls, v):
        for txt in v:
            if len(txt or "") > 1000:
                raise ValueError("Not en fazla 1000 karakter")
        return v


class PlanRequest(BaseModel):
    title: Optional[str] = Field(None, max_length=150)
    lines: List[PlanLineIn] = Field(..., min_length=1, max_length=MAX_LINES)
    manual_lines: List[ManualLineIn] = Field(default_factory=list, max_length=100)
    options: PlanOptionsIn = Field(default_factory=PlanOptionsIn)
