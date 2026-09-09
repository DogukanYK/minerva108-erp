# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Influencer programı hesap motoru — SAF fonksiyonlar (DB session/query yok).

Router satırları yükler, buradaki fonksiyonlara düz veri (dict/list/date)
verir, sonucu serialize eder.  core/pdks.py ile aynı ayrım: hesap mantığı DB
fixture'sız unit-test edilebilir (`tests/test_influencer_engine.py`).

Kapsam (tek kaynak — UI, scheduler ve raporlar bu modülün çıktısını gösterir):
  • Kademe (tier) seçimi — takipçi aralığına göre; `ambassador` ELLE atanır,
    `tier_for` onu asla otomatik döndürmez.
  • Gönderi istatistikleri — son N gönderiden ER'ler, izlenme/takipçi,
    yorum/beğeni oranı, izlenme değişkenliği (CV).
  • Özgünlük skoru (0–100) — 8 bileşen, ağırlık toplamı 100 (SCORE_WEIGHTS).
    Eksik veri o bileşene ORTALAMA (50) puan verir ve `eksik_veri:<ad>`
    bayrağı kaldırır — sessizce 0 ya da 100 yazmaz.  Kırmızı bayraklar
    (`hidden_subs`, `sahte_takipci_yuksek`, `kitle_hedef_disi`) skoru
    düşürmenin ötesinde ayrı listede döner; red kararı yöneticinindir.
  • İş birliği statü makinesi — STAGES sıralı, TRANSITIONS izinli geçişler;
    `iptal` her aktif aşamadan ulaşılabilir, terminal aşamalardan çıkış yok.
  • Atıf — UpPromote link kodu (`sca_ref`) sipariş `note_attributes`'ında
    kesin eşleşme; `UpPromote_order` etiketi "referral ama kimliği belirsiz".
  • Ekonomi — yüklü hediye maliyeti, sipariş başı katkı, kırılma noktası,
    KPI'lar (EMV YOK — bilinçli karar) ve 30 gün sonrası karar önerisi
    (birak / tekrarla / yukselt).  Komisyon ve clawback HESABI burada YOK:
    UpPromote hesaplar, IMS okur ve raporlar.
  • Uyum — pazar etiketi (TR "Reklam", US/UK/EU "Ad", DE "Werbung") ilk
    satırda mı, marka anıldı mı, yasak sağlık iddiası var mı → skor.

Veri sözleşmeleri:
  tier:       {"key", "label", "min_followers", "max_followers"|None,
               "commission_pct", "customer_discount_pct", "max_gift_items",
               "launch_access", "code_allowed", "sort_order"}
  benchmark:  {"platform", "tier_key", "metric", "low", "high"}  (seed listesi)
              motor içinde dict[(platform, tier_key, metric) -> (low, high)]
  gönderi:    {"likes", "comments", "views", "saves", "shares"}  (eksik → None)
  sipariş:    Shopify order dict — "note_attributes": [{"name","value"}],
              "tags": "a, b" | ["a","b"]
  affiliate:  {"sca_ref", "affiliate_email", "creator_id"}
  referral:   {"order_total", "refunded_total", "commission_amount",
               "is_new_customer": bool|None}
