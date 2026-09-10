# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""Ürün İçerik Kartı — doğrulama motoru (saf fonksiyonlar, DB'siz).

`core/pdks.py` deseni: bu modül veritabanına dokunmaz, yan etkisi yoktur, tek
başına test edilir. IMS'in "Ürün İçerik Kartı" (Faz 1) doğruluk motorudur:

  - Sitede/pazaryerinde geçen her "öne çıkan bileşen" / koku iddiası
    laboratuvarın bildirdiği INCI listesinde karşılığı olmak ZORUNDADIR.
  - Yasaklı (tıbbi/abartılı) iddialar engellenir.
  - Beden/hacim metinle yapısal alan arasında tutarlı olmalı.
  - `body_html` üretimi tema sözleşmesine (tam 3 `<h3>`) uymalı.

Kart şekli (bkz. plan `quizzical-mixing-walrus.md`):
    {"brand": "minerva", "title": {"en": "...", "tr": "..."},
     "scent": "lavender"|None, "sizes": [{"net_qty": 200, "net_unit": "ml"}],
     "inci_declared": ["Aqua", ...], "allergens": [...],
     "key_ingredients": [{"name_en","name_tr","benefit_en","benefit_tr","inci_ref"}],
     "tagline": {"en","tr"}, "intro": {"en","tr"}, "benefits": [{"en","tr"}],
     "usage": {"en","tr"}, "details": [{"en","tr"}], "warnings": {"en","tr"},
     "seo": {"title": {"en","tr"}, "description": {"en","tr"}},
     "marketplace": {"amazon": {"bullets": [...], "search_terms": "..."},
                     "etsy": {"tags": [...]}},
     "body_html": {"en": "...", "tr": "..."}}   # varsa ayrıca denetlenir

Claude/ajanlar bu modülün ÜRETTİĞİ metni değil, kartın alanlarını yazar — bir
bileşen/koku iddiası ancak `inci_declared`'da karşılığı varsa metne girebilir.
`validate_card` 0 `error` döndürmeden hiçbir kart onaylanmaz/yayınlanmaz.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "inci_synonyms.json"

_LANGS = ("en", "tr")

_HEADINGS = {
    "en": {"benefits": "Key Benefits", "usage": "How to Use", "details": "Details", "size": "Size"},
    "tr": {"benefits": "Öne Çıkan Faydalar", "usage": "Kullanım", "details": "Ürün Detayları", "size": "Hacim"},
}

_ALLOWED_BODY_TAGS = {"p", "ul", "li", "strong", "em", "h3"}

_SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(ml|g|gr|kg|l)\b", re.IGNORECASE)


# ─── Metin normalizasyonu ───────────────────────────────────────────────────

def _fold(s: Any) -> str:
    """Türkçe İ/ı dahil, aksansız + küçük harf normalize (`scripts/import_barcodes.fold` ile aynı teknik, bağımsız kopya)."""
    if s is None:
        return ""
    s = str(s).replace("İ", "I").replace("ı", "i")
    nfkd = unicodedata.normalize("NFKD", s)
    no_diacritics = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", no_diacritics.lower()).strip()


