# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
PDKS router — personel devam takibi (giriş/çıkış, program, izin, puantaj).

Cross-cutting modül (CRM/Drive gibi): domain scoping YOK.  Hesap mantığı
core/pdks.py'de (saf fonksiyonlar); burada yalnız DB I/O + doğrulama +
serileştirme var.  Self giriş/çıkışta zaman damgası DAİMA sunucu saatidir.
Manuel düzeltmeler audit-log'a yazılır; olay silme soft-delete'tir (iz kalır).
"""
import secrets
import time
from datetime import date, datetime, timedelta
from typing import Dict, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, text
from sqlalchemy.orm import Session

from database import (
    TR_OFFSET, get_db, to_tr, User, AppSetting,
    Employee, EmployeeSchedule, AttendanceEvent, AttendanceRequest,
    LeaveRecord, PublicHoliday,
)
from core.audit import log_admin_event
from core.auth import SECRET_KEY
from core.permissions import require_permission
from core.pdks import (
    LEAVE_TYPES, LEAVE_TYPE_LABELS, DAY_STATUS_LABELS,
    WEEKDAY_LABELS, MONTH_LABELS, OPEN_PAIR_MAX_HOURS,
    NUMERIC_CODE_LEN, QR_BUCKET_SECONDS, TOTAL_BREAK_MINUTES, breaks_view,
    STATIC_CODE_ALPHABET, STATIC_CODE_LEN,
    compute_day, compute_month, fmt_minutes,
    geo_within, haversine_m, ip_allowed, leave_for, make_numeric_code,
    make_qr_token, make_static_token, open_in_still_valid, qr_bucket,
    schedule_for, tr_date_of,
    validate_template, verify_numeric_code, verify_qr_token,
    verify_static_code, verify_static_token,
)

router = APIRouter(prefix="/api/pdks", tags=["pdks"])

# Aynı tip olayın bu süre içindeki tekrarı idempotent sayılır (çift tık).
DOUBLE_TAP_SECONDS = 120

# Yedek sayısal kod, QR'a göre kaba kuvvete daha açık (6 hane + ±2 bucket =
# ~5/1.000.000).  Personel başına deneme penceresi bunu pratikte imkânsız
# kılar.  Süreç belleğinde tutulur — restart'ta sıfırlanır (kabul edilebilir:
# saldırgan zaten ofis ağında + ofis konumunda olmak zorunda).
CODE_FAIL_LIMIT = 5
CODE_FAIL_WINDOW_SECONDS = 300
_code_fails: dict = {}          # employee_id → [unix_ts, …]


def _code_fail_blocked(employee_id: int, now_unix: float) -> bool:
    hits = [t for t in _code_fails.get(employee_id, [])
            if now_unix - t < CODE_FAIL_WINDOW_SECONDS]
    _code_fails[employee_id] = hits
    return len(hits) >= CODE_FAIL_LIMIT


def _code_fail_record(employee_id: int, now_unix: float) -> None:
    _code_fails.setdefault(employee_id, []).append(now_unix)

# ── Check-in doğrulama ayarları — AppSetting anahtarları ────────────────────
_CFG_ENFORCE = "pdks.checkin.enforce"
_CFG_IPS = "pdks.checkin.allowed_ips"
_CFG_LAT = "pdks.checkin.lat"
_CFG_LON = "pdks.checkin.lon"
_CFG_RADIUS = "pdks.checkin.radius_m"
# QR kaynağı: "static" = girişe asılan BASILI kod (ekran gerekmez, varsayılan)
# | "rotating" = kiosk ekranında 30 sn'de bir dönen kod (cihaz gerekir).
_CFG_QR_MODE = "pdks.checkin.qr_mode"
_CFG_STATIC_CODE = "pdks.checkin.static_code"
_DEFAULT_RADIUS_M = 150
_QR_MODES = ("static", "rotating")

_SOURCE_LABELS = {"self": "Kendi cihazı", "manual": "Manuel (yönetici)",
                  "request": "Personel bildirimi (onaylı)"}
# Personel en fazla bu kadar geriye dönük bildirim yapabilir (gün).
REQUEST_MAX_AGE_DAYS = 14
_TYPE_LABELS = {"in": "Giriş", "out": "Çıkış"}


# ─── Pydantic modelleri ──────────────────────────────────────────────────────

class CheckBody(BaseModel):
    type: str = Field(..., pattern="^(in|out)$")
    # Üçlü doğrulama açıkken zorunlu; kapalıyken yok sayılır.
    qr_token: Optional[str] = Field(None, max_length=120)
    # Kamera çalışmadığında QR yerine geçen, ekranda QR'ın altında yazan kod.
    manual_code: Optional[str] = Field(None, max_length=12)
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lon: Optional[float] = Field(None, ge=-180, le=180)
    accuracy: Optional[float] = Field(None, ge=0)


class RequestBody(BaseModel):
    """Personelin "unuttum" bildirimi — TR duvar saati verilir."""
    event_type: str = Field(..., pattern="^(in|out)$")
    date: date
    time: str = Field(..., pattern="^[0-2][0-9]:[0-5][0-9]$")
    note: str = Field(..., min_length=3, max_length=300)


class DecisionBody(BaseModel):
    note: Optional[str] = Field(None, max_length=300)


class CheckinConfigBody(BaseModel):
    enforce: Optional[bool] = None
    allowed_ips: Optional[str] = Field(None, max_length=500)
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lon: Optional[float] = Field(None, ge=-180, le=180)
    radius_m: Optional[int] = Field(None, ge=10, le=5000)
    qr_mode: Optional[str] = Field(None, pattern="^(static|rotating)$")


class EmployeeBody(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=150)
    title: Optional[str] = Field(None, max_length=100)
    start_date: Optional[date] = None
    notes: Optional[str] = Field(None, max_length=2000)
    user_id: Optional[int] = None
    is_active: Optional[bool] = None          # yalnız PUT'ta anlamlı


class ScheduleBody(BaseModel):
    effective_from: date
    weekly_template: Dict[str, Optional[dict]]
    lunch_break_minutes: int = Field(60, ge=0, le=240)


class EventBody(BaseModel):
    employee_id: int
    event_type: str = Field(..., pattern="^(in|out)$")
    date: date                                 # TR-yerel takvim günü
    time: str = Field(..., pattern="^[0-2][0-9]:[0-5][0-9]$")
    note: str = Field(..., min_length=3, max_length=300)


class EventUpdateBody(BaseModel):
    date: date
    time: str = Field(..., pattern="^[0-2][0-9]:[0-5][0-9]$")
    note: str = Field(..., min_length=3, max_length=300)   # düzeltme gerekçesi zorunlu


class LeaveBody(BaseModel):
    employee_id: int
    leave_type: str
    start_date: date
    end_date: date
    note: Optional[str] = Field(None, max_length=300)


class HolidayBody(BaseModel):
    holiday_date: date
    name: str = Field(..., min_length=1, max_length=150)
    is_half_day: bool = False


# ─── Ortak yardımcılar ───────────────────────────────────────────────────────

def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": msg})


# ── AppSetting KV yardımcıları (core/shopify.py:104-118 kalıbı) ─────────────

def _get_setting(db: Session, key: str) -> Optional[str]:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row else None


def _set_setting(db: Session, key: str, value: Optional[str]) -> None:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if value is None:
        if row:
            db.delete(row)
        return
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


def _checkin_cfg(db: Session) -> dict:
    """Üçlü doğrulama ayarları — okunamayan/eksik değerler güvenli varsayılana
    düşer (enforce=False, radius=150; lat/lon eksikse enforce zaten PUT'ta
    engellenir ama burada da None kalabilir — check_in_out ayrıca kontrol eder)."""
    def _f(key):
        raw = _get_setting(db, key)
        try:
            return float(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            return None

    try:
        radius = int(float(_get_setting(db, _CFG_RADIUS) or _DEFAULT_RADIUS_M))
    except (TypeError, ValueError):
        radius = _DEFAULT_RADIUS_M
    mode = (_get_setting(db, _CFG_QR_MODE) or "static").lower()
    if mode not in _QR_MODES:
        mode = "static"
    return {
        "enforce": (_get_setting(db, _CFG_ENFORCE) or "false").lower() == "true",
        "allowed_ips": _get_setting(db, _CFG_IPS) or "",
        "lat": _f(_CFG_LAT),
        "lon": _f(_CFG_LON),
        "radius_m": max(10, radius),
        "qr_mode": mode,
        "static_code": _get_setting(db, _CFG_STATIC_CODE) or "",
    }


def _real_client_ip(request: Request) -> str:
    """Gerçek istemci IP'si — nginx arkasında `request.client.host` genelde
    127.0.0.1'dir.  Yalnız DOĞRUDAN bağlantı loopback/test istemcisiyse
    X-Real-IP başlığına güvenilir (nginx bu başlığı proxy_set_header ile
    KENDİSİ yazar, istemci değerini EZER — dışarıdan sahtelenemez).  Peer
    doğrudan bir dış adres ise (nginx yoksa/atlanmışsa) header'a asla
    güvenilmez — sahte X-Real-IP ile allowlist atlatılabilirdi."""
    peer = (request.client.host if request and request.client else "") or ""
    if peer in ("127.0.0.1", "::1", "testclient"):
        real = (request.headers.get("x-real-ip") or "").strip()
        return real or peer
    return peer


def _actor(current_user: dict) -> str:
    return current_user.get("full_name") or current_user.get("username") or "—"


def _uid(current_user: dict) -> int:
    try:
        return int(current_user.get("sub", 0))
    except (TypeError, ValueError):
        return 0


def _employee_for_user(db: Session, current_user: dict) -> Optional[Employee]:
    uid = _uid(current_user)
    if not uid:
        return None
    return (db.query(Employee)
            .filter(Employee.user_id == uid, Employee.is_active == True)  # noqa: E712
            .first())


def _get_employee(db: Session, employee_id: int) -> Optional[Employee]:
    """Yönetici uçları için personel — pasif dahil.  İşten ayrılan personelin
    son ayı için olay/izin düzeltmesi ve program kaydı yapılabilmeli."""
    return db.query(Employee).filter(Employee.id == employee_id).first()


def _lock_employee(db: Session, employee_id: int) -> None:
    """Personel bazlı danışmanlık kilidi — /check ve manuel olay yazımlarını
    serileştirir (read-then-write yarışında çift açık 'in' oluşmasın).
    872101: PDKS modül anahtarı (keyfî sabit).  SQLite'ta (lokal dev) no-op."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(872101, :eid)"),
                   {"eid": employee_id})


