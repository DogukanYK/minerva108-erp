# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Minerva Asistan — operasyon araçları (13.10.2026, "her şeyi okusun" kararı).

`handlers.py` ile aynı sözleşme: `fn(ctx, db, args) -> dict`, HEPSİ salt-okunur,
izin denetimi `tools.run_tool`'da (her araç kendi modülünün okuma izniyle —
sistemdeki uçla AYNI).  Mevcut motorlar yeniden kullanılır; ikinci bir hesap
kopyası yazılmaz:

  deliveries          routers/delivery (Delivery + satırlar)       inventory.view
  product_returns     routers/returns (ProductReturn)               inventory.view
  b2b_orders          core.b2b_orders.list_orders / order_detail    b2b_orders.view
  quotations          Quotation (B2B teklifleri)                    b2b.view
  qc_pending          routers/production.list_quarantine mantığı    qc.view
  outsourcing_jobs    core.outsourcing.list_jobs / job_detail       outsourcing.view
  stock_movements     Transaction defteri (kart + tarih aralığı)    inventory|reports.view
  consumption         routers/reports.usage_between                 reports.view
  month_end_stock     routers/reports.report_monthly_stock          reports.view
  production_capacity core.production_sim.simulate                  reports.view
  retention_samples   routers/retention._filtered_query             retention.view
  sample_analyses     SampleAnalysis (numune analiz formları)       qc.view
  trendyol_orders     core.trendyol.orders_view                     trendyol.view
  shopify_orders      ShopifyOrder                                  admin.view
  influencer_overview kampanya düzeyi toplamlar                     influencer.view
  reviews_overview    ürün bazında puan özetleri                    reviews.view

