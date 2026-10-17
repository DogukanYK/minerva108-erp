# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Paraşüt alış faturaları — SALT OKUMA fiyat onay kuyruğu (16.10.2026).

Her gece (`core.scheduler.parasut_purchases_sync`, 02:41 UTC) Paraşüt'ten alış
faturaları okunur (`GET /purchase_bills?sort=-issue_date&include=supplier,
details,details.product`, sayfa 25), satırları `parasut_purchase_lines`
tablosuna "onay bekliyor" düşer.  Yönetici / lab (`suppliers.prices`)
`/fiyatlar` sayfasındaki kartta satırı bir IMS kartına, firmaya ve AMBALAJA
bağlayıp onaylar → `supplier_prices`'a `source='parasut'` satırı yazılır.
Paraşüt'e bu akıştan HİÇBİR şey yazılmaz: `Api` sınıfının tek HTTP metodu
`get`, yol izin listesi yalnız `purchase_bills` (test kaynak taramasıyla
kilitler); `core.parasut._api` (her metotlu yazar yardımcısı) KULLANILMAZ —
yalnız `_env` / `_get_access_token` (parola grant, önbellekli) ve `_RATE_LOCK`
(10 istek / 10 sn — iki modül aynı kilidi paylaşır) yeniden kullanılır.

Beyaz liste (KVKK): fatura id / no / tarih / vade / para birimi / kur / net ve
brüt toplam / açıklama; tedarikçi YALNIZ Paraşüt id + ad (e-posta, telefon,
vergi no, adres SAKLANMAZ); satır açıklama / ürün id-ad-birim / adet / birim
fiyat / indirim / KDV / net toplam.  Ham JSON tutulmaz.

Öneri (`suggest_item`): önce Belge Oku'nun kart eşleştirme kuralı (`doc_key` /
`score_item` / `material_key`; kesin cevabı başka karta DEĞİŞMEZ — yalnız ad
uzunluğuyla 0,1 puanla seçildiyse öneri yok), yoksa ters kapsama (kart adı
Paraşüt metninin içinde → 80 + kapsama; genel kart özgülüne yenilir, ambalaj
parçası satırı ("… KAVANOZ KAPAĞI") kaba bağlanmaz, "A + B" / tek kalemi kartlı
set satırı ve aynı malzemenin firmasız iki kartı öneri almaz), faturanın
firmasına ait kart +5; 3'ten az harfli metne öneri yok — yalnız TEK ve ≥60
puanlı aday önerilir, ASLA otomatik bağlanmaz.  Onay / atlama kart → satır `FOR UPDATE`
kilidiyle sıraya girer (aynı satırın ikinci kararı 409); kademe betiği tüm
kartları sonra tüm satırları önceden kilitler (`lock_for_approval`); kalan
nadir kilitlenmede uç 409 `busy` döner (`is_lock_busy`).  Onay (`approve_line`):
net birim fiyat = birim fiyat × (1 − indirim) ÷ ambalaj; `vat_included=False`
+ satırın KDV oranı; para birimi faturadan ("TRL" → "TRY"); `quoted_at` fatura
tarihi; `source_label` "Paraşüt alış faturası <no> — gg.aa.yyyy"; (kart,
firma) başına TEK kayıt, en yeni fatura kazanır (eski fatura mevcut satırı
EZMEZ); aynı firmanın ELLE fiyatı varsa yazılmaz (`manual_exists`); ambalaj
Paraşüt ürünü başına hatırlanır (`parasut_product_packs`).  12 aydan eski
faturanın fiyatı satın alma planında seçilmez (`purchase_pricing._offer_status`
"eski fatura (12 aydan eski)") — fiyat sayfası aynı rozeti gösterir.

Su işareti `AppSetting parasut.purchases.watermark` (okunan en yeni fatura
tarihi; sonraki okuma OVERLAP_DAYS geriden başlar, aynı fatura tekrar
yazılmaz).  Zamanlayıcı yalnız `core.parasut.is_configured()` VE
`AppSetting parasut.purchases.enabled == "1"` (varsayılan KAPALI) iken okur.
Kapanış bayrağı (`request_stop`) süren okumayı istek / bekleme arasında
durdurur; yarım kalan iş sonraki gece kaldığı yerden sürer (su işareti yalnız
tamamlanan okumayla ilerler).

