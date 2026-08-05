"""PDKS modülü testleri — hesap motoru (saf unit) + API (CRUD/RBAC/rapor).

Saf testler core/pdks fonksiyonlarını DB'siz sınar; API testleri conftest
fixture'larını kullanır.  TR duvar saati → UTC çevirisi: utc = tr - 3 saat.
"""
import json
import time
from datetime import date, datetime, timedelta

import pytest

from core.auth import SECRET_KEY
from core.pdks import (
    NUMERIC_CODE_LEN, compute_day, compute_month, geo_within, haversine_m,
    ip_allowed, make_numeric_code, make_qr_token, pair_events, qr_bucket,
    schedule_for, tr_date_of, verify_numeric_code, verify_qr_token,
)
from database import (
    AdminAuditLog, AppSetting, AttendanceEvent, Employee, EmployeeSchedule,
    LeaveRecord, User,
)

ORIGIN = {"Origin": "http://testserver"}

# Pzt–Cum 09:00–18:00 — testlerin standart programı.  Molalar artık programa
# DEĞİL şirket geneli sabit şemaya bağlı (BREAKS: 09:30/15 · 12:45/45 · 16:00/15);
# bu pencerede üçü de tam kapsanır → 75 dk, beklenen 540 − 75 = 465 dk.
STD_TEMPLATE = {str(i): {"start": "09:00", "end": "18:00"} for i in range(5)}
STD_TEMPLATE.update({"5": None, "6": None})
STD_SCHEDULES = [{"effective_from": date(2026, 1, 1),
                  "template": STD_TEMPLATE, "lunch_break_minutes": 60}]

MONDAY = date(2026, 7, 6)      # Pazartesi
SATURDAY = date(2026, 7, 11)   # Cumartesi


def tr(y, m, d, hh, mm=0):
    """TR duvar saati → naif UTC datetime."""
    return datetime(y, m, d, hh, mm) - timedelta(hours=3)


def ev(event_type, ts):
    return {"id": None, "event_type": event_type, "ts_utc": ts, "source": "self"}


def std_day(events, work_date=MONDAY, leave_type=None, holiday=None):
    return compute_day(work_date, events,
                       schedule_for(STD_SCHEDULES, work_date),
                       leave_type=leave_type, holiday=holiday)


# ─── Saf unit: hesap motoru ─────────────────────────────────────────────────

def test_tr_date_of_midnight():
    # 22:30Z = TR 01:30 (ertesi gün)
    assert tr_date_of(datetime(2026, 7, 6, 22, 30)) == date(2026, 7, 7)
    assert tr_date_of(datetime(2026, 7, 6, 17, 0)) == date(2026, 7, 6)


def test_overtime_single_pair():
    # 09:00–20:00 → brüt 660 − 75 dk mola = 585; beklenen 465; mesai 120
    d = std_day([ev("in", tr(2026, 7, 6, 9)), ev("out", tr(2026, 7, 6, 20))])
    assert d["worked_minutes"] == 585
    assert d["expected_minutes"] == 465
    assert d["overtime_minutes"] == 120
    assert d["break_deducted"] is True and d["break_minutes"] == 75
    assert d["status"] == "calisti"


def test_no_late_early_flags():
    """Geç/erken bayrağı KALDIRILDI (bilinçli): saat kaydedilir, kimse
    'geç geldi' diye işaretlenmez — yalnız süre ve eksik görünür."""
    d = std_day([ev("in", tr(2026, 7, 6, 9, 20)), ev("out", tr(2026, 7, 6, 17, 30))])
    assert "late_minutes" not in d and "early_leave_minutes" not in d
    # brüt 490 − 75 dk mola = 415; eksik = 465 − 415 = 50
    assert d["worked_minutes"] == 415
    assert d["missing_minutes"] == 50


def test_break_rules():
    # İki çift = personel molada çıkış basmış → ikinci kez kesinti YOK
    d = std_day([ev("in", tr(2026, 7, 6, 9)), ev("out", tr(2026, 7, 6, 13)),
                 ev("in", tr(2026, 7, 6, 14)), ev("out", tr(2026, 7, 6, 18))])
    assert d["worked_minutes"] == 480
    assert d["break_deducted"] is False and d["break_minutes"] == 0
    assert d["overtime_minutes"] == 15          # 480 − 465
    # Tek çift, kısa gün: yalnız PENCEREYE DÜŞEN molalar düşülür.
    # 09:00–14:00 → kahvaltı 15 + öğle 45 (12:45–13:30 tamamı) = 60;
    # 16:00 molası pencere dışında kaldı (tam günün 75'i düşmedi)
    d2 = std_day([ev("in", tr(2026, 7, 6, 9)), ev("out", tr(2026, 7, 6, 14))])
    assert d2["break_minutes"] == 60
    assert d2["worked_minutes"] == 240
    # Kısmi kesişim: 09:00–13:00 → kahvaltı 15 + öğlenin ilk 15 dk'sı = 30
    d2b = std_day([ev("in", tr(2026, 7, 6, 9)), ev("out", tr(2026, 7, 6, 13))])
    assert d2b["break_minutes"] == 30
    # Hiçbir molaya değmeyen kısa çalışma → kesinti yok
    d3 = std_day([ev("in", tr(2026, 7, 6, 10)), ev("out", tr(2026, 7, 6, 12))])
    assert d3["break_minutes"] == 0 and d3["worked_minutes"] == 120


def test_standard_workday_nets_eight_hours():
    """Şirketin gerçek programı: 08:30–17:45, 75 dk mola → tam 8 saat net."""
    from core.pdks import (BREAKS, DEFAULT_WORK_END, DEFAULT_WORK_START,
                           TOTAL_BREAK_MINUTES, break_minutes_within)
    assert (DEFAULT_WORK_START, DEFAULT_WORK_END) == ("08:30", "17:45")
    assert TOTAL_BREAK_MINUTES == 75
    assert break_minutes_within(DEFAULT_WORK_START, DEFAULT_WORK_END) == 75

    tpl = {str(i): {"start": DEFAULT_WORK_START, "end": DEFAULT_WORK_END} for i in range(5)}
    tpl.update({"5": None, "6": None})
    scheds = [{"effective_from": date(2026, 1, 1), "template": tpl}]
    s = schedule_for(scheds, MONDAY)
    assert s["span_minutes"] == 555 and s["break_minutes"] == 75
    d = compute_day(MONDAY,
                    [ev("in", tr(2026, 7, 6, 8, 30)), ev("out", tr(2026, 7, 6, 17, 45))],
                    s)
    assert d["expected_minutes"] == 480          # 8 saat
    assert d["worked_minutes"] == 480            # tam mesai → eksik/mesai yok
    assert d["missing_minutes"] == 0 and d["overtime_minutes"] == 0
    # Molaların hepsi mesai penceresinin İÇİNDE olmalı (şema tutarlılığı)
    for b in BREAKS:
        assert DEFAULT_WORK_START < b["start"] < DEFAULT_WORK_END