def _last_event(db: Session, employee_id: int) -> Optional[AttendanceEvent]:
    return (db.query(AttendanceEvent)
            .filter(AttendanceEvent.employee_id == employee_id,
                    AttendanceEvent.is_active == True)                     # noqa: E712
            .order_by(AttendanceEvent.ts_utc.desc(), AttendanceEvent.id.desc())
            .first())


def _open_in(db: Session, employee_id: int, now_utc: datetime) -> Optional[AttendanceEvent]:
    """Kapanmamış güncel 'in' olayı.

    Kural core.pdks.open_in_still_valid'de (saf, test edilebilir): 16 saatten
    eski olmayacak VE önceki güne aitse 12 saatten eski olmayacak — böylece
    unutulmuş çıkış ertesi gün hayalî mesai yazmaz, gece vardiyası bozulmaz."""
    last = _last_event(db, employee_id)
    if (last and last.event_type == "in"
            and open_in_still_valid(last.ts_utc, last.work_date, now_utc)):
        return last
    return None


def _resync_out_work_dates(db: Session, employee_id: int, around_ts_list) -> None:
    """Olay mutasyonu sonrası etkilenen 'out' olaylarının work_date'ini yeniden
    türet.  work_date yazım anında denormalize edilir ('out' kapattığı 'in'in
    gününü alır); bir olay taşınınca/silinince/araya eklenince sonraki
    çıkışların günü değişebilir — aksi halde gece vardiyası çifti ikiye
    bölünüp çalışılan süre puantajdan sessizce düşer.
    Çağıran, bekleyen değişiklikleri önce db.flush() etmeli (autoflush kapalı)."""
    seen = set()
    for ts in around_ts_list:
        if ts is None:
            continue
        outs = (db.query(AttendanceEvent)
                .filter(AttendanceEvent.employee_id == employee_id,
                        AttendanceEvent.is_active == True,                 # noqa: E712
                        AttendanceEvent.event_type == "out",
                        AttendanceEvent.ts_utc > ts - timedelta(hours=OPEN_PAIR_MAX_HOURS),
                        AttendanceEvent.ts_utc < ts + timedelta(hours=OPEN_PAIR_MAX_HOURS))
                .all())
        for o in outs:
            if o.id in seen:
                continue
            seen.add(o.id)
            o.work_date = _work_date_for(db, employee_id, "out", o.ts_utc,
                                         exclude_id=o.id)


def _event_dict(ev: AttendanceEvent) -> dict:
    return {"id": ev.id, "event_type": ev.event_type, "ts_utc": ev.ts_utc,
            "source": ev.source}


def _schedule_dicts(db: Session, employee_id: int) -> list:
    rows = (db.query(EmployeeSchedule)
            .filter(EmployeeSchedule.employee_id == employee_id)
            .order_by(EmployeeSchedule.effective_from).all())
    return [{"effective_from": r.effective_from, "template": r.weekly_template,
             "lunch_break_minutes": r.lunch_break_minutes} for r in rows]


def _leave_dicts(db: Session, employee_id: int, start: date, end: date) -> list:
    rows = (db.query(LeaveRecord)
            .filter(LeaveRecord.employee_id == employee_id,
                    LeaveRecord.is_active == True,                         # noqa: E712
                    LeaveRecord.start_date <= end,
                    LeaveRecord.end_date >= start).all())
    return [{"leave_type": r.leave_type, "start_date": r.start_date,
             "end_date": r.end_date} for r in rows]


def _holiday_map(db: Session, start: date, end: date) -> dict:
    rows = (db.query(PublicHoliday)
            .filter(PublicHoliday.is_active == True,                       # noqa: E712
                    PublicHoliday.holiday_date >= start,
                    PublicHoliday.holiday_date <= end).all())
    return {r.holiday_date: {"name": r.name, "is_half_day": bool(r.is_half_day)}
            for r in rows}


def _events_by_date(db: Session, employee_id: int, start: date, end: date) -> dict:
    rows = (db.query(AttendanceEvent)
            .filter(AttendanceEvent.employee_id == employee_id,
                    AttendanceEvent.is_active == True,                     # noqa: E712
                    AttendanceEvent.work_date >= start,
                    AttendanceEvent.work_date <= end)
            .order_by(AttendanceEvent.ts_utc).all())
    out: dict = {}
    for r in rows:
        out.setdefault(r.work_date, []).append(_event_dict(r))
    return out


def _valid_month(year: int, month: int) -> bool:
    return 2000 <= year <= 2100 and 1 <= month <= 12


# ─── Serileştiriciler ────────────────────────────────────────────────────────

def _hhmm(dt_utc: Optional[datetime]) -> str:
    return to_tr(dt_utc).strftime("%H:%M") if dt_utc else ""


def _day_view(day: dict) -> dict:
    d: date = day["date"]
    return {
        "date": d.isoformat(),
        "date_label": d.strftime("%d.%m.%Y"),
        "weekday_label": WEEKDAY_LABELS[d.weekday()],
        "status": day["status"],
        "status_label": day["status_label"],
        "leave_type": day["leave_type"],
        "holiday_name": day["holiday_name"],
        "first_in": _hhmm(day["first_in"]),
        "last_out": _hhmm(day["last_out"]),
        "worked_minutes": day["worked_minutes"],
        "worked_label": fmt_minutes(day["worked_minutes"]),
        "expected_minutes": day["expected_minutes"],
        "overtime_minutes": day["overtime_minutes"],
        "overtime_label": fmt_minutes(day["overtime_minutes"]),
        "missing_minutes": day["missing_minutes"],
        "missing_checkout": day["missing_checkout"],
        "orphan_out": day["orphan_out"],
        "break_deducted": day["break_deducted"],
        "break_minutes": day.get("break_minutes", 0),
        "break_label": fmt_minutes(day.get("break_minutes", 0)),
        "pairs": [{"in": _hhmm(p["in"]["ts_utc"]) if p["in"] else "",
                   "out": _hhmm(p["out"]["ts_utc"]) if p["out"] else "",
                   "minutes": p["minutes"]} for p in day["pairs"]],
    }