AĞ: testler `Api(transport=httpx.MockTransport, sleep=…, token_fn=…)` ile
çalışır; gerçek Paraşüt API'si testlerden / betiklerden ASLA çağrılmaz.
"""
import logging
import math
import re
import threading
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from core import parasut as P
from core.supplier_prices import (CURRENCIES, PRICE_UNITS, SOURCE_MANUAL, SOURCE_PARASUT, clean_name,
                                  default_price_unit, firm_keys, normalize, price_unit_ok, serialize_price)
from database import (AppSetting, Item, ParasutProductPack, ParasutPurchaseBill, ParasutPurchaseLine, Supplier,
                      SupplierPrice)

logger = logging.getLogger("minerva108.parasut.purchases")

SETTING_ENABLED = "parasut.purchases.enabled"
SETTING_WATERMARK = "parasut.purchases.watermark"
SETTING_LAST_RUN = "parasut.purchases.last_run"
DEFAULT_DOMAIN = "cosmetics"          # öneri kartları bu panelde aranır (hammaddeler burada)
PAGE_SIZE = 25
MAX_PAGES = 200
OVERLAP_DAYS = 30                     # su işaretinden bu kadar geriye yeniden bakılır (geç girilen fatura)
INCLUDE = "supplier,details,details.product"
RETRY_429 = (10.0, 30.0, 60.0)
RETRY_5XX = (2.0, 5.0, 15.0)
MAX_RETRY_AFTER = 120.0
STALE_DAYS = 365
SUGGEST_MIN_SCORE = 60.0
QUEUE_LIMIT = 200
CURRENCY_MAP = {"TRL": "TRY", "TL": "TRY", "TRY": "TRY", "USD": "USD", "EUR": "EUR"}
#: İzin verilen yollar — yalnız alış faturası listesi.  Başka bir yola GET
#  atmak ValueError'dır (okuma-yalnızlık testi kilitler).
ALLOWED_PATHS = (re.compile(r"^/purchase_bills$"),)
# Gider ipucu (kargo, seyahat, kiralama, ilan …) — yalnız öneri; satır yine kuyruğa düşer
_EXPENSE_RX = re.compile(
    r"\b(kargo|tasima|nakliye|seyahat|turizm|bilet|ucus|dis hat|kira|kiralama|kariyer|ilan|reklam|danismanlik|"
    r"muhasebe|sigorta|yakit|otopark|konaklama|abonelik|lisans|iade bedeli|hizmet bedeli)\b")
REQUIRED_ENV = ("PARASUT_CLIENT_ID", "PARASUT_CLIENT_SECRET", "PARASUT_COMPANY_ID", "PARASUT_EMAIL",
                "PARASUT_PASSWORD")
STATUSES = ("pending", "approved", "skipped")


class ParasutPurchasesError(RuntimeError):
    """Taşıma / API hatası — mesaj kullanıcıya gösterilebilir, kimlik bilgisi içermez."""


class ParasutPurchasesNotConfigured(ParasutPurchasesError):
    pass


class ParasutPurchasesStopped(ParasutPurchasesError):
    """Uygulama kapanıyor — okuma bir istek / bekleme sonunda durdu (hata SAYILMAZ)."""


class ApproveError(ValueError):
    """Onay reddi: `code` (manual_exists | not_pending | not_found | item_not_found |
    supplier_not_found | supplier_inactive | bad_unit | bad_package | bad_currency |
    no_price) + okunur mesaj; uç `http` koduyla döner."""

    def __init__(self, code: str, message: str, http: int = 400):
        super().__init__(message)
        self.code = code
        self.http = http


STOP_MSG = "Uygulama kapanıyor — Paraşüt okuması durduruldu (sonraki gece kaldığı yerden sürer)."
_STOP = threading.Event()
_RUN_LOCK = threading.Lock()


def request_stop() -> None:
    _STOP.set()


def reset_stop() -> None:
    _STOP.clear()


def _interruptible_sleep(seconds: float) -> None:
    if _STOP.wait(max(0.0, seconds)):
        raise ParasutPurchasesStopped(STOP_MSG)


# ─── Yapılandırma / ayarlar ──────────────────────────────────────────────────

def config_problems() -> List[str]:
    """Eksik ayarların ADLARI — değer asla dönmez."""
    return [n for n in REQUIRED_ENV if not P._env(n[len("PARASUT_"):])]


def is_configured() -> bool:
    return not config_problems()


def _get_setting(db: Session, key: str) -> Optional[str]:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row else None


def _set_setting(db: Session, key: str, value: Optional[str]) -> None:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


def get_enabled(db: Session) -> bool:
    """Gece okuması açık mı — varsayılan KAPALI (yalnız "1" açar)."""
    return (_get_setting(db, SETTING_ENABLED) or "").strip() == "1"


def set_enabled(db: Session, enabled: bool) -> None:
    _set_setting(db, SETTING_ENABLED, "1" if enabled else "0")
    db.commit()


def get_watermark(db: Session) -> Optional[date]:
    v = (_get_setting(db, SETTING_WATERMARK) or "").strip()
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _retry_after(r: httpx.Response) -> Optional[float]:
    try:
        v = float(r.headers.get("Retry-After", ""))
    except ValueError:
        return None
    return min(max(v, 1.0), MAX_RETRY_AFTER)


def _body_snippet(r: httpx.Response) -> str:
    try:
        return " ".join((r.text or "").split())[:200]
    except Exception:
        return ""


# ─── İstemci (yalnız GET) ────────────────────────────────────────────────────

class Api:
    """Paraşüt v4 SALT OKUMA istemcisi.  Tek HTTP metodu `get` (izin listeli
    yol); bearer token `core.parasut._get_access_token` ile (parola grant,
    süreç içi önbellek) — `core.parasut._api` KULLANILMAZ.  Her istek
    `core.parasut._RATE_LOCK` altında + ardından `_RATE_SLEEP` bekler (yazar
    modülle aynı kota).  429 → Retry-After / artan bekleme, 5xx / 408 / taşıma
    hatası sınırlı tekrar; 3xx yönlendirme izlenmez (başarı SAYILMAZ).
    Token ve parola hiçbir mesaja / log satırına girmez.  Testler
    `transport=httpx.MockTransport`, `sleep=`, `token_fn=` enjekte eder."""

    def __init__(self, *, transport=None, sleep: Optional[Callable[[float], None]] = None,
                 token_fn: Optional[Callable[[], str]] = None, base_url: Optional[str] = None):
        if not is_configured():
            raise ParasutPurchasesNotConfigured(
                "Paraşüt yapılandırılmamış — eksik: " + ", ".join(config_problems()))
        self._http = httpx.Client(base_url=(base_url or P._BASE).rstrip("/") + f"/v4/{P.company_id()}",
                                  headers={"Accept": "application/json"},
                                  timeout=httpx.Timeout(P._TIMEOUT, connect=10.0),
                                  follow_redirects=False, transport=transport)
        self._token_fn = token_fn or P._get_access_token
        self._sleep = sleep or _interruptible_sleep
        self.calls = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def get(self, path: str, params: Optional[dict] = None):
        if not any(rx.match(path) for rx in ALLOWED_PATHS):
            raise ValueError(f"İzin verilmeyen Paraşüt yolu: {path}")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        return self._send(lambda tok: self._http.get(path, params=params,
                                                     headers={"Authorization": f"Bearer {tok}"}))

    def _send(self, call: Callable[[str], httpx.Response]):
        n429 = n5xx = 0
        while True:
            if _STOP.is_set():
                raise ParasutPurchasesStopped(STOP_MSG)
            try:
                token = self._token_fn()
            except P.ParasutError as e:
                raise ParasutPurchasesError(f"Paraşüt girişi başarısız: {str(e)[:200]}") from None
            err = None
            with P._RATE_LOCK:
                try:
                    r = call(token)
                except httpx.TimeoutException:
                    err = "zaman aşımı"
                except httpx.TransportError as e:
                    err = type(e).__name__
                self._sleep(P._RATE_SLEEP)
            if err is not None:
                if n5xx < len(RETRY_5XX):
                    self._sleep(RETRY_5XX[n5xx])
                    n5xx += 1
                    continue
                raise ParasutPurchasesError(f"Paraşüt'e ulaşılamadı ({err}).")
            self.calls += 1
            sc = r.status_code
            if sc == 429:
                if n429 < len(RETRY_429):
                    self._sleep(_retry_after(r) or RETRY_429[n429])
                    n429 += 1
                    continue
                raise ParasutPurchasesError("Paraşüt istek sınırı aşıldı (429) — biraz sonra yeniden deneyin.")
            if sc >= 500 or sc == 408:
                if n5xx < len(RETRY_5XX):
                    self._sleep(RETRY_5XX[n5xx])
                    n5xx += 1
                    continue
                raise ParasutPurchasesError(f"Paraşüt sunucu hatası ({sc}) — sonra yeniden denenecek.")
            if sc == 401:
                P._TOKEN_CACHE.pop("token", None)        # süresi dolmuş token: sonraki çağrı yeniden girer
                raise ParasutPurchasesError("Paraşüt kimlik doğrulaması başarısız (401) — "
                                            "PARASUT_EMAIL / PARASUT_PASSWORD / istemci ayarlarını kontrol edin.")
            if sc == 403:
                raise ParasutPurchasesError("Paraşüt isteği reddetti (403) — hesabın alış faturası okuma yetkisi yok.")
            if sc >= 400:
                raise ParasutPurchasesError(f"Paraşüt isteği kabul etmedi ({sc}): {_body_snippet(r)}")
            if not 200 <= sc < 300:            # 3xx: yönlendirme izlenmez — boş başarı SAYILMAZ
                raise ParasutPurchasesError(f"Paraşüt beklenmeyen yanıt döndü ({sc}).")
            if sc == 204 or not r.content:
                return {}
            try:
                data = r.json()
            except ValueError:
                raise ParasutPurchasesError("Paraşüt geçersiz yanıt döndü (JSON değil).") from None
            return data if isinstance(data, dict) else {}


def make_api() -> Api:
    """Varsayılan istemci — testler / betik bunu sahteyle değiştirir."""
    return Api()


# ─── Okuma + beyaz liste ─────────────────────────────────────────────────────

def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _txt(v, n: int) -> Optional[str]:
    s = clean_name(v)
    return s[:n] if s else None


def _date(v) -> Optional[date]:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def currency_code(v) -> Optional[str]:
    """Paraşüt para birimi → IMS kodu ("TRL" → "TRY"); bilinmeyen → olduğu gibi (büyük harf)."""
    s = (str(v or "")).strip().upper()
    return CURRENCY_MAP.get(s, s) or None


def expense_hint(*texts) -> bool:
    """Metinlerden biri gider faturası / kalemine benziyor mu (kargo, seyahat,
    kiralama, ilan, hizmet bedeli …).  Yalnız öneri amaçlı işaret."""
    for t in texts:
        if t and _EXPENSE_RX.search(normalize(t)):
            return True
    return False


def _discount_rate(att: dict, qty: Optional[float], unit_price: Optional[float]) -> Optional[float]:
    """Satır indirimi 0–1 kesir: yüzde → /100; tutar → tutar ÷ (adet × birim fiyat)."""
    dv = _num(att.get("discount_value"))
    if not dv or dv <= 0:
        return None
    dt = (str(att.get("discount_type") or "")).strip().lower()
    if dt == "percentage" or dt == "percent":
        rate = dv / 100.0
    else:
        base = (qty or 0.0) * (unit_price or 0.0)
        if base <= 0:
            return None
        rate = dv / base
    return max(0.0, min(1.0, rate))


def _incmap(payload: dict) -> Dict[Tuple[str, str], dict]:
    return {(x.get("type"), str(x.get("id"))): x for x in (payload.get("included") or [])
            if isinstance(x, dict) and x.get("type") and x.get("id") is not None}


def _rel(obj: dict, name: str):
    return ((obj.get("relationships") or {}).get(name) or {}).get("data")


def parse_bill(raw: dict, incmap: Dict[Tuple[str, str], dict]) -> Optional[dict]:
    """JSON:API alış faturası → beyaz listeli sözlük (tedarikçi yalnız id + ad;
    iletişim / vergi alanları ATILIR).  item_type purchase_bill dışı → None."""
    att = raw.get("attributes") or {}
    item_type = (str(att.get("item_type") or "")).strip().lower() or None
    if item_type not in (None, "purchase_bill"):
        return None
    pid = _int(raw.get("id"))
    if pid is None:
        return None
    sup = _rel(raw, "supplier")
    sup_obj = incmap.get((sup.get("type"), str(sup.get("id")))) if isinstance(sup, dict) else None
    sup_name = _txt(((sup_obj or {}).get("attributes") or {}).get("name"), 150)
    desc = _txt(att.get("description"), 250)
    lines = []
    for d in (_rel(raw, "details") or []):
        if not isinstance(d, dict):
            continue
        det = incmap.get((d.get("type"), str(d.get("id"))))
        if not det:
            continue
        da = det.get("attributes") or {}
        pr = _rel(det, "product")
        prod = incmap.get((pr.get("type"), str(pr.get("id")))) if isinstance(pr, dict) else None
        pa = (prod or {}).get("attributes") or {}
        qty, up = _num(da.get("quantity")), _num(da.get("unit_price"))
        ldesc = _txt(da.get("description"), 250)
        pname = _txt(pa.get("name"), 150)
        lines.append({
            "parasut_detail_id": _int(det.get("id")),
            "description": ldesc,
            "product_parasut_id": _int((prod or {}).get("id")),
            "product_name": pname,
            "product_unit": _txt(pa.get("unit"), 20),
            "quantity": qty,
            "unit_price": up,
            "discount_rate": _discount_rate(da, qty, up),
            "vat_rate": _num(da.get("vat_rate")),
            "net_total": _num(da.get("net_total")),
            "expense_hint": expense_hint(ldesc, pname),
        })
    lines = [ln for ln in lines if ln["parasut_detail_id"] is not None]
    bill_expense = expense_hint(sup_name, desc) or (bool(lines) and all(ln["expense_hint"] for ln in lines))
    return {
        "parasut_id": pid,
        "item_type": item_type or "purchase_bill",
        "invoice_no": _txt(att.get("invoice_no"), 60),
        "issue_date": _date(att.get("issue_date")),
        "due_date": _date(att.get("due_date")),
        "currency": currency_code(att.get("currency")),
        "exchange_rate": _num(att.get("exchange_rate")),
        "net_total": _num(att.get("net_total")),
        "gross_total": _num(att.get("gross_total")),
        "description": desc,
        "supplier_parasut_id": _int((sup or {}).get("id")) if isinstance(sup, dict) else None,
        "supplier_name": sup_name,
        "expense_hint": bill_expense,
        "lines": lines,
    }


BILL_KEYS = frozenset(("parasut_id", "item_type", "invoice_no", "issue_date", "due_date", "currency", "exchange_rate",
                       "net_total", "gross_total", "description", "supplier_parasut_id", "supplier_name",
                       "expense_hint", "lines"))
LINE_KEYS = frozenset(("parasut_detail_id", "description", "product_parasut_id", "product_name", "product_unit",
                       "quantity", "unit_price", "discount_rate", "vat_rate", "net_total", "expense_hint"))


def fetch_bills(api, since: Optional[date] = None, *, max_pages: int = MAX_PAGES) -> List[dict]:
    """Alış faturalarını yeniden eskiye sayfalar; `since` verilirse ondan eski
    faturaya gelince durur (sıralama -issue_date).  Döner: `parse_bill` listesi."""
    out: List[dict] = []
    page = 1
    while page <= max_pages:
        if _STOP.is_set():
            raise ParasutPurchasesStopped(STOP_MSG)
        payload = api.get("/purchase_bills", {"sort": "-issue_date", "include": INCLUDE,
                                              "page[size]": PAGE_SIZE, "page[number]": page}) or {}
        inc = _incmap(payload)
        rows = payload.get("data") or []
        stop = False
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            b = parse_bill(raw, inc)
            if b is None:
                continue
            if since is not None and b["issue_date"] is not None and b["issue_date"] < since:
                stop = True
                break
            out.append(b)
        total_pages = _int(((payload.get("meta") or {}).get("total_pages"))) or 1
        if stop or not rows or page >= total_pages:
            break
        page += 1
    return out


# ─── Öneri (kart eşleştirme) ─────────────────────────────────────────────────

def build_item_index(db: Session, domain: str) -> list:
    """Belge Oku'nun kart dizini (aktif, panel içi, bitmiş ürün hariç)."""
    from core.assistant.documents import doc_key
    from core.material_groups import material_key
    rows = (db.query(Item).filter(Item.domain == domain, Item.is_active == True)        # noqa: E712
            .filter(Item.category != "Bitmiş Ürün").all())
    return [(it, normalize(it.name), normalize(it.name_tr), normalize(it.sku), normalize(it.barcode),
             material_key(it.name), (doc_key(it.name), doc_key(it.name_tr))) for it in rows]