"""
import math
import re
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

# ─── İş birliği statü makinesi ───────────────────────────────────────────────

STAGES: List[str] = [
    "teklif_gonderildi", "kabul_edildi", "adres_bekleniyor", "urun_hazirlaniyor",
    "kargoda", "teslim_edildi", "icerik_bekleniyor", "taslak_incelemede",
    "revizyon", "onaylandi", "yayinlandi", "metrik_toplandi", "tamamlandi", "iptal",
]

STAGE_LABELS: Dict[str, str] = {
    "teklif_gonderildi": "Teklif Gönderildi",
    "kabul_edildi": "Kabul Edildi",
    "adres_bekleniyor": "Adres Bekleniyor",
    "urun_hazirlaniyor": "Ürün Hazırlanıyor",
    "kargoda": "Kargoda",
    "teslim_edildi": "Teslim Edildi",
    "icerik_bekleniyor": "İçerik Bekleniyor",
    "taslak_incelemede": "Taslak İncelemede",
    "revizyon": "Revizyon",
    "onaylandi": "Onaylandı",
    "yayinlandi": "Yayınlandı",
    "metrik_toplandi": "Metrik Toplandı",
    "tamamlandi": "Tamamlandı",
    "iptal": "İptal",
}

TERMINAL_STAGES = frozenset({"tamamlandi", "iptal"})
# "Ürün aldı paylaşmadı" bayrağı YALNIZ bu aşamadan kaldırılır.
NO_POST_STAGE = "icerik_bekleniyor"

TRANSITIONS: Dict[str, set] = {
    "teklif_gonderildi": {"kabul_edildi"},
    "kabul_edildi":      {"adres_bekleniyor", "urun_hazirlaniyor"},
    "adres_bekleniyor":  {"urun_hazirlaniyor"},
    "urun_hazirlaniyor": {"kargoda", "teslim_edildi"},      # elden teslim → doğrudan
    "kargoda":           {"teslim_edildi"},
    "teslim_edildi":     {"icerik_bekleniyor"},
    "icerik_bekleniyor": {"taslak_incelemede", "yayinlandi"},  # taslak onayı istenmeyen model
    "taslak_incelemede": {"revizyon", "onaylandi"},
    "revizyon":          {"taslak_incelemede"},
    "onaylandi":         {"yayinlandi"},
    "yayinlandi":        {"metrik_toplandi"},
    "metrik_toplandi":   {"tamamlandi"},
    "tamamlandi":        set(),
    "iptal":             set(),
}
# iptal her aktif aşamadan
for _st in STAGES:
    if _st not in TERMINAL_STAGES:
        TRANSITIONS[_st].add("iptal")


def can_transition(from_stage: str, to_stage: str) -> bool:
    """İzinli geçiş mi?  Bilinmeyen aşama → False."""
    return to_stage in TRANSITIONS.get(from_stage, set())


def can_flag_no_post(stage: str) -> bool:
    return stage == NO_POST_STAGE


# ─── Creator ilişki aşamaları ────────────────────────────────────────────────

RELATIONSHIP_STAGES: List[str] = [
    "havuz", "basvurdu", "seeded", "affiliate", "ambassador", "pasif", "kara_liste",
]
RELATIONSHIP_LABELS: Dict[str, str] = {
    "havuz": "Havuz",
    "basvurdu": "Başvurdu",
    "seeded": "Ürün Gönderildi",
    "affiliate": "Affiliate",
    "ambassador": "Ambassador",
    "pasif": "Pasif",
    "kara_liste": "Kara Liste",
}

# ─── Skor ağırlıkları (toplam 100) ───────────────────────────────────────────

SCORE_WEIGHTS: Dict[str, int] = {
    "er_fit": 20,
    "comment_quality": 15,
    "comment_like": 10,
    "view_follower": 15,
    "geo": 15,
    "growth": 10,
    "saves_shares": 10,
    "fake_pct": 5,
}
assert sum(SCORE_WEIGHTS.values()) == 100

HIDDEN_SUBS_PENALTY = 20      # YouTube hiddenSubscriberCount → −20
FLAT_VIEWS_PENALTY = 30       # her videoda ±%5 sabit izlenme → −30 (bot imzası)
FLAT_VIEWS_CV_PCT = 5.0
FLAT_VIEWS_MIN_POSTS = 3
FAKE_PCT_OK = 10.0            # ≤%10 tam puan
FAKE_PCT_RED = 25.0           # >%25 kırmızı bayrak
GEO_DISQUALIFY_PCT = 50.0     # hedef ülke <%50 → kitle_hedef_disi

# ─── Kademeler (seed; F1 init_db bunları influencer_tier'a yazar) ────────────

DEFAULT_TIERS: List[dict] = [
    {"key": "nano", "label": "Nano", "min_followers": 200, "max_followers": 1999,
     "commission_pct": 10.0, "customer_discount_pct": 0.0, "max_gift_items": 1,
     "launch_access": False, "code_allowed": False, "sort_order": 1},
    {"key": "mikro", "label": "Mikro", "min_followers": 2000, "max_followers": 19999,
     "commission_pct": 10.0, "customer_discount_pct": 0.0, "max_gift_items": 1,
     "launch_access": False, "code_allowed": True, "sort_order": 2},
    {"key": "orta", "label": "Orta", "min_followers": 20000, "max_followers": 199999,
     "commission_pct": 10.0, "customer_discount_pct": 0.0, "max_gift_items": 2,
     "launch_access": True, "code_allowed": True, "sort_order": 3},
    {"key": "makro", "label": "Makro", "min_followers": 200000, "max_followers": None,
     "commission_pct": 10.0, "customer_discount_pct": 0.0, "max_gift_items": 2,
     "launch_access": True, "code_allowed": True, "sort_order": 4},
    {"key": "ambassador", "label": "Ambassador", "min_followers": 0, "max_followers": None,
     "commission_pct": 10.0, "customer_discount_pct": 0.0, "max_gift_items": 3,
     "launch_access": True, "code_allowed": True, "sort_order": 5},
]
# Takipçi sayısıyla otomatik atanMAYAN kademeler (performansla, patron kararı).
MANUAL_TIER_KEYS = frozenset({"ambassador"})


def tier_for(followers, tiers: Optional[Iterable[dict]] = None) -> Optional[str]:
    """Takipçi sayısına göre kademe anahtarı.  Alt sınırın altında → None.
    `ambassador` (MANUAL_TIER_KEYS) atlanır — elle atanır."""
    try:
        f = int(followers or 0)
    except (TypeError, ValueError):
        return None
    rows = sorted((t for t in (tiers or DEFAULT_TIERS) if t.get("key") not in MANUAL_TIER_KEYS),
                  key=lambda t: (t.get("sort_order") or 0, t.get("min_followers") or 0))
    for t in rows:
        lo = int(t.get("min_followers") or 0)
        hi = t.get("max_followers")
        if f >= lo and (hi is None or f <= int(hi)):
            return t["key"]
    return None


# ─── Benchmark seed ──────────────────────────────────────────────────────────

_ER = {
    "instagram": {"nano": (3.5, 9), "mikro": (1.5, 5), "orta": (1, 4), "makro": (0.8, 3)},
    "tiktok":    {"nano": (9, 15), "mikro": (5, 9), "orta": (3, 6), "makro": (2, 4)},
    "youtube":   {"nano": (3, 8), "mikro": (1.5, 4), "orta": (1, 3), "makro": (0.5, 2)},
}
_VPF = {"instagram": (7, 100), "tiktok": (20, 1000), "youtube": (10, 1000)}
_ALL_TIERS = ("nano", "mikro", "orta", "makro")


def _build_default_benchmarks() -> List[dict]:
    out = []
    for platform in ("instagram", "tiktok", "youtube"):
        for tier in _ALL_TIERS:
            lo, hi = _ER[platform][tier]
            out.append({"platform": platform, "tier_key": tier, "metric": "er_follower",
                        "low": float(lo), "high": float(hi)})
            v_lo, v_hi = _VPF[platform]
            out.append({"platform": platform, "tier_key": tier, "metric": "view_per_follower",
                        "low": float(v_lo), "high": float(v_hi)})
            out.append({"platform": platform, "tier_key": tier, "metric": "comment_like_ratio",
                        "low": 5.0, "high": 15.0})
            out.append({"platform": platform, "tier_key": tier, "metric": "geo_target_pct",
                        "low": 70.0, "high": 100.0})
            out.append({"platform": platform, "tier_key": tier, "metric": "comment_quality_pct",
                        "low": 0.0, "high": 15.0})
    return out


DEFAULT_BENCHMARKS: List[dict] = _build_default_benchmarks()
BENCHMARK_METRICS = ("er_follower", "er_view", "view_per_follower",
                     "comment_like_ratio", "geo_target_pct", "comment_quality_pct")


def benchmark_map(rows: Iterable[dict]) -> Dict[tuple, tuple]:
    """Seed/DB satırlarını motorun beklediği dict[(platform,tier,metric)->(low,high)]'a çevir."""
    return {(r["platform"], r["tier_key"], r["metric"]): (float(r["low"]), float(r["high"]))
            for r in rows}


