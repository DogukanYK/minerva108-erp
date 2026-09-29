"""Ofis mesaisi 09:00–18:00 — 23.09.2026'dan geçerli program versiyonları.

NEDEN
─────
Ofis çalışma saatleri 23.09.2026'da 08:30–17:45'ten 09:00–18:00'e geçti
(patron, 29.09.2026).  Aynı kararla mola kesintisi de kalktı — o kısım kodda
(`core/pdks.BREAK_REGIMES`), bu script yalnız PROGRAMLARI taşır.

NE YAPAR
────────
Her personel için:
  1. 23.09'dan ÖNCE başlayan geçerli versiyonda 08:30–17:45 gün varsa aynı
     haftalık düzenle YENİ versiyon ekler (effective_from = 23.09.2026).
  2. 23.09 veya SONRASI tarihli versiyonlarda (UI'dan eski varsayılanla
     girilmiş yeni personel vb.) 08:30–17:45 günleri YERİNDE düzeltir —
     router'daki `upsert_schedule` ile aynı kural (aynı effective_from = aynı
     versiyonun düzeltmesi).  Eski şablon çıktıya yazılır (geri alma izi).
Gün kuralı:
  • 08:30–17:45 → 09:00–18:00
  • zaten 09:00–18:00 / hafta tatili → aynen kalır
  • başka saatli gün (yarı zamanlı vb.) → DOKUNULMAZ, raporda listelenir
23.09 öncesi versiyonlara DOKUNMAZ — 22.09 ve öncesinin puantajı aynen kalır.

Atlananlar (raporda gerekçesiyle): 23.09'dan önce ayrılmış personel; hiç
programı olmayan personel.  Hiç çalışma günü okunamayan (bozuk/boş) şablon
değiştirilmez, ELLE BAKILACAK listesine yazılır.

İdempotent: ikinci çalıştırmada her şey "zaten güncel" çıkar.

KULLANIM (prod'da)
──────────────────
    venv/bin/python scripts/pdks_mesai_0900_1800_20260923.py            # kuru
    venv/bin/python scripts/pdks_mesai_0900_1800_20260923.py --commit   # yaz
"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.pdks import WEEKDAY_LABELS, parse_template               # noqa: E402
from database import Employee, EmployeeSchedule, SessionLocal      # noqa: E402

COMMIT = "--commit" in sys.argv
EFFECTIVE = date(2026, 9, 23)
OLD = ("08:30", "17:45")
NEW = ("09:00", "18:00")
ACTOR = "sistem — mesai 09:00–18:00 (23.09.2026)"


def _migrate(raw):
    """(yeni şablon | None, değişen gün sayısı, standart dışı günler).

    Şablonda hiç çalışma günü okunamıyorsa (bozuk/boş) None döner — çağıran
    DOKUNMAZ, elle bakılsın diye raporlar."""
    tpl = parse_template(raw)
    if not any(tpl.values()):
        return None, 0, []
    out, changed, odd = {}, 0, []
    for wd in range(7):
        v = tpl.get(wd)
        if not v:
            out[str(wd)] = None
            continue
        hours = (v["start"], v["end"])
        if hours == OLD:
            out[str(wd)] = {"start": NEW[0], "end": NEW[1]}
            changed += 1
        else:
            out[str(wd)] = {"start": v["start"], "end": v["end"]}
            if hours != NEW:
                odd.append(f"{WEEKDAY_LABELS[wd]} {v['start']}–{v['end']}")
    return out, changed, odd


def main() -> int:
    db = SessionLocal()
    added = fixed = current = skipped = 0
    notes = []
    try:
        emps = db.query(Employee).order_by(Employee.full_name).all()
        print(f"Personel: {len(emps)} · geçerlilik {EFFECTIVE:%d.%m.%Y} · "
              f"{OLD[0]}–{OLD[1]} → {NEW[0]}–{NEW[1]}\n")
        for e in emps:
            tag = f"{e.full_name:24}"
            pasif = "" if e.is_active else "  [pasif kart]"
            if e.end_date and e.end_date < EFFECTIVE:
                print(f"   · {tag} atlandı — {e.end_date:%d.%m.%Y}'de ayrılmış")
                skipped += 1
                continue
            vers = (db.query(EmployeeSchedule)
                    .filter(EmployeeSchedule.employee_id == e.id)
                    .order_by(EmployeeSchedule.effective_from).all())
            if not vers:
                print(f"   ⚠ {tag} atlandı — hiç programı yok")
                notes.append(f"{e.full_name}: programı yok")
                skipped += 1
                continue
            before = [v for v in vers if v.effective_from < EFFECTIVE]
            from_eff = [v for v in vers if v.effective_from >= EFFECTIVE]
            touched = flagged = False

            # 1) 23.09 öncesi geçerli versiyon → 23.09'da yeni versiyon
            #    (23.09 tarihli bir versiyon zaten varsa o, 2. adımda düzeltilir)
            has_eff_row = any(v.effective_from == EFFECTIVE for v in from_eff)
            if before and not has_eff_row:
                base = before[-1]
                new_tpl, n, odd = _migrate(base.weekly_template)
                if new_tpl is None:
                    print(f"   ⚠ {tag} {base.effective_from:%d.%m.%Y} versiyonunda "
                          f"çalışma günü okunamadı — dokunulmadı")
                    notes.append(f"{e.full_name}: bozuk/boş şablon "
                                 f"({base.effective_from:%d.%m.%Y})")
                    flagged = True
                elif n:
                    touched = True
                    print(f"   + {tag} {n} gün → {NEW[0]}–{NEW[1]} "
                          f"(önceki versiyon {base.effective_from:%d.%m.%Y}){pasif}"
                          + (f"  · dokunulmadı: {', '.join(odd)}" if odd else ""))
                    if odd:
                        notes.append(f"{e.full_name}: standart dışı — {', '.join(odd)}")
                    if COMMIT:
                        db.add(EmployeeSchedule(
                            employee_id=e.id, effective_from=EFFECTIVE,
                            weekly_template=json.dumps(new_tpl, ensure_ascii=False),
                            lunch_break_minutes=base.lunch_break_minutes,
                            created_by=ACTOR[:100]))
                    added += 1
                elif odd:
                    notes.append(f"{e.full_name}: standart dışı — {', '.join(odd)}")

            # 2) 23.09 ve sonrası tarihli versiyonlar → yerinde düzeltme
            for v in from_eff:
                new_tpl, n, odd = _migrate(v.weekly_template)
                if new_tpl is None:
                    print(f"   ⚠ {tag} {v.effective_from:%d.%m.%Y} versiyonunda "
                          f"çalışma günü okunamadı — dokunulmadı")
                    notes.append(f"{e.full_name}: bozuk/boş şablon "
                                 f"({v.effective_from:%d.%m.%Y})")
                    flagged = True
                    continue
                if odd:
                    notes.append(f"{e.full_name}: standart dışı "
                                 f"({v.effective_from:%d.%m.%Y}) — {', '.join(odd)}")
                if not n:
                    continue
                touched = True
                print(f"   ~ {tag} {v.effective_from:%d.%m.%Y} versiyonu yerinde: "
                      f"{n} gün → {NEW[0]}–{NEW[1]}{pasif}")
                print(f"       eski şablon: {v.weekly_template}")
                if COMMIT:
                    v.weekly_template = json.dumps(new_tpl, ensure_ascii=False)
                    v.created_by = ACTOR[:100]
                fixed += 1

            if not touched and not flagged:
                print(f"   · {tag} zaten güncel")
                current += 1
        if COMMIT:
            db.commit()
    except Exception as exc:                                   # pragma: no cover
        db.rollback()
        print(f"\n✖ HATA — geri alındı: {exc}")
        return 1
    finally:
        db.close()

    print("\n" + "═" * 72)
    print(f"Yeni versiyon: {added} · yerinde düzeltilen: {fixed} · "
          f"zaten güncel: {current} · atlanan: {skipped}")
    if notes:
        print("\n⚠ ELLE BAKILACAK:")
        for n in dict.fromkeys(notes):
            print(f"   · {n}")
    print("\n✓ YAZILDI." if COMMIT else "\nKURU ÇALIŞTIRMA — yazmak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