def _totals_view(t: dict) -> dict:
    return {
        **t,
        "toplam_calisma_label": fmt_minutes(t["toplam_calisma"]),
        "toplam_fazla_mesai_label": fmt_minutes(t["toplam_fazla_mesai"]),
        "toplam_eksik_label": fmt_minutes(t["toplam_eksik"]),
        "izin_gunleri_label": ", ".join(
            f"{LEAVE_TYPE_LABELS[k]}: {v}" for k, v in t["izin_gunleri"].items() if v
        ) or "—",
    }


def _month_view(data: dict) -> dict:
    return {
        "year": data["year"], "month": data["month"],
        "month_label": f"{MONTH_LABELS[data['month']]} {data['year']}",
        "days": [_day_view(d) for d in data["days"]],
        "totals": _totals_view(data["totals"]),
    }


def _employee_view(e: Employee, db: Session) -> dict:
    uname = None
    if e.user_id:
        u = db.query(User).filter(User.id == e.user_id).first()
        uname = u.username if u else None
    return {
        "id": e.id, "full_name": e.full_name, "title": e.title or "",
        "start_date": e.start_date.isoformat() if e.start_date else None,
        "start_date_label": e.start_date.strftime("%d.%m.%Y") if e.start_date else "",
        "notes": e.notes or "", "user_id": e.user_id, "username": uname,
        "is_active": bool(e.is_active),
    }


def _schedule_view(s: EmployeeSchedule) -> dict:
    return {
        "id": s.id, "employee_id": s.employee_id,
        "effective_from": s.effective_from.isoformat(),
        "effective_from_label": s.effective_from.strftime("%d.%m.%Y"),
        "weekly_template": s.weekly_template,
        "lunch_break_minutes": s.lunch_break_minutes,
        "created_by": s.created_by or "",
        "created_at": to_tr(s.created_at).strftime("%d.%m.%Y %H:%M") if s.created_at else "",
    }


def _event_view(ev: AttendanceEvent) -> dict:
    tr = to_tr(ev.ts_utc)
    return {
        "id": ev.id, "employee_id": ev.employee_id,
        "event_type": ev.event_type, "type_label": _TYPE_LABELS.get(ev.event_type, ev.event_type),
        "ts_label": tr.strftime("%d.%m.%Y %H:%M") if tr else "",
        "date": tr.date().isoformat() if tr else "",
        "time": tr.strftime("%H:%M") if tr else "",
        "work_date": ev.work_date.isoformat() if ev.work_date else "",
        "work_date_label": ev.work_date.strftime("%d.%m.%Y") if ev.work_date else "",
        "source": ev.source, "source_label": _SOURCE_LABELS.get(ev.source, ev.source),
        "corrected_by": ev.corrected_by or "",
        "correction_note": ev.correction_note or "",
        "is_active": bool(ev.is_active),
    }


def _leave_view(lv: LeaveRecord, db: Session) -> dict:
    emp = db.query(Employee).filter(Employee.id == lv.employee_id).first()
    return {
        "id": lv.id, "employee_id": lv.employee_id,
        "employee_name": emp.full_name if emp else "—",
        "leave_type": lv.leave_type,
        "leave_type_label": LEAVE_TYPE_LABELS.get(lv.leave_type, lv.leave_type),
        "start_date": lv.start_date.isoformat(),
        "start_date_label": lv.start_date.strftime("%d.%m.%Y"),
        "end_date": lv.end_date.isoformat(),
        "end_date_label": lv.end_date.strftime("%d.%m.%Y"),
        "note": lv.note or "", "created_by": lv.created_by or "",
    }


def _holiday_view(h: PublicHoliday) -> dict:
    return {
        "id": h.id, "holiday_date": h.holiday_date.isoformat(),
        "holiday_date_label": h.holiday_date.strftime("%d.%m.%Y"),
        "weekday_label": WEEKDAY_LABELS[h.holiday_date.weekday()],
        "name": h.name, "is_half_day": bool(h.is_half_day),
    }


def _today_status(db: Session, emp: Employee) -> dict:
    """Giriş/Çıkış sekmesinin durum kartı — bugünün olayları + canlı süre."""
    now = datetime.utcnow()
    today = tr_date_of(now)
    events = (db.query(AttendanceEvent)
              .filter(AttendanceEvent.employee_id == emp.id,
                      AttendanceEvent.is_active == True,                   # noqa: E712
                      AttendanceEvent.work_date == today)
              .order_by(AttendanceEvent.ts_utc).all())
    schedules = _schedule_dicts(db, emp.id)
    sched = schedule_for(schedules, today)
    leaves = _leave_dicts(db, emp.id, today, today)
    holidays = _holiday_map(db, today, today)
    day = compute_day(today, [_event_dict(e) for e in events], sched,
                      leave_type=leave_for(leaves, today),
                      holiday=holidays.get(today))
    open_ev = _open_in(db, emp.id, now)
    worked_live = day["worked_minutes"]
    if open_ev is not None:
        worked_live += int((now - open_ev.ts_utc).total_seconds() // 60)
    cfg = _checkin_cfg(db)
    return {
        "employee": {"id": emp.id, "full_name": emp.full_name},
        "state": "iceride" if open_ev is not None else "disarida",
        "open_since": _hhmm(open_ev.ts_utc) if open_ev is not None else "",
        "today": _day_view(day),
        "worked_live_minutes": worked_live,
        "worked_live_label": fmt_minutes(worked_live),
        "schedule": sched,
        "events": [_event_view(e) for e in events],
        # İstemci (pdks.html) bu bayrağa göre konum+QR akışını devreye alır.
        # DİKKAT: static_code ASLA buraya konmaz — personele sızarsa ofise
        # gelmeden imza atılabilirdi.  Yalnız akışı belirleyen mod paylaşılır.
        "checkin": {"enforce": cfg["enforce"], "qr_mode": cfg["qr_mode"]},
        # Mola şeması şirket geneli sabit — personel ekranda görsün.
        "breaks": breaks_view(),
        "break_minutes_total": TOTAL_BREAK_MINUTES,
    }


def _month_payload(db: Session, emp: Employee, year: int, month: int) -> dict:
    from calendar import monthrange
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    data = compute_month(
        year, month,
        _schedule_dicts(db, emp.id),
        _events_by_date(db, emp.id, start, end),
        _leave_dicts(db, emp.id, start, end),
        _holiday_map(db, start, end),
        tr_date_of(datetime.utcnow()),
    )
    return _month_view(data)


# ─── Self endpoint'ler ───────────────────────────────────────────────────────

@router.post("/check")
def check_in_out(
    data: CheckBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "check")),
):
    emp = _employee_for_user(db, current_user)
    if not emp:
        return _err(404, "Personel kaydınız bulunamadı — yöneticinize başvurun.")

    ip = _real_client_ip(request)
    cfg = _checkin_cfg(db)
    if cfg["enforce"]:
        # Sıra: IP (en ucuz) → QR (sahte token'ı erken ele) → konum.
        if not ip_allowed(ip, cfg["allowed_ips"]):
            return _err(400, "Ofis internetine bağlı değilsiniz — giriş/çıkış "
                             "yalnızca ofis ağından (Minerva108 Wi-Fi) yapılabilir.")
        now_unix = time.time()
        if cfg["qr_mode"] == "static":
            # Basılı QR: içerik sabit, süre dolmaz.  Kamera okuyamazsa personel
            # afişteki kodu elle yazabilir (aynı kod, aynı doğrulama).
            given = data.qr_token or data.manual_code
            if not given:
                return _err(400, "QR kod okutulmadı — girişteki QR kodu okutun "
                                 "ya da afişteki kodu elle girin.")
            if not cfg["static_code"]:
                return _err(400, "PDKS doğrulama ayarları eksik — yöneticinize "
                                 "başvurun.")
            if _code_fail_blocked(emp.id, now_unix):
                return _err(429, "Çok fazla hatalı deneme — birkaç dakika sonra "
                                 "tekrar deneyin.")
            state = (verify_static_token(cfg["static_code"], given)
                     if data.qr_token else
                     verify_static_code(cfg["static_code"], given))
            if state != "ok":
                _code_fail_record(emp.id, now_unix)
                return _err(400, "QR kod geçersiz — girişteki Minerva PDKS "
                                 "kodunu okutun.")
            _code_fails.pop(emp.id, None)
        elif data.qr_token:
            qr_state = verify_qr_token(SECRET_KEY, data.qr_token, now_unix)
            if qr_state == "expired":
                return _err(400, "QR kodun süresi dolmuş — ekrandaki güncel kodu "
                                 "tekrar okutun.")
            if qr_state != "ok":
                return _err(400, "QR kod geçersiz — girişteki ekrandaki canlı "
                                 "kodu okutun.")
        elif data.manual_code:
            # Kamera yoksa/okumuyorsa: ekranda QR'ın altında yazan sayısal kod.
            if _code_fail_blocked(emp.id, now_unix):
                return _err(429, "Çok fazla hatalı kod denemesi — birkaç dakika "
                                 "sonra tekrar deneyin veya QR'ı okutun.")
            code_state = verify_numeric_code(SECRET_KEY, data.manual_code, now_unix)
            if code_state != "ok":
                _code_fail_record(emp.id, now_unix)
            if code_state == "expired":
                return _err(400, "Kodun süresi dolmuş — ekranda o an yazan yeni "
                                 "kodu girin.")
            if code_state != "ok":
                return _err(400, f"Kod geçersiz — girişteki ekranda yazan "
                                 f"{NUMERIC_CODE_LEN} haneli kodu girin.")
            _code_fails.pop(emp.id, None)      # doğru kod → sayaç sıfırlanır
        else:
            return _err(400, "QR kod okutulmadı — girişteki ekrandan güncel QR "
                             "kodu okutun ya da ekrandaki sayısal kodu girin.")
        if data.lat is None or data.lon is None:
            return _err(400, "Konum bilgisi alınamadı — konum iznini verip "
                             "tekrar deneyin.")
        if cfg["lat"] is None or cfg["lon"] is None:
            return _err(400, "PDKS doğrulama ayarları eksik — yöneticinize "
                             "başvurun.")
        dist = haversine_m(data.lat, data.lon, cfg["lat"], cfg["lon"])
        if not geo_within(dist, cfg["radius_m"], data.accuracy):
            return _err(400, "Ofis konumunda görünmüyorsunuz — giriş/çıkış "
                             "yalnızca ofiste yapılabilir.")

    _lock_employee(db, emp.id)   # paralel iki istek çift açık 'in' yazmasın
    now = datetime.utcnow()
    last = _last_event(db, emp.id)

    # Çift tık koruması — aynı tip olay kısa süre içinde tekrar geldiyse
    # yenisini yazma, mevcut durumu döndür.  (>= 0: gelecek tarihli hatalı
    # olay gerçek girişi "Zaten kaydedildi" diye yutmasın.)
    if (last and last.event_type == data.type
            and timedelta(0) <= now - last.ts_utc <= timedelta(seconds=DOUBLE_TAP_SECONDS)):
        st = _today_status(db, emp)
        st["message"] = "Zaten kaydedildi."
        return st

    open_ev = _open_in(db, emp.id, now)
    if data.type == "in":
        if open_ev is not None:
            return _err(400, "Zaten giriş yaptınız — önce çıkış yapın.")
        work_date = tr_date_of(now)
    else:
        if open_ev is None:
            return _err(400, "Açık giriş kaydınız yok — önce giriş yapın.")
        work_date = open_ev.work_date   # gece yarısını aşan çıkış doğru güne yazılır

    ev = AttendanceEvent(
        employee_id=emp.id, event_type=data.type, ts_utc=now,
        work_date=work_date, source="self",
        created_by_user_id=_uid(current_user) or None, ip_address=ip or None,
        # Doğrulama kapalıyken de gelmişse (istemci her zaman geo göndermeyi
        # dener) kaydedilir — ileride analiz/denetim için faydalı, zararsız.
        geo_lat=data.lat, geo_lon=data.lon, geo_accuracy_m=data.accuracy,
    )
    try:
        db.add(ev)
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Kayıt oluşturulamadı.")
    st = _today_status(db, emp)
    st["message"] = "Giriş kaydedildi." if data.type == "in" else "Çıkış kaydedildi."
    return st


