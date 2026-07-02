"""
Ürün İadesi router — çıkışı yapılmış ürünlerin stoğa geri alınması.

İki tür iade:
  • Teslimata bağlı (delivery_id): Son Teslimatlar'dan belge seçilir, kalem bazında
    KISMİ iade; aşırı-iade SUM guard'ı (iade_edilen + istenen ≤ teslim_edilen).
  • Serbest (delivery_id yok): teslimat kaydı olmayan girişler (online platform vb.).

Kalem durumu: SAĞLAM → Item.current_stock += qty + Transaction(Input,
'İade (RET-…) ← …') — teslimat Output'unun simetriği; HASARLI/AÇILMIŞ → stoğa
GİRMEZ, fire izi condition + restocked=False'ta. Belge no: RET-YYYY-NNNNN.
Transaction tipi bilinçli 'Input' (yeni tip snapshot/aylık rapor rekonstrüksiyonunu
bozar — core/snapshots.py yalnız Input/Output/Adjustment tanır).
"""
import io
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import (get_db, to_tr, Item, Transaction, Delivery, DeliveryItem,
                      ProductReturn, ProductReturnItem)
from core.permissions import require_permission
from core.domain import active_domain

router = APIRouter(prefix="/api", tags=["returns"])

_CHANNELS = ("teslimat", "online", "toplanti", "diger")
_CONDITIONS = ("saglam", "hasarli", "acilmis")
_CONDITION_LABELS = {"saglam": "Sağlam", "hasarli": "Hasarlı", "acilmis": "Açılmış"}
_CHANNEL_LABELS = {"teslimat": "Teslimat dönüşü", "online": "Online satış iadesi",
                   "toplanti": "Toplantı dönüşü", "diger": "Diğer"}
# Yalnız stoğu gerçekten düşmüş teslimatlar iade edilebilir
_RETURNABLE_STATUSES = ("completed", "approved", "shipped")


def _fmt(n) -> str:
    try:
        f = float(n)
        return str(int(f)) if f == int(f) else f"{f:g}"
    except (TypeError, ValueError):
        return str(n)


def _doc_name(it: Item, doc_lang: str) -> str:
    """Belge dili TR ise Türkçe ad (yoksa İngilizce'ye düş); EN ise İngilizce ad."""
    if (doc_lang or "TR").upper() == "TR" and (getattr(it, "name_tr", None) or "").strip():
        return it.name_tr.strip()
    return it.name


class ReturnLine(BaseModel):
    # Teslimata bağlı iadede delivery_item_id zorunlu; serbest iadede item_id zorunlu.
    delivery_item_id: Optional[int] = None
    item_id: Optional[int] = None
    quantity: float = Field(..., gt=0)
    condition: str = Field("saglam")
    note: Optional[str] = Field(None, max_length=500)


class ReturnCreate(BaseModel):
    delivery_id: Optional[int] = None                # dolu = belgeye bağlı iade
    channel: str = Field("teslimat", max_length=30)
    reason: Optional[str] = Field(None, max_length=2000)
    returned_by: Optional[str] = Field(None, max_length=150)   # iade eden kişi/platform
    doc_lang: str = Field("TR")
    items: List[ReturnLine] = Field(..., min_length=1, max_length=200)


def _returned_sums(db, delivery_item_ids) -> dict:
    """delivery_item_id → şimdiye kadar iade edilmiş toplam miktar."""
    if not delivery_item_ids:
        return {}
    rows = (db.query(ProductReturnItem.delivery_item_id,
                     func.coalesce(func.sum(ProductReturnItem.quantity), 0.0))
            .filter(ProductReturnItem.delivery_item_id.in_(list(delivery_item_ids)))
            .group_by(ProductReturnItem.delivery_item_id).all())
    return {rid: float(total or 0.0) for rid, total in rows}


def _view(r) -> dict:
    return {
        "id": r.id,
        "document_no": r.document_no,
        "date": to_tr(r.created_at).strftime("%d.%m.%Y %H:%M") if r.created_at else "",
        "delivery_id": r.delivery_id,
        "delivery_document_no": r.delivery_document_no,
        "channel": r.channel or "diger",
        "channel_label": _CHANNEL_LABELS.get(r.channel or "diger", r.channel or ""),
        "reason": r.reason,
        "returned_by": r.returned_by,
        "received_by": r.received_by,
        "doc_lang": r.doc_lang or "TR",
        "restocked_qty": round(sum(i.quantity for i in r.items if i.restocked), 4),
        "damaged_qty": round(sum(i.quantity for i in r.items if not i.restocked), 4),
        "items": [{
            "item_id": i.item_id, "item_name": i.item_name, "quantity": i.quantity,
            "unit": i.unit, "condition": i.condition,
            "condition_label": _CONDITION_LABELS.get(i.condition, i.condition),
            "restocked": bool(i.restocked), "note": i.note,
        } for i in r.items],
    }