def match_supplier(db: Session, domain: str, name: Optional[str]) -> Optional[dict]:
    """Fatura firmasının IMS kartı (Belge Oku kuralı: ≥70 puan, aktif, tek) → {id, name, score} | None."""
    from core.assistant.documents import _supplier_match
    return _supplier_match(db, domain, name).get("match")


QUERY_MIN_ALPHA = 3          # "1", "10", "A" gibi ürün adları → öneri yok (alt-dizgi kuralı her karta uyardı)
REVERSE_BASE = 80.0          # kart adı Paraşüt metninin İÇİNDE geçiyor ("KARNAUBA MUMU (CARNAUBA WAX)")
REVERSE_MIN_TOKENS = 2       # ters kapsama yalnız ≥2 kelimelik ...
REVERSE_MIN_CHARS = 6        # ... ve ≥6 harf/rakamlık kart adında (tek kelime / kısa ad her metinde geçer)
# Kart adı metinde bir kelimenin ORTASINDA bitebilir yalnız bu eklerle (katlanmış):
# iyelik / belirtme hâli ("PROTEİN" → "PROTEİNİ", "AROMA" → "AROMASI") ve İngilizce
# kesik yazım ("GLUCOSİD" → "GLUCOSIDE").  "-lı/-li" (ile: "GOLD KAPAKLI CAM
# KAVANOZ" kapaklı kavanozdur, kapak DEĞİL), "-sız" (olmadan), çoğul ve rakam
# ("OLİVEM 1000" → "10000") ters kapsama SAYILMAZ.
REVERSE_SUFFIXES = frozenset({"", "i", "u", "si", "su", "yi", "yu", "e"})
REVERSE_TIE_MARGIN = 1.0     # ters kapsanan iki kart: yalnız firma +5'i ayırır (kapsama farkı < 1)
# Ambalaj PARÇASI satırı: Türkçede baş isim sondadır — "50 ML CAM KAVANOZ KAPAĞI"
# kavanozun KAPAĞIDIR, "100 ML CAM ŞİŞE POMPASI" şişenin pompası.  Ambalaj kartının
# adından (ve izinli ekinden) hemen sonraki kelime iyelik ekli bir isimse (katlanmış:
# kapagi, pompasi, bilyesi, contasi, kutusu) ya da "için" ise o kart satırın kalemi
# DEĞİLDİR.  "-lı/-lu" sıfatı ("KAPAKLI", "RENKLİ") ve renk kabın kendisini niteler.
_PART_HEAD_RX = re.compile(r"\s+([a-z]+)(?![a-z0-9])")
_PART_HEAD_WORD = re.compile(r"[a-z]{2,}(?:si|su|i|u)")
_NOT_PART_HEAD = frozenset({"mavi", "gri", "sari", "kirmizi", "turuncu"})