@router.get("/me/today")
def my_today(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "view_own")),
):
    emp = _employee_for_user(db, current_user)
    if not emp:
        return _err(404, "Personel kaydınız bulunamadı — yöneticinize başvurun.")
    return _today_status(db, emp)


@router.get("/me/month")
def my_month(
    year: int,
    month: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "view_own")),
):
    if not _valid_month(year, month):
        return _err(400, "Geçersiz yıl/ay.")
    emp = _employee_for_user(db, current_user)
    if not emp:
        return _err(404, "Personel kaydınız bulunamadı — yöneticinize başvurun.")
    return _month_payload(db, emp, year, month)


# ─── Günlük genel bakış (yönetici) ───────────────────────────────────────────

@router.get("/day")
def day_overview(
    date_param: str = Query(None, alias="date"),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("pdks", "view_all")),
):
    now = datetime.utcnow()
    if date_param:
        try:
            d = date.fromisoformat(date_param)
        except ValueError:
            return _err(400, "Geçersiz tarih (YYYY-AA-GG bekleniyor).")
    else:
        d = tr_date_of(now)
    holidays = _holiday_map(db, d, d)
    rows = []
    today_tr = tr_date_of(now)
    # O gün olayı olan pasif (ayrılmış) personel de geçmiş gün görünümüne girer
    ids_with_events = [r[0] for r in
                       (db.query(AttendanceEvent.employee_id)
                        .filter(AttendanceEvent.is_active == True,         # noqa: E712
                                AttendanceEvent.work_date == d)
                        .distinct())]
    employees = (db.query(Employee)
                 .filter(or_(Employee.is_active == True,                   # noqa: E712
                             Employee.id.in_(ids_with_events or [0])))
                 .order_by(Employee.full_name).all())
    for emp in employees:
        events = (db.query(AttendanceEvent)
                  .filter(AttendanceEvent.employee_id == emp.id,
                          AttendanceEvent.is_active == True,               # noqa: E712
                          AttendanceEvent.work_date == d)
                  .order_by(AttendanceEvent.ts_utc).all())
        day = compute_day(
            d, [_event_dict(e) for e in events],
            schedule_for(_schedule_dicts(db, emp.id), d),
            leave_type=leave_for(_leave_dicts(db, emp.id, d, d), d),
            holiday=holidays.get(d),
        )
        if d > today_tr:
            # Gelecek tarih: kimse "Devamsız" damgası yememeli
            day["status"] = "bekliyor"
            day["status_label"] = DAY_STATUS_LABELS["bekliyor"]
        rows.append({
            "employee": {"id": emp.id, "full_name": emp.full_name, "title": emp.title or ""},
            "state": "iceride" if _open_in(db, emp.id, now) is not None else "disarida",
            "day": _day_view(day),
            "events": [_event_view(e) for e in events],
        })
    return {"date": d.isoformat(), "date_label": d.strftime("%d.%m.%Y"),
            "weekday_label": WEEKDAY_LABELS[d.weekday()],
            "holiday": holidays.get(d, {}).get("name"), "rows": rows}


# ─── Personel CRUD ───────────────────────────────────────────────────────────

def _validate_user_link(db: Session, user_id: Optional[int],
                        exclude_employee_id: Optional[int] = None):
    """user_id bağı doğrulaması.  Hata → JSONResponse, geçerli → None."""
    if user_id is None:
        return None
    u = db.query(User).filter(User.id == user_id).first()
    if not u or not u.is_active:
        return _err(404, "Bağlanacak kullanıcı bulunamadı.")
    if u.role == "Distributor":
        return _err(400, "Distribütör hesabı personele bağlanamaz.")
    q = db.query(Employee).filter(Employee.user_id == user_id)
    if exclude_employee_id:
        q = q.filter(Employee.id != exclude_employee_id)
    if q.first():
        return _err(400, "Bu kullanıcı zaten başka bir personele bağlı.")
    return None


@router.get("/employees")
def list_employees(
    include_inactive: int = 0,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("pdks", "view_all")),
):
    q = db.query(Employee)
    if not include_inactive:
        q = q.filter(Employee.is_active == True)                           # noqa: E712
    rows = q.order_by(Employee.full_name).all()
    out = []
    for e in rows:
        v = _employee_view(e, db)
        sched = schedule_for(_schedule_dicts(db, e.id), tr_date_of(datetime.utcnow()))
        v["today_schedule"] = sched
        out.append(v)
    return {"employees": out, "count": len(out)}


@router.get("/linkable-users")
def linkable_users(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("pdks", "manage")),
):
    """Personele bağlanabilir hesaplar — aktif, Distribütör olmayan, bağsız."""
    linked = {e.user_id for e in db.query(Employee).filter(Employee.user_id != None).all()}  # noqa: E711
    users = (db.query(User)
             .filter(User.is_active == True, User.role != "Distributor")   # noqa: E712
             .order_by(User.full_name).all())
    return {"users": [{"id": u.id, "username": u.username, "full_name": u.full_name,
                       "role": u.role}
                      for u in users if u.id not in linked]}


