"""
Distribütör hesap + fiyat yönetimi (PERSONEL tarafı). Gate: distributors:* .

Distribütör = role='Distributor' bir User'a 1:1 bağlı Distributor profili.
Fiyat listesi (DistributorPrice) aynı zamanda o distribütörün kataloğudur:
fiyatı olmayan ürün portalda görünmez / sipariş edilemez.
"""
import bcrypt as _bcrypt
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional, List

from database import get_db, User, Distributor, DistributorPrice, Quotation
from core.permissions import require_permission
from core.password_strength import validate_password_strength
from core.audit import log_admin_event
from core.domain import active_domain
from core.distributor import serialize_distributor, sellable_items

router = APIRouter(prefix="/api/distributors", tags=["distributors"])

_CURRENCIES = ("TRY", "USD", "EUR")


# ─── Şemalar ─────────────────────────────────────────────────────────────────

class DistributorCreateRequest(BaseModel):
    username:     str = Field(..., min_length=3, max_length=50)
    password:     str
    company_name: str
    contact_name: Optional[str] = None
    email:        Optional[str] = None
    phone:        Optional[str] = None
    address:      Optional[str] = None
    country:      Optional[str] = None
    vat:          Optional[str] = None
    currency:     str = "TRY"


class DistributorUpdateRequest(BaseModel):
    company_name: Optional[str] = None
    contact_name: Optional[str] = None
    email:        Optional[str] = None
    phone:        Optional[str] = None
    address:      Optional[str] = None
    country:      Optional[str] = None
    vat:          Optional[str] = None
    currency:     Optional[str] = None
    is_active:    Optional[bool] = None


class ResetPasswordRequest(BaseModel):
    new_password: str


class PriceRow(BaseModel):
    item_id:    int
    unit_price: Optional[float] = None   # None / 0 → fiyatı sil (katalogdan çıkar)


class PricesUpdateRequest(BaseModel):
    prices: List[PriceRow]


# ─── Hesap yönetimi ──────────────────────────────────────────────────────────

@router.get("")
def list_distributors(db: Session = Depends(get_db),
                      _: dict = Depends(require_permission("distributors", "view"))):
    out = []
    for d in db.query(Distributor).order_by(Distributor.company_name).all():
        oc = db.query(Quotation).filter(Quotation.distributor_id == d.id).count()
        out.append(serialize_distributor(d, order_count=oc))
    return out


@router.post("", status_code=201)
def create_distributor(request: Request, data: DistributorCreateRequest,
                       db: Session = Depends(get_db),
                       current_user: dict = Depends(require_permission("distributors", "create"))):
    if data.currency not in _CURRENCIES:
        return JSONResponse(status_code=422, content={"detail": "Geçersiz para birimi (TRY/USD/EUR)."})
    if not (data.company_name or "").strip():
        return JSONResponse(status_code=422, content={"detail": "Firma adı zorunludur."})
    pw_ok, pw_err = validate_password_strength(data.password)
    if not pw_ok:
        return JSONResponse(status_code=422, content={"detail": pw_err})
    if db.query(User).filter(User.username == data.username).first():
        return JSONResponse(status_code=409, content={
            "detail": f"'{data.username}' kullanıcı adı zaten kullanılıyor."})

    pw_hash = _bcrypt.hashpw(data.password.encode(), _bcrypt.gensalt()).decode()
    user = User(username=data.username.strip(), full_name=data.company_name.strip(),
                password_hash=pw_hash, role="Distributor")
    db.add(user)
    db.flush()  # user.id gerekli
    dist = Distributor(
        user_id=user.id, company_name=data.company_name.strip(),
        contact_name=data.contact_name, email=data.email, phone=data.phone,
        address=data.address, country=data.country, vat=data.vat,
        currency=data.currency,
    )
    db.add(dist)
    db.commit()
    db.refresh(dist)
    log_admin_event(db, request, current_user, action="distributor.create",
                    target_type="distributor", target_id=dist.id, target_name=dist.company_name,
                    details={"username": user.username, "currency": dist.currency})
    return {"id": dist.id, "message": f"Distribütör '{data.company_name}' oluşturuldu."}


