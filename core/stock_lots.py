# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Lot farkındalıklı stok düşümü — TEK KAYNAK.

NEDEN VAR (09.09.2026 tespiti)
─────────────────────────────
`Item.current_stock` ile `Inventory` lot satırları İKİ AYRI SAYAÇTI ve yalnız
biri güncelleniyordu.  Bitmiş üründe lot satırı açan tek yol üretim çıktısı ve
mal kabuldü; teslimat (`routers/delivery.py` içinde `Inventory` kelimesi hiç
geçmiyordu), Shopify satışı ve elle stok düzeltmesi SADECE `current_stock`'u
hareket ettiriyordu.  Sonuç: lotlar bir kez açılıp bir daha hiç azalmıyor.

Sahada ölçüldü: 25 bitmiş üründe toplam 274 adetlik sapma.  Örnek — Serenida
Bikini Area 200 ml: defter 0 diyor (doğru), lot tablosu 15 adet gösteriyor;
25.06 üretiminden sonraki 11 çıkışın hiçbiri lottan düşmemiş.

DEFTER KURALI KORUNUR
─────────────────────
`core/snapshots.py` yalnız Input/Output/Adjustment sayar ve Transaction satırı
ASLA silinmez/taşınmaz.  Bu modül lot başına AYRI Output yazar; toplam yine
istenen miktara eşittir, yani rekonstrüksiyon değişmez — sadece
`Transaction.lot_number` dolduğu için izlenebilirlik kazanılır
(`routers/production.py`'nin hammadde tüketiminde yıllardır yaptığı şeyin
aynısı; FIFO motoru artık ikisinde de burada).