def _belge_oku_pick(cands: List[dict]) -> Optional[dict]:
    """Belge Oku'nun seçim kuralı (`match_document` ile aynı): ilk aday ≥60 ve
    ikinci aday ondan düşük puanlıysa o; değilse None."""
    best = cands[0] if cands and cands[0]["score"] >= SUGGEST_MIN_SCORE else None
    if best and len(cands) > 1 and cands[1]["score"] == best["score"]:
        return None
    return best


def _forward_near_tie(pick: dict, cands: List[dict], qn: str, items: list) -> bool:
    """Belge Oku'nun kesin cevabı yalnız AD UZUNLUĞUYLA mı seçildi?  Kısa / genel
    metin birden çok kart adının içinde geçince ("Kil" → MAVİ KİL 81,0 ·
    BENTONİT KİL 80,9; "Etil" → ETİL ALKOL · METİL SELÜLOZ) en kısa ad 0,1 puanla
    kazanır — bu bir eşleşme değil, eşitliktir.  İkisi de ≥80 ve fark
    `REVERSE_TIE_MARGIN`'den küçükse (firma +5'i farkı açar) ve metin kartın kendi
    adı değilse evet.  Sonuç yalnız "öneri yok"tur; başka kart SEÇİLMEZ."""
    if pick["score"] < REVERSE_BASE or len(cands) < 2:
        return False
    second = cands[1]["score"]
    if second < REVERSE_BASE or pick["score"] - second >= REVERSE_TIE_MARGIN:
        return False
    own = next(((name_n, tr_n) for it, name_n, tr_n, *_ in items if it.id == pick["id"]), ())
    return qn not in own


def _part_head_after(qn: str, end: int) -> bool:
    """Kart adının bittiği yerden (`end`; izinli ek atlanır) sonraki ilk kelime
    bir parça başı mı (bkz. `_PART_HEAD_RX`)?"""
    k = end
    while k < len(qn) and qn[k].isalnum():
        k += 1
    m = _PART_HEAD_RX.match(qn, k)
    if not m:
        return False
    w = m.group(1)
    if w == "icin":
        return True
    return (_PART_HEAD_WORD.fullmatch(w) is not None and w not in _NOT_PART_HEAD
            and not w.endswith(("li", "lu")))


def _starts_word_in(n: str, qn: str) -> bool:
    """`n`, `qn` içinde bir kelime BAŞINDA geçiyor mu (sonu serbest)."""
    i = qn.find(n)
    while i >= 0:
        if i == 0 or not qn[i - 1].isalnum():
            return True
        i = qn.find(n, i + 1)
    return False


def _reverse_span(n: str, qn: str) -> Optional[Tuple[int, int]]:
    """Ters kapsama: ≥2 kelime ve ≥6 harf/rakamlık kart adı `n`, metin `qn`
    içinde bir kelime BAŞINDA geçiyor ve kelime sonunda ya da izin verilen bir
    ekle (`REVERSE_SUFFIXES`) bitiyorsa ilk geçişin (başlangıç, bitiş) aralığı;
    yoksa None."""
    if not n or len(n.split()) < REVERSE_MIN_TOKENS or sum(c.isalnum() for c in n) < REVERSE_MIN_CHARS:
        return None
    i = qn.find(n)
    while i >= 0:
        if i == 0 or not qn[i - 1].isalnum():
            j = k = i + len(n)
            while k < len(qn) and qn[k].isalnum():
                k += 1
            if qn[j:k] in REVERSE_SUFFIXES:
                return i, j
        i = qn.find(n, i + 1)
    return None


def _reverse_hits(items: list, qn: str, supplier_id: Optional[int], bonus: float) -> Dict[int, dict]:
    """Metnin içinde adı (ya da TR adı) geçen kartlar → {kart id: {score, span,
    name, key, it}}.  Puan 80 + kapsama (kart adı ÷ metin uzunluğu, 0–1) +
    faturanın firmasının kartıysa `bonus`; `key` eşleşen adın `doc_key`'i.
    Ambalaj kartının adından sonra parça başı geliyorsa ("… KAVANOZ KAPAĞI",
    "… ŞİŞE İÇİN SPREY POMPA") o geçiş sayılmaz (`_part_head_after`)."""
    hits: Dict[int, dict] = {}
    for it, name_n, tr_n, _sku, _bc, _key, dkeys in items:
        best = None
        packaging = (it.category or "") == "Ambalaj"
        for n, dk in ((name_n, dkeys[0]), (tr_n, dkeys[1])):
            span = _reverse_span(n, qn)
            if span is None or (packaging and _part_head_after(qn, span[1])):
                continue
            s = REVERSE_BASE + min(1.0, len(n) / len(qn))
            if best is None or s > best["score"]:
                best = {"score": s, "span": span, "name": n, "key": frozenset(dk or ())}
        if best is None:
            continue
        if supplier_id and it.supplier_id == supplier_id:
            best["score"] += bonus
        best["it"] = it
        hits[it.id] = best
    return hits


SUBSUME_MIN_JACCARD = 0.5   # özgül kart metnin anahtarıyla en az Belge Oku'nun aday eşiği kadar örtüşmeli


def _subsumed(hit: dict, items: list, qd: frozenset) -> bool:
    """Kapsanan kart GENEL ad mı?  Başka bir kartın adı onun adını içeriyor ya da
    `doc_key`'i onunkini kesin kapsıyor, o kart metnin anahtarından DAHA
    FAZLASINI karşılıyor VE metinle kendisi de makul eşleşiyorsa (anahtar
    Jaccard ≥0,5 — Belge Oku'nun aday eşiği) evet: "KARBOKSİ METİL SELÜLOZ (CMC)"
    metninde METİL SELÜLOZ (başka kimyasal), "LAMESOFT (COCO GLUCOSIDE GLYCERYL
    OLEATE)" metninde COCO GLUCOSIDE.  Aynı anahtarlı kart (aynı malzemenin başka
    firmaya kartı) genel SAYILMAZ; "ŞİŞE + KAPAK" satırında iki kalemin
    sözcüklerini birden taşıyan uzak kart (Jaccard <0,5) bir kalemi DÜŞÜRMEZ."""
    wk, wn, wid = hit["key"], hit["name"], hit["it"].id
    w_cov = len(wk & qd)
    for it, name_n, tr_n, _sku, _bc, _key, dkeys in items:
        if it.id == wid:
            continue
        for n, dk in ((name_n, dkeys[0]), (tr_n, dkeys[1])):
            if not n:
                continue
            rk = frozenset(dk or ())
            cov = len(rk & qd)
            if (cov > w_cov and (rk > wk or (n != wn and _starts_word_in(wn, n)))
                    and cov / len(rk | qd) >= SUBSUME_MIN_JACCARD):
                return True
    return False


