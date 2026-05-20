# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Undo log helpers — kayıt + geri alma.

Tasarım kararları
-----------------
* **Per-user log, 50-deep ring buffer.**  User A'nın undo'su sadece A'nın
  son hareketini etkiler; eşzamanlı çalışan B/C kullanıcılarını ilgilendirmez.
* **Action-type registry pattern.**  Her undoable action için iki callable:
    - `record(...)` — mutation öncesi/sonrası snapshot al, undo_log'a yaz.
    - `undo(db, entry)` — payload'daki BEFORE state'e göre geri al; çakışma
      varsa exception fırlat.
  Yeni action eklemek = registry'ye iki fonksiyon eklemek.  Ana endpoint kodu
  `record_*` wrapper'ını import edip mutation sonrasında çağırır.
* **Çakışma kontrolü.**  Undo, kaydı alındığı andan beri değişmemiş satırlar
  üzerinde çalışmalı.  Aksi halde başkasının değişikliğini sessizce eziyoruz.
  Her undo handler kendi tutarlılık check'ini yapar (örn: stok hâlâ
  after_stock değerinde mi?).  Değilse `UndoConflict` raise.
* **Transaction safety.**  undo() ana endpoint'in transaction'ı içinde
  çalışır; hata olursa rollback temizler.

Kapsam (V1):
    stock_adjust, item_edit, inventory_receive