LOT YETMEZSE ENGELLEMEZ
───────────────────────
Lot verisi eksik olan eski kalemlerde (ör. hiç lotu olmayan hammadde) artık
`uncovered` olarak lotsuz tek Output ile yazılır.  `current_stock` her zaman
tam miktar kadar düşer — otorite odur, laboratuvarın işi durmaz.
"""
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from database import Inventory, Item, Transaction

EPS = 1e-9

#: FIFO sırasında atlanan lot durumları — yalnız APPROVED lot tüketilir.
APPROVED = "APPROVED"


def plan_fifo(
    db: Session,
    item: Item,
    qty: float,
    *,
    exclude_samples: bool = True,
    lock: bool = True,
) -> Tuple[List[Tuple[Inventory, float]], float]:
    """`qty` kadar stoğun hangi lotlardan düşeceğini planla — en eski önce.

    Dönüş: ``(allocations, uncovered)``
      • allocations — ``[(Inventory satırı, düşülecek miktar), …]``
      • uncovered   — lot kaydıyla karşılanamayan artık (0 ise tam karşılandı)

    Satırlar ``with_for_update()`` ile kilitlenir; aynı anda iki teslimat aynı
    lottan düşemez.  (joinedload(supplier) ile BİRLEŞTİRİLMEZ — PostgreSQL
    "FOR UPDATE cannot be applied to the nullable side of an outer join"
    hatası verir; tedarikçi gerekiyorsa tüketim aşamasında lazy yüklenir.)

    `exclude_samples=True` varsayılan: numune lotu (`is_sample`) satışa/
    teslimata KONU OLAMAZ — 24.08.2026 numune olayının kuralı.
    """
    qty = float(qty or 0.0)
    if qty <= EPS:
        return [], 0.0

    q = (
        db.query(Inventory)
        .filter(
            Inventory.item_id == item.id,
            Inventory.status == APPROVED,
            Inventory.quantity > 0,
        )
        .order_by(Inventory.created_at.asc(), Inventory.id.asc())
    )
    if exclude_samples:
        q = q.filter(Inventory.is_sample == False)      # noqa: E712
    lots = q.with_for_update().all() if lock else q.all()

    allocations: List[Tuple[Inventory, float]] = []
    remaining = qty
    for lot in lots:
        if remaining <= EPS:
            break
        take = min(float(lot.quantity or 0.0), remaining)
        if take > EPS:
            allocations.append((lot, round(take, 6)))
            remaining -= take
    return allocations, max(0.0, round(remaining, 6))


def draw_down(
    db: Session,
    item: Item,
    qty: float,
    *,
    exclude_samples: bool = True,
) -> Tuple[List[Tuple[str, float]], float]:
    """Yalnız LOT satırlarını FIFO düş — Transaction YAZMAZ, stoğa DOKUNMAZ.

    Çağıran kendi defter kaydını yazar.  Elle stok düzeltmesi (`adjust_stock`)
    bunu kullanır: orada defter kaydı tek bir imzalı `Adjustment` olmak
    zorundadır (birleştirme script'leri bu konvansiyona dayanıyor), ama lot
    satırları da gerçeği izlemelidir.

    Dönüş: ``([(lot_number, düşülen), …], uncovered)``
    """
    allocations, uncovered = plan_fifo(
        db, item, qty, exclude_samples=exclude_samples)
    touched: List[Tuple[str, float]] = []
    for lot, take in allocations:
        lot.quantity = round(float(lot.quantity or 0.0) - take, 6)
        touched.append((lot.lot_number or "—", take))
    return touched, uncovered


def consume(
    db: Session,
    item: Item,
    qty: float,
    *,
    note: str,
    actor: str,
    exclude_samples: bool = True,
    apply_stock: bool = True,
) -> List[Tuple[str, float]]:
    """Stok çıkışının TAMAMI — lot düş + Output yaz + `current_stock` düş.

    Teslimat, kargo, proforma onayı ve Shopify satışı bu tek yoldan geçer.

    • Her lot tahsisi için AYRI `Transaction(Output)` (lot_number dolu) yazılır;
      lotla karşılanamayan artık için lotsuz tek Output.  Yazılan Output'ların
      TOPLAMI daima `qty`'dir → defter rekonstrüksiyonu değişmez.
    • `apply_stock=False` yalnız çağıran `current_stock`'u kendi güncelliyorsa
      kullanılır (geri uyum kancası; normalde dokunma).

    Dönüş: ``[(lot_number|'—', miktar), …]`` — belge/nota basmak için.
    """
    qty = round(float(qty or 0.0), 6)
    if qty <= EPS:
        return []

    allocations, uncovered = plan_fifo(
        db, item, qty, exclude_samples=exclude_samples)

    used: List[Tuple[str, float]] = []
    for lot, take in allocations:
        lot.quantity = round(float(lot.quantity or 0.0) - take, 6)
        sup = lot.supplier.name if getattr(lot, "supplier", None) else None
        detail = f"{note} | Kaynak Lot: {lot.lot_number}"
        if sup:
            detail += f" | Tedarikçi: {sup}"
        db.add(Transaction(
            item_id=item.id, transaction_type="Output", quantity=take,
            lot_number=lot.lot_number, notes=detail[:500], performed_by=actor,
        ))
        used.append((lot.lot_number or "—", take))

    if uncovered > EPS:
        # Lot kaydı yetmedi (eski/lotsuz kalem).  Stok yine tam düşer; artığı
        # lotsuz Output olarak yaz ki toplam = qty kalsın.
        db.add(Transaction(
            item_id=item.id, transaction_type="Output", quantity=uncovered,
            notes=f"{note} | (lot kaydı dışı, toplam stoktan)"[:500],
            performed_by=actor,
        ))
        used.append(("—", uncovered))

    if apply_stock:
        item.current_stock = round(float(item.current_stock or 0.0) - qty, 6)
    return used


def lot_summary(used: List[Tuple[str, float]]) -> str:
    """`consume()` çıktısını tek satırlık nota çevir: ``MNR006×3 · SR004×1``."""
    return " · ".join(f"{lot}×{qty:g}" for lot, qty in used) if used else "—"
