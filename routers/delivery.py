"""
Teslimat (hediye / numune çıkışı) router.

Barkod okutarak market-kasası mantığıyla stok düşülen teslimatlar + imzalı belge.
Stok `Item.current_stock`'tan düşülür, her kalem için immutable Transaction(Output)
yazılır. Üretim/satış değil — üretmediğimiz için stoktan çıkış. Belge no: TES-YYYY-<id>.
"""
import io
import json
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db, to_tr, tr_now, Item, Transaction, Delivery, DeliveryItem
from core.permissions import require_permission
from core.stock_lots import consume as _lot_consume
from core.auth import require_role
from core.domain import active_domain

router = APIRouter(prefix="/api", tags=["delivery"])

_TYPE_LABELS = {"hediye": "Hediye", "numune": "Numune", "diger": "Diğer",
                "proforma": "Proforma Fatura", "kargo": "Kargo"}
_METHOD_LABELS = {"elden": "Elden Teslim", "kargo": "Kargo"}
_STATUS_LABELS = {"completed": "Tamamlandı", "pending": "Onay Bekliyor",
                  "approved": "Onaylandı", "rejected": "Reddedildi",
                  "preparing": "Hazırlanıyor", "shipped": "Kargolandı", "canceled": "İptal"}


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


def _doc_no(year: int, did: int, prefix: str = "TES") -> str:
    """Belge no: TES- (teslim) / PRF- (proforma) / KRG- (kargo)."""
    return f"{prefix}-{year}-{did:05d}"


class DeliveryLine(BaseModel):
    item_id: int
    quantity: float = Field(..., gt=0)
    unit_price: Optional[float] = Field(None, ge=0)   # proforma — birim fiyat
    weight_ml: Optional[float] = Field(None, ge=0)    # proforma — ağırlık/hacim (ml)


class DeliveryCreate(BaseModel):
    # recipient_name kargo'da OPSİYONEL; diğer türlerde endpoint gövdesinde zorunlu kılınır.
    recipient_name: Optional[str] = Field(None, max_length=150)
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


class ShipRequest(BaseModel):
    tracking_no: str = Field(..., min_length=1, max_length=100)   # stok düşümünü tetikler
    carrier: Optional[str] = Field(None, max_length=80)


class CancelRequest(BaseModel):
    reason: Optional[str] = Field(None, max_length=2000)


class TrackingLeg(BaseModel):
    label: Optional[str] = Field(None, max_length=60)        # Yerel (TR) / Global / Varış
    carrier: Optional[str] = Field(None, max_length=80)
    tracking_no: str = Field(..., min_length=1, max_length=100)


class DeliveryEditRequest(BaseModel):
    recipient_name: Optional[str] = Field(None, max_length=150)
    recipient_org: Optional[str] = Field(None, max_length=150)
    recipient_phone: Optional[str] = Field(None, max_length=40)
    note: Optional[str] = Field(None, max_length=2000)
    carrier: Optional[str] = Field(None, max_length=80)
    tracking_no: Optional[str] = Field(None, max_length=100)
    tracking_legs: Optional[List[TrackingLeg]] = Field(None, max_length=10)  # çok-bacaklı takip
    items: Optional[List[DeliveryLine]] = None   # yalnız taslak (preparing) — yeniden snapshot


def _legs_of(d) -> list:
    """tracking_legs JSON → liste; boşsa tracking_no'dan tek bacak sentezle (geriye uyum)."""
    raw = getattr(d, "tracking_legs", None)
    if raw:
        try:
            legs = json.loads(raw)
            if isinstance(legs, list):
                out = [{"label": (l.get("label") or ""), "carrier": (l.get("carrier") or ""),
                        "tracking_no": (l.get("tracking_no") or "")}
                       for l in legs if isinstance(l, dict) and (l.get("tracking_no") or "").strip()]
                if out:
                    return out
        except Exception:
            pass
    if d.tracking_no:
        return [{"label": "", "carrier": d.carrier or "", "tracking_no": d.tracking_no}]
    return []


class DeliveryError(ValueError):
    """build_delivery / ship_core iş kuralı hatası → endpoint HTTP `status_code`'a çevirir."""

    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _actor_of(current_user: dict) -> str:
    return current_user.get("full_name") or current_user.get("username") or "—"


