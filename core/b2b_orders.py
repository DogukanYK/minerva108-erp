# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""B2B sipariş akışı — onay, proforma, sipariş partileri, tek sevkiyat.

Spec: docs/superpowers/specs/2026-10-09-b2b-onay-proforma-uretim-design.md

  Taslak teklif (Quotation, DRAFT) ─ manage: siparişe dönüştür ─▶ SUBMITTED
  SUBMITTED ─ atanmış teknik kişi (Songül): stoktan kullanım / üretim ─▶ TECH_REVIEWED
  TECH_REVIEWED ─ atanmış imzacı (Işık): şifre + çizilen imza ─▶ APPROVED (proforma)
  APPROVED: ödeme doğrulama · alım satırları · son hazırlık · partiler ─ ship ─▶ SHIPPED

Kurallar
  • Ticari ve teknik içerik AYRI sürümlenir (snapshot + sha256).  Ürün/adet/
    etiket dili değişirse teknik + yönetim onayı, yalnız fiyat/şart/banka
    değişirse yönetim onayı yenilenir.  İmza bir (ticari, teknik) sürüm çiftine
    aittir; canlı stok hareketi imzalı belgeyi değiştirmez.
  • Onay ve proforma stok hareketi YAZMAZ; legacy "Onayla & stoktan düş"
    (POST /quotations/{id}/confirm) bu akışta hiç çağrılmaz (teklif ORDER olur).
  • Rezervasyon yok: her parti başlangıcında güncel stok yeniden planlanır;
    yetersizse HİÇBİR tüketim yazılmaz.  Hammaddede lot + geçerli SKT zorunlu,
    QC bekleyen/numune/SKT'si geçmiş lot kullanılmaz.  Kaynak seçimi kapalı.
  • Parti BAŞLAT yalnız tüketimi yazar (bitmiş ürün yok); TAMAMLA yalnız
    bitmiş ürünü + ProductionHistory/dökümü yazar — malzeme ikinci kez düşmez
    (core/production_run iki yarıyı ayrı çağrılır kılar).
  • Başlamış partinin iptali malzeme İADE ETMEZ; fiziksel iade gerekçeli,
    tüketimle sınırlı `+Adjustment` ("B2B parti iadesi").
  • Tek sevkiyat: hiçbir parti açık değil + her satırda QC'si bitmiş, şahit
    olmayan stok sipariş adedini karşılıyor; lotlar kilit altında yeniden
    doğrulanıp BİR kez düşer (Delivery 'kargo', ship_core released_only).
  • İşlem yetkisi + atanmış kullanıcı + aktif panel + sürüm SUNUCUDA denetlenir.
  • Motor commit ETMEZ; AdminAuditLog transaction içinde eklenir (log_admin_event
    içeride commit ettiği için KULLANILMAZ).  Kilit sırası: sipariş → kart → lot.
