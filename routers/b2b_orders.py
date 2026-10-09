# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
B2B sipariş akışı router'ı — /api/b2b-orders.

İş kuralları core/b2b_orders.py'de; burada yetki + panel + tek commit + bildirim.
  • Her uç iç kullanıcıya açık (Distributor 403) ve aktif panelle sınırlı.
  • Fiyat / banka / ödeme tutarları yalnız `b2b.view` sahibine döner (teknik
    görünümde satış fiyatı yok).  Ticari revizyon ve proforma da b2b.view ister.
  • İmza: şifre önce giriş kilit sayaçlarıyla doğrulanır (ayrı commit), sonra
    imza + durum geçişi tek commit.
  • Hata → rollback + {"detail", "code", ...}.  Sıradaki adımın sahibine push.
"""
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core import b2b_orders as b2b
from core import bank_accounts, proforma_template
from core.b2b_proforma import proforma_filename, render_order_proforma
from core.b2b_technical_sheet import render_technical_sheet, sheet_doc, technical_sheet_filename
from core.proforma_template import ProformaTermsIn
from core.delivery_note import content_disposition
from core.domain import active_domain
from core.notifications import notify_b2b_step
from core.permissions import _resolve_permissions, require_any_permission, require_internal_user
from database import B2BOrder, BankProfile, BankRule, User, get_db

router = APIRouter(prefix="/api/b2b-orders", tags=["b2b-orders"])


# ─── Gövdeler ────────────────────────────────────────────────────────────────

class ConvertBody(BaseModel):
    label_language: str = Field("EN", max_length=8)
    target_date: Optional[str] = Field(None, max_length=20)
    payment_terms: str = Field("prepaid", max_length=12)
    advance_percent: Optional[float] = None
    bank_profile_id: Optional[int] = None                     # eski tek-banka gövdesi
    bank_profile_ids: Optional[List[int]] = Field(None, max_length=10)
    terms: Optional[ProformaTermsIn] = None
    technical_user_id: int
    signer_user_id: int


class LineIn(BaseModel):
    item_id: int
    quantity: float
    unit_price: float


class ReviseBody(BaseModel):
    commercial_revision: int
    lines: Optional[List[LineIn]] = Field(None, max_length=500)
    customer_name: Optional[str] = Field(None, max_length=150)
    customer_contact: Optional[str] = Field(None, max_length=150)
    customer_email: Optional[str] = Field(None, max_length=150)
    customer_phone: Optional[str] = Field(None, max_length=50)
    customer_address: Optional[str] = Field(None, max_length=500)
    customer_country: Optional[str] = Field(None, max_length=100)
    customer_vat: Optional[str] = Field(None, max_length=50)
    notes: Optional[str] = Field(None, max_length=2000)
    tax_percentage: Optional[float] = None
    shipping_amount: Optional[float] = None
    payment_terms: Optional[str] = Field(None, max_length=12)
    advance_percent: Optional[float] = None
    label_language: Optional[str] = Field(None, max_length=8)
    target_date: Optional[str] = Field(None, max_length=20)
    bank_profile_id: Optional[int] = None                     # eski tek-banka gövdesi
    bank_profile_ids: Optional[List[int]] = Field(None, max_length=10)
    terms: Optional[ProformaTermsIn] = None


class ReassignBody(BaseModel):
    technical_user_id: Optional[int] = None
    signer_user_id: Optional[int] = None


class ReviewLine(BaseModel):
    item_id: int
    use_stock_quantity: float = 0
    recipe_id: Optional[int] = None


class ReviewBody(BaseModel):
    commercial_revision: int
    lines: List[ReviewLine] = Field(..., min_length=1, max_length=500)
    note: Optional[str] = Field(None, max_length=4000)


class SignBody(BaseModel):
    commercial_revision: int
    technical_revision: int
    document_hash: str = Field(..., max_length=64)
    password: str = Field(..., min_length=1, max_length=200)
    signature: str = Field(..., max_length=450_000)


class PaymentBody(BaseModel):
    amount: float
    currency: Optional[str] = Field(None, max_length=3)
    received_on: Optional[str] = Field(None, max_length=20)
    reference: Optional[str] = Field(None, max_length=150)
    note: Optional[str] = Field(None, max_length=2000)


class NoteBody(BaseModel):
    note: Optional[str] = Field(None, max_length=2000)


class PurchaseLineBody(BaseModel):
    status: Optional[str] = Field(None, max_length=12)
    ordered_quantity: Optional[float] = None
    received_quantity: Optional[float] = None
    supplier_name: Optional[str] = Field(None, max_length=150)
    note: Optional[str] = Field(None, max_length=2000)


class BatchStartBody(BaseModel):
    item_id: int
    quantity: float


class BatchCompleteBody(BaseModel):
    produced_quantity: float
    witness_quantity: float = 0


class ReasonBody(BaseModel):
    reason: str = Field(..., max_length=2000)


class BatchReturnBody(BaseModel):
    item_id: int
    quantity: float
    reason: str = Field(..., max_length=2000)


class ShipBody(BaseModel):
    tracking_no: str = Field(..., min_length=1, max_length=100)
    carrier: Optional[str] = Field(None, max_length=80)


class BankBody(BaseModel):
    label: str = Field(..., min_length=1, max_length=80)
    bank_name: str = Field(..., min_length=1, max_length=150)
    branch: Optional[str] = Field(None, max_length=150)
    swift: Optional[str] = Field(None, max_length=20)
    account_holder: str = Field(..., min_length=1, max_length=200)
    iban_usd: Optional[str] = Field(None, max_length=40)
    iban_eur: Optional[str] = Field(None, max_length=40)
    iban_try: Optional[str] = Field(None, max_length=40)
    iban_rub: Optional[str] = Field(None, max_length=40)
    is_active: bool = True
    is_default: bool = False
    sort_order: Optional[int] = None


class BankRuleBody(BaseModel):
    country: str = Field(..., min_length=1, max_length=100)
    currency: str = Field(..., min_length=3, max_length=3)
    bank_profile_id: int


# ─── Yardımcılar ─────────────────────────────────────────────────────────────

def _perm(action):
    return require_internal_user(("b2b_orders", action))


def _user(db, actor):
    return db.query(User).filter(User.id == int(actor.get("sub", 0))).first()


def _show_prices(user) -> bool:
    return bool(user and (_resolve_permissions(user).get("b2b") or {}).get("view"))


def _error(exc):
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail, "code": exc.code, **exc.extra})


def _run(db, fn, *args, **kwargs):
    try:
        result = fn(*args, **kwargs)
        db.commit()
        return result
    except b2b.B2BError as exc:
        db.rollback()
        return _error(exc)
    except IntegrityError:
        db.rollback()
        return JSONResponse(status_code=409, content={
            "detail": "Aynı kayıt eşzamanlı değişti; sayfayı yenileyip yeniden deneyin.",
            "code": "concurrent_request"})


def _detail(db, actor, order_id, domain):
    user = _user(db, actor)
    order = b2b._order(db, order_id, domain)
    return b2b.order_detail(db, order, user=user, show_prices=_show_prices(user))


def _holders(db, action, exclude=None):
    """Bir b2b_orders yetkisine sahip aktif iç kullanıcılar (bildirim için)."""
    out = []
    for u in db.query(User).filter(User.is_active == True, User.role != "Distributor").all():  # noqa: E712
        if u.id != exclude and (_resolve_permissions(u).get("b2b_orders") or {}).get(action):
            out.append(u.id)
    return out


def _notify(db, background: BackgroundTasks, order_id, domain, actor):
    order = db.query(B2BOrder).filter(B2BOrder.id == order_id, B2BOrder.domain == domain).first()
    if order is None:
        return
    step = b2b.next_step(db, order)
    if not step:
        return
    me = int(actor.get("sub", 0))
    ids = [step["user_id"]] if step.get("user_id") else _holders(db, step["permission"], exclude=me)
    ids = [i for i in ids if i and i != me]
    q = order.quotation
    background.add_task(notify_b2b_step, ids, "B2B sipariş",
                        f"{q.quote_number if q else order.id}: {step['text']}", f"/b2b-siparisler#{order.id}")


def _respond(db, background, actor, domain, result, order_id):
    if isinstance(result, Response):
        return result
    _notify(db, background, order_id, domain, actor)
    return _detail(db, actor, order_id, domain)


# ─── Okuma ───────────────────────────────────────────────────────────────────

@router.get("/my-tasks")
def my_tasks(db: Session = Depends(get_db), actor: dict = Depends(require_internal_user()),
             domain: str = Depends(active_domain)):
    """Ana ekran "Benden bekleyen işler" — B2B adımları + fason onayları (aktif panel)."""
    tasks = [t for t in b2b.my_tasks(db, _user(db, actor)) if t.get("domain") in (None, domain)]
    return {"tasks": tasks}


@router.get("/bootstrap")
def bootstrap(db: Session = Depends(get_db), actor: dict = Depends(_perm("view"))):
    user = _user(db, actor)
    show = _show_prices(user)
    perms = _resolve_permissions(user).get("b2b_orders") or {}
    people = db.query(User).filter(User.is_active == True, User.role != "Distributor").order_by(  # noqa: E712
        User.full_name, User.username).all()

    def who(action):
        return [{"id": u.id, "name": u.full_name or u.username, "role": u.role} for u in people
                if (_resolve_permissions(u).get("b2b_orders") or {}).get(action)]
    data = {"permissions": dict(perms), "show_prices": show, "me": user.id if user else None,
            "technical_users": who("tech_review"), "signers": who("sign"),
            "payment_terms": b2b.PAYMENT_TERMS}
    if show:
        data["banks"] = [bank_accounts.profile_view(b)
                         for b in db.query(BankProfile).order_by(BankProfile.sort_order, BankProfile.id)]
        data["max_banks"] = bank_accounts.MAX_BANKS
        data["default_terms"] = dict(proforma_template.DEFAULT_TERMS)
        data["account_holder"] = proforma_template.COMPANY["account_holder"]
        data["bank_rules"] = [{"id": r.id, "country": r.country, "currency": r.currency,
                               "bank_profile_id": r.bank_profile_id}
                              for r in db.query(BankRule).order_by(BankRule.country, BankRule.currency)]
    return data


@router.get("")
def list_orders(status: Optional[str] = None, db: Session = Depends(get_db),
                actor: dict = Depends(_perm("view")), domain: str = Depends(active_domain)):
    return {"orders": b2b.list_orders(db, domain, show_prices=_show_prices(_user(db, actor)), status=status)}


@router.get("/{order_id}")
def get_order(order_id: int, db: Session = Depends(get_db), actor: dict = Depends(_perm("view")),
              domain: str = Depends(active_domain)):
    try:
        return _detail(db, actor, order_id, domain)
    except b2b.B2BError as exc:
        return _error(exc)


@router.get("/{order_id}/proforma")
def proforma(order_id: int, db: Session = Depends(get_db), actor: dict = Depends(_perm("view")),
             domain: str = Depends(active_domain)):
    user = _user(db, actor)
    if not _show_prices(user):
        return JSONResponse(status_code=403, content={"detail": "Proforma ticari bilgidir (b2b.view).",
                                                      "code": "forbidden"})
    try:
        order = b2b._order(db, order_id, domain)
        sig = b2b._valid_signature(db, order)
        if sig is None:
            b2b.fail("Proforma, güncel sürüm yönetim onayından sonra üretilir.", "approval_required", 409)
        _, com = b2b._revision(db, order, "commercial", sig.commercial_revision)
    except b2b.B2BError as exc:
        return _error(exc)
    pdf = render_order_proforma(com, {"signer_name": sig.signer_name, "signed_at": b2b._when(sig.signed_at),
                                      "document_hash": sig.document_hash, "revision": sig.commercial_revision,
                                      "png_b64": sig.signature_png})
    b2b._audit(db, actor, "proforma", order.id, {"commercial_revision": sig.commercial_revision})
    db.commit()
    return Response(content=pdf, media_type="application/pdf", headers={
        "Content-Disposition": content_disposition(proforma_filename(com.get("quote_number"),
                                                                     com["customer"].get("name")), inline=True),
        "Cache-Control": "no-store"})


@router.get("/{order_id}/technical-sheet")
def technical_sheet(order_id: int, db: Session = Depends(get_db), actor: dict = Depends(_perm("view")),
                    domain: str = Depends(active_domain)):
    """İç teknik föy (müşteriye gönderilmez) — reçete, ihtiyaç, alım, partiler,
    kaynak lotlar.  Satış fiyatı / banka YOK; alım tutarı yalnız b2b.view ile."""
    user = _user(db, actor)
    show = _show_prices(user)
    try:
        order = b2b._order(db, order_id, domain)
        detail = b2b.order_detail(db, order, user=user, show_prices=show)
    except b2b.B2BError as exc:
        return _error(exc)
    doc = sheet_doc(db, order, detail, show_prices=show,
                    generated_by=(user.full_name or user.username) if user else "")
    return Response(content=render_technical_sheet(doc), media_type="application/pdf", headers={
        "Content-Disposition": content_disposition(technical_sheet_filename(doc["reference"]), inline=True),
        "Cache-Control": "no-store"})


# ─── Ticari adımlar ──────────────────────────────────────────────────────────

@router.post("/from-quotation/{quotation_id}")
def convert(quotation_id: int, body: ConvertBody, background: BackgroundTasks,
            db: Session = Depends(get_db), actor: dict = Depends(_perm("manage")),
            domain: str = Depends(active_domain)):
    if not _show_prices(_user(db, actor)):
        return JSONResponse(status_code=403, content={"detail": "Teklif ticari bilgidir (b2b.view).",
                                                      "code": "forbidden"})
    data = body.model_dump()
    data["terms"] = body.terms.model_dump(exclude_unset=True) if body.terms is not None else None
    order = _run(db, b2b.convert_quotation, db, domain, actor, quotation_id, data)
    if isinstance(order, Response):
        return order
    return _respond(db, background, actor, domain, order, order.id)


@router.post("/{order_id}/revise")
def revise(order_id: int, body: ReviseBody, background: BackgroundTasks, db: Session = Depends(get_db),
           actor: dict = Depends(_perm("manage")), domain: str = Depends(active_domain)):
    if not _show_prices(_user(db, actor)):
        return JSONResponse(status_code=403, content={"detail": "Ticari revizyon b2b.view ister.",
                                                      "code": "forbidden"})
    data = body.model_dump(exclude_unset=True)
    if data.get("lines") is not None:
        data["lines"] = [dict(l) for l in data["lines"]]
    return _respond(db, background, actor, domain,
                    _run(db, b2b.revise_commercial, db, domain, actor, order_id, data), order_id)


@router.post("/{order_id}/reassign")
def reassign(order_id: int, body: ReassignBody, background: BackgroundTasks, db: Session = Depends(get_db),
             actor: dict = Depends(_perm("manage")), domain: str = Depends(active_domain)):
    return _respond(db, background, actor, domain,
                    _run(db, b2b.reassign, db, domain, actor, order_id, body.model_dump()), order_id)


@router.post("/{order_id}/cancel")
def cancel(order_id: int, body: ReasonBody, background: BackgroundTasks, db: Session = Depends(get_db),
           actor: dict = Depends(_perm("manage")), domain: str = Depends(active_domain)):
    return _respond(db, background, actor, domain,
                    _run(db, b2b.cancel_order, db, domain, actor, order_id, body.model_dump()), order_id)


@router.post("/{order_id}/technical-review")
def technical_review(order_id: int, body: ReviewBody, background: BackgroundTasks,
                     db: Session = Depends(get_db), actor: dict = Depends(_perm("tech_review")),
                     domain: str = Depends(active_domain)):
    data = body.model_dump()
    data["lines"] = [dict(l) for l in data["lines"]]
    return _respond(db, background, actor, domain,
                    _run(db, b2b.technical_review, db, domain, actor, order_id, data), order_id)


@router.post("/{order_id}/sign")
def sign(order_id: int, body: SignBody, request: Request, background: BackgroundTasks,
         db: Session = Depends(get_db), actor: dict = Depends(_perm("sign")),
         domain: str = Depends(active_domain)):
    # Sıra: sipariş + atama + aşama kontrolü (yan etkisiz) → şifre (kendi
    # commit'iyle sayaç) → imza.  Yanlış kişi şifre denemesi harcamasın.
    try:
        order = b2b._order(db, order_id, domain)
        b2b._require_assigned(order, actor, "signer_user_id", "yönetici")
        if order.status != "TECH_REVIEWED":
            b2b.fail("İmza için teknik değerlendirme tamamlanmış ve onay bekliyor olmalı.", "invalid_stage", 409)
        db.rollback()
        b2b.verify_password_step_up(db, int(actor.get("sub", 0)), body.password)
    except b2b.B2BError as exc:
        db.rollback()
        return _error(exc)
    data = body.model_dump(exclude={"password"})
    ip = request.client.host if request.client else None
    result = _run(db, b2b.sign, db, domain, actor, order_id, data, ip=ip,
                  user_agent=request.headers.get("user-agent"))
    return _respond(db, background, actor, domain, result, order_id)


@router.post("/{order_id}/payments")
def payment(order_id: int, body: PaymentBody, background: BackgroundTasks, db: Session = Depends(get_db),
            actor: dict = Depends(_perm("payment")), domain: str = Depends(active_domain)):
    if not _show_prices(_user(db, actor)):
        return JSONResponse(status_code=403, content={"detail": "Ödeme ticari bilgidir (b2b.view).",
                                                      "code": "forbidden"})
    return _respond(db, background, actor, domain,
                    _run(db, b2b.record_payment, db, domain, actor, order_id, body.model_dump()), order_id)


@router.post("/{order_id}/preparation")
def preparation(order_id: int, body: NoteBody, background: BackgroundTasks, db: Session = Depends(get_db),
                actor: dict = Depends(_perm("tech_review")), domain: str = Depends(active_domain)):
    return _respond(db, background, actor, domain,
                    _run(db, b2b.confirm_preparation, db, domain, actor, order_id, body.model_dump()), order_id)


@router.post("/{order_id}/purchase-lines/{line_id}")
def purchase_line(order_id: int, line_id: int, body: PurchaseLineBody, background: BackgroundTasks,
                  db: Session = Depends(get_db),
                  actor: dict = Depends(require_any_permission(("b2b_orders", "manage"),
                                                               ("b2b_orders", "payment"),
                                                               ("b2b_orders", "tech_review"))),
                  domain: str = Depends(active_domain)):
    if (_user(db, actor) or User(role="")).role == "Distributor":
        return JSONResponse(status_code=403, content={"detail": "Bu bölüm bayi hesaplarına kapalı."})
    return _respond(db, background, actor, domain,
                    _run(db, b2b.update_purchase_line, db, domain, actor, order_id, line_id,
                         body.model_dump(exclude_unset=True)), order_id)


# ─── Üretim partileri ve sevkiyat ────────────────────────────────────────────

@router.post("/{order_id}/batches")
def start_batch(order_id: int, body: BatchStartBody, background: BackgroundTasks,
                db: Session = Depends(get_db), actor: dict = Depends(_perm("produce")),
                domain: str = Depends(active_domain)):
    try:
        b2b.start_batch(db, domain, actor, order_id, body.model_dump())
        db.commit()
    except b2b.B2BError as exc:
        db.rollback()
        codes = {exc.code} | {e.get("code") for e in exc.extra.get("errors") or [] if isinstance(e, dict)}
        if "insufficient_stock" in codes:
            # Hiçbir tüketim yazılmadı; siparişin eksikleri ayrı commit'le güncellenir.
            # Güncelleme başarısız olsa da kullanıcı asıl stok hatasını görür.
            try:
                refreshed = _run(db, b2b.refresh_shortages, db, domain, actor, order_id)
                exc.extra["shortages_refreshed"] = not isinstance(refreshed, Response)
            except Exception:                                   # noqa: BLE001
                db.rollback()
                exc.extra["shortages_refreshed"] = False
        return _error(exc)
    except IntegrityError:
        db.rollback()
        return JSONResponse(status_code=409, content={
            "detail": "Aynı kayıt eşzamanlı değişti; sayfayı yenileyip yeniden deneyin.",
            "code": "concurrent_request"})
    return _respond(db, background, actor, domain, None, order_id)


@router.post("/{order_id}/batches/{batch_id}/complete")
def complete_batch(order_id: int, batch_id: int, body: BatchCompleteBody, background: BackgroundTasks,
                   db: Session = Depends(get_db), actor: dict = Depends(_perm("produce")),
                   domain: str = Depends(active_domain)):
    from routers.production import retention_cfg_months
    return _respond(db, background, actor, domain,
                    _run(db, b2b.complete_batch, db, domain, actor, order_id, batch_id, body.model_dump(),
                         retention_months=lambda: retention_cfg_months(db)), order_id)


@router.post("/{order_id}/batches/{batch_id}/cancel")
def cancel_batch(order_id: int, batch_id: int, body: ReasonBody, background: BackgroundTasks,
                 db: Session = Depends(get_db), actor: dict = Depends(_perm("produce")),
                 domain: str = Depends(active_domain)):
    return _respond(db, background, actor, domain,
                    _run(db, b2b.cancel_batch, db, domain, actor, order_id, batch_id, body.model_dump()),
                    order_id)


@router.post("/{order_id}/batches/{batch_id}/returns")
def batch_return(order_id: int, batch_id: int, body: BatchReturnBody, background: BackgroundTasks,
                 db: Session = Depends(get_db), actor: dict = Depends(_perm("produce")),
                 domain: str = Depends(active_domain)):
    return _respond(db, background, actor, domain,
                    _run(db, b2b.return_batch_material, db, domain, actor, order_id, batch_id,
                         body.model_dump()), order_id)


@router.post("/{order_id}/ship")
def ship(order_id: int, body: ShipBody, background: BackgroundTasks, db: Session = Depends(get_db),
         actor: dict = Depends(_perm("ship")), domain: str = Depends(active_domain)):
    return _respond(db, background, actor, domain,
                    _run(db, b2b.ship, db, domain, actor, order_id, body.model_dump()), order_id)


# ─── Banka profilleri ve kuralları (ticari — b2b.view + manage) ─────────────

def _bank_guard(db, actor):
    if not _show_prices(_user(db, actor)):
        return JSONResponse(status_code=403, content={"detail": "Banka bilgisi ticari bilgidir (b2b.view).",
                                                      "code": "forbidden"})
    return None


_BANK_FIELDS = ("label", "bank_name", "branch", "swift", "account_holder", "iban_usd", "iban_eur",
                "iban_try", "iban_rub", "is_active", "is_default", "sort_order")


def _bank_values(body, row=None):
    """Gövde → kolon değerleri (boşluklar kırpılır, boş metin NULL).  Güncellemede
    yalnız gönderilen alanlar yazılır — eski istemci `iban_rub` / `is_default`
    göndermeden kaydederse onları silmez.  Sonuçta etiket, banka adı, hesap sahibi
    ve en az bir IBAN şart (IBAN'sız profil proformaya basılamaz)."""
    data = body.model_dump(exclude_unset=row is not None)
    out = {}
    for key in _BANK_FIELDS:
        if key in data:
            val = data[key]
            out[key] = (val.strip() or None) if isinstance(val, str) else val
    if "sort_order" in out and out["sort_order"] is None:
        out.pop("sort_order")
    merged = {k: getattr(row, k) for k in _BANK_FIELDS} if row is not None else {}
    merged.update(out)
    if not (merged.get("label") and merged.get("bank_name") and merged.get("account_holder")):
        return JSONResponse(status_code=400, content={"detail": "Etiket, banka adı ve hesap sahibi gerekli.",
                                                      "code": "bank_fields_required"})
    if not any(merged.get(f) for _, f, _ in bank_accounts.IBAN_FIELDS):
        return JSONResponse(status_code=400, content={"detail": "En az bir IBAN (USD / EUR / TRY / RUB) girin.",
                                                      "code": "bank_iban_missing"})
    return out


@router.post("/banks")
def create_bank(body: BankBody, db: Session = Depends(get_db), actor: dict = Depends(_perm("manage"))):
    denied = _bank_guard(db, actor)
    if denied:
        return denied
    values = _bank_values(body)
    if isinstance(values, Response):
        return values
    row = BankProfile(**values)
    db.add(row)
    db.flush()
    b2b._audit(db, actor, "bank_create", None, {"bank_profile_id": row.id,
                                               **{k: v for k, v in values.items() if v not in (None, "")}})
    db.commit()
    return {"ok": True, "id": row.id}


@router.put("/banks/{bank_id}")
def update_bank(bank_id: int, body: BankBody, db: Session = Depends(get_db),
                actor: dict = Depends(_perm("manage"))):
    denied = _bank_guard(db, actor)
    if denied:
        return denied
    row = db.query(BankProfile).filter(BankProfile.id == bank_id).with_for_update().first()
    if row is None:
        return JSONResponse(status_code=404, content={"detail": "Banka profili bulunamadı."})
    values = _bank_values(body, row)
    if isinstance(values, Response):
        return values
    # IBAN değişikliği dolandırıcılık yüzeyidir → audit eski → yeni değeri tutar.
    changes = {}
    for key, value in values.items():
        if getattr(row, key) != value:
            changes[key] = [getattr(row, key), value]
            setattr(row, key, value)
    if changes:
        b2b._audit(db, actor, "bank_update", None, {"bank_profile_id": row.id, "changes": changes})
    db.commit()
    return {"ok": True, "id": row.id, "changed": sorted(changes)}


@router.post("/bank-rules")
def upsert_bank_rule(body: BankRuleBody, db: Session = Depends(get_db),
                     actor: dict = Depends(_perm("manage"))):
    denied = _bank_guard(db, actor)
    if denied:
        return denied
    if not db.query(BankProfile).filter_by(id=body.bank_profile_id, is_active=True).first():
        return JSONResponse(status_code=400, content={"detail": "Banka profili bulunamadı."})
    country = "*" if body.country.strip() == "*" else b2b._fold(body.country)
    cur = body.currency.upper()
    row = db.query(BankRule).filter_by(country=country, currency=cur).first()
    if row is None:
        row = BankRule(country=country, currency=cur, bank_profile_id=body.bank_profile_id,
                       created_by=b2b._actor(actor)[1])
        db.add(row)
    else:
        row.bank_profile_id = body.bank_profile_id
    db.flush()
    b2b._audit(db, actor, "bank_rule", None, {"country": country, "currency": cur,
                                             "bank_profile_id": body.bank_profile_id})
    db.commit()
    return {"ok": True, "id": row.id}


@router.delete("/bank-rules/{rule_id}")
def delete_bank_rule(rule_id: int, db: Session = Depends(get_db), actor: dict = Depends(_perm("manage"))):
    denied = _bank_guard(db, actor)
    if denied:
        return denied
    row = db.query(BankRule).filter_by(id=rule_id).first()
    if row is None:
        return JSONResponse(status_code=404, content={"detail": "Kural bulunamadı."})
    db.delete(row)
    b2b._audit(db, actor, "bank_rule_delete", None, {"rule_id": rule_id})
    db.commit()
    return {"ok": True}
