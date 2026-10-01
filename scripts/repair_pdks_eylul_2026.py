"""Eylül 2026 puantaj düzeltmesi — maaş öncesi (01.10.2026).

NEDEN
─────
Eylül puantajı bordroya gitmeden önce gün gün incelendi (01.10.2026).
Yönetici (Doğukan) teyidiyle:

  • Meltem Erkmen 11.09'da ayrıldı — kartta ayrılış 29.09 yazıyordu, bu
    yüzden 14–29.09 arası her iş günü DEVAMSIZ görünüyordu.
  • Betül Akkuş 11.09 Cuma 08:29'da girdi; Işık Hanım onu erken çıkardı
    (≈09:00).  Çıkış basılmadığı için gün "çıkış eksik" (0 dk) kalmıştı.
  • Songül Akgül 18.09 Cuma vaktinde çıktı ama çıkış basmadı.
  • Doğukan 14.09'dan (dahil) itibaren Pazartesi ve Çarşamba okulda; o
    günler çalışma günü değil.  29.09'da bu düzenle bir program girilmişti
    ama başlangıcı 14.09'a çekilmemişti → 14/16/21/23/28.09 DEVAMSIZ
    görünüyordu.

2. tur (aynı gün, yönetici cevapları):
  • 07.09 Pazartesi kimse basmamıştı — Betül, Meltem, Vedat, Sued ofisteydi.
  • Betül'ün son günü 11.09; 01.09 normal saatte çıktı.  Dudu'nun son günü 04.09.
  • Sued 07.09'da başladı (kartta tarih yoktu, basımları 18.09'da başlıyor),
    düzenli geldi, normal saatte çıktı.  Vedat 08.09 normal saatte çıktı.
  • Songül'ün eksik günü yok (24.09 tam gün); 14.09 Pazartesi (izin günü)
    geldi, o haftanın iznini 16.09 Çarşamba kullandı.

3. tur: Betül 02.09 gelmedi (devamsız kalır).  Dudu olduğu gibi kalır.
Çiçek 15.09'da başladı, sistem 24.09'da açıldı, arada düzgün saatlerde geldi.
Doğukan 24.09 sabah geldi, girişi yanlışlıkla 17:50'de bastı; 30.09'da da
ofisteydi (çıkış saati verilmedi → normal 18:00).

4. tur: Doğukan 30.09'da (okul günü) ekstra geldi ve bunu 07.09'daki sınav
izninin telafisi saydı ("10 saat fazla mesai yazma") → gün değişimi.

NE YAPAR
────────
1. İşe giriş tarihlerini yazar.
2. Hiç kaydı olmayan ama gelinen günlere `source="manual"` giriş+çıkış ekler.
3. Eksik çıkışları `source="manual"` olayla kapatır (not + yönetici izi).
4. Ayrılış tarihlerini düzeltir.
5. Program versiyonlarını ekler / aynı tarihli versiyonu YERİNDE düzeltir
   (router'daki upsert_schedule kuralı; eski şablon çıktıya basılır).

İdempotent: ikinci çalıştırmada "zaten var" der, çift yazmaz.

KULLANIM (prod'da)
──────────────────
    venv/bin/python scripts/repair_pdks_eylul_2026.py            # kuru
    venv/bin/python scripts/repair_pdks_eylul_2026.py --commit   # yaz
"""
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import (AttendanceEvent, Employee, EmployeeSchedule,  # noqa: E402
                      LeaveRecord, SessionLocal)

COMMIT = "--commit" in sys.argv
ACTOR_USER_ID = 1                     # Doğukan (IMS user id)
NOTE = "Eylül 2026 puantaj düzeltmesi (01.10.2026, yönetici teyidi)"
TR_OFFSET = timedelta(hours=3)


def _tpl(days: dict) -> str:
    """{gün_no: (başlangıç, bitiş)} → 7 günlük şablon JSON'ı (eksik gün = tatil)."""
    return json.dumps({str(i): ({"start": days[i][0], "end": days[i][1]} if i in days else None)
                       for i in range(7)}, ensure_ascii=False)