def test_break_window_intersection_edges():
    from core.pdks import break_minutes_within
    # Molaya bitişik ama değmiyor (09:45 kahvaltının bitişi)
    assert break_minutes_within("09:45", "12:45") == 0
    # Tek molanın tamamı
    assert break_minutes_within("16:00", "16:15") == 15
    # Molanın yalnız yarısı
    assert break_minutes_within("13:00", "13:15") == 15      # öğlenin ortası
    # Ters/boş pencere (gece vardiyası) → şema uygulanmaz
    assert break_minutes_within("20:00", "01:00") == 0
    assert break_minutes_within("10:00", "10:00") == 0
    # Tüm günü kapsayan pencere → hepsi
    assert break_minutes_within("00:00", "23:59") == 75


def test_midnight_crossing_pair():
    # Giriş 20:00 TR, çıkış ertesi gün 01:30 TR — aynı work_date'e yazılmış
    d = std_day([ev("in", tr(2026, 7, 6, 20)), ev("out", tr(2026, 7, 7, 1, 30))])
    assert d["worked_minutes"] == 330          # 5,5 saat, kesintisiz (<6 sa)
    assert d["status"] == "calisti"


def test_weekend_work_is_all_overtime():
    d = std_day([ev("in", tr(2026, 7, 11, 10)), ev("out", tr(2026, 7, 11, 14))],
                work_date=SATURDAY)
    assert d["expected_minutes"] == 0
    assert d["worked_minutes"] == 240
    assert d["overtime_minutes"] == 240
    assert d["status"] == "hafta_tatili"


def test_holiday_full_and_half():
    hol = {"name": "Kurban Bayramı", "is_half_day": False}
    d = std_day([ev("in", tr(2026, 7, 6, 9)), ev("out", tr(2026, 7, 6, 13))],
                holiday=hol)
    assert d["status"] == "resmi_tatil"
    assert d["expected_minutes"] == 0
    # Tatilde 09:00–13:00: kahvaltı 15 + öğlenin ilk 15 dk'sı = 30 düşülür
    assert d["overtime_minutes"] == 210
    # Yarım gün (arefe): beklenen = (540 − 75) // 2 = 232
    half = {"name": "Arefe", "is_half_day": True}
    d2 = std_day([], holiday=half)
    assert d2["expected_minutes"] == 232
    assert d2["status"] == "resmi_tatil"


def test_missing_checkout():
    d = std_day([ev("in", tr(2026, 7, 6, 9))])
    assert d["missing_checkout"] is True
    assert d["worked_minutes"] == 0
    assert d["status"] == "eksik_cikis"
    assert d["missing_minutes"] == 0           # gün toplam dışı — eksik üretmez


def test_leave_overlay():
    d = std_day([], leave_type="yillik")
    assert d["status"] == "izinli"
    assert d["expected_minutes"] == 0
    d2 = std_day([])
    assert d2["status"] == "devamsiz"


def test_schedule_versioning():
    schedules = STD_SCHEDULES + [{
        "effective_from": date(2026, 7, 15),
        "template": {str(i): {"start": "10:00", "end": "16:00"} for i in range(5)},
        "lunch_break_minutes": 30,     # artık YOK SAYILIR (sabit şema geçerli)
    }]
    assert schedule_for(schedules, date(2026, 7, 10))["start"] == "09:00"
    s = schedule_for(schedules, date(2026, 7, 20))
    # 10:00–16:00 penceresi: öğle 45 dk tam içinde, kahvaltı/16:00 molası dışında
    assert s["start"] == "10:00" and s["break_minutes"] == 45


def test_pair_events_orphans():
    pairs = pair_events([ev("out", tr(2026, 7, 6, 9)),
                         ev("in", tr(2026, 7, 6, 10)),
                         ev("in", tr(2026, 7, 6, 12)),
                         ev("out", tr(2026, 7, 6, 18))])
    # yetim out + açık kalan ilk in + kapalı (12→18) çifti
    assert len(pairs) == 3
    assert pairs[0]["in"] is None
    assert pairs[1]["out"] is None
    assert pairs[2]["minutes"] == 360


def test_compute_month_leave_counts_workdays_only():
    # Cumartesi–Pazartesi izin: sayaç yalnız Pazartesi'yi (iş günü) sayar,
    # görüntü önceliği yine 'izinli'.
    leaves = [{"leave_type": "yillik",
               "start_date": date(2026, 7, 11), "end_date": date(2026, 7, 13)}]
    m = compute_month(2026, 7, STD_SCHEDULES, {}, leaves, {}, date(2026, 7, 31))
    assert m["totals"]["izin_gunleri"]["yillik"] == 1
    by_date = {d["date"]: d for d in m["days"]}
    assert by_date[date(2026, 7, 11)]["status"] == "izinli"
    assert by_date[date(2026, 7, 13)]["status"] == "izinli"
    assert by_date[date(2026, 7, 14)]["status"] == "devamsiz"


def test_compute_month_future_days_pending():
    m = compute_month(2026, 7, STD_SCHEDULES, {}, [], {}, date(2026, 7, 10))
    by_date = {d["date"]: d for d in m["days"]}
    assert by_date[date(2026, 7, 20)]["status"] == "bekliyor"
    # bekliyor günler toplamlara girmez → devamsızlık yalnız 1–10 iş günleri
    assert m["totals"]["devamsizlik_gun"] == 8   # 1–10 Tem 2026: 8 iş günü


# ─── API yardımcıları ───────────────────────────────────────────────────────

def _mk_user(db, username, role="Staff", full_name=None):
    import bcrypt
    u = User(username=username,
             password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode(),
             full_name=full_name or username.title(), role=role, is_active=True)
    db.add(u)
    db.commit()
    return u


def _mk_employee(db, user_id=None, name="Ali Çalışkan"):
    e = Employee(full_name=name, title="Laborant", user_id=user_id,
                 start_date=date(2026, 1, 5))
    db.add(e)
    db.commit()
    return e


def _mk_schedule(db, employee_id, eff=date(2026, 1, 1)):
    s = EmployeeSchedule(employee_id=employee_id, effective_from=eff,
                         weekly_template=json.dumps(STD_TEMPLATE),
                         lunch_break_minutes=60, created_by="test")
    db.add(s)
    db.commit()
    return s


@pytest.fixture
def staff_employee_client(client, db_session):
    """Staff rolünde, Employee kaydı + programı olan kullanıcıyla login'li client."""
    u = _mk_user(db_session, "personel1", full_name="Ali Çalışkan")
    e = _mk_employee(db_session, user_id=u.id)
    _mk_schedule(db_session, e.id)
    r = client.post("/api/login",
                    json={"username": "personel1", "password": "minerva123"},
                    headers=ORIGIN)
    assert r.status_code == 200, f"Login başarısız: {r.text}"
    return client, e.id


# ─── API: sayfa + check akışı ───────────────────────────────────────────────

def test_page_renders_for_staff(staff_employee_client):
    c, _ = staff_employee_client
    r = c.get("/pdks")
    assert r.status_code == 200
    assert "PDKS" in r.text


