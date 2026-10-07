# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Satın Alma Planı — hesap motoru.

"Şu ürünlerden şu adetlerde üreteceğiz → hangi malzemeden ne kadar almalıyız?"
sorusunu Rusya siparişi için yazılan tek seferlik betiklerin
(`~/Desktop/Claude/Rusya-Siparis`: build.py, urunler.py) kurallarıyla, ama
sistemin kendi verisinden yanıtlar.  Fiyat/tedarikçi katmanı ayrı:
`core/purchase_pricing.py`.

İki parça:
  • `load_inputs(db, req, domain)` — TEK DB okuyucu.  Bütün sorgular aktif
    panele (domain) kapsanır; panel dışı id → `PlanInputError` ("Bu panelde
    değil"), router 400'e çevirir.
  • `compute(inputs, req)` — SAF.  DB'siz; testler `ItemRec`/`RecipeRec`'i
    doğrudan kurar, kabul testi Rusya JSON'larından `PlanInputs` üretir.

Tüketim kuralı `core/consumption.expand_recipe` (üretimle birebir) — burada
yeniden yazılmaz.  Bu modülün kendi kuralları:
  • Sanal birleştirme: aynı katlanmış ad + birim → stoklar toplanır.  DB'ye
    DOKUNULMAZ; lab'ın "farklı" dediği (`DuplicateItemDecision.status='kept'`)
    kümeler ve aynı "aynı malzeme" grubundaki kartlar (`Item.
    material_group_id`) birleştirilmez.
  • "Aynı malzeme" grubu ihtiyacı azaltmaz: gruptaki diğer kartlar satıra
    `material_group.alts` olarak yazılır, stoğu varsa yalnız bilgi uyarısı
    (`group_alt_stock`) çıkar — lab onları ayrı ürün sayıyor.  TEK İSTİSNA
    (`count_phase_out_stock`, varsayılan açık, yalnız net mod): kartının
    tedarikçisi "bitirilecek" (ya da bu malzemede "alma") olan grup kartının
    stoğu önce kullanılacağı için ihtiyaçtan düşülür — satırlar arasında
    ORTAK havuzdan, aynı stok iki kez sayılmaz; planda kendi satırı olan kart
    sayılmaz.  Uyarı `phase_out_stock`.
  • Negatif kart stoğu 0 sayılır (build.py ham toplamı kullanıyordu).
  • `safe_triplet` = urunler.triple5'in birebir uyarlaması: gereken YUKARI,
    elimizde AŞAĞI yuvarlanır → yazılan rakamlarla alınacak = gereken −
    elimizde tutar ve gerçek eksikten asla az olmaz.
  • Uyarılar (cautions) Türkçe, `{code, severity, text}`; Rusya listesindeki
    elle yazılmış notların otomatik karşılığı.
"""
import difflib
import math
import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Set, Tuple

from core.brands import product_brand
from core.consumption import (ItemRec, RecipeRec, _kind, expand_recipe, item_rec,
                              load_recipe_recs, parse_ml)
from core.purchase_plan_models import (ExtraPkgIn, HeldItemIn, ManualLineIn,  # noqa: F401  (re-export)
                                       PlanLineIn, PlanOptionsIn, PlanRequest, WastePctIn)

# ─── Sabitler ───────────────────────────────────────────────────────────────

# AppSetting: JSON int listesi.  Panel başına anahtar '<bu>.<domain>' önceliklidir;
# domain'siz genel anahtar yalnız o panelin kartlarına süzülür
# (bkz. resolve_default_excluded).
CFG_DEFAULT_EXCLUDED = "purchase_plan.default_excluded"
# AppSetting yoksa varsayılan hariç: bu katlanmış adlı kartlar (saf su kendi üretimimiz)
DEFAULT_EXCLUDED_NAMES = ("DISTILESU", "SAFSU")

# Büyük ikinci-kart stoğu → depoda teyit eşiği (build.py TEYIT_ESIK); l = kg gibi
OTHER_CARD_THRESHOLD = {"g": 10000.0, "ml": 10000.0, "kg": 10.0, "l": 10.0, "adet": 1000.0}

LOOKALIKE_RATIO = 0.85
LOOKALIKE_MAX = 6
RECIPE_SIZE_TOLERANCE = 0.10          # reçete hammadde toplamı ↔ boy farkı (%10)
# TEK hammaddenin 1 adetlik miktarı (g/ml) ürün boyunun bu katını aşarsa birim
# hatası şüphesi (TUZ vakası: kart 'kg', reçetede 269,5 → 350 ml'lik peelinge
# 269,5 kg).  Boy toleransından (%10) bilinçli olarak geniş: yoğunluğu yüksek
# üründe (tuz peelingi) tek bileşenin gramı boyun ml'sine yaklaşabilir, ama
# hiçbir kozmetikte ikiye katlayamaz; kg↔g karışıklığı ise ×1000 fark verir.
SUSPECT_UNIT_FACTOR = 2.0

KINDS = ("raw", "packaging", "label")
KIND_LABEL = {"raw": "hammadde", "packaging": "ambalaj", "label": "etiket"}
LABEL_MODE_TEXT = {"TR": "Türkçe etiket kartları", "EN": "İngilizce etiket kartları",
                   "exclude": "hesaba katılmadı"}

SAMPLE_CONVERTED_MARK = "Numune stoğa çevrildi"


class PlanInputError(ValueError):
    """Kullanıcıya gösterilebilir girdi hatası (router → 400)."""


# ─── Girdi kayıtları (saf; DB'siz kurulabilir) ──────────────────────────────

@dataclass(frozen=True)
class LotRec:
    item_id: int
    lot_number: str = ""
    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = None
    is_sample: bool = False
    created_at: Optional[datetime] = None
    quantity: float = 0.0
    inventory_id: Optional[int] = None    # fiyat katmanı lot_meta / analiz eşlemesi için


@dataclass(frozen=True)
class OpenOrderRec:
    """Açık `StockOrderFlag` — "sipariş verildi, henüz gelmedi"."""
    item_id: int
    quantity: Optional[float] = None
    unit: Optional[str] = None
    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = None
    expected_date: Optional[date] = None


@dataclass(frozen=True)
class DecisionRec:
    """`DuplicateItemDecision` kümesi — yalnız 'kept' ve 'pending' yüklenir."""
    item_ids: frozenset
    status: str                       # kept | pending
    title: str = ""


@dataclass
class PlanInputs:
    items: Dict[int, ItemRec]
    recipes: Dict[int, RecipeRec]
    label_siblings: Dict[str, Dict[str, ItemRec]] = field(default_factory=dict)
    recipe_by_target: Dict[int, int] = field(default_factory=dict)
    open_orders: Dict[int, List[OpenOrderRec]] = field(default_factory=dict)
    decisions: List[DecisionRec] = field(default_factory=list)
    lots: Dict[int, List[LotRec]] = field(default_factory=dict)
    converted_lots: Set[Tuple[int, str]] = field(default_factory=set)
    stock_as_of: Optional[datetime] = None
    domain: str = "cosmetics"
    default_excluded: Tuple[int, ...] = ()
    warnings: List[str] = field(default_factory=list)
    # "Aynı malzeme" grupları: kart id → grup id, grup id → ad (yalnız aktif
    # gruplar, bu panel).  Boş varsayılan — Rusya fixture'ı etkilenmez.
    mgroup_of: Dict[int, int] = field(default_factory=dict)
    mgroup_names: Dict[int, str] = field(default_factory=dict)
    # Tedarikçi tercihleri ("bitirilecek" stok hesabı) — boş varsayılan.
    # card_supplier: kart → tedarikçi id; supplier_status: tedarikçi id →
    # normal|preferred|phase_out (firma anahtarı düzeyinde: mükerrer
    # kartlardan biri bitirilecekse hepsi); avoid: kart → bu malzemede
    # "alma" denen tedarikçi id'leri (grup tercihleri kartlara açılmış,
    # mükerrer firma kartları dahil); supplier_names: uyarı metni için.
    card_supplier: Dict[int, int] = field(default_factory=dict)
    supplier_status: Dict[int, str] = field(default_factory=dict)
    avoid: Dict[int, Set[int]] = field(default_factory=dict)
    supplier_names: Dict[int, str] = field(default_factory=dict)

    def recipe_item_ids(self) -> Set[int]:
        """Herhangi bir aktif reçetede geçen kalemler (birleştirme kökü seçimi)."""
        out: Set[int] = set()
        for r in self.recipes.values():
            out.update(ing.item_id for ing in r.ingredients)
        return out


# ─── Biçim / katlama yardımcıları ───────────────────────────────────────────

def alnum_fold(name) -> str:
    """build.py `norm`: büyük harf, TR harf katlama, NFKD, yalnız harf+rakam."""
    t = (name or "").upper()
    for a, b in (("İ", "I"), ("Ş", "S"), ("Ğ", "G"), ("Ü", "U"), ("Ö", "O"), ("Ç", "C")):
        t = t.replace(a, b)
    t = unicodedata.normalize("NFKD", t)
    return "".join(ch for ch in t if ch.isalnum())


def tr_num(x, d: int = 0) -> str:
    """Türkçe sayı: 1.234,5"""
    return f"{x:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def clean_text(v) -> Optional[str]:
    s = " ".join(str(v).split()) if v is not None else ""
    return s or None


def unit_norm(unit) -> str:
    """Kalem birimi → g | kg | ml | l | adet (bilinmeyen = sayılan birim)."""
    u = (unit or "").strip().lower()
    if u in ("g", "gr", "gram"):
        return "g"
    if u == "kg":
        return "kg"
    if u == "ml":
        return "ml"
    if u in ("l", "lt", "litre", "liter"):
        return "l"
    return "adet"


def _amount_near(v, unit) -> str:
    """urunler._amount — en yakın yuvarlama (benzer kart / bilgi metinleri)."""
    u = unit_norm(unit)
    if u == "g":
        return f"{tr_num(v / 1000, 1)} kg" if v >= 1000 else f"{tr_num(v)} gram"
    if u == "kg":
        return f"{tr_num(v, 1)} kg"
    if u == "ml":
        return f"{tr_num(v / 1000, 1)} litre" if v >= 1000 else f"{tr_num(v)} ml"
    if u == "l":
        return f"{tr_num(v, 1)} litre"
    return f"{tr_num(v)} adet"


def _amount_rounded(v, unit, mode: str = "near") -> str:
    """build.py `amount`: up (eksik — gerçekten az gösterilmez) / down (stok —
    fazla gösterilmez) / near."""
    u = unit_norm(unit)
    if mode != "near":
        step = {"g": 100 if v >= 1000 else 1, "ml": 100 if v >= 1000 else 1,
                "kg": 0.1, "l": 0.1}.get(u, 1)
        v = (math.ceil(v / step - 1e-9) if mode == "up" else math.floor(v / step + 1e-9)) * step
    if v < 0.5 and u not in ("kg", "l"):
        return "yok"
    if u in ("kg", "l") and v <= 0:
        return "yok"
    return _amount_near(v, u)


# ─── Güvenli yuvarlama (urunler.triple5) ────────────────────────────────────

def _stock_down(stock, unit) -> Tuple[str, float]:
    """triple5'in 'elimizde' kuralı tek başına — AŞAĞI yuvarlanmış (metin,
    kg/l/adet sayısı).  Brüt moddaki bilgi sütunları için."""
    s = max(0.0, float(stock or 0.0))
    u = unit_norm(unit)
    if u in ("g", "ml", "kg", "l"):
        s_ = s * 1000 if u in ("kg", "l") else s
        big, small = ("kg", "gram") if u in ("g", "kg") else ("litre", "ml")
        if s_ >= 1000:
            b = math.floor(s_ / 100 + 1e-9) / 10
            return f"{tr_num(b, 1)} {big}", b
        b = math.floor(s_ + 1e-9)
        return (f"{tr_num(b)} {small}" if b > 0 else "yok"), b / 1000
    b = math.floor(s + 1e-9)
    return (f"{tr_num(b)} adet" if b > 0 else "yok"), float(b)


def safe_triplet(need, stock, unit, *, safe: bool = True, open_qty=0.0) -> dict:
    """(gereken, elimizde, [yolda,] alınacak) gösterimi + sayıları.

    `safe=True` → urunler.triple5'in BİREBİR uyarlaması:
      • g/ml/kg(/l): küçük birime (g/ml) çevrilir.  gereken ≥ 1000 VE
        gereken − elimizde ≥ 1000 ise gereken YUKARI, elimizde AŞAĞI 0,1 kg/l'ye
        yuvarlanır, alınacak = ikisinin farkı; değilse küçük birimde tamsayı
        tavan/taban.
      • adet: gereken tavan, elimizde taban.
    `safe=False` → düz (en yakın) yuvarlama.
    Negatif stok 0'a çekilir.  `buy_num` DAİMA kg / l / adet cinsindendir
    (`buy_unit`) — fiyat bu sayıyla çarpılır.

    `open_qty` (açık siparişler, "yolda") elimizdekine KATILMAZ: ayrı aşağı
    yuvarlanır, `open_text`/`open_num` olarak döner ve alınacak = gereken −
    elimizde − yolda olur.  "Elimizde" sütunu yoldakini içerseydi raporda
    (PDF/Excel'de ayrı Elimizde + Yolda sütunları, Excel G = D − E − F) yoldaki
    mal iki kez düşülmüş görünürdü.  İki ayrı taban, toplamın tabanından ≤
    olduğu için alınacak yine gerçek eksiğin altına inmez.  `open_qty=0` iken
    sonuç triple5 ile aynıdır.
    `need_num`/`stock_num`/`open_num` yazılan metinlerin kg/l/adet sayılarıdır;
    `safe=True` iken `max(0, round(need_num − stock_num − open_num, 3))` =
    `buy_num` (Excel'in G = MAX(0, ROUND(D−E−F, 3)) formülü aynı sayıyı verir).
    """
    need = max(0.0, float(need or 0.0))
    stock = max(0.0, float(stock or 0.0))
    oq = max(0.0, float(open_qty or 0.0))
    t = tr_num
    u = unit_norm(unit)
    if u in ("g", "ml", "kg", "l"):
        k = 1000 if u in ("kg", "l") else 1
        n, s_, o_ = need * k, stock * k, oq * k
        big, small = ("kg", "gram") if u in ("g", "kg") else ("litre", "ml")
        buy_unit = "kg" if big == "kg" else "l"
        if not safe:
            q = max(n - s_ - o_, 0.0)

            def near(v):
                return f"{t(v / 1000, 1)} {big}" if v >= 1000 else f"{t(v)} {small}"
            return {"need_text": near(n), "stock_text": near(s_) if s_ >= 0.5 else "yok",
                    "open_text": near(o_) if o_ >= 0.5 else "yok", "buy_text": near(q),
                    "buy_num": round(q / 1000, 6), "buy_unit": buy_unit,
                    "need_num": round(n / 1000, 6), "stock_num": round(s_ / 1000, 6),
                    "open_num": round(o_ / 1000, 6)}
        if n >= 1000 and n - s_ - o_ >= 1000:
            a = math.ceil(n / 100 - 1e-9) / 10
            b, c = math.floor(s_ / 100 + 1e-9) / 10, math.floor(o_ / 100 + 1e-9) / 10
            q = round(max(a - b - c, 0), 1)

            def down(x, x_):
                return (f"{t(x, 1)} {big}" if x > 0
                        else (f"{t(math.floor(x_))} {small}" if math.floor(x_) > 0 else "yok"))
            return {"need_text": f"{t(a, 1)} {big}", "stock_text": down(b, s_),
                    "open_text": down(c, o_), "buy_text": f"{t(q, 1)} {big}",
                    "buy_num": q, "buy_unit": buy_unit, "need_num": a, "stock_num": b, "open_num": c}
        a, b, c = math.ceil(n - 1e-9), math.floor(s_ + 1e-9), math.floor(o_ + 1e-9)
        q = max(a - b - c, 0)
        return {"need_text": f"{t(a)} {small}", "stock_text": (f"{t(b)} {small}" if b > 0 else "yok"),
                "open_text": (f"{t(c)} {small}" if c > 0 else "yok"),
                "buy_text": f"{t(q)} {small}", "buy_num": q / 1000, "buy_unit": buy_unit,
                "need_num": a / 1000, "stock_num": b / 1000, "open_num": c / 1000}
    if not safe:
        q = math.floor(round(max(need - stock - oq, 0.0), 9) + 0.5)    # 9 hane: 10,2−3,7 = 6,4999… gürültüsü
        b = math.floor(round(stock, 9) + 0.5)
        c = math.floor(round(oq, 9) + 0.5)
        return {"need_text": f"{t(need)} adet", "stock_text": (f"{t(b)} adet" if b > 0 else "yok"),
                "open_text": (f"{t(c)} adet" if c > 0 else "yok"),
                "buy_text": f"{t(q)} adet", "buy_num": q, "buy_unit": "adet",
                "need_num": round(need, 6), "stock_num": b, "open_num": c}
    a, b, c = math.ceil(need - 1e-9), math.floor(stock + 1e-9), math.floor(oq + 1e-9)
    q = max(a - b - c, 0)
    return {"need_text": f"{t(a)} adet", "stock_text": (f"{t(b)} adet" if b > 0 else "yok"),
            "open_text": (f"{t(c)} adet" if c > 0 else "yok"),
            "buy_text": f"{t(q)} adet", "buy_num": q, "buy_unit": "adet",
            "need_num": a, "stock_num": b, "open_num": c}


def purchase_display(need, stock, open_qty, unit, *, mode: str = "net", safe: bool = True) -> dict:
    """Satır gösterimi.  `stock` = YALNIZ elimizdeki (kart stoklarının toplamı),
    `open_qty` = yoldaki (açık sipariş; seçenek kapalıysa 0).  Net modda
    alınacak = gereken − elimizde − yolda.  Brüt modda gereken = alınacak;
    elimizde/yolda yalnız bilgidir."""
    if mode == "gross":
        d = safe_triplet(need, 0.0, unit, safe=safe)
        d["stock_text"], d["stock_num"] = _stock_down(stock, unit)
        d["open_text"], d["open_num"] = _stock_down(open_qty, unit)
        d["need_text"], d["need_num"] = d["buy_text"], d["buy_num"]
        return d
    return safe_triplet(need, stock, unit, safe=safe, open_qty=open_qty)


# ─── Kayıt yardımcıları ─────────────────────────────────────────────────────

def _caution(code: str, text: str, severity: str = "warn") -> dict:
    return {"code": code, "severity": severity, "text": text}


def _r6(x) -> float:
    return round(float(x or 0.0), 6)


def _cover_qty(need, stock, open_qty, unit) -> float:
    """Güvenli yuvarlamalı gösterimde (`safe_triplet`, eksik < 1 kg/l iken
    küçük birimde tamsayı: gereken tavan, elimizde/yolda taban) alınacağı
    0 yapan en küçük ek stok — kart biriminde.  Bitirilecek havuzundan
    eksiği tam kapatan alım bu kadar olur."""
    u = unit_norm(unit)
    k = 1000 if u in ("kg", "l") else 1
    n = max(0.0, float(need or 0.0)) * k
    s_ = max(0.0, float(stock or 0.0)) * k
    o_ = max(0.0, float(open_qty or 0.0)) * k
    return max(0.0, (math.ceil(n - 1e-9) - math.floor(o_ + 1e-9) - s_) / k)


def _digits(s: str) -> Tuple[str, ...]:
    return tuple(re.findall(r"\d+", s))


def _word_tokens(name) -> List[str]:
    return [t for t in re.split(r"[^0-9a-z]+", alnum_spaced(name)) if t]


def alnum_spaced(name) -> str:
    """alnum_fold'un kelime sınırlarını koruyan küçük harfli hâli."""
    return " ".join(alnum_fold(w).lower() for w in re.split(r"[\s\-/.,()%+&]+", name or "") if alnum_fold(w))


def _single_short_token_diff(a, b) -> bool:
    """'E VİTAMİNİ' ↔ 'C VİTAMİNİ': tek kelime farkı ve iki kelime de ≤2 harf →
    farklı malzeme (difflib oranı yüksek çıksa da benzer sayılmaz)."""
    ta, tb = _word_tokens(a), _word_tokens(b)
    if len(ta) != len(tb):
        return False
    diff = [(x, y) for x, y in zip(ta, tb) if x != y]
    return len(diff) == 1 and len(diff[0][0]) <= 2 and len(diff[0][1]) <= 2


# ─── "Aynı malzeme" grupları ────────────────────────────────────────────────

def material_group_members(inputs: PlanInputs) -> Dict[int, List[int]]:
    """{grup id: [paneldeki üye kart id'leri]} (pasif kartlar dahil; alternatif
    listesi aktifleri ayrıca süzer)."""
    out: Dict[int, List[int]] = {}
    for iid, gid in inputs.mgroup_of.items():
        it = inputs.items.get(iid)
        if it is None or it.domain != inputs.domain:
            continue
        out.setdefault(gid, []).append(iid)
    return {g: sorted(v) for g, v in out.items()}


def _kept_sets(inputs: PlanInputs) -> List[frozenset]:
    """Birleştirilmeyecek / benzer-ad uyarısı bastırılacak kümeler: lab'ın
    'kept' kararları + "aynı malzeme" grupları (lab ayrı kart tutuyor)."""
    kept = [d.item_ids for d in inputs.decisions if d.status == "kept"]
    kept += [frozenset(ids) for ids in material_group_members(inputs).values() if len(ids) >= 2]
    return kept


# ─── Birleştirme haritası ───────────────────────────────────────────────────

def merge_map(inputs: PlanInputs, opts: PlanOptionsIn) -> Tuple[Dict[int, int], Dict[int, List[int]]]:
    """Sanal birleştirme → (root_of {item_id: kök}, members {kök: [üyeler]}).

    Otomatik grup: aktif Hammadde/Ambalaj kartları (etiket hariç) katlanmış ad
    + birimle.  Lab'ın 'kept' kararı ya da bir "aynı malzeme" grubu otomatik
    grubun ≥2 üyesini kapsıyorsa grup birleştirilmez.  `manual_merges`
    çiftleri (açık kullanıcı kararı) daima
    uygulanır.  Kök: grubun reçetelerde kullanılan en küçük id'li üyesi, yoksa
    en küçük id.  Yalnız bu hesap içindir; DB değişmez.
    """
    items = inputs.items
    parent: Dict[int, int] = {}

    def find(i):
        while parent.get(i, i) != i:
            parent[i] = parent.get(parent[i], parent[i])
            i = parent[i]
        return i

    def union(a, b):
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    if opts.merge_duplicates:
        kept = _kept_sets(inputs)
        groups: Dict[tuple, List[int]] = {}
        for it in items.values():
            if not it.is_active or it.domain != inputs.domain:
                continue
            if it.category not in ("Hammadde", "Ambalaj") or _kind(it) == "label":
                continue
            groups.setdefault((alnum_fold(it.name), unit_norm(it.unit)), []).append(it.id)
        for ids in groups.values():
            if len(ids) < 2:
                continue
            if any(len(k & set(ids)) >= 2 for k in kept):
                continue
            for i in ids[1:]:
                union(ids[0], i)
    for a, b in opts.manual_merges or []:
        if a in items and b in items and a != b:
            union(int(a), int(b))

    clusters: Dict[int, List[int]] = {}
    for i in list(parent.keys()):
        clusters.setdefault(find(i), []).append(i)
    used = inputs.recipe_item_ids()
    root_of: Dict[int, int] = {}
    members: Dict[int, List[int]] = {}
    for ids in clusters.values():
        ids = sorted(set(ids))
        if len(ids) < 2:
            continue
        in_recipes = [i for i in ids if i in used]
        root = min(in_recipes) if in_recipes else ids[0]
        members[root] = ids
        for i in ids:
            root_of[i] = root
    return root_of, members


# ─── DB yükleyici ───────────────────────────────────────────────────────────

def _json_int_list(raw) -> Optional[List[int]]:
    import json
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(v, list):
        return None
    out = []
    for x in v:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def load_inputs(db, req: PlanRequest, domain: str) -> PlanInputs:
    """Bütün DB okumaları — aktif panele kapsanmış.  Panel dışı / var olmayan
    reçete-kalem id'si `PlanInputError("Bu panelde değil: …")` atar; pasif
    reçete de hata (senaryo silinmiş reçeteye bakıyorsa sessizce düşmesin).
    """
    from database import (AppSetting, DuplicateItemDecision, Inventory, Item, MaterialGroup, Recipe,
                          StockOrderFlag, Supplier, Transaction)

    # Paneldeki BÜTÜN kalemler (pasifler dahil; birleştirme/benzer kart aktif
    # olanlara bakar, pasif kart yalnız açıkça istenirse kullanılır)
    items: Dict[int, ItemRec] = {it.id: item_rec(it)
                                 for it in db.query(Item).filter(Item.domain == domain).all()}

    rec_rows = (db.query(Recipe.id, Recipe.target_item_id, Recipe.is_active, Recipe.created_at)
                .filter(Recipe.domain == domain).all())
    domain_recipe = {r.id: r for r in rec_rows}

    # ── İstek id doğrulaması ────────────────────────────────────────────────
    foreign, inactive_recipes = [], []
    for ln in req.lines:
        if ln.recipe_id is not None:
            r = domain_recipe.get(ln.recipe_id)
            if r is None:
                foreign.append(f"reçete #{ln.recipe_id}")
            elif not r.is_active:
                inactive_recipes.append(f"#{ln.recipe_id}")
        if ln.target_item_id is not None and ln.target_item_id not in items:
            foreign.append(f"ürün #{ln.target_item_id}")
        for ex in ln.extra_packaging:
            if ex.item_id is not None and ex.item_id not in items:
                foreign.append(f"ambalaj #{ex.item_id}")
    o = req.options
    opt_ids = set(o.excluded_item_ids or []) | {h.item_id for h in o.held_items} \
        | {int(k) for k in o.item_notes} | {i for pair in o.manual_merges for i in pair}
    for iid in sorted(opt_ids):
        if iid not in items:
            foreign.append(f"kalem #{iid}")
    if foreign:
        raise PlanInputError("Bu panelde değil: " + ", ".join(dict.fromkeys(foreign)))
    if inactive_recipes:
        raise PlanInputError("Reçete pasif ya da silinmiş: " + ", ".join(inactive_recipes))

    warnings: List[str] = []
    for iid in sorted(opt_ids):
        if not items[iid].is_active:
            warnings.append(f"Pasif kart seçeneklerde: {items[iid].name} (#{iid})")

    # ── Reçeteler (paneldeki tüm aktifler: kök seçimi + hedef eşlemesi) ─────
    active_ids = [r.id for r in rec_rows if r.is_active]
    recs, ritems, siblings = load_recipe_recs(db, active_ids, domain)
    for k, v in ritems.items():
        items.setdefault(k, v)           # panel dışı kardeş/kalem yalnız referans için
    recipes = {r.id: r for r in recs}
    recipe_by_target: Dict[int, int] = {}
    newest: Dict[int, tuple] = {}
    for r in rec_rows:
        if not r.is_active or not r.target_item_id:
            continue
        key = (r.created_at or datetime.min, r.id)
        if r.target_item_id not in newest or key > newest[r.target_item_id]:
            newest[r.target_item_id] = key
            recipe_by_target[r.target_item_id] = r.id

    # ── Açık siparişler ─────────────────────────────────────────────────────
    open_orders: Dict[int, List[OpenOrderRec]] = {}
    flags = (db.query(StockOrderFlag, Supplier.name)
             .outerjoin(Supplier, Supplier.id == StockOrderFlag.supplier_id)
             .filter(StockOrderFlag.domain == domain, StockOrderFlag.closed_at.is_(None))
             .all())
    for f, sname in flags:
        open_orders.setdefault(f.item_id, []).append(OpenOrderRec(
            item_id=f.item_id, quantity=f.quantity, unit=f.unit, supplier_id=f.supplier_id,
            supplier_name=sname, expected_date=f.expected_date))

    # ── Kopya kart kararları (domain kolonu yok → üyeler panelde mi) ───────
    decisions: List[DecisionRec] = []
    for d in (db.query(DuplicateItemDecision)
              .filter(DuplicateItemDecision.status.in_(("kept", "pending"))).all()):
        ids = _json_int_list(d.item_ids) or []
        ids = frozenset(i for i in ids if i in items and items[i].domain == domain)
        if len(ids) >= 2:
            decisions.append(DecisionRec(item_ids=ids, status=d.status, title=d.title or ""))

    # ── Lotlar (hammadde/ambalaj) + numuneden çevrilenler ───────────────────
    lots: Dict[int, List[LotRec]] = {}
    lot_rows = (db.query(Inventory.item_id, Inventory.lot_number, Inventory.supplier_id,
                         Inventory.is_sample, Inventory.created_at, Inventory.quantity,
                         Supplier.name)
                .join(Item, Item.id == Inventory.item_id)
                .outerjoin(Supplier, Supplier.id == Inventory.supplier_id)
                .filter(Inventory.domain == domain, Item.domain == domain,
                        Item.category.in_(("Hammadde", "Ambalaj")))
                .all())
    for (iid, lot_no, sid, is_s, cat, qty, sname) in lot_rows:
        lots.setdefault(iid, []).append(LotRec(
            item_id=iid, lot_number=lot_no or "", supplier_id=sid, supplier_name=sname,
            is_sample=bool(is_s), created_at=cat, quantity=float(qty or 0.0)))
    converted = {(iid, lot or "") for (iid, lot) in
                 db.query(Transaction.item_id, Transaction.lot_number)
                 .join(Item, Item.id == Transaction.item_id)
                 .filter(Item.domain == domain,
                         Transaction.notes.like(f"%{SAMPLE_CONVERTED_MARK}%")).all()}

    # ── "Aynı malzeme" grupları (aktif, bu panel) — tek sorgu ───────────────
    mgroup_of: Dict[int, int] = {}
    mgroup_names: Dict[int, str] = {}
    for iid, gid, gname in (db.query(Item.id, MaterialGroup.id, MaterialGroup.name)
                            .join(MaterialGroup, MaterialGroup.id == Item.material_group_id)
                            .filter(Item.domain == domain, MaterialGroup.domain == domain,
                                    MaterialGroup.is_active == True)          # noqa: E712
                            .all()):
        mgroup_of[iid] = gid
        mgroup_names[gid] = clean_text(gname) or f"Grup #{gid}"

    # ── Tedarikçi durumu + malzeme "alma" tercihleri ("bitirilecek" stok) ──
    card_supplier, supplier_status, avoid, supplier_names = _load_supplier_prefs(
        db, domain, items, mgroup_of)

    # ── Varsayılan hariçler ─────────────────────────────────────────────────
    cfg = {r.key: r.value for r in db.query(AppSetting).filter(
        AppSetting.key.in_((default_excluded_key(domain), CFG_DEFAULT_EXCLUDED))).all()}
    default_excluded = resolve_default_excluded(
        cfg.get(default_excluded_key(domain)), cfg.get(CFG_DEFAULT_EXCLUDED), items, domain)

    return PlanInputs(
        items=items, recipes=recipes, label_siblings=siblings,
        recipe_by_target=recipe_by_target, open_orders=open_orders, decisions=decisions,
        lots=lots, converted_lots=converted, stock_as_of=datetime.utcnow(), domain=domain,
        default_excluded=tuple(default_excluded), warnings=warnings,
        mgroup_of=mgroup_of, mgroup_names=mgroup_names,
        card_supplier=card_supplier, supplier_status=supplier_status, avoid=avoid,
        supplier_names=supplier_names,
    )


def _load_supplier_prefs(db, domain: str, items: Dict[int, ItemRec], mgroup_of: Dict[int, int]):
    """`load_inputs` yardımcısı — (card_supplier, supplier_status, avoid,
    supplier_names).  Durum `core.suppliers.status_by_key` ile firma anahtarı
    düzeyinde (TATLIDİLİMLER / TATLİDİLİMLER kartlarından biri bitirilecekse
    ikisi de); "alma" tercihleri gruptan kartlara açılır, kartın kendi
    tercihi aynı tedarikçinin grup satırını ezer, mükerrer firma kartları da
    kümeye girer."""
    from core.purchase_pricing import SupplierIndex
    from core.suppliers import effective_prefs, normalize_status, status_by_key
    from database import Item, MaterialSupplierPref, Supplier

    sups = db.query(Supplier).filter(Supplier.domain == domain).all()
    names = {s.id: s.name or "" for s in sups}
    ix = SupplierIndex(list(names.values()))
    by_key, _ = status_by_key(sups, ix)
    key_of = {sid: ix.key(n) for sid, n in names.items()}
    status = {s.id: by_key.get(key_of[s.id]) or normalize_status(s.purchase_status) or "normal"
              for s in sups}
    card_supplier = {iid: sid for iid, sid in (db.query(Item.id, Item.supplier_id)
                                               .filter(Item.domain == domain,
                                                       Item.supplier_id.isnot(None)).all())
                     if iid in items}
    per_item: Dict[int, list] = {}
    members_of: Dict[int, List[int]] = {}
    for iid, gid in mgroup_of.items():
        members_of.setdefault(gid, []).append(iid)
    for p in db.query(MaterialSupplierPref).filter(MaterialSupplierPref.domain == domain).all():
        rec = (p.supplier_id, p.preference, p.rank, p.item_id is not None)
        targets = [p.item_id] if p.item_id else members_of.get(p.material_group_id, [])
        for iid in targets:
            per_item.setdefault(iid, []).append(rec)
    by_firm: Dict[str, Set[int]] = {}
    for sid, k in key_of.items():
        if k:
            by_firm.setdefault(k, set()).add(sid)
    avoid: Dict[int, Set[int]] = {}
    for iid, rows in per_item.items():
        out: Set[int] = set()
        for sid, (pref, _rank) in effective_prefs(rows).items():
            if pref == "avoid":
                out.add(sid)
                out |= by_firm.get(key_of.get(sid) or "", set())
        if out:
            avoid[iid] = out
    return card_supplier, status, avoid, names


def default_excluded_ids(items: Dict[int, ItemRec], domain: str) -> List[int]:
    """AppSetting yoksa: adı DİSTİLE SU / SAF SU olan aktif panel kartları."""
    return sorted(i for i, it in items.items()
                  if it.is_active and it.domain == domain
                  and alnum_fold(it.name) in DEFAULT_EXCLUDED_NAMES)


def default_excluded_key(domain: str) -> str:
    """Panel başına varsayılan hariç anahtarı: 'purchase_plan.default_excluded.<domain>'."""
    return f"{CFG_DEFAULT_EXCLUDED}.{domain}"


def resolve_default_excluded(domain_raw, global_raw, items: Dict[int, ItemRec], domain: str) -> List[int]:
    """Varsayılan hariç kalemler (saf).  Öncelik:

      1. panel anahtarı (`default_excluded_key(domain)`) — açık karar; boş liste
         de geçerlidir ("bu panelde varsayılan hariç yok");
      2. genel anahtar (`CFG_DEFAULT_EXCLUDED`) — domain'siz olduğu için yalnız
         BU panelin kartlarına süzülür.  Süzülünce boş kalıyorsa (listede yalnız
         öbür panelin id'leri var) ad kuralına düşülür: Kozmetik için kaydedilen
         liste Supplement'in suyunu sessizce alım listesine sokmamalı.  Genel
         anahtar bilinçli `[]` ise hariç yok;
      3. ad kuralı (`default_excluded_ids`: DİSTİLE SU / SAF SU).
    Bozuk JSON anahtar yokmuş gibi atlanır.
    """
    def in_panel(ids):
        return [i for i in dict.fromkeys(ids) if i in items and items[i].domain == domain]

    ids = _json_int_list(domain_raw) if domain_raw else None
    if ids is not None:
        return in_panel(ids)
    ids = _json_int_list(global_raw) if global_raw else None
    if ids is not None:
        mine = in_panel(ids)
        if mine or not ids:
            return mine
    return default_excluded_ids(items, domain)


# ─── Hesap ──────────────────────────────────────────────────────────────────

_FACE_RE = re.compile(r"(?<![a-zçğıöşü0-9])(üst|yan|ön|arka)(?![a-zçğıöşü0-9])")
_LABEL_NOISE = {"en", "eng", "ing", "tr", "etiket", "etiketi"}


def _tr_lower(s: str) -> str:
    return (s or "").replace("İ", "i").replace("I", "ı").lower()


def face_of(name) -> Optional[str]:
    """Etiket adındaki yüz kelimesi: üst / yan / ön / arka (yoksa None)."""
    m = _FACE_RE.search(_tr_lower(name))
    return m.group(1) if m else None


def label_base(name) -> str:
    """Etiketin ürün tabanı: yüz kelimesi, dil eki (EN/TR) ve 'etiket' atılmış,
    katlanmış ad — aynı ürünün ön/arka (üst/yan) kartlarını eşlemek için."""
    t = _FACE_RE.sub(" ", _tr_lower(name))
    toks = [x for x in re.split(r"[^0-9a-zçğıöşüâîû]+", t) if x and x not in _LABEL_NOISE]
    return alnum_fold(" ".join(toks))


def label_faces_auto(groups: List[Tuple[str, Optional[str]]],
                     label_pool: Dict[str, Dict[Optional[str], Set[str]]]) -> Tuple[int, str, str]:
    """Yeni basılacak etiketin yüz sayısı → (yüz, metin, kaynak).

    build.py ~199-212'nin genişletilmiş hâli: reçetedeki etiket gruplarının
    adındaki yüz kelimeleri (üst/yan/ön/arka) + aynı ürün tabanına sahip diğer
    aktif etiket kartlarının yüz kelimeleri (reçetede yalnız 'Ön' kartı varken
    sistemde 'Arka' kartı da duruyorsa iki yüz).  Kartlar DİL BAŞINA takım
    sayılır ve yalnız reçetedeki etiketin kendi yüz kelimesini İÇEREN takımlar
    birleştirilir: aynı kavanozun EN takımı üst+yan, TR takımı ön+arka diye
    adlandırılmış olabiliyor (Su Bazlı Saç Maskesi 200 ml) — takımları
    körlemesine toplamak yüzü ikiye katlardı; kartın dil alanı boş olsa bile
    (adında 'TR' yazan dilsiz kart) doğru takım bulunur.
    Yüz kelimesi yoksa grup sayısı.  `groups` = [(etiket adı, dil)].
    """
    if not groups:
        return 1, "tek etiket (sistemde etiket tanımı yok; tasarıma göre)", "default"
    faces: List[str] = []
    plain = 0
    source = "recipe"
    for nm, _lang in groups:
        f = face_of(nm)
        if f is None:
            plain += 1
            continue
        if f not in faces:
            faces.append(f)
        for fset in label_pool.get(label_base(nm), {}).values():
            if f not in fset:
                continue
            for f2 in sorted(fset):
                if f2 not in faces:
                    faces.append(f2)
                    source = "siblings"
    order = {"üst": 0, "ön": 1, "yan": 2, "arka": 3}
    faces.sort(key=lambda f: order.get(f, 9))
    n = len(faces) + plain
    if not faces:
        return n, ("tek etiket" if n == 1 else f"{n} etiket"), source
    text = " + ".join(faces + (["tek etiket"] * plain))
    if n == 1:
        text += " (ikinci etiket gerekip gerekmediğine bakılmalı)"
    return n, text, source


def _label_pool(inputs: PlanInputs) -> Dict[str, Dict[Optional[str], Set[str]]]:
    """{etiket tabanı: {dil: {yüz kelimeleri}}} — paneldeki aktif etiket kartları."""
    pool: Dict[str, Dict[Optional[str], Set[str]]] = {}
    for it in inputs.items.values():
        if not it.is_active or it.domain != inputs.domain or _kind(it) != "label":
            continue
        f = face_of(it.name)
        if f:
            pool.setdefault(label_base(it.name), {}).setdefault(it.language or None, set()).add(f)
    return pool


def _new_acc(key, *, item: Optional[ItemRec], kind: str, name=None, unit=None,
             category=None, pkg_type=None, is_new=False, new_note=None) -> dict:
    return {
        "key": key, "item_id": item.id if item else None,
        "name": clean_text(name if name is not None else (item.name if item else "")) or "—",
        "kind": kind, "category": category if category is not None else (item.category if item else "Ambalaj"),
        "pkg_type": pkg_type if pkg_type is not None else (item.pkg_type if item else None),
        "unit": unit if unit is not None else ((item.unit or "") if item else "adet"),
        "need_production": 0.0, "per_product": OrderedDict(),
        "products": set(), "est_products": set(), "extra_for": set(),
        "unit_mismatch": [], "suspect_unit": [], "is_new_item": is_new, "new_note": new_note,
    }


def compute(inputs: PlanInputs, req: PlanRequest) -> dict:
    """Saf hesap — sözleşme spec 'Output dict'.  Bkz. modül docstring'i."""
    opts = req.options
    items = inputs.items
    recipes = inputs.recipes
    sibs = inputs.label_siblings
    waste = {"raw": opts.extra_waste_pct.raw, "packaging": opts.extra_waste_pct.packaging,
             "label": opts.extra_waste_pct.label}
    mode = opts.stock_mode
    root_of, groups = merge_map(inputs, opts)

    def root(i):
        return root_of.get(i, i)

    mats: "OrderedDict[str, dict]" = OrderedDict()
    used_cards: Set[int] = set()
    products: List[dict] = []
    labels_new: List[dict] = []
    skipped_labels: List[dict] = []
    line_roots: Dict[int, "OrderedDict[str, float]"] = {}
    label_pool: Optional[Dict[str, Dict[Optional[str], Set[str]]]] = None

    for no, line in enumerate(req.lines, 1):
        # ── 1. Satırı çöz ───────────────────────────────────────────────────
        if line.recipe_id is not None:
            rec = recipes.get(line.recipe_id)
        else:
            rid = inputs.recipe_by_target.get(line.target_item_id)
            rec = recipes.get(rid) if rid is not None else None
        target_id = line.target_item_id if line.target_item_id is not None else (
            rec.target_item_id if rec else None)
        tgt = items.get(target_id) if target_id is not None else None
        tname = tgt.name if tgt else None
        rname = rec.name if rec else None
        name = clean_text(line.display_name) or clean_text(tname) or clean_text(rname) or f"Satır {no}"
        brand = product_brand(tname, rname)
        size_ml = parse_ml(line.display_name, tname, rname)
        finished = float(tgt.current_stock or 0.0) if tgt else 0.0
        # ── 2. Üretilecek adet ──────────────────────────────────────────────
        produce = max(0.0, line.qty - max(0.0, finished)) if opts.subtract_finished_stock else float(line.qty)
        estimated = bool(rec) and (line.scale != 1 or (target_id is not None and rec.target_item_id != target_id))
        pcaut: List[dict] = []
        prow = {"no": no, "name": name, "brand": brand,
                "recipe_id": rec.id if rec else line.recipe_id,
                "recipe_name": rname, "target_item_id": target_id,
                "qty": float(line.qty), "produce_qty": produce, "finished_stock": finished,
                "scale": float(line.scale), "estimated": estimated, "size_ml": size_ml,
                "has_packaging": False, "note": clean_text(line.note),
                "capacity": {"producible": None, "limiting": []}, "cautions": pcaut}
        roots_here: "OrderedDict[str, float]" = OrderedDict()
        line_roots[no] = roots_here
        products.append(prow)

        face_names: List[Tuple[str, Optional[str]]] = []
        if rec is None:
            pcaut.append(_caution("recipeless_product",
                                  "Sistemde reçetesi yok; malzeme ihtiyacı hesaplanamadı (listeye katılmadı)."))
        else:
            # ── 3. Reçeteyi aç ──────────────────────────────────────────────
            exp = expand_recipe(rec, produce, items, sibs, label_mode=opts.label_mode,
                                scale=line.scale, include_packaging=line.use_recipe_packaging)
            exp_lines = list(exp.lines)
            lab_exp = exp
            if not line.use_recipe_packaging and opts.label_mode != "exclude":
                # "Reçete ambalajını kullan" kapalı = kavanoz/kapak ek ambalajla
                # DEĞİŞTİRİLDİ; etiket yine ürünündür.  expand_recipe
                # include_packaging=False'ta etiketi de düşürdüğü için etiketler
                # (TR/EN satırları, 'new' modunda etiket düzeni) reçeteden ayrıca
                # açılır — aksi halde etiket ihtiyacı sessizce kaybolurdu.
                lab_exp = expand_recipe(rec, produce, items, sibs, label_mode=opts.label_mode,
                                        scale=line.scale)
                exp_lines += [l for l in lab_exp.lines if l.kind == "label"]
            if opts.label_mode == "new":
                face_names = [(g["name"], (items[g["item_id"]].language if g["item_id"] in items else None))
                              for g in lab_exp.label_groups]
            for sk in lab_exp.skipped_labels:
                skipped_labels.append({"no": no, "product": name, "label": sk["label"],
                                       "language": sk["language"]})
            if lab_exp.skipped_labels:
                pcaut.append(_caution(
                    "labels_skipped",
                    f"Seçilen dilde ({opts.label_mode}) etiketi olmayan: "
                    + ", ".join(clean_text(s["label"]) or "—" for s in lab_exp.skipped_labels)
                    + " — etiket listeye eklenmedi."))
            if exp.missing_item_ids:
                pcaut.append(_caution(
                    "missing_ingredient",
                    f"Reçetedeki {len(exp.missing_item_ids)} kalemin kartı bulunamadı; hesaba katılmadı."))
            raw_net = 0.0
            ing_units: Dict[int, Set[str]] = {}
            for ing in rec.ingredients:
                if ing.unit:
                    ing_units.setdefault(ing.item_id, set()).add(ing.unit)
            for ln in exp_lines:
                it = items[ln.item_id]
                r = root(ln.item_id)
                key = f"i:{r}"
                m = mats.get(key)
                if m is None:
                    ritem = items.get(r) or it
                    m = mats[key] = _new_acc(key, item=ritem, kind=ln.kind)
                m["need_production"] += ln.gross
                pp = m["per_product"].setdefault(no, {"no": no, "product": name, "need": 0.0, "extra": False})
                pp["need"] += ln.gross
                m["products"].add(no)
                used_cards.add(ln.item_id)
                if estimated and ln.kind == "raw":
                    m["est_products"].add(no)
                for ru in ing_units.get(ln.source_item_id or ln.item_id, ()):
                    src = items.get(ln.source_item_id or ln.item_id) or it
                    if unit_norm(ru) != unit_norm(src.unit):
                        m["unit_mismatch"].append((rname, ru, src.unit or "—"))
                roots_here[key] = roots_here.get(key, 0.0) + ln.per_unit
                if ln.rows > 1:
                    pcaut.append(_caution(
                        "duplicate_ingredient_row",
                        f"Reçetede “{clean_text(it.name)}” {ln.rows} ayrı satır olarak yazılmış "
                        f"(toplandı); laboratuvar teyit etsin."))
                if ln.kind == "raw":
                    u = unit_norm(it.unit)
                    small = (ln.net_per_unit * 1000 if u in ("kg", "l")
                             else ln.net_per_unit if u in ("g", "ml") else None)
                    if small is not None:
                        raw_net += small
                        if size_ml and small > SUSPECT_UNIT_FACTOR * size_ml:
                            m["suspect_unit"].append((name, it.unit or "—", u, round(small, 6), size_ml))
            if size_ml and raw_net > 0:
                if abs(raw_net - size_ml) > max(1.0, RECIPE_SIZE_TOLERANCE * size_ml):
                    pcaut.append(_caution(
                        "recipe_total_vs_size",
                        f"Reçetedeki hammadde toplamı 1 adet için {tr_num(raw_net, 1)} g/ml"
                        + (f" (×{tr_num(line.scale, 2)} ölçekle)" if line.scale != 1 else "")
                        + f"; ürün boyu {tr_num(size_ml)} ml. Reçete laboratuvarda teyit edilmeli."))
            recipe_has_pkg = any(
                ing.item_id in items and items[ing.item_id].category == "Ambalaj"
                and _kind(items[ing.item_id]) != "label" for ing in rec.ingredients)
            prow["has_packaging"] = (recipe_has_pkg and line.use_recipe_packaging)

        # ── 4. Ek ambalaj ───────────────────────────────────────────────────
        for ex in line.extra_packaging:
            if ex.item_id is not None:
                if ex.item_id not in items:
                    continue
                r = root(ex.item_id)
                key = f"i:{r}"
                m = mats.get(key)
                if m is None:
                    ritem = items.get(r) or items[ex.item_id]
                    m = mats[key] = _new_acc(key, item=ritem, kind=_kind(ritem))
                used_cards.add(ex.item_id)
            else:
                nm = clean_text(ex.new_name) or "Yeni kalem"
                key = "new:" + (alnum_fold(nm).lower() or "yeni")
                m = mats.get(key)
                if m is None:
                    m = mats[key] = _new_acc(key, item=None, kind="packaging", name=nm, unit="adet",
                                             category="Ambalaj", pkg_type=clean_text(ex.pkg_type),
                                             is_new=True, new_note=clean_text(ex.note))
            amt = ex.per_unit * produce
            m["need_production"] += amt
            pp = m["per_product"].setdefault(no, {"no": no, "product": name, "need": 0.0, "extra": False})
            pp["need"] += amt
            pp["extra"] = True
            m["products"].add(no)
            m["extra_for"].add(no)
            roots_here[key] = roots_here.get(key, 0.0) + ex.per_unit
        if line.extra_packaging:
            prow["has_packaging"] = True
        if rec is not None and not prow["has_packaging"]:
            pcaut.append(_caution(
                "no_packaging_in_recipe",
                "Reçete ambalajı kullanılmıyor ve ek ambalaj girilmedi; bu ürünün ambalajı listeye dahil değil."
                if not line.use_recipe_packaging else
                "Reçetede ambalaj (kavanoz/şişe/kapak) tanımlı değil; bu ürünün ambalajı listeye dahil değil."))
        if estimated:
            pcaut.append(_caution(
                "estimated",
                "Tahmini hesap: " + ("başka ürünün reçetesi" if rec and target_id is not None
                                     and rec.target_item_id != target_id else "reçete")
                + (f" ×{tr_num(line.scale, 2)} ölçekle" if line.scale != 1 else "") + " kullanıldı.",
                "info"))

        # ── Yeni etiket satırı (label_mode=new) ─────────────────────────────
        if opts.label_mode == "new":
            if line.label_faces:
                faces, face_src = line.label_faces, "manual"
                face_text = "tek etiket" if faces == 1 else f"{faces} etiket (elle girildi)"
            else:
                if label_pool is None:
                    label_pool = _label_pool(inputs)
                faces, face_text, face_src = label_faces_auto(face_names, label_pool)
            total = math.ceil(produce * faces * (1 + waste["label"] / 100.0) - 1e-9)
            title = clean_text(opts.new_label_title) or "Yeni etiket"
            labels_new.append({"no": no, "product": name, "title": title,
                               "name": f"{title} — {name}", "units": produce, "faces": faces,
                               "face_text": face_text, "faces_source": face_src, "total": total})

    # ── 5–8. Kök başına ihtiyaç, fire, stok ─────────────────────────────────
    excluded_ids = set(opts.excluded_item_ids if opts.excluded_item_ids is not None
                       else inputs.default_excluded)
    held_reason = {h.item_id: (h.reason or "").strip() for h in opts.held_items}
    kept = _kept_sets(inputs)
    pending = [d for d in inputs.decisions if d.status == "pending"]
    lookalike_pool = _lookalike_pool(inputs) if opts.lookalike_check else []
    mg_members = material_group_members(inputs)

    def stock_of(i):
        it = items.get(i)
        return max(0.0, float(it.current_stock or 0.0)) if it else 0.0

    def in_plan(i):
        return f"i:{root(i)}" in mats

    # "Bitirilecek" tedarikçi kartlarının kalan stoğu — satırlar arası ORTAK
    # havuz (aynı gruptaki iki satır aynı stoğu iki kez saymasın).
    count_po = bool(opts.count_phase_out_stock) and mode == "net"
    po_pool: Dict[int, float] = {}

    rows: List[dict] = []
    for key, m in mats.items():
        kind = m["kind"]
        members = [] if m["is_new_item"] else groups.get(m["item_id"], [m["item_id"]])
        unit = m["unit"]
        stock = sum(stock_of(i) for i in members)
        used_stock = sum(stock_of(i) for i in members if i in used_cards)
        other = max(0.0, stock - used_stock)
        oo_list = [o for i in members for o in inputs.open_orders.get(i, [])]
        open_qty = sum(float(o.quantity) for o in oo_list if o.quantity) if opts.subtract_open_orders else 0.0
        need_prod = m["need_production"]
        need = need_prod * (1 + waste.get(kind, 0.0) / 100.0)
        is_held = any(i in held_reason for i in members)
        is_excluded = any(i in excluded_ids for i in members)
        # ── "Bitirilecek" grup kartlarının stoğu önce kullanılır ────────────
        # (hariç / bekletilen satır havuzdan pay almaz — listeye girmiyor)
        po_ids: Set[int] = set()
        po_from: List[dict] = []
        po_taken = 0.0
        if count_po and members and not is_held and not is_excluded:
            for a in _phase_out_alts(members, unit, inputs, mg_members, in_plan):
                po_ids.add(a["item_id"])
                deficit = need - (stock + po_taken) - open_qty
                avail = po_pool.setdefault(a["item_id"], stock_of(a["item_id"]))
                # Güvenli yuvarlamada gereken yukarı, elimizde aşağı yuvarlanır —
                # tam eksik kadar almak "yeterli" satırda "Alınacak 1 gram"
                # bırakırdı; havuz yetiyorsa gösterimde de kapatan miktar alınır.
                want = (max(deficit, _cover_qty(need, stock + po_taken, open_qty, unit))
                        if opts.safe_rounding else deficit)
                take = min(avail, want) if deficit > 1e-9 else 0.0
                if take <= 1e-9:
                    continue
                po_pool[a["item_id"]] = avail - take
                po_taken += take
                po_from.append(dict(a, qty=_r6(take), qty_text=_amount_near(take, unit)))
        stock += po_taken
        available = stock + open_qty
        buy = max(0.0, need - available) if mode == "net" else need
        display = purchase_display(need, stock, open_qty, unit, mode=mode, safe=opts.safe_rounding)
        status = "to_buy" if buy > 1e-6 else "yeterli"

        caut: List[dict] = []
        if is_held:
            why = next((held_reason[i] for i in members if held_reason.get(i)), "")
            caut.append(_caution("held", "Bekletiliyor" + (f": {why}" if why else "")
                                 + " — listeye ve toplama girmedi."))
        if m["is_new_item"]:
            caut.append(_caution("new_item", m["new_note"] or "Sistemde böyle bir kart yok; yeni alınacak."))
        for i in members:
            if opts.item_notes.get(i):
                caut.append(_caution("item_note", clean_text(opts.item_notes[i]), "info"))
        if len(members) > 1:
            caut.append(_caution("duplicate_merged",
                                 f"Sistemde {len(members)} ayrı kartta kayıtlı; stoklar toplandı.", "info"))
            uset = {unit_norm(items[i].unit) for i in members if i in items}
            if len(uset) > 1:
                caut.append(_caution(
                    "unit_mismatch",
                    "Birleştirilen kartların birimi farklı ("
                    + " / ".join(sorted({items[i].unit or '—' for i in members if i in items}))
                    + "); miktarlar olduğu gibi toplandı, laboratuvar teyit etsin."))
            thr = OTHER_CARD_THRESHOLD.get(unit_norm(unit), OTHER_CARD_THRESHOLD["adet"])
            if other >= thr:
                part = ("tamamı" if other >= stock - 1e-9 else
                        "neredeyse tamamı" if other >= 0.95 * stock else "bir kısmı")
                txt = (f"Stoğun {part} ({_amount_rounded(other, unit, 'down')}) bu planda kullanılmayan "
                       "başka kartta görünüyor; depoda fiziken teyit edilmeli.")
                if mode == "net":
                    alt = safe_triplet(need, stock - other, unit, safe=opts.safe_rounding,
                                       open_qty=open_qty)
                    txt += f" Bulunmazsa alınacak {alt['buy_text']} olur."
                caut.append(_caution("other_card_stock", txt))
        negs = [(i, items[i].current_stock) for i in members if i in items and (items[i].current_stock or 0) < 0]
        if negs:
            caut.append(_caution(
                "negative_stock",
                "Stok kartında eksi stok var ("
                + ", ".join(f"{_amount_near(abs(s), items[i].unit)}" for i, s in negs)
                + "); 0 sayıldı. Stok sayımı kontrol edilmeli."))
        for rname, ru, iu in dict.fromkeys(m["unit_mismatch"]):
            caut.append(_caution(
                "unit_mismatch",
                f"“{clean_text(rname) or 'Reçete'}” reçetesinde birim “{ru}” yazılmış, kartın birimi “{iu}”; "
                "miktar kart birimiyle hesaplandı, laboratuvar teyit etsin."))
        # Reçete birimi kaydederken DAİMA kartın birimi yazıldığı için
        # (routers/recipes.py) yukarıdaki kontrol yalnız kart birimi sonradan
        # değişince yakalar.  Asıl görülen hata miktarın yanlış birimle
        # girilmesi (TUZ: 269,5 'kg') — bunu yalnız boyla kıyas yakalar.
        for pname, cunit, u, small, size in dict.fromkeys(m["suspect_unit"]):
            txt = (f"“{pname}” için reçetede 1 adet başına {_amount_near(small, 'g' if u in ('g', 'kg') else 'ml')} "
                   f"çıkıyor; ürün boyu {tr_num(size)} ml. Birim hatası olabilir")
            if u in ("kg", "l") and small / 1000 <= SUSPECT_UNIT_FACTOR * size:
                txt += (f" (kart birimi “{cunit}”; miktar {'gram' if u == 'kg' else 'ml'} olarak "
                        f"yazılmış gibi: 1 adet için {tr_num(small / 1000, 1)} {'g' if u == 'kg' else 'ml'})")
            txt += (". Miktar olduğu gibi hesaplandı — gereken/alınacak bu yüzden çok yüksek olabilir; "
                    "alımdan önce laboratuvar reçeteyi teyit etmeli.")
            caut.append(_caution("suspect_unit", txt))
        if kind == "raw" and unit_norm(unit) == "adet":
            caut.append(_caution("raw_unit_adet",
                                 "Hammadde adetle sayılıyor; birim (g/ml) kartta düzeltilmeli, miktar teyit edilmeli."))
        if opts.subtract_open_orders and any(not o.quantity for o in oo_list):
            caut.append(_caution("open_order_no_qty",
                                 "Açık sipariş işareti var ama miktarı girilmemiş; yoldaki miktar hesaba katılmadı."))
        sample_qty = sum(l.quantity for i in members for l in inputs.lots.get(i, [])
                         if l.is_sample and l.quantity > 0)
        if sample_qty > 0:
            caut.append(_caution("samples_available",
                                 f"Numune lotu var ({_amount_near(sample_qty, unit)}); numune stoğa dahil değil.",
                                 "info"))
        for d in pending:
            if any(i in d.item_ids for i in members):
                caut.append(_caution(
                    "pending_duplicate_decision",
                    "Kopya kart kararı bekliyor" + (f" (“{d.title}”)" if d.title else "")
                    + "; laboratuvar karar verince stok/kart değişebilir."))
                break
        if lookalike_pool and not m["is_new_item"] and kind != "label":
            alts = _lookalikes(members, items, lookalike_pool, kept)
            if alts:
                caut.append(_caution(
                    "lookalike",
                    "Sistemde adı benzeyen başka kart(lar) var: "
                    + ", ".join(f"“{clean_text(c.name)}” {_amount_near(c.current_stock, c.unit)}" for c in alts)
                    + ". Aynı malzemeyse alım azalır ya da gerekmez; laboratuvar teyit etmeli."))
        mgroup = None if m["is_new_item"] else _material_group_row(
            members, unit, inputs, mg_members, lambda i: f"i:{root(i)}" in mats)
        if po_from:
            parts = [f"«{a['name']}» ({a['supplier'] or 'tedarikçi yazılmamış'}, "
                     f"{PHASE_OUT_REASON_TEXT[a['reason']]}) kartındaki {a['qty_text']}" for a in po_from]
            caut.append(_caution("phase_out_stock",
                                 "; ".join(parts) + " önce kullanılacak — ihtiyaçtan düşüldü."))
        if mgroup:
            stocked = [a for a in mgroup["alts"]
                       if a["stock"] > 0 and not a["in_plan"] and a["item_id"] not in po_ids]
            if stocked:
                parts = [(a["name"], _amount_near(a["stock"], a["unit"])
                          + ("; birimi farklı" if a["unit_mismatch"] else "")) for a in stocked]
                if len(parts) == 1:
                    txt = f"Aynı malzeme grubundaki «{parts[0][0]}» kartında stok var ({parts[0][1]})"
                else:
                    txt = ("Aynı malzeme grubundaki " + ", ".join(f"«{n}» ({a})" for n, a in parts)
                           + " kartlarında stok var")
                caut.append(_caution("group_alt_stock",
                                     txt + "; lab ayrı ürün saydığı için ihtiyaçtan düşülmedi.", "info"))
        if m["est_products"]:
            caut.append(_caution("estimated", "Tahmini." if m["est_products"] >= m["products"]
                                 else "Kısmen tahmini.", "info"))

        per_product = [{"no": p["no"], "product": p["product"], "need": _r6(p["need"]), "extra": p["extra"]}
                       for p in m["per_product"].values()]
        rows.append({
            "key": key, "item_id": m["item_id"], "member_ids": members, "name": m["name"],
            "kind": kind, "category": m["category"], "pkg_type": m["pkg_type"], "unit": unit,
            "need_production": _r6(need_prod), "need": _r6(need), "stock": _r6(stock),
            "stock_used_cards": _r6(used_stock), "stock_other_cards": _r6(other),
            # "bitirilecek" grup kartlarından düşülen (stock'un içinde) + kaynakları
            "stock_phase_out": _r6(po_taken), "phase_out_from": po_from,
            "open_orders": _r6(open_qty),
            "open_orders_info": [{"quantity": o.quantity, "unit": o.unit, "supplier": o.supplier_name,
                                  "expected_date": o.expected_date.isoformat() if o.expected_date else None}
                                 for o in oo_list],
            "available": _r6(available), "buy": _r6(buy), "display": display, "status": status,
            "per_product": per_product, "products": sorted(m["products"]),
            "cautions": caut, "is_new_item": m["is_new_item"], "estimated": bool(m["est_products"]),
            # "Aynı malzeme" grubu — `group` (fiyat listesi bayrağı, attach) ile KARIŞMASIN
            "material_group": mgroup,
            "_held": is_held, "_excluded": is_excluded,
        })

    # ── 9. Hariç / bekletilen ayrımı ────────────────────────────────────────
    materials, excluded, held = [], [], []
    water_ids = set(default_excluded_ids(items, inputs.domain))
    for r in rows:
        is_ex, is_held = r.pop("_excluded"), r.pop("_held")
        if is_ex:
            own = any(i in water_ids for i in r["member_ids"])
            excluded.append({"key": r["key"], "item_id": r["item_id"], "member_ids": r["member_ids"],
                             "name": r["name"], "kind": r["kind"], "unit": r["unit"], "need": r["need"],
                             "need_text": _excluded_amount(r["need"], r["unit"]),
                             "reason": "kendi üretimimiz" if own else "hariç tutuldu",
                             "products": r["products"]})
        elif is_held:
            r["reason"] = next((held_reason[i] for i in r["member_ids"] if held_reason.get(i)), "")
            held.append(r)
        else:
            materials.append(r)

    # ── 13. Kapasite ────────────────────────────────────────────────────────
    # Hariç kalemler kısıt sayılmaz: DİSTİLE SU kendi üretimimiz, stok kartı
    # 0 durur — sayılsaydı her kozmetik ürün "0 üretilebilir, sebep su" çıkardı.
    # Bekletilenler fiziksel stok olduğu için sayılır.
    excluded_keys = {e["key"] for e in excluded}
    stock_by_key = {r["key"]: (r["stock"], r["name"], r["unit"]) for r in rows
                    if r["key"] not in excluded_keys}
    for p in products:
        roots_here = line_roots.get(p["no"]) or {}
        caps = []
        for key, per in roots_here.items():
            if per <= 0 or key not in stock_by_key:
                continue
            st, nm, un = stock_by_key[key]
            caps.append((max(0.0, st) / per, nm, st, un))
        if not caps:
            continue
        lo = min(c[0] for c in caps)
        lim = sorted({(c[1], c[2], c[3]) for c in caps if c[0] <= lo + 1e-9}, key=lambda x: x[0])
        p["capacity"] = {"producible": max(0, int(math.floor(lo))),
                         "limiting": [{"name": n, "stock": _r6(s), "unit": u} for n, s, u in lim[:2]]}

    # ── Serbest satırlar ────────────────────────────────────────────────────
    manual = [{"no": i, "section": ml.section, "name": clean_text(ml.name) or "—", "qty": ml.qty,
               "qty_text": clean_text(ml.qty_text) or (
                   f"{tr_num(ml.qty, 0 if float(ml.qty).is_integer() else 2)} {ml.unit}" if ml.qty is not None else ""),
               "unit": ml.unit, "note": clean_text(ml.note)}
              for i, ml in enumerate(req.manual_lines, 1)]

    # ── Özet + meta ─────────────────────────────────────────────────────────
    units = sum(p["qty"] for p in products)
    produce_units = sum(p["produce_qty"] for p in products)
    brands = list(dict.fromkeys(p["brand"] for p in products if p["brand"]))
    by_kind = {k: {"count": 0, "to_buy": 0} for k in KINDS}
    for r in materials:
        by_kind[r["kind"]]["count"] += 1
        by_kind[r["kind"]]["to_buy"] += r["status"] == "to_buy"
    counts = {
        "lines": len(req.lines), "products": len(products),
        "recipeless": sum(1 for p in products
                          if any(c["code"] == "recipeless_product" for c in p["cautions"])),
        "estimated": sum(1 for p in products if p["estimated"]),
        "units": units, "produce_units": produce_units,
        "materials": len(materials),
        "to_buy": sum(1 for r in materials if r["status"] == "to_buy"),
        "sufficient": sum(1 for r in materials if r["status"] == "yeterli"),
        "excluded": len(excluded), "held": len(held),
        "labels_new": len(labels_new), "labels_new_total": sum(l["total"] for l in labels_new),
        "manual_lines": len(manual),
    }
    now = inputs.stock_as_of or datetime.utcnow()
    meta = {
        "title": clean_text(req.title) or "Satın Alma Planı",
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "stock_as_of": now.isoformat(timespec="seconds"),
        "stock_as_of_tr": _tr_stamp(now),
        "domain": inputs.domain,
        "options": opts.model_dump(mode="json"),
        "brands": brands,
        "counts": counts,
        "warnings": list(inputs.warnings),
    }
    result = {
        "meta": meta, "products": products, "materials": materials, "labels_new": labels_new,
        "manual_lines": manual, "excluded": excluded, "held": held, "skipped_labels": skipped_labels,
        "summary": {
            "by_kind": by_kind, "units": units, "produce_units": produce_units,
            "labels_new_total": counts["labels_new_total"],
            "warnings": sum(1 for r in materials for c in r["cautions"] if c["severity"] == "warn")
            + sum(1 for p in products for c in p["cautions"] if c["severity"] == "warn"),
            "fires": sorted({float(recipes[p["recipe_id"]].waste_percentage or 0.0)
                             for p in products if p["recipe_id"] in recipes}),
        },
    }
    meta["scope_text"] = scope_text(result, opts)
    return result


PHASE_OUT_REASON_TEXT = {"phase_out": "bitirilecek tedarikçi", "avoid": "bu malzemede alınmayacak tedarikçi"}


def _phase_out_alts(members: List[int], unit, inputs: PlanInputs,
                    mg_members: Dict[int, List[int]], in_plan) -> List[dict]:
    """Satırın grubundaki stoğu önce bitirilecek kartlar: satırda olmayan,
    planda kendi satırı olmayan (`in_plan`), aktif, aynı birim (`unit_norm`),
    stoklu, kartının tedarikçisi "bitirilecek" ya da satırın "alma" kümesinde
    olan grup kartları.  Sıra: bitirilecek önce, sonra ad, id."""
    gids = list(dict.fromkeys(inputs.mgroup_of[i] for i in members if i in inputs.mgroup_of))
    if not gids:
        return []
    mset = set(members)
    avoid: Set[int] = set()
    for i in members:
        avoid |= inputs.avoid.get(i, set())
    u = unit_norm(unit)
    out: List[dict] = []
    seen: Set[int] = set()
    for g in gids:
        for i in mg_members.get(g, []):
            a = inputs.items.get(i)
            if i in mset or i in seen or a is None or not a.is_active or in_plan(i):
                continue
            seen.add(i)
            if unit_norm(a.unit) != u or float(a.current_stock or 0.0) <= 1e-9:
                continue
            sid = inputs.card_supplier.get(i)
            if sid is None:
                continue
            if inputs.supplier_status.get(sid) == "phase_out":
                why = "phase_out"
            elif sid in avoid:
                why = "avoid"
            else:
                continue
            out.append({"item_id": i, "name": clean_text(a.name) or "—", "supplier_id": sid,
                        "supplier": clean_text(inputs.supplier_names.get(sid)), "reason": why})
    out.sort(key=lambda x: (x["reason"] != "phase_out", alnum_fold(x["name"]), x["item_id"]))
    return out


def _material_group_row(members: List[int], unit, inputs: PlanInputs,
                        mg_members: Dict[int, List[int]], in_plan) -> Optional[dict]:
    """Satırın "aynı malzeme" grubu + gruptaki diğer AKTİF kartlar (satırda
    olmayanlar).  Üyeler birden fazla gruba dağılmışsa (elle birleştirme)
    alternatifler hepsinden toplanır; `id`/`name` ilk grubun.  Grup yoksa None.
    `in_plan(item_id)` → kart bu planda kendi satırında mı (stoğu o satırda
    kullanılıyor; "stok var" uyarısına girmez)."""
    gids = list(dict.fromkeys(inputs.mgroup_of[i] for i in members if i in inputs.mgroup_of))
    if not gids:
        return None
    mset = set(members)
    alts: List[dict] = []
    seen: Set[int] = set()
    for g in gids:
        for i in mg_members.get(g, []):
            a = inputs.items.get(i)
            if i in mset or i in seen or a is None or not a.is_active:
                continue
            seen.add(i)
            alts.append({"item_id": i, "name": clean_text(a.name) or "—", "unit": a.unit or "",
                         "unit_mismatch": unit_norm(a.unit) != unit_norm(unit),
                         "stock": _r6(max(0.0, float(a.current_stock or 0.0))),
                         "in_plan": bool(in_plan(i))})
    alts.sort(key=lambda x: (alnum_fold(x["name"]), x["item_id"]))
    return {"id": gids[0], "name": inputs.mgroup_names.get(gids[0]) or f"Grup #{gids[0]}", "alts": alts}


def _tr_stamp(dt_utc: datetime) -> str:
    from database import to_tr
    return to_tr(dt_utc).strftime("%d.%m.%Y %H:%M")


def _excluded_amount(v, unit) -> str:
    """Notlardaki hariç kalem miktarı: 'Saf su 3.541 litre'."""
    u = unit_norm(unit)
    if u == "ml":
        return f"{tr_num(v / 1000)} litre" if v >= 1000 else f"{tr_num(v)} ml"
    if u == "g":
        return f"{tr_num(v / 1000)} kg" if v >= 1000 else f"{tr_num(v)} gram"
    if u in ("kg", "l"):
        return f"{tr_num(v, 1)} {'kg' if u == 'kg' else 'litre'}"
    return f"{tr_num(v)} adet"


# ─── Benzer adlı kartlar ────────────────────────────────────────────────────

def _lookalike_pool(inputs: PlanInputs) -> List[Tuple[str, ItemRec]]:
    return [(alnum_fold(it.name), it) for it in inputs.items.values()
            if it.is_active and it.domain == inputs.domain
            and it.category in ("Hammadde", "Ambalaj") and _kind(it) != "label"
            and (it.current_stock or 0) > 0]


def _lookalikes(members: List[int], items: Dict[int, ItemRec],
                pool: List[Tuple[str, ItemRec]], kept: List[frozenset]) -> List[ItemRec]:
    """difflib oranı ≥ 0.85 (katlanmış ad) ya da aynı katlanmış ad + farklı birim;
    üye olmayan, stoklu, aktif kartlar.  Addaki sayılar (boy, %) farklıysa
    benzer sayılmaz (100 ml ↔ 150 ml kavanoz farklı üründür).  Lab'ın 'kept'
    kararı çifti kapsıyorsa bastırılır."""
    mset = set(members)
    folds = [(alnum_fold(items[i].name), items[i]) for i in members if i in items]
    if not folds:
        return []
    found: Dict[int, float] = {}
    sm = difflib.SequenceMatcher(autojunk=False)
    for mf, mit in folds:
        if not mf:
            continue
        sm.set_seq2(mf)
        mdig = _digits(mf)
        for cf, c in pool:
            if c.id in mset or not cf:
                continue
            if cf == mf:
                if unit_norm(c.unit) == unit_norm(mit.unit):
                    continue           # aynı ad + birim → birleşmeliydi (kept kararıyla ayrı)
                ratio = 1.0
            else:
                if _digits(cf) != mdig:
                    continue
                sm.set_seq1(cf)
                if sm.real_quick_ratio() < LOOKALIKE_RATIO or sm.quick_ratio() < LOOKALIKE_RATIO:
                    continue
                ratio = sm.ratio()
                if ratio < LOOKALIKE_RATIO or _single_short_token_diff(mit.name, c.name):
                    continue
            if any(c.id in k and (k & mset) for k in kept):
                continue
            found[c.id] = max(found.get(c.id, 0.0), ratio)
    ordered = sorted(found.items(), key=lambda kv: (-kv[1], items[kv[0]].name))
    return [items[i] for i, _ in ordered[:LOOKALIKE_MAX]]


# ─── Kapsam metni ───────────────────────────────────────────────────────────

def scope_text(result: dict, opts: PlanOptionsIn) -> str:
    """Raporun başındaki Türkçe kapsam cümlesi — seçeneklerden kurulur."""
    c = result["meta"]["counts"]
    brands = result["meta"].get("brands") or []
    parts = []
    s = f"{c['products']} ürün"
    if brands:
        s += f" ({', '.join(brands)})"
    s += f", toplam {tr_num(c['units'])} adet"
    if opts.subtract_finished_stock and abs(c["produce_units"] - c["units"]) > 1e-9:
        s += f"; bitmiş ürün stoğu düşülünce üretilecek {tr_num(c['produce_units'])} adet"
    parts.append(s)
    if opts.stock_mode == "net":
        f = "“Alınacak” = toplam gereken − elimizde"
        if opts.subtract_open_orders:
            f += " − yoldaki (açık siparişler)"
        parts.append(f)
    else:
        parts.append("Alınacak stoktan bağımsız; yalnız seçilenler için gereken (elimizdeki stok bilgi amaçlı)")
    if opts.label_mode == "new":
        parts.append(f"Etiket: yeni basılacak ({clean_text(opts.new_label_title) or 'Yeni etiket'})")
    else:
        parts.append(f"Etiket: {LABEL_MODE_TEXT[opts.label_mode]}")
    fire = "Üretim firesi reçeteden; ambalaj/etiket firesiz"
    w = opts.extra_waste_pct
    extra = [f"{KIND_LABEL[k]} %{tr_num(v, 0 if float(v).is_integer() else 1)}"
             for k, v in (("raw", w.raw), ("packaging", w.packaging), ("label", w.label)) if v]
    if extra:
        fire += "; ek alım firesi: " + ", ".join(extra)
    parts.append(fire)
    parts.append(f"Stoklar {result['meta']['stock_as_of_tr']} sistem kaydı")
    return ". ".join(parts) + "."