@router.post("/employees", status_code=201)
def create_employee(
    data: EmployeeBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "manage")),
):
    err = _validate_user_link(db, data.user_id)
    if err:
        return err
    e = Employee(
        full_name=data.full_name.strip(), title=(data.title or "").strip() or None,
        start_date=data.start_date, notes=(data.notes or "").strip() or None,
        user_id=data.user_id,
    )
    try:
        db.add(e)
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Personel kaydedilemedi.")
    log_admin_event(db, request, actor=current_user, action="pdks.employee.create",
                    target_type="pdks_employee", target_id=e.id, target_name=e.full_name,
                    details={"user_id": e.user_id})
    return {"id": e.id, "message": "Personel kaydedildi."}


@router.get("/employees/{employee_id}")
def get_employee(
    employee_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("pdks", "view_all")),
):
    e = db.query(Employee).filter(Employee.id == employee_id).first()
    if not e:
        return _err(404, "Personel bulunamadı.")
    v = _employee_view(e, db)
    versions = (db.query(EmployeeSchedule)
                .filter(EmployeeSchedule.employee_id == e.id)
                .order_by(EmployeeSchedule.effective_from.desc()).all())
    v["schedules"] = [_schedule_view(s) for s in versions]
    return v


@router.put("/employees/{employee_id}")
def update_employee(
    employee_id: int,
    data: EmployeeBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "manage")),
):
    e = db.query(Employee).filter(Employee.id == employee_id).first()
    if not e:
        return _err(404, "Personel bulunamadı.")
    err = _validate_user_link(db, data.user_id, exclude_employee_id=e.id)
    if err:
        return err
    e.full_name = data.full_name.strip()
    e.title = (data.title or "").strip() or None
    e.start_date = data.start_date
    e.notes = (data.notes or "").strip() or None
    e.user_id = data.user_id
    if data.is_active is not None:
        e.is_active = bool(data.is_active)
    try:
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Personel güncellenemedi.")
    log_admin_event(db, request, actor=current_user, action="pdks.employee.update",
                    target_type="pdks_employee", target_id=e.id, target_name=e.full_name,
                    details={"user_id": e.user_id, "is_active": bool(e.is_active)})
    return {"id": e.id, "message": "Personel güncellendi."}


@router.delete("/employees/{employee_id}")
def delete_employee(
    employee_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "manage")),
):
    e = db.query(Employee).filter(Employee.id == employee_id,
                                  Employee.is_active == True).first()      # noqa: E712
    if not e:
        return _err(404, "Personel bulunamadı.")
    e.is_active = False
    db.commit()
    log_admin_event(db, request, actor=current_user, action="pdks.employee.delete",
                    target_type="pdks_employee", target_id=e.id, target_name=e.full_name)
    return {"message": "Personel pasifleştirildi."}


@router.put("/employees/{employee_id}/schedule")
def upsert_schedule(
    employee_id: int,
    data: ScheduleBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "manage")),
):
    e = _get_employee(db, employee_id)
    if not e:
        return _err(404, "Personel bulunamadı.")
    msg = validate_template(data.weekly_template)
    if msg:
        return _err(400, msg)
    import json as _json
    raw = _json.dumps(data.weekly_template, ensure_ascii=False)
    # Aynı effective_from = aynı versiyonun düzeltilmesi (yerinde güncellenir);
    # farklı tarih = YENİ versiyon.  Puantaj canlı hesaplandığından geçmiş
    # effective_from'lu her yazım RETROAKTİFTİR — eski değerler audit'e yazılır
    # ki kaza geri alınabilsin (UI varsayılanı bugüne çekilerek kaza yüzeyi
    # ayrıca küçültüldü).
    details = {"effective_from": data.effective_from.isoformat(),
               "lunch": data.lunch_break_minutes,
               "retroaktif": data.effective_from < tr_date_of(datetime.utcnow())}
    existing = (db.query(EmployeeSchedule)
                .filter(EmployeeSchedule.employee_id == e.id,
                        EmployeeSchedule.effective_from == data.effective_from)
                .first())
    if existing:
        details["eski_template"] = existing.weekly_template
        details["eski_mola"] = existing.lunch_break_minutes
        existing.weekly_template = raw
        existing.lunch_break_minutes = data.lunch_break_minutes
        existing.created_by = _actor(current_user)
        sid = existing.id
    else:
        s = EmployeeSchedule(
            employee_id=e.id, effective_from=data.effective_from,
            weekly_template=raw, lunch_break_minutes=data.lunch_break_minutes,
            created_by=_actor(current_user),
        )
        db.add(s)
        db.flush()
        sid = s.id
    try:
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Program kaydedilemedi.")
    log_admin_event(db, request, actor=current_user, action="pdks.schedule.upsert",
                    target_type="pdks_employee", target_id=e.id, target_name=e.full_name,
                    details=details)
    return {"id": sid, "message": "Çalışma programı kaydedildi."}


# ─── Olaylar (manuel düzeltme) ───────────────────────────────────────────────

def _tr_wall_to_utc(d: date, hm: str) -> datetime:
    h, m = hm.split(":")
    return datetime(d.year, d.month, d.day, int(h), int(m)) - TR_OFFSET


def _work_date_for(db: Session, employee_id: int, event_type: str,
                   ts_utc: datetime, exclude_id: Optional[int] = None) -> date:
    """work_date atama kuralı — 'in' kendi TR günü; 'out' kapattığı açık 'in'
    <16 saatlikse onun work_date'i."""
    if event_type == "in":
        return tr_date_of(ts_utc)
    q = (db.query(AttendanceEvent)
         .filter(AttendanceEvent.employee_id == employee_id,
                 AttendanceEvent.is_active == True,                        # noqa: E712
                 AttendanceEvent.ts_utc < ts_utc))
    if exclude_id:
        q = q.filter(AttendanceEvent.id != exclude_id)
    prev = q.order_by(AttendanceEvent.ts_utc.desc(), AttendanceEvent.id.desc()).first()
    if (prev and prev.event_type == "in"
            and ts_utc - prev.ts_utc < timedelta(hours=OPEN_PAIR_MAX_HOURS)):
        return prev.work_date
    return tr_date_of(ts_utc)


@router.get("/events")
def list_events(
    employee_id: int,
    start: str,
    end: str,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("pdks", "view_all")),
):
    try:
        d1, d2 = date.fromisoformat(start), date.fromisoformat(end)
    except ValueError:
        return _err(400, "Geçersiz tarih aralığı.")
    rows = (db.query(AttendanceEvent)
            .filter(AttendanceEvent.employee_id == employee_id,
                    AttendanceEvent.work_date >= d1,
                    AttendanceEvent.work_date <= d2)
            .order_by(AttendanceEvent.ts_utc).all())
    return {"events": [_event_view(e) for e in rows]}


@router.post("/events", status_code=201)
def create_event(
    data: EventBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "manage")),
):
    e = _get_employee(db, data.employee_id)
    if not e:
        return _err(404, "Personel bulunamadı.")
    try:
        ts = _tr_wall_to_utc(data.date, data.time)
    except ValueError:
        return _err(400, "Geçersiz saat.")
    if ts > datetime.utcnow() + timedelta(minutes=5):
        return _err(400, "Gelecek tarihli olay girilemez.")
    _lock_employee(db, e.id)
    ev = AttendanceEvent(
        employee_id=e.id, event_type=data.event_type, ts_utc=ts,
        work_date=_work_date_for(db, e.id, data.event_type, ts),
        source="manual", created_by_user_id=_uid(current_user) or None,
        correction_note=data.note.strip(),
    )
    try:
        db.add(ev)
        db.flush()
        # Araya retroaktif olay girmek sonraki çıkışların gününü değiştirebilir
        _resync_out_work_dates(db, e.id, [ts])
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Olay kaydedilemedi.")
    log_admin_event(db, request, actor=current_user, action="pdks.event.create",
                    target_type="pdks_event", target_id=ev.id, target_name=e.full_name,
                    details={"tip": _TYPE_LABELS[data.event_type],
                             "zaman": f"{data.date.isoformat()} {data.time}",
                             "not": data.note})
    return {"id": ev.id, "message": "Olay kaydedildi."}