def test_page_redirects_anonymous(client):
    r = client.get("/pdks", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_check_flow(staff_employee_client, db_session):
    c, emp_id = staff_employee_client
    # Açık giriş yokken çıkış → 400
    r = c.post("/api/pdks/check", json={"type": "out"}, headers=ORIGIN)
    assert r.status_code == 400
    # Giriş
    r = c.post("/api/pdks/check", json={"type": "in"}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "iceride"
    assert r.json()["message"] == "Giriş kaydedildi."
    # Çift tık — ikinci 'in' idempotent, olay sayısı artmaz
    r = c.post("/api/pdks/check", json={"type": "in"}, headers=ORIGIN)
    assert r.status_code == 200
    assert r.json()["message"] == "Zaten kaydedildi."
    n = db_session.query(AttendanceEvent).filter_by(employee_id=emp_id).count()
    assert n == 1
    row = db_session.query(AttendanceEvent).filter_by(employee_id=emp_id).first()
    assert row.source == "self"
    assert row.work_date == tr_date_of(datetime.utcnow())
    # Çıkış → dışarıda
    r = c.post("/api/pdks/check", json={"type": "out"}, headers=ORIGIN)
    assert r.status_code == 200
    assert r.json()["state"] == "disarida"


def test_check_without_employee_record(labtech_client):
    r = labtech_client.post("/api/pdks/check", json={"type": "in"}, headers=ORIGIN)
    assert r.status_code == 404
    assert "Personel kaydınız" in r.json()["detail"]


def test_me_today_and_month(staff_employee_client):
    c, _ = staff_employee_client
    r = c.get("/api/pdks/me/today")
    assert r.status_code == 200
    assert r.json()["state"] == "disarida"
    now_tr = datetime.utcnow() + timedelta(hours=3)
    r = c.get(f"/api/pdks/me/month?year={now_tr.year}&month={now_tr.month}")
    assert r.status_code == 200
    assert len(r.json()["days"]) >= 28


# ─── API: RBAC ──────────────────────────────────────────────────────────────

def test_rbac_staff_cannot_manage_or_report(staff_employee_client):
    c, emp_id = staff_employee_client
    assert c.get("/api/pdks/report?year=2026&month=7").status_code == 403
    assert c.get("/api/pdks/day").status_code == 403
    assert c.get("/api/pdks/employees").status_code == 403
    r = c.post("/api/pdks/events",
               json={"employee_id": emp_id, "event_type": "in",
                     "date": "2026-07-06", "time": "09:00", "note": "deneme"},
               headers=ORIGIN)
    assert r.status_code == 403


def test_rbac_superadmin_full_access(authed_client):
    assert authed_client.get("/api/pdks/employees").status_code == 200
    assert authed_client.get("/api/pdks/day").status_code == 200
    assert authed_client.get("/api/pdks/report?year=2026&month=7").status_code == 200


# ─── API: personel + program + manuel düzeltme ──────────────────────────────

def test_employee_crud_and_schedule(authed_client, db_session):
    r = authed_client.post("/api/pdks/employees",
                           json={"full_name": "Veli Işgüzar", "title": "Depocu"},
                           headers=ORIGIN)
    assert r.status_code == 201, r.text
    emp_id = r.json()["id"]
    r = authed_client.put(f"/api/pdks/employees/{emp_id}/schedule",
                          json={"effective_from": "2026-07-01",
                                "weekly_template": STD_TEMPLATE,
                                "lunch_break_minutes": 45},
                          headers=ORIGIN)
    assert r.status_code == 200, r.text
    r = authed_client.get(f"/api/pdks/employees/{emp_id}")
    assert r.status_code == 200
    assert len(r.json()["schedules"]) == 1
    # Geçersiz şablon → 400
    r = authed_client.put(f"/api/pdks/employees/{emp_id}/schedule",
                          json={"effective_from": "2026-08-01",
                                "weekly_template": {"0": {"start": "18:00", "end": "09:00"}},
                                "lunch_break_minutes": 60},
                          headers=ORIGIN)
    assert r.status_code == 400


def test_manual_event_correction_with_audit(authed_client, db_session):
    e = _mk_employee(db_session, name="Düzeltme Testi")
    r = authed_client.post("/api/pdks/events",
                           json={"employee_id": e.id, "event_type": "in",
                                 "date": "2026-07-06", "time": "09:00",
                                 "note": "kağıt imzadan aktarıldı"},
                           headers=ORIGIN)
    assert r.status_code == 201, r.text
    ev_id = r.json()["id"]
    row = db_session.query(AttendanceEvent).get(ev_id)
    assert row.source == "manual"
    # TR 09:00 → UTC 06:00, work_date = aynı gün
    assert row.ts_utc == datetime(2026, 7, 6, 6, 0)
    assert row.work_date == date(2026, 7, 6)
    # PUT — düzeltme izi
    r = authed_client.put(f"/api/pdks/events/{ev_id}",
                          json={"date": "2026-07-06", "time": "09:30",
                                "note": "saat düzeltildi"},
                          headers=ORIGIN)
    assert r.status_code == 200
    db_session.expire_all()
    row = db_session.query(AttendanceEvent).get(ev_id)
    assert row.corrected_by and row.correction_note == "saat düzeltildi"
    # DELETE — soft + gerekçe zorunlu
    r = authed_client.delete(f"/api/pdks/events/{ev_id}", headers=ORIGIN)
    assert r.status_code == 422          # note parametresi eksik
    r = authed_client.delete(f"/api/pdks/events/{ev_id}?note=hatalı kayıt",
                             headers=ORIGIN)
    assert r.status_code == 200
    db_session.expire_all()
    assert db_session.query(AttendanceEvent).get(ev_id).is_active is False
    # Audit satırları
    actions = {a.action for a in db_session.query(AdminAuditLog).all()}
    assert {"pdks.event.create", "pdks.event.update", "pdks.event.delete"} <= actions


def test_manual_out_closes_open_in_across_midnight(authed_client, db_session):
    e = _mk_employee(db_session, name="Gece Vardiyası")
    for payload in (
        {"employee_id": e.id, "event_type": "in", "date": "2026-07-06",
         "time": "20:00", "note": "manuel giriş"},
        {"employee_id": e.id, "event_type": "out", "date": "2026-07-07",
         "time": "01:30", "note": "manuel çıkış"},
    ):
        r = authed_client.post("/api/pdks/events", json=payload, headers=ORIGIN)
        assert r.status_code == 201, r.text
    rows = (db_session.query(AttendanceEvent)
            .filter_by(employee_id=e.id).order_by(AttendanceEvent.ts_utc).all())
    # Çıkış, açık girişin gününe (6 Temmuz) yazılır
    assert rows[0].work_date == date(2026, 7, 6)
    assert rows[1].work_date == date(2026, 7, 6)


# ─── API: izin + tatil ──────────────────────────────────────────────────────

def test_leave_crud_and_overlap(authed_client, db_session):
    e = _mk_employee(db_session, name="İzinli Personel")
    r = authed_client.post("/api/pdks/leaves",
                           json={"employee_id": e.id, "leave_type": "yillik",
                                 "start_date": "2026-07-01", "end_date": "2026-07-05"},
                           headers=ORIGIN)
    assert r.status_code == 201, r.text
    # Çakışan aralık → 400
    r = authed_client.post("/api/pdks/leaves",
                           json={"employee_id": e.id, "leave_type": "raporlu",
                                 "start_date": "2026-07-04", "end_date": "2026-07-10"},
                           headers=ORIGIN)
    assert r.status_code == 400
    # 'diger' not olmadan → 400
    r = authed_client.post("/api/pdks/leaves",
                           json={"employee_id": e.id, "leave_type": "diger",
                                 "start_date": "2026-08-01", "end_date": "2026-08-01"},
                           headers=ORIGIN)
    assert r.status_code == 400


def test_holiday_unique(authed_client):
    body = {"holiday_date": "2026-10-29", "name": "Cumhuriyet Bayramı"}
    assert authed_client.post("/api/pdks/holidays", json=body,
                              headers=ORIGIN).status_code == 201
    assert authed_client.post("/api/pdks/holidays", json=body,
                              headers=ORIGIN).status_code == 400


# ─── API: rapor + Excel ─────────────────────────────────────────────────────

def _report_fixture(db):
    e = _mk_employee(db, name="Rapor Personeli")
    _mk_schedule(db, e.id)
    # Pazartesi 09:00–18:00 çalışma + Salı yıllık izin
    db.add(AttendanceEvent(employee_id=e.id, event_type="in",
                           ts_utc=datetime(2026, 7, 6, 6, 0),
                           work_date=date(2026, 7, 6), source="manual"))
    db.add(AttendanceEvent(employee_id=e.id, event_type="out",
                           ts_utc=datetime(2026, 7, 6, 15, 0),
                           work_date=date(2026, 7, 6), source="manual"))
    db.add(LeaveRecord(employee_id=e.id, leave_type="yillik",
                       start_date=date(2026, 7, 7), end_date=date(2026, 7, 7)))
    db.commit()
    return e


def test_report_json_with_leave(authed_client, db_session):
    e = _report_fixture(db_session)
    r = authed_client.get(f"/api/pdks/report?year=2026&month=7&employee_id={e.id}")
    assert r.status_code == 200
    month = r.json()["employees"][0]["month"]
    by_date = {d["date"]: d for d in month["days"]}
    day6 = by_date["2026-07-06"]
    assert day6["status"] == "calisti"
    assert day6["worked_minutes"] == 465       # 9 sa − 75 dk mola
    day7 = by_date["2026-07-07"]
    assert day7["status"] == "izinli"
    assert day7["status_label"] == "Yıllık İzin"
    assert month["totals"]["izin_gunleri"]["yillik"] == 1


# ─── İnceleme bulgusu regresyonları ─────────────────────────────────────────

def test_half_day_no_late_early():
    # Yarım gün tatilde beklenen süre yarıya iner; dolduran personel
    # "eksik" görünmemeli.
    half = {"name": "Arefe", "is_half_day": True}
    # 09:00–14:00 → brüt 300 − 60 mola (kahvaltı 15 + öğle 45) = 240 net;
    # yarım gün beklenen (540 − 75) // 2 = 232 → dolduruldu
    d = std_day([ev("in", tr(2026, 7, 6, 9)), ev("out", tr(2026, 7, 6, 14))],
                holiday=half)
    assert d["expected_minutes"] == 232
    assert d["worked_minutes"] == 240
    assert d["missing_minutes"] == 0


def test_leave_overlapping_holiday_not_counted():
    # Yıllık izne rastlayan tam resmi tatil izin hakkından düşmez (İK m.56).
    leaves = [{"leave_type": "yillik",
               "start_date": date(2026, 7, 13), "end_date": date(2026, 7, 17)}]
    holidays = {date(2026, 7, 15): {"name": "Tatil", "is_half_day": False}}
    m = compute_month(2026, 7, STD_SCHEDULES, {}, leaves, holidays, date(2026, 7, 31))
    assert m["totals"]["izin_gunleri"]["yillik"] == 4
    by_date = {d["date"]: d for d in m["days"]}
    assert by_date[date(2026, 7, 15)]["status"] == "resmi_tatil"


def test_future_manual_event_rejected(authed_client, db_session):
    e = _mk_employee(db_session, name="Gelecek Test")
    future = (datetime.utcnow() + timedelta(days=2)).date()
    r = authed_client.post("/api/pdks/events",
                           json={"employee_id": e.id, "event_type": "in",
                                 "date": future.isoformat(), "time": "09:00",
                                 "note": "yanlış tarih"},
                           headers=ORIGIN)
    assert r.status_code == 400
    assert "Gelecek" in r.json()["detail"]


def test_edit_paired_in_resyncs_out_work_date(authed_client, db_session):
    # Gece vardiyası çiftinin 'in'i ertesi güne taşınınca partner 'out' da
    # taşınmalı — aksi halde çift bölünür, çalışılan süre kaybolur.
    e = _mk_employee(db_session, name="Resync Test")
    for payload in (
        {"employee_id": e.id, "event_type": "in", "date": "2026-07-06",
         "time": "20:00", "note": "manuel giriş"},
        {"employee_id": e.id, "event_type": "out", "date": "2026-07-07",
         "time": "01:30", "note": "manuel çıkış"},
    ):
        assert authed_client.post("/api/pdks/events", json=payload,
                                  headers=ORIGIN).status_code == 201
    rows = (db_session.query(AttendanceEvent)
            .filter_by(employee_id=e.id).order_by(AttendanceEvent.ts_utc).all())
    in_id = rows[0].id
    r = authed_client.put(f"/api/pdks/events/{in_id}",
                          json={"date": "2026-07-07", "time": "00:30",
                                "note": "gün düzeltme"},
                          headers=ORIGIN)
    assert r.status_code == 200, r.text
    db_session.expire_all()
    rows = (db_session.query(AttendanceEvent)
            .filter_by(employee_id=e.id).order_by(AttendanceEvent.ts_utc).all())
    assert rows[0].work_date == date(2026, 7, 7)
    assert rows[1].work_date == date(2026, 7, 7)   # out resync'lendi


def test_inactive_user_gets_401(staff_employee_client, db_session):
    # Pasifleştirilen kullanıcının hâlâ geçerli JWT'si API'ye erişememeli
    # (require_permission auth bypass regresyonu).
    c, _ = staff_employee_client
    u = db_session.query(User).filter_by(username="personel1").first()
    u.is_active = False
    db_session.commit()
    assert c.get("/api/pdks/me/today").status_code == 401
    r = c.post("/api/pdks/check", json={"type": "in"}, headers=ORIGIN)
    assert r.status_code == 401


def test_inactive_employee_in_past_report(authed_client, db_session):
    # İşten ayrılan (pasifleştirilen) personel, olayının olduğu ayın
    # raporundan kaybolmamalı; Excel'de "(ayrıldı)" işaretlenmeli.
    import io
    from openpyxl import load_workbook
    e = _report_fixture(db_session)
    assert authed_client.delete(f"/api/pdks/employees/{e.id}",
                                headers=ORIGIN).status_code == 200
    r = authed_client.get("/api/pdks/report?year=2026&month=7")
    names = [x["employee"]["full_name"] for x in r.json()["employees"]]
    assert "Rapor Personeli" in names
    r = authed_client.get("/api/pdks/report/excel?year=2026&month=7")
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content))
    vals = [v for row in wb["Özet"].iter_rows(values_only=True) for v in row]
    assert any(isinstance(v, str) and "(ayrıldı)" in v for v in vals)


