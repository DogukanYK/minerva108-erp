# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Şahit numune dolabı — saf yardımcılar (DB session/query YOK).

Kavram: her üretimden birkaç adet, markasına ayrılmış dolapta saklanır.  Bu
adetler **satılabilir stoktan düşmez** — bugünkü davranış korunur, dolapta
durdukları sürece `Item.current_stock` içinde sayılırlar.  Yalnızca fiilen
tüketildiklerinde (çıkış) veya imha edildiklerinde stoktan da düşerler.

Saklama süresi: `retention_until = üretim tarihi + raf ömrü + ek süre`.
İki değer de AppSetting'ten okunur (`retention.shelf_life_months` = 24,
`retention.extra_months` = 6 → 30 ay), yani deploysuz değiştirilebilir.
Hesaplanan tarih satıra YAZILIR; sonradan tek tek düzeltilebilir.
"""
from calendar import monthrange
from datetime import date, datetime
from typing import Optional

# AppSetting anahtarları + varsayılanlar
CFG_SHELF_LIFE = "retention.shelf_life_months"
CFG_EXTRA = "retention.extra_months"
DEFAULT_SHELF_LIFE_MONTHS = 24
DEFAULT_EXTRA_MONTHS = 6

# Numune neden çıkarıldı (dolaptan alınma gerekçesi)
CHECKOUT_REASONS = ("test", "musteri", "denetim", "lab", "diger")
REASON_LABELS = {
    "test":    "Analiz / test",
    "musteri": "Müşteri şikayeti",
    "denetim": "Denetim / resmi talep",
    "lab":     "Laboratuvar incelemesi",
    "diger":   "Diğer",
}

MOVEMENT_TYPES = ("giris", "cikis", "imha", "konum", "duzeltme")
MOVEMENT_LABELS = {
    "giris":    "Dolaba yerleştirildi",
    "cikis":    "Dolaptan çıkarıldı",
    "imha":     "İmha edildi",
    "konum":    "Konum değişti",
    "duzeltme": "Düzeltme",
}

STATUS_LABELS = {
    "stored":    "Dolapta",
    "depleted":  "Tükendi",
    "destroyed": "İmha edildi",
}

# Saklama bitişine bu kadar gün kalınca "yaklaşıyor" sayılır (UI rozeti)
DUE_SOON_DAYS = 90


def add_months(d: date, months: int) -> date:
    """Takvimsel ay ekleme — ayın son gününü taşırmaz (31 Ocak + 1 ay = 28/29 Şubat).

    `dateutil` bağımlılığı eklememek için elle yazıldı (repo'da yok).
    """
    if not months:
        return d
    total = d.month - 1 + int(months)
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def retention_until(produced_at, shelf_life_months=None, extra_months=None) -> Optional[date]:
    """Saklama bitiş tarihi.  `produced_at` yoksa None."""
    if produced_at is None:
        return None
    d = produced_at.date() if isinstance(produced_at, datetime) else produced_at
    shelf = DEFAULT_SHELF_LIFE_MONTHS if shelf_life_months is None else int(shelf_life_months)
    extra = DEFAULT_EXTRA_MONTHS if extra_months is None else int(extra_months)
    return add_months(d, shelf + extra)


def expiry_state(until: Optional[date], today: Optional[date] = None) -> str:
    """'expired' | 'due_soon' | 'ok' | 'unknown' — UI rozeti ve filtre için."""
    if until is None:
        return "unknown"
    today = today or date.today()
    if until < today:
        return "expired"
    return "due_soon" if (until - today).days <= DUE_SOON_DAYS else "ok"


def reason_label(reason: Optional[str]) -> str:
    return REASON_LABELS.get(reason or "", reason or "—")


def movement_label(mtype: Optional[str]) -> str:
    return MOVEMENT_LABELS.get(mtype or "", mtype or "—")


def status_label(status: Optional[str]) -> str:
    return STATUS_LABELS.get(status or "", status or "—")


def location_label(shelf: Optional[str], slot: Optional[str]) -> str:
    """'Raf 2 · Göz B' — ikisi de boşsa '—'."""
    parts = []
    if (shelf or "").strip():
        parts.append(f"Raf {shelf.strip()}")
    if (slot or "").strip():
        parts.append(f"Göz {slot.strip()}")
    return " · ".join(parts) or "—"
