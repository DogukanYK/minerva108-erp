# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
B2B router — quotations workflow + TCMB currency rates.
"""
from fastapi import APIRouter, Depends, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional, List

from database import get_db, Item, Transaction, Quotation, QuotationItem, to_tr
from core.permissions import require_permission
from core.notifications import notify_low_stock
from core.domain import active_domain

router = APIRouter(prefix="/api", tags=["b2b"])


# ─── Currency Rates (Phase 3 / Task 2 — TCMB live feed) ─────────────────────

_rate_cache = {"data": None, "fetched_at": None}
_RATE_TTL_SECONDS = 3600   # 1 hour

# Last-known-good fallback (overwritten on first successful TCMB fetch)
_RATE_FALLBACK = {"USD": 34.50, "EUR": 37.20}


def _fetch_tcmb_rates():
    """
    Fetch today's USD/EUR forex selling rates from TCMB.
    Returns dict like {"USD": 34.61, "EUR": 37.25} on success, None on failure.
    Uses stdlib only — no extra deps.
    """
    import urllib.request
    import xml.etree.ElementTree as ET

    url = "https://www.tcmb.gov.tr/kurlar/today.xml"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Minerva108-ERP/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            xml_data = resp.read().decode("utf-8")

        root = ET.fromstring(xml_data)
        out = {}
        for currency in root.findall("Currency"):
            code = currency.get("CurrencyCode")
            if code in ("USD", "EUR"):
                node = currency.find("ForexSelling")
                if node is not None and node.text:
                    out[code] = float(node.text.strip())
        return out if {"USD", "EUR"}.issubset(out.keys()) else None
    except Exception:
        return None


@router.get("/currency-rates")
def get_currency_rates(_: dict = Depends(require_permission("finance", "view"))):
    """
    Live USD & EUR rates against TRY (TCMB ForexSelling).
    Cached 1 hour in-memory; falls back to last cache or hardcoded values on outage.

    Response:
      { source, USD, EUR, fetched_at, cached, stale?, warning? }
    All rates are TRY per 1 unit of foreign currency.
    e.g. usd_amount = try_amount / response.USD
    """
    from datetime import datetime as _dt
    now = _dt.utcnow()
    cached_data = _rate_cache.get("data")
    cached_at   = _rate_cache.get("fetched_at")

    # ── Serve fresh cache ───────────────────────────────────────────────
    if cached_data and cached_at and (now - cached_at).total_seconds() < _RATE_TTL_SECONDS:
        return {
            **cached_data,
            "fetched_at": cached_at.isoformat() + "Z",
            "cached": True,
        }

    # ── Try live fetch ──────────────────────────────────────────────────
    fresh = _fetch_tcmb_rates()
    if fresh and "USD" in fresh and "EUR" in fresh:
        data = {"source": "TCMB", "USD": fresh["USD"], "EUR": fresh["EUR"]}
        _rate_cache["data"]       = data
        _rate_cache["fetched_at"] = now
        return {**data, "fetched_at": now.isoformat() + "Z", "cached": False}

    # ── Fall back to stale cache if available ──────────────────────────
    if cached_data:
        return {
            **cached_data,
            "fetched_at": cached_at.isoformat() + "Z" if cached_at else None,
            "cached": True,
            "stale":  True,
            "warning": "TCMB'ye ulaşılamadı; önceki kur kullanılıyor.",
        }

    # ── Final hardcoded fallback ───────────────────────────────────────
    return {
        "source":     "fallback",
        "USD":        _RATE_FALLBACK["USD"],
        "EUR":        _RATE_FALLBACK["EUR"],
        "fetched_at": now.isoformat() + "Z",
        "cached":     False,
        "stale":      True,
        "warning":    "TCMB'ye ulaşılamadı; varsayılan kurlar kullanılıyor. Manuel doğrulama önerilir.",
    }


# ─── Quotations / Order Fulfillment (Phase 3 / Task 3) ──────────────────────

class QuotationLineRequest(BaseModel):
    item_id:            int
    quantity:           float           = Field(..., gt=0,  le=1_000_000)
    unit_price_foreign: float           = Field(..., ge=0,  le=10_000_000)
    unit_cost_try:      Optional[float] = Field(None, ge=0, le=10_000_000)
    item_name_snapshot: Optional[str]   = Field(None, max_length=200)


class QuotationCreateRequest(BaseModel):
    quote_number:     str = Field(..., min_length=1, max_length=50)
    customer_name:    str = Field(..., min_length=1, max_length=150)
    customer_contact: Optional[str] = Field(None, max_length=150)
    customer_email:   Optional[str] = Field(None, max_length=150)
    customer_phone:   Optional[str] = Field(None, max_length=50)
    customer_address: Optional[str] = Field(None, max_length=500)
    customer_country: Optional[str] = Field(None, max_length=100)
    customer_vat:     Optional[str] = Field(None, max_length=50)

    currency:        str   = Field("TRY", max_length=3)
    exchange_rate:   Optional[float] = Field(None, gt=0, le=100_000)
    subtotal_amount: float = Field(0.0,  ge=0, le=1_000_000_000)
    tax_percentage:  float = Field(0.0,  ge=0, le=100)
    tax_amount:      float = Field(0.0,  ge=0, le=1_000_000_000)
    shipping_amount: float = Field(0.0,  ge=0, le=1_000_000_000)
    total_amount:    float = Field(...,  ge=0, le=1_000_000_000)

    notes:      Optional[str] = Field(None, max_length=2000)
    valid_days: int           = Field(30,   ge=1, le=365)

    # En fazla 500 satırlık teklif — operasyonel olarak makul, payload DoS önler
    items: List[QuotationLineRequest] = Field(..., min_length=1, max_length=500)


def _serialize_quotation_summary(q: Quotation) -> dict:
    return {
        "id":               q.id,
        "quote_number":     q.quote_number,
        "customer_name":    q.customer_name,
        "customer_country": q.customer_country or "",
        "currency":         q.currency,
        "total_amount":     q.total_amount,
        "status":           q.status,
        "created_at":       to_tr(q.created_at).strftime("%d.%m.%Y %H:%M") if q.created_at else "",
        "created_by":       q.created_by or "—",
        "confirmed_at":     to_tr(q.confirmed_at).strftime("%d.%m.%Y %H:%M") if q.confirmed_at else None,
        "confirmed_by":     q.confirmed_by,
        "item_count":       len(q.items),
        # Distribütör sipariş portalı — distributor_id set ise sipariş distribütörden geldi
        "distributor_id":   q.distributor_id,
        "distributor_name": (q.distributor.company_name if q.distributor_id and q.distributor else None),
        "rejected_at":      to_tr(q.rejected_at).strftime("%d.%m.%Y %H:%M") if q.rejected_at else None,
        "rejected_by":      q.rejected_by,
        "reject_reason":    q.reject_reason,
    }


def _serialize_quotation_full(q: Quotation) -> dict:
    return {
        **_serialize_quotation_summary(q),
        "customer_contact": q.customer_contact,
        "customer_email":   q.customer_email,
        "customer_phone":   q.customer_phone,
        "customer_address": q.customer_address,
        "customer_vat":     q.customer_vat,
        "exchange_rate":    q.exchange_rate,
        "subtotal_amount":  q.subtotal_amount,
        "tax_percentage":   q.tax_percentage,
        "tax_amount":       q.tax_amount,
        "shipping_amount":  q.shipping_amount,
        "notes":            q.notes,
        "valid_days":       q.valid_days,
        "items": [
            {
                "id":                 i.id,
                "item_id":            i.item_id,
                "item_name_snapshot": i.item_name_snapshot,
                "quantity":           i.quantity,
                "unit_price_foreign": i.unit_price_foreign,
                "unit_cost_try":      i.unit_cost_try,
                "line_total":         i.line_total,
            }
            for i in q.items
        ],
    }


@router.post("/quotations", status_code=201)
def create_quotation(
    data: QuotationCreateRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("b2b", "create")),
    domain: str = Depends(active_domain),
):
    """Save a quotation as DRAFT. Validates quote_number uniqueness + at least one line."""
    actor = current_user.get("full_name") or current_user.get("username") or "—"

    if data.currency not in ("USD", "EUR", "TRY"):
        return JSONResponse(status_code=400, content={"detail": "Geçersiz para birimi (USD/EUR/TRY)."})
    if not data.items:
        return JSONResponse(status_code=400, content={"detail": "Teklifte en az bir kalem olmalıdır."})
    if not data.customer_name or not data.customer_name.strip():
        return JSONResponse(status_code=400, content={"detail": "Müşteri / şirket adı zorunludur."})

    if db.query(Quotation).filter(Quotation.quote_number == data.quote_number).first():
        return JSONResponse(status_code=400, content={
            "detail": f"Teklif numarası '{data.quote_number}' zaten kullanımda. Lütfen farklı bir numara giriniz."
        })

    try:
        q = Quotation(
            quote_number=data.quote_number.strip(),
            customer_name=data.customer_name.strip(),
            customer_contact=data.customer_contact,
            customer_email=data.customer_email,
            customer_phone=data.customer_phone,
            customer_address=data.customer_address,
            customer_country=data.customer_country,
            customer_vat=data.customer_vat,
            currency=data.currency,
            exchange_rate=data.exchange_rate,
            subtotal_amount=data.subtotal_amount,
            tax_percentage=data.tax_percentage,
            tax_amount=data.tax_amount,
            shipping_amount=data.shipping_amount,
            total_amount=data.total_amount,
            notes=data.notes,
            valid_days=data.valid_days,
            status="DRAFT",
            domain=domain,                     # Faz 3 — aktif panel
            created_by=actor,
        )
        db.add(q)
        db.flush()  # Need q.id for items

        for line in data.items:
            line_total = round(line.quantity * line.unit_price_foreign, 4)
            db.add(QuotationItem(
                quotation_id=q.id,
                item_id=line.item_id,
                item_name_snapshot=line.item_name_snapshot,
                quantity=line.quantity,
                unit_price_foreign=line.unit_price_foreign,
                unit_cost_try=line.unit_cost_try,
                line_total=line_total,
            ))

        db.commit()
        db.refresh(q)
        return {
            "id":           q.id,
            "quote_number": q.quote_number,
            "message":      f"Teklif #{q.quote_number} taslak olarak kaydedildi.",
        }
    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Teklif kaydedilemedi."})


@router.get("/quotations")
def list_quotations(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("b2b", "view")),
    limit:  int = 50,
    status: Optional[str] = None,
    domain: str = Depends(active_domain),
):
    q = db.query(Quotation).filter(Quotation.domain == domain).order_by(Quotation.id.desc())
    if status:
        q = q.filter(Quotation.status == status.upper())
    return [_serialize_quotation_summary(r) for r in q.limit(limit).all()]


@router.get("/quotations/{quote_id}")
def get_quotation(quote_id: int, db: Session = Depends(get_db), _: dict = Depends(require_permission("b2b", "view"))):
    q = db.query(Quotation).filter(Quotation.id == quote_id).first()
    if not q:
        return JSONResponse(status_code=404, content={"detail": "Teklif bulunamadı."})
    return _serialize_quotation_full(q)


@router.post("/quotations/{quote_id}/confirm")
def confirm_quotation(
    quote_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("b2b", "confirm")),
):
    """
    The CRITICAL endpoint. Flips DRAFT → CONFIRMED and:
      1) Pre-flight stock check across ALL lines (collects every issue, no partial state)
      2) Atomically deducts current_stock for each line
      3) Logs an Output Transaction for each deduction (audit trail)
      4) Stamps confirmed_at + confirmed_by
    Idempotent: re-confirming a CONFIRMED quote is rejected.
    """
    actor = current_user.get("full_name") or current_user.get("username") or "—"

    quote = db.query(Quotation).filter(Quotation.id == quote_id).first()
    if not quote:
        return JSONResponse(status_code=404, content={"detail": "Teklif bulunamadı."})
    # DRAFT (iç teklif) veya PENDING (distribütör siparişi) onaylanabilir → CONFIRMED.
    if quote.status not in ("DRAFT", "PENDING"):
        return JSONResponse(status_code=400, content={
            "detail": f"Bu teklif zaten {quote.status} durumunda — tekrar onaylanamaz."
        })
    if not quote.items:
        return JSONResponse(status_code=400, content={"detail": "Teklifte hiç kalem yok."})

    # ── Stage 1: pre-flight stock check (no mutation yet) ──────────────────
    issues = []
    item_lookup = {}
    for line in quote.items:
        item = db.query(Item).filter(Item.id == line.item_id).with_for_update().first()
        if not item or item.is_active is False:
            issues.append(f"Ürün artık mevcut değil veya pasif (ID: {line.item_id})")
            continue
        # Defense in depth: block abstract parents (shouldn't happen via UI, but…)
        if db.query(Item).filter(Item.parent_id == item.id).count() > 0:
            issues.append(f"'{item.name}' bir ana üründür — varyasyon olarak siparişe alınamaz")
            continue
        if (item.current_stock or 0) < line.quantity:
            issues.append(
                f"'{item.name}' yetersiz stok: gereken {line.quantity} {item.unit}, "
                f"mevcut {item.current_stock} {item.unit}"
            )
            continue
        item_lookup[line.item_id] = item

    if issues:
        db.rollback()
        return JSONResponse(status_code=400, content={
            "detail": "Stok kontrolü başarısız — sipariş onaylanamadı.",
            "issues": issues,
        })

    # ── Stage 2: atomic deduction + transaction logging ───────────────────
    try:
        from datetime import datetime as _dt
        for line in quote.items:
            item = item_lookup[line.item_id]
            item.current_stock = round((item.current_stock or 0) - line.quantity, 6)
            db.add(Transaction(
                item_id=line.item_id,
                transaction_type="Output",
                quantity=line.quantity,
                notes=f"Export Sale: Quotation #{quote.quote_number} → {quote.customer_name}",
                performed_by=actor,
            ))

        quote.status       = "CONFIRMED"
        quote.confirmed_at = _dt.utcnow()
        quote.confirmed_by = actor
        db.commit()

        # ── Low-stock alert: any line that crossed its threshold queues a
        #     single notification. Primitive snapshots — the BackgroundTask
        #     fires after the response, no extra latency for the caller.
        for line in quote.items:
            item = item_lookup[line.item_id]
            if item.min_stock_level > 0 and item.current_stock <= item.min_stock_level:
                background_tasks.add_task(
                    notify_low_stock,
                    item.name, item.current_stock, item.min_stock_level, item.unit or "",
                )

        return {
            "message":         f"Sipariş onaylandı — Teklif #{quote.quote_number} stoktan düşüldü.",
            "quote_number":    quote.quote_number,
            "items_deducted":  len(quote.items),
            "confirmed_by":    actor,
        }
    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Onay sırasında hata oluştu — değişiklikler geri alındı."})


class RejectRequest(BaseModel):
    reason: Optional[str] = None


@router.post("/quotations/{quote_id}/reject")
def reject_quotation(
    quote_id: int,
    data: RejectRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("b2b", "confirm")),
):
    """Bekleyen bir distribütör siparişini reddet (PENDING → REJECTED). Stok değişmez."""
    actor = current_user.get("full_name") or current_user.get("username") or "—"
    quote = db.query(Quotation).filter(Quotation.id == quote_id).first()
    if not quote:
        return JSONResponse(status_code=404, content={"detail": "Sipariş bulunamadı."})
    if quote.status != "PENDING":
        return JSONResponse(status_code=400, content={
            "detail": "Yalnız bekleyen (distribütör) siparişleri reddedilebilir."
        })
    from datetime import datetime as _dt
    quote.status        = "REJECTED"
    quote.rejected_at   = _dt.utcnow()
    quote.rejected_by   = actor
    quote.reject_reason = (data.reason or "").strip() or None
    db.commit()
    return {"message": f"Sipariş #{quote.quote_number} reddedildi.", "quote_number": quote.quote_number}


@router.delete("/quotations/{quote_id}")
def delete_quotation(
    quote_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("b2b", "create")),
):
    """Delete a draft. CONFIRMED quotes are immutable for audit and cannot be deleted."""
    quote = db.query(Quotation).filter(Quotation.id == quote_id).first()
    if not quote:
        return JSONResponse(status_code=404, content={"detail": "Teklif bulunamadı."})
    if quote.status != "DRAFT":
        return JSONResponse(status_code=400, content={
            "detail": "Onaylanmış teklifler silinemez (denetim izi gereği)."
        })
    db.delete(quote)
    db.commit()
    return {"message": f"Taslak #{quote.quote_number} silindi."}