def _covered_by_longer(hit: dict, hits: Dict[int, dict]) -> bool:
    """Metinde bu kartın geçişini İÇEREN, daha uzun ve `doc_key`'i kesin kapsayan
    başka bir kapsanan kart var mı?  "DUY.LAV.01 TIBBİ LAVANTA UÇUCU YAĞI
    (Lavandula angustifolia) 10 ml." metninde TIBBİ LAVANTA kartı LAVANTA kartını
    düşürür — kod + Latince ad metnin anahtarını sulandırdığı için `_subsumed`'un
    Jaccard koşulu burada tutmaz; iki ad da metinde AYNEN geçtiği için gerekmez.
    Aynı anahtarlı yazım ikizleri (UCUCU YAĞ / UÇUCU YAĞI) birbirini düşürmez."""
    s, e = hit["span"]
    for o in hits.values():
        if o is hit:
            continue
        os_, oe = o["span"]
        if os_ <= s and e <= oe and oe - os_ > e - s and o["key"] > hit["key"]:
            return True
    return False


def _composite(a: dict, b: dict, qn: str) -> bool:
    """İki kapsanan kart metnin AYRI yerlerinde ve ya araları "+" ("ŞİŞE + KAPAK")
    ya da anahtarları ortaksız (farklı malzeme) → satır birden çok kalemdir."""
    (s1, e1), (s2, e2) = a["span"], b["span"]
    if e1 > s2 and e2 > s1:
        return False                                  # örtüşüyor (aynı yerde iki ad)
    lo, hi = (e1, s2) if e1 <= s2 else (e2, s1)
    return "+" in qn[lo:hi] or not (a["key"] & b["key"])


def suggest_item(db: Session, line: dict, domain: str = DEFAULT_DOMAIN, *, index=None,
                 supplier_id: Optional[int] = None) -> dict:
    """Satır → {"suggested": {id, name, unit, category, score} | None, "candidates": [...]}.
    Metin: ürün adı, yoksa satır açıklaması; 3'ten az harfli metin ("1", "A") →
    öneri yok.  Hiçbir şey YAZMAZ, ASLA otomatik bağlamaz.

    1) Belge Oku'nun kuralı (`_item_candidates` + `match_document`'in seçimi,
       dokunulmaz; faturanın firmasının kartı +5): TEK ve ≥60 puanlı aday varsa
       öneri ODUR ve adaylar onun listesidir — ters kapsama bu cevabı DEĞİŞTİRMEZ
       ("CMC KARBOKSİ METİL SELÜLOZ TEKNİK" → CMC kartı, METİL SELÜLOZ değil).
    2) Yoksa TERS KAPSAMA — Paraşüt ürün adları çoğunlukla kart adını İÇERİR
       ("DUY.FRK-SE.07 AKGÜNLÜK UÇUÇU YAĞI (Boswellia serrata) 10 ml." ⊃
       "AKGÜNLÜK UÇUCU YAĞI"): `_reverse_span` (≥2 kelime, ≥6 karakter, kelime
       başı, yalnız iyelik / kesik yazım eki) → 80 + kapsama (+5 firma).
       Ambalaj kartından sonra parça başı gelen geçiş sayılmaz ("… KAVANOZ
       KAPAĞI" kavanoz değil, `_part_head_after`).  Daha özgül bir kartın
       kapsadığı genel kart düşer (`_subsumed`, `_covered_by_longer`).  Kalan
       iki kapsanan kart satırın ayrı kalemleriyse (`_composite`, "ŞİŞE + POMPA")
       ya da kazanan kartın dışında "+" kalıyorsa (tek kalemi kartlı set satırı)
       öneri yok; aynı malzemenin iki kartıysa yalnız firma +5'i ayırır (aksi
       hâlde öneri yok).  Yalnız TEK ve ≥60 puanlı aday önerilir.
    1'in tek istisnası: kesin cevap yalnız ad uzunluğuyla seçildiyse
    (`_forward_near_tie`, "Kil" → MAVİ KİL 81,0 / BENTONİT KİL 80,9) öneri yok —
    başka karta GEÇİLMEZ."""
    from core.assistant.documents import CANDIDATES_MAX, SUPPLIER_BONUS, _item_candidates, doc_key
    q = clean_name(line.get("product_name")) or clean_name(line.get("description")) or ""
    qn = normalize(q)
    if sum(1 for c in qn if c.isalpha()) < QUERY_MIN_ALPHA:
        return {"suggested": None, "candidates": []}
    items = index if index is not None else build_item_index(db, domain)
    base = _item_candidates(items, {"material": q, "material_code": None}, supplier_id)
    pick = _belge_oku_pick(base)
    if pick is not None:
        if _forward_near_tie(pick, base, qn, items):
            return {"suggested": None, "candidates": base}
        return {"suggested": pick, "candidates": base}
    qd = frozenset(doc_key(q))
    raw = _reverse_hits(items, qn, supplier_id, SUPPLIER_BONUS)
    hits = {k: h for k, h in raw.items() if not _subsumed(h, items, qd) and not _covered_by_longer(h, raw)}
    scored: Dict[int, Tuple[float, dict]] = {c["id"]: (float(c["score"]), c) for c in base}
    for iid, h in hits.items():
        if iid not in scored or h["score"] > scored[iid][0]:
            it = h["it"]
            scored[iid] = (h["score"], {"id": it.id, "name": it.name, "unit": it.unit or "",
                                        "category": it.category or ""})
    ranked = sorted(scored.values(), key=lambda t: (-t[0], t[1]["name"]))[:CANDIDATES_MAX]
    cands = [{**c, "score": round(s, 1)} for s, c in ranked]
    best = cands[0] if ranked and ranked[0][0] >= SUGGEST_MIN_SCORE else None
    if best and len(ranked) > 1 and abs(ranked[1][0] - ranked[0][0]) < 1e-9:
        best = None
    hl = list(hits.values())
    if best and any(_composite(a, b, qn) for i, a in enumerate(hl) for b in hl[i + 1:]):
        best = None                                   # "A + B" satırı — tek karta bağlanmaz
    if best and best["id"] in hits and any(
            h["score"] > ranked[0][0] - REVERSE_TIE_MARGIN for k, h in hits.items() if k != best["id"]):
        best = None                                   # aynı malzemenin iki kartı — firma ayırmıyor
    if best and best["id"] in hits:
        s_, e_ = hits[best["id"]]["span"]
        if "+" in qn[:s_] or "+" in qn[e_:]:
            best = None                               # "KAVANOZ + METALİZE KAPAK" — kapağın kartı yok
    return {"suggested": best, "candidates": cands}


# ─── Senkron ─────────────────────────────────────────────────────────────────

def _bill_fields(b: dict) -> dict:
    return {k: b.get(k) for k in BILL_KEYS if k != "lines"}


