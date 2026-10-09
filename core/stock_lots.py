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
import math
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database import (Inventory, Item, ProductionConsumption, RetentionSample,
                      SampleAnalysisIngredient, Supplier, Transaction)

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
    reserved: Optional[Dict[int, float]] = None,
    released_only: bool = False,
    accept: Optional[Callable[[Inventory], bool]] = None,
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

    `reserved` — {inventory_id: miktar}: aynı planda ÖNCEKİ satırların bu
    lotlardan aldığı (henüz yazılmamış) miktar; lotun kullanılabilir kalanı
    bundan düşülür.  Üretim planlayıcısı (core/production_plan) aynı kartı
    birden çok satırda kullanırken aynı lotu iki kez vermesin diye.  Sözlük
    OKUNUR, güncellenmez — çağıran kendi tahsisini ekler.

    `accept` — lot başına ek koşul (SQL'e dökülemeyen; ör. B2B sipariş
    partisinin "geçerli SKT" şartı).  False dönen lot havuzda yokmuş gibi
    atlanır; kalan ihtiyaç `uncovered`'a düşer.
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
    if released_only:
        q = _released(db, q)
    lots = q.with_for_update().all() if lock else q.all()

    allocations: List[Tuple[Inventory, float]] = []
    remaining = qty
    for lot in lots:
        if remaining <= EPS:
            break
        if accept is not None and not accept(lot):
            continue
        avail = float(lot.quantity or 0.0)
        if reserved:
            avail -= float(reserved.get(lot.id, 0.0))
        take = round(min(avail, remaining), 6)
        if take > EPS:
            allocations.append((lot, take))
            remaining = round(remaining - take, 6)
    return allocations, max(0.0, round(remaining, 6))


def _released(db: Session, q):
    """Sevke uygun lot: QC kararı verilmiş (`qc_required` değil) ve şahit
    numune dolabına bağlı OLMAYAN.  Varsayılan havuz bunları da verir (iç
    üretim QC'den önce stoğa girer, dolaptaki şahit stokta sayılır — bilinçli
    karar); B2B sipariş sevkiyatı `released_only=True` ile istemez."""
    witness = (db.query(RetentionSample.inventory_id)
               .filter(RetentionSample.is_active == True,           # noqa: E712
                       RetentionSample.inventory_id.isnot(None)))
    return q.filter(Inventory.qc_required == False,                  # noqa: E712
                    ~Inventory.id.in_(witness))


def unreleased_quantity(db: Session, item_id: int) -> float:
    """Kart stoğunda sayılıp sevke uygun OLMAYAN: QC kararı bekleyen ya da
    şahit numune dolabına bağlı APPROVED lotlar (numune lotu zaten stok değil)."""
    witness = (db.query(RetentionSample.inventory_id)
               .filter(RetentionSample.is_active == True,           # noqa: E712
                       RetentionSample.inventory_id.isnot(None)))
    q = db.query(func.coalesce(func.sum(Inventory.quantity), 0.0)).filter(
        Inventory.item_id == item_id, Inventory.status == APPROVED,
        Inventory.quantity > 0, Inventory.is_sample == False,       # noqa: E712
        or_(Inventory.qc_required == True, Inventory.id.in_(witness)))  # noqa: E712
    return float(q.scalar() or 0.0)


def shippable_quantity(db: Session, item: Item) -> float:
    """B2B sipariş sevkiyatına uygun stok = kart stoğu − QC bekleyen − şahit.
    Lot kaydı olmayan eski stok (lotlar açılmadan önceki) uygundur: QC bekleyen
    ve şahit stok DAİMA lot satırıdır.  `consume(released_only=True)` önce
    `_released` lotlarını düşer, kalanı lotsuz Output yazar."""
    stock = float(item.current_stock or 0.0)
    return max(0.0, round(stock - unreleased_quantity(db, item.id), 6))


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
    released_only: bool = False,
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
        db, item, qty, exclude_samples=exclude_samples, released_only=released_only)

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


