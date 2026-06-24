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

from database import get_db, to_tr, Item, Transaction, Delivery, DeliveryItem
from core.permissions import require_permission
from core.domain import active_domain

router = APIRouter(prefix="/api", tags=["delivery"])

_TYPE_LABELS = {"hediye": "Hediye", "numune": "Numune", "diger": "Diğer"}
_METHOD_LABELS = {"elden": "Elden Teslim", "kargo": "Kargo"}


def _fmt(n) -> str:
    try:
        f = float(n)
        return str(int(f)) if f == int(f) else f"{f:g}"
    except (TypeError, ValueError):
        return str(n)


def _doc_no(year: int, did: int) -> str:
    return f"TES-{year}-{did:05d}"


class DeliveryLine(BaseModel):
    item_id: int
    quantity: float = Field(..., gt=0)


class DeliveryCreate(BaseModel):
    recipient_name: str = Field(..., min_length=2, max_length=150)
    recipient_org: Optional[str] = Field(None, max_length=150)
    recipient_phone: Optional[str] = Field(None, max_length=40)
    delivery_type: str = Field("hediye")
    method: str = Field("elden")
    note: Optional[str] = Field(None, max_length=2000)
    items: List[DeliveryLine]


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
    if dtype not in ("hediye", "numune", "diger"):
        dtype = "diger"
    method = (data.method or "elden").lower()
    if method not in ("elden", "kargo"):
        method = "elden"
    actor = current_user.get("full_name") or current_user.get("username") or "—"

    # Aynı ürün birden çok satırda gelirse miktarları birleştir (market kasası: çok okutma)
    qty_by_item: dict = {}
    for ln in data.items:
        qty_by_item[ln.item_id] = qty_by_item.get(ln.item_id, 0.0) + float(ln.quantity)

    # Önce hepsini kilitle + stok doğrula (kısmi düşüm olmasın)
    items: dict = {}
    shortages: List[str] = []
    for iid, qty in qty_by_item.items():
        it = (db.query(Item)
              .filter(Item.id == iid, Item.domain == domain)
              .with_for_update().first())
        if not it:
            return JSONResponse(status_code=404, content={"detail": f"Ürün bulunamadı (id {iid})."})
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
    )
    db.add(d)
    db.flush()   # id almak için
    year = (d.created_at or datetime.utcnow()).year
    d.document_no = _doc_no(year, d.id)

    # Stok düş + kalem + immutable Output kaydı
    for iid, qty in qty_by_item.items():
        it = items[iid]
        it.current_stock = round(float(it.current_stock or 0.0) - qty, 6)
        db.add(DeliveryItem(
            delivery_id=d.id, item_id=it.id, item_name=it.name,
            quantity=qty, unit=it.unit or "",
        ))
        db.add(Transaction(
            item_id=it.id, transaction_type="Output", quantity=qty,
            notes=(f"Teslimat ({_TYPE_LABELS.get(dtype, dtype)}) → "
                   f"{d.recipient_name} | Belge: {d.document_no}"),
            performed_by=actor,
        ))
    db.commit()
    db.refresh(d)
    return {"id": d.id, "document_no": d.document_no, "item_count": len(qty_by_item)}


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
        "dispatched_by": d.dispatched_by,
        "note": d.note,
        "items": [{"item_name": i.item_name, "quantity": i.quantity, "unit": i.unit}
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
        "type_label": _TYPE_LABELS.get(d.delivery_type, d.delivery_type),
        "method_label": _METHOD_LABELS.get(d.method, d.method),
        "item_count": len(d.items),
        "total_qty": round(sum(i.quantity for i in d.items), 4),
        "dispatched_by": d.dispatched_by,
        "date": to_tr(d.created_at).strftime("%d.%m.%Y %H:%M") if d.created_at else "",
    } for d in rows]
    return {"deliveries": out, "count": len(out)}


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
