# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Trendyol Liste Doktoru — kural puanı (SAF, DB'siz; core/pdks.py tarzı).

Spec: docs/superpowers/specs/2026-10-13-trendyol-satis-artirma-design.md.
Puan 100 üzerinden, yedi başlık; ağırlıklar `WEIGHTS`'ta TEK yerde.  Her
kontrol (puan, en yüksek puan, açıklama) döner; en çok puan kazandıracak ilk
üç düzeltme `fixes`'te.  Ücretsiz ve deterministiktir — iyileşme günlük puan
geçmişinden izlenir.  Yapay zekâ PUAN VERMEZ, yalnız yazar (core/trendyol_ai).

Metin süzgeçleri (`claim_flags`, `promo_words_in`, `unverified_claims`) burada
durur; yapay zekâ çıktısının kod denetimi de AYNI listeleri kullanır.
"""
import html
import re
from typing import Dict, Iterable, List, Optional

from core.supplier_prices import normalize as fold

WEIGHTS = {"title": 20, "attributes": 20, "description": 15, "images": 15,
           "price": 10, "delivery": 10, "status": 10}
SECTION_LABELS = {"title": "Ürün adı", "attributes": "Özellikler", "description": "Açıklama",
                  "images": "Görseller", "price": "Fiyat", "delivery": "Teslimat",
                  "status": "Satış durumu"}

TITLE_MIN, TITLE_MAX = 50, 150
DESC_MIN, DESC_RICH = 200, 600
IMAGE_STEPS = ((6, 15), (5, 11), (4, 8))          # (en az görsel, puan) — büyükten küçüğe
FIX_COUNT = 3

#: Başlıkta promosyon dili (katlanmış; TR karakterler normalize edilir).
PROMO_WORDS = ("indirim", "kampanya", "ucretsiz kargo", "bedava", "en ucuz", "firsat",
               "son sans", "kacirma", "stoklarla sinirli", "hediye")

#: Kozmetikte yasak tıbbi/mutlak iddialar (katlanmış metinde arama).
CLAIM_PATTERNS = (
    (r"\btedavi", "tedavi iddiası"),
    (r"\biyilestir", "iyileştirme iddiası"),
    (r"\bsifa", "şifa iddiası"),
    (r"\bmucize", "mucize iddiası"),
    (r"\bgaranti", "garanti iddiası"),
    (r"\b(kesin|kalici) (cozum|sonuc)", "kesin/kalıcı sonuç iddiası"),
    (r"\bdoktor (onerisi|tavsiyesi|onayli)", "doktor önerisi iddiası"),
    (r"%\s*100\s*(etki|garanti|sonu|kesin|basar)", "%100 etkinlik iddiası"),
    (r"\b(egzama|sedef|psoriazis|dermatit|rozasea|enfeksiyon|mantar tedavi)", "hastalık adı"),
)

#: Kanıt gerektiren iddialar — bilgi notunda ya da mevcut listede geçmiyorsa
#: yapay zekâ yazamaz (uyarı).
EVIDENCE_PATTERNS = (
    (r"dermatolojik(?:\s+olarak)?\s+test", "dermatolojik test"),
    (r"klinik(?:\s+olarak)?\s+(test|kanit)", "klinik test"),
    (r"hipoalerjenik", "hipoalerjenik"),
    (r"sertifika", "sertifika"),
    (r"\b(ecocert|cosmos|fda|vegan society|peta)\b", "kuruluş onayı"),
)

SIZE_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s?(?:ml|gr|g|kg|lt|l|oz|cl|adet)\b|\b\d+\s?['’]?l[iıuü]\b", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_TAG_RE = re.compile(r"</?(p|br|li|ul|ol|div|h[1-6])\b[^>]*>", re.I)
_BULLET_RE = re.compile(r"^\s*(?:[-•*·▪]|\d+[.)])\s+\S", re.M)
_GENERIC_WORDS = {"urun", "urunleri", "diger", "bakim", "kozmetik", "kisisel", "set", "seti", "ve"}


# ─── Metin yardımcıları ──────────────────────────────────────────────────────

def strip_html(text: Optional[str]) -> str:
    """HTML → düz metin (blok etiketleri satır sonuna, varlıklar çözülür)."""
    if not text:
        return ""
    s = _BLOCK_TAG_RE.sub("\n", str(text))
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    lines = [" ".join(ln.split()) for ln in s.replace("\r", "").split("\n")]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def claim_flags(text: Optional[str]) -> List[str]:
    """Yasak iddiaların etiketleri (tekrarsız, sırayla)."""
    f = fold(text or "")
    out: List[str] = []
    for pat, label in CLAIM_PATTERNS:
        if re.search(pat, f) and label not in out:
            out.append(label)
    return out


def promo_words_in(text: Optional[str]) -> List[str]:
    f = fold(text or "")
    return [w for w in PROMO_WORDS if w in f]


def unverified_claims(text: Optional[str], evidence: Iterable[Optional[str]]) -> List[str]:
    """Metindeki kanıt gerektiren iddialardan `evidence` metinlerinde (bilgi
    notu, mevcut liste) GEÇMEYENLER."""
    f = fold(text or "")
    ev = fold(" ".join(e for e in evidence if e))
    out: List[str] = []
    for pat, label in EVIDENCE_PATTERNS:
        if re.search(pat, f) and not re.search(pat, ev) and label not in out:
            out.append(label)
    return out


def _upper_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def _stems(*names: Optional[str]) -> List[str]:
    out = []
    for n in names:
        for w in fold(n or "").replace("/", " ").replace("-", " ").split():
            if len(w) >= 4 and w not in _GENERIC_WORDS and w[:4] not in out:
                out.append(w[:4])
    return out


def _check(key: str, label: str, points: float, max_points: int, detail: str = "", fix: str = "") -> dict:
    return {"key": key, "label": label, "points": round(points, 1), "max": max_points,
            "ok": points >= max_points - 1e-9, "detail": detail, "fix": fix}


# ─── Bölümler ────────────────────────────────────────────────────────────────

def _title(p: dict, ims: dict) -> List[dict]:
    title = (p.get("title") or "").strip()
    n = len(title)
    ft = fold(title)
    if TITLE_MIN <= n <= TITLE_MAX:
        lp = 8
    elif 30 <= n < TITLE_MIN or n > TITLE_MAX:
        lp = 4
    else:
        lp = 0
    brand = (p.get("brand") or ims.get("brand") or "").strip()
    has_brand = bool(brand) and fold(brand).split()[0] in ft if brand else False
    stems = _stems(p.get("category"))
    has_type = bool(stems) and any(s in ft for s in stems)
    has_size = bool(SIZE_RE.search(title))
    promo = promo_words_in(title)
    shouty = n > 10 and _upper_ratio(title) > 0.7
    clean = not promo and not shouty
    return [
        _check("title.length", "Uzunluk 50–150 karakter", lp, 8, f"{n} karakter",
               f"Ürün adını 50–150 karaktere getir (şu an {n})"),
        _check("title.brand", "Marka adı geçiyor", 4 if has_brand else 0, 4,
               brand or "marka bilinmiyor", "Marka adını başlığa ekle"),
        _check("title.type", "Ürün türü geçiyor", 4 if has_type else 0, 4,
               p.get("category") or "kategori bilinmiyor",
               "Ürün türünü (kategori adını) başlıkta geçir"),
        _check("title.size", "Hacim/ölçü geçiyor", 2 if has_size else 0, 2, "",
               "Hacmi/ölçüyü başlığa ekle (ör. 50 ml)"),
        _check("title.clean", "Büyük harf/promosyon dili yok", 2 if clean else 0, 2,
               ", ".join(promo) if promo else ("tamamı büyük harf" if shouty else ""),
               "Başlıkta tamamı büyük harf ya da promosyon kelimesi kullanma"),
    ]


def _attributes(p: dict, attrs_def: List[dict]) -> List[dict]:
    if not attrs_def:
        return [
            _check("attributes.required", "Zorunlu özellikler dolu", 0, 10, "kategori özellik listesi henüz yok",
                   "Kategori özellikleri çekilince yeniden değerlendirilecek"),
            _check("attributes.optional", "Süzgeç/isteğe bağlı özellikler", 0, 10, "kategori özellik listesi henüz yok",
                   ""),
        ]
    filled = {int(a["id"]) for a in (p.get("attributes") or [])
              if a.get("id") is not None and (a.get("values") or a.get("value_ids"))}
    required = [a for a in attrs_def if a.get("required")]
    optional = [a for a in attrs_def if not a.get("required")]
    if required:
        got = sum(1 for a in required if int(a["attribute_id"]) in filled)
        rp = 10.0 * got / len(required)
        missing = [a.get("name") for a in required if int(a["attribute_id"]) not in filled]
        rdetail = f"{got}/{len(required)}" + (f" — eksik: {', '.join(m for m in missing[:5] if m)}" if missing else "")
    else:
        rp, rdetail, missing = 10.0, "zorunlu özellik yok", []
    if optional:
        # Süzgeçte kullanılan (slicer/varianter) özellik iki kat sayılır.
        def w(a):
            return 2 if (a.get("slicer") or a.get("varianter")) else 1
        total = sum(w(a) for a in optional)
        got_w = sum(w(a) for a in optional if int(a["attribute_id"]) in filled)
        op = 10.0 * got_w / total
        got_n = sum(1 for a in optional if int(a["attribute_id"]) in filled)
        odetail = f"{got_n}/{len(optional)}"
    else:
        op, odetail = 10.0, "isteğe bağlı özellik yok"
    return [
        _check("attributes.required", "Zorunlu özellikler dolu", rp, 10, rdetail,
               f"Zorunlu özellikleri doldur ({len(missing)} eksik)"),
        _check("attributes.optional", "Süzgeç/isteğe bağlı özellikler", op, 10, odetail,
               "Süzgeçte kullanılan özellikleri doldur"),
    ]


def _description(p: dict) -> List[dict]:
    raw = p.get("description") or ""
    text = strip_html(raw)
    n = len(text)
    paragraphs = [b for b in text.split("\n\n") if b.strip()]
    bullets = len(_BULLET_RE.findall(text)) + len(re.findall(r"<li\b", str(raw), re.I))
    structured = len(paragraphs) >= 2 or bullets >= 3 or text.count("\n") >= 3
    ft = fold(text)
    usage = any(k in ft for k in ("kullanim", "nasil kullanilir", "uygula", "uygulayin", "surun", "kullanin"))
    content = any(k in ft for k in ("icerik", "icindekiler", "ingredients", "inci", "formul", "ozu", "yagi"))
    return [
        _check("description.min", "En az 200 karakter", 5 if n >= DESC_MIN else 0, 5, f"{n} karakter",
               "Açıklamayı en az 200 karaktere çıkar"),
        _check("description.rich", "600+ karakter", 3 if n >= DESC_RICH else 0, 3, f"{n} karakter",
               "Açıklamayı zenginleştir (600+ karakter)"),
        _check("description.structure", "Paragraf/madde yapısı", 3 if structured else 0, 3, "",
               "Açıklamayı paragraf ve maddelere böl"),
        _check("description.usage", "Kullanım bilgisi", 2 if usage else 0, 2, "", "Kullanım talimatı ekle"),
        _check("description.content", "İçerik bilgisi", 2 if content else 0, 2, "", "Öne çıkan içerikleri yaz"),
    ]


def _images(p: dict) -> List[dict]:
    n = int(p.get("image_count") or 0)
    pts = 0
    for minimum, value in IMAGE_STEPS:
        if n >= minimum:
            pts = value
            break
    return [_check("images.count", "Görsel sayısı (6+ tam puan)", pts, 15, f"{n} görsel",
                   f"Görsel sayısını 6'ya çıkar (şu an {n})")]


def _price(p: dict) -> List[dict]:
    sale, lst = p.get("sale_price"), p.get("list_price")
    discount = sale is not None and lst is not None and lst > sale + 0.009
    order, multi = p.get("buybox_order"), p.get("has_multiple_seller")
    bfix = "Buybox başkasında — satıcıyı ve fiyatı kontrol et"
    if order == 1:
        bb, bdetail = 5, "buybox bizde"
    elif order is None and multi is False:
        bb, bdetail = 5, "tek satıcı"
    elif order is None:
        bb, bdetail = 0, "buybox henüz kontrol edilmedi"
        bfix = "Buybox bilgisi bir sonraki katalog senkronunda gelir"
    else:
        bp = p.get("buybox_price")
        bb, bdetail = 0, f"buybox {order}. sırada" + (f" (buybox fiyatı {bp:.2f})" if bp is not None else "")
    return [
        _check("price.discount", "İndirim görünüyor", 5 if discount else 0, 5,
               f"satış {sale} · liste {lst}" if sale is not None else "fiyat yok",
               "İndirim görünmüyor — gerçek bir kampanya varsa liste fiyatını güncelle"),
        _check("price.buybox", "Buybox bizde / tek satıcı", bb, 5, bdetail, bfix),
    ]


def _delivery(p: dict) -> List[dict]:
    d = p.get("delivery_duration")
    if d is None:
        pts, detail = 0, "bilinmiyor"
    elif d <= 1:
        pts, detail = 10, f"{d} gün"
    elif d == 2:
        pts, detail = 5, "2 gün"
    else:
        pts, detail = 0, f"{d} gün"
    return [_check("delivery.duration", "Teslimat 1 gün", pts, 10, detail,
                   f"Teslimat süresini 1 güne indir (şu an {detail})")]


def _status(p: dict, ims: dict) -> List[dict]:
    active = bool(p.get("on_sale")) and not p.get("archived") and not p.get("locked") and not p.get("blacklisted")
    qty = p.get("quantity")
    stock = ims.get("stock")
    if qty is None:
        over_pts, over_detail = 0, "Trendyol stoğu bilinmiyor"
    elif stock is None:
        over_pts, over_detail = 0, "IMS kartıyla eşleşmemiş"
    elif qty <= stock:
        over_pts, over_detail = 3, f"Trendyol {qty} ≤ IMS {stock:g}"
    else:
        over_pts, over_detail = 0, f"Trendyol {qty} > IMS {stock:g}"
    return [
        _check("status.active", "Satışta, kilitsiz", 4 if active else 0, 4, "",
               "Ürün satışta değil ya da kilitli/arşivde — panelden kontrol et"),
        _check("status.stock", "Trendyol stoğu var", 3 if (qty or 0) > 0 else 0, 3, f"{qty}",
               "Trendyol stoğu 0 — stok gir"),
        _check("status.oversell", "Trendyol stoğu ≤ IMS stoğu", over_pts, 3, over_detail,
               "Trendyol stoğunu IMS stoğuyla uyumla (ya da kartı eşleştir)"),
    ]


# ─── Ana fonksiyon ───────────────────────────────────────────────────────────

def score_listing(p: dict, attrs_def: Optional[List[dict]] = None, ims: Optional[dict] = None) -> dict:
    """`p`: katalog ürünü (title, brand, category, description, attributes,
    image_count, sale_price, list_price, buybox_order, buybox_price,
    has_multiple_seller, delivery_duration, on_sale, archived, locked,
    blacklisted, quantity).  `attrs_def`: kategori özellikleri
    (attribute_id, name, required, slicer, varianter).  `ims`: {"stock",
    "brand"} — eşleşmemişse stock None.  → {score, breakdown, fixes}."""
    ims = ims or {}
    sections = {
        "title": _title(p, ims), "attributes": _attributes(p, attrs_def or []),
        "description": _description(p), "images": _images(p), "price": _price(p),
        "delivery": _delivery(p), "status": _status(p, ims),
    }
    breakdown, total = [], 0.0
    for key, checks in sections.items():
        pts = sum(c["points"] for c in checks)
        total += pts
        breakdown.append({"key": key, "label": SECTION_LABELS[key], "points": round(pts, 1),
                          "max": WEIGHTS[key], "checks": checks})
    gaps = [c for checks in sections.values() for c in checks if not c["ok"] and c["fix"]]
    gaps.sort(key=lambda c: -(c["max"] - c["points"]))
    fixes = [{"key": c["key"], "gain": round(c["max"] - c["points"], 1), "text": c["fix"]}
             for c in gaps[:FIX_COUNT]]
    return {"score": int(round(total)), "breakdown": breakdown, "fixes": fixes}


def missing_attributes(p: dict, attrs_def: List[dict]) -> List[dict]:
    """Üründe boş kalan kategori özellikleri (önce zorunlu, sonra süzgeç)."""
    filled = {int(a["id"]) for a in (p.get("attributes") or [])
              if a.get("id") is not None and (a.get("values") or a.get("value_ids"))}
    out = [a for a in attrs_def if int(a["attribute_id"]) not in filled]
    out.sort(key=lambda a: (not a.get("required"), not (a.get("slicer") or a.get("varianter")),
                            a.get("name") or ""))
    return out
