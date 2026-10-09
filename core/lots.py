# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Üretim lot numarası — öneri ve sayaç.

Biçim: `{MARKA_KODU}{SIRA}` → **MNR006 · SR005 · EV012** (labın fiilen
kullandığı düzen).  Sayaç ÜRÜN BAZLIDIR: dark spot'tan 5 üretildiyse sonraki
MNR006, clay mask'ten 4 üretildiyse MNR005.  Yani lot kodu ürünü TANIMLAMAZ —
farklı ürünler aynı kodu taşıyabilir; ürün + lot birlikte anahtardır.
(Eski üretimler `PRD-YYYYMMDD-HHMMSS` biçiminde; iki biçim yan yana yaşar.)

Sayaç iki kaynaktan beslenir ve büyüğü kazanır:
  1. `Item.lot_seq` — hızlı sayaç
  2. `scan_max_seq()` — geçmiş tarama (SELF-HEAL): kullanıcı elle "MNR020"
     yazarsa sayaç geride kalsa bile sonraki öneri MNR021 olur.

Eşzamanlılık: çağıran `routers/production.py`, hedef `Item` satırını
`with_for_update()` ile KİLİTLEDİKTEN sonra `next_sequence()` çağırmalıdır.
Aynı ürünün iki eşzamanlı üretimi böylece sıraya girer.  (Bu kilit, bugünkü
"aynı saniyede iki üretim = aynı PRD- lotu" hatasını da kapatır.)
"""
import re
from datetime import date, datetime
from typing import Optional

from sqlalchemy import and_, not_, or_
from sqlalchemy.orm import Session

from core.brands import lot_code
from database import B2BOrderBatch, Inventory, Item, ProductionHistory, to_tr, tr_now

# ─── Lot SKT'si (Inventory.expiry_date metin) — TEK ayrıştırıcı ─────────────
# B2B sıkı havuzu, fason sevk, SKT kontrol raporu ve "Yaklaşan SKT" aynı
# biçimleri okur.  Okunamayan metin None'dır (geçerli SKT SAYILMAZ).
EXPIRY_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y")


def parse_expiry(text) -> Optional[date]:
    text = str(text or "").strip()
    for fmt in EXPIRY_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def expiry_today() -> date:
    """SKT karşılaştırmasının "bugün"ü — TR yerel takvim günü (sunucu UTC)."""
    return tr_now().date()


LOT_SEQ_PAD = 3          # MNR006 — dolgusuz istenirse 0 yapmak yeterli
_MAX_LOT_LEN = 100       # ProductionHistory.lot_number / Inventory.lot_number


def lot_prefix(item: Item) -> str:
    """Ürünün lot öneki — markadan türer (MNR / SR / EV)."""
    return lot_code(getattr(item, "name", "") or "")


def format_lot(prefix: str, seq: int) -> str:
    return f"{prefix}{int(seq):0{LOT_SEQ_PAD}d}" if LOT_SEQ_PAD else f"{prefix}{int(seq)}"


def normalize_lot(raw) -> str:
    """Kullanıcının elle girdiği lotu düzelt: trim + iç boşlukları at + BÜYÜK harf."""
    if raw is None:
        return ""
    return "".join(str(raw).split()).upper()[:_MAX_LOT_LEN]


def parse_sequence(lot: str, prefix: str) -> Optional[int]:
    """'MNR006' + 'MNR' → 6.  Önek uymuyor ya da kalan sayı değilse None."""
    if not lot or not prefix:
        return None
    m = re.fullmatch(re.escape(prefix) + r"(\d+)", normalize_lot(lot))
    return int(m.group(1)) if m else None


def _not_released():
    """İptal edilip lot no'su serbest bırakılmış üretimler SAYILMAZ
    (core/production_cancel — `lot_released`).  İptal edilip serbest
    bırakılMAMIŞ üretim lotu tutmaya devam eder (GMP varsayılanı)."""
    return not_(and_(ProductionHistory.cancelled_at.isnot(None),
                     ProductionHistory.lot_released == True))      # noqa: E712


def scan_max_seq(db: Session, item: Item, prefix: str) -> int:
    """Bu ÜRÜNE ait geçmiş lotlardaki en büyük sıra (yoksa 0).

    Yalnız hedef ürünün üretim geçmişine bakar — başka ürünün aynı önekli
    lotları sayacı ileri itmemeli (her ürünün kendi sayacı var).
    """
    rows = (db.query(ProductionHistory.lot_number)
            .filter(ProductionHistory.target_item_id == item.id,
                    ProductionHistory.lot_number.isnot(None),
                    ProductionHistory.lot_number.like(f"{prefix}%"),
                    _not_released())
            .all())
    best = 0
    for (lot,) in rows:
        seq = parse_sequence(lot, prefix)
        if seq and seq > best:
            best = seq
    return best


def next_sequence(db: Session, item: Item) -> int:
    """Bir sonraki sıra — sayaç ile geçmiş taramasının büyüğü + 1.

    ÖNEMLİ: çağırmadan önce `item` satırı with_for_update() ile kilitlenmiş
    olmalı (bkz. modül docstring'i).
    """
    prefix = lot_prefix(item)
    return max(int(item.lot_seq or 0), scan_max_seq(db, item, prefix)) + 1


def is_taken(db: Session, item_id: int, lot: str) -> bool:
    """Bu lot AYNI ÜRÜNDE kullanılmış mı?

    Farklı üründe aynı lot serbesttir (labın düzeni bunu gerektiriyor), bu
    yüzden kontrol item_id ile sınırlı.  Hem üretim geçmişine hem stok
    lotlarına bakılır — `-S` (şahit) türevi de çakışma sayılır.

    İptal edilip lot no'su serbest bırakılmış üretim ve iptalle kapanmış
    (`status='CANCELLED'`) stok satırları çakışma SAYILMAZ.
    """
    lot = normalize_lot(lot)
    if not lot:
        return False
    if (db.query(ProductionHistory.id)
            .filter(ProductionHistory.target_item_id == item_id,
                    ProductionHistory.lot_number == lot,
                    _not_released()).first()):
        return True
    # B2B sipariş partisi lot no'yu BAŞLATIRKEN alır; bitmiş ürün (geçmiş +
    # stok satırı) ancak tamamlanınca yazılır.  Arada aynı no elle verilmesin;
    # iptal edilen parti de (tüketim notlarında geçtiği için) no'yu bırakmaz.
    if (db.query(B2BOrderBatch.id)
            .filter(B2BOrderBatch.item_id == item_id,
                    B2BOrderBatch.lot_number == lot).first()):
        return True
    return bool(db.query(Inventory.id)
                .filter(Inventory.item_id == item_id,
                        Inventory.lot_number.in_([lot, f"{lot}-S"]),
                        or_(Inventory.status.is_(None),
                            Inventory.status != "CANCELLED")).first())


def last_production(db: Session, item: Item, prefix: str):
    """Bu ürünün önekle uyuşan EN SON üretimi (lot, tarih) — öneri metni için."""
    rows = (db.query(ProductionHistory.lot_number, ProductionHistory.produced_at)
            .filter(ProductionHistory.target_item_id == item.id,
                    ProductionHistory.lot_number.isnot(None),
                    ProductionHistory.lot_number.like(f"{prefix}%"),
                    _not_released())
            .order_by(ProductionHistory.produced_at.desc())
            .limit(50).all())
    best = None
    for lot, produced_at in rows:
        seq = parse_sequence(lot, prefix)
        if seq is None:
            continue
        if best is None or seq > best[0]:
            best = (seq, lot, produced_at)
    return (best[1], best[2]) if best else (None, None)


def suggest(db: Session, item: Item) -> dict:
    """Üretim ekranının gösterdiği öneri paketi."""
    prefix = lot_prefix(item)
    seq = next_sequence(db, item)
    lot = format_lot(prefix, seq)
    last_lot, last_at = last_production(db, item, prefix)
    if last_lot:
        when = to_tr(last_at).strftime("%d.%m.%Y") if last_at else ""
        msg = (f"Bu üründen en son {last_lot} numaralı lotu ürettiniz"
               + (f" ({when})" if when else "") + f" — bu {lot} olmalı.")
    elif seq > 1:
        # Üretim kaydı yok ama sayaç ilerlemiş — numara şahit numune
        # sayımından geliyor (lab sistemden önce üretmiş).  "İlk üretim"
        # demek yanıltıcı olurdu: EV009 önerirken "ilk üretim" yazıyordu.
        msg = (f"Sistemde bu ürünün üretim kaydı yok, ancak sayaç "
               f"{format_lot(prefix, seq - 1)} numarasında (şahit numune "
               f"sayımından) — bu {lot} olmalı.")
    else:
        msg = f"Bu üründen ilk üretim — lot {lot} olarak açılıyor."
    return {
        "lot_number": lot,
        "prefix": prefix,
        "sequence": seq,
        "last_lot": last_lot,
        "last_produced_at": to_tr(last_at).strftime("%d.%m.%Y") if last_at else "",
        "message": msg,
    }