# ── Eksik çıkışlar (personel, TR gün, TR saat, gerekçe) ─────────────────────
SADECE_CIKIS = [
    ("Betül Akkuş", date(2026, 9, 11), "09:00", "Işık Hanım erken çıkardı"),
    ("Songül Akgül", date(2026, 9, 18), "17:45", "vaktinde çıktı, çıkış basılmamıştı"),
    # 2. tur (01.10.2026): "normal saatte çıktı"
    ("Betül Akkuş", date(2026, 9, 1), "17:45", "normal saatte çıktı"),
    ("Vedat Doğan", date(2026, 9, 8), "17:45", "normal saatte çıktı"),
    ("Sued", date(2026, 9, 18), "17:45", "normal saatte çıktı"),
    ("Sued", date(2026, 9, 23), "18:00", "normal saatte çıktı"),
    # 3. tur: "dün de buradaydım" — çıkış saati verilmedi, normal saat
    ("Doğukan Yalçınkaya", date(2026, 9, 30), "18:00", "ofisteydi, normal saatte çıktı"),
]

# ── Yanlış saatle basılmış olaylar → pasifleştir (personel, gün, TR saat, tip)
# Doğukan 24.09: sabah gelmiş, girişi 17:50'de basmış (çıkışla aynı anda) —
# giriş pasifleşir, sabah girişi eklenir, 17:50 çıkışı yerinde kalır.
PASIF = [
    ("Doğukan Yalçınkaya", date(2026, 9, 24), "17:50", "in",
     "yanlış saatle basılmış giriş (sabah gelmişti)"),
]

# ── Eksik girişler (personel, gün, TR saat, gerekçe) ───────────────────────
SADECE_GIRIS = [
    ("Doğukan Yalçınkaya", date(2026, 9, 24), "09:00", "sabah normal saatte geldi"),
]

# ── Hiç kaydı olmayan ama gelinen günler (personel, gün, giriş, çıkış, gerekçe)
# 07.09 Pazartesi kimse basmamıştı; yönetici: "Betül, Meltem, Vedat, Sued
# vardı" (Doğukan izinli, Songül'ün Pazartesi izin günü).  Sued 07.09'da işe
# başladı, kendi basımları 18.09'da başlıyor — "düzenli geldi, normal saatte
# çıktı".  Songül: "eksik günü yok, tam yap".
_SUED_GUNLER = [date(2026, 9, d) for d in (7, 8, 9, 10, 11, 14, 15, 16, 17)]
TAM_GUN = [
    ("Betül Akkuş", date(2026, 9, 7), "08:30", "17:45", "07.09 ofisteydi"),
    ("Meltem Erkmen", date(2026, 9, 7), "08:30", "17:45", "07.09 ofisteydi"),
    ("Vedat Doğan", date(2026, 9, 7), "08:30", "17:45", "07.09 işe başladı, ofisteydi"),
    *[("Sued", d, "08:30", "17:45", "07.09'da başladı, düzenli geldi") for d in _SUED_GUNLER],
    ("Songül Akgül", date(2026, 9, 24), "09:00", "18:00", "eksik günü yok, tam gün"),
    # 3. tur: Çiçek 15.09'da başladı; sisteme 24.09'da açıldı, arada düzgün geldi
    *[("Çiçek Yüksel", date(2026, 9, d), "08:30", "17:45",
       "15.09'da başladı, sisteme sonradan açıldı") for d in (15, 16, 17, 18, 21, 22)],
    ("Çiçek Yüksel", date(2026, 9, 23), "09:00", "18:00",
     "15.09'da başladı, sisteme sonradan açıldı"),
]

# ── Ayrılış tarihleri (personel, gerçek son gün — dahil) ────────────────────
AYRILIS = [
    ("Meltem Erkmen", date(2026, 9, 11)),
    ("Betül Akkuş", date(2026, 9, 11)),
    ("Dudu Ekmekçi", date(2026, 9, 4)),
]

# ── İşe giriş tarihleri (personel, ilk gün) ─────────────────────────────────
BASLANGIC = [
    ("Sued", date(2026, 9, 7)),
]