"""
import base64
import binascii
import hashlib
import json
from datetime import date, datetime

from sqlalchemy import func

from core import bank_accounts, lots, production_plan, production_run, proforma_template, stock_lots
from core.consumption import parse_ml
from core.stock_lots import lot_kind
from database import (AdminAuditLog, B2BOrder, B2BOrderBatch, B2BOrderBatchReturn,
                      B2BOrderPayment, B2BOrderPurchaseLine, B2BOrderRevision,
                      B2BOrderSignature, Inventory, Item,
                      ProductionConsumption, ProductionHistory, Quotation, QuotationItem,
                      Recipe, RecipeIngredient, Transaction, User, to_tr)

EPS = 1e-6
STATUS_LABEL = {
    "SUBMITTED": "Teknik değerlendirme bekliyor",
    "TECH_REVIEWED": "Yönetim onayı bekliyor",
    "APPROVED": "Onaylandı",
    "SHIPPED": "Sevk edildi",
    "CANCELLED": "İptal edildi",
}
OPEN = ("SUBMITTED", "TECH_REVIEWED", "APPROVED")
PAYMENT_TERMS = {"prepaid": "Peşin (üretimden önce %100)",
                 "advance": "Avans (üretimden önce %X, kalan sevkiyattan önce)",
                 "net": "Vadeli (ödeme üretimi/sevkiyatı durdurmaz)"}
BATCH_RETURN_MARK = "B2B parti iadesi"
SIGNATURE_MAX_BYTES = 300_000
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class B2BError(Exception):
    def __init__(self, status, detail, code="invalid_request", extra=None):
        self.status, self.detail, self.code, self.extra = status, detail, code, extra or {}
        super().__init__(detail)


def fail(detail, code="invalid_request", status=400, **extra):
    raise B2BError(status, detail, code, extra)


# ─── Yardımcılar ─────────────────────────────────────────────────────────────

def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _actor(actor):
    return int(actor.get("sub", 0)), (actor.get("full_name") or actor.get("username") or "—")[:100]


def _audit(db, actor, action, order_id, details=None):
    uid, name = _actor(actor)
    db.add(AdminAuditLog(actor_id=uid or None, actor_name=name, action="b2b_order." + action,
                         target_type="b2b_order", target_id=order_id, details=_json(details or {})))


def _when(value):
    return to_tr(value).strftime("%d.%m.%Y %H:%M") if value else ""


def _money(value):
    return round(float(value or 0) + 0.0, 2)


def _qty(value, *, zero=False, field="Miktar"):
    try:
        v = float(value)
    except (TypeError, ValueError):
        fail(f"{field} sayı olmalıdır.", "invalid_quantity")
    if v != v or v in (float("inf"), float("-inf")) or v < 0 or (not zero and v <= 0):
        fail(f"{field} sıfırdan büyük olmalıdır." if not zero else f"{field} negatif olamaz.",
             "invalid_quantity")
    return round(v, 6)


def _whole_only(unit):
    """Adetle sayılan ürün — miktar tam sayı olmalı."""
    return (unit or "adet").strip().lower() == "adet"


def _fold(text):
    from core.supplier_prices import normalize
    return normalize(text or "")


def _user(db, user_id):
    return db.query(User).filter(User.id == user_id).first() if user_id else None


def _user_name(db, user_id, cache=None):
    if cache is not None and user_id in cache:
        return cache[user_id]
    u = _user(db, user_id)
    name = (u.full_name or u.username) if u else "—"
    if cache is not None:
        cache[user_id] = name
    return name


def _perm(user, category, action):
    from core.permissions import _resolve_permissions
    return bool((_resolve_permissions(user).get(category) or {}).get(action))


def _order(db, order_id, domain, *, lock=False):
    q = db.query(B2BOrder).filter(B2BOrder.id == order_id, B2BOrder.domain == domain)
    row = q.with_for_update().populate_existing().first() if lock else q.first()
    if row is None:
        fail("Sipariş bulunamadı.", "not_found", 404)
    return row


def _require_open(order):
    if order.status not in OPEN:
        fail(f"Sipariş {STATUS_LABEL.get(order.status, order.status).lower()} — işlem yapılamaz.",
             "order_terminal", 409)


def _require_assigned(order, actor, field, label):
    if int(actor.get("sub", 0)) != getattr(order, field):
        fail(f"Bu adımı yalnız atanmış {label} yapabilir.", "not_assigned", 403)


def _revision(db, order, kind, revision=None):
    rev = revision if revision is not None else (
        order.commercial_revision if kind == "commercial" else order.technical_revision)
    row = (db.query(B2BOrderRevision)
           .filter_by(order_id=order.id, kind=kind, revision=rev).first())
    return (row, json.loads(row.snapshot)) if row else (None, None)


def _save_technical(db, order, snapshot, actor):
    order.technical_revision = (order.technical_revision or 0) + 1
    row = B2BOrderRevision(order_id=order.id, kind="technical", revision=order.technical_revision,
                           snapshot=_json(snapshot), snapshot_hash=_hash(snapshot),
                           created_by=_actor(actor)[1])
    db.add(row)
    db.flush()
    return row


def document_hash(order, commercial_row, technical_row):
    return _hash({"order_id": order.id,
                  "commercial_revision": commercial_row.revision if commercial_row else None,
                  "commercial_hash": commercial_row.snapshot_hash if commercial_row else None,
                  "technical_revision": technical_row.revision if technical_row else None,
                  "technical_hash": technical_row.snapshot_hash if technical_row else None})


def _scope(snap):
    """Teknik onayın kapsamı: etiket dili + (ürün, adet).  Fiyat/şart/banka
    değişikliği kapsamı değiştirmez → teknik değerlendirme geçerli kalır."""
    return (snap.get("label_language"),
            sorted((l["item_id"], l.get("quantity", l.get("ordered"))) for l in snap.get("lines") or []))


def technical_current(com, tech):
    """Teknik sürüm güncel ticari sürümün kapsamına mı ait."""
    return bool(com and tech) and _scope(com) == _scope(tech)


def _valid_signature(db, order):
    """Güncel (ticari, teknik) sürüm çiftine ait imza — yoksa None."""
    if not order.technical_revision:
        return None
    return (db.query(B2BOrderSignature)
            .filter_by(order_id=order.id, commercial_revision=order.commercial_revision,
                       technical_revision=order.technical_revision)
            .order_by(B2BOrderSignature.id.desc()).first())


# ─── Banka + proforma şartları (core/bank_accounts, core/proforma_template) ──

def suggest_bank(db, country, currency):
    return bank_accounts.suggest_bank(db, country, currency)


def _resolve_banks(db, ids):
    """Siparişe 0–3 banka; seçim boş olabilir (imzadan önce en az bir banka şart)."""
    try:
        return bank_accounts.resolve_banks(db, ids, required=False)
    except bank_accounts.BankSelectionError as exc:
        fail(exc.detail, exc.code)


def _order_bank_ids(order):
    return bank_accounts.parse_ids(order.bank_profile_ids) or (
        [order.bank_profile_id] if order.bank_profile_id else [])


def _set_banks(order, banks):
    ids = [b.id for b in banks]
    order.bank_profile_ids = bank_accounts.dump_ids(ids)
    order.bank_profile_id = ids[0] if ids else None


def _clean_terms(data):
    try:
        return proforma_template.clean_terms(data)
    except ValueError as exc:
        fail(str(exc), "invalid_terms")


def _stored_terms(raw):
    try:
        val = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        val = {}
    return val if isinstance(val, dict) else {}


def snapshot_banks(com):
    """İmzalı ticari sürümün bankaları — eski tek-banka sürümleri (`bank`) dahil."""
    banks = com.get("banks")
    if banks is None:
        banks = [com["bank"]] if com.get("bank") else []
    return [b for b in banks if b]


# ─── Ticari içerik ───────────────────────────────────────────────────────────

def _lines(db, quotation, domain):
    """Teklif satırları — sipariş başına ürün tekil, aktif bitmiş ürün."""
    seen, out = set(), []
    for line in sorted(quotation.items, key=lambda r: r.id):
        if line.item_id in seen:
            fail("Siparişte aynı ürün iki satırda olamaz; satırları birleştirin.", "duplicate_item")
        seen.add(line.item_id)
        item = db.query(Item).filter(Item.id == line.item_id, Item.domain == domain).first()
        if item is None or not item.is_active:
            fail(f"Ürün bu panelde aktif değil: {line.item_name_snapshot or line.item_id}.", "invalid_item")
        if lot_kind(item.category) != "finished":
            fail(f"Yalnız bitmiş ürün sipariş edilebilir: {item.name}.", "invalid_item")
        out.append({"item_id": item.id, "name": line.item_name_snapshot or item.name,
                    "weight_ml": parse_ml(item.variation_name, line.item_name_snapshot, item.name),
                    "unit": item.unit or "adet", "quantity": round(float(line.quantity), 6),
                    "unit_price": round(float(line.unit_price_foreign), 6),
                    "line_total": _money(line.line_total)})
    if not out:
        fail("Siparişte en az bir ürün olmalı.", "empty_order")
    return out


def commercial_snapshot(db, order, quotation):
    """Ticari sürüm: müşteri, satırlar (+ ml), tutarlar, şartlar, SEÇİLEN bankalar."""
    cur = (quotation.currency or "TRY").upper()
    banks = [bank_accounts.bank_snapshot(b, cur) for b in _resolve_banks(db, _order_bank_ids(order))]
    terms = proforma_template.merged_terms(
        _stored_terms(order.proforma_terms),
        proforma_template.payment_terms_text(order.payment_terms, order.advance_percent))
    return {
        "quotation_id": quotation.id, "quote_number": quotation.quote_number,
        "customer": {"name": quotation.customer_name, "contact": quotation.customer_contact or "",
                     "email": quotation.customer_email or "", "phone": quotation.customer_phone or "",
                     "address": quotation.customer_address or "",
                     "country": quotation.customer_country or "", "vat": quotation.customer_vat or ""},
        "currency": cur, "exchange_rate": quotation.exchange_rate,
        "subtotal": _money(quotation.subtotal_amount), "tax_percentage": float(quotation.tax_percentage or 0),
        "tax_amount": _money(quotation.tax_amount), "shipping": _money(quotation.shipping_amount),
        "total": _money(quotation.total_amount), "notes": quotation.notes or "",
        "valid_days": quotation.valid_days,
        "label_language": order.label_language,
        "target_date": order.target_date.isoformat() if order.target_date else None,
        "payment_terms": order.payment_terms, "advance_percent": order.advance_percent,
        "bank": banks[0] if banks else None,
        "banks": banks,
        "terms": terms,
        "lines": _lines(db, quotation, order.domain),
    }


def _recompute_totals(quotation):
    subtotal = sum(float(i.line_total or 0) for i in quotation.items)
    shipping = float(quotation.shipping_amount or 0)
    tax = (subtotal + shipping) * float(quotation.tax_percentage or 0) / 100.0
    quotation.subtotal_amount = round(subtotal, 2)
    quotation.tax_amount = round(tax, 2)
    quotation.total_amount = round(subtotal + shipping + tax, 2)


def _terms(data, *, current=None):
    terms = (data.get("payment_terms") or (current.payment_terms if current else "prepaid")).strip()
    if terms not in PAYMENT_TERMS:
        fail("Ödeme koşulu peşin / avans / vadeli olmalıdır.", "invalid_terms")
    pct = data.get("advance_percent", current.advance_percent if current else None)
    if terms == "advance":
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            fail("Avans yüzdesi gerekli.", "invalid_terms")
        if not 0 < pct < 100:
            fail("Avans yüzdesi 0 ile 100 arasında olmalıdır.", "invalid_terms")
    else:
        pct = None
    return terms, pct


def _language(value, default="EN"):
    lang = (value or default or "EN").strip().upper()
    if lang not in ("TR", "EN"):
        fail("Etiket dili TR veya EN olmalıdır.", "invalid_language")
    return lang


def _target_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            pass
    fail("Hedef tarih YYYY-AA-GG olmalıdır.", "invalid_date")


def _assignee(db, user_id, category, action, label):
    u = _user(db, user_id)
    if u is None or not u.is_active or u.role == "Distributor" or not _perm(u, category, action):
        fail(f"Atanan {label} hesabı aktif değil ya da yetkisi yok.", "invalid_assignee")
    return u


def convert_quotation(db, domain, actor, quotation_id, data):
    """Taslak teklifi siparişe dönüştür (ticari onay) → laboratuvara gönder."""
    q = (db.query(Quotation).filter(Quotation.id == quotation_id, Quotation.domain == domain)
         .with_for_update().first())
    if q is None:
        fail("Teklif bulunamadı.", "not_found", 404)
    if q.status != "DRAFT" or q.distributor_id:
        fail("Yalnız iç taslak teklif siparişe dönüştürülebilir.", "invalid_quotation", 409)
    if db.query(B2BOrder.id).filter(B2BOrder.quotation_id == q.id).first():
        fail("Bu teklif zaten siparişe dönüştürülmüş.", "already_converted", 409)
    uid = int(actor.get("sub", 0))
    tech = _assignee(db, data.get("technical_user_id"), "b2b_orders", "tech_review", "teknik değerlendirme")
    signer = _assignee(db, data.get("signer_user_id"), "b2b_orders", "sign", "yönetim onayı")
    if signer.id == uid:
        fail("Siparişi açan kişi yönetim onayını kendisi veremez.", "four_eyes", 400)
    terms, pct = _terms(data)
    cur = (q.currency or "TRY").upper()
    # Banka seçimi: istekte → teklifte saklı → ülke kuralı / varsayılan bankalar
    explicit = data.get("bank_profile_ids") is not None or bool(data.get("bank_profile_id"))
    if data.get("bank_profile_ids") is not None:
        ids = bank_accounts.parse_ids(data["bank_profile_ids"])
    elif data.get("bank_profile_id"):
        ids = [int(data["bank_profile_id"])]
    else:
        ids = bank_accounts.parse_ids(q.bank_profile_ids) or bank_accounts.default_bank_ids(
            db, q.customer_country, cur)
    banks = _resolve_banks(db, ids)
    # Şartlar: teklifte saklı olanın üstüne istekte gelen alanlar
    proforma_terms = _clean_terms({**_stored_terms(q.proforma_terms), **(data.get("terms") or {})})
    order = B2BOrder(quotation_id=q.id, domain=domain, status="SUBMITTED",
                     label_language=_language(data.get("label_language")),
                     target_date=_target_date(data.get("target_date")),
                     payment_terms=terms, advance_percent=pct,
                     proforma_terms=_json(proforma_terms), owner_user_id=uid,
                     technical_user_id=tech.id, signer_user_id=signer.id,
                     commercial_revision=1, technical_revision=0)
    _set_banks(order, banks)
    db.add(order)
    db.flush()
    q.status = "ORDER"
    snap = commercial_snapshot(db, order, q)
    db.add(B2BOrderRevision(order_id=order.id, kind="commercial", revision=1, snapshot=_json(snap),
                            snapshot_hash=_hash(snap), created_by=_actor(actor)[1]))
    db.flush()
    _audit(db, actor, "convert", order.id, {"quotation_id": q.id, "commercial_revision": 1,
                                            "bank_ids": [b.id for b in banks],
                                            "bank_suggested": bool(banks and not explicit)})
    return order


def _new_commercial(db, order, quotation, actor, *, technical_changed):
    """Yeni ticari sürüm → onaylar yenilenir (adet/ürün/dil değiştiyse teknik de)."""
    snap = commercial_snapshot(db, order, quotation)
    order.commercial_revision += 1
    db.add(B2BOrderRevision(order_id=order.id, kind="commercial", revision=order.commercial_revision,
                            snapshot=_json(snap), snapshot_hash=_hash(snap),
                            created_by=_actor(actor)[1]))
    order.status = "SUBMITTED" if (technical_changed or not order.technical_revision) else "TECH_REVIEWED"
    order.prep_confirmed_at = order.prep_confirmed_by = None
    order.updated_at = datetime.utcnow()
    db.flush()


def revise_commercial(db, domain, actor, order_id, data):
    """Ticari revizyon: satırlar, müşteri, şartlar, banka, dil, hedef tarih."""
    order = _order(db, order_id, domain, lock=True)
    _require_open(order)
    q = db.query(Quotation).filter(Quotation.id == order.quotation_id).with_for_update().first()
    _, before = _revision(db, order, "commercial")
    if int(data.get("commercial_revision", 0)) != order.commercial_revision:
        fail("Sipariş başka biri tarafından değiştirildi; yeniden açın.", "revision_stale", 409)
    if "lines" in data and data["lines"] is not None:
        wanted = data["lines"]
        if not wanted:
            fail("Siparişte en az bir ürün olmalı.", "empty_order")
        if len({int(r["item_id"]) for r in wanted}) != len(wanted):
            fail("Siparişte aynı ürün iki satırda olamaz.", "duplicate_item")
        old = {i.item_id: i for i in q.items}
        for line in list(q.items):
            q.items.remove(line)
        db.flush()
        for r in wanted:
            item = db.query(Item).filter(Item.id == int(r["item_id"]), Item.domain == domain).first()
            if item is None or not item.is_active or lot_kind(item.category) != "finished":
                fail("Siparişe yalnız bu paneldeki aktif bitmiş ürün eklenebilir.", "invalid_item")
            qty = _qty(r.get("quantity"))
            price = _qty(r.get("unit_price"), zero=True, field="Birim fiyat")
            prev = old.get(item.id)
            q.items.append(QuotationItem(item_id=item.id, quantity=qty, unit_price_foreign=price,
                                         unit_cost_try=prev.unit_cost_try if prev else None,
                                         item_name_snapshot=(prev.item_name_snapshot if prev else None) or item.name,
                                         line_total=round(qty * price, 2)))
    for key, attr in (("customer_name", "customer_name"), ("customer_contact", "customer_contact"),
                      ("customer_email", "customer_email"), ("customer_phone", "customer_phone"),
                      ("customer_address", "customer_address"), ("customer_country", "customer_country"),
                      ("customer_vat", "customer_vat"), ("notes", "notes")):
        if key in data and data[key] is not None:
            setattr(q, attr, str(data[key]).strip() or None)
    if not (q.customer_name or "").strip():
        fail("Müşteri adı gerekli.", "customer_required")
    for key in ("tax_percentage", "shipping_amount"):
        if key in data and data[key] is not None:
            setattr(q, key, _qty(data[key], zero=True, field="Tutar"))
    if "payment_terms" in data or "advance_percent" in data:
        order.payment_terms, order.advance_percent = _terms(data, current=order)
    if data.get("label_language"):
        order.label_language = _language(data["label_language"])
    if "target_date" in data:
        order.target_date = _target_date(data.get("target_date"))
    if data.get("bank_profile_ids") is not None:
        _set_banks(order, _resolve_banks(db, data["bank_profile_ids"]))
    elif "bank_profile_id" in data:                                  # eski tek-banka gövdesi
        _set_banks(order, _resolve_banks(db, [data["bank_profile_id"]] if data["bank_profile_id"] else []))
    if data.get("terms") is not None:                                # kısmi gövde: diğer alanlar korunur
        order.proforma_terms = _json(_clean_terms({**_stored_terms(order.proforma_terms), **data["terms"]}))
    db.flush()
    _recompute_totals(q)
    after = commercial_snapshot(db, order, q)
    if _hash(after) == _hash(before):
        fail("Değişiklik yok.", "no_change")
    technical_changed = _scope(after) != _scope(before)
    _new_commercial(db, order, q, actor, technical_changed=technical_changed)
    _audit(db, actor, "commercial_revision", order.id,
           {"revision": order.commercial_revision, "technical_reset": technical_changed})
    return order


def reassign(db, domain, actor, order_id, data):
    """Atanmış teknik kişi / imzacı değişikliği (kişi yoksa iş beklemesin)."""
    order = _order(db, order_id, domain, lock=True)
    _require_open(order)
    changes = {}
    if data.get("technical_user_id") and data["technical_user_id"] != order.technical_user_id:
        order.technical_user_id = _assignee(db, data["technical_user_id"], "b2b_orders", "tech_review",
                                            "teknik değerlendirme").id
        changes["technical_user_id"] = order.technical_user_id
    if data.get("signer_user_id") and data["signer_user_id"] != order.signer_user_id:
        signer = _assignee(db, data["signer_user_id"], "b2b_orders", "sign", "yönetim onayı")
        if signer.id == order.owner_user_id:
            fail("Siparişi açan kişi yönetim onayını kendisi veremez.", "four_eyes")
        order.signer_user_id = signer.id
        changes["signer_user_id"] = signer.id
    if not changes:
        fail("Değişiklik yok.", "no_change")
    order.updated_at = datetime.utcnow()
    _audit(db, actor, "reassign", order.id, changes)
    return order


# ─── Teknik değerlendirme ────────────────────────────────────────────────────

def _recipes_for(db, item_id, domain):
    return (db.query(Recipe).filter(Recipe.target_item_id == item_id, Recipe.domain == domain,
                                    Recipe.is_active == True).order_by(Recipe.id).all())   # noqa: E712


def recipe_fingerprint(db, recipe):
    """Onaylanan reçetenin içeriği — teknik değerlendirmede saklanır, parti
    başlangıcında yeniden hesaplanır.  Reçete düzenlenince (satırlar silinip
    yeniden yaratılır) özet değişir → teknik + yönetim onayı yenilenmeli."""
    rows = db.query(RecipeIngredient).filter(RecipeIngredient.recipe_id == recipe.id).all()
    return _hash({"target": recipe.target_item_id,
                  "waste": round(float(recipe.waste_percentage or 0), 6),
                  "output": round(float(recipe.output_quantity or 1), 6),
                  "lines": sorted([r.item_id, round(float(r.quantity or 0), 6), r.unit or "", r.phase or ""]
                                  for r in rows)})


def recipe_view(db, recipe):
    """Reçete bileşimi — teknik değerlendirmede snapshot'a yazılır (iç teknik föy
    onaylanan içeriği basar; reçete sonradan değişse de föy değişmez)."""
    rows = (db.query(RecipeIngredient, Item).outerjoin(Item, Item.id == RecipeIngredient.item_id)
            .filter(RecipeIngredient.recipe_id == recipe.id).order_by(RecipeIngredient.id).all())
    return {"id": recipe.id, "name": recipe.name,
            "output_quantity": round(float(recipe.output_quantity or 1), 6),
            "output_unit": recipe.output_unit or "", "waste_percentage": round(float(recipe.waste_percentage or 0), 6),
            "ingredients": [{"item_id": ri.item_id, "name": it.name if it else f"#{ri.item_id}",
                             "category": (it.category if it else "") or "",
                             "quantity": round(float(ri.quantity or 0), 6),
                             "unit": ri.unit or (it.unit if it else "") or "", "phase": ri.phase or ""}
                            for ri, it in rows]}


def _recipe_changed(db, line):
    """Teknik plandaki reçete artık onaylandığı gibi değil mi (pasif / silinmiş /
    içerik değişmiş)."""
    if not line.get("recipe_id") or not line.get("recipe_hash"):
        return False
    rec = db.query(Recipe).filter(Recipe.id == line["recipe_id"]).first()
    return (rec is None or not rec.is_active or rec.target_item_id != line["item_id"]
            or recipe_fingerprint(db, rec) != line["recipe_hash"])


def released_stock(db, item_id):
    """Sevkiyata uygun stok: kart stoğu − QC bekleyen − şahit lotlar."""
    item = db.query(Item).filter(Item.id == item_id).first()
    return stock_lots.shippable_quantity(db, item) if item is not None else 0.0


def _shortages(db, domain, lang, produce_lines):
    """Satın Alma Planı motoru — ortak hammaddeler sipariş genelinde toplanır,
    tedarikçi politikası korunur.  Üretilecek satır yoksa boş."""
    if not produce_lines:
        return [], []
    from core.purchase_plan_models import PlanLineIn, PlanOptionsIn, PlanRequest
    from core import purchase_plan as pp
    from routers.purchase_plan import build_report
    req = PlanRequest(lines=[PlanLineIn(recipe_id=l["recipe_id"], qty=l["produce"]) for l in produce_lines],
                      options=PlanOptionsIn(label_mode=lang, safe_rounding=False))
    try:
        report = build_report(db, req, domain)
    except pp.PlanInputError as exc:
        return [], [str(exc)]
    rows = []
    for m in report.get("materials") or []:
        rows.append({"item_id": m.get("item_id"), "name": m.get("name"), "kind": m.get("kind"),
                     "unit": m.get("unit"), "need": round(float(m.get("need") or 0), 6),
                     "stock": round(float(m.get("stock") or 0), 6),
                     "buy": round(float(m.get("buy") or 0), 6), "status": m.get("status"),
                     "supplier": m.get("supplier"), "price": m.get("price"),
                     "price_unit": m.get("price_unit"), "amount": m.get("amount")})
    return rows, list((report.get("meta") or {}).get("warnings") or [])


def technical_review(db, domain, actor, order_id, data):
    order = _order(db, order_id, domain, lock=True)
    _require_open(order)
    _require_assigned(order, actor, "technical_user_id", "teknik sorumlu")
    if int(data.get("commercial_revision", 0)) != order.commercial_revision:
        fail("Ticari sürüm değişti; siparişi yeniden açın.", "revision_stale", 409)
    _, com = _revision(db, order, "commercial")
    given = {int(r["item_id"]): r for r in data.get("lines") or []}
    if set(given) != {l["item_id"] for l in com["lines"]}:
        fail("Her sipariş satırı için stoktan kullanım / üretim kararı gerekli.", "lines_incomplete")
    lines, warnings = [], []
    for l in com["lines"]:
        r = given[l["item_id"]]
        use = _qty(r.get("use_stock_quantity", 0), zero=True, field="Stoktan kullanım")
        if _whole_only(l.get("unit")) and use != int(use):
            fail(f"{l['name']}: adet kesirli olamaz.", "invalid_quantity")
        if use > l["quantity"] + EPS:
            fail(f"{l['name']}: stoktan kullanım sipariş adedini aşamaz.", "invalid_split")
        produce = round(l["quantity"] - use, 6)
        released = released_stock(db, l["item_id"])
        if use > released + EPS:
            warnings.append(f"{l['name']}: şu an sevke uygun stok {released:g}, stoktan kullanım {use:g} — "
                            "sevkiyat anında yeniden kontrol edilir.")
        recipe_id = recipe_name = None
        if produce > EPS:
            options = _recipes_for(db, l["item_id"], domain)
            if r.get("recipe_id"):
                chosen = next((x for x in options if x.id == int(r["recipe_id"])), None)
                if chosen is None:
                    fail(f"{l['name']}: seçilen reçete bu ürüne ait aktif reçete değil.", "invalid_recipe")
            elif len(options) == 1:
                chosen = options[0]
            elif not options:
                fail(f"{l['name']}: aktif reçetesi yok — tamamı stoktan karşılanmalı.", "recipe_missing")
            else:
                fail(f"{l['name']}: birden çok reçete var, birini seçin.", "recipe_choice_required")
            recipe_id, recipe_name = chosen.id, chosen.name
        lines.append({"item_id": l["item_id"], "name": l["name"], "unit": l["unit"],
                      "ordered": l["quantity"], "use_stock": use, "produce": produce,
                      "recipe_id": recipe_id, "recipe_name": recipe_name,
                      "recipe_hash": recipe_fingerprint(db, chosen) if recipe_id else None,
                      "recipe": recipe_view(db, chosen) if recipe_id else None,
                      "released_stock_at_review": round(released, 6)})
    materials, plan_warnings = _shortages(db, domain, order.label_language,
                                          [l for l in lines if l["produce"] > EPS])
    snap = {"commercial_revision": order.commercial_revision, "label_language": order.label_language,
            "lines": lines, "materials": materials, "warnings": warnings + plan_warnings,
            "note": str(data.get("note") or "").strip()[:4000]}
    row = _save_technical(db, order, snap, actor)
    _sync_purchase_lines(db, order, materials, actor)
    order.status = "TECH_REVIEWED"
    order.prep_confirmed_at = order.prep_confirmed_by = None
    order.updated_at = datetime.utcnow()
    _audit(db, actor, "technical_review", order.id, {"technical_revision": row.revision,
                                                     "shortage_count": sum(1 for m in materials if m["buy"] > EPS)})
    return order


def _sync_purchase_lines(db, order, materials, actor):
    """Eksikler → alım satırları.  Henüz sipariş verilmemiş eski satırlar iptal;
    sipariş verilmiş ama henüz GELMEMİŞ miktar yeni ihtiyaçtan düşülür (teslim
    alınan zaten stokta — eksik hesabı onu stoktan görür, iki kez düşülmez)."""
    name = _actor(actor)[1]
    active = (db.query(B2BOrderPurchaseLine)
              .filter(B2BOrderPurchaseLine.order_id == order.id,
                      B2BOrderPurchaseLine.status != "cancelled").all())
    committed = {}
    for line in active:
        if line.status == "open" and (line.ordered_quantity or 0) <= EPS:
            line.status, line.updated_by, line.updated_at = "cancelled", name, datetime.utcnow()
        elif line.status != "received":
            committed[line.item_id] = committed.get(line.item_id, 0.0) + max(
                0.0, float(line.ordered_quantity or 0) - float(line.received_quantity or 0))
    for m in materials:
        need = round(float(m["buy"] or 0) - committed.get(m["item_id"], 0.0), 6)
        if m.get("item_id") and need > EPS:
            db.add(B2BOrderPurchaseLine(order_id=order.id, technical_revision=order.technical_revision,
                                        item_id=m["item_id"], item_name=m["name"], unit=m["unit"],
                                        need_quantity=need, supplier_name=m.get("supplier"),
                                        status="open", updated_by=name))


def update_purchase_line(db, domain, actor, order_id, line_id, data):
    order = _order(db, order_id, domain, lock=True)
    _require_open(order)
    line = (db.query(B2BOrderPurchaseLine)
            .filter_by(id=line_id, order_id=order.id).with_for_update().first())
    if line is None:
        fail("Alım satırı bulunamadı.", "not_found", 404)
    status = data.get("status", line.status)
    if status not in ("open", "ordered", "received", "cancelled"):
        fail("Durum açık / sipariş verildi / teslim alındı / iptal olmalıdır.", "invalid_status")
    for key in ("ordered_quantity", "received_quantity"):
        if data.get(key) is not None:
            setattr(line, key, _qty(data[key], zero=True))
    if data.get("supplier_name") is not None:
        line.supplier_name = str(data["supplier_name"]).strip()[:150] or None
    if data.get("note") is not None:
        line.note = str(data["note"]).strip()[:2000] or None
    line.status = status
    line.updated_by, line.updated_at = _actor(actor)[1], datetime.utcnow()
    _audit(db, actor, "purchase_line", order.id, {"line_id": line.id, "status": status,
                                                  "ordered": line.ordered_quantity,
                                                  "received": line.received_quantity})
    return order


# ─── Yönetim onayı (şifre + çizilen imza) ────────────────────────────────────

def verify_password_step_up(db, user_id, password):
    """Giriş kilit sayaçlarını paylaşan şifre doğrulaması.  Sayaçlar KENDİ
    commit'iyle yazılır (yanlış şifre sonrası onay işlemi geri alınsa bile
    deneme sayılır — imza ekranı ayrı bir kaba kuvvet kapısı olmasın)."""
    from datetime import timedelta
    from core.auth import verify_password
    from routers.auth import LOCKOUT_DURATION_MIN, LOCKOUT_THRESHOLD
    user = db.query(User).filter(User.id == user_id, User.is_active == True).with_for_update().first()  # noqa: E712
    if user is None:
        fail("Hesap aktif değil.", "unauthorized", 401)
    now = datetime.utcnow()
    if user.lockout_until and user.lockout_until > now:
        db.rollback()
        fail("Hesap güvenlik gereği geçici olarak kilitli; birkaç dakika sonra deneyin.", "locked", 423)
    if not password or not verify_password(password, user.password_hash):
        user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
        if user.failed_login_attempts >= LOCKOUT_THRESHOLD:
            user.lockout_until = now + timedelta(minutes=LOCKOUT_DURATION_MIN)
        db.commit()
        fail("Şifre hatalı.", "invalid_password", 401)
    if user.failed_login_attempts:
        user.failed_login_attempts = 0
        user.lockout_until = None
    db.commit()
    return user


def _signature_png(data_url):
    text = str(data_url or "")
    prefix = "data:image/png;base64,"
    if not text.startswith(prefix):
        fail("İmza PNG olarak çizilmelidir.", "signature_invalid")
    try:
        raw = base64.b64decode(text[len(prefix):], validate=True)
    except (binascii.Error, ValueError):
        fail("İmza verisi bozuk.", "signature_invalid")
    if len(raw) > SIGNATURE_MAX_BYTES or not raw.startswith(PNG_MAGIC) or len(raw) < 100:
        fail("İmza verisi geçersiz.", "signature_invalid")
    width, height = int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    if not (50 <= width <= 4000 and 30 <= height <= 3000):
        fail("İmza ölçüsü geçersiz.", "signature_invalid")
    return base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest()


def sign(db, domain, actor, order_id, data, *, ip=None, user_agent=None):
    """Şifre router'da `verify_password_step_up` ile ÖNCE doğrulanır."""
    order = _order(db, order_id, domain, lock=True)
    _require_open(order)
    _require_assigned(order, actor, "signer_user_id", "yönetici")
    if order.status != "TECH_REVIEWED":
        fail("İmza için teknik değerlendirme tamamlanmış ve onay bekliyor olmalı.", "invalid_stage", 409)
    com_row, com = _revision(db, order, "commercial")
    tech_row, tech = _revision(db, order, "technical")
    if not technical_current(com, tech):
        fail("Teknik değerlendirme güncel ticari sürüme ait değil; yeniden değerlendirme gerekli.",
             "technical_stale", 409)
    expected = document_hash(order, com_row, tech_row)
    if (int(data.get("commercial_revision", 0)) != order.commercial_revision or
            int(data.get("technical_revision", 0)) != order.technical_revision or
            data.get("document_hash") != expected):
        fail("Onaylanan belge değişti; son sürümü yeniden açın.", "revision_stale", 409)
    if not snapshot_banks(com):
        fail("Proforma için en az bir banka hesabı seçilip ticari sürüme işlenmeli.", "bank_required", 409)
    png, digest = _signature_png(data.get("signature"))
    uid, name = _actor(actor)
    db.add(B2BOrderSignature(order_id=order.id, role="management", user_id=uid, signer_name=name,
                             commercial_revision=order.commercial_revision,
                             technical_revision=order.technical_revision,
                             document_hash=expected, signature_png=png, signature_sha256=digest,
                             ip_address=(ip or "")[:64] or None, user_agent=(user_agent or "")[:300] or None))
    order.status = "APPROVED"
    order.updated_at = datetime.utcnow()
    _audit(db, actor, "sign", order.id, {"commercial_revision": order.commercial_revision,
                                         "technical_revision": order.technical_revision,
                                         "document_hash": expected, "signature_sha256": digest})
    return order