@router.get("/returns/delivery/{delivery_id}/remaining")
def returnable_remaining(
    delivery_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("inventory", "view")),
    domain: str = Depends(active_domain),
):
    """Bir teslimatın kalem bazında iade edilebilir kalan miktarları."""
    d = db.query(Delivery).filter(Delivery.id == delivery_id, Delivery.domain == domain).first()
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Teslimat bulunamadı."})
    if d.status not in _RETURNABLE_STATUSES:
        return JSONResponse(status_code=400, content={
            "detail": "Bu teslimatın stoğu henüz düşmemiş — iade edilemez."})
    sums = _returned_sums(db, [li.id for li in d.items])
    out = []
    for li in d.items:
        returned = sums.get(li.id, 0.0)
        out.append({
            "delivery_item_id": li.id, "item_id": li.item_id, "item_name": li.item_name,
            "unit": li.unit or "", "delivered": float(li.quantity),
            "returned": round(returned, 4),
            "remaining": round(max(0.0, float(li.quantity) - returned), 4),
        })
    return {"delivery_id": d.id, "document_no": d.document_no,
            "recipient": d.recipient_org or d.recipient_name, "items": out}


@router.post("/returns", status_code=201)
def create_return(
    data: ReturnCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("inventory", "adjust")),
    domain: str = Depends(active_domain),
):
    """İade al: SAĞLAM kalemler stoğa döner (Transaction Input), hasarlılar iz olarak kalır."""
    actor = current_user.get("full_name") or current_user.get("username") or "—"
    channel = (data.channel or "diger").lower()
    if channel not in _CHANNELS:
        channel = "diger"
    doc_lang = (data.doc_lang or "TR").upper()
    if doc_lang not in ("TR", "EN"):
        doc_lang = "TR"

    for ln in data.items:
        if (ln.condition or "").lower() not in _CONDITIONS:
            return JSONResponse(status_code=400,
                                content={"detail": f"Geçersiz durum: {ln.condition} (saglam/hasarli/acilmis)."})

    d = None
    resolved = []   # (ReturnLine, DeliveryItem|None, Item|None)
    if data.delivery_id is not None:
        # ── Teslimata bağlı iade: kilitle + status + aidiyet + aşırı-iade guard ──
        d = (db.query(Delivery)
             .filter(Delivery.id == data.delivery_id, Delivery.domain == domain)
             .with_for_update().first())
        if not d:
            return JSONResponse(status_code=404, content={"detail": "Teslimat bulunamadı."})
        if d.status not in _RETURNABLE_STATUSES:
            return JSONResponse(status_code=400, content={
                "detail": "Bu teslimatın stoğu henüz düşmemiş — iade edilemez."})
        li_by_id = {li.id: li for li in d.items}
        # Aynı kaleme birden çok satır gelirse toplamı guard'a girsin
        want_by_li: dict = {}
        for ln in data.items:
            if not ln.delivery_item_id or ln.delivery_item_id not in li_by_id:
                return JSONResponse(status_code=400, content={
                    "detail": "Kalem bu teslimata ait değil (delivery_item_id)."})
            want_by_li[ln.delivery_item_id] = want_by_li.get(ln.delivery_item_id, 0.0) + float(ln.quantity)
        sums = _returned_sums(db, list(want_by_li.keys()))
        for li_id, want in want_by_li.items():
            li = li_by_id[li_id]
            already = sums.get(li_id, 0.0)
            if already + want > float(li.quantity) + 1e-9:
                kalan = max(0.0, float(li.quantity) - already)
                return JSONResponse(status_code=400, content={
                    "detail": (f"Aşırı iade — {li.item_name}: teslim {_fmt(li.quantity)}, "
                               f"iade edilmiş {_fmt(already)}, kalan {_fmt(kalan)}, istenen {_fmt(want)}.")})
        for ln in data.items:
            li = li_by_id[ln.delivery_item_id]
            it = (db.query(Item).filter(Item.id == li.item_id, Item.domain == domain)
                  .with_for_update().first()) if li.item_id else None
            resolved.append((ln, li, it))
    else:
        # ── Serbest iade: item_id zorunlu ──
        for ln in data.items:
            if not ln.item_id:
                return JSONResponse(status_code=400, content={
                    "detail": "Serbest iadede her kalem için ürün (item_id) zorunludur."})
            it = (db.query(Item).filter(Item.id == ln.item_id, Item.domain == domain)
                  .with_for_update().first())
            if not it:
                return JSONResponse(status_code=404,
                                    content={"detail": f"Ürün bulunamadı (id {ln.item_id})."})
            resolved.append((ln, None, it))

    # ── Başlık ──
    r = ProductReturn(
        delivery_id=d.id if d else None,
        delivery_document_no=d.document_no if d else None,
        channel=channel,
        reason=(data.reason or "").strip() or None,
        returned_by=(data.returned_by or "").strip() or None,
        received_by=actor,
        doc_lang=doc_lang,
        domain=domain,
    )
    db.add(r)
    db.flush()
    year = (r.created_at or datetime.utcnow()).year
    r.document_no = f"RET-{year}-{r.id:05d}"

    # ── Kalemler: yalnız SAĞLAM stok + Transaction(Input); hasarlı sadece iz ──
    src = d.document_no if d else _CHANNEL_LABELS.get(channel, channel)
    restocked_n, damaged_n = 0, 0
    for ln, li, it in resolved:
        cond = (ln.condition or "saglam").lower()
        ok = (cond == "saglam")
        if li is not None:
            name = li.item_name
            unit = li.unit or ""
        else:
            name = _doc_name(it, doc_lang)
            unit = it.unit or ""
        qty = float(ln.quantity)
        db.add(ProductReturnItem(
            return_id=r.id,
            delivery_item_id=li.id if li else None,
            item_id=(li.item_id if li else it.id),
            item_name=name, quantity=qty, unit=unit,
            condition=cond, restocked=(ok and it is not None),
            note=(ln.note or "").strip() or None,
        ))
        if ok and it is not None:
            it.current_stock = round(float(it.current_stock or 0.0) + qty, 6)
            db.add(Transaction(
                item_id=it.id, transaction_type="Input", quantity=qty,
                notes=f"İade ({r.document_no}) ← {src} | {_CONDITION_LABELS[cond]}",
                performed_by=actor,
            ))
            restocked_n += 1
        else:
            damaged_n += 1

    db.commit()
    db.refresh(r)

    try:
        from core.notifications import notify_return_created
        notify_return_created(r.document_no, src, restocked_n, damaged_n, actor)
    except Exception:
        pass

    return {"id": r.id, "document_no": r.document_no,
            "restocked_items": restocked_n, "damaged_items": damaged_n}


