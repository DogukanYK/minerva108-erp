# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Üretimi iptal et — tüketilen her kalemi stoğa iade, bitmiş ürünü stoktan düş.

NEDEN VAR (07.10.2026)
──────────────────────
Songül Hanım (LabLead): "başlattığım üretimi nasıl iptal edeceğim, yanlışlık
oldu."  Sistemde iptal yoktu; `ProductionHistory` neyin tüketildiğini de
saklamıyordu.  Artık `start_production` her Transaction'ı
`production_consumptions` satırına döker (source='live'); bu modül o dökümü
tersine çevirir.

DEFTER KURALI (core/snapshots.py)
─────────────────────────────────
Hiçbir Transaction SİLİNMEZ/DEĞİŞTİRİLMEZ.  Her tüketim satırı için +Adjustment
(lot_number = kaynak lot), bitmiş ürün için orijinal Input'u aynalayan TEK
−Adjustment yazılır → `compute_stock_at` üretim öncesi bir an için aynı sonucu
verir (telafi net sıfır).  Notlar `NOTE_MARK` ile başlar — "Stok düzeltme"
(numune çevirme koruması elle sayım sayar) ve "Lot taşındı" ile BAŞLAMAZ.

ESKİ ÜRETİMLER (dökümü olmayan) — defterden yeniden kurma
──────────────────────────────────────────────────────────
Lot numaraları ürün bazlıdır: SR005 birden çok üründe vardır.  Bu yüzden lot
no'ya göre değil, üretimin KENDİ imzasına göre eşlenir: Output notu
``"Üretim tüketimi — Reçete: {ad} | "`` ile başlar VE ``"Üretim Lot: {lot}"``
ile BİTER (SR0051 eşleşmez; ham madde notu da ambalaj notu da bu sonekle
biter — routers/production.py'deki üç not biçimi), `performed_by` üretimi
başlatan kişidir, zaman `produced_at −10 dk / +1 dk` penceresindedir, kart
aynı paneldedir ve tx başka bir üretimin dökümünde geçmez.  İptal anında bu
satırlar `source='ledger'` olarak KALICI yazılır.

TUZAK — imza tekil DEĞİLDİR: `Recipe.name` benzersiz değil (kart yeniden
adlandırma / force ile açılan kopya kart) ve lot sayacı ürün bazlı → aynı
kişinin aynı dakikalarda aynı reçete ADI + aynı lot no'lu İKİ eski üretimi
olabilir; ikisinin Output'ları imzadan ayrılamaz (çift iade).  Bu durum
`_ambiguity()` ile ENGEL olur; ayrıca bir Output kendi Input'undan SONRA
yazılmış olamaz (start_production Output'ları Input'tan önce ekler).

Engeller (iptali durduran) ve uyarılar `check()`'te; önizleme ile POST
arasında hiçbir şey değişmediğini `fingerprint` kanıtlar (değiştiyse 409).
Commit ÇAĞIRANA aittir.
"""
import hashlib
import re
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlalchemy import or_, select, union
from sqlalchemy.orm import Session

from core import lots as lots_mod
from core.consumption import _kind as consumption_kind
from core.item_merge import _adjust
from core.stock_lots import LOT_MOVE_MARK
from database import (Delivery, DeliveryItem, Inventory, Item, ProductionConsumption,
                      ProductionHistory, RetentionSample, RetentionSampleMovement,
                      Transaction, to_tr)

EPS = 1e-6
NOTE_MARK = "Üretim iptali"
WINDOW_BEFORE = timedelta(minutes=10)
WINDOW_AFTER = timedelta(minutes=1)
STALE_DAYS = 7                                   # bundan eski üretimde uyarı (engel değil)
CONSUME_KINDS = ("raw", "packaging", "label")
KIND_LABELS = {"raw": "hammadde", "packaging": "ambalaj", "label": "etiket",
               "output": "bitmiş ürün"}
# Şahit dolabında iptale engel OLMAYAN hareketler — dolap içi, stok yazmaz.
RETENTION_SAFE_MOVES = ("giris", "konum", "kontrol")
NO_LEDGER_OUTPUTS = "Tüketim kayıtları defterde bulunamadı — otomatik iptal yapılamaz."
AMBIGUOUS_LEDGER = ("Aynı kişinin aynı dakikalarda aynı reçete adı ve aynı lot no ile başka bir "
                    "üretimi daha var — defter satırları birbirinden ayrılamıyor, otomatik "
                    "iptal yapılamaz.")

_PRD_LOT_RE = re.compile(r"Üretim Lot: (PRD-\d{8}-\d{6})$")
_FIRE_RE = re.compile(r"\| %([\d.]+) fire dahil")
_SUPPLIER_RE = re.compile(r"\| Tedarikçi: (.*?)(?: \(numune\))? \| Kaynak Lot:")
_SPLIT_RE = re.compile(r"Showroom: ([\d.]+), Şahit: ([\d.]+)")


class CancelError(Exception):
    """Kullanıcıya gösterilebilir iptal engeli (core/stock_lots.LotMoveError
    kalıbı).  `status` HTTP kodu, `extra` yanıt gövdesine eklenir
    (ör. ``code='preview_stale'``)."""

    def __init__(self, status: int, detail: str, **extra):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra

    def payload(self) -> dict:
        return {"detail": self.detail, **self.extra}


class Rows(list):
    """`consumption_rows()` çıktısı: ProductionConsumption satırları + kaynak.

    `problems` — defterden yeniden kurmada eşlenemeyen parçalar (engel olur);
    `lot` — çözülen üretim lotu (PRD- lotlu eski üretimde nottan okunur)."""

    def __init__(self, rows=(), *, source: str = "snapshot", problems=(), lot=None):
        super().__init__(rows)
        self.source = source
        self.problems = list(problems)
        self.lot = lot


# ─── Yardımcılar ─────────────────────────────────────────────────────────────

def cancelled_view(prod: ProductionHistory) -> Optional[dict]:
    """Liste/föy/izlenebilirlik için iptal bilgisi — iptal değilse None."""
    if not prod.cancelled_at:
        return None
    return {"at": to_tr(prod.cancelled_at).strftime("%d.%m.%Y %H:%M"),
            "by": prod.cancelled_by or "—",
            "reason": prod.cancel_reason or "",
            "lot_released": bool(prod.lot_released)}


def _by(col, value):
    return col.is_(None) if value is None else col == value


def _window(prod: ProductionHistory):
    return prod.produced_at - WINDOW_BEFORE, prod.produced_at + WINDOW_AFTER


def _wide_window(prod: ProductionHistory):
    """Başka bir üretimin Output'larının bu üretimin penceresine düşebileceği
    `produced_at` aralığı (iki pencere çakışıyor) — belirsizlik taraması."""
    span = WINDOW_BEFORE + WINDOW_AFTER
    return prod.produced_at - span, prod.produced_at + span


def _ambiguity(db: Session, prod: ProductionHistory, lot: str, own_input_id: Optional[int],
               claimed_tx: set) -> bool:
    """Bu üretimin defter imzasını (reçete adı + lot + kişi + pencere) taşıyan
    BAŞKA bir eski üretim var mı?

    • Dökümü olmayan başka bir ProductionHistory: aynı recipe_name, lot_number,
      produced_by ve panel; produced_at genişletilmiş pencerede.  (Dökümü
      olanın tx'leri zaten sahiplenilmiştir, karışmaz.)
    • Ya da hedefi ne olursa olsun aynı imzalı başka bir "Üretim çıktısı"
      Input'u (sahiplenilmemiş) — PH kaydı olmasa bile defter ikinci bir
      üretimi gösteriyor."""
    wlo, whi = _wide_window(prod)
    has_rows = (select(ProductionConsumption.id)
                .where(ProductionConsumption.production_id == ProductionHistory.id)
                .exists())
    other_ph = (db.query(ProductionHistory.id)
                .filter(ProductionHistory.id != prod.id,
                        ProductionHistory.recipe_name == prod.recipe_name,
                        ProductionHistory.lot_number == lot,
                        _by(ProductionHistory.produced_by, prod.produced_by),
                        ProductionHistory.domain == (prod.domain or "cosmetics"),
                        ProductionHistory.produced_at >= wlo,
                        ProductionHistory.produced_at <= whi,
                        ~has_rows)
                .first())
    if other_ph is not None:
        return True
    prefix = f"Üretim çıktısı — Reçete: {prod.recipe_name or ''} |"
    for tx in (db.query(Transaction).join(Item, Item.id == Transaction.item_id)
               .filter(Transaction.transaction_type == "Input",
                       Item.domain == (prod.domain or "cosmetics"),
                       Transaction.lot_number == lot,
                       Transaction.timestamp >= wlo, Transaction.timestamp <= whi,
                       _by(Transaction.performed_by, prod.produced_by))
               .all()):
        if (tx.id != own_input_id and tx.id not in claimed_tx
                and (tx.notes or "").startswith(prefix)):
            return True
    return False


def _claimed(db: Session, production_id: int) -> Tuple[set, set]:
    """BAŞKA üretimlerin dökümünde geçen (tx id'leri, bitmiş inventory id'leri)
    — yeniden kurma bunları sahiplenmez (iki üretim aynı satırı paylaşamaz)."""
    tx_ids, inv_ids = set(), set()
    for tx_id, ctx_id, inv_id, kind in (
            db.query(ProductionConsumption.transaction_id,
                     ProductionConsumption.cancel_transaction_id,
                     ProductionConsumption.inventory_id, ProductionConsumption.kind)
            .filter(ProductionConsumption.production_id != production_id).all()):
        if tx_id:
            tx_ids.add(tx_id)
        if ctx_id:
            tx_ids.add(ctx_id)
        if kind == "output" and inv_id:
            inv_ids.add(inv_id)
    return tx_ids, inv_ids


def _source_lot(db: Session, item_id: int, lot_number: str, produced_at) -> Optional[Inventory]:
    """Eski Output'un kaynak lot satırı: aynı kart + lot no, numune değil,
    üretimden önce açılmış EN YENİ satır.  Kartta yoksa lot başka karta
    taşınmış olabilir (`moved_from_item_id`)."""
    base = (db.query(Inventory)
            .filter(Inventory.lot_number == lot_number,
                    Inventory.is_sample == False,                   # noqa: E712
                    Inventory.created_at <= produced_at)
            .order_by(Inventory.created_at.desc(), Inventory.id.desc()))
    return (base.filter(Inventory.item_id == item_id).first()
            or base.filter(Inventory.moved_from_item_id == item_id).first())


def _lot_moved_away(db: Session, item_id: int, lot_number: str, since) -> bool:
    """Kaynak lot üretimden sonra bu karttan "Karta taşı" ile çıkmış mı?
    (core/stock_lots.move_lot — kaynak kartta lot no'lu −Adjustment).  Satır
    hedefte aynı lot no'lu ikize katılıp silindiyse lotun bugünkü yeri
    bilinmez; iade eski karta lotsuz yazılırsa orada hayalî stok oluşurdu."""
    return db.query(Transaction.id).filter(
        Transaction.item_id == item_id,
        Transaction.transaction_type == "Adjustment",
        Transaction.lot_number == lot_number,
        Transaction.quantity < 0,
        Transaction.notes.like(f"{LOT_MOVE_MARK}%"),
        Transaction.timestamp >= since).first() is not None


def _note_factor(notes: str) -> float:
    m = _FIRE_RE.search(notes or "")
    try:
        return 1.0 + float(m.group(1)) / 100.0 if m else 1.0
    except ValueError:
        return 1.0


def _note_supplier(notes: str) -> Optional[str]:
    m = _SUPPLIER_RE.search(notes or "")
    sup = m.group(1).strip() if m else ""
    return sup if sup and sup != "—" else None


# ─── Döküm: snapshot ya da defterden yeniden kurma ──────────────────────────

def reconstruct_from_ledger(db: Session, prod: ProductionHistory) -> Rows:
    """Dökümü olmayan eski üretimin satırlarını defterden kur — KALICI YAZMAZ.

    Satırlar oturuma eklenmemiş `ProductionConsumption` nesneleridir
    (source='ledger'); `apply_cancel` iptal anında ekler."""
    problems: List[str] = []
    if not prod.produced_at:
        return Rows(source="ledger", problems=["Üretim zamanı kayıtlı değil — otomatik iptal yapılamaz."])
    lo, hi = _window(prod)
    dom = prod.domain or "cosmetics"
    claimed_tx, claimed_inv = _claimed(db, prod.id)

    prefix = f"Üretim tüketimi — Reçete: {prod.recipe_name or ''} | "
    cands = [tx for tx in (
        db.query(Transaction).join(Item, Item.id == Transaction.item_id)
        .filter(Transaction.transaction_type == "Output",
                Transaction.timestamp >= lo, Transaction.timestamp <= hi,
                Item.domain == dom,
                _by(Transaction.performed_by, prod.produced_by))
        .order_by(Transaction.id.asc()).all())
        if (tx.notes or "").startswith(prefix) and tx.id not in claimed_tx]

    lot = prod.lot_number
    if lot:
        suffix = f"Üretim Lot: {lot}"
        outs = [tx for tx in cands if (tx.notes or "").rstrip().endswith(suffix)]
    else:
        # Lot no'su kaydedilmemiş eski üretim (PRD- biçimi) — pencerede TEK
        # böyle lot varsa o; yoksa belirsiz, engel.
        by_lot = {}
        for tx in cands:
            m = _PRD_LOT_RE.search((tx.notes or "").rstrip())
            if m:
                by_lot.setdefault(m.group(1), []).append(tx)
        if len(by_lot) == 1:
            lot, outs = next(iter(by_lot.items()))
        else:
            outs = []
            if len(by_lot) > 1:
                problems.append("Lot numarası kayıtlı olmayan eski üretim — aynı anda birden "
                                "çok PRD- lotu bulundu, hangisi olduğu belirsiz.")
    if not outs:
        problems.append(NO_LEDGER_OUTPUTS)

    # ── Bitmiş ürün Input'u — Output'lardan ÖNCE bulunur: imza belirsizliği
    #    ve "Output kendi Input'undan önce yazılır" kontrolü ona dayanır.
    inputs, inp = [], None
    if prod.target_item_id and lot:
        inputs = [tx for tx in (
            db.query(Transaction)
            .filter(Transaction.item_id == prod.target_item_id,
                    Transaction.transaction_type == "Input",
                    Transaction.lot_number == lot,
                    Transaction.timestamp >= lo, Transaction.timestamp <= hi,
                    _by(Transaction.performed_by, prod.produced_by))
            .order_by(Transaction.id.asc()).all())
            if (tx.notes or "").startswith(f"Üretim çıktısı — Reçete: {prod.recipe_name or ''} |")
            and tx.id not in claimed_tx]
        if len(inputs) == 1:
            inp = inputs[0]
    if prod.lot_number and _ambiguity(db, prod, lot, inp.id if inp is not None else None,
                                      claimed_tx):
        problems.append(AMBIGUOUS_LEDGER)
    if inp is not None:
        late = [tx.id for tx in outs if tx.id > inp.id]
        if late:
            problems.append(f"Defter kaydı #{late[0]} üretim çıktısından (#{inp.id}) sonra "
                            f"yazılmış — başka bir üretime ait olabilir, otomatik iptal "
                            f"yapılamaz.")

    rows: List[ProductionConsumption] = []
    items = {}
    if outs:
        items = {it.id: it for it in db.query(Item).filter(
            Item.id.in_({tx.item_id for tx in outs})).all()}
    for tx in outs:
        item = items.get(tx.item_id)
        kind = consumption_kind(item) if item is not None else "raw"
        inv = _source_lot(db, tx.item_id, tx.lot_number, prod.produced_at) if tx.lot_number else None
        sup_id = inv.supplier_id if inv is not None else None
        sup_name = (inv.supplier.name if inv is not None and inv.supplier is not None
                    else _note_supplier(tx.notes))
        rows.append(ProductionConsumption(
            production_id=prod.id, kind=kind, recipe_item_id=None, item_id=tx.item_id,
            inventory_id=inv.id if inv is not None else None, lot_number=tx.lot_number,
            supplier_id=sup_id, supplier_name=sup_name, quantity=tx.quantity,
            factor=_note_factor(tx.notes) if kind == "raw" else 1.0,
            unit=item.unit if item is not None else None, phase=None,
            transaction_id=tx.id, source="ledger", domain=dom))

    # ── Bitmiş ürün: tek Input + showroom / -S satırları ──
    if prod.target_item_id and lot:
        if inp is None:
            problems.append(f"Üretim çıktısı (Input) defterde {'bulunamadı' if not inputs else 'birden çok eşleşti'}"
                            f" — otomatik iptal yapılamaz.")
        else:
            produced = float(prod.produced_quantity or 0.0)
            witness = float(prod.witness_quantity or 0.0)
            if witness <= EPS:
                m = _SPLIT_RE.search(inp.notes or "")
                if m:
                    witness = float(m.group(2))
            showroom = round(produced - witness, 6)
            if abs(float(inp.quantity or 0.0) - produced) > EPS:
                problems.append(f"Üretim çıktısı miktarı ({inp.quantity:g}) üretim kaydıyla "
                                f"({produced:g}) uyuşmuyor — otomatik iptal yapılamaz.")
            fin = [inv for inv in (
                db.query(Inventory)
                .filter(Inventory.item_id == prod.target_item_id,
                        Inventory.lot_number.in_([lot, f"{lot}-S"]),
                        Inventory.created_at >= lo, Inventory.created_at <= hi,
                        _by(Inventory.received_by, prod.produced_by))
                .order_by(Inventory.id.asc()).all())
                if inv.id not in claimed_inv]
            unit = None
            if prod.target_item_id:
                tgt = db.query(Item).filter(Item.id == prod.target_item_id).first()
                unit = tgt.unit if tgt else None
            for lot_name, qty in ((lot, showroom), (f"{lot}-S", witness)):
                if qty <= EPS:
                    continue
                match = [inv for inv in fin if inv.lot_number == lot_name]
                if len(match) != 1:
                    problems.append(f"Bitmiş ürün lotu {lot_name} defterde "
                                    f"{'bulunamadı' if not match else 'birden çok eşleşti'} — "
                                    f"otomatik iptal yapılamaz.")
                    continue
                rows.append(ProductionConsumption(
                    production_id=prod.id, kind="output", recipe_item_id=None,
                    item_id=prod.target_item_id, inventory_id=match[0].id, lot_number=lot_name,
                    quantity=qty, factor=1.0, unit=unit, transaction_id=inp.id,
                    source="ledger", domain=dom))
    elif prod.target_item_id and not lot:
        problems.append("Bitmiş ürün lotu belirlenemedi — otomatik iptal yapılamaz.")

    return Rows(rows, source="ledger", problems=problems, lot=lot)


def consumption_rows(db: Session, prod: ProductionHistory) -> Tuple[Rows, str]:
    """(satırlar, kaynak) — döküm varsa o ('snapshot'), yoksa defterden
    yeniden kurulmuş satırlar ('ledger')."""
    rows = (db.query(ProductionConsumption)
            .filter(ProductionConsumption.production_id == prod.id)
            .order_by(ProductionConsumption.id.asc()).all())
    if rows:
        return Rows(rows, source="snapshot", lot=prod.lot_number), "snapshot"
    out = reconstruct_from_ledger(db, prod)
    return out, "ledger"


# ─── Kilitler ────────────────────────────────────────────────────────────────

def lock_rows(db: Session, prod: ProductionHistory, rows: Rows) -> None:
    """İlgili kartlar (id sıralı) → lot satırları (id artan) → şahit kaydı
    FOR UPDATE.  `populate_existing` — kilitten sonra güncel değer okunur."""
    inv_ids = sorted({r.inventory_id for r in rows if r.inventory_id})
    item_ids = {r.item_id for r in rows}
    if prod.target_item_id:
        item_ids.add(prod.target_item_id)
    if inv_ids:
        item_ids |= {iid for (iid,) in db.query(Inventory.item_id)
                     .filter(Inventory.id.in_(inv_ids)).all()}
    if item_ids:
        (db.query(Item).filter(Item.id.in_(item_ids)).order_by(Item.id.asc())
         .with_for_update().populate_existing().all())
    if inv_ids:
        locked = (db.query(Inventory).filter(Inventory.id.in_(inv_ids))
                  .order_by(Inventory.id.asc()).with_for_update().populate_existing().all())
        # Okuma ile kilit arasında lot başka karta geçtiyse o kartı da kilitle.
        late = {inv.item_id for inv in locked} - item_ids
        if late:
            (db.query(Item).filter(Item.id.in_(late)).order_by(Item.id.asc())
             .with_for_update().populate_existing().all())
    out_inv = [r.inventory_id for r in rows if r.kind == "output" and r.inventory_id]
    if out_inv:
        (db.query(RetentionSample).filter(RetentionSample.inventory_id.in_(out_inv))
         .order_by(RetentionSample.id.asc()).with_for_update().populate_existing().all())


# ─── Denetim: engeller + uyarılar + iade/düşüm planı ────────────────────────

def _dedupe(msgs: List[str]) -> List[str]:
    seen, out = set(), []
    for m in msgs:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def check(db: Session, prod: ProductionHistory, rows: Rows) -> dict:
    """Engeller, uyarılar ve iptalin yapacağı iş.

    Dönüş: ``{blockers, warnings, restore, remove, retention, fingerprint,
    _plan}`` — `_plan` iç kullanım (ORM nesneleri), yanıta konmaz."""
    source = getattr(rows, "source", "snapshot")
    blockers: List[str] = list(getattr(rows, "problems", []))
    warnings: List[str] = []
    lot = getattr(rows, "lot", None) or prod.lot_number
    cons = [r for r in rows if r.kind in CONSUME_KINDS]
    outs = [r for r in rows if r.kind == "output"]

    if prod.cancelled_at:
        blockers.append("Bu üretim zaten iptal edilmiş.")
    if source == "ledger" and not cons and NO_LEDGER_OUTPUTS not in blockers:
        blockers.append(NO_LEDGER_OUTPUTS)

    # ── Tüketim defter satırları hâlâ dökümle aynı mı ──
    tx_map = {}
    tx_ids = [r.transaction_id for r in cons if r.transaction_id]
    if tx_ids:
        tx_map = {t.id: t for t in db.query(Transaction).filter(Transaction.id.in_(tx_ids)).all()}
    for r in cons:
        t = tx_map.get(r.transaction_id) if r.transaction_id else None
        if (t is None or t.transaction_type != "Output" or t.item_id != r.item_id
                or abs(float(t.quantity or 0.0) - float(r.quantity or 0.0)) > EPS):
            blockers.append(f"Defter kaydı #{r.transaction_id or '—'} tüketim dökümüyle "
                            f"uyuşmuyor — otomatik iptal yapılamaz.")

    # ── İade planı: hangi karta / lota ──
    inv_ids = {r.inventory_id for r in cons if r.inventory_id}
    invs = ({i.id: i for i in db.query(Inventory).filter(Inventory.id.in_(inv_ids)).all()}
            if inv_ids else {})
    card_ids = {r.item_id for r in cons} | {i.item_id for i in invs.values()}
    cards = ({i.id: i for i in db.query(Item).filter(Item.id.in_(card_ids)).all()}
             if card_ids else {})
    restore_plan, restore = [], []
    for r in cons:
        consumed = cards.get(r.item_id)
        if consumed is None:
            blockers.append(f"Tüketilen kart (#{r.item_id}) bulunamadı — otomatik iptal yapılamaz.")
            continue
        card, inv, note = consumed, None, None
        if r.lot_number:
            inv = invs.get(r.inventory_id) if r.inventory_id else None
            if inv is not None and inv.item_id != consumed.id:
                card = cards.get(inv.item_id)
                if card is None:
                    blockers.append(f"Lot {r.lot_number} bugün bulunamayan bir kartta — "
                                    f"otomatik iptal yapılamaz.")
                    continue
                note = (f"Lot {r.lot_number} «{consumed.name}» kartından «{card.name}» "
                        f"kartına taşınmış — iade lotun bugünkü kartına yapılacak.")
                warnings.append(note)
            elif inv is None:
                if prod.produced_at and _lot_moved_away(db, consumed.id, r.lot_number,
                                                        prod.produced_at - WINDOW_BEFORE):
                    blockers.append(f"Kaynak lot {r.lot_number} «{consumed.name}» kartından başka "
                                    f"bir karta taşınıp oradaki aynı lotla birleşmiş — iadenin "
                                    f"gideceği lot belirsiz, otomatik iptal yapılamaz.")
                    continue
                if not consumed.is_active:
                    blockers.append(f"«{consumed.name}» kartı pasif ve kaynak lot {r.lot_number} "
                                    f"artık yok (kart birleştirilmiş olabilir) — otomatik iptal "
                                    f"yapılamaz.")
                    continue
                note = (f"«{consumed.name}» kaynak lotu {r.lot_number} artık yok — yalnız kart "
                        f"stoğuna iade edilecek.")
                warnings.append(note)
        if not card.is_active:
            # Pasif kart = kopya kart birleştirmesinin kaybedeni ya da silinmiş
            # kart.  Birleştirmede stok kazanana taşındı; iade pasif karta
            # yazılırsa fiziksel iade kazananın rafına gider ama sistemde
            # görünmez stok birikir (listeler/compute_stock_at pasifi saymaz).
            blockers.append(f"«{card.name}» kartı pasif (kopya kart birleştirmesi ya da silme) — "
                            f"iadenin gideceği kart belirsiz, otomatik iptal yapılamaz.")
            continue
        restore_plan.append((r, card, inv))
        row = {"item_id": card.id, "consumed_item_id": consumed.id, "name": card.name,
               "kind": r.kind, "kind_label": KIND_LABELS.get(r.kind, r.kind),
               "lot": r.lot_number, "inventory_id": inv.id if inv is not None else None,
               "supplier": r.supplier_name or "", "qty": round(float(r.quantity or 0.0), 6),
               "unit": card.unit or r.unit or "", "tx_id": r.transaction_id}
        if note:
            row["target_card_note"] = note
        restore.append(row)

    # ── Bitmiş ürün satırları ──
    target = (db.query(Item).filter(Item.id == prod.target_item_id).first()
              if prod.target_item_id else None)
    produced = float(prod.produced_quantity or 0.0)
    finished, remove = [], []
    input_tx_ids = sorted({r.transaction_id for r in outs if r.transaction_id})
    if prod.target_item_id:
        if target is None:
            blockers.append("Hedef ürün kartı bulunamadı — otomatik iptal yapılamaz.")
        if not outs and source != "ledger":
            blockers.append("Bitmiş ürün dökümü yok — otomatik iptal yapılamaz.")
        if outs and abs(sum(float(o.quantity or 0.0) for o in outs) - produced) > EPS:
            blockers.append("Bitmiş ürün dökümü üretim miktarıyla uyuşmuyor — otomatik iptal "
                            "yapılamaz.")
        if len(input_tx_ids) > 1:
            blockers.append("Bitmiş ürün dökümü birden çok Input'a bağlı — otomatik iptal "
                            "yapılamaz.")
        for o in outs:
            inv = (db.query(Inventory).filter(Inventory.id == o.inventory_id).first()
                   if o.inventory_id else None)
            if inv is None:
                blockers.append(f"Bitmiş ürün lotu {o.lot_number} kaydı bulunamadı — otomatik "
                                f"iptal yapılamaz.")
                continue
            st = inv.status or "APPROVED"
            if inv.item_id != prod.target_item_id:
                blockers.append(f"Bitmiş ürün lotu {inv.lot_number} başka bir karta geçmiş — "
                                f"otomatik iptal yapılamaz.")
            if st == "REJECTED":
                blockers.append("QC'de reddedilmiş üretim iptal edilemez — hammadde fiilen "
                                "harcandı.")
            elif st == "CANCELLED":
                blockers.append(f"Bitmiş ürün lotu {inv.lot_number} zaten iptal edilmiş.")
            elif st != "APPROVED":
                blockers.append(f"Bitmiş ürün lotu {inv.lot_number} durumu '{st}' — iptal "
                                f"edilemez.")
            if abs(float(inv.quantity or 0.0) - float(o.quantity or 0.0)) > EPS:
                blockers.append(f"Bitmiş ürün lotu {inv.lot_number} miktarı değişmiş (üretimde "
                                f"{o.quantity:g}, bugün {float(inv.quantity or 0.0):g}) — "
                                f"sonradan stok hareketi görmüş.")
            finished.append((inv, o))
            remove.append({"inventory_id": inv.id, "lot": inv.lot_number,
                           "location": inv.location or "—",
                           "qty": round(float(o.quantity or 0.0), 6)})

        # Bitmiş lotlarda üretimden SONRA stok hareketi (teslimat, sevkiyat,
        # proforma, Shopify — stock_lots.consume; şahit çıkış/imha; QC reddi).
        if lot and input_tx_ids:
            others, _ = _claimed(db, prod.id)
            others |= {r.cancel_transaction_id for r in rows if r.cancel_transaction_id}
            later = [t for t in (
                db.query(Transaction)
                .filter(Transaction.item_id == prod.target_item_id,
                        Transaction.lot_number.in_([lot, f"{lot}-S"]),
                        Transaction.transaction_type.in_(("Input", "Output", "Adjustment")),
                        Transaction.id > input_tx_ids[0])
                .order_by(Transaction.id.asc()).all())
                if t.id not in input_tx_ids and t.id not in others]
            for t in later[:5]:
                when = to_tr(t.timestamp).strftime("%d.%m.%Y %H:%M") if t.timestamp else "—"
                blockers.append(f"Bitmiş lotta sonradan stok hareketi var: #{t.id} "
                                f"{t.transaction_type} {t.quantity:g} ({when}) — "
                                f"{(t.notes or '')[:80]}")
            if len(later) > 5:
                blockers.append(f"… ve {len(later) - 5} hareket daha.")
        if target is not None and float(target.current_stock or 0.0) + EPS < produced:
            blockers.append(f"«{target.name}» stoğu ({float(target.current_stock or 0.0):g}) "
                            f"üretilen miktardan ({produced:g}) az — bitmiş ürün kullanılmış.")

    # ── Şahit numune ──
    retention_obj, retention = None, None
    for o in outs:
        if not (o.lot_number or "").endswith("-S") or not o.inventory_id:
            continue
        rs = (db.query(RetentionSample)
              .filter(RetentionSample.inventory_id == o.inventory_id).first())
        if rs is None:
            warnings.append("Şahit numunenin dolap kaydı bulunamadı — yalnız stok lotu "
                            "kapatılacak.")
            continue
        q = float(o.quantity or 0.0)
        if not rs.is_active:
            blockers.append("Şahit numune dolap kaydı silinmiş — otomatik iptal yapılamaz.")
        if rs.status != "stored":
            blockers.append(f"Şahit numune dolapta değil (durum: {rs.status}) — iptal "
                            f"edilemez.")
        if (abs(float(rs.quantity or 0.0) - q) > EPS
                or abs(float(rs.initial_quantity or 0.0) - q) > EPS):
            blockers.append(f"Şahit numune adedi değişmiş (üretimde {q:g}, dolapta "
                            f"{float(rs.quantity or 0.0):g}) — iptal edilemez.")
        if rs.item_id != prod.target_item_id:
            blockers.append(f"Şahit numunenin boyu düzeltilmiş («{rs.item_name or rs.item_id}») — "
                            f"otomatik iptal yapılamaz.")
        moved = sorted({m.movement_type for m in rs.movements
                        if m.movement_type not in RETENTION_SAFE_MOVES})
        if moved:
            blockers.append(f"Şahit numune hareket görmüş ({', '.join(moved)}) — otomatik "
                            f"iptal yapılamaz.")
        if any(m.movement_type == "kontrol" for m in rs.movements):
            warnings.append("Şahit numunenin kontrol kayıtları var — kayıt dolaptan kalkacak.")
        retention_obj = rs
        retention = {"id": rs.id, "qty": q, "lot": rs.lot_number, "brand": rs.brand}

    # ── Uyarılar ──
    if prod.target_item_id:
        docs = [d for (d,) in (
            db.query(Delivery.document_no).join(DeliveryItem, DeliveryItem.delivery_id == Delivery.id)
            .filter(DeliveryItem.item_id == prod.target_item_id,
                    Delivery.domain == (prod.domain or "cosmetics"),
                    or_(Delivery.status == "pending", Delivery.status == "preparing"))
            .distinct().all())]
        if docs:
            warnings.append(f"Bu ürünü içeren bekleyen proforma / hazırlanan sevkiyat var: "
                            f"{', '.join(d or '—' for d in docs[:5])}.")
    if prod.produced_at and datetime.utcnow() - prod.produced_at > timedelta(days=STALE_DAYS):
        warnings.append(f"Üretim {STALE_DAYS} günden eski — iptalden önce fiziki stoğu "
                        f"kontrol edin.")
    if source == "ledger":
        warnings.append("Bu üretimin tüketim dökümü yok (özellik öncesi) — liste defterden "
                        "yeniden kuruldu; iade satırlarını kontrol edin.")

    plan = {"source": source, "lot": lot, "restore": restore_plan, "finished": finished,
            "retention": retention_obj, "target": target, "input_tx_ids": input_tx_ids}
    return {
        "blockers": _dedupe(blockers),
        "warnings": _dedupe(warnings),
        "restore": restore,
        "remove": remove,
        "retention": retention,
        "fingerprint": fingerprint(prod, plan),
        "_plan": plan,
    }


def fingerprint(prod: ProductionHistory, plan: dict) -> str:
    """Önizleme ile POST arasında değişmemesi gerekenlerin özeti (sha1):
    sıralı tüketim tx'leri + iade kartı/lotu + bitmiş satırlar (id, adet,
    durum) + şahit (id, adet) + kaynak."""
    parts = [f"p:{prod.id}", f"s:{plan['source']}", f"l:{plan['lot'] or ''}"]
    for r, card, inv in sorted(plan["restore"], key=lambda x: (x[0].transaction_id or 0)):
        parts.append(f"c:{r.transaction_id}:{card.id}:{inv.id if inv is not None else '-'}:"
                     f"{round(float(r.quantity or 0.0), 6)}")
    for inv, _o in sorted(plan["finished"], key=lambda x: x[0].id):
        parts.append(f"f:{inv.id}:{round(float(inv.quantity or 0.0), 6)}:{inv.status}")
    rs = plan["retention"]
    if rs is not None:
        parts.append(f"r:{rs.id}:{round(float(rs.quantity or 0.0), 6)}:{rs.item_id}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


# ─── Önizleme + uygulama ────────────────────────────────────────────────────

def preview(db: Session, prod: ProductionHistory) -> dict:
    """GET cancel-preview yanıtı — salt okur, hiçbir şey yazmaz/kilitlemez."""
    rows, source = consumption_rows(db, prod)
    if prod.cancelled_at:
        # Zaten iptal — denetim çalıştırılmaz (kapanmış satırlar engel üretirdi).
        res = {"restore": [], "remove": [], "retention": None, "warnings": [],
               "blockers": ["Bu üretim zaten iptal edilmiş."], "fingerprint": ""}
    else:
        res = check(db, prod, rows)
    return {
        "production": {
            "id": prod.id, "lot": getattr(rows, "lot", None) or prod.lot_number,
            "recipe": prod.recipe_name or "", "target": prod.target_item_name or "",
            "target_item_id": prod.target_item_id,
            "qty": prod.produced_quantity, "witness": prod.witness_quantity or 0.0,
            "produced_at": (to_tr(prod.produced_at).strftime("%d.%m.%Y %H:%M")
                            if prod.produced_at else ""),
            "by": prod.produced_by or "",
        },
        "source": source,
        "restore": res["restore"],
        "remove": res["remove"],
        "retention": res["retention"],
        "blockers": res["blockers"],
        "warnings": res["warnings"],
        "fingerprint": res["fingerprint"],
        "can_cancel": not res["blockers"],
        "cancelled": cancelled_view(prod),
    }


def apply_cancel(db: Session, prod: ProductionHistory, *, actor: str, reason: str,
                 release_lot: bool = False, expected_fingerprint: Optional[str] = None) -> dict:
    """İptali uygula — commit ÇAĞIRANA aittir.  `prod` çağıranca FOR UPDATE
    kilitlenmiş olmalı.  Dökümü yeniden hesaplar, kilitler, denetler;
    `expected_fingerprint` farklıysa 409 ``preview_stale``, engel varsa 409
    ``blocked`` (`CancelError`).  Dönüş: özet (audit + yanıt)."""
    if prod.cancelled_at:
        raise CancelError(409, "Bu üretim zaten iptal edilmiş.", code="already_cancelled")
    reason = (reason or "").strip()
    rows, source = consumption_rows(db, prod)
    lock_rows(db, prod, rows)
    res = check(db, prod, rows)
    if expected_fingerprint is not None and expected_fingerprint != res["fingerprint"]:
        raise CancelError(409, "Önizlemeden bu yana stok değişti — önizlemeyi yenileyip tekrar "
                               "deneyin.", code="preview_stale", fingerprint=res["fingerprint"])
    if res["blockers"]:
        raise CancelError(409, "Bu üretim iptal edilemiyor.", code="blocked",
                          blockers=res["blockers"])

    plan = res["_plan"]
    lot = plan["lot"]
    now = datetime.utcnow()
    head = (f"{NOTE_MARK} — #{prod.id} Lot {lot or '—'} | "
            f"Reçete: {prod.recipe_name or '—'}")

    if source == "ledger":
        for r in rows:                      # yeniden kurulan döküm KALICI olur
            db.add(r)

    # ── Tüketim iadesi — satır başına +Adjustment (kaynak lot no'lu) ──
    cancel_txs = []
    for r, card, inv in plan["restore"]:
        q = round(float(r.quantity or 0.0), 6)
        note = (f"{head} | İade: {KIND_LABELS.get(r.kind, r.kind)} | "
                f"Kaynak Lot: {r.lot_number or '—'}")
        if card.id != r.item_id:
            note += f" | Kart: «{card.name}» (tüketilen #{r.item_id})"
        note += f" | Sebep: {reason}"
        # Lot taşınırken adı değişmiş olabilir — telafi kaydı, miktarı artan
        # satırın BUGÜNKÜ lot no'sunu taşır (kaynak lot notta).
        tx = _adjust(db, card, q, note, actor,
                     lot_number=inv.lot_number if inv is not None else r.lot_number)
        if inv is not None:
            inv.quantity = round(float(inv.quantity or 0.0) + q, 6)
            inv.updated_at = now
        if tx is not None:
            r.cancel_transaction = tx
            cancel_txs.append(tx)

    # ── Bitmiş ürün — Input'u aynalayan TEK −Adjustment ──
    target = plan["target"]
    if target is not None and plan["finished"]:
        split = ", ".join(f"{o.lot_number}: {float(o.quantity or 0.0):g}"
                          for _inv, o in plan["finished"])
        produced = round(sum(float(o.quantity or 0.0) for _inv, o in plan["finished"]), 6)
        tx = _adjust(db, target, -produced,
                     f"{head} | Bitmiş ürün geri alındı ({split}) | Sebep: {reason}",
                     actor, lot_number=lot)
        if tx is not None:
            cancel_txs.append(tx)
        stamp = f"[{NOTE_MARK} #{prod.id} — {actor}: {reason}]"
        for inv, o in plan["finished"]:
            inv.quantity = 0.0
            inv.status = "CANCELLED"
            inv.qc_required = False
            inv.qc_notes = (f"{inv.qc_notes}\n{stamp}" if inv.qc_notes else stamp)
            inv.updated_at = now
            if tx is not None:
                o.cancel_transaction = tx

    # ── Şahit numune dolabı ──
    rs = plan["retention"]
    if rs is not None:
        qty = float(rs.quantity or 0.0)
        rs.quantity = 0.0
        rs.is_active = False
        db.add(RetentionSampleMovement(
            sample_id=rs.id, movement_type="duzeltme", quantity=qty,
            note=f"{NOTE_MARK} #{prod.id} — {reason}"[:500], performed_by=actor))

    # ── Üretim kaydı damgası + lot no ──
    prod.cancelled_at = now
    prod.cancelled_by = (actor or "—")[:100]
    prod.cancel_reason = reason
    prod.lot_released = bool(release_lot)
    seq_rolled_back = False
    if release_lot and target is not None and lot:
        seq = lots_mod.parse_sequence(lot, lots_mod.lot_prefix(target))
        if seq and int(target.lot_seq or 0) == seq:
            target.lot_seq = seq - 1
            seq_rolled_back = True

    db.flush()
    return {
        "production_id": prod.id,
        "lot": lot,
        "recipe": prod.recipe_name,
        "qty": prod.produced_quantity,
        "source": source,
        "restored": len(plan["restore"]),
        "tx_ids": sorted({r.transaction_id for r in rows if r.transaction_id}),
        "cancel_tx_ids": sorted({t.id for t in cancel_txs}),
        "release_lot": bool(release_lot),
        "lot_seq_rolled_back": seq_rolled_back,
        "warnings": res["warnings"],
    }


# ─── Raporlar için ───────────────────────────────────────────────────────────

def cancelled_tx_ids_subq(db: Session):
    """İptal edilmiş üretimlerin defter satırları (tüketim/çıktı tx'leri ∪
    telafi tx'leri) — raporlardan dışlamak için ``~Transaction.id.in_(…)``.

    NULL'lar dışarıda — `NOT IN (… NULL …)` her satırı elerdi."""
    pc, ph = ProductionConsumption, ProductionHistory
    a = (select(pc.transaction_id.label("tx_id"))
         .join(ph, ph.id == pc.production_id)
         .where(ph.cancelled_at.isnot(None), pc.transaction_id.isnot(None)))
    b = (select(pc.cancel_transaction_id.label("tx_id"))
         .join(ph, ph.id == pc.production_id)
         .where(ph.cancelled_at.isnot(None), pc.cancel_transaction_id.isnot(None)))
    sq = union(a, b).subquery()
    return select(sq.c.tx_id)


def cancelled_tx_ids(db: Session) -> set:
    """`cancelled_tx_ids_subq`'un Python kümesi (bellekte süzen raporlar için)."""
    return {tid for (tid,) in db.execute(cancelled_tx_ids_subq(db)).all()}