# ─── Ödeme, hazırlık ─────────────────────────────────────────────────────────

def payment_gate(db, order, com=None):
    if com is None:
        _, com = _revision(db, order, "commercial")
    total = float(com["total"] or 0)
    cur = com["currency"]
    paid = float(db.query(func.coalesce(func.sum(B2BOrderPayment.amount), 0.0))
                 .filter(B2BOrderPayment.order_id == order.id, B2BOrderPayment.currency == cur).scalar() or 0)
    pct = {"prepaid": 100.0, "advance": float(order.advance_percent or 0), "net": 0.0}[order.payment_terms]
    need_prod = round(total * pct / 100.0, 2)
    need_ship = round(total if order.payment_terms in ("prepaid", "advance") else 0.0, 2)
    return {"currency": cur, "total": round(total, 2), "paid": round(paid, 2),
            "required_for_production": need_prod, "required_for_shipment": need_ship,
            "production_ok": paid + 0.005 >= need_prod, "shipment_ok": paid + 0.005 >= need_ship}


def record_payment(db, domain, actor, order_id, data):
    order = _order(db, order_id, domain, lock=True)
    _require_open(order)
    if order.status != "APPROVED":
        fail("Ödeme, yönetim onayı ve proformadan sonra kaydedilir.", "invalid_stage", 409)
    _, com = _revision(db, order, "commercial")
    cur = (data.get("currency") or com["currency"]).upper()
    if cur != com["currency"]:
        fail(f"Ödeme proforma para biriminde ({com['currency']}) kaydedilir.", "currency_mismatch")
    amount = _qty(data.get("amount"), field="Tutar")
    uid, name = _actor(actor)
    db.add(B2BOrderPayment(order_id=order.id, amount=round(amount, 2), currency=cur,
                           received_on=_target_date(data.get("received_on")),
                           reference=(str(data.get("reference") or "").strip()[:150] or None),
                           note=(str(data.get("note") or "").strip()[:2000] or None),
                           verified_by_id=uid, verified_by=name))
    db.flush()
    _audit(db, actor, "payment", order.id, {"amount": round(amount, 2), "currency": cur})
    return order