# ─── Pazar etiketleri ────────────────────────────────────────────────────────

LABEL_TEMPLATES: Dict[str, str] = {
    "TR": "Reklam — @{brand} tarafından sağlandı",
    "US": "Ad | Paid partnership with @{brand}",
    "UK": "Ad — @{brand}",
    "DE": "Werbung — @{brand}",
    "EU": "Ad — @{brand}",
}
# Etiket VAR MI kontrolü için pazar başına regex (büyük/küçük harf duyarsız).
_LABEL_RE: Dict[str, str] = {
    "TR": r"(?:^|\W)(?:#?reklam|#?işbirliği|#?isbirligi)(?:\W|$)",
    "US": r"(?:^|\W)(?:#?ad|#?advertisement|#?sponsored|paid partnership)(?:\W|$)",
    "UK": r"(?:^|\W)(?:#?ad|#?advert|#?advertisement|paid partnership)(?:\W|$)",
    "DE": r"(?:^|\W)(?:#?werbung|#?anzeige)(?:\W|$)",
    "EU": r"(?:^|\W)(?:#?ad|#?advertisement|#?sponsored|paid partnership)(?:\W|$)",
}


def label_template(market: str, brand: str) -> str:
    """Pazar etiketi metni; bilinmeyen pazar → EU şablonu."""
    tpl = LABEL_TEMPLATES.get((market or "").upper(), LABEL_TEMPLATES["EU"])
    return tpl.format(brand=(brand or "").strip().lstrip("@"))


# ─── Ayar anahtarları (AppSetting) ───────────────────────────────────────────

CFG_REMINDER_DAYS = "influencer.reminder_days"
CFG_NO_POST_DAYS = "influencer.no_post_days"
CFG_MIN_PAYOUT_TRY = "influencer.min_payout_try"
CFG_PACK_COST_TRY = "influencer.pack_cost_try"
CFG_SHIP_COST_TRY = "influencer.ship_cost_try"
CFG_GROSS_MARGIN_PCT = "influencer.gross_margin_pct"
CFG_BRAND_HANDLES = "influencer.brand_handles"
CFG_FORBIDDEN_WORDS = "influencer.forbidden_words"
CFG_GUIDELINE_VERSION = "influencer.guideline_version"