@router.put("/{dist_id}")
def update_distributor(dist_id: int, request: Request, data: DistributorUpdateRequest,
                       db: Session = Depends(get_db),
                       current_user: dict = Depends(require_permission("distributors", "edit"))):
    d = db.query(Distributor).filter(Distributor.id == dist_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Distribütör bulunamadı."})
    if data.currency is not None and data.currency not in _CURRENCIES:
        return JSONResponse(status_code=422, content={"detail": "Geçersiz para birimi (TRY/USD/EUR)."})
    for f in ("company_name", "contact_name", "email", "phone", "address", "country", "vat", "currency"):
        v = getattr(data, f)
        if v is not None:
            setattr(d, f, v)
    if data.is_active is not None:
        d.is_active = data.is_active
        u = db.query(User).filter(User.id == d.user_id).first()
        if u:                       # hesabı da (de)aktive et → giriş engellenir
            u.is_active = data.is_active
    db.commit()
    log_admin_event(db, request, current_user, action="distributor.update",
                    target_type="distributor", target_id=d.id, target_name=d.company_name)
    return {"message": "Distribütör güncellendi."}


@router.put("/{dist_id}/reset-password")
def reset_distributor_password(dist_id: int, request: Request, data: ResetPasswordRequest,
                               db: Session = Depends(get_db),
                               current_user: dict = Depends(require_permission("distributors", "edit"))):
    d = db.query(Distributor).filter(Distributor.id == dist_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Distribütör bulunamadı."})
    pw_ok, pw_err = validate_password_strength(data.new_password)
    if not pw_ok:
        return JSONResponse(status_code=422, content={"detail": pw_err})
    u = db.query(User).filter(User.id == d.user_id).first()
    if not u:
        return JSONResponse(status_code=404, content={"detail": "Bağlı kullanıcı bulunamadı."})
    u.password_hash = _bcrypt.hashpw(data.new_password.encode(), _bcrypt.gensalt()).decode()
    u.failed_login_attempts = 0
    u.lockout_until = None
    db.commit()
    log_admin_event(db, request, current_user, action="distributor.reset_password",
                    target_type="distributor", target_id=d.id, target_name=d.company_name)
    return {"message": "Şifre güncellendi."}


# ─── Fiyat listesi (= katalog) ───────────────────────────────────────────────

@router.get("/{dist_id}/prices")
def get_distributor_prices(dist_id: int, db: Session = Depends(get_db),
                           _: dict = Depends(require_permission("distributors", "prices")),
                           domain: str = Depends(active_domain)):
    d = db.query(Distributor).filter(Distributor.id == dist_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Distribütör bulunamadı."})
    priced = {p.item_id: p.unit_price for p in
              db.query(DistributorPrice).filter(DistributorPrice.distributor_id == dist_id).all()}
    items = []
    for it in sellable_items(db, domain):
        items.append({
            "item_id":    it.id,
            "name":       it.name,
            "name_tr":    it.name_tr,
            "sku":        it.sku,
            "unit":       it.unit,
            "unit_price": priced.get(it.id),   # None → fiyat tanımsız (katalogda yok)
        })
    return {"distributor_id": d.id, "company_name": d.company_name,
            "currency": d.currency, "domain": domain, "items": items}


@router.put("/{dist_id}/prices")
def set_distributor_prices(dist_id: int, request: Request, data: PricesUpdateRequest,
                           db: Session = Depends(get_db),
                           current_user: dict = Depends(require_permission("distributors", "prices"))):
    d = db.query(Distributor).filter(Distributor.id == dist_id).first()
    if not d:
        return JSONResponse(status_code=404, content={"detail": "Distribütör bulunamadı."})
    now = datetime.utcnow()
    changed = 0
    for row in data.prices:
        existing = (db.query(DistributorPrice)
                    .filter(DistributorPrice.distributor_id == dist_id,
                            DistributorPrice.item_id == row.item_id).first())
        if row.unit_price is None or row.unit_price <= 0:
            if existing:                       # fiyatı sil → katalogdan çıkar
                db.delete(existing); changed += 1
            continue
        if existing:
            existing.unit_price = round(row.unit_price, 4); existing.updated_at = now
        else:
            db.add(DistributorPrice(distributor_id=dist_id, item_id=row.item_id,
                                    unit_price=round(row.unit_price, 4),
                                    created_at=now, updated_at=now))
        changed += 1
    db.commit()
    log_admin_event(db, request, current_user, action="distributor.set_prices",
                    target_type="distributor", target_id=d.id, target_name=d.company_name,
                    details={"changed": changed})
    return {"message": f"{changed} ürün fiyatı güncellendi.", "changed": changed}
