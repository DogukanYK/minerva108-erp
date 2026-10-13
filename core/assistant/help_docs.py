# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Nasıl-yapılır rehberi (`docs/assistant/*.md`) — yükleme + `search_help` araması.

Neden ayrı: 13.10.2026 canlı ölçümde (count_tokens) önbellekli önek 19.661
token'dı; bunun 14.196'sı SSS bloğuydu (Türkçe ≈1,8 karakter/token) ve soğuk
1h yazma ≈0,16 $ tutuyordu.  Artık önekte yalnız giriş (menü haritası + panel
değiştirme) ve konu DİZİNİ durur (`prompt.guide_block_text`); konu metinleri
`search_help` aracıyla istek anında gelir.

Dosya biçimi: ilk satır `# Başlık`, ardından `Yetki: …` ve `Sayfa: …`
satırları, sonra gövde.  Konu id'si dosya adından sayı öneki atılarak türer
(`02_lot_tasima.md` → `lot_tasima`).  Dosyalar repo dosyasıdır (kullanıcı
girdisi değil), import anında BİR kez okunur; kullanıcı metninden yol
KURULMAZ.  Bu modül saf Python'dur (DB/ORM import etmez) — `prompt.py`
önekini import anında DB'siz kurabilsin.
"""
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

INTRO_ID = "giris"            # 00_giris.md — önekte tam metin, aramada yok
TOPICS_MAX = 2                # tam metniyle dönen en çok konu
OTHERS_MAX = 4                # yalnız id + başlıkla dönen aday
SECOND_TOPIC_RATIO = 0.6      # 2. konu yalnız skoru 1.'nin ≥ %60'ıysa tam metinle gelir
QUERY_ECHO_MAX = 200
NOT_FOUND_NOTE = "rehberde bu konu yok"

# core.supplier_prices._TR_FOLD ile AYNI tablo (orada `database` import edildiği
# için burada kopya; eşitliği testte kilitli).
_TR_FOLD = str.maketrans({
    "ç": "c", "Ç": "c", "ğ": "g", "Ğ": "g", "ı": "i", "İ": "i",
    "ö": "o", "Ö": "o", "ş": "s", "Ş": "s", "ü": "u", "Ü": "u", "I": "i",
})
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_APOS_SUFFIX_RE = re.compile(r"['’`][a-z]+")
_ID_PREFIX_RE = re.compile(r"^\d+[_-]+")
# Soru kalıbı sözcükleri — hiçbir konuyu ayırt etmez (katlanmış biçimde).
_STOPWORDS = frozenset("""
    a acaba ama bana ben beni benim bir biri bu buna bunu da daha de diye en
    gibi hangi hangisi icin ile ise kim mi mu mi misin nasil ne neden nedir
    nerde nerede nereden nereye niye o olur olarak onu sen su sunu ve veya
    ya yani yap yapilir yaparim yapabilirim yapmak yapiyorum yapmaliyim
    edilir ederim etmek gerek gerekir lazim istiyorum adim adimlar