CFG_DEFAULTS: Dict[str, str] = {
    CFG_REMINDER_DAYS: "7,14",
    CFG_NO_POST_DAYS: "45",
    CFG_MIN_PAYOUT_TRY: "1000",
    CFG_PACK_COST_TRY: "0",
    CFG_SHIP_COST_TRY: "0",
    CFG_GROSS_MARGIN_PCT: "60",
    CFG_BRAND_HANDLES: "@minerva108",
    CFG_FORBIDDEN_WORDS: (r"tedavi|iyileştir|geçir|akne|egzama|sedef|garantili|3 günde|"
                          r"bakanlık onaylı|kimyasal içermez|%100 doğal"),
    CFG_GUIDELINE_VERSION: "2026-08",
}
CFG_KEYS: List[str] = list(CFG_DEFAULTS.keys())


def parse_int_list(s, default: Optional[List[int]] = None) -> List[int]:
    """'7,14' → [7, 14]; bozuk/boş → default (ya da [])."""
    out = []
    for part in str(s or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(float(part)))
        except ValueError:
            continue
    return out or list(default or [])


def parse_handles(s) -> List[str]:
    """'@minerva108, @serenida' → ['minerva108', 'serenida'] (küçük harf, @'siz)."""
    return [h.strip().lstrip("@").lower()
            for h in str(s or "").replace(";", ",").split(",") if h.strip()]


# ─── Gönderi istatistikleri ──────────────────────────────────────────────────

def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _avg(vals: List[float]) -> Optional[float]:
    return (sum(vals) / len(vals)) if vals else None


def _pct(num: Optional[float], den: Optional[float]) -> Optional[float]:
    if num is None or not den:
        return None
    return round(num / den * 100.0, 2)


def post_stats(posts: List[dict], followers) -> dict:
    """Son N gönderiden ortalamalar + oranlar.  Hesaplanamayan alan None
    (takipçi 0, izlenme yok, gönderi yok) — çağıran eksik veri olarak ele alır.

    er_follower       = (beğeni+yorum+kaydet+paylaş) ÷ takipçi × 100
    er_view           = aynı etkileşim ÷ izlenme × 100
    view_per_follower = izlenme ÷ takipçi × 100
    comment_like_ratio= yorum ÷ beğeni × 100
    views_cv          = izlenme std ÷ ort × 100 (popülasyon std)
    """
    likes = [x for x in (_num(p.get("likes")) for p in posts or []) if x is not None]
    comments = [x for x in (_num(p.get("comments")) for p in posts or []) if x is not None]
    views = [x for x in (_num(p.get("views")) for p in posts or []) if x is not None]
    saves = [x for x in (_num(p.get("saves")) for p in posts or []) if x is not None]
    shares = [x for x in (_num(p.get("shares")) for p in posts or []) if x is not None]
    f = _num(followers) or 0.0

    avg_likes, avg_comments, avg_views = _avg(likes), _avg(comments), _avg(views)
    avg_saves, avg_shares = _avg(saves), _avg(shares)

    engagement = None
    if avg_likes is not None or avg_comments is not None:
        engagement = (avg_likes or 0.0) + (avg_comments or 0.0) + (avg_saves or 0.0) + (avg_shares or 0.0)

    views_cv = None
    if len(views) >= 2 and avg_views:
        var = sum((v - avg_views) ** 2 for v in views) / len(views)
        views_cv = round(math.sqrt(var) / avg_views * 100.0, 2)

    return {
        "post_count": len(posts or []),
        "avg_likes": avg_likes, "avg_comments": avg_comments, "avg_views": avg_views,
        "avg_saves": avg_saves, "avg_shares": avg_shares,
        "er_follower": _pct(engagement, f),
        "er_view": _pct(engagement, avg_views),
        "view_per_follower": _pct(avg_views, f),
        "comment_like_ratio": _pct(avg_comments, avg_likes),
        "views_cv": views_cv,
    }


# ─── Skor bileşenleri ────────────────────────────────────────────────────────

def component_range_fit(value, low, high) -> Optional[float]:
    """Aralık uyumu 0–100.  [low, high] → 100.  Altı: low/2'de 0, low'a
    doğru lineer 100.  Üstü: 3×high'ta 20, high'tan itibaren lineer 100→20
    (aşırı yüksek değer bot/click-farm şüphesi — sıfırlamaz, düşürür)."""
    v = _num(value)
    if v is None:
        return None
    low, high = float(low), float(high)
    if low <= v <= high:
        return 100.0
    if v < low:
        half = low / 2.0
        if v <= half:
            return 0.0
        return round((v - half) / (low - half) * 100.0, 2)
    # v > high
    top = 3.0 * high
    if v >= top:
        return 20.0
    return round(100.0 - (v - high) / (top - high) * 80.0, 2)