def build_delivery(db: Session, data: DeliveryCreate, actor: str, domain: str) -> Delivery:
    """Teslimat oluştur (çekirdek): doğrulama + Delivery/DeliveryItem + belge no;
    anlık türlerde (hediye/numune/diğer) stok düşümü + Transaction(Output).

    COMMIT ÇAĞIRANA AİTTİR (flush yapılır, id/document_no hazır döner).  İş
    kuralı ihlalinde `DeliveryError(detail, status_code)` fırlatır — endpoint
    400/404'e çevirir; influencer gönderimi gibi diğer çağıranlar aynı yolu
    kullanır (yeni stok yolu YOK — defter kuralı).
    """
    if not data.items:
        raise DeliveryError("En az bir ürün okutmalısınız.")

    dtype = (data.delivery_type or "hediye").lower()
    if dtype in ("diğer", "diger"):
        dtype = "diger"
    if dtype not in ("hediye", "numune", "diger", "proforma", "kargo"):
        dtype = "diger"
    is_proforma = (dtype == "proforma")
    is_kargo = (dtype == "kargo")
    deferred = is_proforma or is_kargo       # create'te stok DÜŞMEZ (onayda/kargoda düşer)

    # Alıcı adı: kargo'da OPSİYONEL; diğer türlerde zorunlu (en az 2 karakter).
    rname = (data.recipient_name or "").strip()
    if not is_kargo and len(rname) < 2:
        raise DeliveryError("Alıcı adı zorunludur (en az 2 karakter).")

    method = "kargo" if is_kargo else (data.method or "elden").lower()
    if method not in ("elden", "kargo"):
        method = "elden"
    doc_lang = (data.doc_lang or "TR").upper()
    if doc_lang not in ("TR", "EN"):
        doc_lang = "TR"
    currency = (data.currency or "USD").upper()[:3]

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

    # Ürünleri doğrula. Ertelenmiş (proforma/kargo) → stok SONRA düşer (kilit yok);
    # anlık → şimdi düş, kilitle + stok yeterlilik kontrolü (kısmi düşüm olmasın).
    items: dict = {}
    shortages: List[str] = []
    for iid, qty in qty_by_item.items():
        q = db.query(Item).filter(Item.id == iid, Item.domain == domain)
        it = q.first() if deferred else q.with_for_update().first()
        if not it:
            raise DeliveryError(f"Ürün bulunamadı (id {iid}).", 404)
        if is_proforma and not (meta_by_item[iid]["unit_price"] and meta_by_item[iid]["unit_price"] > 0):
            raise DeliveryError(f"{it.name}: proforma için birim fiyat zorunludur.")
        if not deferred:
            stock = float(it.current_stock or 0.0)
            if qty > stock + 1e-9:
                shortages.append(f"{it.name}: stok {_fmt(stock)} {it.unit or ''}, istenen {_fmt(qty)}")
        items[iid] = it
    if shortages:
        raise DeliveryError("Yetersiz stok — " + "; ".join(shortages))

    status = "pending" if is_proforma else ("preparing" if is_kargo else "completed")
    prefix = "PRF" if is_proforma else ("KRG" if is_kargo else "TES")
    # Teslimat başlığı (recipient_name NOT NULL → kargo'da alıcı yoksa boş string)
    d = Delivery(
        recipient_name=(rname or ""),
        recipient_org=(data.recipient_org or "").strip() or None,
        recipient_phone=(data.recipient_phone or "").strip() or None,
        delivery_type=dtype,
        method=method,
        note=(data.note or "").strip() or None,
        dispatched_by=actor,
        domain=domain,
        doc_lang=doc_lang,
        status=status,
        customer_address=((data.customer_address or "").strip() or None) if is_proforma else None,
        customer_country=((data.customer_country or "").strip() or None) if is_proforma else None,
        currency=currency if is_proforma else None,
    )
    db.add(d)
    db.flush()   # id almak için
    year = (d.created_at or datetime.utcnow()).year
    d.document_no = _doc_no(year, d.id, prefix)

    # Kalem snapshot (ad belge diline göre).  Stok: ertelenmiş DEĞİL ise şimdi düş + Output.
    for iid, qty in qty_by_item.items():
        it = items[iid]
        meta = meta_by_item[iid]
        db.add(DeliveryItem(
            delivery_id=d.id, item_id=it.id, item_name=_doc_name(it, doc_lang),
            quantity=qty, unit=it.unit or "",
            unit_price=meta["unit_price"], weight_ml=meta["weight_ml"],
        ))
        if not deferred:
            # Stok + LOT birlikte düşer (core/stock_lots) — lot satırları
            # yıllarca güncellenmediği için 25 üründe sapma birikmişti.
            _lot_consume(
                db, it, qty, actor=actor,
                note=(f"Teslimat ({_TYPE_LABELS.get(dtype, dtype)}) → "
                      f"{d.recipient_name or '—'} | Belge: {d.document_no}"),
            )
    db.flush()
    return d