@router.get("/returns")
def list_returns(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("inventory", "view")),
    domain: str = Depends(active_domain),
    limit: int = 100,
):
    rows = (db.query(ProductReturn).filter(ProductReturn.domain == domain)
            .order_by(ProductReturn.id.desc()).limit(min(max(limit, 1), 500)).all())
    return {"returns": [_view(r) for r in rows], "count": len(rows)}


@router.get("/returns/{return_id}")
def get_return(
    return_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("inventory", "view")),
    domain: str = Depends(active_domain),
):
    r = (db.query(ProductReturn)
         .filter(ProductReturn.id == return_id, ProductReturn.domain == domain).first())
    if not r:
        return JSONResponse(status_code=404, content={"detail": "İade bulunamadı."})
    return _view(r)


@router.get("/returns/{return_id}/document")
def return_document(
    return_id: int,
    format: str = "pdf",
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("inventory", "view")),
    domain: str = Depends(active_domain),
):
    """Ürün iade belgesi (antetli A4 PDF)."""
    r = (db.query(ProductReturn)
         .filter(ProductReturn.id == return_id, ProductReturn.domain == domain).first())
    if not r:
        return JSONResponse(status_code=404, content={"detail": "İade bulunamadı."})
    from core.return_note import render_return_pdf, return_doc_filename
    from core.delivery_note import content_disposition
    try:
        content = render_return_pdf(_view(r))
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "Belge üretilemedi."})
    fname = return_doc_filename(r.document_no, r.returned_by)
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition(fname)},
    )