VERİ SINIRI (kullanıcı kararı 13.10.2026 — "yalnız işin verisi"): kişisel
İLETİŞİM verisi (telefon, e-posta, adres, T.C./VKN) hiçbir araçtan ÇIKMAZ;
müşteri/alıcı yalnız firma (yoksa ad) olarak görünür; creator ve yorumcu
kimliği hiç çıkmaz.  Fiyat/tutar modülün kendi fiyat kuralıyla: B2B tutarları
`b2b.view`, Trendyol komisyon/kâr `core.trendyol.can_profit`.  Panel süzgeci
domain kolonu olan her tabloda `ctx.domain`; Trendyol/Shopify/influencer/
yorumlar sistemde de panelden bağımsızdır (cross-cutting).
"""
import datetime as _dt
import json
from collections import Counter
from typing import TYPE_CHECKING, Dict, Optional

from sqlalchemy.orm import Session, selectinload

from database import (B2BOrder, Delivery, InfluencerCampaign, InfluencerCollab, InfluencerContent,
                      InfluencerShipment, Inventory, Item, ProductReturn, ProductReview, Quotation,
                      SampleAnalysis, ShopifyOrder, Transaction, User, to_tr, tr_now)
from core.assistant.handlers import (ToolError, _date_text, _dt_text, _int_arg, _parse_iso, _r,
                                     _ratio, _text)
from core.production_history_report import date_in_range, utc_bounds
from core.supplier_prices import normalize

if TYPE_CHECKING:
    from core.assistant.tools import ToolContext

ROWS_MAX = 15
LINES_MAX = 8
NOTE_MAX = 200

DELIVERY_FILTERS = ("kargo_bekleyen", "onay_bekleyen", "kargolanan", "son")
B2B_FILTERS = ("open", "all", "SUBMITTED", "TECH_REVIEWED", "APPROVED", "SHIPPED", "CANCELLED")
QUOTE_STATUSES = ("DRAFT", "PENDING", "CONFIRMED", "REJECTED", "ORDER")
QUOTE_LABELS = {"DRAFT": "Taslak", "PENDING": "Onay bekliyor", "CONFIRMED": "Onaylandı",
                "REJECTED": "Reddedildi", "ORDER": "Siparişe dönüştü"}
JOB_LABELS = {"DRAFT": "Taslak", "TECH_APPROVED": "Teknik onaylı", "APPROVED": "Onaylandı",
              "DISPATCHED": "Sevk edildi", "RECONCILING": "Mutabakatta", "CLOSED": "Kapandı",
              "CANCELLED": "İptal"}
MONTH_CATEGORIES = ("all", "hammadde", "ambalaj", "etiket", "finished")
RETENTION_STATUSES = ("stored", "depleted", "destroyed")
CONSUMPTION_DEFAULT_DAYS = 30


def _fold(s) -> str:
    return normalize(s or "")


def _matches(q_norm: str, *texts) -> bool:
    return not q_norm or any(q_norm in _fold(t) for t in texts if t)


def _note(s) -> str:
    s = _text(s)
    return s[:NOTE_MAX] + ("…" if len(s) > NOTE_MAX else "")


def _range(args: dict, default_days: int):
    """TR takvim günleri [start, end] — boşsa son `default_days` gün."""
    today = tr_now().date()
    start, end = _parse_iso(args.get("start")), _parse_iso(args.get("end"))
    if end is None:
        end = today
    if start is None:
        start = end - _dt.timedelta(days=default_days - 1)
    if not (date_in_range(start) and date_in_range(end)):
        raise ToolError("Tarih 2000–2100 aralığında olmalı")
    if start > end:
        raise ToolError("Başlangıç bitişten sonra olamaz")
    return start, end


def _user(db: Session, ctx: "ToolContext") -> Optional[User]:
    return db.query(User).filter(User.id == ctx.user_id).first()


# ─── deliveries ──────────────────────────────────────────────────────────────

def _delivery_row(d: Delivery, labels) -> dict:
    type_l, method_l, status_l = labels
    items = sorted(d.items or [], key=lambda i: i.id)
    st = d.status or "completed"
    return {
        "id": d.id, "document_no": d.document_no or "", "date": _dt_text(d.created_at),
        "type": type_l.get(d.delivery_type, d.delivery_type or ""),
        "method": method_l.get(d.method, d.method or ""),
        "status": st, "status_label": status_l.get(st, st),
        # Alıcı: firma, yoksa ad — telefon / adres ASLA (kullanıcı kararı)
        "recipient": (d.recipient_org or "").strip() or (d.recipient_name or "").strip(),
        "country": d.customer_country or "",
        "items": [{"name": i.item_name, "qty": _r(i.quantity), "unit": i.unit or ""}
                  for i in items[:LINES_MAX]],
        "items_total": len(items),
        "carrier": d.carrier or "", "tracking_no": d.tracking_no or "",
        "shipped_at": _dt_text(d.shipped_at),
    }


def deliveries(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Teslimat / kargo kuyruğu.  `kargo_bekleyen` = Teslimat sayfasındaki
    "Bekleyen Kargo Sevkiyatları" (kargo + preparing — routers/delivery.
    _pending_shipments ile aynı); `onay_bekleyen` = proforma + pending ve
    sistemdeki gibi YALNIZ SuperAdmin görür."""
    from routers.delivery import _METHOD_LABELS, _STATUS_LABELS, _TYPE_LABELS
    labels = (_TYPE_LABELS, _METHOD_LABELS, _STATUS_LABELS)
    flt = args.get("status") or "son"
    if flt not in DELIVERY_FILTERS:
        raise ToolError("status kargo_bekleyen|onay_bekleyen|kargolanan|son olmalı")
    can_pending = ctx.role == "SuperAdmin"
    if flt == "onay_bekleyen" and not can_pending:
        raise ToolError("Onay bekleyen proformaları yalnız SuperAdmin görür")
    limit = _int_arg(args, "limit", 10, 1, ROWS_MAX)
    qn = _fold(args.get("q"))
    base = (db.query(Delivery).options(selectinload(Delivery.items))
            .filter(Delivery.domain == ctx.domain))
    waiting = base.filter(Delivery.delivery_type == "kargo", Delivery.status == "preparing")
    pending = base.filter(Delivery.delivery_type == "proforma", Delivery.status == "pending")
    shipped = base.filter(Delivery.status == "shipped")
    since30 = _dt.datetime.utcnow() - _dt.timedelta(days=30)
    summary = {"kargo_bekleyen": waiting.count(),
               "kargolanan_son_30_gun": shipped.filter(Delivery.shipped_at >= since30).count()}
    if can_pending:
        summary["onay_bekleyen_proforma"] = pending.count()
    query = {"kargo_bekleyen": waiting, "onay_bekleyen": pending,
             "kargolanan": shipped, "son": base}[flt]
    order = Delivery.shipped_at.desc().nullslast() if flt == "kargolanan" else Delivery.id.desc()
    rows = []
    for d in query.order_by(order, Delivery.id.desc()).limit(300).all():
        if qn and not _matches(qn, d.document_no, d.recipient_org, d.recipient_name,
                               *[i.item_name for i in (d.items or [])]):
            continue
        rows.append(_delivery_row(d, labels))
    return {"filter": flt, "summary": summary, "rows": rows[:limit], "count": len(rows),
            "link": "/delivery"}


# ─── product_returns ─────────────────────────────────────────────────────────