def growth_stability(series: List[Tuple]) -> Optional[float]:
    """Takipçi zaman serisinden istikrar 0–100.  <2 nokta → None.
    Günde >1.000 ya da 7 gün içinde >10K sıçrama → 0 (satın alınmış takipçi
    imzası).  Zirveden %20+ düşüş → 50 puan ceza.  Aksi hâlde 100."""
    pts = []
    for d, f in series or []:
        if isinstance(d, datetime):
            d = d.date()
        elif isinstance(d, str):
            try:
                d = date.fromisoformat(d[:10])
            except ValueError:
                continue
        fv = _num(f)
        if isinstance(d, date) and fv is not None:
            pts.append((d, fv))
    pts.sort()
    if len(pts) < 2:
        return None
    score = 100.0
    peak = pts[0][1]
    for (d1, f1), (d2, f2) in zip(pts, pts[1:]):
        days = max((d2 - d1).days, 1)
        delta = f2 - f1
        if delta / days > 1000 or (days <= 7 and delta > 10000):
            return 0.0
        peak = max(peak, f1, f2)
        if peak > 0 and f2 < peak * 0.8:
            score = min(score, 50.0)
    return score


def _fake_component(fake_pct: Optional[float]) -> Optional[float]:
    if fake_pct is None:
        return None
    if fake_pct <= FAKE_PCT_OK:
        return 100.0
    if fake_pct >= FAKE_PCT_RED:
        return 0.0
    return round(100.0 - (fake_pct - FAKE_PCT_OK) / (FAKE_PCT_RED - FAKE_PCT_OK) * 100.0, 2)


def _saves_shares_component(stats: dict) -> Optional[float]:
    """Kaydetme+paylaşım varlığı — beğeninin ≥%5'i ise tam puan, 0 ise 0."""
    saves, shares, likes = stats.get("avg_saves"), stats.get("avg_shares"), stats.get("avg_likes")
    if saves is None and shares is None:
        return None
    if not likes:
        return None
    ratio = ((saves or 0.0) + (shares or 0.0)) / likes * 100.0
    return round(min(ratio / 5.0, 1.0) * 100.0, 2)


def authenticity_score(stats: dict, benchmarks: Dict[tuple, tuple], platform: str, tier_key: str,
                       geo_target_pct=None, growth_series=None, comment_quality_pct=None,
                       fake_pct=None, hidden_subs: bool = False) -> dict:
    """Özgünlük skoru → {"score": int 0–100, "components": {ad: puan}, "flags": [...]}.

    Eksik bileşen = 50 puan + 'eksik_veri:<ad>' bayrağı.  Cezalar: hidden_subs
    −20, sabit izlenme (CV<%5, ≥3 gönderi) −30.  Kırmızı bayraklar
    (sahte_takipci_yuksek, kitle_hedef_disi) skoru düşürmenin yanında ayrıca
    döner — yönetici tek başına red gerekçesi olarak görür.
    """
    stats = stats or {}
    platform = (platform or "").lower()
    flags: List[str] = []
    comps: Dict[str, Optional[float]] = {}

    def bench(metric):
        return (benchmarks or {}).get((platform, tier_key, metric))

    def fit(metric, value):
        b = bench(metric)
        if b is None or value is None:
            return None
        return component_range_fit(value, b[0], b[1])

    comps["er_fit"] = fit("er_follower", stats.get("er_follower"))
    comps["comment_quality"] = fit("comment_quality_pct", _num(comment_quality_pct))
    comps["comment_like"] = fit("comment_like_ratio", stats.get("comment_like_ratio"))
    comps["view_follower"] = fit("view_per_follower", stats.get("view_per_follower"))
    geo = _num(geo_target_pct)
    comps["geo"] = fit("geo_target_pct", geo)
    comps["growth"] = growth_stability(growth_series) if growth_series else None
    comps["saves_shares"] = _saves_shares_component(stats)
    fp = _num(fake_pct)
    comps["fake_pct"] = _fake_component(fp)

    total = 0.0
    for name, w in SCORE_WEIGHTS.items():
        v = comps.get(name)
        if v is None:
            v = 50.0
            comps[name] = v
            flags.append(f"eksik_veri:{name}")
        total += v * w / 100.0

    if hidden_subs:
        total -= HIDDEN_SUBS_PENALTY
        flags.append("gizli_abone_sayisi")
    cv = stats.get("views_cv")
    if (cv is not None and cv < FLAT_VIEWS_CV_PCT
            and int(stats.get("post_count") or 0) >= FLAT_VIEWS_MIN_POSTS):
        total -= FLAT_VIEWS_PENALTY
        flags.append("izlenme_sabit")
    if fp is not None and fp > FAKE_PCT_RED:
        flags.append("sahte_takipci_yuksek")
    if geo is not None and geo < GEO_DISQUALIFY_PCT:
        flags.append("kitle_hedef_disi")

    score = int(round(max(0.0, min(100.0, total))))
    return {"score": score, "components": comps, "flags": flags}


# ─── Atıf (UpPromote) ────────────────────────────────────────────────────────

REFERRAL_TAG = "uppromote_order"
REFERRAL_ATTR = "sca_ref"


def _tags_of(order: dict) -> List[str]:
    tags = (order or {}).get("tags")
    if not tags:
        return []
    if isinstance(tags, str):
        return [t.strip().lower() for t in tags.split(",") if t.strip()]
    if isinstance(tags, (list, tuple)):
        return [str(t).strip().lower() for t in tags if str(t).strip()]
    return []