@router.put("/events/{event_id}")
def update_event(
    event_id: int,
    data: EventUpdateBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "manage")),
):
    ev = db.query(AttendanceEvent).filter(AttendanceEvent.id == event_id,
                                          AttendanceEvent.is_active == True).first()  # noqa: E712
    if not ev:
        return _err(404, "Olay bulunamadı.")
    try:
        ts = _tr_wall_to_utc(data.date, data.time)
    except ValueError:
        return _err(400, "Geçersiz saat.")
    if ts > datetime.utcnow() + timedelta(minutes=5):
        return _err(400, "Gelecek tarihli olay girilemez.")
    _lock_employee(db, ev.employee_id)
    old_ts = ev.ts_utc
    ev.ts_utc = ts
    ev.work_date = _work_date_for(db, ev.employee_id, ev.event_type, ts,
                                  exclude_id=ev.id)
    ev.corrected_by = _actor(current_user)
    ev.corrected_at = datetime.utcnow()
    ev.correction_note = data.note.strip()
    try:
        db.flush()
        # Taşınan olayın eski ve yeni konumunu izleyen çıkışların günü değişebilir
        _resync_out_work_dates(db, ev.employee_id, [old_ts, ts])
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Olay güncellenemedi.")
    log_admin_event(db, request, actor=current_user, action="pdks.event.update",
                    target_type="pdks_event", target_id=ev.id,
                    details={"zaman": f"{data.date.isoformat()} {data.time}",
                             "not": data.note})
    return {"id": ev.id, "message": "Olay güncellendi."}


@router.delete("/events/{event_id}")
def delete_event(
    event_id: int,
    note: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "manage")),
):
    if not (note or "").strip() or len(note.strip()) < 3:
        return _err(400, "Silme gerekçesi zorunlu.")
    ev = db.query(AttendanceEvent).filter(AttendanceEvent.id == event_id,
                                          AttendanceEvent.is_active == True).first()  # noqa: E712
    if not ev:
        return _err(404, "Olay bulunamadı.")
    _lock_employee(db, ev.employee_id)
    ev.is_active = False
    ev.corrected_by = _actor(current_user)
    ev.corrected_at = datetime.utcnow()
    ev.correction_note = note.strip()
    db.flush()
    # Silinen 'in'in yetim bıraktığı çıkışların günü yeniden türetilmeli
    _resync_out_work_dates(db, ev.employee_id, [ev.ts_utc])
    db.commit()
    log_admin_event(db, request, actor=current_user, action="pdks.event.delete",
                    target_type="pdks_event", target_id=ev.id,
                    details={"not": note.strip()})
    return {"message": "Olay silindi."}


# ─── İzinler ─────────────────────────────────────────────────────────────────

def _validate_leave(db: Session, data: LeaveBody,
                    exclude_id: Optional[int] = None):
    if data.leave_type not in LEAVE_TYPES:
        return _err(400, "Geçersiz izin türü.")
    if data.end_date < data.start_date:
        return _err(400, "Bitiş tarihi başlangıçtan önce olamaz.")
    if data.leave_type == "diger" and not (data.note or "").strip():
        return _err(400, "'Diğer' izin türünde açıklama zorunlu.")
    q = (db.query(LeaveRecord)
         .filter(LeaveRecord.employee_id == data.employee_id,
                 LeaveRecord.is_active == True,                            # noqa: E712
                 LeaveRecord.start_date <= data.end_date,
                 LeaveRecord.end_date >= data.start_date))
    if exclude_id:
        q = q.filter(LeaveRecord.id != exclude_id)
    if q.first():
        return _err(400, "Bu aralık mevcut bir izinle çakışıyor.")
    return None


@router.get("/leaves")
def list_leaves(
    year: int = None,
    employee_id: int = None,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("pdks", "view_all")),
):
    q = db.query(LeaveRecord).filter(LeaveRecord.is_active == True)        # noqa: E712
    if employee_id:
        q = q.filter(LeaveRecord.employee_id == employee_id)
    if year:
        q = q.filter(LeaveRecord.start_date <= date(year, 12, 31),
                     LeaveRecord.end_date >= date(year, 1, 1))
    rows = q.order_by(LeaveRecord.start_date.desc()).limit(500).all()
    return {"leaves": [_leave_view(r, db) for r in rows]}


@router.post("/leaves", status_code=201)
def create_leave(
    data: LeaveBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "manage")),
):
    e = _get_employee(db, data.employee_id)
    if not e:
        return _err(404, "Personel bulunamadı.")
    err = _validate_leave(db, data)
    if err:
        return err
    lv = LeaveRecord(
        employee_id=e.id, leave_type=data.leave_type,
        start_date=data.start_date, end_date=data.end_date,
        note=(data.note or "").strip() or None, created_by=_actor(current_user),
    )
    try:
        db.add(lv)
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "İzin kaydedilemedi.")
    log_admin_event(db, request, actor=current_user, action="pdks.leave.create",
                    target_type="pdks_leave", target_id=lv.id, target_name=e.full_name,
                    details={"tur": LEAVE_TYPE_LABELS[lv.leave_type],
                             "aralik": f"{lv.start_date} → {lv.end_date}"})
    return {"id": lv.id, "message": "İzin kaydedildi."}


@router.put("/leaves/{leave_id}")
def update_leave(
    leave_id: int,
    data: LeaveBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "manage")),
):
    lv = db.query(LeaveRecord).filter(LeaveRecord.id == leave_id,
                                      LeaveRecord.is_active == True).first()  # noqa: E712
    if not lv:
        return _err(404, "İzin bulunamadı.")
    e = _get_employee(db, data.employee_id)
    if not e:
        return _err(404, "Personel bulunamadı.")
    err = _validate_leave(db, data, exclude_id=lv.id)
    if err:
        return err
    lv.employee_id = e.id
    lv.leave_type = data.leave_type
    lv.start_date = data.start_date
    lv.end_date = data.end_date
    lv.note = (data.note or "").strip() or None
    try:
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "İzin güncellenemedi.")
    log_admin_event(db, request, actor=current_user, action="pdks.leave.update",
                    target_type="pdks_leave", target_id=lv.id, target_name=e.full_name,
                    details={"tur": LEAVE_TYPE_LABELS[lv.leave_type],
                             "aralik": f"{lv.start_date} → {lv.end_date}"})
    return {"id": lv.id, "message": "İzin güncellendi."}


@router.delete("/leaves/{leave_id}")
def delete_leave(
    leave_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "manage")),
):
    lv = db.query(LeaveRecord).filter(LeaveRecord.id == leave_id,
                                      LeaveRecord.is_active == True).first()  # noqa: E712
    if not lv:
        return _err(404, "İzin bulunamadı.")
    lv.is_active = False
    db.commit()
    log_admin_event(db, request, actor=current_user, action="pdks.leave.delete",
                    target_type="pdks_leave", target_id=lv.id)
    return {"message": "İzin silindi."}


# ─── Resmi tatiller ──────────────────────────────────────────────────────────

@router.get("/holidays")
def list_holidays(
    year: int = None,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("pdks", "view_own")),
):
    q = db.query(PublicHoliday).filter(PublicHoliday.is_active == True)    # noqa: E712
    if year:
        q = q.filter(PublicHoliday.holiday_date >= date(year, 1, 1),
                     PublicHoliday.holiday_date <= date(year, 12, 31))
    rows = q.order_by(PublicHoliday.holiday_date).all()
    return {"holidays": [_holiday_view(h) for h in rows]}


@router.post("/holidays", status_code=201)
def create_holiday(
    data: HolidayBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "manage")),
):
    if (db.query(PublicHoliday)
            .filter(PublicHoliday.holiday_date == data.holiday_date,
                    PublicHoliday.is_active == True).first()):             # noqa: E712
        return _err(400, "Bu tarihte zaten bir resmi tatil kayıtlı.")
    h = PublicHoliday(holiday_date=data.holiday_date, name=data.name.strip(),
                      is_half_day=data.is_half_day, created_by=_actor(current_user))
    try:
        db.add(h)
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Tatil kaydedilemedi.")
    log_admin_event(db, request, actor=current_user, action="pdks.holiday.create",
                    target_type="pdks_holiday", target_id=h.id, target_name=h.name,
                    details={"tarih": h.holiday_date.isoformat(),
                             "yarim_gun": h.is_half_day})
    return {"id": h.id, "message": "Resmi tatil kaydedildi."}