def confirm_preparation(db, domain, actor, order_id, data):
    """Songül'ün son hazırlık kontrolü — eksik malzeme kabulü + hazırlık tamam."""
    order = _order(db, order_id, domain, lock=True)
    _require_open(order)
    _require_assigned(order, actor, "technical_user_id", "teknik sorumlu")
    if order.status != "APPROVED":
        fail("Hazırlık kontrolü yönetim onayından sonra yapılır.", "invalid_stage", 409)
    order.prep_confirmed_at, order.prep_confirmed_by = datetime.utcnow(), _actor(actor)[1]
    order.updated_at = datetime.utcnow()
    open_lines = (db.query(B2BOrderPurchaseLine)
                  .filter(B2BOrderPurchaseLine.order_id == order.id,
                          B2BOrderPurchaseLine.status.in_(("open", "ordered"))).count())
    _audit(db, actor, "preparation", order.id, {"open_purchase_lines": open_lines,
                                                "note": str(data.get("note") or "")[:500]})
    return order


# ─── Partiler ────────────────────────────────────────────────────────────────

def _line_progress(db, order, tech):
    batches = db.query(B2BOrderBatch).filter_by(order_id=order.id).all()
    out = {}
    for l in (tech or {}).get("lines", []):
        rows = [b for b in batches if b.item_id == l["item_id"]]
        started = sum(b.planned_quantity for b in rows if b.status == "STARTED")
        produced = sum(b.produced_quantity or 0 for b in rows if b.status == "COMPLETED")
        witness = sum(b.witness_quantity or 0 for b in rows if b.status == "COMPLETED")
        customer = round(produced - witness, 6)
        out[l["item_id"]] = {"started": round(started, 6), "produced": round(produced, 6),
                             "witness": round(witness, 6), "customer_quantity": customer,
                             "remaining_to_start": round(max(0.0, l["produce"] - customer - started), 6)}
    return out


