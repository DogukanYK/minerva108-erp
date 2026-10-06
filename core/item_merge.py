# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""Kopya ürün kartı birleştirme motoru + karar kümesi görünümleri.

Aynı fiziksel malzeme birden fazla kartla yaşayınca stok bölünür (Lauryl
Glucoside: 1.5 kg bir kartta, 218 kg diğerinde) ve reçeteler yanlış karta
bakar — üretim "yetmez" der, malzeme yan kartta durur.  Bu modül lab'ın
popup'tan verdiği "birleştir" kararını uygular.

DEFTER KURALI (core/snapshots.py): stok değişikliği YALNIZ Input/Output/
Adjustment transaction'ı ile olur ve Transaction satırları ASLA taşınmaz/
silinmez.  Birleştirmede kaybeden kartın stoğu Adjustment çiftiyle taşınır
(kaybeden −X, kazanan +X); eski Input/Output geçmişi kaybeden kartta kalır ve
kart bazlı rekonstrüksiyon iki tarafta da tutarlı kalır — aynı kalıp
scripts/merge_duplicate_products.py'de kanıtlandı.

Taşınan FK'ler: inventory · recipe_ingredients (aynı reçetede çift satır
oluşursa birimler eşitse toplanır) · supplier_prices · distributor_prices
(UNIQUE çakışmasında kaybedenin satırı düşer) · delivery_items ·
product_return_items · quotation_items · retention_samples (item_name
snapshot'ı da güncellenir).  Taşınmayanlar: transactions (defter kuralı),
stock_snapshot (donmuş tarih — ASLA).  "Aynı malzeme" grubu: kazananın grubu
yoksa kaybedeninki devredilir, kaybeden pasif üye olarak kalır
(core.material_groups.transfer_on_merge); tek aktif kartı kalan grup dağılır
ve özetin `dissolved_groups`'una girer (çağıran audit'e yazar).