def parse_referral_marker(order: dict) -> dict:
    """Shopify sipariş dict'inden UpPromote atıf işaretini çıkar.

    ① note_attributes içinde name=='sca_ref' → {affiliate_key, 'note_attribute'} (kesin)
    ② yoksa tags'te 'UpPromote_order' → {None, 'tag'} (referral, kimlik belirsiz — CSV doldurur)
    ③ hiçbiri → {None, None}
    """
    for attr in (order or {}).get("note_attributes") or []:
        if not isinstance(attr, dict):
            continue
        if str(attr.get("name") or "").strip().lower() == REFERRAL_ATTR:
            val = str(attr.get("value") or "").strip()
            if val:
                return {"affiliate_key": val, "source": "note_attribute"}
    if REFERRAL_TAG in _tags_of(order):
        return {"affiliate_key": None, "source": "tag"}
    return {"affiliate_key": None, "source": None}


def normalize_email(s) -> str:
    return str(s or "").strip().lower()


def match_affiliate(key_or_email, affiliates: List[dict]) -> Optional[int]:
    """`sca_ref` (büyük/küçük harf duyarsız) ya da normalize e-posta ile creator_id."""
    key = str(key_or_email or "").strip()
    if not key:
        return None
    key_l = key.lower()
    for a in affiliates or []:
        ref = str(a.get("sca_ref") or "").strip().lower()
        if ref and ref == key_l:
            return a.get("creator_id")
    for a in affiliates or []:
        em = normalize_email(a.get("affiliate_email"))
        if em and em == key_l:
            return a.get("creator_id")
    return None


# ─── Ödeme dönemi ────────────────────────────────────────────────────────────

def payout_period(dt) -> str:
    """datetime/date → 'YYYY-MM'."""
    if isinstance(dt, str):
        return dt[:7]
    return f"{dt.year:04d}-{dt.month:02d}"