@router.post("/delivery", status_code=201)
def create_delivery(
    data: DeliveryCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("inventory", "adjust")),
    domain: str = Depends(active_domain),
):
    """Teslimat oluştur: stok düş + Transaction(Output) + DeliveryItem snapshot."""
    actor = _actor_of(current_user)
    try:
        d = build_delivery(db, data, actor, domain)
    except DeliveryError as e:
        return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
    db.commit()
    db.refresh(d)
    dtype = d.delivery_type

    if dtype == "proforma":
        try:
            from core.notifications import notify_proforma_pending
            notify_proforma_pending(d.document_no, d.recipient_name, actor)
        except Exception:
            pass

    return {"id": d.id, "document_no": d.document_no, "item_count": len(d.items),
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
        "tracking_no": d.tracking_no,
        "carrier": d.carrier,
        "tracking_legs": _legs_of(d),
        "shipped_at": to_tr(d.shipped_at).strftime("%d.%m.%Y %H:%M") if d.shipped_at else None,
        "shipped_by": d.shipped_by,
        "dispatched_by": d.dispatched_by,
        "note": d.note,
        "items": [{"item_id": i.item_id, "item_name": i.item_name, "quantity": i.quantity,
                   "unit": i.unit, "unit_price": i.unit_price, "weight_ml": i.weight_ml}
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
        "tracking_no": d.tracking_no,
        "carrier": d.carrier,
        "tracking_legs": _legs_of(d),
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


def _pending_shipments(db, domain):
    return (db.query(Delivery)
            .filter(Delivery.domain == domain,
                    Delivery.delivery_type == "kargo",
                    Delivery.status == "preparing")
            .order_by(Delivery.id.desc()).all())


@router.get("/delivery/shipments")
def list_pending_shipments(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("inventory", "view")),
    domain: str = Depends(active_domain),
):
    """Bekleyen kargo sevkiyatları (status='preparing') — 'elime gelecekler' kuyruğu."""
    rows = _pending_shipments(db, domain)
    return {"pending": [_view(d) for d in rows], "count": len(rows)}


@router.get("/delivery/shipments/packing-list")
def master_packing_list(
    format: str = "pdf",
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("inventory", "view")),
    domain: str = Depends(active_domain),
):
    """Tüm bekleyen kargoların toplu 'elime gelecekler' master toplama listesi (PDF)."""
    rows = _pending_shipments(db, domain)
    from core.shipment_note import render_master_packing_pdf, master_packing_filename
    from core.delivery_note import content_disposition
    try:
        content = render_master_packing_pdf([_view(d) for d in rows])
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "Liste üretilemedi."})
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition(master_packing_filename())},
    )


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


