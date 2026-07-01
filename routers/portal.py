"""
Distribütör Sipariş Portalı — DAĞITICI tarafı API. Gate: portal:* .

Distribütör kendi kataloğundan (fiyatı tanımlı ürünler) sipariş verir. Sipariş
PENDING durumunda bir Quotation olarak doğar (distributor_id set) → personel
onaylayınca (mevcut confirm akışı) stok düşer. Fiyat DAİMA sunucu tarafında
distributor_prices'tan alınır — istemciden gelen fiyat asla kullanılmaz.
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional

from database import get_db, Item, Distributor, DistributorPrice, Quotation, QuotationItem
from core.permissions import require_permission
from core.domain import active_domain
from core.notifications import notify_new_distributor_order
from core.distributor import (
    serialize_distributor, serialize_portal_product,
    serialize_portal_order_summary, serialize_portal_order_full, sellable_items,
)

router = APIRouter(prefix="/api/portal", tags=["portal"])


def _current_distributor(payload: dict, db: Session) -> Optional[Distributor]:
    """Oturumdaki kullanıcıya bağlı Distributor profili (yoksa None)."""
    uid = int(payload.get("sub", 0) or 0)
    return db.query(Distributor).filter(Distributor.user_id == uid).first()


_NO_PROFILE = JSONResponse(status_code=403, content={"detail": "Distribütör profili bulunamadı."})


class OrderLine(BaseModel):
    item_id:  int
    quantity: float


class OrderCreateRequest(BaseModel):
    items: List[OrderLine]
    notes: Optional[str] = None


@router.get("/me")
def portal_me(payload: dict = Depends(require_permission("portal", "view")),
              db: Session = Depends(get_db)):
    d = _current_distributor(payload, db)
    if not d:
        return _NO_PROFILE
    return serialize_distributor(d)


@router.get("/products")
def portal_products(payload: dict = Depends(require_permission("portal", "view")),
                    db: Session = Depends(get_db), domain: str = Depends(active_domain)):
    """Distribütörün kataloğu: aktif domain'de fiyatı tanımlı bitmiş ürünler + var/yok."""
    d = _current_distributor(payload, db)
    if not d:
        return _NO_PROFILE
    priced = {p.item_id: p.unit_price for p in
              db.query(DistributorPrice).filter(DistributorPrice.distributor_id == d.id).all()}
    products = [serialize_portal_product(it, priced[it.id], d.currency)
                for it in sellable_items(db, domain) if it.id in priced]
    return {"currency": d.currency, "domain": domain, "products": products}


@router.post("/orders", status_code=201)
def portal_create_order(data: OrderCreateRequest, background_tasks: BackgroundTasks,
                        payload: dict = Depends(require_permission("portal", "order")),
                        db: Session = Depends(get_db), domain: str = Depends(active_domain)):
    """Sipariş oluştur (PENDING). Fiyat sunucudan (distributor_prices), stok onayda düşer."""
    d = _current_distributor(payload, db)
    if not d:
        return _NO_PROFILE
    if not d.is_active:
        return JSONResponse(status_code=403, content={"detail": "Distribütör hesabınız pasif."})
    if not data.items:
        return JSONResponse(status_code=400, content={"detail": "Sipariş en az bir kalem içermelidir."})

    prices = {p.item_id: p.unit_price for p in
              db.query(DistributorPrice).filter(DistributorPrice.distributor_id == d.id).all()}
    sellable_ids = {it.id for it in sellable_items(db, domain)}

    lines, subtotal = [], 0.0
    seen = set()
    for ln in data.items:
        if ln.item_id in seen:
            return JSONResponse(status_code=400, content={"detail": "Aynı ürün birden fazla kez gönderildi."})
        seen.add(ln.item_id)
        if ln.quantity is None or ln.quantity <= 0:
            return JSONResponse(status_code=400, content={"detail": "Geçersiz miktar."})
        if ln.item_id not in prices or ln.item_id not in sellable_ids:
            return JSONResponse(status_code=400, content={
                "detail": f"Ürün siparişe uygun değil (ID {ln.item_id})."})
        item = db.query(Item).filter(Item.id == ln.item_id).first()
        unit_price = prices[ln.item_id]
        line_total = round(ln.quantity * unit_price, 4)
        subtotal += line_total
        lines.append((item, ln.quantity, unit_price, line_total))

    now = datetime.utcnow()
    q = Quotation(
        quote_number=f"TMP-{uuid.uuid4().hex}",        # geçici benzersiz → flush sonrası SIP-...
        customer_name=d.company_name, customer_contact=d.contact_name, customer_email=d.email,
        customer_phone=d.phone, customer_address=d.address, customer_country=d.country,
        customer_vat=d.vat,
        currency=d.currency, subtotal_amount=round(subtotal, 4), total_amount=round(subtotal, 4),
        notes=(data.notes or None), status="PENDING", domain=domain,
        created_by=payload.get("username"), distributor_id=d.id, submitted_at=now,
    )
    db.add(q)
    db.flush()  # q.id gerekli
    q.quote_number = f"SIP-{now.year}-{q.id:05d}"
    for item, qty, unit_price, line_total in lines:
        db.add(QuotationItem(
            quotation_id=q.id, item_id=item.id, item_name_snapshot=item.name,
            quantity=qty, unit_price_foreign=unit_price, unit_cost_try=None, line_total=line_total,
        ))
    db.commit()
    db.refresh(q)
    background_tasks.add_task(notify_new_distributor_order,
                             q.quote_number, d.company_name, q.total_amount, q.currency)
    return {"id": q.id, "order_number": q.quote_number,
            "message": f"Sipariş {q.quote_number} alındı — onay bekliyor."}


@router.get("/orders")
def portal_orders(payload: dict = Depends(require_permission("portal", "view")),
                  db: Session = Depends(get_db)):
    d = _current_distributor(payload, db)
    if not d:
        return _NO_PROFILE
    rows = (db.query(Quotation).filter(Quotation.distributor_id == d.id)
            .order_by(Quotation.id.desc()).limit(100).all())
    return [serialize_portal_order_summary(q) for q in rows]


@router.get("/orders/{order_id}")
def portal_order_detail(order_id: int,
                        payload: dict = Depends(require_permission("portal", "view")),
                        db: Session = Depends(get_db)):
    d = _current_distributor(payload, db)
    if not d:
        return _NO_PROFILE
    q = (db.query(Quotation)
         .filter(Quotation.id == order_id, Quotation.distributor_id == d.id).first())
    if not q:
        return JSONResponse(status_code=404, content={"detail": "Sipariş bulunamadı."})
    return serialize_portal_order_full(q)