def upsert_bill(db: Session, b: dict) -> Tuple[ParasutPurchaseBill, List[ParasutPurchaseLine], bool]:
    """Fatura başlığını yazar / günceller; YENİ satırları ekler (mevcut satırın
    onay durumu korunur, fatura alanları yerinde güncellenir).
    Döner: (fatura, yeni satırlar, fatura yeni miydi)."""
    row = db.query(ParasutPurchaseBill).filter(ParasutPurchaseBill.parasut_id == b["parasut_id"]).first()
    created = row is None
    if created:
        row = ParasutPurchaseBill(**_bill_fields(b))
        db.add(row)
        db.flush()
    else:
        for k, v in _bill_fields(b).items():
            setattr(row, k, v)
    seen = {ln.parasut_detail_id: ln for ln in row.lines}
    new_lines: List[ParasutPurchaseLine] = []
    for ld in b.get("lines") or []:
        ex = seen.get(ld["parasut_detail_id"])
        if ex is not None:
            for k in LINE_KEYS:
                setattr(ex, k, ld.get(k))
            continue
        ln = ParasutPurchaseLine(bill_id=row.id, status="pending", **{k: ld.get(k) for k in LINE_KEYS})
        db.add(ln)
        new_lines.append(ln)
    db.flush()
    return row, new_lines, created


def is_running() -> bool:
    return _RUN_LOCK.locked()


def run_sync(db: Session, api=None, *, domain: str = DEFAULT_DOMAIN, notify: bool = True,
             since: Optional[date] = None) -> dict:
    """Gece okuması: su işaretinden (− OVERLAP_DAYS) bugüne faturalar → upsert;
    yeni satırlara öneri; su işareti yalnız TAMAMLANAN okumayla ve yalnız ileri;
    bekleyen yeni satır varsa yöneticilere bildirim.  Süreç içi kilit: dolu ise
    {"skipped": "running"}.  Hiçbir Paraşüt yazma çağrısı yoktur."""
    if not _RUN_LOCK.acquire(blocking=False):
        return {"skipped": "running"}
    try:
        api = api or make_api()
        wm = get_watermark(db)
        if since is None and wm is not None:
            since = wm - timedelta(days=OVERLAP_DAYS)
        bills = fetch_bills(api, since)
        stats = {"bills": len(bills), "bills_new": 0, "lines_new": 0, "suggested": 0, "expense_lines": 0,
                 "since": since.isoformat() if since else None}
        index = None
        sup_cache: Dict[str, Optional[int]] = {}
        newest: Optional[date] = wm
        samples: List[str] = []
        for b in bills:
            if _STOP.is_set():
                raise ParasutPurchasesStopped(STOP_MSG)
            row, new_lines, created = upsert_bill(db, b)
            stats["bills_new"] += int(created)
            for ln in new_lines:
                stats["lines_new"] += 1
                stats["expense_lines"] += int(bool(ln.expense_hint))
                if index is None:
                    index = build_item_index(db, domain)
                key = normalize(b.get("supplier_name"))
                if key not in sup_cache:
                    m = match_supplier(db, domain, b.get("supplier_name"))
                    sup_cache[key] = m["id"] if m else None
                sug = suggest_item(db, {"product_name": ln.product_name, "description": ln.description},
                                   domain, index=index, supplier_id=sup_cache[key])["suggested"]
                if sug:
                    ln.suggested_item_id, ln.suggested_score = sug["id"], sug["score"]
                    stats["suggested"] += 1
                if len(samples) < 3:
                    samples.append(ln.product_name or ln.description or "?")
            if b.get("issue_date") and (newest is None or b["issue_date"] > newest):
                newest = b["issue_date"]
            db.commit()                                     # fatura başına — yarıda kalırsa kaldığı yerden
        if newest is not None and (wm is None or newest > wm):
            _set_setting(db, SETTING_WATERMARK, newest.isoformat())
        _set_setting(db, SETTING_LAST_RUN, datetime.utcnow().isoformat(timespec="seconds"))
        db.commit()
        stats["watermark"] = newest.isoformat() if newest else None
        pending = db.query(ParasutPurchaseLine).filter(ParasutPurchaseLine.status == "pending").count()
        stats["pending"] = pending
        if notify and stats["lines_new"]:
            from core.notifications import notify_parasut_queue
            notify_parasut_queue(pending, samples)
        return stats
    finally:
        _RUN_LOCK.release()


# ─── Kuyruk görünümü ─────────────────────────────────────────────────────────

def _dmy(d: Optional[date]) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


def net_unit_price(line: ParasutPurchaseLine, package_qty: float) -> Optional[float]:
    """Onay hesabı: birim fiyat × (1 − indirim) ÷ ambalaj (6 ondalık)."""
    if line.unit_price is None or not package_qty or package_qty <= 0:
        return None
    return round(float(line.unit_price) * (1.0 - float(line.discount_rate or 0.0)) / float(package_qty), 6)


def line_view(db: Session, ln: ParasutPurchaseLine, domain: str, *, index=None, sup_cache: Optional[dict] = None,
              pack_cache: Optional[dict] = None) -> dict:
    """Kuyruk satırı (uç yanıtı).  Öneri: senkronun kaydettiği kart aktif
    paneldeyse o; değilse (başka panel / kayıt yok) canlı hesaplanır."""
    b = ln.bill
    sug = None
    if ln.suggested_item_id:
        it = db.query(Item).filter(Item.id == ln.suggested_item_id, Item.domain == domain,
                                   Item.is_active == True).first()                         # noqa: E712
        if it is not None:
            sug = {"id": it.id, "name": it.name, "unit": it.unit or "", "category": it.category or "",
                   "score": ln.suggested_score}
    key = normalize(b.supplier_name if b else None)
    if sup_cache is not None and key in sup_cache:
        sup = sup_cache[key]
    else:
        sup = match_supplier(db, domain, b.supplier_name if b else None)
        if sup_cache is not None:
            sup_cache[key] = sup
    # Adaylar her zaman canlı (kart seçici için); öneri kayıtlı değilse canlı
    res = suggest_item(db, {"product_name": ln.product_name, "description": ln.description}, domain,
                       index=index, supplier_id=(sup or {}).get("id"))
    cands = res["candidates"]
    if sug is None:
        sug = res["suggested"]
    pack = None
    if ln.product_parasut_id:
        if pack_cache is not None and ln.product_parasut_id in pack_cache:
            pk = pack_cache[ln.product_parasut_id]
        else:
            pk = db.query(ParasutProductPack).filter(
                ParasutProductPack.product_parasut_id == ln.product_parasut_id).first()
            if pack_cache is not None:
                pack_cache[ln.product_parasut_id] = pk
        if pk is not None:
            pack = {"package_qty": pk.package_qty, "price_unit": pk.price_unit, "item_id": pk.item_id}
    net = None
    if pack and pack["package_qty"]:
        net = net_unit_price(ln, pack["package_qty"])
    return {
        "id": ln.id, "status": ln.status,
        "bill": {"id": b.id, "invoice_no": b.invoice_no, "issue_date": b.issue_date.isoformat() if b.issue_date else None,
                 "date_text": _dmy(b.issue_date), "supplier_name": b.supplier_name, "currency": b.currency,
                 "description": b.description, "expense_hint": bool(b.expense_hint),
                 "net_total": b.net_total} if b else None,
        "description": ln.description, "product_name": ln.product_name, "product_unit": ln.product_unit,
        "product_parasut_id": ln.product_parasut_id,
        "quantity": ln.quantity, "unit_price": ln.unit_price, "discount_rate": ln.discount_rate,
        "discount_pct": round(float(ln.discount_rate) * 100.0, 2) if ln.discount_rate else None,
        "vat_rate": ln.vat_rate, "net_total": ln.net_total, "currency": (b.currency if b else None),
        "expense_hint": bool(ln.expense_hint) or bool(b and b.expense_hint),
        "stale": bool(b and b.issue_date and (date.today() - b.issue_date).days > STALE_DAYS),
        "suggestion": sug, "candidates": cands,
        "supplier": sup, "pack": pack, "net_preview": net,
        "source_label": source_label(b) if b else None,
        "chosen_item_id": ln.chosen_item_id, "chosen_supplier_id": ln.chosen_supplier_id,
        "chosen_supplier_name": ln.chosen_supplier_name, "package_qty": ln.package_qty,
        "price_unit": ln.price_unit, "decided_by": ln.decided_by,
        "decided_at": ln.decided_at.isoformat(timespec="seconds") if ln.decided_at else None,
        "supplier_price_id": ln.supplier_price_id,
    }


