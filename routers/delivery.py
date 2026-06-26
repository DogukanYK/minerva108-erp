"""
Teslimat (hediye / numune çıkışı) router.

Barkod okutarak market-kasası mantığıyla stok düşülen teslimatlar + imzalı belge.
Stok `Item.current_stock`'tan düşülür, her kalem için immutable Transaction(Output)
yazılır. Üretim/satış değil — üretmediğimiz için stoktan çıkış. Belge no: TES-YYYY-<id>.
"""
import io
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db, to_tr, tr_now, Item, Transaction, Delivery, DeliveryItem
from core.permissions import require_permission
from core.auth import require_role
from core.domain import active_domain

router = APIRouter(prefix="/api", tags=["delivery"])

_TYPE_LABELS = {"hediye": "Hediye", "numune": "Numune", "diger": "Diğer",
                "proforma": "Proforma Fatura"}
_METHOD_LABELS = {"elden": "Elden Teslim", "kargo": "Kargo"}
_STATUS_LABELS = {"completed": "Tamamlandı", "pending": "Onay Bekliyor",
                  "approved": "Onaylandı", "rejected": "Reddedildi"}


def _doc_name(it: Item, doc_lang: str) -> str:
    """Belge dili TR ise Türkçe ad (yoksa İngilizce'ye düş); EN ise İngilizce ad."""
    if (doc_lang or "TR").upper() == "TR" and (it.name_tr or "").strip():
        return it.name_tr.strip()
    return it.name


def _fmt(n) -> str:
    try:
        f = float(n)
        return str(int(f)) if f == int(f) else f"{f:g}"
    except (TypeError, ValueError):
        return str(n)


def _doc_no(year: int, did: int, proforma: bool = False) -> str:
    return f"{'PRF' if proforma else 'TES'}-{year}-{did:05d}"


class DeliveryLine(BaseModel):
    item_id: int
    quantity: float = Field(..., gt=0)
    unit_price: Optional[float] = Field(None, ge=0)   # proforma — birim fiyat
    weight_ml: Optional[float] = Field(None, ge=0)    # proforma — ağırlık/hacim (ml)


class DeliveryCreate(BaseModel):
    recipient_name: str = Field(..., min_length=2, max_length=150)
    recipient_org: Optional[str] = Field(None, max_length=150)
    recipient_phone: Optional[str] = Field(None, max_length=40)
    delivery_type: str = Field("hediye")
    method: str = Field("elden")
    note: Optional[str] = Field(None, max_length=2000)
    doc_lang: str = Field("TR")
    # Proforma alanları (yalnız delivery_type='proforma')
    customer_address: Optional[str] = Field(None, max_length=2000)
    customer_country: Optional[str] = Field(None, max_length=100)
    currency: str = Field("USD", max_length=3)
    items: List[DeliveryLine]


class RejectRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=2000)