# ── Program versiyonları (personel, geçerlilik, şablon) ─────────────────────
# Doğukan: 14.09'dan itibaren Pzt + Çar okul.  23.09 öncesi saat 08:30–17:45,
# sonrası 09:00–18:00 (mesai değişikliği) → iki versiyon.
PROGRAM = [
    ("Doğukan Yalçınkaya", date(2026, 9, 14),
     _tpl({1: ("08:30", "17:45"), 3: ("08:30", "17:45"), 4: ("08:30", "17:45")})),
    ("Doğukan Yalçınkaya", date(2026, 9, 23),
     _tpl({1: ("09:00", "18:00"), 3: ("09:00", "18:00"), 4: ("09:00", "18:00")})),
    # Sued 07.09'da başladı; programı 17.09'dan başlıyordu.
    ("Sued", date(2026, 9, 7), _tpl({i: ("08:30", "17:45") for i in range(5)})),
    # Songül (Sal–Cum çalışır): 14.09 Pazartesi geldi, o haftanın izin gününü
    # 16.09 Çarşamba kullandı → o hafta Pzt/Sal/Per/Cum, 21.09'dan yine Sal–Cum.
    # (Yönetici "Cuma" dedi ama 18.09 Cuma basımı var ve "vaktinde çıktı" diye
    # teyit edilmişti; 16.09 Çarşamba hiç kayıt yok.)
    ("Songül Akgül", date(2026, 9, 14),
     _tpl({0: ("08:30", "17:45"), 1: ("08:30", "17:45"), 3: ("08:30", "17:45"),
           4: ("08:30", "17:45")})),
    ("Songül Akgül", date(2026, 9, 21), _tpl({i: ("08:30", "17:45") for i in (1, 2, 3, 4)})),
    # Çiçek 15.09'da başladı; programı 24.09'dan başlıyordu.
    ("Çiçek Yüksel", date(2026, 9, 15), _tpl({i: ("08:30", "17:45") for i in range(5)})),
    ("Çiçek Yüksel", date(2026, 9, 23), _tpl({i: ("09:00", "18:00") for i in range(5)})),
    # 4. tur — Doğukan: 07.09 (Pzt) sınav izni 30.09 (Çar, okul günü) ekstra
    # çalışmasıyla telafi edildi → gün değişimi: 07.09 çalışma günü değil,
    # 30.09 normal iş günü.  Tek günlük versiyonlar; ertesi gün eski düzen döner.
    ("Doğukan Yalçınkaya", date(2026, 9, 7), _tpl({i: ("08:30", "17:45") for i in (1, 2, 3, 4)})),
    ("Doğukan Yalçınkaya", date(2026, 9, 8), _tpl({i: ("08:30", "17:45") for i in range(5)})),
    ("Doğukan Yalçınkaya", date(2026, 9, 30), _tpl({i: ("09:00", "18:00") for i in (1, 2, 3, 4)})),
    ("Doğukan Yalçınkaya", date(2026, 10, 1), _tpl({i: ("09:00", "18:00") for i in (1, 3, 4)})),
]

# ── Telafi edilen izinler → pasifleştir (personel, başlangıç, bitiş, not) ───
IZIN_PASIF = [
    ("Doğukan Yalçınkaya", date(2026, 9, 7), date(2026, 9, 7),
     "30.09 Çarşamba ekstra çalışmasıyla telafi edildi (gün değişimi)"),
]


def utc_of(d: date, hm: str) -> datetime:
    h, m = hm.split(":")
    return datetime(d.year, d.month, d.day, int(h), int(m)) - TR_OFFSET