def pending_lines(db: Session, limit: int = QUEUE_LIMIT) -> List[ParasutPurchaseLine]:
    """Bekleyen satırlar: gider ipucu olmayanlar önce, yeni fatura önce."""
    rows = (db.query(ParasutPurchaseLine).join(ParasutPurchaseBill)
            .filter(ParasutPurchaseLine.status == "pending")
            .order_by(ParasutPurchaseLine.expense_hint.asc(), ParasutPurchaseBill.expense_hint.asc(),
                      ParasutPurchaseBill.issue_date.desc(), ParasutPurchaseLine.id.asc())
            .limit(limit).all())
    return rows


def queue_view(db: Session, domain: str, limit: int = QUEUE_LIMIT) -> dict:
    rows = pending_lines(db, limit)
    total = db.query(ParasutPurchaseLine).filter(ParasutPurchaseLine.status == "pending").count()
    index = build_item_index(db, domain) if rows else []
    sup_cache: dict = {}
    pack_cache: dict = {}
    out = [line_view(db, ln, domain, index=index, sup_cache=sup_cache, pack_cache=pack_cache) for ln in rows]
    wm = get_watermark(db)
    return {"count": total, "shown": len(out), "pending": out,
            "configured": is_configured(), "enabled": get_enabled(db),
            "watermark": wm.isoformat() if wm else None,
            "last_run": _get_setting(db, SETTING_LAST_RUN)}


# ─── Onay / atlama ───────────────────────────────────────────────────────────

def source_label(bill: ParasutPurchaseBill) -> str:
    no = (bill.invoice_no or f"#{bill.parasut_id}").strip()
    return f"Paraşüt alış faturası {no} — {_dmy(bill.issue_date) or 'tarihsiz'}"[:120]


def _price_text(v: float) -> str:
    s = f"{v:,.4f}".rstrip("0").rstrip(".")
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


#: PostgreSQL kilit çakışması kodları: deadlock_detected, lock_not_available
LOCK_BUSY_PGCODES = frozenset({"40P01", "55P03"})
BUSY_TEXT = "Bu satır ya da kart şu an başka bir onayda — birkaç saniye sonra tekrar deneyin."


def is_lock_busy(exc: BaseException) -> bool:
    """SQLAlchemy `OperationalError`'ı bir kilit çakışması mı (kilitlenme /
    kilit alınamadı)?  Uç bunu 409 `busy`'ye çevirir, başka hata yükselir."""
    return getattr(getattr(exc, "orig", None), "pgcode", None) in LOCK_BUSY_PGCODES


def lock_for_approval(db: Session, item_ids, line_ids) -> None:
    """Toplu onay (kademe betiği) ÖNCESİ: tüm kartlar id sırasıyla, SONRA tüm
    satırlar id sırasıyla `FOR UPDATE`.  `approve_line` her çağrıda kart → satır
    kilitler; tek işlemde birden çok satırı onaylayan çağıran kilitleri tek tek
    alırsa, başka kartı tutup bu satırı bekleyen bir arayüz onayıyla kilitlenme
    döngüsü kurabilir (A: L1→X tutar; B: Y tutar, L1'i bekler; A: L2→Y ister).
    Kartların hepsi satırlardan önce alınınca arayüz onayı ya kartta bekler ya
    da çağıranın hiç istemeyeceği kartı tutar — döngü kurulamaz.  COMMIT ETMEZ."""
    db.flush()
    iids = sorted({int(i) for i in item_ids if i})
    lids = sorted({int(i) for i in line_ids if i})
    if iids:
        db.query(Item.id).filter(Item.id.in_(iids)).order_by(Item.id).with_for_update().all()
    if lids:
        (db.query(ParasutPurchaseLine.id).filter(ParasutPurchaseLine.id.in_(lids))
         .order_by(ParasutPurchaseLine.id).with_for_update().all())


