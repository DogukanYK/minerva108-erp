"""Ağustos 2026 puantaj düzeltmesi — eksik basımlar, izin ve ayrılış tarihleri.

NEDEN
─────
PDKS 03–13.08 arasında kademeli devreye girdi.  Sonuç: aylık puantaj gerçeği
göstermiyordu.

  • 04.08 sistemin ilk günü — Songül/Berkan/Betül sabah basmadan gün ortasında
    birer DENEME basımı yaptı (1 dk / 8 sn).  O günün mesaisi kayıtta yok.
    Müyesser aynı denemeyi 06.08'de yaptı.
  • 9 günde çıkış basılmamış (Berkan 5, Doğukan 1, Songül 1 + yukarıdaki
    denemeler) → o günler 0 dk yazıldı, kişi başı 8 saat eksik göründü.
  • Program versiyonları personelin ilk gününden SONRA başlıyor (Meltem
    10.08, Müyesser 06.08, Betül/Berkan 04.08) → önceki günler "program
    tanımsız", çalışma hiç sayılmıyor.
  • Songül 25–28.08 yıllık izindeydi; izin kaydı girilmediği için DEVAMSIZ.
  • Müyesser 14.08'de, Berkan 24.08'de ayrıldı; ayrılış tarihi alanı yoktu
    (bu deploy'da eklendi) → ayın kalanı devamsız yazılıyordu.

Patron gün gün teyit etti (01.09.2026): "neredeyse her gün herkes geldi",
Müyesser ayrıldı · Songül izinliydi · Berkan'ın son günü 24.08.

NE YAPAR
────────
1. Deneme basımlarını PASİFLEŞTİRİR (silmez — `is_active=False`, iz kalır).
2. Eksik giriş/çıkışları `source="manual"` olayla tamamlar; saatler kişinin
   KENDİ Ağustos medyanından (giriş) ve en sık çıkışından türetildi.
3. Program versiyonlarını 03.08'e geri taşır (yeni versiyon ekler, mevcut
   versiyona DOKUNMAZ — versiyonlama zaten bunun için).
4. Songül'e 25–28.08 yıllık izin kaydı açar.
5. Müyesser/Berkan'a ayrılış tarihi yazar; Müyesser'i pasifleştirir.

Her adım idempotenttir: ikinci çalıştırmada "zaten var" der, çift yazmaz.

KULLANIM
────────
    venv/bin/python scripts/repair_pdks_agustos_2026.py            # kuru
    venv/bin/python scripts/repair_pdks_agustos_2026.py --commit   # yaz
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import (AttendanceEvent, Employee, EmployeeSchedule,      # noqa: E402
                      LeaveRecord, SessionLocal)

COMMIT = "--commit" in sys.argv
ACTOR_USER_ID = 1                     # Doğukan (IMS user id)
NOTE = "Ağustos 2026 puantaj düzeltmesi (01.09.2026, yönetici teyidi)"
TR_OFFSET = timedelta(hours=3)

# ── Deneme basımları — pasifleştirilecek (emp_id, TR gün, TR saat, tip) ──────
# Sistemin ilk gününde gün ortasında yapılan deneme imzaları.  Gerçek mesai
# bunlar değil; aşağıdaki TAM_GUN satırları o günü doğru dolduruyor.
DENEME = [
    (2, date(2026, 8, 4), "11:24", "in"), (2, date(2026, 8, 4), "11:25", "out"),
    (3, date(2026, 8, 4), "11:32", "in"), (3, date(2026, 8, 4), "11:32", "out"),
    (3, date(2026, 8, 4), "11:33", "in"),
    (4, date(2026, 8, 4), "11:39", "in"), (4, date(2026, 8, 4), "11:39", "out"),
    (5, date(2026, 8, 6), "14:41", "in"), (5, date(2026, 8, 6), "14:42", "out"),
]

# ── Tam gün eklenecek (emp_id, TR gün, giriş, çıkış) ────────────────────────
TAM_GUN = [
    # Doğukan — 11.08 hiç basım yok
    (1, date(2026, 8, 11), "08:30", "17:45"),
    # Songül — 04.08 yalnız deneme basımı vardı
    (2, date(2026, 8, 4), "08:30", "17:41"),
    # Berkan — 03.08 program yoktu, 04.08 deneme
    (3, date(2026, 8, 3), "08:35", "17:40"),
    (3, date(2026, 8, 4), "08:35", "17:40"),
    # Betül — işe giriş günü 03.08 sistemde yoktu, 04.08 deneme
    (4, date(2026, 8, 3), "08:30", "17:36"),
    (4, date(2026, 8, 4), "08:30", "17:36"),
    # Müyesser — 03–05.08 program yoktu, 06.08 deneme, 11.08 basım yok
    (5, date(2026, 8, 3), "09:00", "14:00"),
    (5, date(2026, 8, 4), "09:00", "14:00"),
    (5, date(2026, 8, 5), "09:00", "14:00"),
    (5, date(2026, 8, 6), "09:00", "14:00"),
    (5, date(2026, 8, 11), "09:00", "14:00"),
    # Meltem — sisteme 10.08'de tanımlandı, önceki hafta hiç kaydı yok
    (6, date(2026, 8, 3), "08:30", "17:45"),
    (6, date(2026, 8, 4), "08:30", "17:45"),
    (6, date(2026, 8, 5), "08:30", "17:45"),
    (6, date(2026, 8, 6), "08:30", "17:45"),
    (6, date(2026, 8, 7), "08:30", "17:45"),
]

# ── Yalnız çıkış eklenecek (emp_id, TR gün, çıkış) — giriş kayıtta var ──────
SADECE_CIKIS = [
    (1, date(2026, 8, 28), "17:45"),
    (2, date(2026, 8, 21), "17:41"),
    (3, date(2026, 8, 6), "17:40"),
    (3, date(2026, 8, 13), "17:40"),
    (3, date(2026, 8, 21), "17:40"),
    (3, date(2026, 8, 24), "17:40"),
]

# ── Program versiyonunu geriye taşı: emp_id → yeni effective_from ───────────
# Şablon personelin MEVCUT en erken versiyonundan kopyalanır.
PROGRAM_GERI = {3: date(2026, 8, 3), 4: date(2026, 8, 3),
                5: date(2026, 8, 3), 6: date(2026, 8, 3)}

# ── İzin kayıtları (emp_id, tür, başlangıç, bitiş, not) ─────────────────────
IZINLER = [
    (2, "yillik", date(2026, 8, 25), date(2026, 8, 28),
     "Yıllık izin — puantaj düzeltmesinde girildi (01.09.2026)"),
]

# ── Ayrılışlar: emp_id → (ayrılış tarihi, pasifleştir?) ─────────────────────
AYRILISLAR = {5: (date(2026, 8, 14), True),      # Müyesser Üner
              3: (date(2026, 8, 24), True)}      # Berkan Yıldız (zaten pasif)


def utc_of(d: date, hm: str) -> datetime:
    h, m = hm.split(":")
    return datetime(d.year, d.month, d.day, int(h), int(m)) - TR_OFFSET


def main() -> int:
    db = SessionLocal()
    n_off = n_ev = n_sched = n_leave = n_emp = 0
    skipped = []
    try:
        emps = {e.id: e for e in db.query(Employee).all()}
        need = set(x[0] for x in DENEME) | set(x[0] for x in TAM_GUN) \
            | set(x[0] for x in SADECE_CIKIS) | set(PROGRAM_GERI) \
            | set(x[0] for x in IZINLER) | set(AYRILISLAR)
        missing = need - set(emps)
        if missing:
            print(f"✖ Personel kaydı bulunamadı: {sorted(missing)} — İPTAL")
            return 1

        # ── 1) Deneme basımlarını pasifleştir ──────────────────────────────
        print("\n① Deneme basımları (pasifleştirilecek)")
        for eid, d, hm, typ in DENEME:
            ts = utc_of(d, hm)
            row = (db.query(AttendanceEvent)
                   .filter(AttendanceEvent.employee_id == eid,
                           AttendanceEvent.event_type == typ,
                           AttendanceEvent.ts_utc >= ts,
                           AttendanceEvent.ts_utc < ts + timedelta(minutes=1))
                   .order_by(AttendanceEvent.ts_utc).first())
            if row is None:
                skipped.append(f"deneme yok: {emps[eid].full_name} "
                               f"{d:%d.%m} {hm} {typ}")
                continue
            if not row.is_active:
                skipped.append(f"deneme zaten pasif: {emps[eid].full_name} "
                               f"{d:%d.%m} {hm} {typ}")
                continue
            n_off += 1
            print(f"   − {emps[eid].full_name:20} {d:%d.%m} {hm} {typ:3} "
                  f"(id {row.id})")
            if COMMIT:
                row.is_active = False
                row.corrected_by = "sistem"
                row.corrected_at = datetime.utcnow()
                row.correction_note = f"Deneme basımı — {NOTE}"

        # ── 2) Olayları ekle ───────────────────────────────────────────────
        print("\n② Eklenen giriş/çıkış olayları")

        def add_event(eid, d, hm, typ, why):
            nonlocal n_ev
            ts = utc_of(d, hm)
            dup = (db.query(AttendanceEvent.id)
                   .filter(AttendanceEvent.employee_id == eid,
                           AttendanceEvent.event_type == typ,
                           AttendanceEvent.ts_utc == ts,
                           AttendanceEvent.is_active == True).first())   # noqa: E712
            if dup:
                skipped.append(f"olay zaten var: {emps[eid].full_name} "
                               f"{d:%d.%m} {hm} {typ}")
                return
            n_ev += 1
            print(f"   + {emps[eid].full_name:20} {d:%d.%m} {hm} {typ:3}  {why}")
            if COMMIT:
                db.add(AttendanceEvent(
                    employee_id=eid, event_type=typ, ts_utc=ts, work_date=d,
                    source="manual", created_by_user_id=ACTOR_USER_ID,
                    correction_note=f"{why} — {NOTE}"[:300]))

        for eid, d, i_hm, o_hm in TAM_GUN:
            add_event(eid, d, i_hm, "in", "gün eksikti, tam gün eklendi")
            add_event(eid, d, o_hm, "out", "gün eksikti, tam gün eklendi")
        for eid, d, o_hm in SADECE_CIKIS:
            add_event(eid, d, o_hm, "out", "çıkış basılmamıştı")

        # ── 3) Program versiyonlarını geriye taşı ──────────────────────────
        print("\n③ Program versiyonu (geriye dönük kopya)")
        for eid, eff in PROGRAM_GERI.items():
            exists = (db.query(EmployeeSchedule.id)
                      .filter(EmployeeSchedule.employee_id == eid,
                              EmployeeSchedule.effective_from == eff).first())
            if exists:
                skipped.append(f"program zaten var: {emps[eid].full_name} {eff}")
                continue
            src = (db.query(EmployeeSchedule)
                   .filter(EmployeeSchedule.employee_id == eid)
                   .order_by(EmployeeSchedule.effective_from).first())
            if src is None:
                skipped.append(f"kopyalanacak program yok: {emps[eid].full_name}")
                continue
            n_sched += 1
            print(f"   + {emps[eid].full_name:20} {eff} ← "
                  f"{src.effective_from} kopyası")
            if COMMIT:
                db.add(EmployeeSchedule(
                    employee_id=eid, effective_from=eff,
                    weekly_template=src.weekly_template,
                    lunch_break_minutes=src.lunch_break_minutes,
                    created_by=f"sistem — {NOTE}"[:100]))

        # ── 4) İzin kayıtları ──────────────────────────────────────────────
        print("\n④ İzin kayıtları")
        for eid, typ, d1, d2, note in IZINLER:
            dup = (db.query(LeaveRecord.id)
                   .filter(LeaveRecord.employee_id == eid,
                           LeaveRecord.start_date == d1,
                           LeaveRecord.end_date == d2,
                           LeaveRecord.is_active == True).first())       # noqa: E712
            if dup:
                skipped.append(f"izin zaten var: {emps[eid].full_name} {d1}–{d2}")
                continue
            n_leave += 1
            print(f"   + {emps[eid].full_name:20} {typ} {d1:%d.%m}–{d2:%d.%m}")
            if COMMIT:
                db.add(LeaveRecord(employee_id=eid, leave_type=typ,
                                   start_date=d1, end_date=d2, note=note,
                                   created_by="sistem", is_active=True))

        # ── 5) Ayrılış tarihleri ───────────────────────────────────────────
        print("\n⑤ Ayrılış tarihleri")
        for eid, (end, deactivate) in AYRILISLAR.items():
            e = emps[eid]
            if e.end_date == end and (not deactivate or not e.is_active):
                skipped.append(f"ayrılış zaten yazılı: {e.full_name} {end}")
                continue
            n_emp += 1
            print(f"   ~ {e.full_name:20} ayrılış {end:%d.%m.%Y}"
                  f"{' + pasifleştirildi' if deactivate and e.is_active else ''}")
            if COMMIT:
                e.end_date = end
                if deactivate:
                    e.is_active = False

        if COMMIT:
            db.commit()
    except Exception as exc:                                   # pragma: no cover
        db.rollback()
        print(f"\n✖ HATA — hiçbir şey yazılmadı: {exc}")
        return 1
    finally:
        db.close()

    print(f"\nÖzet: {n_off} deneme pasif · {n_ev} olay · {n_sched} program · "
          f"{n_leave} izin · {n_emp} personel")
    for s in skipped:
        print(f"  · atlandı — {s}")
    print("\n✓ YAZILDI." if COMMIT
          else "\nKURU ÇALIŞTIRMA — yazmak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