@router.delete("/holidays/{holiday_id}")
def delete_holiday(
    holiday_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "manage")),
):
    h = db.query(PublicHoliday).filter(PublicHoliday.id == holiday_id,
                                       PublicHoliday.is_active == True).first()  # noqa: E712
    if not h:
        return _err(404, "Tatil bulunamadı.")
    # Soft delete — geçmiş tatili silmek kapanmış ayların puantajını geriye
    # dönük değiştirir; iz + geri alma imkânı için satır kalır, detay audit'e.
    h.is_active = False
    db.commit()
    log_admin_event(db, request, actor=current_user, action="pdks.holiday.delete",
                    target_type="pdks_holiday", target_id=h.id, target_name=h.name,
                    details={"tarih": h.holiday_date.isoformat(),
                             "yarim_gun": bool(h.is_half_day)})
    return {"message": "Resmi tatil silindi."}


# ─── Check-in doğrulama — kiosk QR ekranı + ayarlar ─────────────────────────

@router.get("/qr")
def kiosk_qr(
    _: dict = Depends(require_permission("pdks", "kiosk")),
):
    """Girişteki ekranın çektiği uç — 30 sn'de bir yenilenen imzalı QR.
    Kiosk cihazı, yalnız `pdks.kiosk` yetkisi olan özel bir hesapla girer.
    (Ekranı olmayan ofisler bunun yerine basılı QR kullanır: /qr/static.)"""
    now = time.time()
    bucket = qr_bucket(now)
    token = make_qr_token(SECRET_KEY, bucket)
    seconds_left = QR_BUCKET_SECONDS - int(now) % QR_BUCKET_SECONDS
    return JSONResponse(
        content={"token": token, "svg": _qr_svg(token),
                 # Kamerası olmayan/okumayan personel için yedek: QR ile aynı
                 # pencerede döner, ayrı HMAC domain'inden türetilir.
                 "code": make_numeric_code(SECRET_KEY, bucket),
                 "seconds_left": seconds_left, "period": QR_BUCKET_SECONDS},
        # Canlı token — ara katman/tarayıcı önbelleğinde tutulmasın
        headers={"Cache-Control": "no-store, max-age=0"},
    )


def _qr_svg(text: str) -> str:
    """QR'ı SVG olarak üret.

    İKİ ŞART birden gerekli, ikisi de sahada yaşandı:
    1. `make_qr()` — `make()` DEĞİL.  make() en küçük sembolü seçer ve kısa
       metinlerde **Micro QR**'a (M1–M4) düşer; Micro QR'ı telefon kameraları
       ve html5-qrcode/OpenCV/jsQR OKUMAZ → basılı afiş taranmaz.  make_qr()
       her zaman normal QR üretir.
    2. `omitsize=True` — yoksa sabit width/height yazılır, viewBox olmaz ve
       sembol kartın içinde kırpılıp okunamaz hale gelir.
    Regresyon: test_qr_svg_is_scalable_not_clipped + test_qr_is_not_micro_qr.
    """
    import io as _io

    import segno

    buf = _io.BytesIO()
    segno.make_qr(text, error="m").save(buf, kind="svg", xmldecl=False,
                                     scale=12, dark="#111827", border=2,
                                     omitsize=True)
    return buf.getvalue().decode("utf-8")


def _static_qr_payload(cfg: dict) -> dict:
    code = cfg.get("static_code") or ""
    if not code:
        return {"exists": False, "code": "", "token": "", "svg": ""}
    token = make_static_token(code)
    return {"exists": True, "code": code, "token": token, "svg": _qr_svg(token)}


@router.get("/qr/static")
def static_qr(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("pdks", "manage")),
):
    """Girişe asılacak BASILI QR (kiosk ekranı olmayan ofisler için).
    İçerik sabittir; yalnız yönetici yenilediğinde değişir."""
    return JSONResponse(content=_static_qr_payload(_checkin_cfg(db)),
                        headers={"Cache-Control": "no-store, max-age=0"})


@router.post("/qr/static/regenerate")
def regenerate_static_qr(
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "manage")),
):
    """Yeni basılı kod üret — ESKİ ÇIKTI ANINDA GEÇERSİZ olur, afiş yeniden
    basılmalı.  Kod kaybolduğunda/sızdığında kullanılır."""
    code = "".join(secrets.choice(STATIC_CODE_ALPHABET) for _ in range(STATIC_CODE_LEN))
    _set_setting(db, _CFG_STATIC_CODE, code)
    try:
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Kod üretilemedi.")
    log_admin_event(db, request, actor=current_user, action="pdks.qr_static.regenerate",
                    target_type="integration", target_name="pdks",
                    details={"not": "yeni basılı QR kodu üretildi, eski çıktı geçersiz"})
    payload = _static_qr_payload(_checkin_cfg(db))
    payload["message"] = "Yeni kod üretildi — afişi yeniden yazdırın."
    return JSONResponse(content=payload,
                        headers={"Cache-Control": "no-store, max-age=0"})


# ─── Unutulan giriş/çıkış bildirimi (personel → yönetici onayı) ─────────────

_REQ_STATUS_LABELS = {"pending": "Onay bekliyor", "approved": "Onaylandı",
                      "rejected": "Reddedildi"}


def _request_view(r: AttendanceRequest, db: Session) -> dict:
    emp = db.query(Employee).filter(Employee.id == r.employee_id).first()
    tr_ts = to_tr(r.ts_utc)
    return {
        "id": r.id,
        "employee_id": r.employee_id,
        "employee_name": emp.full_name if emp else "—",
        "event_type": r.event_type,
        "type_label": _TYPE_LABELS.get(r.event_type, r.event_type),
        "date": r.work_date.isoformat(),
        "date_label": r.work_date.strftime("%d.%m.%Y"),
        "weekday_label": WEEKDAY_LABELS[r.work_date.weekday()],
        "time": tr_ts.strftime("%H:%M") if tr_ts else "",
        "note": r.note,
        "status": r.status,
        "status_label": _REQ_STATUS_LABELS.get(r.status, r.status),
        "created_at": to_tr(r.created_at).strftime("%d.%m.%Y %H:%M") if r.created_at else "",
        "decided_by": r.decided_by or "",
        "decided_at": to_tr(r.decided_at).strftime("%d.%m.%Y %H:%M") if r.decided_at else "",
        "decision_note": r.decision_note or "",
    }


@router.post("/requests", status_code=201)
def create_request(
    data: RequestBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "check")),
):
    """Personel kendi unuttuğu giriş/çıkışı bildirir — ONAYA DÜŞER, puantaja
    doğrudan işlemez.  Ofis ağı/konum/QR şartı ARANMAZ (zaten ofis dışından
    yapılıyor); güvenliği sağlayan şey yöneticinin onayıdır."""
    emp = _employee_for_user(db, current_user)
    if not emp:
        return _err(404, "Personel kaydınız bulunamadı — yöneticinize başvurun.")
    try:
        ts = _tr_wall_to_utc(data.date, data.time)
    except ValueError:
        return _err(400, "Geçersiz saat.")
    now = datetime.utcnow()
    if ts > now + timedelta(minutes=5):
        return _err(400, "Gelecek bir zaman bildirilemez.")
    today = tr_date_of(now)
    if (today - data.date).days > REQUEST_MAX_AGE_DAYS:
        return _err(400, f"En fazla {REQUEST_MAX_AGE_DAYS} gün geriye bildirim "
                         f"yapabilirsiniz — daha eskisi için yöneticinize başvurun.")
    if data.date > today:
        return _err(400, "İleri tarihli bildirim yapılamaz.")
    dup = (db.query(AttendanceRequest)
           .filter(AttendanceRequest.employee_id == emp.id,
                   AttendanceRequest.work_date == data.date,
                   AttendanceRequest.event_type == data.event_type,
                   AttendanceRequest.status == "pending").first())
    if dup:
        return _err(400, "Bu gün için zaten onay bekleyen bir bildiriminiz var.")
    req = AttendanceRequest(
        employee_id=emp.id, event_type=data.event_type, ts_utc=ts,
        work_date=data.date, note=data.note.strip(), status="pending",
    )
    try:
        db.add(req)
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Bildirim kaydedilemedi.")
    log_admin_event(db, request, actor=current_user, action="pdks.request.create",
                    target_type="pdks_request", target_id=req.id,
                    target_name=emp.full_name,
                    details={"tip": _TYPE_LABELS[data.event_type],
                             "zaman": f"{data.date.isoformat()} {data.time}",
                             "not": data.note})
    return {"id": req.id,
            "message": "Bildiriminiz yöneticiye iletildi — onaylanınca puantaja işlenir."}