@router.post("/delivery", status_code=201)
def create_delivery(
    data: DeliveryCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("inventory", "adjust")),
    domain: str = Depends(active_domain),
):
    """Teslimat oluştur: stok düş + Transaction(Output) + DeliveryItem snapshot."""
    if not data.items:
        return JSONResponse(status_code=400, content={"detail": "En az bir ürün okutmalısınız."})

    dtype = (data.delivery_type or "hediye").lower()
    if dtype in ("diğer", "diger"):
        dtype = "diger"
    if dtype not in ("hediye", "numune", "diger", "proforma"):
        dtype = "diger"
    method = (data.method or "elden").lower()
    if method not in ("elden", "kargo"):
        method = "elden"
    doc_lang = (data.doc_lang or "TR").upper()
    if doc_lang not in ("TR", "EN"):
        doc_lang = "TR"
    is_proforma = (dtype == "proforma")
    currency = (data.currency or "USD").upper()[:3]
    actor = current_user.get("full_name") or current_user.get("username") or "—"

    # Aynı ürün birden çok satırda gelirse miktarı birleştir; fiyat/ağırlık son değer (proforma)
    qty_by_item: dict = {}
    meta_by_item: dict = {}
    for ln in data.items:
        qty_by_item[ln.item_id] = qty_by_item.get(ln.item_id, 0.0) + float(ln.quantity)
        m = meta_by_item.setdefault(ln.item_id, {"unit_price": None, "weight_ml": None})
        if ln.unit_price is not None:
            m["unit_price"] = float(ln.unit_price)
        if ln.weight_ml is not None:
            m["weight_ml"] = float(ln.weight_ml)

    # Ürünleri doğrula. Proforma'da stok ONAYDA düşer (kilitlemeye gerek yok);
    # diğerlerinde şimdi düşer → kilitle + stok yeterlilik kontrolü (kısmi düşüm olmasın).
    items: dict = {}
    shortages: List[str] = []
    for iid, qty in qty_by_item.items():
        q = db.query(Item).filter(Item.id == iid, Item.domain == domain)
        it = q.first() if is_proforma else q.with_for_update().first()
        if not it:
            return JSONResponse(status_code=404, content={"detail": f"Ürün bulunamadı (id {iid})."})
        if is_proforma and not (meta_by_item[iid]["unit_price"] and meta_by_item[iid]["unit_price"] > 0):
            return JSONResponse(status_code=400,
                                content={"detail": f"{it.name}: proforma için birim fiyat zorunludur."})
        if not is_proforma:
            stock = float(it.current_stock or 0.0)
            if qty > stock + 1e-9:
                shortages.append(f"{it.name}: stok {_fmt(stock)} {it.unit or ''}, istenen {_fmt(qty)}")
        items[iid] = it
    if shortages:
        return JSONResponse(status_code=400, content={
            "detail": "Yetersiz stok — " + "; ".join(shortages)})

    # Teslimat başlığı
    d = Delivery(
        recipient_name=data.recipient_name.strip(),
        recipient_org=(data.recipient_org or "").strip() or None,
        recipient_phone=(data.recipient_phone or "").strip() or None,
        delivery_type=dtype,
        method=method,
        note=(data.note or "").strip() or None,
        dispatched_by=actor,
        domain=domain,
        doc_lang=doc_lang,
        status="pending" if is_proforma else "completed",
        customer_address=((data.customer_address or "").strip() or None) if is_proforma else None,
        customer_country=((data.customer_country or "").strip() or None) if is_proforma else None,
        currency=currency if is_proforma else None,
    )
    db.add(d)
    db.flush()   # id almak için
    year = (d.created_at or datetime.utcnow()).year
    d.document_no = _doc_no(year, d.id, is_proforma)

    # Kalem snapshot (ad belge diline göre).  Stok: proforma DEĞİL ise şimdi düş + Output.
    for iid, qty in qty_by_item.items():
        it = items[iid]
        meta = meta_by_item[iid]
        db.add(DeliveryItem(
            delivery_id=d.id, item_id=it.id, item_name=_doc_name(it, doc_lang),
            quantity=qty, unit=it.unit or "",
            unit_price=meta["unit_price"], weight_ml=meta["weight_ml"],
        ))
        if not is_proforma:
            it.current_stock = round(float(it.current_stock or 0.0) - qty, 6)
            db.add(Transaction(
                item_id=it.id, transaction_type="Output", quantity=qty,
                notes=(f"Teslimat ({_TYPE_LABELS.get(dtype, dtype)}) → "
                       f"{d.recipient_name} | Belge: {d.document_no}"),
                performed_by=actor,
            ))
    db.commit()
    db.refresh(d)

    if is_proforma:
        try:
            from core.notifications import notify_proforma_pending
            notify_proforma_pending(d.document_no, d.recipient_name, actor)
        except Exception:
            pass

    return {"id": d.id, "document_no": d.document_no, "item_count": len(qty_by_item),
            "status": d.status, "delivery_type": dtype}