def main() -> int:
    db = SessionLocal()
    n = {"cikis": 0, "tam_gun": 0, "ayrilis": 0, "baslangic": 0, "program": 0,
         "pasif": 0, "giris": 0, "izin": 0}
    try:
        by_name = {e.full_name: e for e in db.query(Employee).all()}

        def emp(name):
            e = by_name.get(name)
            if not e:
                raise RuntimeError(f"Personel bulunamadı: {name}")
            return e

        print("── İşe giriş tarihleri")
        for name, start in BASLANGIC:
            e = emp(name)
            if e.start_date == start:
                print(f"   · {name:20} başlangıç zaten {start:%d.%m.%Y}")
                continue
            print(f"   ~ {name:20} başlangıç {e.start_date and e.start_date.strftime('%d.%m.%Y')} "
                  f"→ {start:%d.%m.%Y}")
            if COMMIT:
                e.start_date = start
            n["baslangic"] += 1

        print("── Tam gün (kayıt hiç yoktu)")
        for name, d, in_hm, out_hm, why in TAM_GUN:
            e = emp(name)
            any_ev = (db.query(AttendanceEvent.id)
                      .filter(AttendanceEvent.employee_id == e.id,
                              AttendanceEvent.work_date == d,
                              AttendanceEvent.is_active == True)         # noqa: E712
                      .first())
            if any_ev:
                print(f"   · {name:20} {d:%d.%m} zaten kayıt var — dokunulmadı")
                continue
            print(f"   + {name:20} {d:%d.%m} {in_hm}–{out_hm}  ({why})")
            if COMMIT:
                for typ, hm in (("in", in_hm), ("out", out_hm)):
                    db.add(AttendanceEvent(
                        employee_id=e.id, event_type=typ, ts_utc=utc_of(d, hm),
                        work_date=d, source="manual", created_by_user_id=ACTOR_USER_ID,
                        correction_note=f"{why} — {NOTE}"[:300]))
            n["tam_gun"] += 1
        if COMMIT:
            db.flush()

        print("── Yanlış basımlar (pasifleştirilir)")
        for name, d, hm, typ, why in PASIF:
            e = emp(name)
            ts = utc_of(d, hm)
            row = (db.query(AttendanceEvent)
                   .filter(AttendanceEvent.employee_id == e.id,
                           AttendanceEvent.work_date == d,
                           AttendanceEvent.event_type == typ,
                           AttendanceEvent.ts_utc >= ts,
                           AttendanceEvent.ts_utc < ts + timedelta(minutes=1))
                   .first())
            if row is None:
                raise RuntimeError(f"{name} {d} {hm} {typ}: olay bulunamadı")
            if not row.is_active:
                print(f"   · {name:20} {d:%d.%m} {hm} {typ} zaten pasif")
                continue
            print(f"   − {name:20} {d:%d.%m} {hm} {typ} (id {row.id})  ({why})")
            if COMMIT:
                row.is_active = False
                row.corrected_by = "sistem"
                row.corrected_at = datetime.utcnow()
                row.correction_note = f"{why} — {NOTE}"[:300]
            n["pasif"] += 1
        if COMMIT:
            db.flush()

        print("── Eksik girişler")
        for name, d, hm, why in SADECE_GIRIS:
            e = emp(name)
            ts = utc_of(d, hm)
            dup = (db.query(AttendanceEvent.id)
                   .filter(AttendanceEvent.employee_id == e.id,
                           AttendanceEvent.work_date == d,
                           AttendanceEvent.event_type == "in",
                           AttendanceEvent.ts_utc == ts,
                           AttendanceEvent.is_active == True)            # noqa: E712
                   .first())
            if dup:
                print(f"   · {name:20} {d:%d.%m} giriş {hm} zaten var")
                continue
            print(f"   + {name:20} {d:%d.%m} giriş {hm}  ({why})")
            if COMMIT:
                db.add(AttendanceEvent(
                    employee_id=e.id, event_type="in", ts_utc=ts, work_date=d,
                    source="manual", created_by_user_id=ACTOR_USER_ID,
                    correction_note=f"{why} — {NOTE}"[:300]))
            n["giris"] += 1
        if COMMIT:
            db.flush()

        print("── Eksik çıkışlar")
        for name, d, hm, why in SADECE_CIKIS:
            e = emp(name)
            has_out = (db.query(AttendanceEvent.id)
                       .filter(AttendanceEvent.employee_id == e.id,
                               AttendanceEvent.work_date == d,
                               AttendanceEvent.event_type == "out",
                               AttendanceEvent.is_active == True)        # noqa: E712
                       .first())
            if has_out:
                print(f"   · {name:20} {d:%d.%m} çıkış zaten var")
                continue
            ins = (db.query(AttendanceEvent)
                   .filter(AttendanceEvent.employee_id == e.id,
                           AttendanceEvent.work_date == d,
                           AttendanceEvent.event_type == "in",
                           AttendanceEvent.is_active == True)            # noqa: E712
                   .order_by(AttendanceEvent.ts_utc).all())
            ts = utc_of(d, hm)
            if not ins or ins[-1].ts_utc >= ts:
                raise RuntimeError(f"{name} {d}: çıkıştan önce açık giriş yok")
            print(f"   + {name:20} {d:%d.%m} çıkış {hm}  ({why})")
            if COMMIT:
                db.add(AttendanceEvent(
                    employee_id=e.id, event_type="out", ts_utc=ts, work_date=d,
                    source="manual", created_by_user_id=ACTOR_USER_ID,
                    correction_note=f"{why} — {NOTE}"[:300]))
            n["cikis"] += 1

        print("── Ayrılış tarihleri")
        for name, end in AYRILIS:
            e = emp(name)
            if e.end_date == end:
                print(f"   · {name:20} ayrılış zaten {end:%d.%m.%Y}")
                continue
            print(f"   ~ {name:20} ayrılış {e.end_date and e.end_date.strftime('%d.%m.%Y')} "
                  f"→ {end:%d.%m.%Y}")
            if COMMIT:
                e.end_date = end
                e.is_active = False
            n["ayrilis"] += 1

        print("── Program versiyonları")
        for name, ef, raw in PROGRAM:
            e = emp(name)
            row = (db.query(EmployeeSchedule)
                   .filter(EmployeeSchedule.employee_id == e.id,
                           EmployeeSchedule.effective_from == ef).first())
            if row and json.loads(row.weekly_template) == json.loads(raw):
                print(f"   · {name:20} {ef:%d.%m} versiyonu zaten güncel")
                continue
            if row:
                print(f"   ~ {name:20} {ef:%d.%m} versiyonu yerinde düzeltildi")
                print(f"       eski şablon: {row.weekly_template}")
                if COMMIT:
                    row.weekly_template = raw
                    row.created_by = f"sistem — {NOTE}"[:100]
            else:
                print(f"   + {name:20} {ef:%d.%m} yeni versiyon")
                if COMMIT:
                    db.add(EmployeeSchedule(
                        employee_id=e.id, effective_from=ef, weekly_template=raw,
                        lunch_break_minutes=60, created_by=f"sistem — {NOTE}"[:100]))
            print(f"       yeni şablon: {raw}")
            n["program"] += 1

        print("── Telafi edilen izinler (pasifleştirilir)")
        for name, d1, d2, why in IZIN_PASIF:
            e = emp(name)
            lv = (db.query(LeaveRecord)
                  .filter(LeaveRecord.employee_id == e.id,
                          LeaveRecord.start_date == d1,
                          LeaveRecord.end_date == d2).first())
            if lv is None:
                raise RuntimeError(f"{name} {d1}–{d2}: izin kaydı bulunamadı")
            if not lv.is_active:
                print(f"   · {name:20} {d1:%d.%m} izni zaten pasif")
                continue
            print(f"   − {name:20} {d1:%d.%m}–{d2:%d.%m} {lv.leave_type} izni "
                  f"(id {lv.id}, not: {lv.note!r})  ({why})")
            if COMMIT:
                lv.is_active = False
                lv.note = f"{(lv.note or '').strip()} — {why} — {NOTE}"[:300]
            n["izin"] += 1

        if COMMIT:
            db.commit()
    except Exception as exc:                                   # pragma: no cover
        db.rollback()
        print(f"\n✖ HATA — geri alındı: {exc}")
        return 1
    finally:
        db.close()

    print("\n" + "═" * 72)
    print(f"Çıkış: {n['cikis']} · giriş: {n['giris']} · pasif: {n['pasif']} · "
          f"tam gün: {n['tam_gun']} · ayrılış: {n['ayrilis']} · "
          f"başlangıç: {n['baslangic']} · program: {n['program']} · "
          f"izin pasif: {n['izin']}")
    print("\n✓ YAZILDI." if COMMIT else "\nKURU ÇALIŞTIRMA — yazmak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
