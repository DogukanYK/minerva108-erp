# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
PDKS hesap motoru — SAF fonksiyonlar (DB session/query yok).

Router satırları yükler, buradaki fonksiyonlara düz veri (dict/datetime) verir,
sonucu serialize eder.  Bu ayrım hesap mantığını DB fixture'sız unit-test
edilebilir kılar.

Veri sözleşmeleri:
  schedule versiyonu: {"effective_from": date, "template": {int hafta_günü:
      {"start": "09:00", "end": "18:00"} | None}, "lunch_break_minutes": int}
  olay: {"id": int|None, "event_type": "in"|"out", "ts_utc": datetime,
      "source": str}
  tatil: {"name": str, "is_half_day": bool}

Kurallar (tek kaynak — UI ve Excel bu modülün çıktısını gösterir):
  • Çalışma = kapalı in→out çiftlerinin UTC farkları toplamı (gece yarısını
    aşan çift doğal olarak doğru hesaplanır — fark UTC'de alınır).
  • Molalar ŞİRKET GENELİ ve TARİHE BAĞLI (BREAK_REGIMES): 23.09.2026'ya
    kadar 09:30/15 · 12:45/45 · 16:00/15 dk, o günden itibaren 12:30/60 dk.
    Gün TEK kapalı çiftten oluşuyorsa, o çiftin TR saat penceresine düşen
    molalar düşülür (kısmi kesişim orantılı — yarım gün çalışandan tam günün
    molası düşmez).  Çoklu çift = personel molada çıkış basmış, kesinti
    yapılmaz.  Programsız günlerde (hafta tatili/izin) kesinti yok.
  • Beklenen süre = program aralığı − o aralığa düşen molalar (iş günü);
    izin/tam tatil/programsız gün = 0; yarım gün resmi tatilde yarısı.
    Standart mesai 23.09.2026'dan itibaren 09:00–18:00 → 540 − 60 = net
    480 dk (8 saat); öncesi 08:30–17:45 → 555 − 75 = net 480 dk (8 saat).
  • Fazla mesai = max(0, çalışılan − beklenen) → tatil/izin/hafta tatilinde
    çalışılan her dakika mesaidir.
  • Geç gelme / erken çıkma BAYRAĞI YOK (bilinçli karar): saatler dakika
    dakika kaydedilir ama kimse "geç geldi" diye işaretlenmez; puantajda
    giriş-çıkış saati, toplam çalışma, fazla mesai ve eksik süre görünür.
  • Açık çift (çıkış unutulmuş) 0 dakika sayılır ve missing_checkout bayrağı
    kalkar — yönetici düzeltene kadar toplamlara girmez (bilinçli zorlayıcı).
  • Gün durumu önceliği: resmi_tatil > izinli > hafta_tatili > eksik_cikis >
    calisti > devamsiz.  Tatil/izin günü çalışma durumu değiştirmez, süre +
    mesai yine görünür.
"""
import hashlib
import hmac
import json
import math
from calendar import monthrange
from datetime import date, datetime, timedelta

from database import TR_OFFSET, to_tr

# ── Şirket geneli mola şeması — TARİHE BAĞLI (TR duvar saati) ───────────────
# Molalar herkes için aynı; kişi bazlı "öğle molası (dk)" girişi KALDIRILDI.
# Program versiyonlarındaki `lunch_break_minutes` kolonu DB'de KORUNUR (eski
# kayıtların bilgisi kaybolmasın) ama hesapta artık kullanılmaz.
#
# 23.09.2026'ya kadar 75 dk sabit mola düşülüyordu.  O günden itibaren mesai
# 09:00–18:00 ve TEK 60 dk öğle arası düşülür → net 8 saat (patron kararı,
# 2026-10-01: "8 saat olarak değiştir"; 29.09'da önce "molaları hiç düşme"
# denmiş ve 23.09 şeması boş girilmişti — aynı kararın düzeltmesi olduğu için
# o satır yerinde düzeltildi).  Şema tarihe bağlı tutulur ki kapanmış puantaj
# ayları geriye dönük değişmesin: YENİ bir mola kararı = BREAK_REGIMES'e yeni
# satır eklemek, eskisini silmek/düzenlemek DEĞİL.
LEGACY_BREAKS = (
    {"start": "09:30", "minutes": 15, "label": "Kahvaltı"},
    {"start": "12:45", "minutes": 45, "label": "Öğle yemeği"},
    {"start": "16:00", "minutes": 15, "label": "Mola"},
)
BREAK_REGIMES = (                     # (bu günden itibaren, şema) — artan tarih
    (date.min, LEGACY_BREAKS),
    (date(2026, 9, 23), ({"start": "12:30", "minutes": 60, "label": "Öğle arası"},)),
)
# Güncel şema — UI'nın gösterdiği, tarih verilmeyen çağrıların kullandığı.
BREAKS = BREAK_REGIMES[-1][1]
# Standart mesai — yeni program varsayılanı (09:00–18:00 = 540 dk, 60 dk
# öğle arası → net 8 saat).  23.09.2026 öncesi 08:30–17:45 idi.
DEFAULT_WORK_START = "09:00"
DEFAULT_WORK_END = "18:00"

# Geriye dönük: eski kayıtlarda tek 'öğle molası' alanı vardı.  Artık
# kullanılmıyor; sabit tutuluyor ki eski import/testler kırılmasın.
LUNCH_AUTO_DEDUCT_MIN_MINUTES = 360
# Bir 'out' bu süreden (saat) eski bir açık 'in'i kapatmaz — router'ın
# work_date atama kuralı da aynı sabiti kullanır.
OPEN_PAIR_MAX_HOURS = 16
# ÖNCEKİ güne ait ve bu kadar eski bir açık 'in' artık "içerideyim" değil,
# UNUTULMUŞ ÇIKIŞ sayılır.  Gece vardiyası (20:00→05:00 ≈ 9 sa) bu eşiğin
# altında kaldığı için bozulmaz; akşam girip çıkışı unutan personel ise
# ertesi sabah dünkü güne 15 saatlik hayalî mesai yazdırmaz.
FORGOTTEN_AFTER_HOURS = 12

LEAVE_TYPES = ("yillik", "raporlu", "ucretsiz", "diger")

LEAVE_TYPE_LABELS = {
    "yillik":   "Yıllık İzin",
    "raporlu":  "Raporlu",
    "ucretsiz": "Ücretsiz İzin",
    "diger":    "Diğer",
}

DAY_STATUS_LABELS = {
    "calisti":      "Çalıştı",
    "devamsiz":     "Devamsız",
    "eksik_cikis":  "Çıkış eksik",
    "izinli":       "İzinli",
    "hafta_tatili": "Hafta tatili",
    # Program versiyonu HENÜZ tanımlı olmayan gün — "hafta tatili" DEĞİL.
    # İkisini ayırmazsak beklenen süre bilinmediği hâlde 0 sayılır ve o gün
    # çalışılan sürenin TAMAMI fazla mesai olarak yazılır (gerçek bir hataydı).
    "programsiz":   "Program tanımsız",
    "baslamadi":    "İşe başlamadı",
    "ayrildi":      "İşten ayrıldı",
    "resmi_tatil":  "Resmi tatil",
    "bekliyor":     "Bekliyor",
}

# 0=Pazartesi … 6=Pazar (date.weekday() sırası)
WEEKDAY_LABELS = ("Pazartesi", "Salı", "Çarşamba", "Perşembe",
                  "Cuma", "Cumartesi", "Pazar")
# 1-indexli ay adları (MONTH_LABELS[1] = Ocak)
MONTH_LABELS = ("", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
                "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık")


# ─── Küçük yardımcılar ───────────────────────────────────────────────────────

def tr_date_of(ts_utc: datetime) -> date:
    """UTC zaman damgasının TR-yerel takvim günü — work_date yazım kuralı."""
    return (ts_utc + TR_OFFSET).date()


def fmt_minutes(m) -> str:
    """375 → '6 sa 15 dk' (0 → '—'). UI + Excel ortak biçimi."""
    if not m:
        return "—"
    m = int(m)
    h, mm = divmod(m, 60)
    if h and mm:
        return f"{h} sa {mm} dk"
    if h:
        return f"{h} sa"
    return f"{mm} dk"


def _hm_to_minutes(hm: str) -> int:
    """'09:30' → 570.  Bozuk değerde ValueError fırlatır (doğrulama router'da)."""
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def parse_template(raw):
    """weekly_template JSON'ını {int gün: {'start','end'}|None} sözlüğüne çevir.

    Anahtarlar date.weekday() (0=Pazartesi … 6=Pazar).  Eksik anahtar = o gün
    çalışma yok (None ile aynı).  Bozuk JSON → boş şablon (hiç çalışma günü yok)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        try:
            wd = int(k)
        except (TypeError, ValueError):
            continue
        if 0 <= wd <= 6:
            out[wd] = v if (isinstance(v, dict) and v.get("start") and v.get("end")) else None
    return out


def validate_template(raw):
    """Program kaydı öncesi doğrulama.  Hata → Türkçe mesaj str, geçerli → None."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return "Program şablonu geçersiz (JSON okunamadı)."
    if not isinstance(raw, dict):
        return "Program şablonu geçersiz."
    any_workday = False
    for k, v in raw.items():
        try:
            wd = int(k)
        except (TypeError, ValueError):
            return f"Geçersiz gün anahtarı: {k}"
        if not (0 <= wd <= 6):
            return f"Geçersiz gün anahtarı: {k}"
        if v is None:
            continue
        if not isinstance(v, dict) or not v.get("start") or not v.get("end"):
            return "Çalışma günü için başlangıç ve bitiş saati zorunlu."
        try:
            s, e = _hm_to_minutes(v["start"]), _hm_to_minutes(v["end"])
        except (ValueError, AttributeError):
            return "Saat biçimi geçersiz (SS:DD bekleniyor)."
        if not (0 <= s < 1440 and 0 < e <= 1440):
            return "Saat 00:00–24:00 aralığında olmalı."
        if e <= s:
            return "Bitiş saati başlangıçtan sonra olmalı (gece vardiyası v1'de desteklenmiyor)."
        any_workday = True
    if not any_workday:
        return "En az bir çalışma günü tanımlanmalı."
    return None


# ─── Mola ────────────────────────────────────────────────────────────────────

def breaks_for(work_date) -> tuple:
    """work_date günü geçerli mola şeması (BREAK_REGIMES)."""
    scheme = BREAK_REGIMES[0][1]
    for since, regime in BREAK_REGIMES:
        if work_date >= since:
            scheme = regime
    return scheme


def break_minutes_within(start_hm: str, end_hm: str, work_date=None) -> int:
    """[start, end) penceresine düşen toplam mola dakikası.

    Kısmi kesişim orantılı sayılır — 08:30–13:00 çalışan kahvaltının tamamını
    (15) + öğlenin ilk 15 dakikasını görür.  Böylece yarım gün çalışandan tam
    günün molası düşülmez.  Gece yarısını aşan çift (end <= start) için 0 —
    mola şeması gündüz mesaisi içindir, gece vardiyasına uydurulmaz.
    work_date: şemanın seçileceği gün (puantaj hesabı DAİMA verir); None →
    güncel şema."""
    s = _hm_to_minutes(start_hm)
    e = _hm_to_minutes(end_hm)
    if e <= s:
        return 0
    total = 0
    for b in (BREAKS if work_date is None else breaks_for(work_date)):
        bs = _hm_to_minutes(b["start"])
        be = bs + int(b["minutes"])
        total += max(0, min(e, be) - max(s, bs))
    return total


def breaks_view() -> list:
    """UI/rapor için GÜNCEL mola şeması — bitiş saati hesaplanmış hâlde."""
    out = []
    for b in BREAKS:
        bs = _hm_to_minutes(b["start"])
        be = bs + int(b["minutes"])
        out.append({"start": b["start"], "end": f"{be // 60:02d}:{be % 60:02d}",
                    "minutes": int(b["minutes"]), "label": b["label"]})
    return out


TOTAL_BREAK_MINUTES = sum(int(b["minutes"]) for b in BREAKS)


# ─── Program çözümleme ───────────────────────────────────────────────────────

def schedule_for(schedules, work_date):
    """work_date için geçerli program versiyonunun O GÜNKÜ girdisini döndür.

    schedules: parse edilmiş versiyon listesi (sıra önemsiz).
    Dönüş: {"start": "09:00", "end": "18:00", "break_minutes": 60,
            "span_minutes": 540} — ya da None (o gün çalışma yok / geçerli
    versiyon yok).  `break_minutes` o günün mola şemasından (breaks_for)
    türer, programın eski `lunch_break_minutes` alanından DEĞİL."""
    best = None
    for s in schedules or []:
        ef = s.get("effective_from")
        if ef is None or ef > work_date:
            continue
        if best is None or ef > best.get("effective_from"):
            best = s
    if not best:
        return None
    day = parse_template(best.get("template")).get(work_date.weekday())
    if not day:
        return None
    start_m = _hm_to_minutes(day["start"])
    end_m = _hm_to_minutes(day["end"])
    return {
        "start": day["start"],
        "end": day["end"],
        "break_minutes": break_minutes_within(day["start"], day["end"], work_date),
        "span_minutes": max(0, end_m - start_m),
    }


def has_schedule_version(schedules, work_date) -> bool:
    """work_date'i kapsayan BİR program versiyonu var mı?

    `schedule_for` iki ayrı durumda da None döner:
      (a) o tarihe geçerli program versiyonu HİÇ yok → beklenen süre BİLİNMİYOR
      (b) versiyon var ama o gün boş bırakılmış → gerçek hafta tatili, beklenen 0
    Bu ayrım olmadan (a) da hafta tatili sayılıyor ve o gün çalışılan sürenin
    tamamı fazla mesaiye yazılıyordu.
    """
    return any(s.get("effective_from") is not None and s["effective_from"] <= work_date
               for s in schedules or [])


def open_in_still_valid(in_ts_utc, in_work_date, now_utc) -> bool:
    """Açık bir 'in' olayı hâlâ "içerideyim" sayılmalı mı?

    İki koşul birden gerekir:
      • 16 saatten (OPEN_PAIR_MAX_HOURS) eski olmayacak, VE
      • ÖNCEKİ bir güne aitse 12 saatten (FORGOTTEN_AFTER_HOURS) eski olmayacak.

    İkinci kural unutulmuş çıkış içindir: akşam 17:00'de girip çıkışı unutan
    personel ertesi sabah 08:30'da uygulamayı açtığında sistem onu hâlâ
    "içeride" sayarsa, bastığı çıkış dünkü güne 15 saatlik hayalî bir mesai
    yazar.  Bu kuralla dünkü gün "Çıkış eksik" olarak açık kalır (yönetici
    düzeltir) ve personel bugüne temiz bir giriş yapar.

    Gece vardiyası korunur: 20:00→05:00 arası 9 saat, 12 saatlik eşiğin
    altında olduğu için çift normal şekilde kapanır."""
    delta = now_utc - in_ts_utc
    if not (timedelta(0) <= delta < timedelta(hours=OPEN_PAIR_MAX_HOURS)):
        return False
    if (in_work_date is not None
            and in_work_date < tr_date_of(now_utc)
            and delta >= timedelta(hours=FORGOTTEN_AFTER_HOURS)):
        return False
    return True


# ─── Olay eşleme + gün hesabı ────────────────────────────────────────────────

def pair_events(events):
    """ts_utc sıralı ardışık in→out eşlemesi.

    Dönüş: [{"in": olay|None, "out": olay|None, "minutes": int|None}].
    Sonda kapanmamış 'in' → açık çift (out=None).  Yetim 'out' (manuel giriş
    kaynaklı olabilir) → in=None anomali çifti.  Peş peşe iki 'in' → ilki açık
    çift olarak kapanır (0 dk), ikincisi yeni çift başlatır."""
    pairs = []
    open_in = None
    for e in sorted(events or [], key=lambda x: x["ts_utc"]):
        if e["event_type"] == "in":
            if open_in is not None:
                pairs.append({"in": open_in, "out": None, "minutes": None})
            open_in = e
        else:  # out
            if open_in is not None:
                mins = int((e["ts_utc"] - open_in["ts_utc"]).total_seconds() // 60)
                pairs.append({"in": open_in, "out": e, "minutes": max(0, mins)})
                open_in = None
            else:
                pairs.append({"in": None, "out": e, "minutes": None})
    if open_in is not None:
        pairs.append({"in": open_in, "out": None, "minutes": None})
    return pairs


def compute_day(work_date, events, day_schedule, leave_type=None, holiday=None,
                leave=None, unscheduled=False, started_on=None, left_on=None):
    """Bir personelin TEK gününü hesapla.

    events: o work_date'e yazılmış aktif olaylar (dict listesi).
    day_schedule: schedule_for() çıktısı ya da None.
    leave_type: o günü kapsayan izin türü ya da None.
    holiday: {"name", "is_half_day"} ya da None.
    unscheduled: o tarihi kapsayan program versiyonu YOK (bkz.
    `has_schedule_version`).  Bu durumda beklenen süre bilinmediği için
    fazla mesai / eksik süre HESAPLANMAZ — çalışılan süre yine gösterilir.
    started_on / left_on: istihdam penceresi (ikisi de DAHİL, None = sınırsız).
    Pencere dışındaki gün 'işe başlamadı' / 'işten ayrıldı'dır: beklenen süre
    0, devamsızlık YOK.  `is_active` bunun yerine geçmez — pasif kart aylık
    raporda ayın kalanını devamsız yazıyordu ve ayrılanın son ay bordrosu
    yanlış çıkıyordu.  Pencere dışına düşmüş OLAY yine gösterilir (anomali).
    """
    # `leave` verilirse tür ondan türetilir (not + belge no da taşınır);
    # `leave_type=` eski çağrılar için korunuyor.
    if leave:
        leave_type = leave.get("leave_type") or leave_type
    off_roster = ((started_on is not None and work_date < started_on)
                  or (left_on is not None and work_date > left_on))
    pairs = pair_events(events)
    closed = [p for p in pairs if p["in"] and p["out"]]
    missing_checkout = any(p["in"] and not p["out"] for p in pairs)
    orphan_out = any(p["out"] and not p["in"] for p in pairs)

    gross = sum(p["minutes"] for p in closed)
    break_deducted = False
    break_minutes = 0
    worked = gross
    # Tek kapalı çift = personel molada çıkış basmamış → o pencereye düşen
    # sabit molalar düşülür.  Çoklu çift = molalar zaten fiilen basılmış,
    # ikinci kez düşmek çifte kesinti olurdu.
    if day_schedule and len(closed) == 1 and not missing_checkout:
        p0 = closed[0]
        break_minutes = break_minutes_within(
            to_tr(p0["in"]["ts_utc"]).strftime("%H:%M"),
            to_tr(p0["out"]["ts_utc"]).strftime("%H:%M"), work_date)
        worked = max(0, gross - break_minutes)
        break_deducted = break_minutes > 0

    # Beklenen süre
    half_day_holiday = bool(holiday and holiday.get("is_half_day"))
    full_holiday = bool(holiday and not half_day_holiday)
    if off_roster or leave_type or full_holiday or not day_schedule:
        expected = 0
    else:
        expected = max(0, day_schedule["span_minutes"] - day_schedule["break_minutes"])
        if half_day_holiday:
            expected //= 2

    overtime = max(0, worked - expected)
    # Eksik süre: çıkış eksikse hesaplanamaz (gün zaten toplam dışı bayraklı).
    missing = 0 if missing_checkout else max(0, expected - worked)
    if unscheduled or off_roster:
        # Beklenen süre bilinmiyor (program yok) ya da o gün istihdam penceresi
        # dışında → ne fazla mesai ne eksik yazılabilir.  (Aksi hâlde o gün
        # çalışılan sürenin tamamı fazla mesai olurdu.)
        overtime = missing = 0

    first_in = min((p["in"]["ts_utc"] for p in pairs if p["in"]), default=None)
    last_out = max((p["out"]["ts_utc"] for p in pairs if p["out"]), default=None)

    # Durum — öncelik sırası sabit (modül docstring'i).  İstihdam penceresi
    # EN ÜSTTE: ayrılmış personelin günü tatil/izin/devamsız değildir.
    if off_roster:
        status = "baslamadi" if (started_on is not None
                                 and work_date < started_on) else "ayrildi"
    elif holiday:
        status = "resmi_tatil"
    elif leave_type:
        status = "izinli"
    elif unscheduled:
        status = "programsiz"
    elif not day_schedule:
        status = "hafta_tatili"
    elif missing_checkout:
        status = "eksik_cikis"
    elif closed:
        status = "calisti"
    else:
        status = "devamsiz"

    return {
        "date": work_date,
        "status": status,
        "status_label": (LEAVE_TYPE_LABELS.get(leave_type, "İzinli")
                         if status == "izinli" else DAY_STATUS_LABELS[status]),
        "leave_type": leave_type,
        "leave_note": (leave or {}).get("note") or "",
        "leave_document_no": (leave or {}).get("document_no") or "",
        "off_roster": off_roster,
        "holiday_name": holiday.get("name") if holiday else None,
        "first_in": first_in,
        "last_out": last_out,
        "worked_minutes": worked,
        "expected_minutes": expected,
        "overtime_minutes": overtime,
        "missing_minutes": missing,
        "missing_checkout": missing_checkout,
        "orphan_out": orphan_out,
        "break_deducted": break_deducted,
        "break_minutes": break_minutes,
        "pairs": pairs,
    }


def leave_for(leaves, work_date):
    """work_date'i kapsayan ilk aktif iznin TÜRÜ (yoksa None)."""
    lv = leave_detail_for(leaves, work_date)
    return lv["leave_type"] if lv else None


def leave_detail_for(leaves, work_date):
    """work_date'i kapsayan ilk aktif iznin TAMAMI (yoksa None) — türün yanı
    sıra not ve belge no da lazım (raporun e-rapor numarası puantajda görünsün)."""
    for lv in leaves or []:
        if lv["start_date"] <= work_date <= lv["end_date"]:
            return lv
    return None


def compute_month(year, month, schedules, events_by_date, leaves, holidays,
                  today_tr, started_on=None, left_on=None):
    """Bir personelin bir ayını hesapla.

    events_by_date: {date: [olay]}   holidays: {date: {"name","is_half_day"}}
    today_tr: bugünün TR-yerel tarihi — sonraki günler 'bekliyor' olur ve
    toplamlara girmez.
    started_on / left_on: istihdam penceresi (bkz. compute_day) — dışındaki
    günler devamsızlığa, eksik süreye ve izin sayacına GİRMEZ.

    izin_gunleri sayacı yalnız PROGRAMLI iş gününe denk gelen izin günlerini
    sayar (hafta tatiline taşan izin, izin hakkından gün düşürmez); görüntü
    önceliği yine izinli'dir.
    """
    _, ndays = monthrange(year, month)
    days = []
    totals = {
        "toplam_calisma": 0, "toplam_fazla_mesai": 0, "toplam_eksik": 0,
        "devamsizlik_gun": 0, "eksik_cikis_sayisi": 0,
        "izin_gunleri": {k: 0 for k in LEAVE_TYPES},
    }
    for dnum in range(1, ndays + 1):
        d = date(year, month, dnum)
        if d > today_tr:
            days.append({
                "date": d, "status": "bekliyor",
                "status_label": DAY_STATUS_LABELS["bekliyor"],
                "leave_type": None, "leave_note": "", "leave_document_no": "",
                "holiday_name": None,
                "first_in": None, "last_out": None,
                "worked_minutes": 0, "expected_minutes": 0, "overtime_minutes": 0,
                "missing_minutes": 0,
                    "missing_checkout": False, "orphan_out": False,
                "break_deducted": False, "break_minutes": 0, "pairs": [],
                "off_roster": False,
            })
            continue
        sched = schedule_for(schedules, d)
        day = compute_day(
            d,
            (events_by_date or {}).get(d, []),
            sched,
            leave=leave_detail_for(leaves, d),
            holiday=(holidays or {}).get(d),
            unscheduled=not has_schedule_version(schedules, d),
            started_on=started_on, left_on=left_on,
        )
        days.append(day)
        totals["toplam_calisma"] += day["worked_minutes"]
        totals["toplam_fazla_mesai"] += day["overtime_minutes"]
        totals["toplam_eksik"] += day["missing_minutes"]
        if day["status"] == "devamsiz":
            totals["devamsizlik_gun"] += 1
        if day["missing_checkout"]:
            totals["eksik_cikis_sayisi"] += 1
        # Tam resmi tatile denk gelen izin günü izin hakkından düşmez
        # (İş Kanunu m.56 — yıllık izne rastlayan tatil izinden sayılmaz).
        hol = (holidays or {}).get(d)
        if (day["leave_type"] and sched and not day["off_roster"]
                and not (hol and not hol.get("is_half_day"))):
            totals["izin_gunleri"][day["leave_type"]] = \
                totals["izin_gunleri"].get(day["leave_type"], 0) + 1
    return {"year": year, "month": month, "days": days, "totals": totals}


# ─── Check-in doğrulama — saf fonksiyonlar ───────────────────────────────────
# Üçlü doğrulama (ofis ağı + konum + canlı QR) açıkken /api/pdks/check bu
# fonksiyonları kullanır.  Secret her zaman ÇAĞIRAN tarafından verilir
# (core.auth.SECRET_KEY) — burada env okunmaz, sessiz fallback secret'ı yok
# (core/drive.py'deki kalıbın aksine).

QR_BUCKET_SECONDS = 30           # QR ekranı bu sürede bir yenilenir
QR_PREFIX = "PDKSQR1"
QR_SIG_LEN = 16                  # hex karakter — kısa ama tahmin edilemez
_QR_DOMAIN = "pdks-qr:"          # HMAC domain-separation (core/drive.py kalıbı)

GEO_ACCURACY_CAP_M = 100.0       # tarayıcının bildirdiği "accuracy" bu değerle sınırlanır
EARTH_RADIUS_M = 6371000.0


def qr_bucket(now_unix) -> int:
    """Unix saniyeyi 30 sn'lik bir 'bucket' numarasına indirger — hem QR
    ekranı hem de doğrulayan aynı bucket'ı bağımsızca hesaplar."""
    return int(now_unix) // QR_BUCKET_SECONDS


def _qr_sig(secret: str, bucket: int) -> str:
    msg = f"{_QR_DOMAIN}{bucket}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:QR_SIG_LEN]


def make_qr_token(secret: str, bucket: int) -> str:
    """Kiosk ekranının gösterdiği QR metni — 'PDKSQR1:<bucket>:<imza>'."""
    return f"{QR_PREFIX}:{bucket}:{_qr_sig(secret, bucket)}"


def verify_qr_token(secret: str, token: str, now_unix, tolerance: int = 1) -> str:
    """QR token'ı doğrula.  Dönüş: 'ok' | 'expired' | 'invalid'.

    'expired': imza geçerli ama bucket şu anki penceden (±tolerance) daha eski
    — ekrandaki kod süresi dolmuş, personel eski bir ekran görüntüsü/fotoğraf
    kullanıyor olabilir.  'invalid': format bozuk ya da imza tutmuyor
    (sahte/başka secret'la üretilmiş token)."""
    if not token or not isinstance(token, str):
        return "invalid"
    # compare_digest ASCII olmayan str'de TypeError atar — personel yanlışlıkla
    # başka bir QR (ör. Türkçe metin) okutursa 500 değil düzgün hata dönmeli.
    if not token.isascii():
        return "invalid"
    parts = token.split(":")
    if len(parts) != 3 or parts[0] != QR_PREFIX:
        return "invalid"
    try:
        bucket = int(parts[1])
    except ValueError:
        return "invalid"
    if not hmac.compare_digest(_qr_sig(secret, bucket), parts[2]):
        return "invalid"
    if abs(qr_bucket(now_unix) - bucket) > tolerance:
        return "expired"
    return "ok"


# ─── Yedek sayısal kod (kamera çalışmadığında) ───────────────────────────────
# Kiosk ekranı QR'ın ALTINDA aynı bucket'tan türeyen 6 haneli bir kod da
# gösterir; personel kamerayla okutamıyorsa bunu elle yazar.  QR ile AYNI
# pencerede döner ama HMAC domain'i farklıdır — koddan QR token'ı (veya tersi)
# türetilemez.

NUMERIC_CODE_LEN = 6
_CODE_DOMAIN = "pdks-code:"      # QR'dan ayrı domain-separation
CODE_TOLERANCE = 2               # ±2 bucket (60–90 sn) — kod ELLE yazılır, QR'dan uzun sürer
_CODE_STALE_WINDOW = 20          # ±10 dk: imza tutuyor ama çok eski → 'invalid' değil 'expired'


def make_numeric_code(secret: str, bucket: int) -> str:
    """Bucket'ın 6 haneli yedek kodu.  RFC 4226 (HOTP) dinamik kırpma kalıbı:
    HMAC'in son nibble'ı ofseti seçer, oradan 4 bayt okunup mod 10^n alınır —
    böylece her bucket'ta baştaki sıfırlar dahil düzgün dağılmış bir kod çıkar."""
    msg = f"{_CODE_DOMAIN}{bucket}".encode("utf-8")
    dig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
    off = dig[-1] & 0x0F
    num = int.from_bytes(dig[off:off + 4], "big") & 0x7FFFFFFF
    return str(num % (10 ** NUMERIC_CODE_LEN)).zfill(NUMERIC_CODE_LEN)


def verify_numeric_code(secret: str, code, now_unix,
                        tolerance: int = CODE_TOLERANCE) -> str:
    """Yedek kodu doğrula.  Dönüş: 'ok' | 'expired' | 'invalid'.

    QR'dan farkı: kod bucket bilgisi TAŞIMAZ, bu yüzden pencere taranır.
    Kabul penceresi ±tolerance; onun dışında ama son ~10 dk içinde tutan bir
    kod 'expired' döner (personel ekrandaki eski kodu yazmış) — 'invalid' ise
    format bozuk ya da kod hiç bu secret'la üretilmemiş."""
    code = code.strip() if isinstance(code, str) else ""
    # isdigit() Unicode rakamlarını da (٣, ३) True sayar; compare_digest ise
    # ASCII olmayan str'de TypeError atar → önce ascii kontrolü.
    if len(code) != NUMERIC_CODE_LEN or not code.isascii() or not code.isdigit():
        return "invalid"
    now_b = qr_bucket(now_unix)
    for d in range(-tolerance, tolerance + 1):
        if hmac.compare_digest(make_numeric_code(secret, now_b + d), code):
            return "ok"
    for d in range(-_CODE_STALE_WINDOW, _CODE_STALE_WINDOW + 1):
        if hmac.compare_digest(make_numeric_code(secret, now_b + d), code):
            return "expired"
    return "invalid"


# ─── Sabit (basılı) QR — kiosk ekranı olmayan ofisler için ───────────────────
# Girişe asılan KAĞIT QR.  Dönmez: içeriği tek bir gizli koddur ve kod ancak
# yönetici "yeni kod üret" dediğinde değişir (o an eski çıktı geçersizleşir).
# Ekran gerektirmemesi karşılığında zayıflığı fotoğraflanabilir olmasıdır —
# bu yüzden ASIL güvenlik ofis IP'si + konumdur; QR "kapıya kadar geldim"
# kanıtıdır.  Kod, kamerası çalışmayan personel için elle de girilebilsin diye
# kısa ve karışmayan harflerden seçilir (0/O, 1/I/L, 5/S, 8/B yok).

QR_STATIC_PREFIX = "PDKSQRS1"
STATIC_CODE_ALPHABET = "ACDEFGHJKMNPQRTUVWXYZ2346789"
STATIC_CODE_LEN = 8


def normalize_static_code(raw) -> str:
    """Elle girilen kodu karşılaştırılabilir hale getir: büyük harf, boşluk ve
    tire atılır (personel 'k7p2-m9qx' yazsa da tutar).  isascii() ŞART —
    isalnum() Unicode harfleri de (Ç, Ğ, Ş…) geçirir; sonraki
    hmac.compare_digest ASCII olmayan str'de TypeError atar (kardeş
    fonksiyonlar verify_qr_token/verify_numeric_code aynı tuzağa karşı
    korumalı, burada da aynı kural geçerli)."""
    if not isinstance(raw, str):
        return ""
    return "".join(ch for ch in raw.upper() if ch.isalnum() and ch.isascii())


def make_static_token(code: str) -> str:
    """Basılı QR'ın içeriği — 'PDKSQRS1:<kod>'."""
    return f"{QR_STATIC_PREFIX}:{code}"


def verify_static_code(expected_code: str, given) -> str:
    """Sabit kodu doğrula.  Dönüş: 'ok' | 'invalid'.

    expected_code boşsa (yönetici henüz kod üretmedi) DAİMA 'invalid' —
    yapılandırılmamış doğrulama sessizce geçmemeli."""
    exp = normalize_static_code(expected_code)
    got = normalize_static_code(given)
    if not exp or not got:
        return "invalid"
    return "ok" if hmac.compare_digest(exp, got) else "invalid"


def verify_static_token(expected_code: str, token) -> str:
    """Okutulan QR metnini doğrula.  'PDKSQRS1:' öneki zorunlu — böylece
    ofisteki başka bir barkod/QR yanlışlıkla okutulursa net hata verilir."""
    if not isinstance(token, str) or not token.isascii():
        return "invalid"
    parts = token.split(":", 1)
    if len(parts) != 2 or parts[0] != QR_STATIC_PREFIX:
        return "invalid"
    return verify_static_code(expected_code, parts[1])


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """İki koordinat arası kuş uçuşu mesafe (metre)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2)
    return EARTH_RADIUS_M * 2 * math.asin(min(1.0, math.sqrt(a)))


def geo_within(dist_m: float, radius_m: float, accuracy_m=None) -> bool:
    """Mesafe, yarıçap + tarayıcının bildirdiği GPS belirsizliği (100 m'ye
    kadar) toleransı içinde mi.  accuracy_m None/negatifse tolerans 0."""
    tol = min(max(accuracy_m or 0.0, 0.0), GEO_ACCURACY_CAP_M)
    return dist_m <= radius_m + tol


def ip_allowed(ip: str, allowlist_raw: str) -> bool:
    """Virgülle ayrılmış IP/prefiks listesine göre kontrol.

    Girdi noktayla bitiyorsa ("85.10.") prefiks eşleşmesi (o bloktaki her IP);
    aksi halde tam eşleşme.  Boş liste ya da boş ip → False (güvenli varsayılan
    — yapılandırılmamış doğrulama asla sessizce geçmemeli)."""
    ip = (ip or "").strip()
    if not ip:
        return False
    for entry in (allowlist_raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if entry.endswith("."):
            if ip.startswith(entry):
                return True
        elif ip == entry:
            return True
    return False