def test_holiday_soft_delete_and_readd(authed_client, db_session):
    from database import PublicHoliday
    body = {"holiday_date": "2026-08-30", "name": "Zafer Bayramı"}
    r = authed_client.post("/api/pdks/holidays", json=body, headers=ORIGIN)
    assert r.status_code == 201
    hid = r.json()["id"]
    assert authed_client.delete(f"/api/pdks/holidays/{hid}",
                                headers=ORIGIN).status_code == 200
    row = db_session.query(PublicHoliday).get(hid)
    assert row is not None and row.is_active is False    # soft delete — iz kalır
    # listede görünmez, aynı tarihe yeniden eklenebilir
    r = authed_client.get("/api/pdks/holidays?year=2026")
    assert all(h["id"] != hid for h in r.json()["holidays"])
    assert authed_client.post("/api/pdks/holidays", json=body,
                              headers=ORIGIN).status_code == 201


def test_report_excel(authed_client, db_session):
    import io
    from openpyxl import load_workbook
    _report_fixture(db_session)
    r = authed_client.get("/api/pdks/report/excel?year=2026&month=7")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    wb = load_workbook(io.BytesIO(r.content))
    assert "Özet" in wb.sheetnames
    assert "Rapor Personeli" in wb.sheetnames
    ws = wb["Özet"]
    assert ws["A1"].value.startswith("Puantaj")