def product_returns(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """İade kayıtları (routers/returns._view'in kişisiz alt kümesi — iade eden /
    teslim alan kişi adları çıkmaz)."""
    from routers.returns import _CHANNEL_LABELS, _CONDITION_LABELS
    limit = _int_arg(args, "limit", 10, 1, ROWS_MAX)
    qn = _fold(args.get("q"))
    rows = []
    for r in (db.query(ProductReturn).options(selectinload(ProductReturn.items))
              .filter(ProductReturn.domain == ctx.domain)
              .order_by(ProductReturn.id.desc()).limit(300).all()):
        items = sorted(r.items or [], key=lambda i: i.id)
        if qn and not _matches(qn, r.document_no, r.delivery_document_no,
                               *[i.item_name for i in items]):
            continue
        rows.append({
            "id": r.id, "document_no": r.document_no or "", "date": _dt_text(r.created_at),
            "delivery_document_no": r.delivery_document_no or "",
            "channel": _CHANNEL_LABELS.get(r.channel or "diger", r.channel or ""),
            "reason": _note(r.reason),
            "restocked_qty": _r(sum(i.quantity for i in items if i.restocked)),
            "damaged_qty": _r(sum(i.quantity for i in items if not i.restocked)),
            "items": [{"name": i.item_name, "qty": _r(i.quantity), "unit": i.unit or "",
                       "condition": _CONDITION_LABELS.get(i.condition, i.condition or ""),
                       "restocked": bool(i.restocked)} for i in items[:LINES_MAX]],
        })
    return {"rows": rows[:limit], "count": len(rows), "link": "/returns"}


# ─── b2b_orders ──────────────────────────────────────────────────────────────

def _find_b2b_order(db: Session, domain: str, ref: str) -> Optional[B2BOrder]:
    ref = ref.strip()
    up = ref.upper()
    if up.startswith("SIP-") and up[4:].isdigit():
        return db.query(B2BOrder).filter(B2BOrder.id == int(up[4:]), B2BOrder.domain == domain).first()
    return (db.query(B2BOrder).join(Quotation, Quotation.id == B2BOrder.quotation_id)
            .filter(B2BOrder.domain == domain, Quotation.quote_number.ilike(ref)).first())


def _b2b_summary_row(o: dict, show_prices: bool) -> dict:
    step = o.get("next_step") or {}
    row = {"reference": o["reference"], "status": o["status"], "status_label": o["status_label"],
           "customer": o.get("customer_name") or "", "country": o.get("customer_country") or "",
           "label_language": o.get("label_language") or "", "target_date": o.get("target_date"),
           "next_step": ({"text": step.get("text"), "who": step.get("who")} if step else None),
           "updated_at": o.get("updated_at")}
    if show_prices:
        row["total"] = o.get("total")
        row["currency"] = o.get("currency") or ""
    return row


def b2b_orders(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Liste (`status`, vars. açık siparişler) ya da `reference` (teklif no ya da
    SIP-<id>) ile tek siparişin özeti.  Tutar/ödeme yalnız `b2b.view`
    (sistemdeki `_show_prices` kuralı); müşteri iletişimi, banka, imza özeti
    ve belge hash'i ÇIKMAZ."""
    from core import b2b_orders as BO
    show_prices = ctx.can("b2b", "view")
    ref = _text(args.get("reference"))
    if ref:
        order = _find_b2b_order(db, ctx.domain, ref)
        if order is None:
            raise ToolError(f"Bu panelde sipariş bulunamadı: {ref}")
        detail = BO.order_detail(db, order, user=_user(db, ctx), show_prices=show_prices)
        out = {"order": _b2b_summary_row(detail["order"], show_prices)}
        if show_prices:
            out["order"]["payment_terms"] = detail["order"].get("payment_terms_label") or ""
        # Motor fiyat iznine göre süzer: izinsizde yalnız production_ok / shipment_ok
        out["payment_gate"] = detail.get("payment")
        out["lines"] = [{"name": ln.get("name"), "qty": ln.get("quantity"), "unit": ln.get("unit") or "",
                         "use_stock": ln.get("use_stock"), "produce": ln.get("produce"),
                         "recipe": ln.get("recipe_name") or ""} for ln in detail.get("lines") or []]
        tech = detail.get("technical") or {}
        out["technical"] = {"warnings": tech.get("warnings") or [], "stale": bool(tech.get("stale"))} \
            if tech else None
        out["purchase_lines"] = [{"item": p["item_name"], "need": p["need_quantity"], "unit": p["unit"],
                                  "status": p["status"], "supplier": p["supplier_name"]}
                                 for p in detail.get("purchase_lines") or []][:ROWS_MAX]
        out["batches"] = [{"lot": b["lot_number"], "status": b["status"],
                           "planned": b["planned_quantity"], "produced": b["produced_quantity"],
                           "started_at": b["started_at"], "completed_at": b["completed_at"]}
                          for b in detail.get("batches") or []]
        ship = detail.get("shipment") or {}
        out["shipment_ready"] = bool(ship.get("ready")) if ship else False
        out["timeline"] = [{"at": t["at"], "action": t["action"]}
                           for t in (detail.get("timeline") or [])[-8:]]
        out["link"] = "/b2b-siparisler"
        return out
    flt = args.get("status") or "open"
    if flt not in B2B_FILTERS:
        raise ToolError("status open|all|SUBMITTED|TECH_REVIEWED|APPROVED|SHIPPED|CANCELLED olmalı")
    rows = BO.list_orders(db, ctx.domain, show_prices=show_prices,
                          status=None if flt == "all" else flt)
    counts = Counter(r["status_label"] for r in rows)
    return {"filter": flt, "status_counts": dict(counts),
            "rows": [_b2b_summary_row(r, show_prices) for r in rows[:ROWS_MAX]],
            "count": len(rows), "link": "/b2b-siparisler"}


# ─── quotations ──────────────────────────────────────────────────────────────

def quotations(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """B2B teklifleri (routers/b2b._serialize_quotation_summary'nin iletişimsiz
    alt kümesi; tutar teklif listesinde olduğu gibi — araç zaten b2b.view)."""
    status = (args.get("status") or "").upper() or None
    if status and status not in QUOTE_STATUSES:
        raise ToolError("status DRAFT|PENDING|CONFIRMED|REJECTED|ORDER olmalı")
    qn = _fold(args.get("q"))
    query = (db.query(Quotation).options(selectinload(Quotation.items))
             .filter(Quotation.domain == ctx.domain))
    if status:
        query = query.filter(Quotation.status == status)
    rows = []
    for qt in query.order_by(Quotation.id.desc()).limit(300).all():
        if qn and not _matches(qn, qt.quote_number, qt.customer_name, qt.customer_country):
            continue
        rows.append({
            "quote_number": qt.quote_number, "customer": qt.customer_name or "",
            "country": qt.customer_country or "", "status": qt.status,
            "status_label": QUOTE_LABELS.get(qt.status, qt.status),
            "total": qt.total_amount, "currency": qt.currency or "",
            "items": len(qt.items or []), "date": _dt_text(qt.created_at),
            "reject_reason": _note(qt.reject_reason) if qt.status == "REJECTED" else "",
            "distributor": (qt.distributor.company_name
                            if qt.distributor_id and qt.distributor else ""),
            "order": (f"SIP-{qt.b2b_order.id}" if qt.b2b_order is not None else ""),
        })
    by_status = Counter(r["status_label"] for r in rows)
    return {"status_counts": dict(by_status), "rows": rows[:ROWS_MAX], "count": len(rows),
            "link": "/quotations"}


# ─── qc_pending ──────────────────────────────────────────────────────────────

def qc_pending(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """QC sayfasının listesi (`/qc/quarantine`): QUARANTINE lotlar + üretim
    çıktısı qc_required lotlar, kaynak etiketi aynı."""
    from sqlalchemy import or_
    rows = (db.query(Inventory).options(selectinload(Inventory.item))
            .filter(Inventory.domain == ctx.domain,
                    or_(Inventory.status == "QUARANTINE", Inventory.qc_required == True))  # noqa: E712
            .order_by(Inventory.id.asc()).all())
    today = tr_now().date()
    out, by_source = [], Counter()
    for r in rows:
        src = "Fason" if r.outsourcing_receipt_id else ("Üretim" if r.qc_required else "Mal Kabul")
        by_source[src] += 1
        since = to_tr(r.created_at).date() if r.created_at else None
        out.append({
            "item": r.item.name if r.item else "—", "unit": (r.item.unit if r.item else "") or "",
            "lot": r.lot_number or "", "qty": _r(r.quantity), "source": src,
            "status": r.status or "", "location": r.location or "",
            "since": _date_text(since), "days_waiting": (today - since).days if since else None,
        })
    out.sort(key=lambda x: -(x["days_waiting"] or 0))
    return {"count": len(out), "by_source": dict(by_source), "rows": out[:ROWS_MAX], "link": "/qc"}


# ─── outsourcing_jobs ────────────────────────────────────────────────────────

def _job_row(j: dict) -> dict:
    return {"reference": j["reference"], "partner": j.get("partner_name") or "",
            "product": j.get("external_product_name") or "", "qty": j.get("quantity"),
            "unit": j.get("unit") or "", "status": j.get("status"),
            "status_label": JOB_LABELS.get(j.get("status"), j.get("status")),
            "created_at": j.get("created_at"), "frozen_at": j.get("frozen_at"),
            "closed_at": j.get("closed_at")}


def outsourcing_jobs(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Fason işleri.  Liste `core.outsourcing.list_jobs`; `reference` (FS-<id>)
    ile ayrıntı `job_detail` — gerçek malzeme adı motorun kendi kuralıyla
    yalnız `outsourcing.mapping` ile gelir; dış paket (kodlu belge) çıkmaz."""
    from core import outsourcing as OS
    ref = _text(args.get("reference")).upper()
    if ref:
        num = ref[3:] if ref.startswith("FS-") else ref
        if not num.isdigit():
            raise ToolError("reference FS-<numara> biçiminde olmalı")
        try:
            job = OS._job(db, int(num), ctx.domain)
        except OS.OutsourcingError:
            raise ToolError(f"Bu panelde fason işi bulunamadı: {ref}")
        d = OS.job_detail(db, job, user_id=ctx.user_id, permissions=ctx.perms.get("outsourcing") or {})
        return {
            "job": _job_row(d["job"]),
            # item_name motorda YALNIZ outsourcing.mapping ile bulunur (kod ↔ gerçek kart)
            "materials": [{k: m.get(k) for k in ("code", "item_name", "kind", "phase", "quantity",
                                                  "dispatch_quantity", "unit") if k in m}
                          for m in d.get("materials") or []][:20],
            "approvals": [{"role": a["role"], "approved_at": a["approved_at"]}
                          for a in d.get("approvals") or []],
            "balance": d.get("balance"),
            "receipts": d.get("receipts"),
            "shipments": d.get("shipments"),
            "link": "/outsourcing",
        }
    rows = [_job_row(j) for j in OS.list_jobs(db, ctx.domain)]
    status = args.get("status") or None
    if status:
        rows = [r for r in rows if r["status"] == status]
    counts = Counter(r["status_label"] for r in rows)
    return {"status_counts": dict(counts), "rows": rows[:ROWS_MAX], "count": len(rows),
            "link": "/outsourcing"}


# ─── stock_movements ─────────────────────────────────────────────────────────

SIGN = {"Input": 1.0, "Output": -1.0, "Adjustment": 1.0}


def stock_movements(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Kartın defter hareketleri: tür bazında toplam (Input + / Output − /
    Adjustment işaretli — core.snapshots.compute_stock_at ile aynı işaret) ve
    son 20 hareket.  Kişi adı (performed_by) çıkmaz."""
    item_id = _int_arg(args, "item_id", 0, 0, 2 ** 31)
    item = db.query(Item).filter(Item.id == item_id, Item.domain == ctx.domain).first()
    if item is None:
        raise ToolError("Bu panelde böyle bir kart yok")
    start, end = _range(args, 30)
    lo, hi = utc_bounds(start, end)
    rows = (db.query(Transaction)
            .filter(Transaction.item_id == item.id, Transaction.timestamp >= lo, Transaction.timestamp < hi)
            .order_by(Transaction.timestamp.desc(), Transaction.id.desc()).all())
    totals: Dict[str, float] = {}
    net = 0.0
    for t in rows:
        totals[t.transaction_type] = round(totals.get(t.transaction_type, 0.0) + float(t.quantity or 0), 4)
        net += SIGN.get(t.transaction_type, 0.0) * float(t.quantity or 0)
    return {
        "item": {"id": item.id, "name": item.name, "unit": item.unit or "",
                 "current_stock": _r(item.current_stock)},
        "start": start.isoformat(), "end": end.isoformat(),
        "totals_by_type": totals, "net_change": round(net, 4), "count": len(rows),
        "rows": [{"at": _dt_text(t.timestamp), "type": t.transaction_type, "qty": _r(t.quantity),
                  "lot": t.lot_number or "", "note": _note(t.notes)[:120]} for t in rows[:20]],
        "link": "/ledger",
    }


# ─── consumption ─────────────────────────────────────────────────────────────

def consumption(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Dönemde en çok tüketilen malzemeler — `routers.reports.usage_between`
    (top-usage ucuyla aynı kural)."""
    from routers.reports import usage_between
    start, end = _range(args, CONSUMPTION_DEFAULT_DAYS)
    limit = _int_arg(args, "limit", 15, 1, 30)
    lo, hi = utc_bounds(start, end)
    rows = usage_between(db, ctx.domain, lo, hi)
    return {"start": start.isoformat(), "end": end.isoformat(),
            "rows": [{"item_id": r["item_id"], "name": r["name"], "unit": r["unit"] or "",
                      "used": r["total_used"]} for r in rows[:limit]],
            "count": len(rows), "link": "/reports"}


# ─── month_end_stock ─────────────────────────────────────────────────────────

def month_end_stock(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Ay sonu stoğu — Raporlar'daki aylık stok raporu (dondurulmuş kayıt varsa
    o, yoksa canlı rekonstrüksiyon; `report_monthly_stock` doğrudan çağrılır)."""
    from fastapi.responses import JSONResponse
    from routers.reports import report_monthly_stock
    today = tr_now().date()
    year = _int_arg(args, "year", today.year, 2000, 2100)
    month = _int_arg(args, "month", today.month, 1, 12)
    category = args.get("category") or "all"
    if category not in MONTH_CATEGORIES:
        raise ToolError("category all|hammadde|ambalaj|etiket|finished olmalı")
    rep = report_monthly_stock(year=year, month=month, category=category, db=db, _={}, domain=ctx.domain)
    if isinstance(rep, JSONResponse):
        raise ToolError("Bu ay için stok raporu üretilemedi")
    qn = _fold(args.get("q"))
    sections = []
    for sec in rep["sections"]:
        rows = [r for r in sec["rows"] if _matches(qn, r["name"])]
        sections.append({"category": sec["label"], "row_count": len(rows),
                         "total": round(sum(r["stock"] for r in rows), 4),
                         "rows": [{"name": r["name"], "stock": r["stock"], "unit": r["unit"]}
                                  for r in rows[:40]]})
    return {"year": year, "month": month, "source": rep["source"], "as_of": rep["as_of"],
            "sections": sections, "link": "/reports"}


# ─── production_capacity ─────────────────────────────────────────────────────

def production_capacity(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """"N adet üretirsek ne eksik / kaç adet üretilebilir" — Raporlar'daki
    Üretim Stok Analizi motoru (`core.production_sim.simulate`, tüketim
    kuralları üretimle aynı).  Reçete dökümü (birim başına miktar) ÇIKMAZ:
    yalnız üretilebilir adet, sınırlayan malzeme ve eksikler."""
    from core.production_sim import list_plan_products, simulate
    q = _text(args.get("product"))
    if len(q) < 2:
        raise ToolError("Ürün adı en az 2 karakter olmalı")
    qty = _int_arg(args, "quantity", 0, 0, 1_000_000)
    if qty < 1:
        raise ToolError("Adet 1 ya da daha büyük olmalı")
    qn = _fold(q)
    products = list_plan_products(db, ctx.domain)
    exact = [p for p in products if qn in _fold(p["name"])]
    cands = exact or [p for p in products if _ratio(qn, _fold(p["name"])) >= 0.8]
    if not cands:
        raise ToolError(f"Bu panelde reçetesi olan ürün bulunamadı: {q}")
    if len(cands) > 1:
        return {"ambiguous": True, "candidates": [p["name"] for p in cands[:10]],
                "count": len(cands), "link": "/reports"}
    p = cands[0]
    sim = simulate(db, [p["recipe_id"]], float(qty), "TR", ctx.domain)
    prod = (sim.get("producible") or [{}])[0]
    short = [{"name": m["name"], "category": m["category"], "unit": m["unit"],
              "in_stock": m["current"], "shortfall": m["shortfall"]}
             for m in sim.get("materials") or [] if m["status"] == "YETERSİZ"]
    return {"product": p["name"], "quantity": qty,
            "producible_now": prod.get("producible"), "limiting_material": prod.get("limiting"),
            "short_count": len(short), "short_materials": short[:ROWS_MAX],
            "skipped_labels": len(sim.get("skipped_labels") or []), "link": "/reports"}


# ─── retention_samples ───────────────────────────────────────────────────────

def retention_samples(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Şahit numune dolabı — liste ucuyla AYNI süzgeç (`_filtered_query`)."""
    from routers.retention import _filtered_query, _ordered, _row_views
    status = args.get("status") or None
    if status and status not in RETENTION_STATUSES:
        raise ToolError("status stored|depleted|destroyed olmalı")
    query = _filtered_query(db, ctx.domain, brand=_text(args.get("brand")) or None,
                            q=_text(args.get("q")) or None, status=status,
                            expired=1 if args.get("expired") else None)
    total = query.count()
    rows = _row_views(db, _ordered(query).limit(ROWS_MAX).all())
    return {
        "count": total,
        "rows": [{"item": r["item_name"], "lot": r["lot_number"], "brand": r["brand"],
                  "location": r["location_label"], "qty": r["quantity"], "unit": r["unit"],
                  "retention_until": r["retention_until_label"], "expiry_state": r["expiry_state"],
                  "status": r["status"]} for r in rows],
        "link": "/sahit-numune",
    }


# ─── sample_analyses ─────────────────────────────────────────────────────────

def sample_analyses(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Numune analiz formları (alternatif tedarikçi denemeleri); formu
    dolduran kişi çıkmaz."""
    from core.sample_questions import mode_label, result_label, source_label
    qn = _fold(args.get("q"))
    rows = []
    for a in (db.query(SampleAnalysis).options(selectinload(SampleAnalysis.ingredients))
              .filter(SampleAnalysis.domain == ctx.domain, SampleAnalysis.is_active == True)  # noqa: E712
              .order_by(SampleAnalysis.id.desc()).limit(200).all()):
        ings = list(a.ingredients or [])
        if qn and not _matches(qn, a.document_no, a.bulk_name, a.lot_number, a.recipe_name,
                               *[g.item_name for g in ings], *[g.supplier_name for g in ings]):
            continue
        mode = a.mode or ("existing" if a.recipe_id else "new")
        rows.append({
            "document_no": a.document_no or "", "bulk_name": a.bulk_name or "",
            "date": _date_text(a.production_date), "lot": a.lot_number or "",
            "recipe": a.recipe_name or "", "mode": mode_label(mode),
            "result": result_label(a.result), "result_text": _note(a.result_text),
            "ingredients": len(ings),
            "sources": dict(Counter(source_label(g.source) for g in ings)),
            "suppliers": sorted({g.supplier_name for g in ings if g.supplier_name})[:5],
        })
    return {"rows": rows[:ROWS_MAX], "count": len(rows), "link": "/numune-analiz"}


# ─── trendyol_orders ─────────────────────────────────────────────────────────

def trendyol_orders(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Trendyol siparişleri (`core.trendyol.orders_view`, ciro dahil —
    `trendyol.view` kuralı); komisyon/kâr yalnız `can_profit`.  Modül müşteri
    kişisel verisi saklamaz."""
    from core import trendyol as TY
    start, end = _parse_iso(args.get("start")), _parse_iso(args.get("end"))
    user = _user(db, ctx)
    show_cost = bool(user) and TY.can_profit(user)
    view = TY.orders_view(db, start=start.isoformat() if start else None,
                          end=end.isoformat() if end else None,
                          status=_text(args.get("status")) or None, panel="all",
                          q=_text(args.get("q")) or None, page=0, size=ROWS_MAX, show_cost=show_cost)
    labels = view.get("status_labels") or {}
    orders = []
    for o in view["orders"]:
        row = {"order_number": o["order_number"], "status_label": o["status_label"],
               "date": o["order_date"], "total": o["total_price"], "currency": o["currency"],
               "cargo": o.get("cargo_provider") or "", "tracking": o.get("cargo_tracking") or "",
               "panel": o.get("panel_label"), "warnings": o.get("warnings") or [],
               "lines": [{"product": ln["product_name"], "qty": ln["quantity"], "barcode": ln["barcode"]}
                         for ln in o["lines"][:LINES_MAX]]}
        if show_cost:
            row["commission_rates"] = [ln.get("commission_rate") for ln in o["lines"][:LINES_MAX]]
        orders.append(row)
    return {"total": view["total"],
            "status_counts": {labels.get(k, k): v for k, v in (view.get("status_counts") or {}).items()},
            "rows": orders, "link": "/trendyol"}


# ─── shopify_orders ──────────────────────────────────────────────────────────

def shopify_orders(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Shopify siparişleri — müşteri adı/e-posta/VKN ve `lines_json`'daki adres/
    telefon ÇIKMAZ; yalnız sipariş, durum, tutar, ülke ve ürün satırları."""
    from core.shopify import brand_for_store_key
    status = _text(args.get("status")) or None
    limit = _int_arg(args, "limit", 10, 1, ROWS_MAX)
    query = db.query(ShopifyOrder)
    if status:
        query = query.filter(ShopifyOrder.status == status)
    rows = query.order_by(ShopifyOrder.id.desc()).limit(limit).all()
    out = []
    for r in rows:
        try:
            saved = json.loads(r.lines_json or "{}")
        except (TypeError, ValueError):
            saved = {}
        lines = saved.get("lines") if isinstance(saved, dict) else None
        out.append({
            "order_number": r.order_number or "", "store": brand_for_store_key(r.store_key) or r.store_key,
            "status": r.status, "step": r.step or "", "total": r.total, "currency": r.currency or "",
            "country": r.country or "", "date": _dt_text(r.created_at),
            "error": _note(r.last_error) if r.last_error else "",
            "lines": [{"title": (ln or {}).get("title") or "", "qty": (ln or {}).get("quantity"),
                       "sku": (ln or {}).get("sku") or ""} for ln in (lines or [])[:LINES_MAX]],
        })
    counts = dict(Counter(s for (s,) in db.query(ShopifyOrder.status).all()))
    return {"status_counts": counts, "rows": out, "link": "/shopify-sync"}


# ─── influencer_overview ─────────────────────────────────────────────────────

def influencer_overview(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Creator programı — kampanya düzeyi toplamlar; creator kimliği / iletişim
    hiç çıkmaz (iş birliği aşama sayıları, gönderi ve içerik sayıları)."""
    from core.influencer import STAGE_LABELS
    campaigns = (db.query(InfluencerCampaign).filter(InfluencerCampaign.is_active == True)  # noqa: E712
                 .order_by(InfluencerCampaign.id.desc()).all())
    collabs = db.query(InfluencerCollab.id, InfluencerCollab.campaign_id, InfluencerCollab.stage).all()
    by_campaign: Dict[Optional[int], Counter] = {}
    collab_campaign = {}
    for cid, camp, stage in collabs:
        by_campaign.setdefault(camp, Counter())[STAGE_LABELS.get(stage, stage)] += 1
        collab_campaign[cid] = camp
    ships, contents = Counter(), Counter()
    for (cid,) in db.query(InfluencerShipment.collab_id).all():
        ships[collab_campaign.get(cid)] += 1
    content_status = Counter()
    for cid, st in db.query(InfluencerContent.collab_id, InfluencerContent.status).all():
        contents[collab_campaign.get(cid)] += 1
        content_status[st] += 1
    rows = [{
        "campaign": c.name, "store": c.store_key, "market": c.market or "", "status": c.status,
        "start": _date_text(c.start_on), "end": _date_text(c.end_on),
        "collabs_by_stage": dict(by_campaign.get(c.id, Counter())),
        "shipments": ships.get(c.id, 0), "contents": contents.get(c.id, 0),
    } for c in campaigns[:ROWS_MAX]]
    return {"campaigns": rows, "campaign_count": len(campaigns), "collab_count": len(collabs),
            "without_campaign": dict(by_campaign.get(None, Counter())),
            "content_by_status": dict(content_status), "link": "/influencer"}


# ─── reviews_overview ────────────────────────────────────────────────────────

def reviews_overview(ctx: "ToolContext", db: Session, args: dict) -> dict:
    """Ürün yorumları — ürün bazında yayındaki puan ortalaması/sayısı, bekleyen
    moderasyon; yorumcu adı/e-postası ÇIKMAZ.  Metin yalnız YAYINDAKİ (mağazada
    herkese açık) yorumlardan kısa alıntı."""
    qn = _fold(args.get("q"))
    reviews = (db.query(ProductReview).order_by(ProductReview.id.desc()).all())
    groups: Dict[tuple, dict] = {}
    pending = 0
    for r in reviews:
        if qn and not _matches(qn, r.product_title):
            continue
        if r.status == "pending":
            pending += 1
        g = groups.setdefault((r.store_key, r.product_title or "—"),
                              {"product": r.product_title or "—", "store": r.store_key,
                               "published": 0, "pending": 0, "rating_sum": 0, "latest": []})
        if r.is_published:
            g["published"] += 1
            g["rating_sum"] += int(r.rating or 0)
            if len(g["latest"]) < 2 and (r.body or "").strip():
                g["latest"].append({"rating": r.rating, "text": _note(r.body)[:160],
                                    "date": _dt_text(r.created_at)})
        elif r.status == "pending":
            g["pending"] += 1
    rows = []
    for g in groups.values():
        avg = round(g["rating_sum"] / g["published"], 2) if g["published"] else None
        rows.append({"product": g["product"], "store": g["store"], "published": g["published"],
                     "avg_rating": avg, "pending": g["pending"], "latest": g["latest"]})
    rows.sort(key=lambda r: (-r["published"], r["product"]))
    return {"products": rows[:ROWS_MAX], "product_count": len(rows),
            "pending_moderation": pending, "link": "/yorumlar"}