# ─── Lotu başka karta taşı (06.10.2026) ──────────────────────────────────────
#
# NEDEN VAR: Naturalya'dan gelen jojoba/portakal/lavanta numuneleri KRK GIDA ve
# İPEDA kartlarına girilmişti; "Stoğa çevir" stoğu yanlış tedarikçinin kartına
# yazdı.  Lab aynı malzemenin her tedarikçisini ayrı kart tutuyor
# (core/material_groups.py), lotun doğru karta geçmesi gerekiyor.
#
# DEFTER KURALI: normal lot Output/Input DEĞİL Adjustment ÇİFTİYLE taşınır
# (kaynak −q, hedef +q, ikisi de lot_number'lı; core/item_merge._adjust
# kalıbı).  Output/Input yazılsaydı aylık raporda sahte tüketim ve sahte alım
# görünürdü.  Notlar "Stok düzeltme" ile BAŞLAMAZ — numune çevirmedeki çift
# sayım koruması o öneki elle sayım sayar.  Numune lotu stok değildir:
# yalnız satır karta geçer, Transaction yazılmaz.

LOT_MOVE_MARK = "Lot taşındı"


class LotMoveError(Exception):
    """Kullanıcıya gösterilebilir taşıma/çevirme engeli.  `status` HTTP kodu,
    `extra` yanıt gövdesine eklenir (ör. ``code='lot_collision'``)."""

    def __init__(self, status: int, detail: str, **extra):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra

    def payload(self) -> dict:
        return {"detail": self.detail, **self.extra}


def lot_kind(category) -> str:
    """Kart türü — ``finished`` (Bitmiş Ürün) | ``packaging`` (Ambalaj) |
    ``raw`` (hammadde ve diğer her şey: Kimyasal, Yardımcı …)."""
    c = (category or "").strip()
    if c == "Bitmiş Ürün":
        return "finished"
    if c == "Ambalaj":
        return "packaging"
    return "raw"


def target_problem(source: Item, target: Item) -> Optional[str]:
    """Lot/numune `source` kartından `target` kartına geçebilir mi — kart
    kuralları.  Sorun varsa kullanıcı mesajı, yoksa None.

    Aynı panel, aktif hedef, aynı tür (hammadde↔hammadde, ambalaj↔ambalaj),
    Bitmiş Ürün ASLA, aynı birim ailesi (`core.purchase_plan.unit_norm` —
    g/gr aynı, g/kg farklı: miktar çevrilmeden taşınır)."""
    from core.purchase_plan import unit_norm
    if (target.domain or "cosmetics") != (source.domain or "cosmetics"):
        return "Hedef kart aktif panelde değil."
    if not target.is_active:
        return f"Hedef kart pasif: {target.name}"
    if "finished" in (lot_kind(source.category), lot_kind(target.category)):
        return "Bitmiş ürün kartları arasında lot taşınamaz."
    if lot_kind(source.category) != lot_kind(target.category):
        return "Hammadde ile ambalaj kartları arasında lot taşınamaz."
    if unit_norm(source.unit) != unit_norm(target.unit):
        return (f"Birimler farklı ({source.unit or '—'} → {target.unit or '—'}) — "
                f"aynı birimli bir kart seçin.")
    return None


def find_twin(db: Session, item_id: int, lot_number: str, *, is_sample: bool,
              exclude_id: Optional[int] = None) -> Optional[Inventory]:
    """Hedef kartta aynı lot no'lu, aynı türden (numune/normal) satır — upsert
    anahtarı (item_id, lot_number, is_sample) ile aynı."""
    q = db.query(Inventory).filter(Inventory.item_id == item_id,
                                   Inventory.lot_number == lot_number,
                                   Inventory.outsourcing_receipt_id.is_(None),
                                   Inventory.is_sample == bool(is_sample))
    if exclude_id:
        q = q.filter(Inventory.id != exclude_id)
    return q.with_for_update().first()