# ═══ Üçlü doğrulama: ofis ağı (IP) + konum + canlı QR ═══════════════════════

# ─── Saf unit ───────────────────────────────────────────────────────────────

def test_haversine():
    assert haversine_m(41.0, 29.0, 41.0, 29.0) == 0.0
    # İstanbul ↔ Ankara ≈ 350 km (%2 tolerans)
    d = haversine_m(41.0082, 28.9784, 39.9334, 32.8597)
    assert 340_000 < d < 360_000
    # 0.001° enlem ≈ 111 m
    assert 100 < haversine_m(41.0, 29.0, 41.001, 29.0) < 120


def test_geo_within_accuracy_cap():
    assert geo_within(100, 150, None) is True          # yarıçap içinde
    assert geo_within(200, 150, None) is False         # dışında, tolerans yok
    assert geo_within(200, 150, 60) is True            # GPS belirsizliği kurtarır
    assert geo_within(260, 150, 60) is False           # belirsizlik yetmiyor
    # accuracy 100 m'de sınırlanır: 150+500 değil 150+100
    assert geo_within(300, 150, 500) is False
    assert geo_within(240, 150, 500) is True


def test_ip_allowed_forms():
    assert ip_allowed("85.105.20.31", "85.105.20.31") is True
    assert ip_allowed("85.105.20.32", "85.105.20.31") is False
    assert ip_allowed("78.180.4.9", "85.105.20.31, 78.180.") is True   # prefiks
    assert ip_allowed("78.181.4.9", "78.180.") is False
    assert ip_allowed("1.2.3.4", "") is False          # liste boş → asla geçme
    assert ip_allowed("", "1.2.3.4") is False          # ip yok → asla geçme
    assert ip_allowed("1.2.3.4", " 9.9.9.9 , 1.2.3.4 ") is True   # boşluklu liste


def test_qr_token_lifecycle():
    now = 1_785_000_000
    tok = make_qr_token(SECRET_KEY, qr_bucket(now))
    assert verify_qr_token(SECRET_KEY, tok, now) == "ok"
    # ±1 bucket toleransı (sınırda okutma)
    assert verify_qr_token(SECRET_KEY, tok, now + 30) == "ok"
    assert verify_qr_token(SECRET_KEY, tok, now - 30) == "ok"
    # 2 bucket sonra süresi dolar (eski ekran görüntüsü işe yaramaz)
    assert verify_qr_token(SECRET_KEY, tok, now + 90) == "expired"
    # Bozuk imza / format / yanlış secret → invalid
    assert verify_qr_token(SECRET_KEY, tok[:-1] + "0", now) == "invalid"
    assert verify_qr_token(SECRET_KEY, "PDKSQR1:abc:def", now) == "invalid"
    assert verify_qr_token(SECRET_KEY, "garbage", now) == "invalid"
    assert verify_qr_token(SECRET_KEY, "", now) == "invalid"
    assert verify_qr_token("baska-secret-1234567890", tok, now) == "invalid"
    # ASCII olmayan QR (personel başka bir kod okuttu) → 500 DEĞİL, invalid
    assert verify_qr_token(SECRET_KEY, "PDKSQR1:123:ığüşöç0123456789", now) == "invalid"
    assert verify_qr_token(SECRET_KEY, "Şirket menüsü", now) == "invalid"


def test_numeric_code_lifecycle():
    """Yedek kod: QR ile aynı pencerede döner, ±2 bucket kabul edilir."""
    now = 1_785_000_000
    code = make_numeric_code(SECRET_KEY, qr_bucket(now))
    assert len(code) == NUMERIC_CODE_LEN and code.isdigit()
    assert verify_numeric_code(SECRET_KEY, code, now) == "ok"
    # ±2 bucket (60–90 sn) — kod ELLE yazılır, QR'dan uzun sürer
    assert verify_numeric_code(SECRET_KEY, code, now + 60) == "ok"
    assert verify_numeric_code(SECRET_KEY, code, now - 60) == "ok"
    # Pencere dışı ama imzası tutuyor → 'expired' (ekrandaki eski kod yazılmış)
    assert verify_numeric_code(SECRET_KEY, code, now + 150) == "expired"
    # Bozuk / yanlış uzunluk / yanlış secret → invalid
    assert verify_numeric_code(SECRET_KEY, "000000", now + 999_999) == "invalid"
    assert verify_numeric_code(SECRET_KEY, code[:-1], now) == "invalid"
    assert verify_numeric_code(SECRET_KEY, "abcdef", now) == "invalid"
    assert verify_numeric_code(SECRET_KEY, "", now) == "invalid"
    assert verify_numeric_code(SECRET_KEY, None, now) == "invalid"
    assert verify_numeric_code("baska-secret-1234567890", code, now) == "invalid"
    # ASCII olmayan rakamlar (Arapça-Hint) isdigit() True döner → compare_digest
    # TypeError atardı; 500 DEĞİL invalid dönmeli
    assert verify_numeric_code(SECRET_KEY, "٣٤٥٦٧٨", now) == "invalid"


def test_numeric_code_differs_from_qr_signature():
    """Kod ve QR aynı bucket'tan ama AYRI HMAC domain'inden türer — biri
    diğerini ele vermez."""
    b = qr_bucket(1_785_000_000)
    assert make_numeric_code(SECRET_KEY, b) not in make_qr_token(SECRET_KEY, b)
    # ardışık bucket'larda kod değişir (sabit kod = kalıcı şifre olurdu)
    assert make_numeric_code(SECRET_KEY, b) != make_numeric_code(SECRET_KEY, b + 1)