@router.get("/requests/mine")
def my_requests(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "view_own")),
):
    emp = _employee_for_user(db, current_user)
    if not emp:
        return {"requests": []}
    rows = (db.query(AttendanceRequest)
            .filter(AttendanceRequest.employee_id == emp.id)
            .order_by(AttendanceRequest.id.desc()).limit(20).all())
    return {"requests": [_request_view(r, db) for r in rows]}


@router.get("/requests")
def list_requests(
    status: str = "pending",
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("pdks", "view_all")),
):
    q = db.query(AttendanceRequest)
    if status in ("pending", "approved", "rejected"):
        q = q.filter(AttendanceRequest.status == status)
    rows = q.order_by(AttendanceRequest.id.desc()).limit(200).all()
    return {"requests": [_request_view(r, db) for r in rows],
            "pending_count": db.query(AttendanceRequest)
                               .filter(AttendanceRequest.status == "pending").count()}


@router.post("/requests/{request_id}/approve")
def approve_request(
    request_id: int,
    data: DecisionBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "manage")),
):
    """Onay → GERÇEK olay yazılır (source='request').  Manuel girişle aynı
    yoldan geçer: work_date kuralı + komşu çıkışların yeniden senkronu."""
    req = (db.query(AttendanceRequest)
           .filter(AttendanceRequest.id == request_id,
                   AttendanceRequest.status == "pending").first())
    if not req:
        return _err(404, "Bekleyen bildirim bulunamadı.")
    emp = _get_employee(db, req.employee_id)
    if not emp:
        return _err(404, "Personel bulunamadı.")
    _lock_employee(db, emp.id)
    ev = AttendanceEvent(
        employee_id=emp.id, event_type=req.event_type, ts_utc=req.ts_utc,
        work_date=_work_date_for(db, emp.id, req.event_type, req.ts_utc),
        source="request", created_by_user_id=_uid(current_user) or None,
        correction_note=f"Personel bildirimi: {req.note}",
    )
    req.status = "approved"
    req.decided_by = _actor(current_user)
    req.decided_at = datetime.utcnow()
    req.decision_note = (data.note or "").strip() or None
    try:
        db.add(ev)
        db.flush()
        req.event_id = ev.id
        _resync_out_work_dates(db, emp.id, [req.ts_utc])
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Onay kaydedilemedi.")
    log_admin_event(db, request, actor=current_user, action="pdks.request.approve",
                    target_type="pdks_request", target_id=req.id,
                    target_name=emp.full_name,
                    details={"tip": _TYPE_LABELS[req.event_type],
                             "zaman": to_tr(req.ts_utc).strftime("%d.%m.%Y %H:%M"),
                             "olay_id": ev.id})
    return {"id": req.id, "event_id": ev.id, "message": "Bildirim onaylandı ve puantaja işlendi."}


@router.post("/requests/{request_id}/reject")
def reject_request(
    request_id: int,
    data: DecisionBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "manage")),
):
    req = (db.query(AttendanceRequest)
           .filter(AttendanceRequest.id == request_id,
                   AttendanceRequest.status == "pending").first())
    if not req:
        return _err(404, "Bekleyen bildirim bulunamadı.")
    req.status = "rejected"
    req.decided_by = _actor(current_user)
    req.decided_at = datetime.utcnow()
    req.decision_note = (data.note or "").strip() or None
    try:
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "İşlem kaydedilemedi.")
    log_admin_event(db, request, actor=current_user, action="pdks.request.reject",
                    target_type="pdks_request", target_id=req.id,
                    details={"not": req.decision_note})
    return {"id": req.id, "message": "Bildirim reddedildi."}


@router.get("/checkin-config")
def get_checkin_config(
    request: Request,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("pdks", "manage")),
):
    cfg = _checkin_cfg(db)
    cfg["detected_ip"] = _real_client_ip(request)
    return cfg


@router.put("/checkin-config")
def update_checkin_config(
    data: CheckinConfigBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("pdks", "manage")),
):
    cur = _checkin_cfg(db)
    new_enforce = cur["enforce"] if data.enforce is None else data.enforce
    new_ips = cur["allowed_ips"] if data.allowed_ips is None else data.allowed_ips
    new_lat = cur["lat"] if data.lat is None else data.lat
    new_lon = cur["lon"] if data.lon is None else data.lon
    new_mode = cur["qr_mode"] if data.qr_mode is None else data.qr_mode
    if new_enforce and (not (new_ips or "").strip() or new_lat is None or new_lon is None):
        return _err(400, "Önce ofis IP ve konum ayarlarını kaydedin — "
                         "doğrulama ondan sonra açılabilir.")
    if new_enforce and new_mode == "static" and not cur["static_code"]:
        return _err(400, "Önce basılı QR kodunu oluşturup afişi yazdırın — "
                         "doğrulama ondan sonra açılabilir.")
    if data.qr_mode is not None:
        _set_setting(db, _CFG_QR_MODE, data.qr_mode)
    if data.enforce is not None:
        _set_setting(db, _CFG_ENFORCE, "true" if data.enforce else "false")
    if data.allowed_ips is not None:
        _set_setting(db, _CFG_IPS, data.allowed_ips.strip())
    if data.lat is not None:
        _set_setting(db, _CFG_LAT, str(data.lat))
    if data.lon is not None:
        _set_setting(db, _CFG_LON, str(data.lon))
    if data.radius_m is not None:
        _set_setting(db, _CFG_RADIUS, str(data.radius_m))
    try:
        db.commit()
    except Exception:
        db.rollback()
        return _err(500, "Ayarlar kaydedilemedi.")
    log_admin_event(db, request, actor=current_user, action="pdks.checkin_config",
                    target_type="integration", target_name="pdks",
                    details={"enforce": data.enforce, "allowed_ips": data.allowed_ips,
                             "lat": data.lat, "lon": data.lon, "radius_m": data.radius_m,
                             "qr_mode": data.qr_mode})
    result = _checkin_cfg(db)
    result["detected_ip"] = _real_client_ip(request)
    return result


# ─── Aylık rapor ─────────────────────────────────────────────────────────────

def _report_data(db: Session, year: int, month: int,
                 employee_id: Optional[int] = None) -> dict:
    """Ay raporu: aktif personel + o ayda olayı/izni olan pasif (ayrılmış)
    personel.  İşten ayrılanın son ay puantajı bordro için tam da gereken
    rapordur — pasifleştirme onu görünmez yapmamalı."""
    from calendar import monthrange
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    q = db.query(Employee)
    if employee_id:
        q = q.filter(Employee.id == employee_id)
    else:
        ev_ids = {r[0] for r in
                  (db.query(AttendanceEvent.employee_id)
                   .filter(AttendanceEvent.is_active == True,              # noqa: E712
                           AttendanceEvent.work_date >= start,
                           AttendanceEvent.work_date <= end).distinct())}
        lv_ids = {r[0] for r in
                  (db.query(LeaveRecord.employee_id)
                   .filter(LeaveRecord.is_active == True,                  # noqa: E712
                           LeaveRecord.start_date <= end,
                           LeaveRecord.end_date >= start).distinct())}
        q = q.filter(or_(Employee.is_active == True,                       # noqa: E712
                         Employee.id.in_(sorted(ev_ids | lv_ids) or [0])))
    employees = q.order_by(Employee.full_name).all()
    return {
        "year": year, "month": month,
        "month_label": f"{MONTH_LABELS[month]} {year}",
        "employees": [{
            "employee": _employee_view(e, db),
            "month": _month_payload(db, e, year, month),
        } for e in employees],
    }


@router.get("/report")
def monthly_report(
    year: int,
    month: int,
    employee_id: int = None,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("pdks", "report")),
):
    if not _valid_month(year, month):
        return _err(400, "Geçersiz yıl/ay.")
    return _report_data(db, year, month, employee_id)


@router.get("/report/excel")
def monthly_report_excel(
    year: int,
    month: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("pdks", "report")),
):
    if not _valid_month(year, month):
        return _err(400, "Geçersiz yıl/ay.")
    import io
    from core.delivery_note import content_disposition
    from core.pdks_report import puantaj_filename, render_puantaj_excel
    data = _report_data(db, year, month)
    try:
        content = render_puantaj_excel(data)
    except Exception:
        return _err(500, "Rapor üretilemedi.")
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": content_disposition(puantaj_filename(year, month))},
    )