def _remember_pack(db: Session, ln: ParasutPurchaseLine, pq: float, pu: str, item_id: int, actor: str) -> None:
    """Paraşüt ürünü → ambalaj hafızası; `product_parasut_id` tekil indeksine
    `ON CONFLICT DO UPDATE` (eşzamanlı iki onay IntegrityError vermez).  Oturumda
    yüklü eski nesne varsa bayatlatılır (aynı işlemde sonraki okuma tazesini görür)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.orm import util as orm_util
    vals = {"product_name": (ln.product_name or ln.description or "")[:150] or None, "package_qty": pq,
            "price_unit": pu, "item_id": item_id, "updated_by": actor[:100], "updated_at": datetime.utcnow()}
    stmt = pg_insert(ParasutProductPack.__table__).values(product_parasut_id=ln.product_parasut_id, **vals)
    stmt = stmt.on_conflict_do_update(index_elements=["product_parasut_id"], set_=vals)
    pk_id = db.execute(stmt.returning(ParasutProductPack.__table__.c.id)).scalar()
    obj = db.identity_map.get(orm_util.identity_key(ParasutProductPack, pk_id))
    if obj is not None:
        db.expire(obj)


def approve_line(db: Session, line_id: int, *, item_id: int, package_qty, price_unit: str, domain: str,
                 actor: str, supplier_id: Optional[int] = None, supplier_name: Optional[str] = None) -> dict:
    """Bekleyen satırı onaylar → `supplier_prices` satırı (source='parasut').

    Hesap: net = unit_price × (1 − discount_rate) ÷ package_qty; para birimi
    faturadan; `quoted_at` fatura tarihi; `vat_included=False` + satır KDV'si.
    Firma: `supplier_id` (aktif, panel içi) ya da serbest metin `supplier_name`;
    ikisi de yoksa faturanın firması serbest metin olarak yazılır.
    (kart, firma) başına TEK parasut satırı: mevcut satır daha yeni faturadansa
    EZİLMEZ (`kept_newer`), değilse yerinde güncellenir.  Aynı firmanın elle
    (`manual`) satırı varsa `ApproveError('manual_exists')` (409).  Ambalaj
    Paraşüt ürünü başına hatırlanır.  COMMIT ETMEZ (flush) — çağıran tek
    işlemde commit eder; hata olursa hiçbir şey yazılmaz.
    Eşzamanlılık: kilit sırası kart → satır (`FOR UPDATE`, kodun kart → …
    düzeni).  İki sekme / iki kullanıcı / kademe betiği aynı satırı ya da aynı
    (kart, firma)'yı aynı anda onaylarsa ikincisi birincinin commit'ini bekler,
    sonra satırı "karara bağlanmış" (409) görür ya da mevcut parasut satırını
    yerinde günceller — çift `supplier_prices` satırı oluşmaz.  Ambalaj hafızası
    `ON CONFLICT` ile yazılır (aynı Paraşüt ürünü iki farklı karta aynı anda
    onaylanırsa tekil indeks 500 vermez).  Tek işlemde BİRDEN ÇOK satır
    onaylayan çağıran önce `lock_for_approval` çağırmalı (kilitlenme döngüsü).
    Döner: {line_id, supplier_price_id, price: serialize_price, kept_newer, net_unit_price, audit}."""
    db.flush()                                 # bekleyen değişiklik populate_existing ile ezilmesin
    db.query(Item).filter(Item.id == int(item_id)).with_for_update().first()
    ln = (db.query(ParasutPurchaseLine).filter(ParasutPurchaseLine.id == int(line_id))
          .with_for_update().populate_existing().first())
    if ln is None:
        raise ApproveError("not_found", "Fatura satırı bulunamadı.", 404)
    if ln.status != "pending":
        raise ApproveError("not_pending", "Bu satır zaten karara bağlanmış.", 409)
    bill = ln.bill
    if ln.unit_price is None or float(ln.unit_price) <= 0:
        raise ApproveError("no_price", "Satırın birim fiyatı yok — onaylanamaz.")
    item = (db.query(Item).filter(Item.id == int(item_id), Item.domain == domain, Item.is_active == True)   # noqa: E712
            .first())
    if item is None or item.category == "Bitmiş Ürün":
        raise ApproveError("item_not_found", "Malzeme bu panelde bulunamadı.", 404)
    pq = _num(package_qty)
    if pq is None or pq <= 0:
        raise ApproveError("bad_package", "Ambalaj miktarı sıfırdan büyük olmalı (ör. 25 kg'lık bidon → 25).")
    pu = (price_unit or "").strip().lower()
    if pu not in PRICE_UNITS or not price_unit_ok(item.unit, pu):
        fam = "adet" if default_price_unit(item.unit) == "adet" else "kg ya da l"
        raise ApproveError("bad_unit", f"«{item.name}» ({item.unit or 'birimsiz'}) için fiyat birimi {fam} olmalı.")
    cur = currency_code(bill.currency if bill else None)
    if cur not in CURRENCIES:
        raise ApproveError("bad_currency", f"Fatura para birimi ({bill.currency if bill else '—'}) desteklenmiyor.")
    sup = None
    if supplier_id is not None:
        sup = db.query(Supplier).filter(Supplier.id == int(supplier_id), Supplier.domain == domain).first()
        if sup is None:
            raise ApproveError("supplier_not_found", "Tedarikçi bu panelde bulunamadı.", 404)
        if sup.is_active is False:
            raise ApproveError("supplier_inactive", f"«{sup.name}» pasif tedarikçi — fiyat girilemez.")
        name = sup.name
    else:
        name = clean_name(supplier_name) or clean_name(bill.supplier_name if bill else None)
        if not name:
            raise ApproveError("supplier_not_found", "Tedarikçi adı boş.")
    keys = firm_keys(sup.id if sup else None, name)
    rows = (db.query(SupplierPrice).filter(SupplierPrice.item_id == item.id, SupplierPrice.domain == domain)
            .order_by(SupplierPrice.id.asc()).all())
    for r in rows:
        if r.source == SOURCE_MANUAL and firm_keys(r.supplier_id, r.supplier_name) & keys:
            raise ApproveError("manual_exists",
                               f"«{item.name}» için «{name}» elle girilmiş fiyatı var — faturadan yazılmadı; "
                               f"elle satırı düzenleyin.", 409)
    existing = next((r for r in rows if r.source == SOURCE_PARASUT and firm_keys(r.supplier_id, r.supplier_name) & keys),
                    None)
    net = net_unit_price(ln, pq)
    issue = bill.issue_date if bill else None
    label = source_label(bill)
    disc = f", indirim %{_price_text(float(ln.discount_rate) * 100.0)}" if ln.discount_rate else ""
    note = (f"{label}: {_price_text(float(ln.quantity or 0))} {ln.product_unit or 'adet'} × "
            f"{_price_text(float(ln.unit_price))} {cur}{disc} ÷ {_price_text(pq)} {pu} ambalaj · "
            f"KDV %{_price_text(float(ln.vat_rate or 0))} hariç · {ln.product_name or ln.description or ''}")[:2000]
    kept_newer = False
    if existing is not None and existing.quoted_at and issue and existing.quoted_at > issue:
        kept_newer = True                      # daha yeni faturanın satırı duruyor — eski fatura ezmez
        sp = existing
    else:
        sp = existing or SupplierPrice(item_id=item.id, domain=domain, source=SOURCE_PARASUT, created_by=actor[:100])
        sp.supplier_id = sup.id if sup else None
        sp.supplier_name = name[:150]
        sp.package_size = pq
        sp.unit_price = net
        sp.currency = cur
        sp.price_unit = pu
        sp.source = SOURCE_PARASUT
        sp.source_label = label
        sp.quoted_at = issue
        sp.note = note
        sp.vat_included = False
        sp.vat_rate = _num(ln.vat_rate)
        sp.updated_by = actor[:100]
        if existing is None:
            db.add(sp)
    db.flush()
    if ln.product_parasut_id:
        _remember_pack(db, ln, pq, pu, item.id, actor)
    ln.status = "approved"
    ln.chosen_item_id = item.id
    ln.chosen_supplier_id = sup.id if sup else None
    ln.chosen_supplier_name = name[:150]
    ln.package_qty, ln.price_unit = pq, pu
    ln.decided_by, ln.decided_at = actor[:100], datetime.utcnow()
    ln.decision_note = ("mevcut daha yeni fatura fiyatı korundu" if kept_newer else None)
    ln.supplier_price_id = sp.id
    db.flush()
    return {
        "line_id": ln.id, "supplier_price_id": sp.id, "kept_newer": kept_newer, "net_unit_price": net,
        "price": serialize_price(sp, item, supplier_status=(sup.purchase_status if sup else None)),
        "audit": {"line_id": ln.id, "bill_id": bill.id if bill else None, "invoice_no": bill.invoice_no if bill else None,
                  "issue_date": issue.isoformat() if issue else None, "item_id": item.id, "item": item.name,
                  "supplier_id": sup.id if sup else None, "supplier_name": name, "package_qty": pq,
                  "price_unit": pu, "unit_price": net, "currency": cur, "vat_rate": _num(ln.vat_rate),
                  "kept_newer": kept_newer, "domain": domain},
    }


def skip_line(db: Session, line_id: int, *, actor: str, reason: Optional[str] = None) -> dict:
    """Satırı atlar (fiyat yazılmaz).  COMMIT ETMEZ.  Satır `FOR UPDATE`
    kilitlenir: eşzamanlı onay commit ettiyse atlama 409 `not_pending` alır
    (onaylanmış satırın üstüne "atlandı" yazılmaz)."""
    db.flush()
    ln = (db.query(ParasutPurchaseLine).filter(ParasutPurchaseLine.id == int(line_id))
          .with_for_update().populate_existing().first())
    if ln is None:
        raise ApproveError("not_found", "Fatura satırı bulunamadı.", 404)
    if ln.status != "pending":
        raise ApproveError("not_pending", "Bu satır zaten karara bağlanmış.", 409)
    ln.status = "skipped"
    ln.decided_by, ln.decided_at = actor[:100], datetime.utcnow()
    ln.decision_note = (clean_name(reason) or "")[:255] or None
    db.flush()
    return {"line_id": ln.id, "status": ln.status}
