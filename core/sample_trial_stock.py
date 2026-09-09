# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Numune analizi deneme tüketimi — TEK KAYNAK (core/stock_lots.py kalıbı).

FR.KK.01 formundaki her bileşen satırı (`SampleAnalysisIngredient`) kullanılan
miktarı KAYNAĞINDAN düşer:

  • source='sample'  → yalnız `Inventory.quantity` iner.  Numune STOK DEĞİLDİR
                       (24.08.2026 kuralı): `current_stock`'a dokunulmaz,
                       `Transaction` yazılmaz.  Lot/tedarikçi satıra snapshot.
  • source='stock'   → `stock_lots.consume` (FIFO lot + lot başına Output +
                       `current_stock`).  `current_stock` yetmezse 400
                       (üretimdeki otorite kapısı); lot yetmezliği engellemez.
  • source='pending' → hiçbir şey (hammadde henüz gelmedi / kullanılmadı).

DEĞİŞMEZ: `row.consumed_qty` = şu an fiilen alınmış miktar.  PUT satırı hedef
duruma FARK uygulayarak getirir; DELETE her satırı 0'a çeker (`release_all`).
İade: numune lotuna miktar geri yazılır; stokta `Transaction` ASLA silinmez,
imzalı `+Adjustment` yazılır ve `current_stock` artar (lot AÇILMAZ — CLAUDE.md
"girişler lot açmaz" bilinçli açığı; `uncovered` bunu karşılar).