""".split())
# Rehberin kısaltmayla / başka sözcükle yazdığı kavramlar (katlanmış) — sorguda
# gündelik biçimiyle yazılırsa rehberin sözcüğüne çevrilir ("son kullanma tarihi
# yanlış" → "skt yanlis", "üretimi geri al" → "uretimi iptal").
_QUERY_ALIASES = (("son kullanma tarihi", "skt"), ("son kullanma tarihleri", "skt"),
                  ("son kullanma", "skt"), ("geri al", "iptal"))

# Alan ağırlıkları: başlık/id ≫ Yetki/Sayfa satırları > gövde
W_TITLE = 6.0
W_META = 3.0
W_BODY = 1.0
TF_STEP, TF_MAX_EXTRA = 0.25, 4   # gövdede sık geçen sözcük (ör. teslimatta "kargo" ×5)
MIN_TOPIC_SCORE = 3.0   # altı = tek, seyrek bir gövde eşleşmesi → konu değil, yalnız aday
W_PHRASE = 1.5       # ardışık iki sorgu sözcüğü metinde de ardışıksa (ör. "stok düzeltme")
# Başlık kapsaması: skor × (1 + W_TITLE_COV × eşleşen başlık sözcüğü / başlık sözcüğü).
# Eşit skorlu kısa başlık öne geçer ("üretim nasıl yapılır": "Üretim başlatma" 1/2
# > "Üretimi iptal etme" 1/3) — yoksa eşitlik dosya sırasına (03 < 14) kalıyordu.
W_TITLE_COV = 0.3
# Eşleşme kalitesi (Türkçe ekler): tam > önek (kısa olan ≥4 harf: "taşı"~"taşıma")
# > ünsüz yumuşaması ("şahit"~"şahidi", "stok"~"stoğa") > ortak gövde ("düzeltirim"
# ~"düzeltme": ortak önek ≥4 ve kısa sözcüğün en çok son 2 harfi farklı, kısa ≥6)
M_EXACT = 1.0
M_PREFIX = 0.8
M_SOFT = 0.7
M_STEM = 0.5
M_SHORT = 0.6           # 3 harfli sözcük + en çok SHORT_SUFFIX_MAX harf ek
SHORT_SUFFIX_MAX = 4
_SOFTEN = {"k": "g", "t": "d", "p": "b"}   # ç→c katlamada kaybolur


@dataclass(frozen=True)
class HelpDoc:
    id: str            # dosya adı − sayı öneki (ör. "lot_tasima")
    filename: str      # sıralama ve hata iletisi için
    title: str
    yetki: str         # "Yetki:" satırının değeri ("" olabilir)
    sayfa: str         # "Sayfa:" satırının değeri ("" olabilir)
    body: str          # Yetki/Sayfa satırlarından sonraki gövde

    @property
    def text(self) -> str:
        """Başlık hariç tam metin (Yetki + Sayfa + gövde) — araç sonucu bunu taşır."""
        head = []
        if self.yetki:
            head.append(f"Yetki: {self.yetki}")
        if self.sayfa:
            head.append(f"Sayfa: {self.sayfa}")
        parts = ["\n".join(head)] if head else []
        if self.body:
            parts.append(self.body)
        return "\n\n".join(parts)


# ─── Katlama / belirteçler ───────────────────────────────────────────────────

def fold(v) -> str:
    """`core.supplier_prices.normalize` ile aynı: Türkçe-katlanmış, küçük harf,
    boşluk-normalize."""
    s = " ".join(str(v or "").split())
    return " ".join(s.translate(_TR_FOLD).lower().split())


def tokens(v) -> List[str]:
    """Katlanmış sözcükler; kesme işaretli ek atılır ("SKT'si" → "skt")."""
    return _TOKEN_RE.findall(_APOS_SUFFIX_RE.sub("", fold(v)))


def doc_id(stem: str) -> str:
    """`02_lot_tasima` → `lot_tasima`; `-`/boşluk → `_`, katlanmış."""
    s = _ID_PREFIX_RE.sub("", fold(stem))
    return re.sub(r"[\s_\-]+", "_", s).strip("_")


def _query_id(q: str) -> str:
    s = fold(q)
    if s.endswith(".md"):
        s = s[:-3]
    return doc_id(s)


# ─── Yükleme ─────────────────────────────────────────────────────────────────

def parse_doc(text: str, stem: str) -> HelpDoc:
    lines = text.strip().splitlines()
    title = stem
    if lines and lines[0].startswith("# "):
        title = lines[0][2:].strip() or stem
        lines = lines[1:]
    yetki = sayfa = ""
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        if not ln:
            i += 1
            continue
        if ln.startswith("Yetki:") and not yetki:
            yetki = ln[len("Yetki:"):].strip()
        elif ln.startswith("Sayfa:") and not sayfa:
            sayfa = ln[len("Sayfa:"):].strip()
        else:
            break
        i += 1
    body = "\n".join(lines[i:]).strip()
    return HelpDoc(id=doc_id(stem), filename=f"{stem}.md", title=title, yetki=yetki,
                   sayfa=sayfa, body=body)


def load_docs(directory: Path) -> Tuple[HelpDoc, ...]:
    """`directory/*.md` → dosya adına göre SIRALI HelpDoc listesi.  Klasör yoksa
    boş demet (asistan yalnız araç verisiyle çalışır); boş dosya atlanır."""
    d = Path(directory)
    if not d.is_dir():
        return ()
    out: List[HelpDoc] = []
    for p in sorted(d.glob("*.md"), key=lambda x: x.name):
        text = p.read_text(encoding="utf-8").strip()
        if text:
            out.append(parse_doc(text, p.stem))
    return tuple(out)


# ─── Arama ───────────────────────────────────────────────────────────────────

def _common_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _match(qt: str, dt: str) -> float:
    if qt == dt:
        return M_EXACT
    short, long_ = (qt, dt) if len(qt) <= len(dt) else (dt, qt)
    if len(short) < 4:
        # 3 harfli kök ("lot" ~ "lotları", "skt" ~ "sktsi"): yalnız kısa ek
        if len(short) == 3 and long_.startswith(short) and len(long_) <= len(short) + SHORT_SUFFIX_MAX:
            return M_SHORT
        return 0.0
    if long_.startswith(short):
        return M_PREFIX
    if long_.startswith(short[:-1]) and long_[len(short) - 1] == _SOFTEN.get(short[-1]):
        return M_SOFT
    if len(short) >= 6:
        cp = _common_prefix(short, long_)
        if cp >= 4 and cp >= len(short) - 2:
            return M_STEM
    return 0.0


def _best(qt: str, toks: Sequence[str]) -> float:
    best = 0.0
    for t in toks:
        m = _match(qt, t)
        if m > best:
            best = m
            if best >= M_EXACT:
                break
    return best


@dataclass(frozen=True)
class _Index:
    doc: HelpDoc
    title: Tuple[str, ...]
    title_words: Tuple[str, ...]              # yalnız başlık sözcükleri (kapsama oranı)
    meta: Tuple[str, ...]
    body: Tuple[Tuple[str, int], ...]        # (sözcük, gövdedeki geçiş sayısı)
    bigrams: Tuple[Tuple[str, str], ...]     # tüm metinde ardışık sözcük çiftleri


def _index(doc: HelpDoc) -> _Index:
    def uniq(xs):
        return tuple(dict.fromkeys(xs))
    seq = tokens(doc.title) + tokens(doc.yetki) + tokens(doc.sayfa) + tokens(doc.body)
    counts: Dict[str, int] = {}
    for t in tokens(doc.body):
        counts[t] = counts.get(t, 0) + 1
    return _Index(doc, uniq(tokens(doc.title) + doc.id.split("_")), uniq(tokens(doc.title)),
                  uniq(tokens(doc.yetki) + tokens(doc.sayfa)), tuple(counts.items()),
                  uniq(zip(seq, seq[1:])))


def _body_score(qt: str, body: Sequence[Tuple[str, int]]) -> float:
    """En iyi eşleşme × geçiş sıklığı çarpanı (1 + %25 × ek geçiş, en çok ×2)."""
    best, tf = 0.0, 0
    for t, n in body:
        m = _match(qt, t)
        if m > 0:
            tf += n
            best = max(best, m)
    return best * (1.0 + TF_STEP * min(tf - 1, TF_MAX_EXTRA)) if best else 0.0


def _phrase(a: str, b: str, bigrams: Sequence[Tuple[str, str]]) -> float:
    best = 0.0
    for x, y in bigrams:
        m = min(_match(a, x), _match(b, y))
        if m > best:
            best = m
            if best >= M_EXACT:
                break
    return best


_INDEX_CACHE: Dict[Tuple[HelpDoc, ...], Tuple[_Index, ...]] = {}


def _indexed(docs: Tuple[HelpDoc, ...]) -> Tuple[_Index, ...]:
    hit = _INDEX_CACHE.get(docs)
    if hit is None:
        hit = tuple(_index(d) for d in docs)
        if len(_INDEX_CACHE) < 8:
            _INDEX_CACHE[docs] = hit
    return hit


def query_tokens(q: str) -> List[str]:
    s = " " + " ".join(tokens(q)) + " "
    for phrase, alias in _QUERY_ALIASES:
        s = s.replace(f" {phrase} ", f" {alias} ")
    return list(dict.fromkeys(t for t in s.split() if len(t) >= 2 and t not in _STOPWORDS))


def score_docs(docs: Sequence[HelpDoc], q: str) -> List[Tuple[float, HelpDoc]]:
    """[(skor, doc)] skora göre azalan, eşitlikte dosya sırası; skoru 0 olan yok.
    Skor = Σ idf(sözcük) × max(başlık, Yetki/Sayfa, gövde eşleşmesi × ağırlık)
    + ardışık sözcük çifti metinde de ardışıksa ifade bonusu; × başlık kapsaması
    (1 + 0,3 × sorguyla eşleşen başlık sözcüğü oranı); sorgunun tamamı başlıkta
    geçiyorsa ×1,5."""
    docs = tuple(docs)
    idx = _indexed(docs)
    qts = query_tokens(q)
    if not qts or not idx:
        return []
    n = len(idx)
    per_doc: List[List[float]] = []
    df = [0] * len(qts)
    for ix in idx:
        row = []
        for k, qt in enumerate(qts):
            s = max(W_TITLE * _best(qt, ix.title), W_META * _best(qt, ix.meta),
                    W_BODY * _body_score(qt, ix.body))
            if s > 0:
                df[k] += 1
            row.append(s)
        per_doc.append(row)
    idf = [math.log(1.0 + n / float(d)) if d else 0.0 for d in df]
    qfold = " ".join(qts)
    out = []
    for order, (ix, row) in enumerate(zip(idx, per_doc)):
        s = sum(w * v for w, v in zip(idf, row))
        if s <= 0:
            continue
        for k in range(len(qts) - 1):
            if row[k] > 0 and row[k + 1] > 0:
                s += W_PHRASE * (idf[k] + idf[k + 1]) * _phrase(qts[k], qts[k + 1], ix.bigrams)
        if ix.title_words:
            hit = sum(1 for t in ix.title_words if any(_match(qt, t) > 0 for qt in qts))
            s *= 1.0 + W_TITLE_COV * hit / len(ix.title_words)
        if len(qts) > 1 and qfold in " ".join(tokens(ix.doc.title)):
            s *= 1.5
        out.append((round(s, 6), order, ix.doc))
    out.sort(key=lambda x: (-x[0], x[1]))
    return [(s, d) for s, _, d in out]


def _compact(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def search(docs: Sequence[HelpDoc], q, limit: int) -> dict:
    """`search_help` sonucu: {"query","topics":[{id,title,text}≤2],"others":[{id,title}≤4]}.
    Kesin id eşleşmesi (TR-katlı, `-`/boşluk/`_` toleranslı, sayı öneki/.md
    serbest) o konuyu tek başına döndürür.  2. konu yalnız skoru yakınsa VE
    sonuç `limit`e sığıyorsa tam metinle gelir — birinci konunun metni ASLA
    kırpılmaz.  Eşleşme yoksa `{"topics":[],"others":[],"note":…}`; yalnız zayıf
    eşleşme (en iyi skor < `MIN_TOPIC_SCORE`) varsa topics yine boş, zayıf adaylar
    `others`'ta ve aynı not."""
    docs = tuple(docs)
    query = " ".join(str(q or "").split())[:QUERY_ECHO_MAX]
    by_id = {d.id: d for d in docs}
    exact = by_id.get(_query_id(query))
    ranked = score_docs(docs, query)
    if exact is not None:
        topics = [exact]
        rest = [d for _, d in ranked if d.id != exact.id]
    elif ranked and ranked[0][0] < MIN_TOPIC_SCORE:
        return {"query": query, "topics": [],
                "others": [{"id": d.id, "title": d.title} for _, d in ranked[:OTHERS_MAX]],
                "note": NOT_FOUND_NOTE}
    elif ranked:
        top = ranked[0][0]
        topics = [ranked[0][1]]
        rest = []
        for s, d in ranked[1:]:
            if len(topics) < TOPICS_MAX and s >= SECOND_TOPIC_RATIO * top:
                topics.append(d)
            else:
                rest.append(d)
    else:
        return {"query": query, "topics": [], "others": [], "note": NOT_FOUND_NOTE}

    def build(tps: List[HelpDoc], others: List[HelpDoc]) -> dict:
        return {"query": query,
                "topics": [{"id": d.id, "title": d.title, "text": d.text} for d in tps],
                "others": [{"id": d.id, "title": d.title} for d in others[:OTHERS_MAX]]}

    payload = build(topics, rest)
    # Sığmıyorsa: önce 2. konu adaya iner, sonra adaylar azalır.
    while len(_compact(payload)) > limit and len(topics) > 1:
        rest.insert(0, topics.pop())
        payload = build(topics, rest)
    others_n = min(OTHERS_MAX, len(rest))
    while len(_compact(payload)) > limit and others_n > 0:
        others_n -= 1
        payload = build(topics, rest[:others_n])
    return payload