def normalize_inci_text(raw: Any) -> list[str]:
    """INCI dize/listesini tek biçime getirir: virgül/noktalı virgülle böl, katla, aksan+İ/ı normalize et."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        parts = re.split(r"[,;]", str(raw))
    out = []
    for p in parts:
        p = re.sub(r"\s+", " ", str(p)).strip().strip(".")
        if p:
            out.append(p)
    return out


def parse_size(raw: Any) -> tuple[Optional[float], Optional[str]]:
    """'50 ML' → (50.0, 'ml'); ayrıştıramazsa (None, None)."""
    m = _SIZE_RE.search(str(raw or ""))
    if not m:
        return None, None
    qty = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    return qty, unit


# ─── Sinonim tablosu ────────────────────────────────────────────────────────

_syn_cache: Optional[dict] = None


def load_synonyms(path: Optional[Path] = None) -> dict:
    """`data/inci_synonyms.json`'ı yükler (süreç-içi cache; test'ler kendi yolunu verebilir)."""
    global _syn_cache
    p = path or _DATA_PATH
    if path is None and _syn_cache is not None:
        return _syn_cache
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    if path is None:
        _syn_cache = data
    return data


def _forbidden_claim_patterns(path: Optional[Path] = None) -> list[str]:
    p = path or _DATA_PATH
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("_forbidden_claims", [])


def inci_supports(term_key: str, inci_list: list[str], syn: dict) -> list[str]:
    """`term_key` (ör. 'lavender') `inci_list` içindeki hangi INCI adlarınca destekleniyor — kanıt listesi."""
    entry = syn.get(term_key)
    if not entry:
        return []
    folded_inci = [(_fold(x), x) for x in inci_list]
    evidence = []
    for pattern in entry.get("inci_any", []):
        pf = _fold(pattern)
        for f_val, orig in folded_inci:
            if pf in f_val:
                evidence.append(orig)
    return evidence


def _text_mentions(text: str, syn_entry: dict) -> bool:
    folded = _fold(text)
    if not folded:
        return False
    for term in syn_entry.get("match", []):
        tf = _fold(term)
        if tf and re.search(r"(?<![a-z0-9])" + re.escape(tf) + r"(?![a-z0-9])", folded):
            return True
    return False


# ─── Issue ──────────────────────────────────────────────────────────────────

@dataclass
class Issue:
    code: str
    severity: str          # "E" (engelleyici) | "W" (uyarı)
    field: str
    msg_tr: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "field": self.field,
                "msg_tr": self.msg_tr, "evidence": self.evidence}


def _lang_pair(d: Optional[dict]) -> dict:
    return {lang: (d or {}).get(lang, "") for lang in _LANGS}


def _collect_body_texts(card: dict) -> dict[str, str]:
    """Alan adı → birleştirilmiş EN+TR metin (jenerik iddia taramasının döngüsü için)."""
    out: dict[str, str] = {}
    for key in ("tagline", "intro", "usage", "warnings"):
        v = _lang_pair(card.get(key))
        out[key] = " ".join(v.values())
    for key in ("benefits", "details"):
        items = card.get(key) or []
        out[key] = " ".join(_lang_pair(i).get(lang, "") for i in items for lang in _LANGS)
    seo = card.get("seo") or {}
    out["seo.title"] = " ".join(_lang_pair(seo.get("title")).values())
    out["seo.description"] = " ".join(_lang_pair(seo.get("description")).values())
    mk = card.get("marketplace") or {}
    amazon = mk.get("amazon") or {}
    out["marketplace.amazon"] = " ".join(amazon.get("bullets") or []) + " " + str(amazon.get("search_terms") or "")
    etsy = mk.get("etsy") or {}
    out["marketplace.etsy"] = " ".join(etsy.get("tags") or [])
    return out


# ─── Ana doğrulama ──────────────────────────────────────────────────────────

def validate_card(card: dict, syn: Optional[dict] = None) -> list[Issue]:
    syn = syn if syn is not None else load_synonyms()
    issues: list[Issue] = []

    inci_declared = normalize_inci_text(card.get("inci_declared"))
    if not inci_declared:
        issues.append(Issue("INCI_EMPTY", "E", "inci_declared",
                             "INCI (içindekiler) listesi boş — laboratuvar verisi olmadan hiçbir iddia doğrulanamaz."))

    # 1) Öne çıkan içerikler
    key_ingredients = card.get("key_ingredients") or []
    if not key_ingredients:
        issues.append(Issue("KI_MISSING", "W", "key_ingredients", "Öne çıkan içerik tanımlı değil."))
    for i, ki in enumerate(key_ingredients):
        text = " ".join([ki.get("name_en", ""), ki.get("name_tr", ""),
                          ki.get("benefit_en", ""), ki.get("benefit_tr", "")])
        for term_key, entry in syn.items():
            if _text_mentions(text, entry) and not inci_supports(term_key, inci_declared, syn):
                issues.append(Issue("KI_NOT_IN_INCI", "E", f"key_ingredients[{i}]",
                                     f"'{term_key}' öne çıkan içerik olarak yazılmış ama laboratuvarın INCI listesinde karşılığı yok.",
                                     evidence=[text.strip()]))

    # 2) Koku iddiası (özel alan)
    scent = card.get("scent")
    if scent:
        entry = next((e for k, e in syn.items() if _fold(scent) in [_fold(m) for m in e.get("match", [])]), None)
        if entry and not inci_supports(next(k for k, e in syn.items() if e is entry), inci_declared, syn):
            issues.append(Issue("SCENT_NOT_IN_INCI", "E", "scent",
                                 f"'{scent}' kokusu kartta seçilmiş ama INCI listesinde destekleyici bileşen yok."))

    # 3) Başlık
    title = _lang_pair(card.get("title"))
    for term_key, entry in syn.items():
        if _text_mentions(title.get("en", "") + " " + title.get("tr", ""), entry) and not inci_supports(term_key, inci_declared, syn):
            code = "TITLE_SCENT_UNSUPPORTED" if entry.get("scent") else "TEXT_INGREDIENT_UNSUPPORTED"
            issues.append(Issue(code, "E", "title",
                                 f"Başlıkta '{term_key}' geçiyor ama INCI'de karşılığı yok."))

    # 4) Diğer metin alanları (jenerik)
    for field_name, text in _collect_body_texts(card).items():
        for term_key, entry in syn.items():
            if _text_mentions(text, entry) and not inci_supports(term_key, inci_declared, syn):
                issues.append(Issue("TEXT_INGREDIENT_UNSUPPORTED", "E", field_name,
                                     f"'{term_key}' bu alanda geçiyor ama INCI'de karşılığı yok.",
                                     evidence=[text.strip()[:160]]))

    # 5) Yasaklı iddialar
    forbidden = _forbidden_claim_patterns()
    all_text_fields = {"title.en": title.get("en", ""), "title.tr": title.get("tr", ""), **_collect_body_texts(card)}
    for fname, text in all_text_fields.items():
        folded = _fold(text)
        for pat in forbidden:
            if re.search(pat, folded, re.IGNORECASE):
                issues.append(Issue("FORBIDDEN_CLAIM", "E", fname,
                                     f"Yasaklı/kanıtsız iddia kalıbı tespit edildi: /{pat}/", evidence=[text.strip()[:160]]))

    # 6) Beden/hacim tutarlılığı
    sizes = card.get("sizes") or []
    declared_pairs = {(s.get("net_qty"), (s.get("net_unit") or "").lower()) for s in sizes if s.get("net_qty")}
    if declared_pairs:
        for fname in ("title.en", "title.tr"):
            text = all_text_fields.get(fname, "")
            for m in _SIZE_RE.finditer(text):
                found = (float(m.group(1).replace(",", ".")), m.group(2).lower())
                if found not in declared_pairs:
                    issues.append(Issue("SIZE_MISMATCH", "E", fname,
                                         f"Metinde '{m.group(0)}' geçiyor ama karttaki bedenlerle uyuşmuyor "
                                         f"({', '.join(f'{q} {u}' for q, u in declared_pairs)})."))

    # 7) SEO uzunlukları
    seo = card.get("seo") or {}
    for lang in _LANGS:
        t = (seo.get("title") or {}).get(lang, "")
        if t and len(t) > 60:
            issues.append(Issue("SEO_LENGTH", "W", f"seo.title.{lang}", f"SEO başlığı {len(t)} karakter (öneri ≤60)."))
        d = (seo.get("description") or {}).get(lang, "")
        if d and len(d) > 160:
            issues.append(Issue("SEO_LENGTH", "W", f"seo.description.{lang}", f"SEO açıklaması {len(d)} karakter (öneri ≤160)."))

    # 8) Gövde sözleşmesi (varsa)
    body_html = card.get("body_html") or {}
    for lang in _LANGS:
        html = body_html.get(lang)
        if html:
            errs = body_contract_ok(html)
            for e in errs:
                issues.append(Issue("BODY_CONTRACT", "E", f"body_html.{lang}", e))

    # 9) Alerjenler
    allergens = card.get("allergens") or []
    inci_joined = " | ".join(_fold(x) for x in inci_declared)
    for a in allergens:
        if _fold(a) not in inci_joined:
            issues.append(Issue("ALLERGEN_NOT_IN_INCI", "W", "allergens",
                                 f"Alerjen '{a}' INCI listesinde bulunamadı."))

    # 10) Marka tutarlılığı (zayıf ipucu — sadece uyarı)
    brand = (card.get("brand") or "").strip()
    title_en = title.get("en", "")
    if brand and title_en:
        first_word = _fold(title_en.split()[0]) if title_en.split() else ""
        if first_word and not first_word.startswith(_fold(brand)[:6]):
            issues.append(Issue("BRAND_MISMATCH", "W", "title.en",
                                 f"Başlık '{title_en.split()[0]}' ile başlıyor ama kart markası '{brand}'."))

    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "E" for i in issues)


# ─── body_html üretimi ──────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_body_html(card: dict, lang: str) -> str:
    """Kart alanlarından tema sözleşmesine uygun (tam 3 `<h3>`) gövde üretir. INCI asla buraya girmez."""
    h = _HEADINGS[lang]
    intro = _esc((card.get("intro") or {}).get(lang, ""))
    benefits = [_esc(b.get(lang, "")) for b in (card.get("benefits") or []) if b.get(lang)]
    usage = _esc((card.get("usage") or {}).get(lang, ""))
    details = [_esc(d.get(lang, "")) for d in (card.get("details") or []) if d.get(lang)]

    sizes = card.get("sizes") or []
    size_lines = []
    for s in sizes:
        if s.get("net_qty") and s.get("net_unit"):
            qty = s["net_qty"]
            qty_s = str(int(qty)) if float(qty).is_integer() else str(qty)
            size_lines.append(f"{h['size']}: {qty_s} {s['net_unit']}")

    parts = []
    if intro:
        parts.append(f"<p>{intro}</p>")
    parts.append(f"<h3>{h['benefits']}</h3><ul>" + "".join(f"<li>{b}</li>" for b in benefits) + "</ul>")
    parts.append(f"<h3>{h['usage']}</h3><p>{usage}</p>")
    detail_items = "".join(f"<li>{d}</li>" for d in size_lines + details)
    parts.append(f"<h3>{h['details']}</h3><ul>{detail_items}</ul>")
    return "".join(parts)


def body_contract_ok(html: str) -> list[str]:
    """Gövdenin tema sözleşmesine uyup uymadığını denetler; hata mesajları döner (boşsa uygun)."""
    errs = []
    tags_found = re.findall(r"</?([a-zA-Z0-9]+)", html)
    disallowed = sorted({t.lower() for t in tags_found if t.lower() not in _ALLOWED_BODY_TAGS})
    if disallowed:
        errs.append(f"İzin verilmeyen etiket(ler): {', '.join(disallowed)}")

    h3s = re.findall(r"<h3>(.*?)</h3>", html)
    if len(h3s) != 3:
        errs.append(f"Tam 3 adet <h3> bekleniyor, {len(h3s)} bulundu.")
        return errs  # sıra kontrolüne devam etmenin anlamı yok

    segments = re.split(r"<h3>.*?</h3>", html)
    # segments[0]=h3 öncesi(intro), [1]=1.h3 sonrası..2.h3 öncesi, [2]=.., [3]=3.h3 sonrası
    if len(segments) < 4:
        errs.append("Gövde bölümleri (Key Benefits/How to Use/Details) ayrıştırılamadı.")
        return errs
    if "<ul>" not in segments[1] or "<li>" not in segments[1]:
        errs.append("1. bölüm (Key Benefits) en az bir <li> içermeli.")
    if "<p>" not in segments[2]:
        errs.append("2. bölüm (How to Use) bir <p> içermeli.")
    first_li = re.search(r"<li>(.*?)</li>", segments[3])
    if not first_li:
        errs.append("3. bölümün (Details) ilk satırı bulunamadı.")
    elif not re.search(r"\d", first_li.group(1)):
        errs.append("3. bölümün ilk satırı beden/hacim olmalı (sayı içermiyor): " + first_li.group(1)[:60])

    if "İÇİNDEKİLER" in html.upper() or re.search(r"\bAQUA\b", html, re.IGNORECASE):
        errs.append("Gövdede INCI/İçindekiler metni tespit edildi — INCI sitede gösterilmez.")
    return errs


def key_ingredients_metafield(card: dict, lang: str) -> str:
    """`m108.key_ingredients` metafield metnini üretir — canlıdaki format: 'Ad: fayda cümlesi.' art arda."""
    name_key, benefit_key = f"name_{lang}", f"benefit_{lang}"
    lines = []
    for ki in card.get("key_ingredients") or []:
        name = (ki.get(name_key) or "").strip()
        benefit = (ki.get(benefit_key) or "").strip()
        if not name:
            continue
        if benefit and not benefit.endswith("."):
            benefit += "."
        lines.append(f"{name}: {benefit}" if benefit else f"{name}.")
    return " ".join(lines)


def suggest_key_ingredients(inci_declared: list[str], syn: Optional[dict] = None, n: int = 4) -> list[dict]:
    """INCI listesinden öne çıkabilecek bileşenleri önerir — İNSAN ONAYI ŞART, otomatik yayınlanmaz."""
    syn = syn if syn is not None else load_synonyms()
    inci_declared = normalize_inci_text(inci_declared)
    suggestions: list[dict] = []
    seen_terms: set[str] = set()
    for inci in inci_declared:
        folded = _fold(inci)
        for term_key, entry in syn.items():
            if term_key in seen_terms:
                continue
            for pattern in entry.get("inci_any", []):
                if _fold(pattern) in folded:
                    suggestions.append({"inci_ref": inci, "name_en": term_key.title(),
                                         "name_tr": "", "benefit_en": "", "benefit_tr": ""})
                    seen_terms.add(term_key)
                    break
        if len(suggestions) >= n:
            break
    return suggestions[:n]


def diff_cards(a: dict, b: dict, _prefix: str = "") -> dict:
    """Sığ-özyinelemeli fark: {alan: {"old":..., "new":...}}."""
    out: dict = {}
    keys = set(a or {}) | set(b or {})
    for k in sorted(keys):
        path = f"{_prefix}{k}"
        va, vb = (a or {}).get(k), (b or {}).get(k)
        if isinstance(va, dict) and isinstance(vb, dict):
            out.update(diff_cards(va, vb, _prefix=f"{path}."))
        elif va != vb:
            out[path] = {"old": va, "new": vb}
    return out
