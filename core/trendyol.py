# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Trendyol pazaryeri — satıcı API'sinden YALNIZ OKUMA senkronu (Faz 1).

Tek satıcı hesabı (tüm markalar).  Siparişler, cari hesap ekstresi (hakediş +
diğer finansal kalemler), kargo faturası kalemleri ve onaylı ürün kataloğu
(stok + fiyat) çekilip `trendyol_*` tablolarına yazılır.  Trendyol'a HİÇBİR şey
yazılmaz: `Api` sınıfının yalnız `get` metodu var (testler kaynakta
post/put/delete çağrısı olmadığını kilitler).  IMS stoğu ve defter
(Transaction) bu modülden etkilenmez.

Yapılandırma çağrı anında env'den okunur (Shopify/Kommo kalıbı) — eksikse modül
import edilebilir kalır, `is_configured()` False döner, zamanlayıcı işi
kaydedilmez:
    TRENDYOL_SELLER_ID · TRENDYOL_API_KEY · TRENDYOL_API_SECRET
    (isteğe bağlı) TRENDYOL_BASE_URL · TRENDYOL_STOREFRONT_CODE
Kimlik bilgileri DB'ye yazılmaz, loglanmaz, hata mesajına girmez.

Uçlar (developers.trendyol.com, 13.10.2026; eski /orders ve Product V1
15.10.2026'da kapanıyor — kullanılmaz):
  • /order/sellers/{id}/orders/stream — lastModified penceresi ≤14 gün, veri
    ≤3 ay, size ≤200, nextCursor; çağrılar arası ≥5 sn; cursor ortasında süzgeç
    değişirse 400.
  • /finance/che/sellers/{id}/settlements | /otherfinancials — pencere ≤15 gün,
    size 500|1000.  Yanıttaki transactionType Türkçe ETİKET döndüğü için her tür
    AYRI istenir; kova eşlemesi istenen enum'dan (`type_param`) yapılır.
  • /finance/che/sellers/{id}/cargo-invoice/{seri}/items — seri, etiketi "Kargo
    Fatura(sı)" olan DeductionInvoices satırının id'si.
  • /product/sellers/{id}/products/approved (başlık/marka/KDV/durum) +
    /products/approved/inventory-and-price (stok + fiyat).

Zaman: Trendyol epoch ms döner ("GMT+3" notuyla).  Tek dönüşüm noktası
`ty_ts`/`to_ty_ms`; `TS_TR_WALLCLOCK` kalibrasyonda bir siparişin panel saatiyle
karşılaştırılarak belirlenir.  Sözleşmenin tamamı: CLAUDE.md "Trendyol".
"""
import json
import logging
import os
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import httpx
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from core.supplier_prices import normalize as _fold
from database import (AppSetting, Item, SessionLocal, TR_OFFSET, TrendyolBarcodeMap,
                      TrendyolCargoItem, TrendyolFinRow, TrendyolLine, TrendyolPackage,
                      TrendyolProduct, TrendyolSyncState, to_tr)

logger = logging.getLogger("minerva108.trendyol")

DEFAULT_BASE_URL = "https://apigw.trendyol.com/integration"
REQUIRED_ENV = ("TRENDYOL_SELLER_ID", "TRENDYOL_API_KEY", "TRENDYOL_API_SECRET")

#: Kalibrasyon: Trendyol epoch ms'i TR duvar saatini UTC'ymiş gibi mi kodluyor?
#  False = standart epoch (gerçek an).  Bir siparişin saati panelle
#  karşılaştırılıp gerekirse True yapılır (CLAUDE.md kalibrasyon adımı).
TS_TR_WALLCLOCK = False

# Grup başına çağrılar arası en az bekleme (sn) — stream ≥5 sn şartı.
PACING = {"orders": 5.0, "finance": 0.7, "products": 0.35}
RETRY_429 = (10, 30, 60)
RETRY_5XX = (2, 5, 15)
MAX_RETRY_AFTER = 120

ORDERS_WINDOW = timedelta(days=13)      # sınır 14 gün
ORDERS_LOOKBACK = timedelta(days=89)    # stream verisi ~3 ay
ORDERS_OVERLAP = timedelta(hours=6)     # GMT+3 belirsizliği + geç düşen değişiklikler
STREAM_PAGE = 200
MAX_PAGES = 500                          # sonsuz döngü bekçisi (pencere/tür başına)

FIN_WINDOW = timedelta(days=14)         # sınır 15 gün
FIN_REPULL_DAYS = 45                     # günlük çalıştırma: veri gecikmeli gelir
FIN_BACKFILL_DAYS = 90                   # ilk çalıştırma
FIN_BACKFILL_MAX = 365
FIN_PAGE = 1000
CARGO_PAGE = 500
CARGO_BATCH = 300                        # çalıştırma başına en fazla kargo faturası
PRODUCT_PAGE = 100
PRODUCT_TOKEN_AFTER = 10_000             # page × size sınırı → nextPageToken

SETTLEMENT_TYPES = (
    "Sale", "Return", "Discount", "DiscountCancel", "Coupon", "CouponCancel",
    "ProvisionPositive", "ProvisionNegative", "ManualRefund", "ManualRefundCancel",
    "TyDiscount", "TyDiscountCancel", "TyCoupon", "TyCouponCancel",
    "SellerRevenuePositive", "SellerRevenueNegative", "CommissionPositive", "CommissionNegative",
    "SellerRevenuePositiveCancel", "SellerRevenueNegativeCancel",
    "CommissionPositiveCancel", "CommissionNegativeCancel",
    "DeliveryFee", "DeliveryFeeCancel", "PayByLink",
)
OTHER_TYPES = (
    "Stoppage", "CashAdvance", "WireTransfer", "IncomingTransfer", "ReturnInvoice",
    "CommissionAgreementInvoice", "PaymentOrder", "DeductionInvoices", "FinancialItem",
)
PLATFORM_FEE = "PlatformServiceFee"
#: Bölünen paketin aslı — saklanır ama hiçbir toplama/listeye girmez (çocuk
#  paketler ayrı shipmentPackageId ile gelir; ikisini saymak çift sayımdır).
UNPACKED = "UnPacked"

STREAMS = ("orders", "finance", "catalog")
KINDS = STREAMS + ("all",)
STREAM_LABELS = {"orders": "Siparişler", "finance": "Finans (hakediş + kargo)", "catalog": "Ürün kataloğu"}
SCHEDULE_TEXT = {"orders": "30 dakikada bir", "finance": "Her gün 05:23", "catalog": "Saatte bir"}

STATUS_LABELS = {
    "Awaiting": "Ödeme bekleniyor", "Verified": "Ödeme doğrulandı", "Created": "Yeni",
    "Picking": "Hazırlanıyor", "Invoiced": "Faturalandı", "Shipped": "Kargoda",
    "AtCollectionPoint": "Teslim noktasında", "Delivered": "Teslim edildi",
    "UnDelivered": "Teslim edilemedi", "Returned": "İade edildi", "Cancelled": "İptal",
    "UnSupplied": "Tedarik edilemedi", "UnPacked": "Bölündü (aslı)",
}

#: Bildirim politikası — üst üste kaç hatada (kimlik hatası hep ilkinde) ve
#  en sık ne kadarda bir.
NOTIFY_AFTER = {"orders": 3, "catalog": 3, "finance": 1}
NOTIFY_EVERY = timedelta(hours=24)

ALIAS_FILE = Path(__file__).resolve().parent.parent / "data" / "barcode_aliases.json"
FINISHED = "Bitmiş Ürün"


class TrendyolError(RuntimeError):
    """Trendyol API'si / taşıma katmanı hatası — mesaj kullanıcıya gösterilebilir,
    kimlik bilgisi içermez."""


class TrendyolNotConfigured(TrendyolError):
    pass


class TrendyolAuthError(TrendyolError):
    pass


class TrendyolBadRequest(TrendyolError):
    """4xx (401/403/408/429 hariç) — `status` hangi kod olduğunu taşır."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class TrendyolStopped(TrendyolError):
    """Uygulama kapanıyor — süren senkron bir sonraki istekte/beklemede durdu.
    Hata SAYILMAZ (ardışık hata sayacı artmaz, bildirim gitmez)."""


STOP_MSG = "Uygulama kapanıyor — senkron durduruldu (yeniden başlayınca kaldığı yerden sürer)."

#: Kapanış bayrağı.  api_main shutdown'ı `request_stop()` çağırır: süren senkron
#  (elle iş parçacığı ya da zamanlayıcı) en geç bir istek/bekleme sonunda durur,
#  böylece deploy'daki yeniden başlatma uzun bir geçmiş çekimini BEKLEMEZ.
#  Watermark yalnız tamamlanan pencereyle ilerlediği için yarıda kalan iş
#  sonraki çalıştırmada kaldığı yerden sürer.
_STOP = threading.Event()


def request_stop() -> None:
    _STOP.set()


def reset_stop() -> None:
    _STOP.clear()


def _interruptible_sleep(seconds: float) -> None:
    if _STOP.wait(max(0.0, seconds)):
        raise TrendyolStopped(STOP_MSG)


# ─── Yapılandırma (çağrı anında env) ─────────────────────────────────────────

def _env(name: str) -> str:
    return (os.getenv(name, "") or "").strip()


def config_problems() -> List[str]:
    """Eksik/hatalı ayarların ADLARI — değer asla dönmez."""
    out = [n for n in REQUIRED_ENV if not _env(n)]
    sid = _env("TRENDYOL_SELLER_ID")
    if sid and not sid.isdigit():
        out.append("TRENDYOL_SELLER_ID (yalnız rakam olmalı)")
    base = _env("TRENDYOL_BASE_URL")
    if base and not base.startswith("https://"):
        out.append("TRENDYOL_BASE_URL (https:// ile başlamalı)")
    return out


def config() -> Optional[dict]:
    if config_problems():
        return None
    return {"seller_id": _env("TRENDYOL_SELLER_ID"),
            "api_key": _env("TRENDYOL_API_KEY"),
            "api_secret": _env("TRENDYOL_API_SECRET"),
            "base_url": (_env("TRENDYOL_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            "storefront": _env("TRENDYOL_STOREFRONT_CODE")}


def is_configured() -> bool:
    return not config_problems()


# ─── AppSetting (otomatik senkron anahtarı) ──────────────────────────────────

def _get_setting(db: Session, key: str) -> Optional[str]:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row else None


def _set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


def get_enabled(db: Session) -> bool:
    """Zamanlayıcı senkronu açık mı (deploysuz duraklatma; elle senkron hep açık)."""
    return (_get_setting(db, "trendyol.enabled") or "true").lower() != "false"


def set_enabled(db: Session, enabled: bool) -> None:
    _set_setting(db, "trendyol.enabled", "true" if enabled else "false")
    db.commit()


# ─── Zaman ───────────────────────────────────────────────────────────────────

_EPOCH = datetime(1970, 1, 1)


def ty_ts(ms) -> Optional[datetime]:
    """Trendyol epoch ms → naive UTC datetime (DB sözleşmesi)."""
    try:
        v = int(ms)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    dt = _EPOCH + timedelta(milliseconds=v)
    return dt - TR_OFFSET if TS_TR_WALLCLOCK else dt


def to_ty_ms(dt: datetime) -> int:
    """naive UTC datetime → Trendyol epoch ms."""
    if TS_TR_WALLCLOCK:
        dt = dt + TR_OFFSET
    return int((dt - _EPOCH) / timedelta(milliseconds=1))


def _fmt(dt: Optional[datetime]) -> Optional[str]:
    return to_tr(dt).strftime("%d.%m.%Y %H:%M") if dt else None


# ─── HTTP istemci — YALNIZ GET ───────────────────────────────────────────────

def _retry_after(r: httpx.Response) -> Optional[float]:
    try:
        v = float(r.headers.get("Retry-After", ""))
    except ValueError:
        return None
    return min(max(v, 1.0), MAX_RETRY_AFTER)


def _body_snippet(r: httpx.Response) -> str:
    try:
        return " ".join((r.text or "").split())[:300]
    except Exception:
        return ""


class Api:
    """Trendyol satıcı API istemcisi.  Yalnız `get` vardır (okuma-yalnızlık kod
    yapısıyla).  Grup başına bekleme, 429/5xx için sınırlı tekrar; 401/403/400
    anlaşılır hatalara çevrilir.  Kimlik bilgisi yalnız httpx Basic auth'ta
    durur — hiçbir mesaja, log'a girmez.  Testler `transport=httpx.MockTransport`
    ve `sleep=` enjekte eder (ağ yok, bekleme yok)."""

    def __init__(self, cfg: Optional[dict] = None, *, transport=None,
                 sleep: Optional[Callable[[float], None]] = None,
                 clock: Callable[[], float] = time.monotonic):
        cfg = cfg or config()
        if not cfg:
            raise TrendyolNotConfigured(
                "Trendyol yapılandırılmamış — eksik: " + ", ".join(config_problems()))
        self.seller_id = cfg["seller_id"]
        headers = {"User-Agent": f"{self.seller_id} - SelfIntegration",
                   "Accept": "application/json"}
        if cfg.get("storefront"):
            headers["storeFrontCode"] = cfg["storefront"]
        self._http = httpx.Client(base_url=cfg["base_url"], auth=(cfg["api_key"], cfg["api_secret"]),
                                  headers=headers, timeout=httpx.Timeout(30.0, connect=10.0),
                                  follow_redirects=False, transport=transport)
        self._sleep = sleep or _interruptible_sleep      # kapanışta beklemeyi keser
        self._clock = clock
        self._last: Dict[str, float] = {}
        self.calls = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _pace(self, group: str) -> None:
        last = self._last.get(group)
        if last is None:
            return
        wait = PACING.get(group, 0.0) - (self._clock() - last)
        if wait > 0:
            self._sleep(wait)

    def get(self, group: str, path: str, params: Optional[dict] = None):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        n429 = n5xx = 0
        while True:
            if _STOP.is_set():
                raise TrendyolStopped(STOP_MSG)
            self._pace(group)
            err = None
            try:
                r = self._http.get(path, params=params)
            except httpx.TimeoutException:
                err = "zaman aşımı"
            except httpx.TransportError as e:
                err = type(e).__name__
            self._last[group] = self._clock()
            if err is not None:
                if n5xx < len(RETRY_5XX):
                    self._sleep(RETRY_5XX[n5xx])
                    n5xx += 1
                    continue
                raise TrendyolError(f"Trendyol'a ulaşılamadı ({err}).")
            self.calls += 1
            sc = r.status_code
            if sc == 429:
                if n429 < len(RETRY_429):
                    self._sleep(_retry_after(r) or RETRY_429[n429])
                    n429 += 1
                    continue
                raise TrendyolError("Trendyol istek sınırı aşıldı (429) — biraz sonra yeniden deneyin.")
            if sc >= 500 or sc == 408:
                if n5xx < len(RETRY_5XX):
                    self._sleep(RETRY_5XX[n5xx])
                    n5xx += 1
                    continue
                raise TrendyolError(f"Trendyol sunucu hatası ({sc}) — sonra yeniden denenecek.")
            if sc == 401:
                raise TrendyolAuthError(
                    "Trendyol kimlik doğrulaması başarısız (401) — API Key / API Secret'ı kontrol "
                    "edin (canlı ve test ortamı anahtarları farklıdır).")
            if sc == 403:
                raise TrendyolAuthError(
                    "Trendyol isteği reddetti (403) — satıcı ID'si anahtarlarla aynı hesaba ait "
                    "olmalı (User-Agent: \"<satıcı id> - SelfIntegration\").")
            if sc >= 400:
                raise TrendyolBadRequest(f"Trendyol isteği kabul etmedi ({sc}): {_body_snippet(r)}", status=sc)
            if not 200 <= sc < 300:            # 3xx: yönlendirme izlenmez — boş başarı SAYILMAZ
                raise TrendyolError(f"Trendyol beklenmeyen yanıt döndü ({sc}).")
            if sc == 204 or not r.content:
                return {}
            try:
                data = r.json()
            except ValueError:
                raise TrendyolError("Trendyol geçersiz yanıt döndü (JSON değil).") from None
            return data if isinstance(data, (dict, list)) else {}


def make_api() -> Api:
    """Varsayılan istemci — testler bunu sahte istemciyle değiştirir."""
    return Api()


# ─── Alan okuma (yeni ad önce, Nisan 2026 öncesi ad yedek) ───────────────────

def _pick(d, *keys):
    if not isinstance(d, dict):
        return None
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return None


def _num(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _int(v) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        f = _num(v)
        return int(f) if f is not None else None


def _txt(v, n: int) -> Optional[str]:
    if v is None or isinstance(v, (dict, list)):
        return None
    s = " ".join(str(v).split())
    return s[:n] if s else None


def _bool(v) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "evet")
    return bool(v)


def _r(v: Optional[float]) -> Optional[float]:
    return round(v, 4) if v is not None else None


def parse_line(raw: dict) -> Optional[dict]:
    """Paket satırı → beyaz listeli sözlük.  Trendyol tutarları BİRİM verir;
    `discountDetails` (birim başına kayıt) varsa toplamlar onlardan kurulur."""
    line_id = _int(_pick(raw, "lineId", "id"))
    if line_id is None:
        return None
    qty = max(1, _int(raw.get("quantity")) or 1)
    unit_gross = _num(raw.get("lineGrossAmount"))
    unit_seller = _num(raw.get("lineSellerDiscount"))
    unit_ty = _num(raw.get("lineTyDiscount"))
    unit_price = _num(_pick(raw, "lineUnitPrice", "price"))
    gross = seller = ty = net = None
    details = [d for d in (raw.get("discountDetails") or []) if isinstance(d, dict)]
    prices = [_num(d.get("lineItemPrice")) for d in details]
    if details and all(p is not None for p in prices):
        net = sum(prices)
        seller = sum(_num(d.get("lineItemSellerDiscount")) or 0.0 for d in details)
        ty = sum(_num(d.get("lineItemTyDiscount")) or 0.0 for d in details)
        gross = net + seller + ty
    else:
        if unit_gross is None and unit_price is not None:
            unit_gross = unit_price + (unit_seller or 0.0) + (unit_ty or 0.0)
        seller = (unit_seller or 0.0) * qty
        ty = (unit_ty or 0.0) * qty
        gross = unit_gross * qty if unit_gross is not None else None
        if unit_price is not None:
            net = unit_price * qty
        elif gross is not None:
            net = gross - seller - ty
    return {
        "line_id": line_id,
        "barcode": _txt(raw.get("barcode"), 64),
        "stock_code": _txt(_pick(raw, "stockCode", "merchantSku"), 100),
        "product_name": _txt(raw.get("productName"), 300),
        "content_id": _int(_pick(raw, "contentId", "productCode")),
        "quantity": qty,
        "unit_gross": _r(unit_gross),
        "unit_price": _r(unit_price),
        "gross_total": _r(gross),
        "seller_discount_total": _r(seller),
        "ty_discount_total": _r(ty),
        "net_total": _r(net),
        "vat_rate": _num(_pick(raw, "vatRate", "vatBaseAmount")),
        "commission_rate": _num(raw.get("commission")),
        "line_status": _txt(raw.get("orderLineItemStatusName"), 30),
        "currency": _txt(raw.get("currencyCode"), 8),
    }


def parse_package(raw: dict) -> Optional[dict]:
    """Paket → beyaz listeli sözlük.  Adres, alıcı adı, telefon, vergi no ve
    fatura bağlantısı bilerek ALINMAZ (KVKK)."""
    pid = _int(_pick(raw, "shipmentPackageId", "id"))
    order_no = _txt(raw.get("orderNumber"), 40)
    if not pid or not order_no:
        return None
    lm = _int(raw.get("lastModifiedDate"))
    origin = raw.get("originPackageIds")
    if isinstance(origin, list):
        origin_txt = ",".join(str(x) for x in (_int(o) for o in origin) if x is not None)[:255] or None
    else:
        origin_txt = _txt(origin, 255)
    currency = _txt(raw.get("currencyCode"), 8)
    lines = [ln for ln in (parse_line(x) for x in (raw.get("lines") or []) if isinstance(x, dict)) if ln]
    for ln in lines:
        ln["currency"] = ln["currency"] or currency
    return {
        "package_id": pid,
        "order_number": order_no,
        "status": _txt(_pick(raw, "shipmentPackageStatus", "status"), 30),
        "order_date": ty_ts(raw.get("orderDate")),
        "last_modified_ms": lm,
        "last_modified_at": ty_ts(lm),
        "gross_amount": _num(_pick(raw, "packageGrossAmount", "grossAmount")),
        "seller_discount": _num(_pick(raw, "packageSellerDiscount", "totalDiscount")),
        "ty_discount": _num(_pick(raw, "packageTyDiscount", "totalTyDiscount")),
        "total_price": _num(_pick(raw, "packageTotalPrice", "totalPrice")),
        "currency": currency,
        "cargo_provider": _txt(raw.get("cargoProviderName"), 80),
        "cargo_tracking": _txt(raw.get("cargoTrackingNumber"), 64),
        "origin_package_ids": origin_txt,
        "cancelled_by": _txt(raw.get("cancelledBy"), 40),
        "cancel_reason": _txt(_pick(raw, "cancelReason", "cancelReasonCode"), 255),
        "lines": lines,
    }


def parse_fin_row(raw: dict, source: str, type_param: str, sub_type: Optional[str] = None) -> Optional[dict]:
    row_id = _txt(raw.get("id"), 64)
    if not row_id:
        return None
    return {
        "source": source, "row_id": row_id, "type_param": type_param, "sub_type": sub_type,
        "type_label": _txt(raw.get("transactionType"), 80),
        "description": _txt(raw.get("description"), 255),
        "transaction_date": ty_ts(raw.get("transactionDate")),
        "order_number": _txt(raw.get("orderNumber"), 40),
        "package_id": _int(raw.get("shipmentPackageId")),
        "barcode": _txt(raw.get("barcode"), 64),
        "debt": _num(raw.get("debt")),
        "credit": _num(raw.get("credit")),
        "commission_rate": _num(raw.get("commissionRate")),
        "commission_amount": _num(raw.get("commissionAmount")),
        "seller_revenue": _num(raw.get("sellerRevenue")),
        "receipt_id": _int(raw.get("receiptId")),
        "payment_order_id": _int(raw.get("paymentOrderId")),
        "payment_date": ty_ts(raw.get("paymentDate")),
        "order_date": ty_ts(raw.get("orderDate")),
        "commission_invoice_serial": _txt(raw.get("commissionInvoiceSerialNumber"), 64),
    }


def parse_cargo_item(raw: dict) -> dict:
    kind = _txt(raw.get("shipmentPackageType"), 60)
    return {
        "parcel_unique_id": _txt(raw.get("parcelUniqueId"), 40),
        "order_number": _txt(raw.get("orderNumber"), 40),
        "kind": kind,
        "is_return": "iade" in _fold(kind or ""),
        "amount": _num(raw.get("amount")),
        "desi": _num(raw.get("desi")),
    }


def is_cargo_invoice_label(*texts) -> bool:
    return "kargo fatura" in _fold(" ".join(t for t in texts if t))


# ─── Upsert (idempotent) ─────────────────────────────────────────────────────

_PKG_FIELDS = ("order_number", "status", "order_date", "last_modified_ms", "last_modified_at",
               "gross_amount", "seller_discount", "ty_discount", "total_price", "currency",
               "cargo_provider", "cargo_tracking", "origin_package_ids", "cancelled_by", "cancel_reason")
_LINE_FIELDS = ("barcode", "stock_code", "product_name", "content_id", "quantity", "unit_gross",
                "unit_price", "gross_total", "seller_discount_total", "ty_discount_total", "net_total",
                "vat_rate", "commission_rate", "line_status", "currency")
_FIN_FIELDS = ("type_label", "description", "transaction_date", "order_number", "package_id", "barcode",
               "debt", "credit", "commission_rate", "commission_amount", "seller_revenue", "receipt_id",
               "payment_order_id", "payment_date", "order_date", "commission_invoice_serial")


def upsert_package(db: Session, pk: dict) -> str:
    """'new' | 'updated' | 'stale'.  Eski lastModified yeniyi EZMEZ.  Satırlar
    yerinde güncellenir (aynı tekil anahtarla sil+ekle tek flush'ta çakışırdı)."""
    row = db.query(TrendyolPackage).filter(TrendyolPackage.package_id == pk["package_id"]).first()
    if (row is not None and row.last_modified_ms and pk["last_modified_ms"]
            and pk["last_modified_ms"] < row.last_modified_ms):
        return "stale"
    outcome = "updated"
    if row is None:
        row = TrendyolPackage(package_id=pk["package_id"], first_seen_at=datetime.utcnow())
        db.add(row)
        outcome = "new"
    for f in _PKG_FIELDS:
        setattr(row, f, pk[f])
    row.synced_at = datetime.utcnow()
    if pk["lines"]:                       # boş satır listesi = okuma sorunu; eldekini silme
        existing = {ln.line_id: ln for ln in row.lines}
        keep = set()
        for data in pk["lines"]:
            ln = existing.get(data["line_id"])
            if ln is None:
                ln = TrendyolLine(line_id=data["line_id"])
                row.lines.append(ln)
                existing[data["line_id"]] = ln
            for f in _LINE_FIELDS:
                setattr(ln, f, data[f])
            keep.add(data["line_id"])
        for lid, ln in list(existing.items()):
            if lid not in keep:
                row.lines.remove(ln)
    db.flush()
    return outcome


def upsert_fin_row(db: Session, fr: dict) -> str:
    """'new' | 'updated' | 'conflict'.  Aynı id başka bir türün sorgusundan da
    gelirse ilk kayıt OLDUĞU GİBİ korunur (tutarları ikinci türün değerleriyle
    ezilmez); çakışma sayısı akış özetinde görünür."""
    row = (db.query(TrendyolFinRow)
           .filter(TrendyolFinRow.source == fr["source"], TrendyolFinRow.row_id == fr["row_id"]).first())
    outcome = "updated"
    if row is None:
        row = TrendyolFinRow(source=fr["source"], row_id=fr["row_id"], type_param=fr["type_param"])
        db.add(row)
        outcome = "new"
    elif row.type_param != fr["type_param"]:
        return "conflict"
    for f in _FIN_FIELDS:
        setattr(row, f, fr[f])
    if fr.get("sub_type"):
        row.sub_type = fr["sub_type"]
    row.synced_at = datetime.utcnow()
    db.flush()
    return outcome


# ─── Durum kaydı + ilerleme ──────────────────────────────────────────────────

_RUN_LOCK = threading.Lock()
_PROGRESS_LOCK = threading.Lock()
_PROGRESS: dict = {"running": False}


def _progress(**kw) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS.update(kw)


def progress_snapshot() -> dict:
    with _PROGRESS_LOCK:
        d = dict(_PROGRESS)
    for k in ("started_at", "finished_at"):
        if isinstance(d.get(k), datetime):
            d[k] = _fmt(d[k])
    return d


def is_running() -> bool:
    return _RUN_LOCK.locked()


def _state(db: Session, stream: str) -> TrendyolSyncState:
    st = db.query(TrendyolSyncState).filter(TrendyolSyncState.stream == stream).first()
    if st is None:
        st = TrendyolSyncState(stream=stream, consecutive_failures=0)
        db.add(st)
        db.flush()
    return st


def _meta(st: Optional[TrendyolSyncState]) -> dict:
    if not st or not st.meta:
        return {}
    try:
        d = json.loads(st.meta)
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _err_text(e: Exception) -> str:
    if isinstance(e, TrendyolError):
        return str(e)[:480]
    return f"{type(e).__name__}: {str(e)[:300]}"


def _summary(stream: str, res: dict) -> str:
    if stream == "orders":
        return (f"{res.get('new', 0)} yeni, {res.get('updated', 0)} güncellenen paket "
                f"({res.get('windows', 0)} pencere)")
    if stream == "finance":
        c = res.get("cargo") or {}
        return (f"{res.get('new', 0)} yeni, {res.get('updated', 0)} güncellenen satır · "
                f"{c.get('invoices', 0)} kargo faturası ({res.get('days')} gün)")
    return (f"{res.get('products', 0)} ürün ({res.get('new', 0)} yeni) · "
            f"{res.get('with_stock', 0)} stoklu kayıt")


def _mark_ok(db: Session, stream: str, res: dict) -> None:
    st = _state(db, stream)
    st.last_ok_at = datetime.utcnow()
    st.consecutive_failures = 0
    st.last_notified_at = None
    st.last_error = None
    st.last_status = _summary(stream, res)[:255]
    meta = _meta(st)
    meta["last"] = res
    blob = json.dumps(meta, ensure_ascii=False, default=str)
    if len(blob) > 20000:          # kesmek geçersiz JSON üretirdi — ayrıntıyı at
        meta["last"] = {k: v for k, v in res.items() if not isinstance(v, (dict, list))}
        meta["last"]["truncated"] = True
        blob = json.dumps(meta, ensure_ascii=False, default=str)
    st.meta = blob
    db.commit()


def _mark_fail(db: Session, stream: str, e: Exception) -> None:
    st = _state(db, stream)
    st.consecutive_failures = (st.consecutive_failures or 0) + 1
    st.last_error = _err_text(e)[:500]
    st.last_status = ("HATA: " + _err_text(e))[:255]
    db.commit()


# ─── Senkron akışları ────────────────────────────────────────────────────────

def _windows_forward(start: datetime, end: datetime, size: timedelta) -> List[Tuple[datetime, datetime]]:
    out, ws = [], start
    while ws < end:
        we = min(ws + size, end)
        out.append((ws, we))
        ws = we
    return out


def _windows_back(start: datetime, end: datetime, size: timedelta) -> List[Tuple[datetime, datetime]]:
    """En yeniden eskiye — geçmiş sınırına çarpınca (400) geri yürüyüş durur."""
    out, we = [], end
    while we > start:
        ws = max(start, we - size)
        out.append((ws, we))
        we = ws
    return out


def _content(data) -> list:
    """Sayfalı yanıtın `content` listesi.  Boş gövde (204) boş sayfadır; ama
    `content` alanı olmayan bir gövde BOŞ BAŞARI SAYILMAZ — aksi hâlde pencere
    "tamamlandı" sayılıp watermark ilerler ve paketler sessizce kaçardı."""
    if isinstance(data, dict) and "content" in data:
        return [c for c in (data.get("content") or []) if isinstance(c, dict)]
    if data == {}:
        return []
    raise TrendyolError("Trendyol beklenmeyen yanıt biçimi döndürdü (content alanı yok).")


def sync_orders(db: Session, api, *, lookback_days: Optional[int] = None,
                now: Optional[datetime] = None) -> dict:
    """lastModified penceresi üzerinden artımlı sipariş senkronu.

    Başlangıç: watermark − 6 sa; watermark yoksa 89 gün geriye (stream verisi
    ~3 ay).  `lookback_days` başlangıcı yalnız DAHA GERİYE çeker — watermark'ın
    gerisinde kalan boşluğun üstünden ASLA atlatmaz (kısa bir "geçmişi çek"
    taranmamış aralığı kapsanmış saydırmasın).  13 günlük pencereler eskiden
    yeniye; her sayfadan sonra commit, watermark yalnız pencere TAMAMLANINCA ve
    yalnız ileri doğru ilerler → restart kaldığı yerden sürer, backfill
    watermark'ı geri sarmaz."""
    now = now or datetime.utcnow()
    st = _state(db, "orders")
    floor = now - ORDERS_LOOKBACK
    requested = now - timedelta(days=int(lookback_days)) if lookback_days else None
    if st.watermark:
        start = st.watermark - ORDERS_OVERLAP
        if requested is not None:
            start = min(start, requested)
    else:
        start = requested if requested is not None else floor
    start = max(start, floor)
    path = f"/order/sellers/{api.seller_id}/orders/stream"
    counts = Counter()
    for ws, we in _windows_forward(start, now, ORDERS_WINDOW):
        _progress(detail=f"Siparişler {_fmt(ws)} → {_fmt(we)}")
        cursor, seen_cursors = None, set()
        for _ in range(MAX_PAGES):
            params = {"supplierId": api.seller_id, "lastModifiedStartDate": to_ty_ms(ws),
                      "lastModifiedEndDate": to_ty_ms(we), "size": STREAM_PAGE}
            if cursor:
                params["nextCursor"] = cursor
            data = api.get("orders", path, params)
            for raw in _content(data):
                pk = parse_package(raw)
                if pk:
                    counts[upsert_package(db, pk)] += 1
                else:
                    counts["skipped"] += 1
            db.commit()
            counts["pages"] += 1
            nxt = data.get("nextCursor") if isinstance(data, dict) else None
            if not (isinstance(data, dict) and data.get("hasMore")) or not nxt:
                break
            if nxt in seen_cursors:
                raise TrendyolError("Trendyol sipariş akışı aynı imleci tekrar döndürdü — durduruldu.")
            seen_cursors.add(nxt)
            cursor = nxt
        else:
            raise TrendyolError("Sipariş akışında sayfa sınırı aşıldı — durduruldu.")
        st = _state(db, "orders")
        if st.watermark is None or we > st.watermark:
            st.watermark = we
        db.commit()
        counts["windows"] += 1
    return {"new": counts["new"], "updated": counts["updated"], "stale": counts["stale"],
            "skipped": counts["skipped"], "pages": counts["pages"], "windows": counts["windows"],
            "from": _fmt(start), "to": _fmt(now)}


def _pull_fin(db: Session, api, source: str, type_param: str, ws: datetime, we: datetime,
              sub_type: Optional[str] = None) -> Counter:
    kind = "settlements" if source == "settlement" else "otherfinancials"
    path = f"/finance/che/sellers/{api.seller_id}/{kind}"
    counts = Counter()
    for page in range(MAX_PAGES):
        params = {"supplierId": api.seller_id, "transactionType": type_param,
                  "startDate": to_ty_ms(ws), "endDate": to_ty_ms(we), "page": page, "size": FIN_PAGE}
        if sub_type:
            params["transactionSubType"] = sub_type
        data = api.get("finance", path, params)
        content = _content(data)
        for raw in content:
            fr = parse_fin_row(raw, source, type_param, sub_type)
            if fr:
                counts[upsert_fin_row(db, fr)] += 1
        db.commit()
        total_pages = _int(data.get("totalPages")) if isinstance(data, dict) else None
        if not content or total_pages is None or page + 1 >= total_pages:
            break
    return counts


def sync_finance(db: Session, api, *, days: Optional[int] = None,
                 now: Optional[datetime] = None) -> dict:
    """Cari hesap ekstresi: her settlement + otherfinancials türü AYRI, 14
    günlük pencerelerle EN YENİDEN eskiye.  Bir türün en yeni penceresi 400
    dönerse tür "kabul edilmedi" kaydedilir; daha eski pencere 400 dönerse o
    tür için geçmiş sınırına gelinmiştir.  Hiçbir tür kabul edilmezse akış
    HATA sayılır.  Ardından PlatformServiceFee etiketi + yeni kargo faturası
    kalemleri.

    Kapsam: ilk çalıştırma 90 gün; sonrası son BAŞARIDAN bu yana geçen gün +
    45 (uzun kesinti/duraklatma sonrası boşluk kalmasın — kesinti boyunca da
    kayıt oluşur).  Açıkça istenen gün sayısı bu boşluğun altına inemez."""
    now = now or datetime.utcnow()
    st = _state(db, "finance")
    gap = ((now - st.last_ok_at).days + FIN_REPULL_DAYS) if st.last_ok_at else None
    if days is None:
        days = max(FIN_REPULL_DAYS, gap) if gap is not None else FIN_BACKFILL_DAYS
    elif gap is not None:
        days = max(int(days), gap)
    days = max(1, min(int(days), FIN_BACKFILL_MAX))
    windows = _windows_back(now - timedelta(days=days), now, FIN_WINDOW)
    totals = Counter()
    rejected: Dict[str, str] = {}
    oldest_ok: Optional[datetime] = None
    all_types = [("settlement", t) for t in SETTLEMENT_TYPES] + [("other", t) for t in OTHER_TYPES]
    for source, t in all_types:
        for i, (ws, we) in enumerate(windows):
            _progress(detail=f"Finans · {t} · {_fmt(ws)} → {_fmt(we)}")
            try:
                c = _pull_fin(db, api, source, t, ws, we)
            except TrendyolBadRequest as e:
                db.rollback()
                if i == 0:
                    rejected[f"{source}:{t}"] = str(e)[:200]
                break
            totals.update(c)
            if oldest_ok is None or ws < oldest_ok:
                oldest_ok = ws
    if len(rejected) == len(all_types):
        first = next(iter(rejected.values()), "")
        raise TrendyolError(f"Finans uçları hiçbir işlem türünü kabul etmedi — {first}")
    fee = Counter()
    for ws, we in windows:
        _progress(detail=f"Finans · platform hizmet bedeli · {_fmt(ws)} → {_fmt(we)}")
        try:
            fee.update(_pull_fin(db, api, "other", "DeductionInvoices", ws, we, sub_type=PLATFORM_FEE))
        except TrendyolBadRequest as e:
            db.rollback()
            rejected[f"other:DeductionInvoices/{PLATFORM_FEE}"] = str(e)[:200]
            break
    cargo = sync_cargo_items(db, api, now=now)
    return {"days": days, "windows": len(windows), "new": totals["new"], "updated": totals["updated"],
            "conflict": totals["conflict"], "platform_fee_rows": fee["new"] + fee["updated"] + fee["conflict"],
            "rejected": rejected, "oldest_window": _fmt(oldest_ok), "cargo": cargo}


def _cargo_items(api, serial: str) -> Iterable[dict]:
    path = f"/finance/che/sellers/{api.seller_id}/cargo-invoice/{quote(serial, safe='')}/items"
    for page in range(MAX_PAGES):
        data = api.get("finance", path, {"page": page, "size": CARGO_PAGE})
        content = _content(data)
        for raw in content:
            yield parse_cargo_item(raw)
        total_pages = _int(data.get("totalPages")) if isinstance(data, dict) else None
        if not content or total_pages is None or page + 1 >= total_pages:
            break


def sync_cargo_items(db: Session, api, *, now: Optional[datetime] = None, limit: int = CARGO_BATCH) -> dict:
    """Kalemleri henüz çekilmemiş kargo faturaları → `trendyol_cargo_items`.
    Fatura başına toptan yenilenir.  Hata FATURA BAŞINA yalıtılır: kalıcı
    istemci hatası (400/404/422 — bu seri kalem vermiyor) damgalanıp atlanır;
    geçici hata (5xx, 429, ağ) yalnız o faturayı sonraki çalıştırmaya bırakır,
    diğer faturalar ve finans akışı sürer.  Kimlik hatası ve kapanış akışı
    durdurur."""
    now = now or datetime.utcnow()
    rows = (db.query(TrendyolFinRow)
            .filter(TrendyolFinRow.source == "other",
                    TrendyolFinRow.type_param == "DeductionInvoices",
                    TrendyolFinRow.detail_fetched_at.is_(None),
                    or_(TrendyolFinRow.type_label.ilike("%kargo fatura%"),
                        TrendyolFinRow.description.ilike("%kargo fatura%")))
            .order_by(TrendyolFinRow.transaction_date.desc())
            .limit(limit).all())
    out = {"invoices": 0, "items": 0, "errors": {}}
    for r in rows:
        if not is_cargo_invoice_label(r.type_label, r.description):
            continue
        serial = r.row_id
        _progress(detail=f"Kargo faturası {serial}")
        try:
            items = list(_cargo_items(api, serial))
        except (TrendyolAuthError, TrendyolStopped):
            db.rollback()
            raise
        except TrendyolError as e:
            db.rollback()
            permanent = isinstance(e, TrendyolBadRequest) and e.status in (400, 404, 422)
            if permanent:
                r = db.query(TrendyolFinRow).filter(TrendyolFinRow.id == r.id).one()
                r.detail_fetched_at = now
                db.commit()
            if len(out["errors"]) < 50:
                out["errors"][serial] = ("atlandı: " if permanent else "yeniden denenecek: ") + str(e)[:160]
            continue
        db.query(TrendyolCargoItem).filter(TrendyolCargoItem.invoice_serial == serial) \
          .delete(synchronize_session=False)
        for seq, it in enumerate(items, start=1):
            db.add(TrendyolCargoItem(invoice_serial=serial, seq=seq, synced_at=now, **it))
        r.detail_fetched_at = now
        db.commit()
        out["invoices"] += 1
        out["items"] += len(items)
    return out


def _paged_contents(api, path: str, params: dict) -> Iterable[dict]:
    """Ürün uçlarının sayfalaması: page×size 10.000'e kadar sayfa numarasıyla,
    sonrası `nextPageToken` ile."""
    page, token = 0, None
    for _ in range(MAX_PAGES):
        q = dict(params, size=PRODUCT_PAGE)
        if token:
            q["nextPageToken"] = token
        else:
            q["page"] = page
        data = api.get("products", path, q)
        content = _content(data)
        yield from content
        if not content:
            return
        nxt = data.get("nextPageToken") if isinstance(data, dict) else None
        if token is not None:
            if not nxt or nxt == token:
                return
            token = nxt
            continue
        page += 1
        total = _int(data.get("totalPages")) if isinstance(data, dict) else None
        if total is not None and page >= total:
            return
        if page * PRODUCT_PAGE >= PRODUCT_TOKEN_AFTER:
            if not nxt:
                return
            token = nxt


def sync_catalog(db: Session, api, *, now: Optional[datetime] = None) -> dict:
    """Onaylı ürünler (başlık/marka/KDV/durum) + stok/fiyat → `trendyol_products`.
    Görülmeyen satır silinmez; `last_seen_at` eski kalır."""
    now = now or datetime.utcnow()
    sid = api.seller_id
    found: Dict[str, dict] = {}
    _progress(detail="Katalog: onaylı ürünler")
    for c in _paged_contents(api, f"/product/sellers/{sid}/products/approved", {"supplierId": sid}):
        brand, cat = c.get("brand"), c.get("category")
        for v in c.get("variants") or []:
            if not isinstance(v, dict):
                continue
            bc = _txt(v.get("barcode"), 64)
            if not bc:
                continue
            d = found.setdefault(bc, {})
            d.update({
                "content_id": _int(c.get("contentId")),
                "product_main_id": _txt(c.get("productMainId"), 100),
                "title": _txt(c.get("title"), 300),
                "brand": _txt(brand.get("name") if isinstance(brand, dict) else brand, 100),
                "category": _txt(cat.get("name") if isinstance(cat, dict) else None, 150),
                "stock_code": _txt(v.get("stockCode"), 100),
                "vat_rate": _num(v.get("vatRate")),
                "on_sale": _bool(v.get("onSale")),
                "archived": _bool(v.get("archived")),
                "locked": _bool(v.get("locked")),
                "blacklisted": _bool(v.get("blacklisted")),
                "product_url": _txt(v.get("productUrl"), 500),
            })
            price = v.get("price") if isinstance(v.get("price"), dict) else {}
            for key, src in (("sale_price", "salePrice"), ("list_price", "listPrice")):
                val = _num(price.get(src))
                if val is not None:
                    d[key] = val
    _progress(detail="Katalog: stok ve fiyat")
    with_stock = 0
    for c in _paged_contents(api, f"/product/sellers/{sid}/products/approved/inventory-and-price",
                             {"supplierId": sid}):
        for v in c.get("variants") or []:
            if not isinstance(v, dict):
                continue
            bc = _txt(v.get("barcode"), 64)
            if not bc:
                continue
            d = found.setdefault(bc, {"content_id": _int(c.get("contentId")),
                                      "product_main_id": _txt(c.get("productMainId"), 100)})
            qty = _int(v.get("quantity"))
            if qty is not None:
                d["quantity"] = qty
                with_stock += 1
            for key, src in (("sale_price", "salePrice"), ("list_price", "listPrice")):
                val = _num(v.get(src))
                if val is not None:
                    d[key] = val
            if not d.get("stock_code"):
                d["stock_code"] = _txt(v.get("stockCode"), 100)
    # Bu çekimde stok kaydı gelmeyen varyantın eski adedi KALMASIN — bayat
    # Trendyol stoğu yanlış "fazla stok" uyarısı üretirdi.
    for d in found.values():
        d.setdefault("quantity", None)
    existing = {p.barcode: p for p in db.query(TrendyolProduct).all()}
    new = 0
    for bc, d in found.items():
        row = existing.get(bc)
        if row is None:
            row = TrendyolProduct(barcode=bc, first_seen_at=now)
            db.add(row)
            new += 1
        for k, v in d.items():
            setattr(row, k, v)
        row.last_seen_at = now
        row.synced_at = now
    db.flush()
    missing = sum(1 for bc, p in existing.items() if bc not in found)
    db.commit()
    return {"products": len(found), "new": new, "with_stock": with_stock, "missing": missing}


# ─── Orkestrasyon ────────────────────────────────────────────────────────────

def run(kind: str, *, backfill_days: Optional[int] = None, db: Optional[Session] = None,
        api=None, trigger: str = "manual", actor: Optional[str] = None) -> dict:
    """Bir ya da tüm akışları çalıştırır.  Süreç içi kilit: aynı anda tek
    çalıştırma (elle, backfill ve zamanlayıcı aynı kilitten geçer; tek uvicorn
    süreci için yeterli).  Kilit doluysa {"skipped": "running"}.  Akış başına
    hata yakalanır ve durum kaydına yazılır; kimlik hatası kalan akışları
    durdurur.  `db`/`api` verilmezse kendi oturumunu/istemcisini açar."""
    if kind not in KINDS:
        raise ValueError(f"Bilinmeyen senkron türü: {kind}")
    if not _RUN_LOCK.acquire(blocking=False):
        return {"skipped": "running", "progress": progress_snapshot()}
    own_db, own_api = db is None, api is None
    results: Dict[str, dict] = {}
    _progress(running=True, kind=kind, trigger=trigger, actor=actor, started_at=datetime.utcnow(),
              finished_at=None, step=None, detail="Başlıyor…", result=None, error=None)
    try:
        if own_db:
            db = SessionLocal()
        if own_api:
            try:
                api = make_api()
            except TrendyolError as e:
                _progress(error=str(e))
                return {"kind": kind, "error": str(e), "results": {}}
        for stream in (STREAMS if kind == "all" else (kind,)):
            _progress(step=stream)
            st = _state(db, stream)
            st.last_run_at = datetime.utcnow()
            db.commit()
            try:
                if stream == "orders":
                    res = sync_orders(db, api, lookback_days=backfill_days)
                elif stream == "finance":
                    res = sync_finance(db, api, days=backfill_days)
                else:
                    res = sync_catalog(db, api)
            except TrendyolStopped as e:                 # kapanış — hata SAYILMAZ
                db.rollback()
                logger.info("trendyol %s senkronu kapanış nedeniyle durdu", stream)
                results[stream] = {"ok": False, "stopped": True, "error": str(e)}
                break
            except Exception as e:                       # akış başına izole
                db.rollback()
                if isinstance(e, TrendyolError):
                    logger.warning("trendyol %s senkronu başarısız: %s", stream, _err_text(e))
                else:
                    logger.exception("trendyol %s senkronunda beklenmeyen hata", stream)
                _mark_fail(db, stream, e)
                results[stream] = {"ok": False, "error": _err_text(e),
                                   "auth": isinstance(e, TrendyolAuthError)}
                if isinstance(e, TrendyolAuthError):
                    break
                continue
            _mark_ok(db, stream, res)
            results[stream] = {"ok": True, **res}
        return {"kind": kind, "results": results, "calls": getattr(api, "calls", None)}
    finally:
        _progress(running=False, finished_at=datetime.utcnow(), step=None, detail=None,
                  result={s: ("tamam" if r.get("ok") else ("durduruldu" if r.get("stopped")
                                                           else f"HATA: {r.get('error', '')[:120]}"))
                          for s, r in results.items()})
        if own_api and api is not None:
            try:
                api.close()
            except Exception:
                pass
        if own_db and db is not None:
            db.close()
        _RUN_LOCK.release()


_THREAD: Optional[threading.Thread] = None


def _thread_main(kind: str, backfill_days: Optional[int], actor: Optional[str]) -> None:
    try:
        run(kind, backfill_days=backfill_days, trigger="manual", actor=actor)
    except Exception:
        logger.exception("trendyol elle senkron iş parçacığında beklenmeyen hata")


def start_in_background(kind: str, *, backfill_days: Optional[int] = None,
                        actor: Optional[str] = None) -> threading.Thread:
    """Elle senkron/geçmiş çekme — istekten BAĞIMSIZ daemon iş parçacığında.
    FastAPI BackgroundTasks KULLANILMAZ: uvicorn kapanışta süren arka plan
    görevini süresiz bekler, uzun bir geçmiş çekimi deploy'u ~90 sn'lik
    kesintiye (systemd SIGKILL) çevirirdi.  Kapanışta `request_stop()` süren işi
    bir istek/bekleme içinde durdurur; daemon iş parçacığı süreç çıkışını
    bekletmez."""
    global _THREAD
    t = threading.Thread(target=_thread_main, args=(kind, backfill_days, actor),
                         name="trendyol-sync", daemon=True)
    _THREAD = t
    t.start()
    return t


def alerts_due(db: Session, results: Dict[str, dict], *, now: Optional[datetime] = None) -> List[Tuple[str, str]]:
    """Bildirilecek (akış, hata) çiftleri — eşik dolduysa ve son 24 saatte
    bildirilmediyse; döndürülenler `last_notified_at` ile damgalanır.
    Kimlik hatası ilk seferde, orders/catalog üst üste 3 hatada, finans (günde
    bir çalışır) ilk hatada.  Başarı sayacı sessizce sıfırlar (_mark_ok)."""
    now = now or datetime.utcnow()
    out = []
    for stream, r in (results or {}).items():
        if r.get("ok") or r.get("stopped"):
            continue
        st = _state(db, stream)
        threshold = 1 if r.get("auth") else NOTIFY_AFTER.get(stream, 3)
        if (st.consecutive_failures or 0) < threshold:
            continue
        if st.last_notified_at and now - st.last_notified_at < NOTIFY_EVERY:
            continue
        st.last_notified_at = now
        out.append((stream, r.get("error") or "bilinmeyen hata"))
    db.commit()
    return out


# ─── Barkod çözümleme (elle eşleme → tek kart → belirsiz/eşleşmemiş) ─────────

_ALIASES: Optional[Dict[str, str]] = None


def barcode_aliases() -> Dict[str, str]:
    """Eski → yeni barkod (data/barcode_aliases.json) — yalnız ÖNERİ için."""
    global _ALIASES
    if _ALIASES is None:
        out: Dict[str, str] = {}
        try:
            d = json.loads(ALIAS_FILE.read_text(encoding="utf-8"))
            for a in d.get("resolved_aliases") or []:
                if a.get("old") and a.get("new"):
                    out[str(a["old"]).strip()] = str(a["new"]).strip()
            for p in ((d.get("serenida_uts_migration") or {}).get("pairs") or []):
                if p.get("old_uts") and p.get("new_gs1"):
                    out[str(p["old_uts"]).strip()] = str(p["new_gs1"]).strip()
        except (OSError, ValueError, AttributeError):
            out = {}
        _ALIASES = out
    return _ALIASES


def _item_view(it: Item) -> dict:
    return {"id": it.id, "name": it.name, "barcode": (it.barcode or "").strip() or None,
            "domain": it.domain, "unit": it.unit or "adet", "is_active": bool(it.is_active),
            "current_stock": float(it.current_stock or 0.0), "cost_price": float(it.cost_price or 0.0)}


def _panel(components: List[dict]) -> str:
    domains = {c.get("domain") for c in components if c.get("domain")}
    if not domains:
        return "unmatched"
    return domains.pop() if len(domains) == 1 else "mixed"


class Resolver:
    """Barkod → IMS kart(lar)ı.  Sıra: elle eşleme (set = birden çok satır) →
    aktif "Bitmiş Ürün" kartlarında (iki domain) TEK birebir barkod → ≥2 ise
    "belirsiz" (asla tahmin yok) → yoksa "eşleşmemiş" (+ eski barkod ise
    yalnız öneri).  Elle eşlenen kart pasifleşmişse "eşleme geçersiz"."""

    def __init__(self, db: Session):
        self.items: Dict[int, dict] = {}
        self.by_barcode: Dict[str, List[int]] = {}
        for it in (db.query(Item)
                   .filter(Item.category == FINISHED, Item.is_active == True)   # noqa: E712
                   .all()):
            self.items[it.id] = _item_view(it)
            bc = (it.barcode or "").strip()
            if bc:
                self.by_barcode.setdefault(bc, []).append(it.id)
        self.maps: Dict[str, List[dict]] = {}
        maps = db.query(TrendyolBarcodeMap).order_by(TrendyolBarcodeMap.id).all()
        for m in maps:
            self.maps.setdefault(m.barcode, []).append(
                {"map_id": m.id, "item_id": m.item_id, "qty": float(m.qty or 1.0), "note": m.note})
        missing = {m.item_id for m in maps} - set(self.items)
        self.others: Dict[int, dict] = {}
        if missing:
            for it in db.query(Item).filter(Item.id.in_(missing)).all():
                self.others[it.id] = _item_view(it)

    def resolve(self, barcode: Optional[str]) -> dict:
        bc = (barcode or "").strip()
        if not bc:
            return {"status": "unmatched", "components": [], "panel": "unmatched"}
        if bc in self.maps:
            comps, bad = [], False
            for m in self.maps[bc]:
                iv = self.items.get(m["item_id"])
                if iv is None:
                    bad = True
                    iv = self.others.get(m["item_id"]) or {"id": m["item_id"], "name": "(silinmiş kart)",
                                                           "domain": None, "is_active": False}
                comps.append({**iv, "qty": m["qty"], "map_id": m["map_id"]})
            return {"status": "invalid_map" if bad else "mapped", "components": comps,
                    "panel": "unmatched" if bad else _panel(comps)}
        ids = self.by_barcode.get(bc) or []
        if len(ids) == 1:
            comps = [{**self.items[ids[0]], "qty": 1.0}]
            return {"status": "matched", "components": comps, "panel": _panel(comps)}
        if len(ids) > 1:
            return {"status": "ambiguous", "components": [], "panel": "unmatched",
                    "candidates": [self.items[i] for i in ids]}
        out = {"status": "unmatched", "components": [], "panel": "unmatched"}
        new = barcode_aliases().get(bc)
        if new and len(self.by_barcode.get(new) or []) == 1:
            out["suggestion"] = {**self.items[self.by_barcode[new][0]], "via_barcode": new}
        return out


MATCH_LABELS = {"matched": "Eşleşti", "mapped": "Elle eşlendi", "ambiguous": "Belirsiz",
                "unmatched": "Eşleşmedi", "invalid_map": "Eşleme geçersiz"}
PANEL_LABELS = {"cosmetics": "Kozmetik", "supplement": "Food Supplement", "mixed": "Karma",
                "unmatched": "Eşleşmemiş"}


def public_resolution(r: dict, *, show_cost: bool) -> dict:
    """API'ye giden çözüm — maliyet yalnız kâr yetkisiyle."""
    def comp(c):
        d = {k: c.get(k) for k in ("id", "name", "barcode", "domain", "unit", "is_active",
                                   "current_stock", "qty", "map_id", "via_barcode") if k in c}
        if show_cost:
            d["cost_price"] = c.get("cost_price")
        return d
    out = {"status": r["status"], "label": MATCH_LABELS.get(r["status"], r["status"]),
           "panel": r["panel"], "panel_label": PANEL_LABELS.get(r["panel"], r["panel"]),
           "components": [comp(c) for c in r.get("components") or []]}
    if r.get("candidates"):
        out["candidates"] = [comp(c) for c in r["candidates"]]
    if r.get("suggestion"):
        out["suggestion"] = comp(r["suggestion"])
    return out


def ims_stock(r: dict) -> Optional[float]:
    """Çözülen barkodun IMS stoğu — set için min ⌊stok / adet⌋."""
    comps = r.get("components") or []
    if r.get("status") not in ("matched", "mapped") or not comps:
        return None
    if len(comps) == 1 and abs((comps[0].get("qty") or 1.0) - 1.0) < 1e-9:
        return float(comps[0].get("current_stock") or 0.0)
    vals = []
    for c in comps:
        q = c.get("qty") or 1.0
        vals.append(int(max(0.0, float(c.get("current_stock") or 0.0)) // q) if q > 0 else 0)
    return float(min(vals)) if vals else None


def can_profit(user) -> bool:
    """Kâr/maliyet alanları: trendyol.profit + finans rolü + finance.view (asistan
    kuralıyla aynı — override ile LabTech'e 'profit' verilse de maliyet açılmaz)."""
    from core.permissions import _FINANCE_ROLES, _resolve_permissions
    if not user or not getattr(user, "is_active", False):
        return False
    perms = _resolve_permissions(user)
    return (user.role in _FINANCE_ROLES
            and bool((perms.get("finance") or {}).get("view"))
            and bool((perms.get("trendyol") or {}).get("profit")))


# ─── Görünümler (router'a sade sözlük) ───────────────────────────────────────

def status_summary(db: Session) -> dict:
    problems = config_problems()
    states = {s.stream: s for s in db.query(TrendyolSyncState).all()}
    streams = []
    for name in STREAMS:
        s = states.get(name)
        meta = _meta(s)
        streams.append({
            "stream": name, "label": STREAM_LABELS[name], "schedule": SCHEDULE_TEXT[name],
            "last_run_at": _fmt(s.last_run_at) if s else None,
            "last_ok_at": _fmt(s.last_ok_at) if s else None,
            "last_status": s.last_status if s else None,
            "last_error": s.last_error if s else None,
            "consecutive_failures": (s.consecutive_failures or 0) if s else 0,
            "watermark": _fmt(s.watermark) if s else None,
            "last": meta.get("last") or {},
        })
    counts = {
        "packages": db.query(func.count(TrendyolPackage.id)).scalar() or 0,
        "lines": db.query(func.count(TrendyolLine.id)).scalar() or 0,
        "fin_rows": db.query(func.count(TrendyolFinRow.id)).scalar() or 0,
        "cargo_items": db.query(func.count(TrendyolCargoItem.id)).scalar() or 0,
        "products": db.query(func.count(TrendyolProduct.id)).scalar() or 0,
        "mappings": db.query(func.count(func.distinct(TrendyolBarcodeMap.barcode))).scalar() or 0,
    }
    first = db.query(func.min(TrendyolPackage.order_date)).scalar()
    return {"configured": not problems, "missing": problems, "enabled": get_enabled(db),
            "custom_base_url": bool(_env("TRENDYOL_BASE_URL")),
            "storefront_set": bool(_env("TRENDYOL_STOREFRONT_CODE")),
            "ts_tr_wallclock": TS_TR_WALLCLOCK, "streams": streams, "counts": counts,
            "first_order_at": _fmt(first), "progress": progress_snapshot(),
            "running": is_running()}


def _match_q(q: str, *texts) -> bool:
    fq = _fold(q)
    return any(fq in _fold(t) for t in texts if t)


def catalog_view(db: Session, *, panel: str = "all", q: Optional[str] = None,
                 problems_only: bool = False) -> dict:
    """Eşleştirme & Stok sekmesi: katalog ürünü × çözüm × IMS stoğu."""
    res = Resolver(db)
    products = db.query(TrendyolProduct).order_by(TrendyolProduct.title, TrendyolProduct.barcode).all()
    last_seen = max((p.last_seen_at for p in products if p.last_seen_at), default=None)
    rows, summary, covered = [], Counter(), set()
    for p in products:
        r = res.resolve(p.barcode)
        for c in r.get("components") or []:
            covered.add(c.get("id"))
        stock = ims_stock(r)
        stale = bool(last_seen and p.last_seen_at and p.last_seen_at < last_seen)
        ty_qty = None if stale else p.quantity          # son senkronda yoksa adet bilinmiyor
        diff = (ty_qty - stock) if (ty_qty is not None and stock is not None) else None
        row = {
            "barcode": p.barcode, "title": p.title, "brand": p.brand, "stock_code": p.stock_code,
            "sale_price": p.sale_price, "list_price": p.list_price, "quantity": ty_qty,
            "vat_rate": p.vat_rate, "on_sale": p.on_sale, "archived": p.archived,
            "locked": p.locked, "blacklisted": p.blacklisted, "product_url": p.product_url,
            "stale": stale, "last_seen_at": _fmt(p.last_seen_at),
            "resolution": public_resolution(r, show_cost=False),
            "ims_stock": stock, "diff": diff,
            "over": diff is not None and diff > 0,
        }
        summary[r["status"]] += 1
        if row["over"]:
            summary["over"] += 1
        if panel != "all" and r["panel"] != panel:
            continue
        if q and not _match_q(q, p.title, p.barcode, p.stock_code, p.brand,
                              *[c.get("name") for c in r.get("components") or []]):
            continue
        if problems_only and r["status"] in ("matched", "mapped") and not row["over"]:
            continue
        rows.append(row)
    cat_bcs = {p.barcode for p in products}
    not_in_catalog = []
    for bc, name, n in (db.query(TrendyolLine.barcode, func.max(TrendyolLine.product_name),
                                 func.count(TrendyolLine.id))
                        .join(TrendyolPackage, TrendyolPackage.id == TrendyolLine.package_row_id)
                        .filter(TrendyolLine.barcode.isnot(None),
                                or_(TrendyolPackage.status.is_(None), TrendyolPackage.status != UNPACKED))
                        .group_by(TrendyolLine.barcode).all()):
        if bc in cat_bcs:
            continue
        r = res.resolve(bc)
        not_in_catalog.append({"barcode": bc, "product_name": name, "lines": n,
                               "resolution": public_resolution(r, show_cost=False)})
    ims_missing = []
    if products:
        for iid, it in sorted(res.items.items(), key=lambda kv: kv[1]["name"] or ""):
            if it["barcode"] and iid not in covered:
                ims_missing.append({k: it[k] for k in ("id", "name", "barcode", "domain", "current_stock", "unit")})
    return {"rows": rows, "summary": dict(summary), "total": len(products),
            "last_sync": _fmt(last_seen), "not_in_catalog": not_in_catalog,
            "ims_missing": ims_missing}


def _tr_day_bounds(start: Optional[str], end: Optional[str]) -> Tuple[Optional[datetime], Optional[datetime]]:
    """'YYYY-MM-DD' TR günleri → [başlangıç, bitiş) UTC sınırları (bitiş günü dahil)."""
    def parse(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except (TypeError, ValueError):
            return None
    s, e = parse(start), parse(end)
    return (s - TR_OFFSET if s else None, (e + timedelta(days=1)) - TR_OFFSET if e else None)


def package_view(p: TrendyolPackage, res: Resolver, *, show_cost: bool) -> dict:
    lines, warnings = [], set()
    for ln in p.lines:
        r = res.resolve(ln.barcode)
        if r["status"] not in ("matched", "mapped"):
            warnings.add(MATCH_LABELS.get(r["status"], r["status"]) + " barkod")
        lines.append({"line_id": ln.line_id, "barcode": ln.barcode, "product_name": ln.product_name,
                      "quantity": ln.quantity, "unit_price": ln.unit_price, "net_total": ln.net_total,
                      "gross_total": ln.gross_total, "seller_discount_total": ln.seller_discount_total,
                      "ty_discount_total": ln.ty_discount_total, "vat_rate": ln.vat_rate,
                      "status": ln.line_status,
                      "resolution": public_resolution(r, show_cost=show_cost)})
        if show_cost:
            lines[-1]["commission_rate"] = ln.commission_rate
    # Eşleşmeyen satır paneli belirsizleştirmez (uyarı çipi gösterir); bilinen
    # paneller tek ise o, birden çoksa (ya da set ürün karma ise) "mixed".
    known = {ln["resolution"]["panel"] for ln in lines} - {"unmatched"}
    panel = "unmatched" if not known else (known.pop() if len(known) == 1 else "mixed")
    return {"package_id": p.package_id, "order_number": p.order_number, "status": p.status,
            "status_label": STATUS_LABELS.get(p.status or "", p.status),
            "order_date": _fmt(p.order_date), "total_price": p.total_price,
            "gross_amount": p.gross_amount, "seller_discount": p.seller_discount,
            "ty_discount": p.ty_discount, "currency": p.currency or "TRY",
            "cargo_provider": p.cargo_provider, "cargo_tracking": p.cargo_tracking,
            "origin_package_ids": p.origin_package_ids, "cancel_reason": p.cancel_reason,
            "lines": lines, "panel": panel, "panel_label": PANEL_LABELS.get(panel, panel),
            "warnings": sorted(warnings)}


def orders_view(db: Session, *, start: Optional[str] = None, end: Optional[str] = None,
                status: Optional[str] = None, panel: str = "all", q: Optional[str] = None,
                page: int = 0, size: int = 50, show_cost: bool = False) -> dict:
    s_utc, e_utc = _tr_day_bounds(start, end)
    query = db.query(TrendyolPackage).options(selectinload(TrendyolPackage.lines))
    if s_utc:
        query = query.filter(TrendyolPackage.order_date >= s_utc)
    if e_utc:
        query = query.filter(TrendyolPackage.order_date < e_utc)
    pkgs = query.order_by(TrendyolPackage.order_date.desc(), TrendyolPackage.id.desc()).all()
    res = Resolver(db)
    views, status_counts = [], Counter()
    for p in pkgs:
        v = package_view(p, res, show_cost=show_cost)
        if panel != "all" and v["panel"] != panel:
            continue
        if q and not _match_q(q, p.order_number, str(p.package_id),
                              *[ln.barcode for ln in p.lines], *[ln.product_name for ln in p.lines]):
            continue
        status_counts[p.status or "?"] += 1
        if status and p.status != status:
            continue
        if p.status == UNPACKED and status != UNPACKED:
            continue                     # bölünen paketin aslı — çocukları ayrı paket olarak listede
        views.append(v)
    size = max(1, min(int(size or 50), 200))
    page = max(0, int(page or 0))
    return {"orders": views[page * size:(page + 1) * size], "total": len(views), "page": page,
            "size": size, "status_counts": dict(status_counts),
            "status_labels": STATUS_LABELS}


def order_detail(db: Session, order_number: str, *, show_cost: bool) -> Optional[dict]:
    pkgs = (db.query(TrendyolPackage).filter(TrendyolPackage.order_number == order_number)
            .order_by(TrendyolPackage.id).all())
    if not pkgs:
        return None
    res = Resolver(db)
    out = {"order_number": order_number,
           "packages": [package_view(p, res, show_cost=show_cost) for p in pkgs]}
    if show_cost:
        fin = (db.query(TrendyolFinRow).filter(TrendyolFinRow.order_number == order_number)
               .order_by(TrendyolFinRow.transaction_date, TrendyolFinRow.id).all())
        out["finance"] = [{"source": f.source, "type": f.type_param, "label": f.type_label,
                           "sub_type": f.sub_type, "date": _fmt(f.transaction_date),
                           "barcode": f.barcode, "package_id": f.package_id, "debt": f.debt,
                           "credit": f.credit, "commission_rate": f.commission_rate,
                           "commission_amount": f.commission_amount, "seller_revenue": f.seller_revenue,
                           "payment_date": _fmt(f.payment_date)} for f in fin]
        cargo = (db.query(TrendyolCargoItem).filter(TrendyolCargoItem.order_number == order_number)
                 .order_by(TrendyolCargoItem.id).all())
        out["cargo"] = [{"invoice_serial": c.invoice_serial, "kind": c.kind, "is_return": c.is_return,
                         "amount": c.amount, "desi": c.desi, "parcel_unique_id": c.parcel_unique_id}
                        for c in cargo]
    return out


def calibration_view(db: Session, *, start: Optional[str] = None, end: Optional[str] = None) -> dict:
    """Ham toplamlar — panelden indirilen Cari Hesap Ekstresi ile kova kova
    karşılaştırmak için (tür + Trendyol etiketi + alt tür)."""
    s_utc, e_utc = _tr_day_bounds(start, end)
    q = db.query(TrendyolFinRow.source, TrendyolFinRow.type_param, TrendyolFinRow.sub_type,
                 TrendyolFinRow.type_label, func.count(TrendyolFinRow.id), func.sum(TrendyolFinRow.debt),
                 func.sum(TrendyolFinRow.credit), func.sum(TrendyolFinRow.commission_amount),
                 func.sum(TrendyolFinRow.seller_revenue))
    if s_utc:
        q = q.filter(TrendyolFinRow.transaction_date >= s_utc)
    if e_utc:
        q = q.filter(TrendyolFinRow.transaction_date < e_utc)
    groups = [{"source": src, "type": tp, "sub_type": sub, "label": lab, "count": n,
               "debt": round(d or 0.0, 2), "credit": round(c or 0.0, 2),
               "commission_amount": round(ca or 0.0, 4), "seller_revenue": round(sr or 0.0, 4)}
              for src, tp, sub, lab, n, d, c, ca, sr in
              q.group_by(TrendyolFinRow.source, TrendyolFinRow.type_param, TrendyolFinRow.sub_type,
                         TrendyolFinRow.type_label)
               .order_by(TrendyolFinRow.source, TrendyolFinRow.type_param).all()]
    inv_q = db.query(TrendyolFinRow).filter(TrendyolFinRow.source == "other",
                                            TrendyolFinRow.type_param == "DeductionInvoices")
    if s_utc:
        inv_q = inv_q.filter(TrendyolFinRow.transaction_date >= s_utc)
    if e_utc:
        inv_q = inv_q.filter(TrendyolFinRow.transaction_date < e_utc)
    sums = dict(db.query(TrendyolCargoItem.invoice_serial, func.sum(TrendyolCargoItem.amount))
                .group_by(TrendyolCargoItem.invoice_serial).all())
    invoices = []
    for f in inv_q.order_by(TrendyolFinRow.transaction_date).all():
        if not is_cargo_invoice_label(f.type_label, f.description):
            continue
        total = (f.debt or 0.0) or (f.credit or 0.0)
        items = sums.get(f.row_id)
        invoices.append({"serial": f.row_id, "date": _fmt(f.transaction_date), "invoice_total": total,
                         "items_total": round(items, 2) if items is not None else None,
                         "ratio": round(total / items, 4) if items else None,
                         "fetched": f.detail_fetched_at is not None})
    return {"groups": groups, "cargo_invoices": invoices}


# ─── Probe (kalibrasyon için maskeli ham örnek) ──────────────────────────────

_REDACT = ("address", "email", "phone", "gsm", "identity", "taxnumber", "taxoffice", "customer",
           "firstname", "lastname", "fullname", "tckn", "vkn", "invoicelink", "username", "receiver")


def redact(obj):
    """Kişisel veri olabilecek anahtarları maskeler (iç içe)."""
    if isinstance(obj, dict):
        return {k: ("•••" if any(t in str(k).lower() for t in _REDACT) else redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


PROBE_KINDS = ("orders", "settlements", "otherfinancials", "products", "inventory")


def probe(api, kind: str, *, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    sid = api.seller_id
    if kind == "orders":
        data = api.get("orders", f"/order/sellers/{sid}/orders/stream",
                       {"supplierId": sid, "lastModifiedStartDate": to_ty_ms(now - timedelta(days=13)),
                        "lastModifiedEndDate": to_ty_ms(now), "size": 3})
    elif kind in ("settlements", "otherfinancials"):
        data = api.get("finance", f"/finance/che/sellers/{sid}/{kind}",
                       {"supplierId": sid, "transactionType": "Sale" if kind == "settlements" else "DeductionInvoices",
                        "startDate": to_ty_ms(now - timedelta(days=14)), "endDate": to_ty_ms(now),
                        "page": 0, "size": 500})
    elif kind == "products":
        data = api.get("products", f"/product/sellers/{sid}/products/approved",
                       {"supplierId": sid, "page": 0, "size": 3})
    elif kind == "inventory":
        data = api.get("products", f"/product/sellers/{sid}/products/approved/inventory-and-price",
                       {"supplierId": sid, "page": 0, "size": 3})
    else:
        raise ValueError(kind)
    content = _content(data)[:3]
    keys = sorted(data.keys()) if isinstance(data, dict) else []
    return {"kind": kind, "top_level_keys": keys, "sample": redact(content),
            "ts_tr_wallclock": TS_TR_WALLCLOCK}


def connection_test(api) -> dict:
    """Tek, hafif okuma çağrısı (katalogdan 1 kayıt)."""
    data = api.get("products", f"/product/sellers/{api.seller_id}/products/approved",
                   {"supplierId": api.seller_id, "page": 0, "size": 1})
    total = _int(data.get("totalElements")) if isinstance(data, dict) else None
    return {"ok": True, "catalog_total": total}