def strict_lot_ok(lot, today=None):
    """Sipariş partisinin hammadde havuzu: lot no'lu, APPROVED, QC'si bitmiş,
    numune değil, SKT'si okunur ve geçmemiş.  FIFO bu koşulu sağlamayan lotu
    hiç görmez (production_plan `lot_filter`).  SKT eksik / okunamayan / geçmiş
    lotlar İzlenebilirlik → "SKT sorunu olan lotlar" raporunda listelenir ve
    orada düzeltilir (core/expiry_report — aynı ayrıştırıcı ve aynı "bugün")."""
    exp = lots.parse_expiry(lot.expiry_date)
    return bool((lot.lot_number or "").strip() and (lot.status or "") == "APPROVED"
                and not lot.qc_required and not lot.is_sample
                and exp is not None and exp >= (today or lots.expiry_today()))


def _check_strict_lots(pl):
    """Planın hammadde dilimleri sıkı havuzdan mı — havuz yetmediyse kalan
    `uncovered`'dır ve parti BAŞLAMAZ (lot kaydı dışı düşüm yok).  Seçilen
    lotlar FIFO'da zaten süzüldü; burada yeniden denetlenir (savunma)."""
    problems = []
    today = lots.expiry_today()
    for seg in pl.segments():
        if seg.line.kind != "raw":
            continue
        name = pl.names.get(seg.pick.item_id, str(seg.pick.item_id))
        lot = seg.lot
        if lot is None or seg.uncovered:
            problems.append({"code": "lot_missing", "item_id": seg.pick.item_id,
                             "detail": f"{name}: QC'si bitmiş ve SKT'si geçerli lot yetersiz — "
                                       "laboratuvar lot / SKT kaydını tamamlamalı ya da malzeme gelmeli."})
        elif not strict_lot_ok(lot, today):
            problems.append({"code": "lot_not_released", "item_id": seg.pick.item_id,
                             "detail": f"{name} lot {lot.lot_number}: QC bekliyor / numune / SKT geçersiz."})
    return problems


def start_batch(db, domain, actor, order_id, data):
    order = _order(db, order_id, domain, lock=True)
    _require_open(order)
    if order.status != "APPROVED":
        fail("Parti yalnız yönetim onaylı siparişte başlatılır.", "invalid_stage", 409)
    if _valid_signature(db, order) is None:
        fail("Güncel sürümün yönetim onayı yok.", "approval_required", 409)
    gate = payment_gate(db, order)
    if not gate["production_ok"]:
        fail("Ödeme koşulu üretim için henüz sağlanmadı.", "payment_required", 409)
    if not order.prep_confirmed_at:
        fail("Teknik sorumlunun son hazırlık kontrolü bekleniyor.", "preparation_required", 409)
    _, tech = _revision(db, order, "technical")
    line = next((l for l in tech["lines"] if l["item_id"] == int(data.get("item_id", 0))), None)
    if line is None or line["produce"] <= EPS or not line.get("recipe_id"):
        fail("Bu ürün için üretim planı yok.", "no_production_line")
    qty = _qty(data.get("quantity"))
    if _whole_only(line.get("unit")) and qty != int(qty):
        fail("Adet kesirli olamaz.", "invalid_quantity")
    progress = _line_progress(db, order, tech)[line["item_id"]]
    if qty > progress["remaining_to_start"] + EPS:
        fail(f"Kalan üretim {progress['remaining_to_start']:g} — parti bunu aşamaz.", "over_plan", 409)
    recipe = (db.query(Recipe).filter(Recipe.id == line["recipe_id"], Recipe.domain == domain,
                                      Recipe.is_active == True).first())                    # noqa: E712
    if recipe is None or recipe.target_item_id != line["item_id"]:
        fail("Onaylı reçete artık aktif değil; teknik değerlendirme yenilenmeli.", "recipe_changed", 409)
    if line.get("recipe_hash") and recipe_fingerprint(db, recipe) != line["recipe_hash"]:
        fail("Reçete teknik değerlendirmeden sonra değişti — teknik değerlendirme ve yönetim onayı "
             "yenilenmeli.", "recipe_changed", 409)
    lang = order.label_language
    try:
        pl = production_plan.plan(db, recipe, qty, lang, domain=domain, sources=None,
                                  lot_choices=None, lock=True, lot_filter=strict_lot_ok)
    except production_plan.PlanError as exc:
        fail(exc.detail, exc.code, exc.status)
    if pl.errors:
        payload = production_plan.preview_payload(pl)
        fail(pl.first_error()["detail"], pl.first_error()["code"], 409,
             errors=pl.errors, blockers=payload["blockers"])
    problems = _check_strict_lots(pl)
    if problems:
        fail(problems[0]["detail"], problems[0]["code"], 409, errors=problems)
    target = (db.query(Item).filter(Item.id == recipe.target_item_id, Item.domain == domain)
              .with_for_update().populate_existing().first())
    seq = lots.next_sequence(db, target)
    lot_no = lots.format_lot(lots.lot_prefix(target), seq)
    target.lot_seq = seq
    rows = []

    def record(kind, tx, *, item_id, recipe_item_id=None, lot=None, lot_number=None,
               quantity, factor=1.0, unit=None, phase=None):
        sup = lot.supplier if (lot is not None and lot.supplier_id) else None
        if sup is None:
            card = pl.cards.get(item_id)
            sup = card.supplier if card is not None and card.supplier_id else None
        rows.append({"kind": kind, "tx": tx, "item_id": item_id, "recipe_item_id": recipe_item_id,
                     "inventory_id": lot.id if lot is not None else None, "lot_number": lot_number,
                     "supplier_id": sup.id if sup else None, "supplier_name": sup.name if sup else None,
                     "quantity": quantity, "factor": factor, "unit": unit, "phase": phase})

    _, name = _actor(actor)
    lang_label = "İngilizce" if lang == "EN" else "Türkçe"
    production_run.write_consumption(db, pl, recipe, produced_lot=lot_no, actor=name,
                                     sel_lang_label=lang_label, record=record)
    db.flush()
    snapshot = [{**{k: v for k, v in r.items() if k != "tx"}, "transaction_id": r["tx"].id} for r in rows]
    batch = B2BOrderBatch(order_id=order.id, item_id=line["item_id"], recipe_id=recipe.id,
                          planned_quantity=qty, lot_number=lot_no, status="STARTED", label_language=lang,
                          commercial_revision=order.commercial_revision,
                          technical_revision=order.technical_revision,
                          plan_snapshot=_json({"recipe_name": recipe.name, "rows": snapshot,
                                               "label_warnings": list(pl.label_warnings)}),
                          started_by=name)
    db.add(batch)
    db.flush()
    order.updated_at = datetime.utcnow()
    _audit(db, actor, "batch_start", order.id, {"batch_id": batch.id, "item_id": line["item_id"],
                                                "quantity": qty, "lot_number": lot_no,
                                                "outputs": len(snapshot)})
    return batch


