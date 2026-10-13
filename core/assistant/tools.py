# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Araç kayıt defteri — ad, Türkçe açıklama, STRICT şema, izin, işleyici.

Kurallar (bkz. spec §1.4 / §2):
  • Tüm araçlar HER istekte HER kullanıcıya aynı sırayla (ada göre) bildirilir —
    önek deterministik kalsın; kim neyi kullanabilir, oturum bağlamı söyler.
    `search_help` nasıl-yapılır rehberini (docs/assistant) istek anında getirir;
    rehber metinleri önekte değil (önekte yalnız giriş + konu dizini).
  • Strict şema: `additionalProperties: False`, TÜM özellikler `required`,
    opsiyoneller `["string","null"]` / `["integer","null"]` tipli; `strict: True`.
  • İzin "herhangi biri" (any-of) — yoksa `is_error` `{"error":"yetkiniz yok",
    "permission":"…"}` döner ve DB'ye HİÇ dokunulmaz.
  • Her çağrı kendi `SessionLocal()`'ını açar (runner thread'de koşar) ve
    `finally` ile kapatır; işleyici hatası `{"error":"araç hatası"}` (iz stderr).
  • Sonuç kompakt JSON, `MAX_RESULT_CHARS` üstü listeler kısaltılarak
    `"truncated": true` ile sığdırılır.
  • `PROMPT_HASH` = sha256(dondurulmuş sistem metni + araç şemaları)[:12] —
    sohbete yazılır, değişince rotasyon.
