# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
CRM yardımcıları — serializer'lar + küçük util'ler.

Router (routers/crm.py) bu fonksiyonları kullanarak SQLAlchemy modellerini
JSON'a çevirir.  Tüm datetime'lar DB'de naive UTC; burada gösterim için
to_tr() ile Türkiye saatine çevrilir.  İş mantığı (FIFO vb.) yok — saf dönüşüm.
"""
import re
from datetime import datetime
from typing import Optional

from database import to_tr, TR_OFFSET


def parse_tr_to_utc(s: Optional[str]) -> Optional[datetime]:
    """Frontend'den gelen TR-local 'YYYY-MM-DDTHH:MM' (veya '...:SS') string'ini
    naive UTC datetime'a çevir (gösterim TR saatinde, DB UTC).  Geçersiz/boş → None."""
    if not s or not s.strip():
        return None
    s = s.strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            local = datetime.strptime(s, fmt)
            return local - TR_OFFSET   # TR → UTC
        except ValueError:
            continue
    return None


# ─── Tarih/saat gösterimi ────────────────────────────────────────────────────

def fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    """UTC datetime → 'dd.mm.YYYY HH:MM' (Türkiye saati).  None güvenli."""
    return to_tr(dt).strftime("%d.%m.%Y %H:%M") if dt else None


def fmt_date(dt: Optional[datetime]) -> Optional[str]:
    """UTC datetime → 'dd.mm.YYYY' (Türkiye saati).  None güvenli."""
    return to_tr(dt).strftime("%d.%m.%Y") if dt else None


def fmt_iso(dt: Optional[datetime]) -> Optional[str]:
    """UTC datetime → ISO string (frontend <input type=datetime-local> için, TR saati)."""
    return to_tr(dt).strftime("%Y-%m-%dT%H:%M") if dt else None


# ─── WhatsApp tıkla-konuş köprüsü (Phase 3 öncesi anında kazanım) ────────────

def normalize_phone(raw: Optional[str]) -> str:
    """Telefon numarasını wa.me için sadeleştir — yalnızca rakamlar.
    Başında '+' veya '00' varsa ülke kodu korunur (sadece rakama indirilir).
    Boş/None → ''."""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    # '00' uluslararası önek → at (wa.me ülke kodunu önek olmadan ister)
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def wa_link(number: Optional[str], text: str = "") -> Optional[str]:
    """wa.me tıkla-konuş linki üret — kurulum/onay gerektirmez, mobilde mükemmel.
    Numara yoksa None.  Resmi Cloud API (Phase 3) bunun yerini almaz, tamamlar."""
    digits = normalize_phone(number)
    if len(digits) < 7:
        return None
    from urllib.parse import quote
    base = f"https://wa.me/{digits}"
    return f"{base}?text={quote(text)}" if text else base


# ─── Serializer'lar ──────────────────────────────────────────────────────────

def serialize_stage(s) -> dict:
    return {
        "id": s.id, "name": s.name, "sort_order": s.sort_order,
        "is_won": bool(s.is_won), "is_lost": bool(s.is_lost),
    }


def serialize_company(c, *, contact_count: int = 0, open_deal_count: int = 0) -> dict:
    return {
        "id": c.id, "name": c.name, "sector": c.sector or "",
        "website": c.website or "", "phone": c.phone or "", "email": c.email or "",
        "address": c.address or "", "city": c.city or "", "country": c.country or "",
        "tax_office": c.tax_office or "", "tax_no": c.tax_no or "",
        "notes": c.notes or "",
        "owner_user_id": c.owner_user_id, "owner_name": c.owner_name or "",
        "wa_link": wa_link(c.phone),
        "contact_count": contact_count, "open_deal_count": open_deal_count,
        "created_at": fmt_dt(c.created_at), "created_by": c.created_by or "",
    }


def serialize_contact(c, *, company_name: str = "") -> dict:
    return {
        "id": c.id, "company_id": c.company_id, "company_name": company_name,
        "full_name": c.full_name, "title": c.title or "",
        "phone": c.phone or "", "mobile": c.mobile or "", "email": c.email or "",
        "whatsapp_number": c.whatsapp_number or "", "source": c.source or "",
        "notes": c.notes or "",
        "owner_user_id": c.owner_user_id, "owner_name": c.owner_name or "",
        "wa_link": wa_link(c.whatsapp_number or c.mobile or c.phone),
        "created_at": fmt_dt(c.created_at), "created_by": c.created_by or "",
    }


def serialize_deal(d, *, company_name: str = "", contact_name: str = "", stage_name: str = "") -> dict:
    return {
        "id": d.id, "title": d.title,
        "company_id": d.company_id, "company_name": company_name,
        "contact_id": d.contact_id, "contact_name": contact_name,
        "stage_id": d.stage_id, "stage_name": stage_name,
        "value": d.value or 0.0, "currency": d.currency or "TRY",
        "probability": d.probability or 0,
        "expected_close_at": fmt_iso(d.expected_close_at),
        "expected_close_label": fmt_date(d.expected_close_at),
        "status": d.status, "lost_reason": d.lost_reason or "",
        "owner_user_id": d.owner_user_id, "owner_name": d.owner_name or "",
        "quotation_id": d.quotation_id, "sort_order": d.sort_order or 0,
        "created_at": fmt_dt(d.created_at), "created_by": d.created_by or "",
        "won_at": fmt_dt(d.won_at), "closed_at": fmt_dt(d.closed_at),
    }


def serialize_activity(a) -> dict:
    return {
        "id": a.id, "type": a.type, "subject": a.subject or "", "body": a.body or "",
        "company_id": a.company_id, "contact_id": a.contact_id, "deal_id": a.deal_id,
        "author_user_id": a.author_user_id, "author_name": a.author_name or "—",
        "is_pinned": bool(a.is_pinned),
        "created_at": fmt_dt(a.created_at),
    }


def serialize_task(t, *, overdue: bool = False) -> dict:
    return {
        "id": t.id, "title": t.title, "notes": t.notes or "",
        "due_at": fmt_iso(t.due_at), "due_label": fmt_dt(t.due_at),
        "status": t.status,
        "company_id": t.company_id, "contact_id": t.contact_id, "deal_id": t.deal_id,
        "assigned_to_user_id": t.assigned_to_user_id,
        "assigned_to_name": t.assigned_to_name or "—",
        "created_by": t.created_by or "", "created_at": fmt_dt(t.created_at),
        "completed_at": fmt_dt(t.completed_at),
        "overdue": overdue,
    }
