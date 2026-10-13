# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Bütçe — fiyat tablosu, çağrı maliyeti, ay/gün toplamları, tavan denetimi,
%80 uyarısı ve `assistant_usage` kaydı.

Maliyet çağrı anında hesaplanıp `AssistantUsage.cost_usd`'a DONDURULUR
(fiyat tablosu sonra değişse de geçmiş rapor değişmez).  Önbellek yazması
`usage.cache_creation.ephemeral_{5m,1h}_input_tokens` ayrımıyla
fiyatlanır; ayrım yoksa (eski SDK / sahte) tüm yazma MUHAFAZAKÂR olarak
1 saatlik tarifeden sayılır.

Ay ve gün sınırları TR yerel takvimle (database.TR_OFFSET) hesaplanır —
`created_at` naive UTC saklanır (production_history_report.utc_bounds kalıbı).
"""
import datetime as _dt
import json
from dataclasses import dataclass
from typing import Iterable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from database import AdminAuditLog, AppSetting, AssistantUsage, TR_OFFSET, User, tr_now

#: USD / MTok — in: önbelleksiz girdi, out: çıktı, cache_read: önbellek okuma,
#: w5m / w1h: 5 dk / 1 saat önbellek yazma.
PRICES = {
    "claude-opus-5-5":   {"in": 4.0,  "out": 20.0, "cache_read": 0.20, "w5m": 5.0,   "w1h": 8.0},
    "claude-sonnet-5-5": {"in": 2.0,  "out": 10.0, "cache_read": 0.20, "w5m": 2.5,   "w1h": 4.0},
    "claude-haiku-5-5":  {"in": 0.10, "out": 0.50, "cache_read": 0.01, "w5m": 0.125, "w1h": 0.20},
}
DEFAULT_MODEL = "claude-opus-5-5"
WARN_PCT = 0.8
WARN_SENTINEL_PREFIX = "assistant.warned."


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def usage_fields(usage) -> dict:
    """SDK `usage` nesnesi ya da dict → düz alanlar (1h/5m ayrımı varsa ayrı)."""
    cc = _get(usage, "cache_creation")
    one = _get(cc, "ephemeral_1h_input_tokens") if cc is not None else None
    five = _get(cc, "ephemeral_5m_input_tokens") if cc is not None else None
    if cc is None:
        # Düz dict'te (assistant_usage satırı gibi) ayrı alan olabilir
        one = _get(usage, "cache_creation_1h_tokens")
        five = _get(usage, "cache_creation_5m_tokens")
    return {
        "input_tokens": _int(_get(usage, "input_tokens")),
        "cache_creation_input_tokens": _int(_get(usage, "cache_creation_input_tokens")),
        "cache_creation_1h_tokens": _int(one) if one is not None else None,
        "cache_creation_5m_tokens": _int(five) if five is not None else None,
        "cache_read_input_tokens": _int(_get(usage, "cache_read_input_tokens")),
        "output_tokens": _int(_get(usage, "output_tokens")),
    }


def compute_cost(model: str, usage) -> float:
    """Çağrı maliyeti (USD).  Model fiyat tablosunda yoksa varsayılan modelin
    tarifesi (muhafazakâr: Opus)."""
    p = PRICES.get(model) or PRICES[DEFAULT_MODEL]
    f = usage_fields(usage)
    if f["cache_creation_1h_tokens"] is not None or f["cache_creation_5m_tokens"] is not None:
        write = (f["cache_creation_5m_tokens"] or 0) * p["w5m"] + (f["cache_creation_1h_tokens"] or 0) * p["w1h"]
    else:
        write = f["cache_creation_input_tokens"] * p["w1h"]
    total = (f["input_tokens"] * p["in"] + f["cache_read_input_tokens"] * p["cache_read"]
             + write + f["output_tokens"] * p["out"]) / 1_000_000.0
    return round(total, 6)


# ─── TR takvim sınırları ─────────────────────────────────────────────────────

def month_bounds_utc(year: int, month: int):
    lo = _dt.datetime(year, month, 1) - TR_OFFSET
    hi = (_dt.datetime(year + 1, 1, 1) if month == 12 else _dt.datetime(year, month + 1, 1)) - TR_OFFSET
    return lo, hi


def day_bounds_utc(day: _dt.date):
    lo = _dt.datetime.combine(day, _dt.time.min) - TR_OFFSET
    return lo, lo + _dt.timedelta(days=1)


def month_spend(db: Session, year: int, month: int) -> float:
    lo, hi = month_bounds_utc(year, month)
    v = (db.query(func.coalesce(func.sum(AssistantUsage.cost_usd), 0.0))
         .filter(AssistantUsage.created_at >= lo, AssistantUsage.created_at < hi).scalar())
    return float(v or 0.0)


def user_day_spend(db: Session, user_id: int, day: _dt.date) -> float:
    lo, hi = day_bounds_utc(day)
    v = (db.query(func.coalesce(func.sum(AssistantUsage.cost_usd), 0.0))
         .filter(AssistantUsage.user_id == user_id,
                 AssistantUsage.created_at >= lo, AssistantUsage.created_at < hi).scalar())
    return float(v or 0.0)


# ─── Tavan denetimi ──────────────────────────────────────────────────────────

@dataclass
class Denied:
    status: int
    code: str
    detail: str


def check_budget(db: Session, settings, user_id: int, now: Optional[_dt.datetime] = None) -> Optional[Denied]:
    """Sıra: kapalı 409 → anahtar yok 503 → aylık tavan 402 → günlük tavan 402.
    `now` TR yerel saat (vars. tr_now())."""
    from core.assistant.client import is_configured
    if not settings.enabled:
        return Denied(409, "disabled", "Minerva Asistan şu anda kapalı.")
    if not is_configured():
        return Denied(503, "not_configured", "Asistan yapılandırılmamış (API anahtarı yok).")
    now = now or tr_now()
    if settings.monthly_cap_usd > 0 and month_spend(db, now.year, now.month) >= settings.monthly_cap_usd:
        return Denied(402, "monthly_cap", "Aylık asistan bütçesi doldu; ay başında yeniden açılır.")
    if settings.daily_user_cap_usd > 0 and user_day_spend(db, user_id, now.date()) >= settings.daily_user_cap_usd:
        return Denied(402, "daily_cap", "Günlük asistan limitiniz doldu; yarın tekrar deneyin.")
    return None


# ─── Kayıt ───────────────────────────────────────────────────────────────────

def record_usage(db: Session, *, user_id: int, conversation_id: Optional[int], message_id: Optional[int],
                 model: str, usage, cost_usd: float, tool_calls, latency_ms: Optional[int],
                 stop_reason: Optional[str], request_id: Optional[str], domain: str, round_no: int,
                 question_preview: Optional[str]) -> AssistantUsage:
    """Bir API çağrısı = bir satır (`db.add` + `flush`; commit çağıranın)."""
    f = usage_fields(usage)
    row = AssistantUsage(
        user_id=user_id, conversation_id=conversation_id, message_id=message_id,
        domain=domain, model=(model or DEFAULT_MODEL)[:60], round_no=int(round_no or 1),
        input_tokens=f["input_tokens"],
        cache_creation_input_tokens=f["cache_creation_input_tokens"],
        cache_creation_1h_tokens=f["cache_creation_1h_tokens"],
        cache_read_input_tokens=f["cache_read_input_tokens"],
        output_tokens=f["output_tokens"],
        cost_usd=float(cost_usd or 0.0),
        tool_calls_json=json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
        latency_ms=int(latency_ms) if latency_ms is not None else None,
        stop_reason=(stop_reason or None) and str(stop_reason)[:30],
        request_id=(request_id or None) and str(request_id)[:80],
        question_preview=(question_preview or None) and str(question_preview)[:160],
        created_at=_dt.datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    return row


# ─── %80 uyarısı ─────────────────────────────────────────────────────────────

def maybe_warn(db: Session, settings, month_total: float, now: Optional[_dt.datetime] = None) -> bool:
    """Aylık harcama tavanın %80'ine ulaştıysa AYDA BİR: aktif SuperAdmin'lere
    push + `assistant.budget_warning` audit satırı; sentinel AppSetting
    `assistant.warned.<YYYY-MM>`.  Push başarısızlığı akışı bozmaz."""
    cap = float(settings.monthly_cap_usd or 0.0)
    if cap <= 0 or month_total < WARN_PCT * cap:
        return False
    now = now or tr_now()
    key = f"{WARN_SENTINEL_PREFIX}{now.year:04d}-{now.month:02d}"
    if db.query(AppSetting).filter(AppSetting.key == key).first() is not None:
        return False
    db.add(AppSetting(key=key, value=f"{month_total:.2f}"))
    body = f"Aylık bütçenin %80'i kullanıldı (${month_total:.2f} / ${cap:.0f})"
    db.add(AdminAuditLog(actor_name="sistem", action="assistant.budget_warning",
                         target_type="assistant", target_name=key,
                         details=json.dumps({"month_total": round(month_total, 4), "cap": cap},
                                            ensure_ascii=False)))
    db.flush()
    ids = [u.id for u in db.query(User.id).filter(User.role == "SuperAdmin", User.is_active == True).all()]  # noqa: E712
    try:
        from core.notifications import _send_push_to_users
        _send_push_to_users(ids, {"title": "Minerva Asistan bütçesi", "body": body,
                                  "tag": "assistant-budget", "url": "/admin"})
    except Exception:   # push yan etkidir; uyarı kaydı yeter
        pass
    return True