"""
import copy
import hashlib
import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from database import SessionLocal, User
from core.permissions import _FINANCE_ROLES, _resolve_permissions, get_role_labels
from core.assistant import handlers as H
from core.assistant import handlers_ops as O
from core.assistant import handlers_write as W
from core.assistant import prompt
from core.assistant.handlers import ToolError

MAX_RESULT_CHARS = 6000
DENIED_MESSAGE = "yetkiniz yok"
_SHRINK_DEPTH = 3


# ─── Bağlam ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolContext:
    """Bir sorunun sabit bağlamı — router kurar, runner ve araçlar okur."""
    user_id: int
    username: str
    full_name: str
    role: str
    perms: Dict[str, Dict[str, bool]]       # _resolve_permissions(user) kopyası
    domain: str
    can_finance: bool                       # rol ∈ _FINANCE_ROLES VE finance.view
    role_label: str = ""

    def can(self, cat: str, act: str) -> bool:
        return bool((self.perms.get(cat) or {}).get(act, False))

    def can_any(self, *pairs: Tuple[str, str]) -> bool:
        """Çift verilmezse her iç kullanıcı geçer."""
        return (not pairs) or any(self.can(c, a) for c, a in pairs)

    @classmethod
    def from_user(cls, user: User, domain: str, db: Optional[Session] = None) -> "ToolContext":
        perms = copy.deepcopy(_resolve_permissions(user))
        role = user.role or ""
        can_finance = role in _FINANCE_ROLES and bool((perms.get("finance") or {}).get("view"))
        labels = get_role_labels(db)
        return cls(user_id=int(user.id), username=user.username or "", full_name=user.full_name or "",
                   role=role, perms=perms, domain=domain, can_finance=can_finance,
                   role_label=labels.get(role, role))


# ─── Araç tanımı ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Tool:
    name: str
    description: str                        # Türkçe, NE ZAMAN kullanılacağı açık
    input_schema: dict
    perms: Tuple[Tuple[str, str], ...]      # any-of; () = her iç kullanıcı
    handler: Callable[[ToolContext, Session, dict], dict]
    link_label: str
    summarize: Callable[[dict], str]

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": copy.deepcopy(self.input_schema), "strict": True}

    def perm_label(self) -> str:
        return " | ".join(f"{c}.{a}" for c, a in self.perms) or "iç kullanıcı"


@dataclass
class ToolResult:
    content: str                 # tool_result.content (JSON metni)
    is_error: bool
    summary: str                 # widget çipi
    links: List[dict]            # [{"label","href"}] — yalnız iç yollar
    ms: int
    payload: Optional[dict] = field(default=None, repr=False)   # ham dict (API'ye gitmez)


# ─── Şema yardımcıları (strict) ──────────────────────────────────────────────

def _s(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _s_opt(desc: str) -> dict:
    return {"type": ["string", "null"], "description": desc}


def _i(desc: str) -> dict:
    return {"type": "integer", "description": desc}


def _i_opt(desc: str) -> dict:
    return {"type": ["integer", "null"], "description": desc}


def _enum_opt(desc: str, values: Tuple[str, ...]) -> dict:
    # API, `type: ["string","null"]` + `enum` birleşimini REDDEDER (400 "Enum value
    # ... does not match declared type", 13.10.2026 canlıda) → boş bırakılabilir
    # seçim listesi anyOf ile yazılır.
    return {"anyOf": [{"type": "string", "enum": list(values)}, {"type": "null"}],
            "description": desc}


def _schema(props: Dict[str, dict]) -> dict:
    return {"type": "object", "properties": props, "required": list(props.keys()),
            "additionalProperties": False}


# ─── İzin kümeleri (any-of) ──────────────────────────────────────────────────

P_ITEMS_READ = (("items", "view"), ("inventory", "view"), ("reports", "view"))
P_INV_READ = (("inventory", "view"), ("reports", "view"), ("items", "view"))
P_LOT = (("inventory", "view"), ("production", "view"), ("retention", "view"))
P_RECIPE = (("recipes", "view"), ("production", "view"), ("qc", "view"))
P_PROD_REPORT = (("production", "view"), ("reports", "view"))
P_PROD = (("production", "view"),)
P_REPORTS = (("reports", "view"),)
P_INV = (("inventory", "view"),)
P_DRIVE = (("items", "view"), ("inventory", "view"), ("reports", "view"),
           ("production", "view"), ("qc", "view"), ("recipes", "view"))
P_HELP = (("assistant", "use"),)
# Operasyon araçları (handlers_ops) — sistemdeki uçla AYNI izin
P_STOCK_MOVES = (("inventory", "view"), ("reports", "view"))
P_B2B_ORDERS = (("b2b_orders", "view"),)
P_QUOTES = (("b2b", "view"),)
P_QC = (("qc", "view"),)
P_OUTSOURCING = (("outsourcing", "view"),)
P_RETENTION = (("retention", "view"),)
P_TRENDYOL = (("trendyol", "view"),)
P_SHOPIFY = (("admin", "view"),)
P_INFLUENCER = (("influencer", "view"),)
P_REVIEWS = (("reviews", "view"),)
# supplier_detail'in `prices` alanı: sistemdeki `GET /api/suppliers/{id}/prices`
# kuralıyla AYNI (items.view | reports.view; routers/suppliers.py) — yalnız
# inventory.view taşıyan hesap iletişimi görür, fiyat listesini görmez.
P_SUPPLIER_PRICES = H.SUPPLIER_PRICE_PERMS


# ─── Özetleyiciler (widget çipi) ─────────────────────────────────────────────

def _sum_count(key: str, noun: str):
    def f(p: dict) -> str:
        n = p.get("count")
        if n is None:
            n = len(p.get(key) or [])
        return f"{n} {noun}"
    return f


def _sum_text_check(p: dict) -> str:
    e, w = p.get("errors") or 0, p.get("warnings") or 0
    if not e and not w:
        return "Sorun yok"
    return " · ".join(x for x in (f"{e} hata" if e else "", f"{w} uyarı" if w else "") if x)


def _sum_one_or_count(detail_key: str, key: str, noun: str):
    """Tek kayıt ayrıntısı (`reference` ile) → "REF · durum"; liste → sayı."""
    count = _sum_count(key, noun)

    def f(p: dict) -> str:
        one = p.get(detail_key)
        if isinstance(one, dict) and one.get("reference"):
            return f"{one['reference']} · {one.get('status_label') or one.get('status') or ''}".rstrip(" ·")
        return count(p)
    return f


def _num(v) -> str:
    """Çip metni için sayı: 15.0 → '15', 2.5 → '2.5' (JSON'daki değer değişmez)."""
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def _sum_item(p: dict) -> str:
    return f"{p.get('name', '')}: {_num(p.get('current_stock', 0))} {p.get('unit', '')}".strip()


def _sum_lot(p: dict) -> str:
    n = len(p.get("products") or [])
    return f"{n} ürün" + (" (belirsiz)" if p.get("ambiguous") else "")


def _sum_production(p: dict) -> str:
    t = p.get("totals") or {}
    return f"{t.get('count', 0)} üretim · {_num(t.get('quantity', 0))} adet"


def _sum_gaps(p: dict) -> str:
    s = p.get("summary") or {}
    return f"{s.get('sifir', 0)} sıfır · {s.get('kritik', 0)} kritik · {s.get('numune', 0)} numune"


def _sum_drive(p: dict) -> str:
    return f"{len(p.get('files') or [])} dosya · {len(p.get('folders') or [])} klasör"


def _sum_deliveries(p: dict) -> str:
    sm = p.get("summary") or {}
    return f"{sm.get('kargo_bekleyen', 0)} kargo bekliyor · {p.get('count', 0)} kayıt"


def _sum_capacity(p: dict) -> str:
    if p.get("ambiguous"):
        return f"{p.get('count', 0)} aday ürün"
    return f"{_num(p.get('producible_now', 0))} adet üretilebilir · {p.get('short_count', 0)} eksik"


def _sum_month_stock(p: dict) -> str:
    return f"{p.get('month')}/{p.get('year')} · " + ("dondurulmuş" if p.get("source") == "snapshot" else "canlı")


def _sum_movements(p: dict) -> str:
    return f"{p.get('count', 0)} hareket · net {_num(p.get('net_change', 0))}"


def _sum_help(p: dict) -> str:
    topics = p.get("topics") or []
    return "Rehber: " + (topics[0].get("title") or topics[0].get("id") or "") if topics else "Rehber: konu yok"


# ─── Kayıt defteri ───────────────────────────────────────────────────────────

_TOOLS: List[Tool] = [
    Tool(
        "find_item",
        "Ürün/malzeme/ambalaj kartı ara (ad, Türkçe ad, SKU, barkod; Türkçe-İngilizce eşanlamlı "
        "tolerans). Ürün, hammadde ya da malzeme adı geçen HER soruda önce bunu çağır; stok, lot ve "
        "konum ayrıntısı için sonucun id'siyle item_detail çağır. En fazla 8 kart döner.",
        _schema({"q": _s("Aranan ad / SKU / barkod"),
                 "category": _enum_opt("Kategori süzgeci; bilinmiyorsa null", ("all",) + H.CATEGORY_CHOICES[1:])}),
        P_ITEMS_READ, H.find_item, "Ürünler", _sum_count("items", "kart"),
    ),
    Tool(
        "item_detail",
        "Tek kartın stok dökümü: güncel stok, lot bazında miktar/konum/durum/SKT/tedarikçi, "
        "numune ayrımı, konum toplamları, 'aynı malzeme' grubu kardeşleri ve açık sipariş bayrağı. "
        "'Kaç kg kaldı', 'hangi lot nerede', 'numune var mı' sorularında find_item'dan sonra çağır.",
        _schema({"item_id": _i("find_item sonucundaki kart id'si")}),
        P_INV_READ, H.item_detail, "Ürünler", _sum_item,
    ),
    Tool(
        "find_lot",
        "Lot numarasından ürün, envanter satırları (miktar/konum/durum/SKT), üretim kaydı ve şahit "
        "numune dolabı yerini bul (dolap = marka, raf·göz). Lot numarası geçen HER soruda çağır; "
        "'ambiguous' true ise lot birden çok üründe var, kullanıcıya hangisi olduğunu sor.",
        _schema({"lot_number": _s("Lot numarası, ör. SR005 ya da MNR012")}),
        P_LOT, H.find_lot, "İzlenebilirlik", _sum_lot,
    ),
    Tool(
        "find_recipe",
        "Reçete ara (reçete adı ya da hedef ürün adı). 'Bu reçetede ne var', 'formülü nedir', "
        "'kaç bileşen' sorularında önce bunu çağır, sonra recipe_detail. Birden çok sonuç varsa sor.",
        _schema({"q": _s("Reçete ya da ürün adı")}),
        P_RECIPE, H.find_recipe, "Reçeteler", _sum_count("recipes", "reçete"),
    ),
    Tool(
        "recipe_detail",
        "Reçete içeriği: bileşenler (faz, miktar, birim, kartın güncel stoğu), beklenen verim, fire "
        "yüzdesi ve yapılış notu. 'Malzeme yeter mi' için bileşen stoğuna bak.",
        _schema({"recipe_id": _i("find_recipe sonucundaki reçete id'si")}),
        P_RECIPE, H.recipe_detail, "Reçeteler", lambda p: p.get("name", "Reçete"),
    ),
    Tool(
        "production_summary",
        "Dönem üretim özeti: ürün / marka / ürün grubu bazında üretilen adet ve üretim sayısı. "
        "'Bu ay kaç şampuan ürettik', 'Eylül'de ne üretildi' gibi sorularda çağır. Tarih verilmezse "
        "içinde bulunulan ay; ürün grubu toplamı için group='category', ürün adıyla daraltmak için q.",
        _schema({"start": _s_opt("Başlangıç tarihi YYYY-AA-GG; null = ayın 1'i"),
                 "end": _s_opt("Bitiş tarihi YYYY-AA-GG; null = bugün"),
                 "group": _enum_opt("Kırılım; null = product", ("product", "brand", "category")),
                 "q": _s_opt("Ürün adı süzgeci; null = hepsi")}),
        P_PROD_REPORT, H.production_summary, "Raporlar", _sum_production,
    ),
    Tool(
        "recent_productions",
        "En son yapılan üretimler (iptal edilenler hariç): reçete, ürün, adet, lot, tarih, kişi. "
        "'Son üretimler', 'en son ne üretildi' sorularında çağır.",
        _schema({"limit": _i_opt("Kaç kayıt (1–10); null = 5")}),
        P_PROD, H.recent_productions, "Üretim", _sum_count("productions", "üretim"),
    ),
    Tool(
        "find_supplier",
        "Tedarikçi/firma ara: iletişim (telefon, e-posta, yetkili) ve satın alma durumu "
        "(normal / tercih edilen / bitirilecek — sebebiyle). Firma adı geçen soruda çağır; fiyat "
        "listesi ve tercihler için sonucun id'siyle supplier_detail.",
        _schema({"q": _s("Firma adı (kısmi olabilir)")}),
        P_ITEMS_READ, H.find_supplier, "Tedarikçiler", _sum_count("suppliers", "firma"),
    ),
    Tool(
        "supplier_detail",
        "Tek tedarikçinin kartı: iletişim, durum ve sebebi, kayıtlı alış fiyatları (malzeme, birim "
        "fiyat, KDV, tarih, kaynak — yalnız items.view ya da reports.view yetkisi varsa döner), "
        "malzeme bazlı tercih/alma kararları ve kart sayısı.",
        _schema({"supplier_id": _i("find_supplier sonucundaki firma id'si")}),
        P_ITEMS_READ, H.supplier_detail, "Tedarikçiler", lambda p: p.get("name", "Tedarikçi"),
    ),
    Tool(
        "stock_gaps",
        "Eksik hammaddeler: stoğu sıfır, kritik altı ya da yalnız numunesi olan kartlar + açık "
        "sipariş bayrağı (sipariş verildi mi, ne zaman bekleniyor). 'Hangi hammaddeler bitti', "
        "'neyi sipariş etmeliyiz', 'X sipariş edildi mi' sorularında çağır.",
        _schema({"status": _enum_opt("Durum süzgeci; null = hepsi", ("sifir", "kritik", "numune", "all")),
                 "q": _s_opt("Kart/tedarikçi adı süzgeci; null = hepsi")}),
        P_REPORTS, H.stock_gaps_tool, "Raporlar", _sum_gaps,
    ),
    Tool(
        "expiring_lots",
        "Son kullanma tarihi (SKT) yaklaşan onaylı lotlar — kalan gün, miktar, konum. "
        "'SKT'si yaklaşanlar', 'önümüzdeki ay dolacak lotlar' sorularında çağır.",
        _schema({"days": _i_opt("Kaç gün içinde (1–180); null = 60")}),
        P_INV, H.expiring_lots, "İzlenebilirlik", _sum_count("lots", "lot"),
    ),
    Tool(
        "drive_search",
        "Minerva Drive'da dosya/klasör ara (yalnız ad ve klasör yolu; içerik okunmaz). "
        "'Sertifika nerede', 'X belgesi hangi klasörde' sorularında çağır.",
        _schema({"q": _s("Dosya ya da klasör adı (en az 2 karakter)")}),
        P_DRIVE, H.drive_search, "Drive", _sum_drive,
    ),
    Tool(
        "deliveries",
        "Teslimat / kargo durumu: kargolanmayı bekleyen gönderiler (Teslimat sayfasındaki 'Bekleyen "
        "Kargo Sevkiyatları'), kargolananlar, son teslimatlar; yalnız SuperAdmin için onay bekleyen "
        "proformalar. 'Kargolanacak ürünler ne durumda', 'X kargoya verildi mi', 'takip no' "
        "sorularında çağır. Alıcı firma (yoksa ad) görünür; telefon/adres yoktur.",
        _schema({"status": _enum_opt("Süzgeç; null = son kayıtlar",
                                     ("kargo_bekleyen", "onay_bekleyen", "kargolanan", "son")),
                 "q": _s_opt("Belge no / alıcı / ürün adı süzgeci; null = hepsi"),
                 "limit": _i_opt("Kaç kayıt (1–15); null = 10")}),
        P_INV, O.deliveries, "Teslimat", _sum_deliveries,
    ),
    Tool(
        "product_returns",
        "Ürün iadeleri: iade belgesi, bağlı teslimat, sağlam/hasarlı satırlar, sebep. 'Geri gelen "
        "ürünler', 'X iadesi' sorularında çağır.",
        _schema({"q": _s_opt("Belge no / teslimat no / ürün adı; null = hepsi"),
                 "limit": _i_opt("Kaç kayıt (1–15); null = 10")}),
        P_INV, O.product_returns, "İadeler", _sum_count("rows", "iade"),
    ),
    Tool(
        "b2b_orders",
        "B2B müşteri siparişleri: durum, sıradaki adım ve kimde olduğu, satırlar, üretim partileri, "
        "alım satırları, sevk hazırlığı. Liste için status; tek sipariş için reference (teklif no ya "
        "da SIP-<no>). Tutar ve ödeme yalnız B2B fiyat yetkisi olana döner.",
        _schema({"status": _enum_opt("Liste süzgeci; null = açık siparişler",
                                     ("open", "all", "SUBMITTED", "TECH_REVIEWED", "APPROVED",
                                      "SHIPPED", "CANCELLED")),
                 "reference": _s_opt("Teklif numarası ya da SIP-<no>; null = liste")}),
        P_B2B_ORDERS, O.b2b_orders, "B2B Siparişleri", _sum_one_or_count("order", "rows", "sipariş"),
    ),
    Tool(
        "quotations",
        "B2B teklifleri: teklif no, müşteri firma, ülke, durum, tutar, kalem sayısı, siparişe "
        "dönüştüyse sipariş no. 'Bekleyen teklifler', 'X firmasına verilen teklif' sorularında çağır.",
        _schema({"status": _enum_opt("Durum; null = hepsi",
                                     ("DRAFT", "PENDING", "CONFIRMED", "REJECTED", "ORDER")),
                 "q": _s_opt("Teklif no / firma / ülke süzgeci; null = hepsi")}),
        P_QUOTES, O.quotations, "B2B Teklifler", _sum_count("rows", "teklif"),
    ),
    Tool(
        "qc_pending",
        "Kalite kontrol (QC) onayı bekleyen lotlar: ürün, lot, miktar, kaynak (üretim / mal kabul / "
        "fason), kaç gündür beklediği. 'QC bekleyenler', 'onay bekleyen lotlar' sorularında çağır.",
        _schema({}),
        P_QC, O.qc_pending, "Kalite Kontrol", _sum_count("rows", "lot"),
    ),
    Tool(
        "outsourcing_jobs",
        "Fason üretim işleri: FS kodu, üretici firma, ürün, adet, durum (onay/sevk/mutabakat/kapanış). "
        "Tek iş için reference (FS-<no>) ile malzeme kodları, onaylar, bakiye ve kabuller döner.",
        _schema({"reference": _s_opt("FS-<no>; null = liste"),
                 "status": _enum_opt("Liste süzgeci; null = hepsi",
                                     ("DRAFT", "TECH_APPROVED", "APPROVED", "DISPATCHED", "RECONCILING",
                                      "CLOSED", "CANCELLED"))}),
        P_OUTSOURCING, O.outsourcing_jobs, "Fason Üretim", _sum_one_or_count("job", "rows", "iş"),
    ),
    Tool(
        "stock_movements",
        "Bir kartın stok hareketleri (giriş, çıkış, düzeltme) tarih aralığında: tür bazında toplam, "
        "net değişim ve son 20 hareket. 'Bu ay X'ten ne kadar çıktı', 'stok neden azaldı' sorularında "
        "find_item'dan sonra çağır. Tarih verilmezse son 30 gün.",
        _schema({"item_id": _i("find_item sonucundaki kart id'si"),
                 "start": _s_opt("Başlangıç YYYY-AA-GG; null = 30 gün önce"),
                 "end": _s_opt("Bitiş YYYY-AA-GG; null = bugün")}),
        P_STOCK_MOVES, O.stock_movements, "İşlem Defteri", _sum_movements,
    ),
    Tool(
        "consumption",
        "Dönemde en çok tüketilen malzemeler (üretim + fason fiilî tüketim; iptal edilen üretim ve "
        "fason sevkleri hariç). 'Geçen ay en çok ne kullandık' sorularında çağır. Tarih verilmezse son "
        "30 gün.",
        _schema({"start": _s_opt("Başlangıç YYYY-AA-GG; null = 30 gün önce"),
                 "end": _s_opt("Bitiş YYYY-AA-GG; null = bugün"),
                 "limit": _i_opt("Kaç malzeme (1–30); null = 15")}),
        P_REPORTS, O.consumption, "Raporlar", _sum_count("rows", "malzeme"),
    ),
    Tool(
        "month_end_stock",
        "Bir ayın sonundaki stok (Raporlar'daki aylık stok raporu: kapanmış ay için dondurulmuş "
        "kayıt, yoksa canlı hesap). 'Eylül sonunda X stoğu neydi' sorularında çağır.",
        _schema({"year": _i_opt("Yıl; null = bu yıl"),
                 "month": _i_opt("Ay (1–12); null = bu ay"),
                 "category": _enum_opt("Kategori; null = all",
                                       ("all", "hammadde", "ambalaj", "etiket", "finished")),
                 "q": _s_opt("Kart adı süzgeci; null = hepsi")}),
        P_REPORTS, O.month_end_stock, "Raporlar", _sum_month_stock,
    ),
    Tool(
        "production_capacity",
        "'N adet üretirsek ne eksik', 'şu an kaç adet üretebiliriz' sorusu: üretilebilir adet, "
        "sınırlayan malzeme ve eksik malzemeler (eksik miktarıyla). Ürün adıyla çağır; birden çok "
        "ürün eşleşirse adaylar döner, kullanıcıya sor.",
        _schema({"product": _s("Ürün / reçete adı"),
                 "quantity": _i("Planlanan adet (1 ya da daha büyük)")}),
        P_REPORTS, O.production_capacity, "Raporlar", _sum_capacity,
    ),
    Tool(
        "retention_samples",
        "Şahit numune dolabı listesi: ürün, lot, marka (dolap), raf·göz, miktar, saklama bitişi. "
        "'Şahit numunesi nerede', 'imha zamanı gelenler' sorularında çağır (expired=true).",
        _schema({"q": _s_opt("Ürün adı / lot süzgeci; null = hepsi"),
                 "brand": _s_opt("Marka (dolap) süzgeci; null = hepsi"),
                 "status": _enum_opt("Durum; null = imha edilmemişler",
                                     ("stored", "depleted", "destroyed")),
                 "expired": {"type": ["boolean", "null"],
                             "description": "true = saklama süresi dolmuş, dolaptakiler"}}),
        P_RETENTION, O.retention_samples, "Şahit Numune", _sum_count("rows", "numune"),
    ),
    Tool(
        "sample_analyses",
        "Numune analiz formları (alternatif tedarikçi denemeleri): bulk adı, lot, reçete, sonuç "
        "(uygun / uygun değil / beklemede), kullanılan kaynaklar ve tedarikçiler.",
        _schema({"q": _s_opt("Belge no / bulk / malzeme / tedarikçi süzgeci; null = son kayıtlar")}),
        P_QC, O.sample_analyses, "Numune Analizi", _sum_count("rows", "form"),
    ),
    Tool(
        "trendyol_orders",
        "Trendyol siparişleri: durum sayıları, son siparişler (ürün, adet, tutar, kargo takip), "
        "eşleşmeyen barkod uyarıları. Komisyon yalnız kâr yetkisi olana döner.",
        _schema({"start": _s_opt("Başlangıç YYYY-AA-GG; null = hepsi"),
                 "end": _s_opt("Bitiş YYYY-AA-GG; null = bugün"),
                 "status": _s_opt("Trendyol durum kodu; null = hepsi"),
                 "q": _s_opt("Sipariş no / barkod / ürün adı; null = hepsi")}),
        P_TRENDYOL, O.trendyol_orders, "Trendyol", _sum_count("rows", "sipariş"),
    ),
    Tool(
        "shopify_orders",
        "Shopify mağaza siparişleri: mağaza, durum, adım, tutar, ülke, ürün satırları ve hata notu "
        "(müşteri bilgisi yok).",
        _schema({"status": _s_opt("Durum (received, done, failed …); null = hepsi"),
                 "limit": _i_opt("Kaç sipariş (1–15); null = 10")}),
        P_SHOPIFY, O.shopify_orders, "Shopify", _sum_count("rows", "sipariş"),
    ),
    Tool(
        "influencer_overview",
        "Creator programı özeti: kampanyalar, iş birliklerinin aşamalara göre sayısı, gönderi ve "
        "içerik sayıları (kişi bilgisi yok).",
        _schema({}),
        P_INFLUENCER, O.influencer_overview, "Creator Programı", _sum_count("campaigns", "kampanya"),
    ),
    Tool(
        "reviews_overview",
        "Ürün yorumları özeti: ürün bazında yayındaki yorum sayısı ve puan ortalaması, moderasyon "
        "bekleyenler, son yayınlanan yorumlardan kısa alıntı (yorumcu bilgisi yok).",
        _schema({"q": _s_opt("Ürün adı süzgeci; null = hepsi")}),
        P_REVIEWS, O.reviews_overview, "Yorumlar", _sum_count("products", "ürün"),
    ),
    Tool(
        "check_product_text",
        "Ürün metni taslağını (ürün açıklaması, Amazon başlık/madde/arama terimleri, Shopify metni, "
        "etiket metni) yayın kurallarıyla denetler: INCI listesinde karşılığı olmayan bileşen ya da koku "
        "iddiası, yasaklı tıbbi/abartılı iddia, başlıktaki hacmin ürünle tutarlılığı. Metin yazma "
        "modunda ürün metnini kullanıcıya sunmadan ÖNCE çağır; hataları düzelt, kalan uyarıları metnin "
        "altında kısaca belirt. E-posta / WhatsApp / çeviri için gerekmez.",
        _schema({"text": _s("Denetlenecek metnin tamamı"),
                 "title": _s_opt("Ürün başlığı; yoksa null"),
                 "inci": _s_opt("Ürünün INCI listesi (virgüllü); bilinmiyorsa null"),
                 "size": _s_opt("Hacim / ağırlık, ör. 50 ml; yoksa null"),
                 "lang": _enum_opt("Metnin dili; null = tr", ("tr", "en"))}),
        P_HELP, W.check_product_text, "Metin denetimi", _sum_text_check,
    ),
    Tool(
        "search_help",
        "Nasıl-yapılır rehberinde ara: bir işlemin adımları, hangi sayfada hangi düğmeye basılacağı, "
        "gereken yetki ve dikkat notları. 'Nasıl yapılır', 'hangi düğme', 'adımlar neler', 'hangi "
        "yetki gerekir' sorularında cevaplamadan ÖNCE çağır. q = rehber dizinindeki konu id'si (ör. "
        "lot_tasima) ya da Türkçe anahtar kelimeler. En uygun en fazla 2 konunun tam metnini ve diğer "
        "aday başlıkları (others) döner; topics boşsa rehberde bu konu yoktur.",
        _schema({"q": _s("Konu id'si (rehber dizininden) ya da Türkçe anahtar kelimeler")}),
        P_HELP, H.search_help, "Rehber", _sum_help,
    ),
]

REGISTRY: Dict[str, Tool] = {t.name: t for t in _TOOLS}
TOOL_NAMES: Tuple[str, ...] = tuple(sorted(REGISTRY))
assert len(TOOL_NAMES) == len(_TOOLS), "araç adı çakışması"

# Deterministik: bir kez serileştirilir, her çağrı aynı byte'lardan kopya alır.
_SPECS_JSON = json.dumps([REGISTRY[n].spec() for n in TOOL_NAMES],
                         ensure_ascii=False, separators=(",", ":"))

PROMPT_HASH = hashlib.sha256(
    json.dumps({"system": prompt.system_text(), "tools": json.loads(_SPECS_JSON)},
               sort_keys=True, ensure_ascii=False).encode("utf-8")
).hexdigest()[:12]


def tool_specs() -> List[dict]:
    """API `tools` listesi — ada göre sıralı, her çağrıda byte-aynı, taze kopya."""
    return json.loads(_SPECS_JSON)


def tool_specs_json() -> str:
    return _SPECS_JSON


def allowed_tools(ctx: ToolContext) -> List[str]:
    return [n for n in TOOL_NAMES if ctx.can_any(*REGISTRY[n].perms)]


def denied_tools(ctx: ToolContext) -> List[Tuple[str, str]]:
    """[(araç, eksik izin etiketi)] — oturum bağlamı notu için."""
    return [(n, REGISTRY[n].perm_label()) for n in TOOL_NAMES
            if not ctx.can_any(*REGISTRY[n].perms)]


# ─── Çıktı sığdırma ──────────────────────────────────────────────────────────

def compact_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _largest_list(obj, depth: int = 0):
    """payload içindeki (≤ _SHRINK_DEPTH derinlikte) EN UZUN listeyi döner."""
    best = None
    if isinstance(obj, list):
        if obj:
            best = obj
        children = obj
    elif isinstance(obj, dict):
        children = list(obj.values())
    else:
        return None
    if depth >= _SHRINK_DEPTH:
        return best
    for ch in children:
        cand = _largest_list(ch, depth + 1)
        if cand is not None and (best is None or len(cand) > len(best)):
            best = cand
    return best


def fit_payload(payload: dict, limit: Optional[int] = None) -> Tuple[str, bool]:
    """Kompakt JSON; `limit` üstündeyse en uzun listelerden sondan kırparak
    sığdırır ve `"truncated": true` ekler.  Liste kalmazsa son çare metin
    kesilir (`{"truncated":true,"partial":…}`)."""
    limit = MAX_RESULT_CHARS if limit is None else limit
    s = compact_json(payload)
    if len(s) <= limit:
        return s, False
    data = copy.deepcopy(payload) if isinstance(payload, dict) else {"result": copy.deepcopy(payload)}
    data["truncated"] = True
    for _ in range(2000):
        s = compact_json(data)
        if len(s) <= limit:
            return s, True
        lst = _largest_list(data)
        if not lst:
            break
        lst.pop()
    s = compact_json(data)
    if len(s) > limit:
        s = compact_json({"truncated": True, "partial": s[:max(0, limit - 40)]})
    return s, True


# ─── Bağlantı kuralı ─────────────────────────────────────────────────────────

def is_internal_href(href) -> bool:
    """Yalnız iç yol: `/` ile başlar, ikinci karakter `/` ya da `\\` DEĞİL ve
    içinde `\\` yok.  WHATWG URL ayrıştırıcısı özel şemalarda `\\`'ı `/`
    sayar: `/\\evil.com/x` → `https://evil.com/x` (protokol-göreli dış
    bağlantı).  Widget'taki `isInternalHref` ile aynı kural."""
    return (isinstance(href, str) and href.startswith("/")
            and not href.startswith("//") and "\\" not in href)


# ─── Argüman temizliği ───────────────────────────────────────────────────────

def _clean_args(tool: Tool, args) -> dict:
    """Model JSON'u VERİDİR: dict değilse boş; şemadaki her anahtar için değer
    (eksik → None), tamsayı alanında '5' → 5; bilinmeyen anahtar atılır."""
    src = args if isinstance(args, dict) else {}
    out = {}
    for key, spec in tool.input_schema["properties"].items():
        v = src.get(key)
        types = spec.get("type")
        types = types if isinstance(types, list) else [types]
        if v is not None and "integer" in types and not isinstance(v, bool):
            try:
                v = int(float(v))
            except (TypeError, ValueError):
                v = None
        elif v is not None and "string" in types and not isinstance(v, str):
            v = str(v)
        out[key] = v
    return out


# ─── Çalıştırma ──────────────────────────────────────────────────────────────

def run_tool(ctx: ToolContext, name: str, args) -> ToolResult:
    """Sync — runner `anyio.to_thread.run_sync` ile çağırır.  Yetki yoksa DB
    açılmaz; işleyici hatası modele 'araç hatası' olarak döner, iz stderr'e."""
    t0 = time.perf_counter()

    def _ms() -> int:
        return int((time.perf_counter() - t0) * 1000)

    tool = REGISTRY.get(name)
    if tool is None:
        return ToolResult(compact_json({"error": "bilinmeyen araç", "tool": str(name)[:60]}),
                          True, "Bilinmeyen araç", [], _ms())
    if not ctx.can_any(*tool.perms):
        return ToolResult(compact_json({"error": DENIED_MESSAGE, "permission": tool.perm_label()}),
                          True, "Yetki yok", [], _ms())
    clean = _clean_args(tool, args)
    db = SessionLocal()
    try:
        payload = tool.handler(ctx, db, clean)
    except ToolError as e:
        return ToolResult(compact_json({"error": str(e)}), True, str(e)[:80], [], _ms())
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return ToolResult(compact_json({"error": "araç hatası"}), True, "Araç hatası", [], _ms())
    finally:
        db.close()
    if not isinstance(payload, dict):
        payload = {"result": payload}
    content, _truncated = fit_payload(payload)
    links = []
    href = payload.get("link")
    if is_internal_href(href):
        links.append({"label": tool.link_label, "href": href})
    try:
        summary = str(tool.summarize(payload))[:80]
    except Exception:
        summary = tool.link_label
    return ToolResult(content, False, summary, links, _ms(), payload)