Kaybeden kart reçete HEDEFİ ise, varyasyon ana ürünüyse ya da üretim geçmişi
hedefiyse birleştirme REDDEDİLİR — bunlar bitmiş ürün göstergesidir, yanlış
kümeye işaret eder.
"""
import json
from datetime import datetime
from typing import List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.material_groups import transfer_on_merge
from database import (DeliveryItem, DistributorPrice, DuplicateItemDecision,
                      Inventory, Item, ProductionHistory, ProductReturnItem,
                      QuotationItem, Recipe, RecipeIngredient, RetentionSample,
                      SupplierPrice, Transaction)


class MergeError(Exception):
    """Kullanıcıya gösterilebilir birleştirme engeli."""


def _adjust(db: Session, item: Item, delta: float, note: str, actor: str,
            lot_number: Optional[str] = None) -> None:
    """İmzalı Adjustment yaz + `current_stock`'u kaydır (lot taşıma da kullanır,
    core/stock_lots.move_lot — orada `lot_number` dolu)."""
    if abs(delta) < 1e-9:
        return
    db.add(Transaction(item_id=item.id, transaction_type="Adjustment",
                       quantity=round(delta, 6), timestamp=datetime.utcnow(),
                       lot_number=lot_number,
                       notes=note[:500], performed_by=actor))
    item.current_stock = round((item.current_stock or 0.0) + delta, 6)


def _blockers(db: Session, loser: Item) -> List[str]:
    """Birleştirmeyi durduran referanslar — hepsi 'bu kart aslında kopya değil'
    işaretidir."""
    out = []
    if db.query(Item.id).filter(Item.parent_id == loser.id).first():
        out.append("alt varyasyonları var (ana ürün)")
    if db.query(Recipe.id).filter(Recipe.target_item_id == loser.id,
                                  Recipe.is_active == True).first():   # noqa: E712
        out.append("bir reçetenin HEDEF ürünü")
    if db.query(ProductionHistory.id).filter(
            ProductionHistory.target_item_id == loser.id).first():
        out.append("üretim geçmişinin hedef ürünü")
    return out


def merge_items(db: Session, loser_id: int, survivor_id: int, actor: str) -> dict:
    """Kaybeden kartı kazanana birleştir.  Commit ÇAĞIRANA aittir.

    Dönüş: özet dict (stok taşınan, reçete/lot/fiyat sayıları) — audit notu
    ve popup mesajı bundan kurulur.
    """
    if loser_id == survivor_id:
        raise MergeError("Kart kendisiyle birleştirilemez.")
    survivor = (db.query(Item).filter(Item.id == survivor_id)
                .with_for_update().first())
    loser = (db.query(Item).filter(Item.id == loser_id)
             .with_for_update().first())
    if not survivor or not loser:
        raise MergeError("Kart bulunamadı.")
    if not survivor.is_active:
        raise MergeError(f"Hedef kart pasif: {survivor.name}")
    if (loser.domain or "cosmetics") != (survivor.domain or "cosmetics"):
        raise MergeError("Kartlar farklı panellerde — birleştirilemez.")
    blockers = _blockers(db, loser)
    if blockers:
        raise MergeError(f"«{loser.name}» birleştirilemez: {', '.join(blockers)}. "
                         f"Bu kart kopya olmayabilir — yöneticiye danışın.")

    moved_stock = float(loser.current_stock or 0.0)
    summary = {"loser_id": loser.id, "loser_name": loser.name,
               "survivor_id": survivor.id, "survivor_name": survivor.name,
               "moved_stock": moved_stock, "unit": survivor.unit or "",
               "recipes": 0, "recipe_rows_merged": 0, "lots": 0,
               "supplier_prices": 0, "distributor_prices_moved": 0,
               "distributor_prices_dropped": 0, "other_refs": 0}

    # ── Stok — Adjustment çifti (defter kuralı) ──
    if abs(moved_stock) > 1e-9:
        note = (f"Eski: {loser.current_stock:g} → Yeni: 0 | Sebep: kopya kart "
                f"«{survivor.name}» (id {survivor.id}) ile birleştirildi")
        _adjust(db, loser, -moved_stock, note, actor)
        note2 = (f"Eski: {survivor.current_stock:g} → "
                 f"Yeni: {round((survivor.current_stock or 0) + moved_stock, 6):g} | "
                 f"Sebep: kopya kart «{loser.name}» (id {loser.id}) buraya birleştirildi")
        _adjust(db, survivor, moved_stock, note2, actor)

    # ── Envanter lotları ──
    lots = db.query(Inventory).filter(Inventory.item_id == loser.id).all()
    for lot in lots:
        lot.item_id = survivor.id
    summary["lots"] = len(lots)

    # ── Reçete kalemleri — taşı, aynı reçetede çift oluşursa topla ──
    ri_rows = (db.query(RecipeIngredient)
               .filter(RecipeIngredient.item_id == loser.id).all())
    recipes_touched = set()
    for row in ri_rows:
        recipes_touched.add(row.recipe_id)
        twin = (db.query(RecipeIngredient)
                .filter(RecipeIngredient.recipe_id == row.recipe_id,
                        RecipeIngredient.item_id == survivor.id,
                        RecipeIngredient.id != row.id).first())
        if twin is not None and (twin.unit or "") == (row.unit or ""):
            # Reçete her iki kopyayı da içeriyordu — tek satıra topla.
            twin.quantity = round((twin.quantity or 0) + (row.quantity or 0), 6)
            db.delete(row)
            summary["recipe_rows_merged"] += 1
        else:
            row.item_id = survivor.id
    summary["recipes"] = len(recipes_touched)

    # ── Tedarikçi fiyatları ──
    sp = db.query(SupplierPrice).filter(SupplierPrice.item_id == loser.id).all()
    for row in sp:
        row.item_id = survivor.id
    summary["supplier_prices"] = len(sp)

    # ── Distribütör fiyatları — UNIQUE(distributor_id, item_id) çakışabilir ──
    for row in db.query(DistributorPrice).filter(
            DistributorPrice.item_id == loser.id).all():
        clash = (db.query(DistributorPrice.id)
                 .filter(DistributorPrice.distributor_id == row.distributor_id,
                         DistributorPrice.item_id == survivor.id).first())
        if clash:
            db.delete(row)                       # kazananın fiyatı esas
            summary["distributor_prices_dropped"] += 1
        else:
            row.item_id = survivor.id
            summary["distributor_prices_moved"] += 1

    # ── Diğer yumuşak referanslar ──
    for model in (DeliveryItem, ProductReturnItem, QuotationItem):
        rows = db.query(model).filter(model.item_id == loser.id).all()
        for row in rows:
            row.item_id = survivor.id
        summary["other_refs"] += len(rows)
    for rs in db.query(RetentionSample).filter(
            RetentionSample.item_id == loser.id).all():
        rs.item_id = survivor.id
        rs.item_name = survivor.name
        summary["other_refs"] += 1

    # ── Boş alanları kazanana devret ──
    if not (survivor.name_tr or "").strip() and (loser.name_tr or "").strip():
        survivor.name_tr = loser.name_tr
    if not (survivor.barcode or "").strip() and (loser.barcode or "").strip():
        bc = loser.barcode
        loser.barcode = None
        db.flush()                               # unique index önce boşalsın
        survivor.barcode = bc
    if not (survivor.min_stock_level or 0) and (loser.min_stock_level or 0):
        survivor.min_stock_level = loser.min_stock_level
    if not (survivor.cost_price or 0) and (loser.cost_price or 0):
        survivor.cost_price = loser.cost_price

    loser.is_active = False

    # ── "Aynı malzeme" grubu — kazananın grubu yoksa kaybedeninki devredilir ──
    db.flush()
    summary["material_group_id"], summary["dissolved_groups"] = transfer_on_merge(db, loser, survivor)
    return summary


# ─── Popup görünümleri ───────────────────────────────────────────────────────

def _card_view(db: Session, item: Item) -> dict:
    tx_count, last_tx = (db.query(func.count(Transaction.id),
                                  func.max(Transaction.timestamp))
                         .filter(Transaction.item_id == item.id).first())
    recipe_count = (db.query(func.count(RecipeIngredient.id))
                    .filter(RecipeIngredient.item_id == item.id).scalar()) or 0
    return {
        "id": item.id, "name": item.name, "name_tr": item.name_tr or "",
        "unit": item.unit or "", "current_stock": item.current_stock or 0,
        "recipe_count": int(recipe_count), "tx_count": int(tx_count or 0),
        "last_tx": last_tx.strftime("%d.%m.%Y") if last_tx else "",
        "created": item.created_at.strftime("%d.%m.%Y") if item.created_at else "",
    }


def pending_clusters(db: Session) -> List[dict]:
    """Bekleyen karar kümeleri + kart detayları.

    Kümede 2'den az AKTİF kart kalmışsa (biri başka yoldan pasifleştiyse)
    küme kendiliğinden 'kept' olarak kapanır — popup boş soru sormaz.
    """
    out = []
    rows = (db.query(DuplicateItemDecision)
            .filter(DuplicateItemDecision.status == "pending")
            .order_by(DuplicateItemDecision.id).all())
    for row in rows:
        try:
            ids = [int(x) for x in json.loads(row.item_ids)]
        except (TypeError, ValueError):
            continue
        items = (db.query(Item)
                 .filter(Item.id.in_(ids), Item.is_active == True)   # noqa: E712
                 .all())
        if len(items) < 2:
            row.status = "kept"
            row.decided_by = "sistem"
            row.decided_at = datetime.utcnow()
            row.result_note = "Kümede tek aktif kart kaldı — soru kapandı."
            continue
        out.append({
            "id": row.id, "title": row.title,
            "cards": sorted((_card_view(db, it) for it in items),
                            key=lambda c: c["id"]),
        })
    return out