def _view(d) -> dict:
    return {
        "id": d.id,
        "document_no": d.document_no,
        "date": to_tr(d.created_at).strftime("%d.%m.%Y %H:%M") if d.created_at else "",
        "recipient_name": d.recipient_name,
        "recipient_org": d.recipient_org,
        "recipient_phone": d.recipient_phone,
        "delivery_type": d.delivery_type,
        "method": d.method,
        "type_label": _TYPE_LABELS.get(d.delivery_type, d.delivery_type),
        "method_label": _METHOD_LABELS.get(d.method, d.method),
        "doc_lang": (d.doc_lang or "TR"),
        "status": (d.status or "completed"),
        "status_label": _STATUS_LABELS.get(d.status or "completed", d.status or ""),
        "customer_address": d.customer_address,
        "customer_country": d.customer_country,
        "currency": d.currency or "USD",
        "approved_by": d.approved_by,
        "approved_at": to_tr(d.approved_at).strftime("%d.%m.%Y %H:%M") if d.approved_at else None,
        "reject_reason": d.reject_reason,
        "dispatched_by": d.dispatched_by,
        "note": d.note,
        "items": [{"item_name": i.item_name, "quantity": i.quantity, "unit": i.unit,
                   "unit_price": i.unit_price, "weight_ml": i.weight_ml}
                  for i in d.items],
    }


@router.get("/delivery")
def list_deliveries(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("inventory", "view")),
    domain: str = Depends(active_domain),
    limit: int = 100,
):
    rows = (db.query(Delivery).filter(Delivery.domain == domain)
            .order_by(Delivery.id.desc()).limit(min(max(limit, 1), 500)).all())
    out = [{
        "id": d.id, "document_no": d.document_no,
        "recipient_name": d.recipient_name, "recipient_org": d.recipient_org,
        "delivery_type": d.delivery_type,
        "type_label": _TYPE_LABELS.get(d.delivery_type, d.delivery_type),
        "method_label": _METHOD_LABELS.get(d.method, d.method),
        "status": (d.status or "completed"),
        "status_label": _STATUS_LABELS.get(d.status or "completed", d.status or ""),
        "item_count": len(d.items),
        "total_qty": round(sum(i.quantity for i in d.items), 4),
        "dispatched_by": d.dispatched_by,
        "date": to_tr(d.created_at).strftime("%d.%m.%Y %H:%M") if d.created_at else "",
    } for d in rows]
    return {"deliveries": out, "count": len(out)}


@router.get("/delivery/pending")
def pending_proformas(
    db: Session = Depends(get_db),
    _: dict = Depends(require_role(["SuperAdmin"])),
    domain: str = Depends(active_domain),
):
    """Onay bekleyen proformalar (yalnız SuperAdmin)."""
    rows = (db.query(Delivery)
            .filter(Delivery.domain == domain,
                    Delivery.delivery_type == "proforma",
                    Delivery.status == "pending")
            .order_by(Delivery.id.desc()).all())
    return {"pending": [_view(d) for d in rows], "count": len(rows)}


@router.get("/delivery/{delivery_id}")
def get_delivery(
    delivery_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("inventory", "view")),
    domain: str = Depends(active_domain),
):
    d = db.query(Delivery).filter(Delivery.id == delivery_id, Delivery.domain == domain).first()
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Teslimat bulunamadı."})
    return _view(d)


@router.get("/delivery/{delivery_id}/document")
def delivery_document(
    delivery_id: int,
    format: str = "pdf",
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("inventory", "view")),
    domain: str = Depends(active_domain),
):
    """İmzalı teslim belgesi (PDF)."""
    d = db.query(Delivery).filter(Delivery.id == delivery_id, Delivery.domain == domain).first()
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Teslimat bulunamadı."})
    from core.delivery_note import render_delivery_pdf, delivery_doc_filename
    try:
        content = render_delivery_pdf(_view(d))
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "Belge üretilemedi."})
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{delivery_doc_filename(d.document_no)}"'},
    )