def suppliers_compatible(a: Optional[int], b: Optional[int]) -> bool:
    """Aynı lot no iki satırda birleşebilir mi — tedarikçi aynı ya da biri boş."""
    return a is None or b is None or a == b


def collision_error(db: Session, twin: Inventory, lot_number: str) -> LotMoveError:
    """409 ``lot_collision`` — istemci `lot_number` ile tekrar dener."""
    sup = db.query(Supplier).filter(Supplier.id == twin.supplier_id).first() \
        if twin.supplier_id else None
    return LotMoveError(
        409,
        f"Hedef kartta {lot_number} lot numarası başka bir tedarikçiyle kayıtlı — "
        f"bu lot için farklı bir lot numarası girin.",
        code="lot_collision", lot_number=lot_number,
        existing={"inventory_id": twin.id, "supplier_id": twin.supplier_id,
                  "supplier_name": sup.name if sup else None,
                  "quantity": round(float(twin.quantity or 0.0), 4)})


def redirect_analysis_rows(db: Session, inventory_id: int, *, item_id: Optional[int] = None,
                           to_inventory_id: Optional[int] = None) -> int:
    """Numune Analizi bileşen satırlarını lotla birlikte götür.

    `SampleAnalysisIngredient.item_id` lotun kartını izlemeli — yoksa
    core/sample_trial_stock._consume "lot bu hammaddeye ait değil" der,
    `_release` iadeyi YANLIŞ karta Adjustment olarak yazar.  `to_inventory_id`
    birleşmede silinecek satırın yerine geçen satırdır (FK ondelete=SET NULL
    yüzünden eskiden bağ NULL'a düşüyordu)."""
    rows = (db.query(SampleAnalysisIngredient)
            .filter(SampleAnalysisIngredient.inventory_id == inventory_id).all())
    for r in rows:
        if item_id is not None:
            r.item_id = item_id
        if to_inventory_id is not None:
            r.inventory_id = to_inventory_id
    return len(rows)


def redirect_production_rows(db: Session, inventory_id: int, to_inventory_id: int) -> int:
    """Üretim tüketim dökümünü (`production_consumptions`) birleşmede silinecek
    satırdan yerine geçen satıra yönlendir.

    FK ondelete=SET NULL yüzünden bağ NULL'a düşerdi; üretim iptali
    (core/production_cancel) lotu "artık yok" sayıp iadeyi ESKİ karta lotsuz
    yazardı — eski kartta lotu olmayan hayalî stok, lotun bugünkü kartı eksik."""
    rows = (db.query(ProductionConsumption)
            .filter(ProductionConsumption.inventory_id == inventory_id).all())
    for r in rows:
        r.inventory_id = to_inventory_id
    return len(rows)


def absorb_row(db: Session, row: Inventory, twin: Inventory, *, item_id: int) -> int:
    """`row`'un TAMAMINI aynı lot no'lu `twin` satırına kat ve `row`'u sil.

    Tedarikçi/SKT `twin`'de boşsa `row`'dan dolar (COALESCE — mal kabul
    upsert'üyle aynı kural).  Analiz ve üretim dökümü satırları silmeden
    ÖNCE `twin`'e yönlenir.  Dönüş: yönlenen analiz satırı sayısı."""
    twin.quantity = round(float(twin.quantity or 0.0) + float(row.quantity or 0.0), 6)
    if twin.supplier_id is None and row.supplier_id is not None:
        twin.supplier_id = row.supplier_id
    if not twin.expiry_date and row.expiry_date:
        twin.expiry_date = row.expiry_date
    twin.updated_at = datetime.utcnow()
    n = redirect_analysis_rows(db, row.id, item_id=item_id, to_inventory_id=twin.id)
    redirect_production_rows(db, row.id, twin.id)
    # Fason kabı kaynak lotuna FK ile bağlı; silinen satır yerine ikizi gösterir.
    # Kabın kaynak snapshot'ı (lot/tedarikçi/SKT) değişmez — sevk zamanı
    # doğrulaması fark varsa yeniden teknik hazırlık ister.
    from database import OutsourcingContainer
    (db.query(OutsourcingContainer).filter(OutsourcingContainer.inventory_id == row.id)
     .update({OutsourcingContainer.inventory_id: twin.id}, synchronize_session=False))
    db.flush()                                   # bağlar silmeden önce yazılsın
    db.delete(row)
    return n