@router.put("/delivery/{delivery_id}")
def edit_delivery(
    delivery_id: int,
    data: DeliveryEditRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("inventory", "adjust")),
    domain: str = Depends(active_domain),
):
    """Teslimat/sevkiyat düzenle (STOK GÜVENLİ — stoğa asla dokunmaz).

    - kargo 'preparing' (taslak): alıcı/not + ÜRÜNLER (yeniden snapshot, stok bağlı değil).
    - kargo 'shipped' + tamamlanmış hediye/numune/diğer: yalnız META — kargo takip
      bacakları (tracking_legs), taşıyıcı, alıcı, not. ÜRÜNLER değiştirilemez (stok düştü).
    - proforma / iptal / onay-bekleyen → 400.

    NOT: Her teslimata (hediye/numune dahil) kargo takip no eklenebilir; yurtdışı için
    çok-bacaklı (yerel TR → global → varış) tracking_legs desteklenir.
    """
    d = (db.query(Delivery)
         .filter(Delivery.id == delivery_id, Delivery.domain == domain)
         .with_for_update().first())
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Teslimat bulunamadı."})
    editable = ((d.delivery_type == "kargo" and d.status in ("preparing", "shipped"))
                or (d.delivery_type in ("hediye", "numune", "diger") and d.status == "completed"))
    if not editable:
        return JSONResponse(status_code=400, content={"detail": "Bu teslimat düzenlenemez."})

    # Ürünler yalnız KARGO TASLAĞINDA değiştirilebilir (stok bağlı değil); diğerlerinde REDDET.
    if data.items is not None:
        if not (d.delivery_type == "kargo" and d.status == "preparing"):
            return JSONResponse(status_code=400,
                                content={"detail": "Kargolanmış sevkiyatın ürünleri değiştirilemez (stok düştü)."})
        if not data.items:
            return JSONResponse(status_code=400, content={"detail": "En az bir ürün olmalı."})
        qty_by_item: dict = {}
        for ln in data.items:
            qty_by_item[ln.item_id] = qty_by_item.get(ln.item_id, 0.0) + float(ln.quantity)
        resolved = {}
        for iid in qty_by_item:
            it = db.query(Item).filter(Item.id == iid, Item.domain == domain).first()
            if not it:
                return JSONResponse(status_code=404, content={"detail": f"Ürün bulunamadı (id {iid})."})
            resolved[iid] = it
        for li in list(d.items):       # eski kalemleri sil (stok DÜŞMEMİŞTİ → geri alma yok)
            db.delete(li)
        db.flush()
        for iid, qty in qty_by_item.items():
            it = resolved[iid]
            db.add(DeliveryItem(delivery_id=d.id, item_id=it.id,
                                item_name=_doc_name(it, d.doc_lang or "TR"),
                                quantity=qty, unit=it.unit or ""))

    # Meta alanları (her iki durumda da stok-güvenli)
    if data.recipient_name is not None:
        d.recipient_name = data.recipient_name.strip()
    if data.recipient_org is not None:
        d.recipient_org = data.recipient_org.strip() or None
    if data.recipient_phone is not None:
        d.recipient_phone = data.recipient_phone.strip() or None
    if data.note is not None:
        d.note = data.note.strip() or None
    if data.carrier is not None:
        d.carrier = data.carrier.strip() or None
    if data.tracking_no is not None and data.tracking_no.strip():
        d.tracking_no = data.tracking_no.strip()

    # Çok-bacaklı takip (authoritative): bacaklar verilirse primary = ilk bacak (tek-tık Takip).
    if data.tracking_legs is not None:
        legs = []
        for lg in data.tracking_legs:
            tn = (lg.tracking_no or "").strip()
            if not tn:
                continue
            legs.append({"label": (lg.label or "").strip(),
                         "carrier": (lg.carrier or "").strip(),
                         "tracking_no": tn})
        d.tracking_legs = json.dumps(legs, ensure_ascii=False) if legs else None
        if legs:
            d.tracking_no = legs[0]["tracking_no"]
            d.carrier = legs[0]["carrier"] or None
        else:
            d.tracking_no = None
            d.carrier = None

    db.commit()
    db.refresh(d)
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
    # Kargo: belge ('eksiksiz teslim alınmıştır') yalnız KARGOLANDIKTAN sonra üretilir —
    # 'hazırlanıyor'/'iptal' sevkiyat için teslim belgesi anlamsız (stok daha düşmedi).
    if d.delivery_type == "kargo" and d.status != "shipped":
        return JSONResponse(status_code=403,
                            content={"detail": "Teslim belgesi yalnızca kargolandıktan sonra üretilir."})
    from core.delivery_note import render_delivery_pdf, delivery_doc_filename, content_disposition
    try:
        content = render_delivery_pdf(_view(d))
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "Belge üretilemedi."})
    fname = delivery_doc_filename(d.document_no, d.recipient_org or d.recipient_name)
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition(fname)},
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
        _lot_consume(
            db, it, float(li.quantity), actor=actor,
            note=f"Proforma onayı → {d.recipient_name} | Belge: {d.document_no}",
        )
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
    from core.delivery_note import content_disposition
    try:
        content = render_proforma_pdf(_view(d))
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "Proforma üretilemedi."})
    fname = proforma_filename(d.document_no, d.recipient_org or d.recipient_name)
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition(fname)},
    )


# ─── Kargo / Sevkiyat: takip no atama (stok düş) + iptal + hazırlık listesi ───