def payout_batches(rows: List[dict], min_thresholds: Dict[str, float]) -> List[dict]:
    """creator × para birimi bazında topla; eşik altı `payable=False` (sonraki
    döneme devreder).  Eşiği tanımsız para birimi → eşik 0."""
    sums: Dict[tuple, float] = {}
    for r in rows or []:
        cur = str(r.get("currency") or "TRY").upper()
        k = (r.get("creator_id"), cur)
        sums[k] = sums.get(k, 0.0) + (_num(r.get("amount")) or 0.0)
    out = []
    for (cid, cur), total in sorted(sums.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        thr = _num((min_thresholds or {}).get(cur)) or 0.0
        total = round(total, 2)
        out.append({"creator_id": cid, "currency": cur, "total": total,
                    "payable": total > 0 and total >= thr})
    return out


# ─── Ekonomi ─────────────────────────────────────────────────────────────────

def loaded_gift_cost(cogs, pack, ship) -> float:
    """Yüklü hediye maliyeti = COGS + paket + kargo."""
    return round((_num(cogs) or 0.0) + (_num(pack) or 0.0) + (_num(ship) or 0.0), 2)


def contribution_per_order(aov, discount_pct, cogs_per_order, ship_per_order, commission_pct) -> float:
    """Sipariş başı katkı = AOV×(1−indirim) − COGS − kargo − komisyon(net üzerinden)."""
    net = (_num(aov) or 0.0) * (1.0 - (_num(discount_pct) or 0.0) / 100.0)
    commission = net * (_num(commission_pct) or 0.0) / 100.0
    return round(net - (_num(cogs_per_order) or 0.0) - (_num(ship_per_order) or 0.0) - commission, 2)


def breakeven_orders(loaded_cost, contribution) -> float:
    """Kırılma = yüklü maliyet ÷ katkı; katkı ≤ 0 → inf (asla kırılmaz)."""
    c = _num(contribution) or 0.0
    if c <= 0:
        return math.inf
    return round((_num(loaded_cost) or 0.0) / c, 2)


def kpis(inputs: dict) -> dict:
    """Creator/iş birliği KPI'ları.  EMV YOK (bilinçli).

    inputs: referrals (list, bkz. modül docstring), loaded_cost, store_aov,
            gross_margin_pct, cogs_per_order, ship_per_order, commission_pct,
            discount_pct (0), views, received_count, posted_count.
    Çıktı: orders, net_revenue, aov, aov_vs_store (% fark|None),
           new_customer_share (%|None — customer verisi yoksa bilinmiyor),
           cac|None, roi|None, breakeven, post_rate|None, orders_per_1k_views|None,
           commission_total.
    """
    inp = inputs or {}
    refs = inp.get("referrals") or []
    orders = len(refs)
    net = sum((_num(r.get("order_total")) or 0.0) - (_num(r.get("refunded_total")) or 0.0) for r in refs)
    net = round(net, 2)
    commission_total = round(sum(_num(r.get("commission_amount")) or 0.0 for r in refs), 2)
    aov = round(net / orders, 2) if orders else None
    store_aov = _num(inp.get("store_aov"))
    aov_vs_store = round((aov / store_aov - 1.0) * 100.0, 1) if (aov is not None and store_aov) else None

    known = [r for r in refs if r.get("is_new_customer") is not None]
    new_n = sum(1 for r in known if r.get("is_new_customer"))
    new_customer_share = round(new_n / len(known) * 100.0, 1) if known else None

    loaded = _num(inp.get("loaded_cost")) or 0.0
    spend = loaded + commission_total
    cac = round(spend / new_n, 2) if new_n else None
    margin = _num(inp.get("gross_margin_pct"))
    if margin is None:
        margin = float(CFG_DEFAULTS[CFG_GROSS_MARGIN_PCT])
    roi = round((net * margin / 100.0 - spend) / spend, 3) if spend > 0 else None

    contrib_aov = aov if aov is not None else (store_aov or 0.0)
    contribution = contribution_per_order(contrib_aov, inp.get("discount_pct") or 0,
                                          inp.get("cogs_per_order") or 0, inp.get("ship_per_order") or 0,
                                          inp.get("commission_pct") if inp.get("commission_pct") is not None else 10)
    breakeven = breakeven_orders(loaded, contribution)

    received = _num(inp.get("received_count"))
    posted = _num(inp.get("posted_count")) or 0.0
    post_rate = round(posted / received * 100.0, 1) if received else None
    views = _num(inp.get("views"))
    orders_per_1k_views = round(orders / (views / 1000.0), 3) if views else None

    return {
        "orders": orders, "net_revenue": net, "commission_total": commission_total,
        "aov": aov, "aov_vs_store": aov_vs_store,
        "new_customer_share": new_customer_share, "cac": cac, "roi": roi,
        "contribution": contribution, "breakeven": breakeven,
        "post_rate": post_rate, "orders_per_1k_views": orders_per_1k_views,
    }


DECISION_LABELS = {"birak": "BIRAK", "tekrarla": "TEKRARLA", "yukselt": "YÜKSELT"}


def decision_rule(k: dict, cfg: Optional[dict] = None) -> Tuple[str, List[str]]:
    """30 gün sonrası öneri (patron onaylar).

    YÜKSELT  = sipariş ≥ 3×kırılma ∧ yeni müşteri ≥ %60 ∧ R90 ≥ mağaza ort.
    TEKRARLA = sipariş ≥ kırılma ∨ ≥1 sipariş/1.000 izlenme ∨ anket atfı ≥ 2
               (ayrıca: kırılma altı ama uplift ya da yeniden kullanılabilir içerik)
    BIRAK    = aksi hâlde (0 sipariş ∧ uplift yok ∧ içerik yeniden kullanılamaz).
    k: kpis() çıktısı + survey_attribution, r90, store_r90, uplift, content_reusable.
    """
    k = k or {}
    cfg = cfg or {}
    reasons: List[str] = []
    orders = int(k.get("orders") or 0)
    be = k.get("breakeven")
    be = math.inf if be is None else float(be)
    new_share = _num(k.get("new_customer_share"))
    r90, store_r90 = _num(k.get("r90")), _num(k.get("store_r90"))
    opv = _num(k.get("orders_per_1k_views"))
    survey = _num(k.get("survey_attribution")) or 0.0
    mult = _num(cfg.get("upgrade_multiplier")) or 3.0
    new_min = _num(cfg.get("new_customer_min_pct")) or 60.0
    opv_min = _num(cfg.get("orders_per_1k_views_min")) or 1.0
    survey_min = _num(cfg.get("survey_min")) or 2.0

    if orders >= 1 and math.isfinite(be) and orders >= mult * be:
        if new_share is not None and new_share >= new_min:
            if r90 is not None and store_r90 is not None and r90 >= store_r90:
                reasons += [f"sipariş {orders} ≥ {mult:g}×kırılma ({be:g})",
                            f"yeni müşteri %{new_share:g} ≥ %{new_min:g}",
                            f"R90 %{r90:g} ≥ mağaza %{store_r90:g}"]
                return "yukselt", reasons
            reasons.append("R90 verisi yok ya da mağaza ortalamasının altında")
        else:
            reasons.append("yeni müşteri payı eşiğin altında ya da bilinmiyor")

    if orders >= 1 and orders >= be:
        reasons.append(f"sipariş {orders} ≥ kırılma ({be:g})")
        return "tekrarla", reasons
    if opv is not None and opv >= opv_min:
        reasons.append(f"{opv:g} sipariş / 1.000 izlenme ≥ {opv_min:g}")
        return "tekrarla", reasons
    if survey >= survey_min:
        reasons.append(f"anket atfı {survey:g} ≥ {survey_min:g}")
        return "tekrarla", reasons
    if k.get("uplift"):
        reasons.append("marka arama/erişim artışı var")
        return "tekrarla", reasons
    if k.get("content_reusable"):
        reasons.append("içerik yeniden kullanılabilir")
        return "tekrarla", reasons

    reasons.append("kesin sipariş yok" if orders == 0 else f"sipariş {orders} < kırılma ({be:g})")
    reasons.append("uplift yok, içerik yeniden kullanılamaz")
    return "birak", reasons


UPGRADE_POST_WINDOW_DAYS = 14
UPGRADE_INACTIVE_DAYS = 90
AMBASSADOR_MIN_ORDERS_DEFAULT = 10


def upgrade_signal(creator: dict) -> Optional[str]:
    """Kademe/ilişki sinyali (öneri — patron onayı gerekir).

    creator: relationship_stage, delivered_at, posted_at (date|None), post_er,
             avg_er, orders_90d, posts_90d, days_since_delivered,
             ambassador_min_orders (ops.).
    'pasif'            → 90 günde 0 sipariş + 0 paylaşım (seeded/affiliate)
    'affiliate_teklif' → seeded, teslimden ≤14 günde paylaştı, post ER > kendi ort.
    'ambassador_aday'  → affiliate, 90 günde ≥ N net sipariş
    """
    c = creator or {}
    stage = c.get("relationship_stage")
    if stage in ("pasif", "kara_liste", "ambassador", "havuz", "basvurdu"):
        return None
    orders_90 = int(c.get("orders_90d") or 0)
    posts_90 = int(c.get("posts_90d") or 0)
    days_since = c.get("days_since_delivered")
    if days_since is None and c.get("delivered_at"):
        d = c["delivered_at"]
        d = d.date() if isinstance(d, datetime) else d
        days_since = (date.today() - d).days
    if days_since is not None and days_since >= UPGRADE_INACTIVE_DAYS and orders_90 == 0 and posts_90 == 0:
        return "pasif"
    if stage == "seeded":
        delivered, posted = c.get("delivered_at"), c.get("posted_at")
        if delivered and posted:
            delivered = delivered.date() if isinstance(delivered, datetime) else delivered
            posted = posted.date() if isinstance(posted, datetime) else posted
            within = 0 <= (posted - delivered).days <= UPGRADE_POST_WINDOW_DAYS
            post_er, avg_er = _num(c.get("post_er")), _num(c.get("avg_er"))
            if within and post_er is not None and avg_er is not None and post_er > avg_er:
                return "affiliate_teklif"
    if stage == "affiliate":
        need = int(c.get("ambassador_min_orders") or AMBASSADOR_MIN_ORDERS_DEFAULT)
        if orders_90 >= need:
            return "ambassador_aday"
    return None


# ─── Uyum (reklam etiketi) ───────────────────────────────────────────────────

def compliance_check(caption: str, market: str, brand_handles: List[str], forbidden_re: str) -> dict:
    """Yayınlanan içerik açıklamasının mevzuat uyumu.

    has_label        pazar etiketi metinde var mı
    label_first_line etiket ilk (boş olmayan) satırda mı — TR/UK/DE şartı
    brand_mentioned  marka hesabı anılmış mı (@'lı ya da @'sız)
    forbidden_hits   yasak sağlık iddiaları (regex, büyük/küçük harf duyarsız)
    score            100 − etiket yok 50 − ilk satırda değil 20 − marka yok 15
                     − yasak kelime başına 15 (min 0)
    """
    text = caption or ""
    mk = (market or "").upper()
    label_re = re.compile(_LABEL_RE.get(mk, _LABEL_RE["EU"]), re.IGNORECASE)
    has_label = bool(label_re.search(text))
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    label_first_line = bool(label_re.search(first))

    low = text.lower()
    handles = [h.strip().lstrip("@").lower() for h in (brand_handles or []) if h and h.strip()]
    brand_mentioned = any(h in low for h in handles)

    hits: List[str] = []
    if forbidden_re:
        try:
            for m in re.finditer(forbidden_re, text, re.IGNORECASE):
                h = m.group(0).strip()
                if h and h.lower() not in (x.lower() for x in hits):
                    hits.append(h)
        except re.error:
            pass

    score = 100
    if not has_label:
        score -= 50
    elif not label_first_line:
        score -= 20
    if not brand_mentioned:
        score -= 15
    score -= 15 * len(hits)
    return {"has_label": has_label, "label_first_line": label_first_line,
            "brand_mentioned": brand_mentioned, "forbidden_hits": hits,
            "score": max(0, score)}


# ─── Hatırlatma / kod ────────────────────────────────────────────────────────

def reminder_dates(delivered_on: date, days: List[int]) -> List[date]:
    """Teslim tarihi + gün listesi → sıralı hatırlatma tarihleri (tekrarsız)."""
    if isinstance(delivered_on, datetime):
        delivered_on = delivered_on.date()
    uniq = sorted({int(d) for d in (days or []) if int(d) >= 0})
    return [delivered_on + timedelta(days=d) for d in uniq]


def next_collab_code(seq: int, year: int) -> str:
    """İş birliği kodu: INF-2026-00012."""
    return f"INF-{int(year):04d}-{int(seq):05d}"