@router.post("/delivery/{delivery_id}/approve")
def approve_proforma(
    delivery_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(["SuperAdmin"])),
    domain: str = Depends(active_domain),
):
    """Proforma onayla → stok ONAYDA düşer + Transaction(Output) + status=approved."""
    d = db.query(Delivery).filter(Delivery.id == delivery_id, Delivery.domain == domain).first()
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Proforma bulunamadı."})
    if d.delivery_type != "proforma":
        return JSONResponse(status_code=400, content={"detail": "Bu kayıt proforma değil."})
    if d.status != "pending":
        return JSONResponse(status_code=400,
                            content={"detail": f"Proforma zaten {_STATUS_LABELS.get(d.status, d.status)}."})
    actor = current_user.get("full_name") or current_user.get("username") or "—"

    # Onay anında stok yeterlilik (kilitle + doğrula — kısmi düşüm olmasın)
    shortages, locked = [], {}
    for li in d.items:
        if not li.item_id:
            return JSONResponse(status_code=400,
                                content={"detail": f"Ürün artık sistemde yok: {li.item_name}."})
        it = (db.query(Item).filter(Item.id == li.item_id, Item.domain == domain)
              .with_for_update().first())
        if not it:
            return JSONResponse(status_code=400,
                                content={"detail": f"Ürün bulunamadı: {li.item_name}."})
        stock = float(it.current_stock or 0.0)
        if float(li.quantity) > stock + 1e-9:
            shortages.append(f"{it.name}: stok {_fmt(stock)} {it.unit or ''}, istenen {_fmt(li.quantity)}")
        locked[li.id] = it
    if shortages:
        return JSONResponse(status_code=400,
                            content={"detail": "Onay iptal — yetersiz stok: " + "; ".join(shortages)})

    for li in d.items:
        it = locked.get(li.id)
        if not it:
            continue
        it.current_stock = round(float(it.current_stock or 0.0) - float(li.quantity), 6)
        db.add(Transaction(
            item_id=it.id, transaction_type="Output", quantity=float(li.quantity),
            notes=f"Proforma onayı → {d.recipient_name} | Belge: {d.document_no}",
            performed_by=actor,
        ))
    d.status = "approved"
    d.approved_by = actor
    d.approved_at = datetime.utcnow()
    db.commit()
    db.refresh(d)
    try:
        from core.notifications import notify_proforma_decision
        notify_proforma_decision(d.document_no, "approved", actor)
    except Exception:
        pass
    return {"id": d.id, "status": "approved", "document_no": d.document_no}


@router.post("/delivery/{delivery_id}/reject")
def reject_proforma(
    delivery_id: int,
    body: RejectRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role(["SuperAdmin"])),
    domain: str = Depends(active_domain),
):
    """Proforma reddet → status=rejected (stok değişmez)."""
    d = db.query(Delivery).filter(Delivery.id == delivery_id, Delivery.domain == domain).first()
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Proforma bulunamadı."})
    if d.delivery_type != "proforma":
        return JSONResponse(status_code=400, content={"detail": "Bu kayıt proforma değil."})
    if d.status != "pending":
        return JSONResponse(status_code=400,
                            content={"detail": f"Proforma zaten {_STATUS_LABELS.get(d.status, d.status)}."})
    actor = current_user.get("full_name") or current_user.get("username") or "—"
    d.status = "rejected"
    d.reject_reason = (body.reason or "").strip() or None
    d.approved_by = actor
    d.approved_at = datetime.utcnow()
    db.commit()
    try:
        from core.notifications import notify_proforma_decision
        notify_proforma_decision(d.document_no, "rejected", actor)
    except Exception:
        pass
    return {"id": d.id, "status": "rejected", "document_no": d.document_no}


@router.get("/delivery/{delivery_id}/proforma")
def proforma_document(
    delivery_id: int,
    format: str = "pdf",
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("inventory", "view")),
    domain: str = Depends(active_domain),
):
    """Onaylı proforma faturası (antetli PDF). Yalnız status=approved indirilir."""
    d = db.query(Delivery).filter(Delivery.id == delivery_id, Delivery.domain == domain).first()
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Proforma bulunamadı."})
    if d.delivery_type != "proforma":
        return JSONResponse(status_code=400, content={"detail": "Bu kayıt proforma değil."})
    if d.status != "approved":
        return JSONResponse(status_code=403,
                            content={"detail": "Proforma yalnızca onaylandıktan sonra indirilebilir."})
    from core.proforma_invoice import render_proforma_pdf, proforma_filename
    try:
        content = render_proforma_pdf(_view(d))
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "Proforma üretilemedi."})
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{proforma_filename(d.document_no)}"'},
    )