def _batch(db, order, batch_id, *, lock=True):
    q = db.query(B2BOrderBatch).filter_by(id=batch_id, order_id=order.id)
    row = q.with_for_update().populate_existing().first() if lock else q.first()
    if row is None:
        fail("Parti bulunamadı.", "not_found", 404)
    return row


class _NoCards:
    cards = {}


def complete_batch(db, domain, actor, order_id, batch_id, data, *, retention_months):
    """Bitmiş ürünü yaz: ProductionHistory + başlangıç dökümü + çıktı.  Malzeme
    ikinci kez düşülmez (Output'lar parti başlangıcında yazıldı)."""
    order = _order(db, order_id, domain, lock=True)
    batch = _batch(db, order, batch_id)
    if batch.status != "STARTED":
        fail("Yalnız başlamış parti tamamlanabilir.", "batch_not_started", 409)
    produced = _qty(data.get("produced_quantity"), field="Üretilen miktar")
    witness = _qty(data.get("witness_quantity", 0), zero=True, field="Şahit numune")
    if witness > produced + EPS:
        fail("Şahit numune üretilen miktarı aşamaz.", "invalid_witness")
    recipe = db.query(Recipe).filter(Recipe.id == batch.recipe_id).first()
    target = (db.query(Item).filter(Item.id == batch.item_id, Item.domain == domain)
              .with_for_update().populate_existing().first())
    if recipe is None or target is None:
        fail("Reçete / ürün kartı bulunamadı.", "not_found", 404)
    if _whole_only(target.unit) and (produced != int(produced) or witness != int(witness)):
        fail("Adet kesirli olamaz.", "invalid_quantity")
    snap = json.loads(batch.plan_snapshot)
    _, name = _actor(actor)
    now = datetime.utcnow()
    prod = ProductionHistory(recipe_id=recipe.id, recipe_name=snap.get("recipe_name") or recipe.name,
                             target_item_id=target.id, target_item_name=target.name,
                             produced_quantity=produced, produced_at=now, produced_by=name,
                             lot_number=batch.lot_number, witness_quantity=0.0, domain=order.domain)
    db.add(prod)
    db.flush()
    existing_lots = {i for (i,) in db.query(Inventory.id).filter(
        Inventory.id.in_([r["inventory_id"] for r in snap["rows"] if r.get("inventory_id")] or [0]))}
    for r in snap["rows"]:
        db.add(ProductionConsumption(
            production_id=prod.id, kind=r["kind"], recipe_item_id=r.get("recipe_item_id"),
            item_id=r["item_id"],
            inventory_id=r["inventory_id"] if r.get("inventory_id") in existing_lots else None,
            lot_number=r.get("lot_number"), supplier_id=r.get("supplier_id"),
            supplier_name=r.get("supplier_name"), quantity=r["quantity"], factor=r.get("factor") or 1.0,
            unit=r.get("unit"), phase=r.get("phase"), transaction_id=r["transaction_id"],
            source="live", domain=order.domain))
    lang_label = "İngilizce" if batch.label_language == "EN" else "Türkçe"
    witness_qty, showroom_qty, brand = production_run.write_output(
        db, recipe, target_item_row=target, produced_quantity=produced, witness_quantity=witness,
        produced_lot=batch.lot_number, actor=name, sel_lang_label=lang_label, prod=prod, now=now,
        retention_months=retention_months, record=production_run.recorder(db, prod, _NoCards, order.domain))
    batch.status, batch.completed_by, batch.completed_at = "COMPLETED", name, now
    batch.produced_quantity, batch.witness_quantity = produced, witness_qty
    batch.production_history_id = prod.id
    order.updated_at = now
    _audit(db, actor, "batch_complete", order.id, {"batch_id": batch.id, "production_id": prod.id,
                                                   "produced": produced, "witness": witness_qty,
                                                   "planned": batch.planned_quantity})
    return batch


def cancel_batch(db, domain, actor, order_id, batch_id, data):
    order = _order(db, order_id, domain, lock=True)
    batch = _batch(db, order, batch_id)
    if batch.status != "STARTED":
        fail("Yalnız başlamış (tamamlanmamış) parti iptal edilir.", "batch_not_started", 409)
    reason = str(data.get("reason") or "").strip()
    if len(reason) < 5:
        fail("En az beş karakterlik iptal gerekçesi gerekli.", "reason_required")
    batch.status, batch.cancel_reason = "CANCELLED", reason[:2000]
    batch.cancelled_by, batch.cancelled_at = _actor(actor)[1], datetime.utcnow()
    _audit(db, actor, "batch_cancel", order.id, {"batch_id": batch.id, "reason": reason[:500],
                                                 "auto_return": False})
    return batch


def refresh_shortages(db, domain, actor, order_id):
    """Parti başlangıcında kart stoğu yetmedi (hiçbir şey yazılmadı) → kalan
    üretimin GÜNCEL eksikleri alım satırlarına işlenir (spec §3)."""
    order = _order(db, order_id, domain, lock=True)
    if order.status != "APPROVED":
        return None
    _, tech = _revision(db, order, "technical")
    progress = _line_progress(db, order, tech)
    produce = [{"recipe_id": l["recipe_id"], "produce": progress[l["item_id"]]["remaining_to_start"]}
               for l in (tech or {}).get("lines", [])
               if l.get("recipe_id") and progress[l["item_id"]]["remaining_to_start"] > EPS]
    materials, _ = _shortages(db, domain, order.label_language, produce)
    _sync_purchase_lines(db, order, materials, actor)
    _audit(db, actor, "shortage_refresh", order.id,
           {"shortage_count": sum(1 for m in materials if m["buy"] > EPS)})
    return materials


def return_batch_material(db, domain, actor, order_id, batch_id, data):
    """İptal edilen partiden fiziksel iade — tüketimle sınırlı +Adjustment."""
    order = _order(db, order_id, domain, lock=True)
    batch = _batch(db, order, batch_id)
    if batch.status != "CANCELLED":
        fail("Fiziksel iade yalnız iptal edilmiş partiden yapılır.", "batch_not_cancelled", 409)
    reason = str(data.get("reason") or "").strip()
    if len(reason) < 5:
        fail("En az beş karakterlik iade gerekçesi gerekli.", "reason_required")
    snap = json.loads(batch.plan_snapshot)
    consumed, first_lot = {}, {}
    for r in snap["rows"]:
        consumed[r["item_id"]] = consumed.get(r["item_id"], 0.0) + float(r["quantity"])
        if r.get("inventory_id") and r["item_id"] not in first_lot:
            first_lot[r["item_id"]] = r["inventory_id"]
    item_id = int(data.get("item_id", 0))
    if item_id not in consumed:
        fail("Bu malzeme partide tüketilmedi.", "invalid_item")
    qty = _qty(data.get("quantity"))
    returned = float(db.query(func.coalesce(func.sum(B2BOrderBatchReturn.quantity), 0.0))
                     .filter(B2BOrderBatchReturn.batch_id == batch.id,
                             B2BOrderBatchReturn.item_id == item_id).scalar() or 0)
    if qty > consumed[item_id] - returned + EPS:
        fail(f"İade, partide tüketilen kalan miktarı ({consumed[item_id] - returned:g}) aşamaz.",
             "over_consumed", 409)
    item = db.query(Item).filter(Item.id == item_id).with_for_update().populate_existing().first()
    if item is None or not item.is_active:
        fail("Malzeme kartı aktif değil; iade yapılamaz.", "invalid_item")
    lot = None
    if first_lot.get(item_id):
        lot = (db.query(Inventory).filter(Inventory.id == first_lot[item_id])
               .with_for_update().populate_existing().first())
    item.current_stock = round(float(item.current_stock or 0) + qty, 6)
    if lot is not None:
        lot.quantity = round(float(lot.quantity or 0) + qty, 6)
        lot.updated_at = datetime.utcnow()
    _, name = _actor(actor)
    tx = Transaction(item_id=item.id, lot_number=lot.lot_number if lot is not None else None,
                     transaction_type="Adjustment", quantity=qty, performed_by=name,
                     notes=(f"{BATCH_RETURN_MARK} — Sipariş #{order.id} | Parti lot {batch.lot_number} | "
                            f"Gerekçe: {reason[:300]}"))
    db.add(tx)
    db.flush()
    db.add(B2BOrderBatchReturn(batch_id=batch.id, item_id=item.id, quantity=qty, reason=reason[:2000],
                               transaction_id=tx.id, created_by=name))
    _audit(db, actor, "batch_return", order.id, {"batch_id": batch.id, "item_id": item.id,
                                                 "quantity": qty, "transaction_id": tx.id})
    return batch


