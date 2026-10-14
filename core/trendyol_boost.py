# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Trendyol satış artırma — DB'ye dayalı görünümler (Liste Doktoru, sorular).

Spec: docs/superpowers/specs/2026-10-13-trendyol-satis-artirma-design.md.
Puan hesabı SAF motordadır (core/trendyol_listing.score_listing); burası
katalog satırını, kategori özelliklerini ve IMS eşleşmesini (stok) toplayıp
motora verir, günlük puan geçmişini yazar ve router'a sade sözlük döner.
Yapay zekâ çağrısı burada YOK (core/trendyol_ai).
"""
import hashlib
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from core import trendyol as T
from core import trendyol_listing as L
from database import (TR_OFFSET, TrendyolCategoryAttr, TrendyolListingSuggestion, TrendyolProduct,
                      TrendyolProductNote, TrendyolQuestion, TrendyolScoreHistory, to_tr)

NOTE_MAX = 2000
LOW_SCORE = 70
HISTORY_DAYS = 60

QUESTION_STATUS_LABELS = {
    "WAITING_FOR_ANSWER": "Cevap bekliyor", "ANSWERED": "Cevaplandı", "REPORTED": "Bildirildi",
    "REJECTED": "Reddedildi", "UNANSWERED": "Cevaplanmadı",
}


def _fmt(dt: Optional[datetime]) -> Optional[str]:
    return to_tr(dt).strftime("%d.%m.%Y %H:%M") if dt else None


def note_hash(text: Optional[str]) -> str:
    """Bilgi notunun kısa özeti — öneri bu notla mı üretildi?"""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:16]


def attrs_def_by_category(db: Session) -> Dict[int, List[dict]]:
    out: Dict[int, List[dict]] = {}
    for a in db.query(TrendyolCategoryAttr).order_by(TrendyolCategoryAttr.attribute_id).all():
        out.setdefault(a.category_id, []).append({
            "attribute_id": a.attribute_id, "name": a.name, "required": bool(a.required),
            "slicer": bool(a.slicer), "varianter": bool(a.varianter),
            "allow_custom": bool(a.allow_custom), "allow_multiple": bool(a.allow_multiple)})
    return out


def product_dict(p: TrendyolProduct) -> dict:
    try:
        attrs = json.loads(p.attributes_json) if p.attributes_json else []
    except ValueError:
        attrs = []
    return {
        "barcode": p.barcode, "title": p.title, "brand": p.brand, "category": p.category,
        "category_id": p.category_id, "description": p.description, "attributes": attrs,
        "image_count": p.image_count, "sale_price": p.sale_price, "list_price": p.list_price,
        "quantity": p.quantity, "on_sale": p.on_sale, "archived": p.archived, "locked": p.locked,
        "blacklisted": p.blacklisted, "delivery_duration": p.delivery_duration,
        "buybox_order": p.buybox_order, "buybox_price": p.buybox_price,
        "has_multiple_seller": p.has_multiple_seller,
    }


def current_products(db: Session) -> List[TrendyolProduct]:
    """Son katalog senkronunda görülen ürünler (eskimiş/silinmiş liste hariç)."""
    latest = db.query(func.max(TrendyolProduct.last_seen_at)).scalar()
    q = db.query(TrendyolProduct)
    if latest is not None:
        q = q.filter(TrendyolProduct.last_seen_at == latest)
    return q.order_by(TrendyolProduct.title, TrendyolProduct.barcode).all()


def score_all(db: Session) -> List[dict]:
    """Güncel ürünlerin puanı — [{product, result, resolution, ims_stock}]."""
    res = T.Resolver(db)
    defs = attrs_def_by_category(db)
    out = []
    for p in current_products(db):
        r = res.resolve(p.barcode)
        stock = T.ims_stock(r)
        result = L.score_listing(product_dict(p), defs.get(p.category_id) or [],
                                 {"stock": stock, "brand": p.brand})
        out.append({"product": p, "result": result, "resolution": r, "ims_stock": stock})
    return out


def snapshot_scores(db: Session, *, now: Optional[datetime] = None) -> dict:
    """Günlük puan geçmişi (TR takvim günü) — aynı gün tekrar çalışırsa günceller."""
    now = now or datetime.utcnow()
    day = (now + TR_OFFSET).date()
    existing = {h.barcode: h for h in db.query(TrendyolScoreHistory).filter(TrendyolScoreHistory.day == day).all()}
    n = 0
    for row in score_all(db):
        p, result = row["product"], row["result"]
        compact = {"sections": {b["key"]: b["points"] for b in result["breakdown"]},
                   "fixes": [f["text"] for f in result["fixes"]]}
        h = existing.get(p.barcode)
        if h is None:
            h = TrendyolScoreHistory(barcode=p.barcode, day=day)
            db.add(h)
        h.score = result["score"]
        h.breakdown_json = json.dumps(compact, ensure_ascii=False)
        n += 1
    db.commit()
    return {"day": day.isoformat(), "products": n}


buybox_state = T.buybox_state


def _latest_suggestions(db: Session) -> Dict[str, TrendyolListingSuggestion]:
    sub = (db.query(TrendyolListingSuggestion.barcode, func.max(TrendyolListingSuggestion.id).label("mid"))
           .group_by(TrendyolListingSuggestion.barcode).subquery())
    rows = (db.query(TrendyolListingSuggestion)
            .join(sub, TrendyolListingSuggestion.id == sub.c.mid).all())
    return {r.barcode: r for r in rows}


def listing_view(db: Session) -> dict:
    """Liste Doktoru tablosu (en düşük puan üstte) + özet kartları."""
    scored = score_all(db)
    notes = {n.barcode: (n.note or "") for n in db.query(TrendyolProductNote).all()}
    sugg = _latest_suggestions(db)
    rows = []
    for s in scored:
        p, result = s["product"], s["result"]
        last = sugg.get(p.barcode)
        rows.append({
            "barcode": p.barcode, "title": p.title, "brand": p.brand, "category": p.category,
            "score": result["score"], "fixes": result["fixes"], "buybox": buybox_state(p),
            "has_note": bool(notes.get(p.barcode, "").strip()), "image_count": p.image_count,
            "delivery_duration": p.delivery_duration, "on_sale": p.on_sale,
            "suggestion_at": _fmt(last.created_at) if last else None,
            "suggestion_flagged": bool(last.flagged) if last else False,
            "suggestion_stale": bool(last) and not _suggestion_current(last, p, notes.get(p.barcode, "")),
            "match": T.public_resolution(s["resolution"], show_cost=False)["label"],
        })
    rows.sort(key=lambda r: (r["score"], r["title"] or ""))
    scores = [r["score"] for r in rows]
    summary = {
        "products": len(rows),
        "avg_score": round(sum(scores) / len(scores)) if scores else None,
        "low": sum(1 for x in scores if x < LOW_SCORE),
        "buybox_lost": sum(1 for r in rows if r["buybox"]["state"] == "lost"),
        "notes_missing": sum(1 for r in rows if not r["has_note"]),
        "low_threshold": LOW_SCORE,
    }
    return {"rows": rows, "summary": summary}


def _suggestion_current(s: TrendyolListingSuggestion, p: TrendyolProduct,
                        note_text: Optional[str] = None) -> bool:
    """Öneri, ürünün ŞU ANKİ içeriği (ve verildiyse şu anki bilgi notu) için
    mi üretildi?  Panelde düzenleme ya da not değişikliği → "eski"."""
    try:
        meta = json.loads(s.payload_json).get("_meta") or {}
    except (ValueError, AttributeError):
        return False
    if meta.get("content_hash") != p.content_hash:
        return False
    return note_text is None or meta.get("note_hash") == note_hash(note_text)


def suggestion_view(s: Optional[TrendyolListingSuggestion], p: Optional[TrendyolProduct] = None,
                    note_text: Optional[str] = None) -> Optional[dict]:
    if s is None:
        return None
    try:
        payload = json.loads(s.payload_json)
    except ValueError:
        payload = {}
    payload.pop("_meta", None)
    try:
        flags = json.loads(s.flags_json) if s.flags_json else []
    except ValueError:
        flags = []
    return {"id": s.id, "payload": payload, "flags": flags, "flagged": bool(s.flagged), "model": s.model,
            "created_at": _fmt(s.created_at), "created_by": s.created_by, "cost_usd": round(s.cost_usd or 0, 4),
            "current": _suggestion_current(s, p, note_text) if p is not None else None}


def listing_detail(db: Session, barcode: str) -> Optional[dict]:
    p = db.query(TrendyolProduct).filter(TrendyolProduct.barcode == barcode).first()
    if p is None:
        return None
    defs = attrs_def_by_category(db).get(p.category_id) or []
    r = T.Resolver(db).resolve(p.barcode)
    stock = T.ims_stock(r)
    pd = product_dict(p)
    result = L.score_listing(pd, defs, {"stock": stock, "brand": p.brand})
    since = (datetime.utcnow() + TR_OFFSET).date() - timedelta(days=HISTORY_DAYS)
    history = [{"day": h.day.strftime("%d.%m.%Y"), "score": h.score}
               for h in db.query(TrendyolScoreHistory)
               .filter(TrendyolScoreHistory.barcode == barcode, TrendyolScoreHistory.day >= since)
               .order_by(TrendyolScoreHistory.day).all()]
    note = db.query(TrendyolProductNote).filter(TrendyolProductNote.barcode == barcode).first()
    last = (db.query(TrendyolListingSuggestion).filter(TrendyolListingSuggestion.barcode == barcode)
            .order_by(TrendyolListingSuggestion.id.desc()).first())
    return {
        "barcode": p.barcode, "title": p.title, "brand": p.brand, "category": p.category,
        "description_text": L.strip_html(p.description), "attributes": pd["attributes"],
        "image_count": p.image_count, "delivery_duration": p.delivery_duration,
        "sale_price": p.sale_price, "list_price": p.list_price, "quantity": p.quantity,
        "ims_stock": stock, "product_url": p.product_url, "buybox": buybox_state(p),
        "score": result["score"], "breakdown": result["breakdown"], "fixes": result["fixes"],
        "missing_attributes": [{"attribute_id": a["attribute_id"], "name": a["name"],
                                "required": a["required"], "slicer": a["slicer"] or a["varianter"]}
                               for a in L.missing_attributes(pd, defs)],
        "history": history,
        "note": {"text": note.note if note else "", "updated_by": note.updated_by if note else None,
                 "updated_at": _fmt(note.updated_at) if note else None},
        "suggestion": suggestion_view(last, p, note.note if note else ""),
        "match": T.public_resolution(r, show_cost=False),
    }


def save_note(db: Session, barcode: str, text: str, actor: str) -> TrendyolProductNote:
    text = (text or "").strip()[:NOTE_MAX]
    row = db.query(TrendyolProductNote).filter(TrendyolProductNote.barcode == barcode).first()
    if row is None:
        row = TrendyolProductNote(barcode=barcode, note=text)
        db.add(row)
    row.note = text
    row.updated_by = (actor or "—")[:100]
    row.updated_at = datetime.utcnow()
    db.commit()
    return row


def question_view(q: TrendyolQuestion, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    try:
        d = json.loads(q.draft_flags_json) if q.draft_flags_json else {}
    except ValueError:
        d = {}
    if isinstance(d, list):
        d = {"flags": d}
    waiting = q.status == T.WAITING
    return {
        "question_id": q.question_id, "barcode": q.barcode, "product_name": q.product_name,
        "text": q.text, "status": q.status, "status_label": QUESTION_STATUS_LABELS.get(q.status or "", q.status),
        "asked_at": _fmt(q.asked_at),
        "waiting_hours": round((now - q.asked_at).total_seconds() / 3600, 1) if (waiting and q.asked_at) else None,
        "answer_text": q.answer_text, "answered_at": _fmt(q.answered_at),
        "rejected_reason": q.rejected_reason, "web_url": q.web_url,
        "draft_text": q.draft_text, "draft_at": _fmt(q.draft_at), "draft_by": q.draft_by,
        "draft_flags": d.get("flags") or [], "draft_warnings": d.get("warnings") or [],
        "draft_flagged": bool(d.get("flagged")),
    }


def questions_view(db: Session, *, status: Optional[str] = None, limit: int = 200) -> dict:
    q = db.query(TrendyolQuestion)
    if status:
        q = q.filter(TrendyolQuestion.status == status)
    rows = q.all()
    # Cevap bekleyenler üstte (en eski bekleyen en üstte); diğerleri yeniden eskiye.
    waiting = sorted((r for r in rows if r.status == T.WAITING), key=lambda r: r.asked_at or datetime.max)
    others = sorted((r for r in rows if r.status != T.WAITING), key=lambda r: r.asked_at or datetime.min,
                    reverse=True)
    rows = waiting + others
    counts: Dict[str, int] = {}
    for r in db.query(TrendyolQuestion.status, func.count(TrendyolQuestion.id)).group_by(TrendyolQuestion.status).all():
        counts[r[0] or "?"] = r[1]
    now = datetime.utcnow()
    return {"questions": [question_view(r, now) for r in rows[:limit]], "counts": counts,
            "status_labels": QUESTION_STATUS_LABELS}