def move_sample_row(db: Session, lot: Inventory, target: Item) -> int:
    """Satırı olduğu gibi `target` kartına geçir — Transaction YOK, stok YOK.

    Numune çevirmede hedef farklı kartsa, numune lotu taşımada ve normal lotun
    tam taşımasında (defter çifti `move_lot`'ta yazılır) kullanılır.
    `moved_from_item_id` lotun İLK kartıdır, zaten doluysa korunur.  Analiz
    satırları yeni karta yönlenir.  Dönüş: yönlenen analiz satırı sayısı."""
    if lot.item_id == target.id:
        return 0
    if lot.moved_from_item_id is None:
        lot.moved_from_item_id = lot.item_id
    n = redirect_analysis_rows(db, lot.id, item_id=target.id)
    lot.item_id = target.id
    lot.updated_at = datetime.utcnow()
    return n


def move_lot(
    db: Session,
    lot: Inventory,
    source: Item,
    target: Item,
    qty: Optional[float],
    *,
    actor: str,
    reason: str,
    supplier_id: Optional[int] = None,
    lot_number: Optional[str] = None,
) -> dict:
    """Lotu (ya da normal lotun bir kısmını) `target` kartına taşı.  Commit
    ÇAĞIRANA aittir; satırlar çağıranca kilitlenmiş olmalı.

    • Normal lot — Adjustment çifti (kaynak −q / hedef +q, lot no'lu) + iki
      kartın `current_stock`'u.  Tam taşımada satır karta geçer; kısmide
      lottan düşülür (FIFO DEĞİL — bu lot) ve hedefte yeni satır açılır:
      tedarikçi, SKT, konum, statü, QC formu/notu, kabul eden ve `created_at`
      kopyalanır (FIFO yaşı bozulmasın).
    • Numune lotu — yalnız tam taşıma; satır karta geçer, defter/stok yok.
    • Hedefte aynı lot no'lu aynı türden satır varsa: tedarikçi uyumluysa
      birleşir, değilse 409 ``lot_collision`` (istemci `lot_number` verir).

    Engeller `LotMoveError` fırlatır.  Dönüş: özet dict (audit + yanıt)."""
    problem = target_problem(source, target)
    if problem:
        raise LotMoveError(400, problem)
    if target.id == source.id:
        raise LotMoveError(400, "Lot zaten bu kartta.")
    if lot.outsourcing_receipt_id:
        raise LotMoveError(400, "Fason kabul lotunun kaynak kartı değiştirilemez; fason kabul kaydı üzerinden izlenir.")
    if (lot.status or APPROVED) != APPROVED or lot.qc_required:
        raise LotMoveError(400, "QC kararı bekleyen lot taşınamaz.")
    if db.query(RetentionSample.id).filter(RetentionSample.inventory_id == lot.id).first():
        raise LotMoveError(400, "Bu lot şahit numune dolabına bağlı — taşınamaz.")

    is_sample = bool(lot.is_sample)
    unit = source.unit or ""
    have = round(float(lot.quantity or 0.0), 6)
    q = have if qty is None else round(float(qty), 6)
    # NaN her karşılaştırmada False döner → aşağıdaki sınırların hepsini atlayıp
    # iki karta NaN Adjustment yazardı (defter değişmez, rekonstrüksiyon kalıcı
    # NaN).  JSON gövdesi `NaN`'ı taşıyabilir; kapı burada (400).
    if not math.isfinite(q):
        raise LotMoveError(400, "Taşınacak miktar geçerli bir sayı olmalı.")
    if q <= EPS:
        raise LotMoveError(400, "Taşınacak miktar sıfırdan büyük olmalı.")
    if q > have + EPS:
        raise LotMoveError(400, f"Lotta {have:g} {unit} var; {q:g} {unit} taşınamaz.")
    partial = q < have - EPS
    if is_sample and partial:
        raise LotMoveError(400, "Numune lotu yalnız bütün olarak taşınabilir.")
    stock = float(source.current_stock or 0.0)
    if not is_sample and stock + EPS < q:
        raise LotMoveError(400, f"«{source.name}» kartının stoğu ({stock:g} {unit}) taşınacak "
                                f"miktardan az — önce sayım düzeltmesi yapın.")

    old_lot = lot.lot_number
    new_lot = (lot_number or "").strip() or old_lot
    if len(new_lot) > 100:
        raise LotMoveError(400, "Lot numarası en fazla 100 karakter.")
    eff_sup = supplier_id if supplier_id is not None else lot.supplier_id
    twin = find_twin(db, target.id, new_lot, is_sample=is_sample, exclude_id=lot.id)
    if twin is not None and not suppliers_compatible(twin.supplier_id, eff_sup):
        raise collision_error(db, twin, new_lot)

    sup = db.query(Supplier).filter(Supplier.id == eff_sup).first() if eff_sup else None
    sup_name = sup.name if sup else "—"
    origin = lot.moved_from_item_id or source.id
    now = datetime.utcnow()
    analysis_rows = 0

    if not is_sample:
        from core.item_merge import _adjust
        lot_txt = old_lot if new_lot == old_lot else f"{old_lot} → {new_lot}"
        _adjust(db, source, -q,
                f"{LOT_MOVE_MARK} → «{target.name}» (id {target.id}) | Lot: {lot_txt} | "
                f"Tedarikçi: {sup_name} | Sebep: {reason}", actor, lot_number=old_lot)
        _adjust(db, target, q,
                f"{LOT_MOVE_MARK} ← «{source.name}» (id {source.id}) | Lot: {lot_txt} | "
                f"Tedarikçi: {sup_name} | Sebep: {reason}", actor, lot_number=new_lot)

    if partial:
        lot.quantity = round(have - q, 6)
        lot.updated_at = now
        if twin is not None:
            twin.quantity = round(float(twin.quantity or 0.0) + q, 6)
            if twin.supplier_id is None:
                twin.supplier_id = eff_sup
            if not twin.expiry_date and lot.expiry_date:
                twin.expiry_date = lot.expiry_date
            twin.updated_at = now
            result = twin
        else:
            result = Inventory(
                item_id=target.id, supplier_id=eff_sup, lot_number=new_lot,
                expiry_date=lot.expiry_date, quantity=q, location=lot.location,
                status=lot.status, qc_notes=lot.qc_notes, qc_form_data=lot.qc_form_data,
                received_by=lot.received_by, qc_approved_by=lot.qc_approved_by,
                qc_required=False, is_sample=False, domain=lot.domain,
                created_at=lot.created_at, updated_at=now, moved_from_item_id=origin,
                # çevrilmiş numune izi parçayla gider — lot no değişirse
                # core/purchase_pricing (kart, lot) Input'unu bulamaz
                sample_converted_at=lot.sample_converted_at)
            db.add(result)
    else:
        lot.supplier_id = eff_sup
        if twin is not None:
            analysis_rows = absorb_row(db, lot, twin, item_id=target.id)
            result = twin
        else:
            analysis_rows = move_sample_row(db, lot, target)
            lot.lot_number = new_lot
            result = lot
    db.flush()

    return {
        "inventory_id": result.id, "lot_number": new_lot, "old_lot_number": old_lot,
        "quantity": q, "unit": target.unit or "", "partial": partial,
        "merged": twin is not None, "is_sample": is_sample,
        "source_item_id": source.id, "target_item_id": target.id,
        "supplier_id": eff_sup, "supplier_name": sup.name if sup else None,
        "analysis_rows": analysis_rows,
    }