def test_real_client_ip_spoof_guard():
    from routers.pdks import _real_client_ip

    class _Req:
        def __init__(self, peer, headers=None):
            self.client = type("C", (), {"host": peer})()
            self.headers = headers or {}

    # Doğrudan dış bağlantı → X-Real-IP'ye ASLA güvenme (sahtelenebilir)
    assert _real_client_ip(_Req("203.0.113.9", {"x-real-ip": "85.105.20.31"})) == "203.0.113.9"
    # Loopback peer (nginx arkası) → header'a güven
    assert _real_client_ip(_Req("127.0.0.1", {"x-real-ip": "85.105.20.31"})) == "85.105.20.31"
    # Header yoksa peer'e düş
    assert _real_client_ip(_Req("127.0.0.1")) == "127.0.0.1"
    assert _real_client_ip(_Req("testclient")) == "testclient"


# ─── API ────────────────────────────────────────────────────────────────────

def _enforce_on(db, ips="testclient", lat=41.06, lon=29.0, radius=150,
                qr_mode="rotating"):
    """Üçlü doğrulamayı AppSetting üzerinden aç.

    qr_mode VARSAYILANI 'rotating': bu yardımcıyı kullanan testler kiosk
    ekranındaki dönen QR/6 haneli kod akışını sınar.  Ürün varsayılanı ise
    'static' (ekranı olmayan ofis) — o akışın testleri en altta."""
    for key, val in (("pdks.checkin.enforce", "true"),
                     ("pdks.checkin.allowed_ips", ips),
                     ("pdks.checkin.lat", str(lat)),
                     ("pdks.checkin.lon", str(lon)),
                     ("pdks.checkin.qr_mode", qr_mode),
                     ("pdks.checkin.radius_m", str(radius))):
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row:
            row.value = val
        else:
            db.add(AppSetting(key=key, value=val))
    db.commit()


def _valid_token():
    return make_qr_token(SECRET_KEY, qr_bucket(time.time()))


def _login(client, username, password="minerva123"):
    """Aynı TestClient üzerinde kullanıcı değiştir — authed_client ve
    staff_employee_client TEK client paylaşır, ikinci login ilkinin çerezini
    ezer; iki rolü aynı testte kullanırken bunu açıkça yapmak gerekir."""
    r = client.post("/api/login", json={"username": username, "password": password},
                    headers=ORIGIN)
    assert r.status_code == 200, f"{username} login başarısız: {r.text}"
    return client