# ─── Sevkiyat ────────────────────────────────────────────────────────────────

def shipment_readiness(db, order, tech=None, com=None):
    if tech is None:
        _, tech = _revision(db, order, "technical")
    if com is None:
        _, com = _revision(db, order, "commercial")
    problems = []
    if order.status != "APPROVED":
        problems.append("Sipariş yönetim onaylı değil.")
    if db.query(B2BOrderBatch.id).filter_by(order_id=order.id, status="STARTED").first():
        problems.append("Tamamlanmamış parti var.")
    gate = payment_gate(db, order, com)
    if not gate["shipment_ok"]:
        problems.append("Ödeme koşulu sevkiyat için sağlanmadı.")
    lines = []
    for l in (com or {}).get("lines", []):
        released = released_stock(db, l["item_id"])
        pending = float(db.query(func.coalesce(func.sum(Inventory.quantity), 0.0))
                        .filter(Inventory.item_id == l["item_id"], Inventory.qc_required == True,   # noqa: E712
                                Inventory.quantity > 0).scalar() or 0)
        ok = released + EPS >= l["quantity"]
        lines.append({"item_id": l["item_id"], "name": l["name"], "ordered": l["quantity"],
                      "released_stock": round(released, 6), "qc_pending": round(pending, 6), "ok": ok})
        if not ok:
            problems.append(f"{l['name']}: sevke uygun (QC'si bitmiş, şahit olmayan) stok "
                            f"{released:g} < {l['quantity']:g}.")
    return {"ready": not problems, "problems": problems, "lines": lines}


def ship(db, domain, actor, order_id, data):
    """Tam sipariş tek sevkiyat — kilit altında yeniden doğrulanır, BİR kez düşer."""
    from routers.delivery import DeliveryCreate, DeliveryError, build_delivery, ship_core
    order = _order(db, order_id, domain, lock=True)
    _require_open(order)
    if order.delivery_id:
        fail("Sipariş zaten sevk edildi.", "already_shipped", 409)
    _, com = _revision(db, order, "commercial")
    _, tech = _revision(db, order, "technical")
    tracking = str(data.get("tracking_no") or "").strip()
    if not tracking:
        fail("Kargo takip numarası zorunludur.", "tracking_required")
    ids = sorted(l["item_id"] for l in com["lines"])
    db.query(Item).filter(Item.id.in_(ids)).order_by(Item.id).with_for_update().populate_existing().all()
    ready = shipment_readiness(db, order, tech, com)
    if not ready["ready"]:
        fail(ready["problems"][0], "not_ready", 409, problems=ready["problems"])
    cust = com["customer"]
    payload = DeliveryCreate(
        recipient_name=cust["name"], recipient_org=cust["name"][:150],
        recipient_phone=(cust.get("phone") or None), delivery_type="kargo", method="kargo",
        note=f"B2B sipariş #{order.id} — {com['quote_number']}", doc_lang=order.label_language,
        customer_address=cust.get("address") or None, customer_country=cust.get("country") or None,
        currency=com["currency"],
        items=[{"item_id": l["item_id"], "quantity": l["quantity"],
                "unit_price": l["unit_price"]} for l in com["lines"]])
    _, name = _actor(actor)
    try:
        delivery = build_delivery(db, payload, name, domain)
        result = ship_core(db, delivery, tracking, data.get("carrier"), name, released_only=True)
    except DeliveryError as exc:
        fail(exc.detail, "delivery_error", exc.status_code)
    order.status, order.delivery_id = "SHIPPED", delivery.id
    order.shipped_at, order.shipped_by = datetime.utcnow(), name
    order.updated_at = order.shipped_at
    _audit(db, actor, "ship", order.id, {"delivery_id": delivery.id, "document_no": result["document_no"],
                                         "tracking_no": tracking})
    return order


def cancel_order(db, domain, actor, order_id, data):
    order = _order(db, order_id, domain, lock=True)
    _require_open(order)
    reason = str(data.get("reason") or "").strip()
    if len(reason) < 5:
        fail("En az beş karakterlik iptal gerekçesi gerekli.", "reason_required")
    if db.query(B2BOrderBatch.id).filter_by(order_id=order.id, status="STARTED").first():
        fail("Açık parti var — önce partiyi tamamlayın ya da iptal edin.", "open_batch", 409)
    order.status, order.cancel_reason = "CANCELLED", reason[:2000]
    order.cancelled_at, order.cancelled_by = datetime.utcnow(), _actor(actor)[1]
    order.updated_at = order.cancelled_at
    _audit(db, actor, "cancel", order.id, {"reason": reason[:500]})
    return order


# ─── Okuma modelleri ─────────────────────────────────────────────────────────

_ACTION_LABEL = {
    "convert": "Siparişe dönüştürüldü (ticari onay)", "commercial_revision": "Ticari revizyon",
    "reassign": "Sorumlu değişti", "technical_review": "Teknik değerlendirme",
    "purchase_line": "Alım satırı güncellendi", "sign": "Yönetim onayı (imza)",
    "payment": "Ödeme doğrulandı", "preparation": "Son hazırlık kontrolü",
    "batch_start": "Parti başlatıldı", "batch_complete": "Parti tamamlandı",
    "batch_cancel": "Parti iptal edildi", "batch_return": "Partiden fiziksel iade",
    "ship": "Sevk edildi", "cancel": "Sipariş iptal edildi", "proforma": "Proforma indirildi",
    "shortage_refresh": "Eksikler güncellendi (parti başlangıcında stok yetmedi)",
}


def next_step(db, order, com=None):
    """Bekleyen tek adım: (kim — user_id ya da yetki, ne)."""
    if order.status == "SUBMITTED":
        return {"user_id": order.technical_user_id, "text": "Teknik değerlendirme"}
    if order.status == "TECH_REVIEWED":
        return {"user_id": order.signer_user_id, "text": "Yönetim onayı (şifre + imza)"}
    if order.status != "APPROVED":
        return None
    gate = payment_gate(db, order, com)
    if not gate["production_ok"]:
        return {"permission": "payment", "text": "Ödeme doğrulaması"}
    if not order.prep_confirmed_at:
        return {"user_id": order.technical_user_id, "text": "Son hazırlık kontrolü"}
    _, tech = _revision(db, order, "technical")
    progress = _line_progress(db, order, tech)
    if any(p["started"] > EPS for p in progress.values()):
        return {"permission": "produce", "text": "Açık partiyi tamamla"}
    pending = [l for l in (tech or {}).get("lines", [])
               if progress.get(l["item_id"], {}).get("remaining_to_start", 0) > EPS]
    if any(_recipe_changed(db, l) for l in pending):
        return {"user_id": order.technical_user_id, "text": "Reçete değişti — teknik değerlendirmeyi yenile"}
    if pending:
        return {"permission": "produce", "text": "Üretim partisi başlat"}
    return {"permission": "ship", "text": "Sevkiyat (QC bitince)"}


def order_summary(db, order, *, show_prices, names=None):
    names = {} if names is None else names
    q = order.quotation
    step = next_step(db, order)
    return {"id": order.id, "reference": q.quote_number if q else f"SIP-{order.id}",
            "status": order.status, "status_label": STATUS_LABEL.get(order.status, order.status),
            "customer_name": q.customer_name if q else "", "customer_country": (q.customer_country or "") if q else "",
            "currency": q.currency if q else "", "total": _money(q.total_amount) if (q and show_prices) else None,
            "label_language": order.label_language,
            "target_date": order.target_date.isoformat() if order.target_date else None,
            "owner": _user_name(db, order.owner_user_id, names),
            "technical_user": _user_name(db, order.technical_user_id, names),
            "signer": _user_name(db, order.signer_user_id, names),
            "next_step": ({"text": step["text"], "who": _user_name(db, step["user_id"], names)
                           if step.get("user_id") else {"payment": "Ödeme yetkilisi",
                                                       "produce": "Laboratuvar",
                                                       "ship": "Sevkiyat yetkilisi"}[step["permission"]]}
                          if step else None),
            "created_at": _when(order.created_at), "updated_at": _when(order.updated_at)}


def list_orders(db, domain, *, show_prices, status=None):
    q = db.query(B2BOrder).filter(B2BOrder.domain == domain)
    if status == "open":
        q = q.filter(B2BOrder.status.in_(OPEN))
    elif status:
        q = q.filter(B2BOrder.status == status)
    names = {}
    return [order_summary(db, o, show_prices=show_prices, names=names)
            for o in q.order_by(B2BOrder.id.desc()).limit(300).all()]