Kilit sırası: Item önce (`with_for_update`), sonra Inventory — receive_stock
ile aynı.  İki geçiş: önce TÜM iadeler, sonra TÜM tüketimler (A: lot X→Y,
B: lot Y→X çapraz durumu güvenli).
"""
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from database import Inventory, Item, SampleAnalysis, SampleAnalysisIngredient, Transaction
from core import stock_lots

EPS = 1e-9
SOURCES = ("sample", "stock", "pending")
_NOT_RAW = ("Ambalaj", "Bitmiş Ürün")


class TrialStockError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _q(v) -> float:
    return round(float(v or 0.0), 6)


def _load_item(db: Session, item_id: int, domain: str) -> Item:
    item = db.query(Item).filter(Item.id == item_id).with_for_update().first()
    if not item or item.domain != domain or not item.is_active:
        raise TrialStockError(404, "Hammadde bulunamadı.")
    if item.category in _NOT_RAW:
        raise TrialStockError(400, f"'{item.name}' bir hammadde değil ({item.category}).")
    return item


def _item_for_release(db: Session, item_id: int) -> Optional[Item]:
    # Arşivlenmiş/başka kategoriye taşınmış kart stoğunu yine geri alabilmeli.
    return db.query(Item).filter(Item.id == item_id).with_for_update().first()


def _consume(db: Session, row: SampleAnalysisIngredient, item: Item, qty: float,
             *, actor: str, doc_no: str, domain: str) -> Optional[dict]:
    qty = _q(qty)
    if qty <= EPS or row.source == "pending":
        return None
    unit = item.unit or ""
    if row.source == "sample":
        inv = (db.query(Inventory).filter(Inventory.id == row.inventory_id)
               .with_for_update().first()) if row.inventory_id else None
        if not inv or not inv.is_sample or inv.domain != domain:
            raise TrialStockError(404, f"'{item.name}' için seçilen numune lotu bulunamadı.")
        if inv.item_id != item.id:
            raise TrialStockError(400, f"Seçilen numune lotu '{item.name}' hammaddesine ait değil.")
        have = _q(inv.quantity)
        if have + EPS < qty:
            raise TrialStockError(400, f"'{item.name}' numune lotunda ({inv.lot_number}) yeterli "
                                       f"miktar yok: kalan {have:g} {unit}, istenen {qty:g} {unit}.")
        inv.quantity = _q(have - qty)
        inv.updated_at = datetime.utcnow()
        row.lot_number = inv.lot_number
        row.supplier_name = inv.supplier.name if inv.supplier else None
        # BİLİNÇLİ: Transaction YOK, current_stock DOKUNULMAZ — numune stok değildir.
        detail = {"item": item.name, "lot": inv.lot_number, "qty": qty}
    elif row.source == "stock":
        have = _q(item.current_stock)
        if have + EPS < qty:
            raise TrialStockError(400, f"'{item.name}' stoğu yetersiz: mevcut {have:g} {unit}, "
                                       f"istenen {qty:g} {unit}.")
        used = stock_lots.consume(db, item, qty,
                                  note=f"Numune analizi {doc_no} — deneme tüketimi", actor=actor)
        # Lot kaydı olmayan eski kalemde özet "—×5" olur; belgeye lot yazma.
        row.lot_number = stock_lots.lot_summary(used) if any(l != "—" for l, _ in used) else None
        row.supplier_name = None
        detail = {"item": item.name, "lots": row.lot_number or "lot kaydı dışı", "qty": qty}
    else:
        raise TrialStockError(400, f"Geçersiz kaynak: {row.source}")
    row.consumed_qty = _q((row.consumed_qty or 0.0) + qty)
    return detail


def _release(db: Session, row: SampleAnalysisIngredient, item: Optional[Item], qty: float,
             *, actor: str, doc_no: str, reason: str) -> Optional[dict]:
    qty = _q(qty)
    if qty <= EPS or row.source == "pending":
        return None
    detail = None
    if row.source == "sample":
        inv = (db.query(Inventory).filter(Inventory.id == row.inventory_id)
               .with_for_update().first()) if row.inventory_id else None
        if inv is None:
            # Lot silinmiş / birleşmiş (FK SET NULL) — iade edilecek satır yok.
            detail = {"item": row.item_name, "lot": row.lot_number, "qty": qty, "skipped": "lot yok"}
        elif inv.is_sample:
            inv.quantity = _q(inv.quantity + qty)
            inv.updated_at = datetime.utcnow()
            detail = {"item": row.item_name, "lot": inv.lot_number, "qty": qty}
        else:
            # Lot bağlandıktan sonra stoğa çevrilmiş → artık DEFTERDE; iade Adjustment ile.
            inv.quantity = _q(inv.quantity + qty)
            inv.updated_at = datetime.utcnow()
            if item is not None:
                db.add(Transaction(
                    item_id=item.id, lot_number=inv.lot_number, transaction_type="Adjustment",
                    quantity=qty, performed_by=actor,
                    notes=f"Numune analizi {doc_no} iadesi — lot stoğa çevrilmiş | {reason}"[:500]))
                item.current_stock = _q(item.current_stock + qty)
            detail = {"item": row.item_name, "lot": inv.lot_number, "qty": qty, "ledger": True}
    elif row.source == "stock":
        if item is not None:
            db.add(Transaction(
                item_id=item.id, transaction_type="Adjustment", quantity=qty, performed_by=actor,
                notes=f"Numune analizi {doc_no} iadesi — {reason}"[:500]))
            item.current_stock = _q(item.current_stock + qty)
        detail = {"item": row.item_name, "qty": qty}
    row.consumed_qty = _q((row.consumed_qty or 0.0) - qty)
    return detail


def _validate(nr, pos: int):
    src = (getattr(nr, "source", None) or "pending").strip().lower()
    if src not in SOURCES:
        raise TrialStockError(400, f"{pos + 1}. satır: geçersiz kaynak '{src}'.")
    if src == "sample" and not getattr(nr, "inventory_id", None):
        raise TrialStockError(400, f"{pos + 1}. satır: numune lotu seçilmedi.")
    qty = _q(getattr(nr, "quantity", None))
    if qty < 0:
        raise TrialStockError(400, f"{pos + 1}. satır: miktar negatif olamaz.")
    return src, qty


def sync_ingredients(db: Session, analysis: SampleAnalysis, rows: list,
                     *, domain: str, actor: str) -> Dict[str, list]:
    """Create/PUT ortak: satırları hedef duruma getir, yalnız farkı uygula.

    `rows` = IngredientRow benzeri nesneler (row_id, item_id, source,
    inventory_id, quantity, unit, note).  Dönüş: audit özeti
    ``{"consumed": [...], "released": [...]}``.  Hata → TrialStockError
    (çağıran rollback eder; hiçbir parçası kalıcı olmaz).
    """
    doc_no = analysis.document_no or "—"
    existing = {g.id: g for g in analysis.ingredients}
    seen = set()
    consumed: List[dict] = []
    released: List[dict] = []
    plan: List[Tuple[SampleAnalysisIngredient, Item, float]] = []

    for pos, nr in enumerate(rows):
        src, new_qty = _validate(nr, pos)
        inv_id = int(nr.inventory_id) if (src == "sample" and nr.inventory_id) else None
        item = _load_item(db, int(nr.item_id), domain)
        rid = getattr(nr, "row_id", None)
        old = existing.get(rid) if rid else None        # başka forma ait id → yeni satır
        if old is not None and old.id in seen:
            old = None                                   # aynı row_id iki kez → ikincisi yeni
        if old is not None:
            seen.add(old.id)
            same_key = (old.item_id == item.id and old.source == src and
                        (src != "sample" or old.inventory_id == inv_id))
            if same_key:
                delta = _q(new_qty - (old.consumed_qty or 0.0))
                if delta < -EPS:
                    d = _release(db, old, item, -delta, actor=actor, doc_no=doc_no,
                                 reason="miktar azaltıldı")
                    if d: released.append(d)
                to_consume = max(delta, 0.0)
            else:
                d = _release(db, old, _item_for_release(db, old.item_id), old.consumed_qty or 0.0,
                             actor=actor, doc_no=doc_no, reason="kaynak/lot değişti")
                if d: released.append(d)
                old.item_id, old.source, old.inventory_id = item.id, src, inv_id
                old.lot_number = old.supplier_name = None
                old.consumed_qty = 0.0
                to_consume = new_qty
            row = old
        else:
            row = SampleAnalysisIngredient(analysis_id=analysis.id, item_id=item.id, source=src,
                                           inventory_id=inv_id, consumed_qty=0.0)
            db.add(row)
            analysis.ingredients.append(row)
            to_consume = new_qty
        row.item_name = item.name
        row.unit = item.unit or (getattr(nr, "unit", None) or None)
        row.quantity = new_qty
        row.note = (getattr(nr, "note", None) or "").strip()[:300] or None
        row.position = pos
        plan.append((row, item, to_consume))

    for old in list(existing.values()):                  # formdan çıkarılan satırlar
        if old.id in seen:
            continue
        d = _release(db, old, _item_for_release(db, old.item_id), old.consumed_qty or 0.0,
                     actor=actor, doc_no=doc_no, reason="satır silindi")
        if d: released.append(d)
        analysis.ingredients.remove(old)
        db.delete(old)

    for row, item, q in plan:                            # ikinci geçiş: tüketimler
        d = _consume(db, row, item, q, actor=actor, doc_no=doc_no, domain=domain)
        if d: consumed.append(d)
    return {"consumed": consumed, "released": released}


def release_all(db: Session, analysis: SampleAnalysis, *, actor: str, reason: str) -> List[dict]:
    """DELETE — her satırın fiilen düşülmüş miktarını kaynağına geri ver."""
    doc_no = analysis.document_no or "—"
    released: List[dict] = []
    for g in analysis.ingredients:
        d = _release(db, g, _item_for_release(db, g.item_id), g.consumed_qty or 0.0,
                     actor=actor, doc_no=doc_no, reason=reason)
        if d: released.append(d)
    return released