def test_enforce_off_is_backward_compatible(staff_employee_client, db_session):
    """Doğrulama kapalıyken eski akış aynen çalışır (geo/QR alanı gerekmez)."""
    c, emp_id = staff_employee_client
    r = c.post("/api/pdks/check", json={"type": "in"}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert r.json()["checkin"]["enforce"] is False


def test_enforce_blocks_wrong_ip(staff_employee_client, db_session):
    c, _ = staff_employee_client
    _enforce_on(db_session, ips="85.105.20.31")        # testclient listede yok
    r = c.post("/api/pdks/check",
               json={"type": "in", "qr_token": _valid_token(),
                     "lat": 41.06, "lon": 29.0, "accuracy": 10},
               headers=ORIGIN)
    assert r.status_code == 400
    assert "Ofis internetine" in r.json()["detail"]


def test_enforce_qr_required_and_validated(staff_employee_client, db_session):
    c, _ = staff_employee_client
    _enforce_on(db_session)
    base = {"type": "in", "lat": 41.06, "lon": 29.0, "accuracy": 10}
    # QR yok
    r = c.post("/api/pdks/check", json=base, headers=ORIGIN)
    assert r.status_code == 400 and "QR kod okutulmadı" in r.json()["detail"]
    # Geçersiz QR
    r = c.post("/api/pdks/check", json={**base, "qr_token": "PDKSQR1:1:deadbeefdeadbeef"},
               headers=ORIGIN)
    assert r.status_code == 400 and "geçersiz" in r.json()["detail"]
    # Süresi dolmuş QR (10 bucket önce üretilmiş)
    old = make_qr_token(SECRET_KEY, qr_bucket(time.time()) - 10)
    r = c.post("/api/pdks/check", json={**base, "qr_token": old}, headers=ORIGIN)
    assert r.status_code == 400 and "süresi dolmuş" in r.json()["detail"]


def test_manual_code_accepted_instead_of_qr(staff_employee_client, db_session):
    """Kamerası olmayan personel: ekrandaki 6 haneli kodla imza atabilmeli."""
    from routers.pdks import _code_fails
    _code_fails.clear()
    c, _ = staff_employee_client
    _enforce_on(db_session, lat=41.06, lon=29.0, radius=150)
    code = make_numeric_code(SECRET_KEY, qr_bucket(time.time()))
    r = c.post("/api/pdks/check",
               json={"type": "in", "manual_code": code,
                     "lat": 41.06, "lon": 29.0, "accuracy": 10},
               headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "iceride"
    # Konum şartı kodla girişte de aynen geçerli — kod tek başına yetmez
    _code_fails.clear()
    r = c.post("/api/pdks/check",
               json={"type": "out", "manual_code": code,
                     "lat": 39.9334, "lon": 32.8597, "accuracy": 10},
               headers=ORIGIN)
    assert r.status_code == 400 and "Ofis konumunda" in r.json()["detail"]


def test_manual_code_invalid_and_expired(staff_employee_client, db_session):
    from routers.pdks import _code_fails
    _code_fails.clear()
    c, _ = staff_employee_client
    _enforce_on(db_session)
    base = {"type": "in", "lat": 41.06, "lon": 29.0, "accuracy": 10}
    r = c.post("/api/pdks/check", json={**base, "manual_code": "000000"}, headers=ORIGIN)
    assert r.status_code == 400 and "geçersiz" in r.json()["detail"]
    _code_fails.clear()
    old = make_numeric_code(SECRET_KEY, qr_bucket(time.time()) - 8)
    r = c.post("/api/pdks/check", json={**base, "manual_code": old}, headers=ORIGIN)
    assert r.status_code == 400 and "süresi dolmuş" in r.json()["detail"]


def test_manual_code_brute_force_blocked(staff_employee_client, db_session):
    """6 hane kaba kuvvete QR'dan açık — personel başına deneme penceresi var."""
    from routers.pdks import _code_fails, CODE_FAIL_LIMIT
    _code_fails.clear()
    c, _ = staff_employee_client
    _enforce_on(db_session)
    base = {"type": "in", "lat": 41.06, "lon": 29.0, "accuracy": 10}
    for _ in range(CODE_FAIL_LIMIT):
        r = c.post("/api/pdks/check", json={**base, "manual_code": "000000"}, headers=ORIGIN)
        assert r.status_code == 400
    r = c.post("/api/pdks/check", json={**base, "manual_code": "000000"}, headers=ORIGIN)
    assert r.status_code == 429 and "hatalı kod" in r.json()["detail"]
    # DOĞRU kod da bloklu — pencere dolmadan geçilemez
    good = make_numeric_code(SECRET_KEY, qr_bucket(time.time()))
    assert c.post("/api/pdks/check", json={**base, "manual_code": good},
                  headers=ORIGIN).status_code == 429
    # QR yolu bundan etkilenmez (kamera çalışan personel kilitlenmesin)
    r = c.post("/api/pdks/check", json={**base, "qr_token": _valid_token()}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    _code_fails.clear()


def test_enforce_geo_required_and_validated(staff_employee_client, db_session):
    c, _ = staff_employee_client
    _enforce_on(db_session, lat=41.06, lon=29.0, radius=150)
    # Konum yok
    r = c.post("/api/pdks/check", json={"type": "in", "qr_token": _valid_token()},
               headers=ORIGIN)
    assert r.status_code == 400 and "Konum bilgisi alınamadı" in r.json()["detail"]
    # Ankara'dan giriş denemesi
    r = c.post("/api/pdks/check",
               json={"type": "in", "qr_token": _valid_token(),
                     "lat": 39.9334, "lon": 32.8597, "accuracy": 10},
               headers=ORIGIN)
    assert r.status_code == 400 and "Ofis konumunda" in r.json()["detail"]


def test_enforce_all_pass_persists_geo(staff_employee_client, db_session):
    c, emp_id = staff_employee_client
    _enforce_on(db_session)
    r = c.post("/api/pdks/check",
               json={"type": "in", "qr_token": _valid_token(),
                     "lat": 41.0601, "lon": 29.0002, "accuracy": 12.5},
               headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "iceride"
    assert r.json()["checkin"]["enforce"] is True
    ev = db_session.query(AttendanceEvent).filter_by(employee_id=emp_id).first()
    assert ev.source == "self" and ev.ip_address == "testclient"
    assert round(ev.geo_lat, 4) == 41.0601 and round(ev.geo_lon, 4) == 29.0002
    assert ev.geo_accuracy_m == 12.5


def test_enforce_trusts_x_real_ip_behind_proxy(staff_employee_client, db_session):
    """TestClient peer'i loopback sınıfında → nginx'in yazdığı X-Real-IP geçerli."""
    c, _ = staff_employee_client
    _enforce_on(db_session, ips="203.0.113.7")
    r = c.post("/api/pdks/check",
               json={"type": "in", "qr_token": _valid_token(),
                     "lat": 41.06, "lon": 29.0, "accuracy": 10},
               headers={**ORIGIN, "X-Real-IP": "203.0.113.7"})
    assert r.status_code == 200, r.text


def test_checkin_config_rbac_and_roundtrip(staff_employee_client, db_session):
    c, _ = staff_employee_client
    assert c.get("/api/pdks/checkin-config").status_code == 403
    assert c.put("/api/pdks/checkin-config", json={"enforce": True},
                 headers=ORIGIN).status_code == 403

    authed_client = _login(c, "dogukan")     # SuperAdmin'e geç
    r = authed_client.get("/api/pdks/checkin-config")
    assert r.status_code == 200
    d = r.json()
    assert d["enforce"] is False and d["radius_m"] == 150
    assert d["detected_ip"] == "testclient"

    # Eksik ayarla enforce açılamaz
    r = authed_client.put("/api/pdks/checkin-config", json={"enforce": True}, headers=ORIGIN)
    assert r.status_code == 400 and "Önce ofis IP" in r.json()["detail"]

    # Kısmi güncelleme → sonra enforce
    r = authed_client.put("/api/pdks/checkin-config",
                          json={"allowed_ips": "testclient", "lat": 41.06,
                                "lon": 29.0, "radius_m": 200},
                          headers=ORIGIN)
    assert r.status_code == 200 and r.json()["radius_m"] == 200
    # Varsayılan mod 'static' → basılı kod üretilmeden enforce AÇILAMAZ
    r = authed_client.put("/api/pdks/checkin-config", json={"enforce": True}, headers=ORIGIN)
    assert r.status_code == 400 and "basılı QR" in r.json()["detail"]
    # Kiosk moduna geçince (ekran var) kod şartı aranmaz
    r = authed_client.put("/api/pdks/checkin-config",
                          json={"enforce": True, "qr_mode": "rotating"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["enforce"] is True
    assert r.json()["allowed_ips"] == "testclient"     # dokunulmayan alan korundu
    # Yarıçap sınırları
    assert authed_client.put("/api/pdks/checkin-config", json={"radius_m": 5},
                             headers=ORIGIN).status_code == 422
    # Audit
    actions = {a.action for a in db_session.query(AdminAuditLog).all()}
    assert "pdks.checkin_config" in actions


def test_kiosk_qr_endpoint_and_page(staff_employee_client, db_session):
    # Normal personel kiosk yetkisi almaz
    c, _ = staff_employee_client
    assert c.get("/api/pdks/qr").status_code == 403
    assert c.get("/pdks-qr", follow_redirects=False).status_code == 302

    # SuperAdmin (her yetki açık) QR alabilir
    authed_client = _login(c, "dogukan")
    r = authed_client.get("/api/pdks/qr")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["svg"].lstrip().startswith("<svg")
    assert 1 <= d["seconds_left"] <= d["period"] == 30
    assert verify_qr_token(SECRET_KEY, d["token"], time.time()) == "ok"
    # Kamerası olmayan personel için yedek kod da aynı yanıtta gelir
    assert len(d["code"]) == NUMERIC_CODE_LEN and d["code"].isdigit()
    assert verify_numeric_code(SECRET_KEY, d["code"], time.time()) == "ok"
    assert "no-store" in r.headers.get("cache-control", "")   # canlı token cache'lenmesin
    assert authed_client.get("/pdks-qr").status_code == 200


def test_qr_svg_is_scalable_not_clipped(authed_client):
    """QR'ın viewBox'ı OLMAK ZORUNDA.

    viewBox'sız kök <svg>'de CSS width/height yalnız viewport'u değiştirir;
    koordinat sistemi 1:1 px kalır ve sembol kiosk kartında kırpılır → QR
    decode edilemez, doğrulama açıldığı anda kimse imza atamaz.  Bu testi
    'startswith(<svg>)' yakalayamıyordu (canlı tarayıcıda tespit edildi)."""
    svg = authed_client.get("/api/pdks/qr").json()["svg"]
    head = svg[:svg.index(">") + 1]
    assert "viewBox=" in head, f"viewBox yok — QR kırpılır: {head}"
    assert "width=" not in head and "height=" not in head, \
        f"sabit boyut ölçeklemeyi engeller: {head}"


def test_kiosk_override_user_can_only_show_qr(client, db_session):
    """Kiosk cihazı: yalnız pdks.kiosk yetkili özel hesap."""
    perms = {"pdks": {"check": False, "view_own": False, "view_all": False,
                      "manage": False, "report": False, "kiosk": True}}
    u = _mk_user(db_session, "pdks-kiosk", full_name="Giriş Ekranı")
    u.permissions = json.dumps(perms)
    db_session.commit()
    r = client.post("/api/login", json={"username": "pdks-kiosk", "password": "minerva123"},
                    headers=ORIGIN)
    assert r.status_code == 200
    assert client.get("/api/pdks/qr").status_code == 200
    assert client.get("/pdks-qr").status_code == 200
    # Kiosk hesabı puantaj/olay uçlarına giremez
    assert client.get("/api/pdks/me/today").status_code == 403
    assert client.post("/api/pdks/check", json={"type": "in"},
                       headers=ORIGIN).status_code == 403


def test_kiosk_token_accepted_by_check(staff_employee_client, db_session):
    """Uçtan uca: kiosk ekranından alınan token ile gerçek giriş."""
    c, _ = staff_employee_client
    token = _login(c, "dogukan").get("/api/pdks/qr").json()["token"]
    _login(c, "personel1")         # personel olarak geri dön
    _enforce_on(db_session)
    r = c.post("/api/pdks/check",
               json={"type": "in", "qr_token": token,
                     "lat": 41.06, "lon": 29.0, "accuracy": 8},
               headers=ORIGIN)
    assert r.status_code == 200, r.text


# ═══ Basılı (sabit) QR — kiosk ekranı olmayan ofisler ═══════════════════════

def test_static_code_helpers():
    from core.pdks import (STATIC_CODE_LEN, make_static_token,
                           normalize_static_code, verify_static_code,
                           verify_static_token)
    code = "K7P2M9QX"
    # Elle girişte büyük/küçük harf, boşluk ve tire toleranslı
    assert normalize_static_code(" k7p2-m9qx ") == code
    assert verify_static_code(code, "k7p2 m9qx") == "ok"
    assert verify_static_code(code, "K7P2M9QY") == "invalid"
    # Kod üretilmemişse ASLA geçme (fail-closed)
    assert verify_static_code("", code) == "invalid"
    assert verify_static_code(code, "") == "invalid"
    # QR metni: önek zorunlu — ofisteki başka bir barkod okutulursa tutmaz
    assert verify_static_token(code, make_static_token(code)) == "ok"
    assert verify_static_token(code, code) == "invalid"
    assert verify_static_token(code, "PDKSQR1:123:abc") == "invalid"
    assert verify_static_token(code, "ŞİRKET MENÜSÜ") == "invalid"
    assert len(code) == STATIC_CODE_LEN
    # ASCII olmayan girdi 500 DEĞİL invalid dönmeli (compare_digest TypeError
    # tuzağı — verify_qr_token/verify_numeric_code'daki kardeş kural burada da
    # geçerli olmalı)
    assert verify_static_code(code, "çğüşöü") == "invalid"
    assert verify_static_code("çğüşöü", code) == "invalid"


def _enforce_static(db, client, ips="testclient"):
    """Basılı QR modunda doğrulamayı aç; üretilen kodu döndür."""
    r = client.post("/api/pdks/qr/static/regenerate", headers=ORIGIN)
    assert r.status_code == 200, r.text
    code = r.json()["code"]
    r = client.put("/api/pdks/checkin-config",
                   json={"qr_mode": "static", "allowed_ips": ips,
                         "lat": 41.06, "lon": 29.0, "enforce": True},
                   headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert r.json()["qr_mode"] == "static"
    return code


def test_static_qr_generate_and_print_page(staff_employee_client, db_session):
    c, _ = staff_employee_client
    # Personel ne kodu görebilir ne afişi
    assert c.get("/api/pdks/qr/static").status_code == 403
    assert c.post("/api/pdks/qr/static/regenerate", headers=ORIGIN).status_code == 403
    assert c.get("/pdks-qr-yazdir", follow_redirects=False).status_code == 302

    admin = _login(c, "dogukan")
    # Henüz kod yok
    assert admin.get("/api/pdks/qr/static").json()["exists"] is False
    r = admin.post("/api/pdks/qr/static/regenerate", headers=ORIGIN)
    assert r.status_code == 201 or r.status_code == 200, r.text
    d = r.json()
    assert d["exists"] is True and len(d["code"]) == 8
    assert d["token"] == f"PDKSQRS1:{d['code']}"
    assert 'viewBox=' in d["svg"] and "width=" not in d["svg"][:d["svg"].index(">")]
    assert "no-store" in r.headers.get("cache-control", "")
    assert admin.get("/pdks-qr-yazdir").status_code == 200
    # Yenileme eski kodu geçersiz kılar
    old = d["code"]
    new = admin.post("/api/pdks/qr/static/regenerate", headers=ORIGIN).json()["code"]
    assert new != old
    assert admin.get("/api/pdks/qr/static").json()["code"] == new


def test_static_mode_check_flow(staff_employee_client, db_session):
    c, emp_id = staff_employee_client
    admin = _login(c, "dogukan")
    code = _enforce_static(db_session, admin)
    _login(c, "personel1")

    geo = {"lat": 41.06, "lon": 29.0, "accuracy": 10}
    # QR/kod yok → net hata
    r = c.post("/api/pdks/check", json={"type": "in", **geo}, headers=ORIGIN)
    assert r.status_code == 400 and "QR kod okutulmadı" in r.json()["detail"]
    # Yanlış kod
    r = c.post("/api/pdks/check",
               json={"type": "in", "qr_token": "PDKSQRS1:YANLIS12", **geo}, headers=ORIGIN)
    assert r.status_code == 400 and "geçersiz" in r.json()["detail"]
    # Dönen (kiosk) token'ı basılı modda KABUL EDİLMEZ
    rot = make_qr_token(SECRET_KEY, qr_bucket(time.time()))
    r = c.post("/api/pdks/check", json={"type": "in", "qr_token": rot, **geo}, headers=ORIGIN)
    assert r.status_code == 400
    # Doğru QR → giriş
    r = c.post("/api/pdks/check",
               json={"type": "in", "qr_token": f"PDKSQRS1:{code}", **geo}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "iceride"
    assert r.json()["checkin"]["qr_mode"] == "static"
    # Elle kod ile çıkış (kamera çalışmıyor senaryosu) — küçük harf/tire toleranslı
    typed = code.lower()[:4] + "-" + code.lower()[4:]
    r = c.post("/api/pdks/check",
               json={"type": "out", "manual_code": typed, **geo}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "disarida"


def test_static_code_never_leaks_to_staff(staff_employee_client, db_session):
    """Kod /me/today ile personele SIZMAMALI — sızarsa evden imza atılır."""
    c, _ = staff_employee_client
    admin = _login(c, "dogukan")
    code = _enforce_static(db_session, admin)
    _login(c, "personel1")
    body = c.get("/api/pdks/me/today").text
    assert code not in body
    assert c.get("/api/pdks/checkin-config").status_code == 403


def test_enforce_static_requires_code(staff_employee_client, db_session):
    c, _ = staff_employee_client
    admin = _login(c, "dogukan")
    r = admin.put("/api/pdks/checkin-config",
                  json={"qr_mode": "static", "allowed_ips": "testclient",
                        "lat": 41.06, "lon": 29.0, "enforce": True},
                  headers=ORIGIN)
    assert r.status_code == 400
    assert "basılı QR" in r.json()["detail"]


def test_static_manual_code_nonascii_is_400_not_500(staff_employee_client, db_session):
    """Türkçe klavyeli personel afişteki kodu yazarken 'g' yerine 'ğ' gibi
    yazarsa 500 DEĞİL düzgün Türkçe 400 dönmeli (compare_digest TypeError
    tuzağı — kardeş fonksiyonlarda zaten korunuyordu, burada eksikti)."""
    c, _ = staff_employee_client
    admin = _login(c, "dogukan")
    _enforce_static(db_session, admin)
    _login(c, "personel1")
    r = c.post("/api/pdks/check",
              json={"type": "in", "manual_code": "ÇĞÜŞÖÇĞÜ",
                    "lat": 41.06, "lon": 29.0, "accuracy": 10},
              headers=ORIGIN)
    assert r.status_code == 400
    assert "geçersiz" in r.json()["detail"]
