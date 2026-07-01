"""Distribütör sipariş portalı — serializer'lar.

Portal (distribütör tarafı) çıktıları maliyet ve kesin stok İÇERMEZ:
ürün için yalnız distribütöre-özel birim fiyat + var/yok bilgisi döner.
"""
from typing import Optional

from database import to_tr, Distributor, Quotation, Item


def sellable_items(db, domain):
    """Bir domain'de satılabilir bitmiş ürünler: aktif, kategori 'Bitmiş Ürün',
    soyut ana ürün (alt varyasyonu olan) HARİÇ. Teklif builder ile aynı ölçüt."""
    rows = (db.query(Item)
              .filter(Item.is_active.isnot(False),
                      Item.domain == domain,
                      Item.category == "Bitmiş Ürün")
              .order_by(Item.name).all())
    parent_ids = {pid for (pid,) in
                  db.query(Item.parent_id).filter(Item.parent_id.isnot(None)).all()}
    return [it for it in rows if it.id not in parent_ids]


def serialize_distributor(d: Distributor, order_count: Optional[int] = None) -> dict:
    """Personel tarafı — distribütör hesabı özeti."""
    out = {
        "id":           d.id,
        "user_id":      d.user_id,
        "username":     d.user.username if d.user else None,
        "company_name": d.company_name,
        "contact_name": d.contact_name,
        "email":        d.email,
        "phone":        d.phone,
        "address":      d.address,
        "country":      d.country,
        "vat":          d.vat,
        "currency":     d.currency,
        "is_active":    bool(d.is_active),
        "created_at":   to_tr(d.created_at).strftime("%d.%m.%Y") if d.created_at else "",
    }
    if order_count is not None:
        out["order_count"] = order_count
    return out


def serialize_portal_product(item, unit_price: float, currency: str) -> dict:
    """Distribütör kataloğu satırı — fiyat + var/yok. Maliyet ve kesin stok YOK."""
    return {
        "item_id":    item.id,
        "name":       item.name,
        "name_tr":    item.name_tr,
        "sku":        item.sku,
        "category":   item.category,
        "unit":       item.unit,
        "unit_price": round(unit_price, 4),
        "currency":   currency,
        "available":  (item.current_stock or 0) > 0,   # var / yok — kesin adet gizli
    }


def serialize_portal_order_summary(q: Quotation) -> dict:
    """Distribütörün kendi siparişi — özet (durum + tutar)."""
    stamped = q.submitted_at or q.created_at
    return {
        "id":            q.id,
        "order_number":  q.quote_number,
        "status":        q.status,       # PENDING / CONFIRMED / REJECTED
        "currency":      q.currency,
        "total_amount":  q.total_amount,
        "item_count":    len(q.items),
        "created_at":    to_tr(stamped).strftime("%d.%m.%Y %H:%M") if stamped else "",
        "confirmed_at":  to_tr(q.confirmed_at).strftime("%d.%m.%Y %H:%M") if q.confirmed_at else None,
        "rejected_at":   to_tr(q.rejected_at).strftime("%d.%m.%Y %H:%M") if q.rejected_at else None,
        "reject_reason": q.reject_reason,
    }


def serialize_portal_order_full(q: Quotation) -> dict:
    """Distribütörün kendi siparişi — kalemlerle. Maliyet alanı YOK."""
    return {
        **serialize_portal_order_summary(q),
        "notes":           q.notes,
        "subtotal_amount": q.subtotal_amount,
        "shipping_amount": q.shipping_amount,
        "tax_amount":      q.tax_amount,
        "items": [
            {
                "item_id":    i.item_id,
                "name":       i.item_name_snapshot,
                "quantity":   i.quantity,
                "unit_price": i.unit_price_foreign,   # distribütörün gördüğü fiyat
                "line_total": i.line_total,
            }
            for i in q.items
        ],
    }