def order_detail(db, order, *, user, show_prices):
    from core.permissions import _resolve_permissions
    perms = (_resolve_permissions(user).get("b2b_orders") or {}) if user else {}
    names = {}
    com_row, com = _revision(db, order, "commercial")
    tech_row, tech = _revision(db, order, "technical")
    gate = payment_gate(db, order, com)
    sig = _valid_signature(db, order)
    progress = _line_progress(db, order, tech) if tech else {}
    tech_lines = {l["item_id"]: l for l in (tech or {}).get("lines", [])}
    lines = []
    for l in com["lines"]:
        t = tech_lines.get(l["item_id"], {})
        p = progress.get(l["item_id"], {})
        row = {"item_id": l["item_id"], "name": l["name"], "unit": l["unit"], "quantity": l["quantity"],
               "use_stock": t.get("use_stock"), "produce": t.get("produce"),
               "recipe_id": t.get("recipe_id"), "recipe_name": t.get("recipe_name"),
               "recipe_changed": _recipe_changed(db, t) if t else False,
               "released_stock": round(released_stock(db, l["item_id"]), 6), **p,
               "recipe_options": [{"id": r.id, "name": r.name}
                                  for r in _recipes_for(db, l["item_id"], order.domain)]}
        if show_prices:
            row.update(unit_price=l["unit_price"], line_total=l["line_total"])
        lines.append(row)
    uid = user.id if user else None
    batches = []
    for b in db.query(B2BOrderBatch).filter_by(order_id=order.id).order_by(B2BOrderBatch.id).all():
        snap = json.loads(b.plan_snapshot)
        consumed = {}
        for r in snap["rows"]:
            c = consumed.setdefault(r["item_id"], {"item_id": r["item_id"], "quantity": 0.0, "unit": r.get("unit")})
            c["quantity"] = round(c["quantity"] + float(r["quantity"]), 6)
        returned = {}
        for (iid, total) in (db.query(B2BOrderBatchReturn.item_id, func.sum(B2BOrderBatchReturn.quantity))
                             .filter(B2BOrderBatchReturn.batch_id == b.id).group_by(B2BOrderBatchReturn.item_id)):
            returned[iid] = float(total or 0)
        item_names = {i.id: i.name for i in db.query(Item).filter(Item.id.in_(list(consumed) or [0]))}
        batches.append({"id": b.id, "item_id": b.item_id, "lot_number": b.lot_number, "status": b.status,
                        "planned_quantity": b.planned_quantity, "produced_quantity": b.produced_quantity,
                        "witness_quantity": b.witness_quantity, "started_by": b.started_by,
                        "started_at": _when(b.started_at), "completed_by": b.completed_by,
                        "completed_at": _when(b.completed_at), "cancel_reason": b.cancel_reason or "",
                        "production_history_id": b.production_history_id,
                        "consumed": [{**c, "name": item_names.get(c["item_id"], "—"),
                                      "returned": round(returned.get(c["item_id"], 0.0), 6)}
                                     for c in consumed.values()]})
    purchase = [{"id": p.id, "item_id": p.item_id, "item_name": p.item_name, "unit": p.unit,
                 "need_quantity": p.need_quantity, "supplier_name": p.supplier_name or "",
                 "status": p.status, "ordered_quantity": p.ordered_quantity,
                 "received_quantity": p.received_quantity, "note": p.note or "",
                 "technical_revision": p.technical_revision, "updated_by": p.updated_by or "",
                 "updated_at": _when(p.updated_at)}
                for p in db.query(B2BOrderPurchaseLine).filter_by(order_id=order.id)
                .order_by(B2BOrderPurchaseLine.id).all()]
    timeline = [{"at": _when(a.timestamp), "actor": a.actor_name or "—",
                 "action": _ACTION_LABEL.get((a.action or "").split(".", 1)[-1], a.action)}
                for a in db.query(AdminAuditLog).filter(AdminAuditLog.target_type == "b2b_order",
                                                        AdminAuditLog.target_id == order.id)
                .order_by(AdminAuditLog.id).all()]
    ready = shipment_readiness(db, order, tech, com) if order.status == "APPROVED" else None
    open_ = order.status in OPEN
    is_tech, is_signer = uid == order.technical_user_id, uid == order.signer_user_id
    has_open_batch = any(b["status"] == "STARTED" for b in batches)
    actions = {
        "revise": open_ and bool(perms.get("manage")) and show_prices,
        "reassign": open_ and bool(perms.get("manage")),
        "tech_review": open_ and is_tech and bool(perms.get("tech_review")),
        "sign": order.status == "TECH_REVIEWED" and is_signer and bool(perms.get("sign")),
        "payment": order.status == "APPROVED" and bool(perms.get("payment")) and show_prices,
        "prepare": order.status == "APPROVED" and is_tech and bool(perms.get("tech_review"))
                   and not order.prep_confirmed_at,
        "purchase": open_ and bool(perms.get("manage") or perms.get("payment") or perms.get("tech_review")),
        "produce": order.status == "APPROVED" and bool(perms.get("produce")),
        "ship": bool(ready and ready["ready"]) and bool(perms.get("ship")),
        "cancel": open_ and bool(perms.get("manage")) and not has_open_batch,
        "proforma": sig is not None and show_prices,
    }
    detail = {
        "order": {**order_summary(db, order, show_prices=show_prices, names=names),
                  "payment_terms": order.payment_terms,
                  "payment_terms_label": PAYMENT_TERMS.get(order.payment_terms, order.payment_terms),
                  "advance_percent": order.advance_percent, "owner_user_id": order.owner_user_id,
                  "bank_profile_ids": _order_bank_ids(order),
                  "proforma_terms": {**proforma_template.DEFAULT_TERMS, **_stored_terms(order.proforma_terms)},
                  "technical_user_id": order.technical_user_id, "signer_user_id": order.signer_user_id,
                  "commercial_revision": order.commercial_revision,
                  "technical_revision": order.technical_revision,
                  "prep_confirmed_at": _when(order.prep_confirmed_at),
                  "prep_confirmed_by": order.prep_confirmed_by or "",
                  "shipped_at": _when(order.shipped_at), "shipped_by": order.shipped_by or "",
                  "delivery_id": order.delivery_id, "cancel_reason": order.cancel_reason or "",
                  "document_hash": document_hash(order, com_row, tech_row) if tech_row else None},
        "customer": {k: v for k, v in com["customer"].items()},
        "lines": lines,
        "technical": ({"revision": tech_row.revision, "created_by": tech_row.created_by,
                       "created_at": _when(tech_row.created_at), "note": tech.get("note", ""),
                       "warnings": tech.get("warnings", []), "materials": tech.get("materials", []),
                       "stale": not technical_current(com, tech)}
                      if tech_row else None),
        "signature": ({"signer_name": sig.signer_name, "signed_at": _when(sig.signed_at),
                       "commercial_revision": sig.commercial_revision,
                       "technical_revision": sig.technical_revision, "document_hash": sig.document_hash}
                      if sig else None),
        "payment": {k: gate[k] for k in ("production_ok", "shipment_ok")},
        "purchase_lines": purchase, "batches": batches, "timeline": timeline,
        "shipment": ready, "actions": actions,
    }
    if show_prices:
        detail["commercial"] = {k: com.get(k) for k in ("currency", "subtotal", "tax_percentage", "tax_amount",
                                                        "shipping", "total", "notes", "valid_days")}
        detail["commercial"]["banks"] = snapshot_banks(com)
        detail["commercial"]["bank"] = (detail["commercial"]["banks"] or [None])[0]
        detail["commercial"]["terms"] = com.get("terms") or proforma_template.merged_terms(
            {}, proforma_template.payment_terms_text(com.get("payment_terms"), com.get("advance_percent")))
        detail["payment"].update(gate)
        detail["payments"] = [{"id": p.id, "amount": p.amount, "currency": p.currency,
                               "received_on": p.received_on.isoformat() if p.received_on else None,
                               "reference": p.reference or "", "note": p.note or "",
                               "verified_by": p.verified_by or "", "created_at": _when(p.created_at)}
                              for p in db.query(B2BOrderPayment).filter_by(order_id=order.id)
                              .order_by(B2BOrderPayment.id).all()]
        if sig:
            detail["signature"]["image"] = "data:image/png;base64," + sig.signature_png
    else:
        # Teknik görünüm: banka ve satış fiyatları yok; malzeme alım tutarları da gizli.
        detail["order"].pop("bank_profile_ids", None)
        if detail["technical"]:
            detail["technical"]["materials"] = [{k: v for k, v in m.items() if k not in ("price", "amount")}
                                                for m in detail["technical"]["materials"]]
    return detail


def my_tasks(db, user):
    """Ana ekran "Benden bekleyen işler" — B2B sipariş adımları + fason onayları."""
    from core.permissions import _resolve_permissions
    if user is None or user.role == "Distributor":
        return []
    perms = _resolve_permissions(user)
    b2b = perms.get("b2b_orders") or {}
    tasks = []
    if b2b.get("view"):
        for order in (db.query(B2BOrder).filter(B2BOrder.status.in_(OPEN))
                      .order_by(B2BOrder.id).limit(200).all()):
            step = next_step(db, order)
            if not step:
                continue
            mine = (step.get("user_id") == user.id) or (step.get("permission") and b2b.get(step["permission"]))
            if mine:
                q = order.quotation
                tasks.append({"kind": "b2b_order", "id": order.id, "domain": order.domain,
                              "title": f"{q.quote_number if q else order.id} · {q.customer_name if q else ''}",
                              "text": step["text"], "url": f"/b2b-siparisler#{order.id}"})
    out_perms = perms.get("outsourcing") or {}
    if out_perms.get("approve"):
        from database import OutsourcingApproval, OutsourcingJob
        for job in (db.query(OutsourcingJob)
                    .filter(OutsourcingJob.status.in_(("DRAFT", "TECH_APPROVED")),
                            OutsourcingJob.frozen_at.is_(None)).order_by(OutsourcingJob.id).limit(200).all()):
            roles = [r for r in ("technical", "owner", "manager") if getattr(job, r + "_user_id") == user.id]
            if not roles:
                continue
            done = {a.role for a in db.query(OutsourcingApproval).filter_by(job_id=job.id, revision=job.revision)}
            pending = [r for r in roles if r not in done]
            if pending and ("technical" in done or "technical" in pending):
                tasks.append({"kind": "outsourcing", "id": job.id, "domain": job.domain,
                              "title": f"FS-{job.id} · {job.external_product_name}",
                              "text": "Fason paket onayı", "url": "/outsourcing"})
    return tasks
