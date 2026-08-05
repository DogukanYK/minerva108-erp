# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Marka çözümü — TEK KAYNAK.

Sistemde marka ayrı bir tablo değil, ürün adının İLK KELİMESİdir ("Minerva 108
Red Clover Cream" → Minerva).  Ürün adları elle girildiği için aynı marka
"Minerva" / "MİNERVA" gibi varyantlarla yaşıyor; eşleştirme daima TR-katlanmış
anahtar üzerinden yapılır, görüntüde kanonik yazım kullanılır.

Bu modülden ÖNCE marka mantığı üç ayrı yerde vardı (routers/production.py'de
hard-coded harita, core/production_sim.brand_of, core/ingredients_report.
_canonical_brands).  Yeni kod BURAYI kullanmalı; eski çağrı yerleri geriye
uyumluluk için re-export shim'i taşır.

Kavram ayrımı — karıştırma:
  • `cabinet_of()` → şahit numune dolabının adı (kanonik marka, "Minerva 108")
  • `lot_code()`   → lot numarası öneki (kısa kod, "MNR")
  • `core.shopify.canonical_brand()` → mağaza env anahtarı; kendi 3-marka
    allowlist'i vardır (marka ≠ mağaza), buradaki alias'lardan bağımsızdır.
"""
from typing import Optional

from core.supplier_prices import normalize as fold


def brand_of(name: str) -> str:
    """Ürün adından markayı türet — ilk kelime (Minerva / Serenida / Evanira…)."""
    return (name or "").strip().split(" ")[0] or "—"


# Kanonik marka adları — dolap etiketi olarak da kullanılır.  "Minerva 108"
# yazımı bilinçli: mevcut Inventory satırlarında konum "Şahit Numune Dolabı —
# Minerva 108" olarak duruyor, backfill'in onlarla eşleşmesi buna bağlı.
BRAND_ALIASES = {
    "minerva": "Minerva 108",
    "serenida": "Serenida",
    "evanira": "Evanira",
}

# Lot numarası önekleri — kullanıcının fiilen kullandığı kodlar (MNR006, SR005).
BRAND_LOT_CODES = {
    "minerva": "MNR",
    "serenida": "SR",
    "evanira": "EV",
}


def canonical_brand(name: str) -> str:
    """Ürün adı → kanonik marka adı.  Tanımlı alias yoksa ilk kelime aynen döner."""
    b = brand_of(name)
    return BRAND_ALIASES.get(fold(b), b)


def lot_code(name: str) -> str:
    """Ürün adı → lot numarası öneki (MNR / SR / EV).

    Tanımsız markada ilk kelimenin harflerinden 3 karakterlik bir kod türetilir
    ("GEVEN&BOR" → "GEV") — böylece yeni bir marka eklendiğinde modül çalışmayı
    sürdürür, kod sonradan BRAND_LOT_CODES'a eklenebilir.
    """
    b = brand_of(name)
    key = fold(b)
    if key in BRAND_LOT_CODES:
        return BRAND_LOT_CODES[key]
    letters = "".join(ch for ch in fold(b) if ch.isalnum())
    return (letters[:3] or "URN").upper()


def canonical_brands(names: list) -> dict:
    """Marka yazım varyantlarını birleştirir: TR-katlanmış anahtar → en yaygın yazım.

    Görüntü olarak VERİDEKİ en sık yazım kazanır ("Minerva"), alias DEĞİL —
    İçindekiler Raporu gibi ürün listeleyen ekranlar ürün adındaki yazımı
    gösterir.  Dolap/etiket gibi kanonik ad gereken yerler `canonical_brand()`
    veya `cabinet_of()` kullanır ("Minerva 108").
    """
    counts: dict = {}
    for nm in names:
        b = brand_of(nm)
        counts.setdefault(fold(b), {}).setdefault(b, 0)
        counts[fold(b)][b] += 1
    return {k: max(v, key=v.get) for k, v in counts.items()}


def brand_in(name: str, canon: dict) -> str:
    """`canonical_brands()` sonucuna göre bir ürünün marka etiketi."""
    return canon.get(fold(brand_of(name)), brand_of(name))


def cabinet_of(name: str) -> str:
    """Şahit numune dolabının adı — kanonik marka.  Marka çözülemezse genel dolap."""
    b = canonical_brand(name)
    return b if b and b != "—" else "Genel"


def cabinet_location(name: str) -> str:
    """Inventory.location metni — mevcut üretim akışının yazdığı biçimin AYNISI.

    routers/production.py bu biçimi yazıyor; backfill eski satırları bununla
    eşleştiriyor.  Biçim değişirse ikisi birden değişmeli.
    """
    b = cabinet_of(name)
    return f"Şahit Numune Dolabı — {b}" if b != "Genel" else "Şahit Numune Dolabı"


def brand_from_location(location: Optional[str]) -> Optional[str]:
    """'Şahit Numune Dolabı — Minerva 108' → 'Minerva 108'.  Ayraç yoksa None."""
    if not location:
        return None
    for sep in (" — ", " - ", " – "):
        if sep in location:
            return location.split(sep, 1)[1].strip() or None
    return None
