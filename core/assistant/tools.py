# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Araç kayıt defteri — ad, Türkçe açıklama, STRICT şema, izin, işleyici.

Kurallar (bkz. spec §1.4 / §2):
  • 14 araç HER istekte HER kullanıcıya aynı sırayla (ada göre) bildirilir —
    önek deterministik kalsın; kim neyi kullanabilir, oturum bağlamı söyler.
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
    return {"type": ["string", "null"], "enum": list(values) + [None], "description": desc}


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
P_PDKS = (("pdks", "view_own"),)
P_CRM = (("crm", "view"),)
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


def _sum_pdks(p: dict) -> str:
    return "İçeride" if p.get("state") == "iceride" else "Dışarıda"


def _sum_crm(p: dict) -> str:
    return f"{len(p.get('companies') or [])} firma · {len(p.get('contacts') or [])} kişi"


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
        "my_pdks_today",
        "Soruyu soran kullanıcının KENDİ bugünkü mesai durumu: içeride/dışarıda, giriş saati, "
        "çalışılan süre, bugünkü program. 'Bugün giriş yaptım mı', 'kaç saat çalıştım' sorularında "
        "çağır; başka personelin durumu için kullanılamaz.",
        _schema({}),
        P_PDKS, H.my_pdks_today, "PDKS", _sum_pdks,
    ),
    Tool(
        "crm_search",
        "CRM'de müşteri firma ve kişi ara: telefon, e-posta, şehir, açık fırsat sayısı (tutar yok). "
        "Müşteri/firma yetkilisi sorulduğunda çağır (yalnız CRM yetkisi olan kullanıcıda çalışır).",
        _schema({"q": _s("Firma ya da kişi adı / telefon / e-posta (en az 2 karakter)")}),
        P_CRM, H.crm_search, "CRM", _sum_crm,
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
