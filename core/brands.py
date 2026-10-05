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
import difflib
import re
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


# ─── Sağlam marka anahtarı (raporlar / planlama için) ──────────────────────
#
# `brand_of` ilk kelimeyi AYNEN döndürür; elle girilmiş adlarda aynı marka
# "MİNERVA-108" / "Minerva108" / "Minerva" / "SERENİDE" gibi 4-5 ayrı yazımla
# yaşadığından Raporlar panelinde marka çipleri çoğalıyordu.  Aşağıdakiler
# YALNIZ gösterim/gruplama içindir: dolap adı ve lot öneki `brand_of` /
# `canonical_brand` / `lot_code` sözleşmesine bağlı, onlara DOKUNULMADI.
_LEAD_ALNUM = re.compile(r"[^a-z0-9]*([a-z0-9]+)")
_BRAND_FUZZY_CUTOFF = 0.8


def _edit_distance(a: str, b: str) -> int:
    """Optimal string alignment (Damerau) mesafesi — yer değiştirme 1 sayılır."""
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if (prev2 is not None and i > 1 and j > 1
                    and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]):
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[len(b)]


def brand_key(name: str) -> str:
    """Ürün adı → kararlı marka anahtarı ('minerva' / 'serenida' / 'evanira' / …).

    TR-katlanır, baştaki [a-z]+ dizisi alınır ('MİNERVA-108', 'Minerva108',
    'minerva-108' → 'minerva').  Alias'ta yoksa ilk kelimenin yalnız harfleri
    ('SER5ENİDA' → 'serenida') ve difflib yakın eşleşmesi (cutoff 0.8 + en
    fazla tek harf düzeltmesi; 'Serinida' / 'SERENİDE' → 'serenida',
    'Mineral' → eşleşmez) denenir.  Hiçbiri tutmazsa harf
    dizisinin kendisi, o da yoksa 'diger'.
    """
    s = fold(name)
    m = _LEAD_ALNUM.match(s)
    if not m:
        return "diger"
    word = m.group(1)                                   # 'ser5enida', 'minerva108'
    lead = re.match(r"[a-z]*", word).group(0)           # 'ser', 'minerva'
    letters = re.sub(r"[^a-z]", "", word)               # 'serenida', 'minerva'
    for cand in (lead, letters):
        if cand in BRAND_ALIASES:
            return cand
    for cand in (letters, lead):
        if not cand:
            continue
        close = difflib.get_close_matches(cand, list(BRAND_ALIASES), n=1,
                                          cutoff=_BRAND_FUZZY_CUTOFF)
        # Ek bekçi: yazım hatası = en fazla TEK düzeltme.  difflib tek başına
        # 'Mineral …' adını da (oran 0.86) Minerva'ya bağlardı.
        if close and _edit_distance(cand, close[0]) <= 1:
            return close[0]
    return lead or letters or "diger"


def brand_label(name: str) -> str:
    """Ürün adı → görüntülenecek marka ('Minerva 108' / 'Serenida' / 'Evanira').

    Tanımlı markaya düşmeyen adlarda eski davranış: ilk kelime (`brand_of`).
    """
    return BRAND_ALIASES.get(brand_key(name)) or brand_of(name)


def is_known_brand(name: str) -> bool:
    return brand_key(name) in BRAND_ALIASES


def product_brand(target_name: Optional[str], recipe_name: Optional[str] = None) -> str:
    """Hedef ürün adı ile reçete adından hangisi tanımlı bir markaya düşüyorsa onu
    kullanır (önce hedef ürün).  İkisi de düşmüyorsa hedef (yoksa reçete) adının
    `brand_label`'ı."""
    for nm in (target_name, recipe_name):
        if nm and is_known_brand(nm):
            return brand_label(nm)
    return brand_label(target_name or recipe_name or "")


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