def ship_core(db: Session, delivery: Delivery, tracking_no: str, carrier, actor: str) -> dict:
    """Kargo sevkiyatı çekirdeği: stok O AN düşer + Transaction(Output) + status='shipped'.

    `delivery` çağıran tarafından `with_for_update()` ile kilitlenmiş olmalı
    (idempotency: yalnız status='preparing' iken düşer; tekrar çağrı
    DeliveryError 400).  Kalemler hep-ya-hiç (kısmi düşüm olmaz).  COMMIT
    ÇAĞIRANA AİTTİR.  Döner: {id, status, document_no, tracking_no, carrier}.
    """
    d = delivery
    if d.delivery_type != "kargo":
        raise DeliveryError("Bu kayıt kargo sevkiyatı değil.")
    if d.status != "preparing":
        raise DeliveryError(f"Sevkiyat zaten {_STATUS_LABELS.get(d.status, d.status)}.")
    tracking = (tracking_no or "").strip()
    if not tracking:
        raise DeliveryError("Kargo takip numarası zorunludur.")
    domain = d.domain

    # Stok yeterlilik (kilitle + doğrula — kısmi düşüm olmasın), sonra düş.
    shortages, locked = [], {}
    for li in d.items:
        if not li.item_id:
            raise DeliveryError(f"Ürün artık sistemde yok: {li.item_name}.")
        it = (db.query(Item).filter(Item.id == li.item_id, Item.domain == domain)
              .with_for_update().first())
        if not it:
            raise DeliveryError(f"Ürün bulunamadı: {li.item_name}.")
        stock = float(it.current_stock or 0.0)
        if float(li.quantity) > stock + 1e-9:
            shortages.append(f"{it.name}: stok {_fmt(stock)} {it.unit or ''}, istenen {_fmt(li.quantity)}")
        locked[li.id] = it
    if shortages:
        raise DeliveryError("Kargolama iptal — yetersiz stok: " + "; ".join(shortages))

    for li in d.items:
        it = locked.get(li.id)
        if not it:
            continue
        _lot_consume(
            db, it, float(li.quantity), actor=actor,
            note=(f"Kargo sevkiyatı → {d.recipient_name or '—'} | "
                  f"Belge: {d.document_no} | Takip: {tracking}"),
        )
    d.status = "shipped"
    d.tracking_no = tracking
    carrier = (carrier or "").strip() or None
    d.carrier = carrier
    d.tracking_legs = json.dumps([{"label": "Yerel (TR)", "carrier": carrier or "",
                                   "tracking_no": tracking}], ensure_ascii=False)
    d.shipped_at = datetime.utcnow()
    d.shipped_by = actor
    db.flush()
    return {"id": d.id, "status": "shipped", "document_no": d.document_no,
            "tracking_no": d.tracking_no, "carrier": d.carrier}


@router.post("/delivery/{delivery_id}/ship")
def ship_delivery(
    delivery_id: int,
    body: ShipRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("inventory", "adjust")),
    domain: str = Depends(active_domain),
):
    """Kargo takip no ata → stok O AN düşer + Transaction(Output) + status='shipped'.

    Proforma 'approve' deseninin operasyonel ikizi.  İDEMPOTENT: Delivery satırı
    with_for_update ile kilitlenir, yalnız status='preparing' iken düşer; tekrar
    çağrıda 400 (çift düşüm engeli).  Kalemler hep-ya-hiç (kısmi düşüm olmaz).
    Çekirdek `ship_core` — influencer gönderimi de aynı yolu kullanır.
    """
    d = (db.query(Delivery)
         .filter(Delivery.id == delivery_id, Delivery.domain == domain)
         .with_for_update().first())
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Sevkiyat bulunamadı."})
    try:
        out = ship_core(db, d, body.tracking_no, body.carrier, _actor_of(current_user))
    except DeliveryError as e:
        return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
    db.commit()
    return out


@router.post("/delivery/{delivery_id}/cancel")
def cancel_shipment(
    delivery_id: int,
    body: CancelRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("inventory", "adjust")),
    domain: str = Depends(active_domain),
):
    """Bekleyen kargo sevkiyatını iptal et — yalnız status='preparing' (stok nötr)."""
    d = (db.query(Delivery)
         .filter(Delivery.id == delivery_id, Delivery.domain == domain)
         .with_for_update().first())
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Sevkiyat bulunamadı."})
    if d.delivery_type != "kargo":
        return JSONResponse(status_code=400, content={"detail": "Bu kayıt kargo sevkiyatı değil."})
    if d.status != "preparing":
        return JSONResponse(status_code=400,
                            content={"detail": "Yalnız hazırlanıyor durumundaki sevkiyat iptal edilebilir."})
    actor = current_user.get("full_name") or current_user.get("username") or "—"
    d.status = "canceled"
    d.reject_reason = (body.reason or "").strip() or None
    d.shipped_by = actor          # iptal edeni audit için sakla
    db.commit()
    return {"id": d.id, "status": "canceled", "document_no": d.document_no}


@router.get("/delivery/{delivery_id}/packing-list")
def packing_list(
    delivery_id: int,
    format: str = "pdf",
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("inventory", "view")),
    domain: str = Depends(active_domain),
):
    """Tek sevkiyat için hazırlık / paketleme listesi (PDF)."""
    d = db.query(Delivery).filter(Delivery.id == delivery_id, Delivery.domain == domain).first()
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Sevkiyat bulunamadı."})
    from core.shipment_note import render_packing_list_pdf, packing_filename
    from core.delivery_note import content_disposition
    try:
        content = render_packing_list_pdf(_view(d))
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "Liste üretilemedi."})
    fname = packing_filename(d.document_no, d.recipient_org or d.recipient_name)
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition(fname)},
    )