"""
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from database import (
    UndoLog, Item, Inventory, Transaction,
)


# ──────────────────────────────────────────────────────────────────────────
# Hata sınıfları — endpoint'te yakalanır, kullanıcıya net mesaj döner.
# ──────────────────────────────────────────────────────────────────────────

class UndoConflict(Exception):
    """Veri sonradan başkası tarafından değişti — geri alınamıyor."""
    pass


class UndoTargetGone(Exception):
    """Hedef kayıt artık yok (silinmiş) — geri alınamıyor."""
    pass


class UndoUnsupported(Exception):
    """Bu action_type için handler yok (eski/silinmiş tip)."""
    pass


# ──────────────────────────────────────────────────────────────────────────
# Public API: record + undo
# ──────────────────────────────────────────────────────────────────────────

MAX_PER_USER = 50


def record(
    db: Session,
    *,
    user_id: int,
    action_type: str,
    target_table: str,
    target_id: Optional[int],
    payload: dict,
    description: str,
) -> UndoLog:
    """
    Yeni bir undoable entry yaz, eski entry'leri 50'lik ring'e indir.

    Caller'ın transaction'ında çalışır — caller commit etmezse log kaydı da
    kaybolur (mutation rollback olduğunda log da rollback — istenen davranış).
    """
    entry = UndoLog(
        user_id=user_id,
        action_type=action_type,
        target_table=target_table,
        target_id=target_id,
        payload=payload,
        description=description[:255],
        created_at=datetime.utcnow(),
    )
    db.add(entry)
    # 50+ eskileri sil — burada commit etmiyoruz, caller'ın transaction'ı
    # bütünü yutar.  flush ile entry.id'yi alalım önce.
    db.flush()
    # Bu user'ın en yeni 50'den eski entry'lerini sil.  Subquery → ANY-like.
    old_ids = (
        db.query(UndoLog.id)
        .filter(UndoLog.user_id == user_id)
        .order_by(UndoLog.id.desc())
        .offset(MAX_PER_USER)
        .all()
    )
    if old_ids:
        db.query(UndoLog).filter(UndoLog.id.in_([x[0] for x in old_ids])).delete(synchronize_session=False)
    return entry


def peek_latest_undoable(db: Session, user_id: int) -> Optional[UndoLog]:
    """En yeni undoable entry — yoksa None.  Frontend button enable kontrolü."""
    return (
        db.query(UndoLog)
        .filter(UndoLog.user_id == user_id, UndoLog.undone_at.is_(None))
        .order_by(UndoLog.id.desc())
        .first()
    )


def apply_undo(db: Session, entry: UndoLog) -> str:
    """
    Entry'i geri al.  Başarılıysa entry.undone_at set + tablo değiştirilmiş;
    caller commit eder.  Conflict/gone durumunda exception fırlat.
    Dönen string: kullanıcıya gösterilecek başarı mesajı.
    """
    handler = _UNDO_REGISTRY.get(entry.action_type)
    if not handler:
        raise UndoUnsupported(f"Action type '{entry.action_type}' undo edilemez.")
    msg = handler(db, entry)
    entry.undone_at = datetime.utcnow()
    return msg


# ──────────────────────────────────────────────────────────────────────────
# Action handlers — registry
# ──────────────────────────────────────────────────────────────────────────

def _undo_stock_adjust(db: Session, entry: UndoLog) -> str:
    """
    Payload: { item_id, before_stock, after_stock, transaction_id }

    Geri alma:
        1) Item.current_stock değeri hâlâ after_stock mu?  Değilse conflict.
        2) Item.current_stock = before_stock
        3) Oluşturulan Transaction satırı sil (audit trail bilerek bozulur —
           kullanıcı işlemi geri aldıysa o transaction "olmamış" olur).
    """
    p = entry.payload or {}
    item_id        = p.get("item_id")
    before_stock   = float(p.get("before_stock", 0.0))
    after_stock    = float(p.get("after_stock",  0.0))
    transaction_id = p.get("transaction_id")

    item = db.query(Item).filter(Item.id == item_id).with_for_update().first()
    if not item:
        raise UndoTargetGone("Ürün artık yok.")

    cur = float(item.current_stock or 0.0)
    # Float eşitliği güvenli olsun diye küçük tolerans
    if abs(cur - after_stock) > 1e-6:
        raise UndoConflict(
            f"Stok bu işlemden sonra başkası tarafından değiştirilmiş "
            f"(şu an {cur}, beklenen {after_stock})."
        )

    item.current_stock = round(before_stock, 6)

    # İlgili Transaction satırını sil — audit'i tutmak istersen ileride
    # "reverted_by_undo_id" sütunu ekleriz; şimdilik temizliyoruz.
    if transaction_id:
        db.query(Transaction).filter(Transaction.id == transaction_id).delete()

    return (
        f"Stok geri alındı: {after_stock} → {before_stock} "
        f"{item.unit or ''} ({item.name})."
    )


def _undo_item_edit(db: Session, entry: UndoLog) -> str:
    """
    Payload: {
        item_id,
        before: { name, category, unit, min_stock_level, cost_price,
                  parent_id, variation_name, barcode, pkg_type, supplier_id }
    }

    Geri alma: Item kolonlarını before'a göre geri yaz.
    Çakışma: bilinçli olarak gevşek — son N saniyede ardarda 2 edit varsa
    son undo eski 'before'u uygular (kabul edilebilir trade-off, kullanıcı
    göstermek istediği state'i geri istiyor).
    """
    p = entry.payload or {}
    item_id = p.get("item_id")
    before  = p.get("before") or {}

    item = db.query(Item).filter(Item.id == item_id).with_for_update().first()
    if not item:
        raise UndoTargetGone("Ürün artık yok.")

    for col in ("name", "category", "unit", "barcode", "pkg_type",
                "variation_name", "language", "label_group"):
        if col in before:
            setattr(item, col, before[col])
    for col in ("min_stock_level", "cost_price"):
        if col in before and before[col] is not None:
            setattr(item, col, float(before[col]))
    if "parent_id" in before:
        item.parent_id = before["parent_id"]
    if "supplier_id" in before:
        item.supplier_id = before["supplier_id"]

    return f"Ürün düzenlemesi geri alındı: {item.name}"


def _undo_inventory_receive(db: Session, entry: UndoLog) -> str:
    """
    Payload: {
        inventory_id, transaction_id, item_id,
        received_quantity, item_stock_before, item_stock_after,
        lot_number,
    }

    Geri alma:
        1) Item.current_stock hâlâ item_stock_after mı? Değilse conflict.
        2) Inventory satırı hâlâ duruyor + quantity = received_quantity mi?
           Yoksa kısmi tüketim olmuş, undo riskli → conflict.
        3) Inventory satırını sil, Transaction satırını sil,
           Item.current_stock = item_stock_before.
    """
    p = entry.payload or {}
    inventory_id      = p.get("inventory_id")
    transaction_id    = p.get("transaction_id")
    item_id           = p.get("item_id")
    received_quantity = float(p.get("received_quantity", 0.0))
    stock_before      = float(p.get("item_stock_before", 0.0))
    stock_after       = float(p.get("item_stock_after",  0.0))
    lot_number        = p.get("lot_number")

    item = db.query(Item).filter(Item.id == item_id).with_for_update().first()
    if not item:
        raise UndoTargetGone("Ürün artık yok.")

    cur = float(item.current_stock or 0.0)
    if abs(cur - stock_after) > 1e-6:
        raise UndoConflict(
            f"Bu lot kabulünden sonra ürün stoğu başka işlemle değişti "
            f"(şu an {cur}, beklenen {stock_after})."
        )

    inv = db.query(Inventory).filter(Inventory.id == inventory_id).first()
    if inv:
        # Lot tüketilmiş mi?
        if abs(float(inv.quantity or 0.0) - received_quantity) > 1e-6:
            raise UndoConflict(
                f"Lot ({lot_number}) kısmen tüketilmiş, geri alınamaz."
            )
        db.delete(inv)

    if transaction_id:
        db.query(Transaction).filter(Transaction.id == transaction_id).delete()

    item.current_stock = round(stock_before, 6)

    return (
        f"Lot kabulü geri alındı: {lot_number or '—'} "
        f"({received_quantity} {item.unit or ''} {item.name})."
    )


_UNDO_REGISTRY = {
    "stock_adjust":      _undo_stock_adjust,
    "item_edit":         _undo_item_edit,
    "inventory_receive": _undo_inventory_receive,
}


def supported_action_types() -> list[str]:
    return list(_UNDO_REGISTRY.keys())
