# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Satın Alma Planı — PDF çıktıları (16.10.2026 yeniden tasarım).

    render_pdf(report, variant="full")     -> bytes   "Detaylı Rapor"
    render_pdf(report, variant="summary")  -> bytes   "Yönetici Özeti" (2–4 sayfa)

`report` = `core.purchase_plan.compute()` çıktısı + `core.purchase_pricing.
attach()` alanları.  Tasarım sistemi `core/report_theme.py` (Liberation Sans,
lacivert/altın palet, GRID'siz tablolar, "Sayfa X / Y", yer imleri); özet
verisi `purchase_pricing.summary_data()` (önizleme / Excel "Yönetici Özeti" ile
aynı sayılar).

**Yönetici Özeti** (dikey A4): kapak bandı (logo, senaryo adı, stok tarihi,
para birimi) → 3×2 KPI → kapsam cümlesi + uyarı kutusu + özet kutusu →
"Maliyet — tür bazında" tablo + gruplu çubuk (alınacak / stoktan / yoldan) →
"Alınacaklar — tedarikçi bazında" (ilk 8 + Diğer) + çubuk → "Hammadde ihtiyacı"
(kg ve litre yan yana TEK tablo, ilk 12 + kalan; alınacak hammadde toplamı) +
iki çubuk → "Fiyatı eksik kalemler" (tür bazında sayılar, ≤3 örnek, nasıl
tamamlanır) → "Uyarılar ve bekletilenler".

Sayfa düzeni kuralları (17.10.2026 görsel QA): KeepTogether yalnız başlık +
tablo içindir, İÇ İÇE KeepTogether YOK (reportlab iç bloğu 0xffffff yükseklik
sayar → dış blok her seferinde yeni sayfaya atlar); grafik tablosundan ayrı
akar (`CondPageBreak(grafik yüksekliği)`); bölünmesi gereken uzun içerik
`keepWithNext` ile bağlanmaz (o da KeepTogether olur).  Dipnot numaraları belge
boyunca kesintisiz; dipnot metni düz verilir, `footnotes()` bir kez kaçışlar.

**Detaylı Rapor**: aynı yönetici bölümü (+ içindekiler) dikey sayfada, sonra
yatay A4 operasyon bölümleri.  Bölüm başlıkları ve numaraları
`purchase_pricing.sections()`'tan — UI ve Excel ile AYNI; boş bölüm basılmaz.
Kısaltmalar (ABD senaryosu 67 → ≤35 sayfa hedefi): malzeme satırında yalnız
seçilen teklif (kalın) + "+N teklif"; uyarı düzeyindeki notlar ⚠ + dipnot
numarası, metin bölümün sonunda; diğer teklifler, ilişki satırları ("Stok
kartında yazan", "Sipariş:", "Aynı malzeme:"), ambalaj katı ve bilgi notları
"Ek A — Teklif ve ilişki detayı" bölümünde (satır başına bir kez); fiyatsız
satırda ilk ilişki satırı yerinde kalır (kime sorulacağı görünsün).  Tedarikçi
bölümünde zorunlu sayfa sonu yok; aday firmalar tek tablo; ürünler ile ürün
başına maliyet tek tabloda (No, Ürün, Marka, Adet, Üretilebilir, Kısıt, 1 adet
maliyeti, Toplam, Eksik); "Yeterli olanlar" satır başına tek satır, değere göre
sıralı, 8,5 pt; fiyatı eksik kalemler yalnız "Ek B"de (iki sütun, satır
sırasıyla; ilk 120 + "+N"); notlar gruplu (Kaynaklar ve kur / Hesap kuralları /
Kapsam dışı / Diğer); her bölüm için PDF yer imi + içindekiler (sayfa
numaraları çok geçişli derlemeden).
Elle işaretlemek için boş "Alındı" sütunu durur.  "Elimizde" hücresinin
altında grup stoğu dökümü (`purchase_plan.stock_note`) küçük gri satırda;
Tutar'ın altında gri "stoktan X · yoldan Y" (net mod); ambalaj uyarısında
Tutar'ın önünde kırmızı ⚠ + dipnot.  Brüt modda stok değeri dili yok.

Bütün dinamik metin (malzeme/tedarikçi adı, not, iletişim) reportlab
Paragraph'ına `escape()` ile girer — adında "&" ya da "<" geçen bir kart
PDF üretimini düşürmesin / biçim etiketi enjekte edemesin.
"""
import re
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO
from typing import Dict, List, Optional, Tuple

from core.purchase_plan import stock_note, tr_num, unit_norm
from core.purchase_pricing import (CURRENCY_SYMBOL, PKG_SUFFIX, UNIT_TEXT, _qty,  # noqa: F401  (PKG_SUFFIX/_qty re-export)
                                   amount_warn, cost_overview_view, cost_summary_caveats, cost_summary_lines,
                                   cover_text, cover_value, money, offer_lines, pkg_round_text, price_text,
                                   relation_line, relation_texts, sections, summary_data, value_basis_text)
from core.report_theme import (LANDSCAPE_W_MM, PORTRAIT_W_MM, WARN_GLYPH, Bookmark, Theme, TocPanel, e,
                               make_doc, truncate, warn_font)
from reportlab.platypus import Table as _RLTable

CURRENCY_NAME = {"USD": "ABD doları", "EUR": "avro", "TRY": "Türk lirası"}
KIND_HEADER = {"raw": "Hammadde", "packaging": "Ambalaj", "label": "Etiket"}
SECTION_KIND = {"raw_priced": "raw", "raw_unpriced": "raw", "pkg_priced": "packaging",
                "pkg_unpriced": "packaging", "labels": "label"}
COST_UNPRICED_MAX = 120       # Ek B — fiyatı eksik kalemler en çok 120 + "+N" (Rusya 87: tamamı)
VARIANT_TITLE = {"summary": "Yönetici Özeti", "full": "Detaylı Rapor"}
TR_MONTHS = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos",
             "Eylül", "Ekim", "Kasım", "Aralık")
# Bu uyarılar ayrı dipnot yerine adın yanında kısa işaret olarak basılır.
_NAME_CAUTIONS = {"estimated"}
# Ambalaj uyarısı Tutar'ın önünde ⚠ + dipnot (ad hücresinde ikinci kez değil)
_AMOUNT_CAUTIONS = {"package_small", "package_large"}
_PAGE_W = LANDSCAPE_W_MM

# Geriye uyum (eski adlar)
_WARN_GLYPH = WARN_GLYPH
_warn_font = warn_font
_e = e


def _long_date(stamp: Optional[str]) -> str:
    """'02.10.2026 12:00' → '2 Ekim 2026' (sayfa altı)."""
    try:
        d = datetime.strptime((stamp or "")[:10], "%d.%m.%Y")
    except ValueError:
        return stamp or ""
    return f"{d.day} {TR_MONTHS[d.month - 1]} {d.year}"


def _tr_stamp(iso: Optional[str]) -> str:
    """UTC ISO → 'gg.aa.yyyy ss:dd' (TR, sabit UTC+3 — `database.to_tr` ile aynı)."""
    if not iso:
        return ""
    try:
        return (datetime.fromisoformat(iso) + timedelta(hours=3)).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return str(iso)


def _half_up(v: float, places: int) -> float:
    """Excel'in gösterim yuvarlaması: .5 daima yukarı, float'ın kısa ondalık
    yazımı üzerinden (1,25 → 1,3; Python biçimi 1,2 yazardı)."""
    q = Decimal(1).scaleb(-places)
    return float(Decimal(repr(float(v))).quantize(q, rounding=ROUND_HALF_UP))


def _amt(v: float, unit: str) -> str:
    """Özet miktarı — Alım listesi (`display.buy_text`), maliyet bölümü
    (`_qty_sum_text`) ve Excel ile AYNI yazım (bir sayı her sayfada aynı):
    0,026 kg → '26 gram'; 12,3 kg → '12,3 kg'; 1.250 kg → '1.250,0 kg';
    2 l → '2,0 litre'.  Yuvarlama Excel'in "#,##0.0" gösterimiyle aynı (yarım
    yukarı): Excel "Yönetici Özeti" sayının kendisini tutar, 1,25 orada "1,3"."""
    v = float(v or 0)
    if unit in ("kg", "l"):
        if 0 < v < 1:
            g = float((Decimal(repr(v)) * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            if g < 1000:
                return f"{tr_num(g)} {'gram' if unit == 'kg' else 'ml'}"
        return f"{tr_num(_half_up(v, 1), 1)} {UNIT_TEXT[unit]}"
    return f"{tr_num(_half_up(v, 0))} adet"


def _stock_amt(v, unit) -> str:
    """Kısıtlayan malzemenin stoğu — `_amt` yazımı (0,026 kg → "26 gram";
    "0,0 kg" yok); stok yoksa "stok yok", sıfıra yuvarlanan artık "< 1 ml"
    ("0 adet" / "0 ml" / "0,0 kg" diye üç ayrı yazım çıkıyordu)."""
    v = float(v or 0)
    if v <= 0:
        return "stok yok"
    u = unit_norm(unit)
    if u in ("g", "ml"):
        txt = _amt(v / 1000, "kg" if u == "g" else "l")
    else:
        txt = _amt(v, u if u in ("kg", "l") else "adet")
    return "<\u00a01 " + txt[2:] if txt.startswith("0 ") else txt


def _tight(name: str) -> str:
    """Ad verisindeki kapanış parantezi önündeki boşluk ("YAĞI(SARI )") satırı
    ")" ile başlatıyordu — boşluk atılır."""
    return re.sub(r"\s+\)", ")", name or "")


_NB_COUNT = re.compile(r"\b(\d[\d.,]*) ((?:\w+ )?(?:kart|kalem|ürün|adet|firma))\b")


def _nb_count(text: Optional[str]) -> Optional[str]:
    """KPI notunda sayı + (sıfat +) ad bölünmez: "3 yeterli kalem" satır
    sonunda "3 / yeterli kalem" diye ayrılıyordu (U+00A0; Excel'e gitmez)."""
    if not text:
        return text
    return _NB_COUNT.sub(lambda m: m.group(1) + "\u00a0" + m.group(2).replace(" ", "\u00a0"), text)


_NB_UNIT = re.compile(r"(\d) (adet|kg|gram|litre|ml|g|l)(?![\w])")


def _nb(s: str) -> str:
    """Sayı ile birimi bölünmez boşlukla bağlar ("170 ml" satır sonunda
    "170 / ml" diye bölünmesin; reportlab U+00A0'da satır kırmaz)."""
    return _NB_UNIT.sub("\\1\u00a0\\2", s or "")


class _Ctx(Theme):
    """Tema + rapor geneli değerler (para birimi, seçenekler) + bölüm dipnotları."""

    def __init__(self, report: dict, variant: str):
        Theme.__init__(self)
        self.report, self.variant = report, variant
        meta = report.get("meta") or {}
        self.opts = meta.get("options") or {}
        pr = report.get("pricing") or {}
        self.cur = pr.get("currency") or self.opts.get("currency") or "USD"
        self.sym = CURRENCY_SYMBOL.get(self.cur, self.cur)
        self.gross = self.opts.get("stock_mode") == "gross"
        self.open_on = bool(self.opts.get("subtract_open_orders"))
        self.owner = " ".join(str(self.opts.get("checklist_owner") or "").split()) or None
        self.foot: List[str] = []
        self.foot_base = 0            # dipnot numarası belge boyunca kesintisiz (bölümler sayfa paylaşır)
        self.toc_entries: List[Tuple[str, str, int]] = []

    def money(self, v) -> str:
        return money(v, self.cur)

    def no_contact(self) -> str:
        return self.red("İletişim bilgisi sistemde yok" + (f" — {e(self.owner)} girecek" if self.owner else ""))

    # ── dipnotlar (metin bölüm sonunda; numara belge boyunca artar) ────────
    def note(self, text: str) -> int:
        """Dipnot metni DÜZ (kaçışlanmamış) verilir; `footnotes()` bir kez kaçışlar."""
        self.foot.append(text)
        return self.foot_base + len(self.foot)

    def fmark(self, nums) -> str:
        nums = [nums] if isinstance(nums, int) else list(nums)
        return self.warn_mark + "<super><font size='6.5'>" + ",".join(str(n) for n in nums) + "</font></super>"

    def footnotes(self) -> list:
        from reportlab.platypus import Paragraph, Spacer
        if not self.foot:
            return []
        out = [Spacer(1, 2)] + [Paragraph(f"{self.warn_mark} {i}. {self.marks(e(t))}", self.S["foot"])
                                for i, t in enumerate(self.foot, start=self.foot_base + 1)]
        self.foot_base += len(self.foot)
        self.foot = []
        return out


# ─── Malzeme satırı (detaylı rapor) ─────────────────────────────────────────

def _name_cell(c: _Ctx, m: dict) -> str:
    """Malzeme adı + "tahmini" işareti + uyarı dipnotları (⚠ ve numara)."""
    t = e(m["name"])
    marks: List[int] = []
    for cau in m.get("cautions") or []:
        if cau["code"] in _NAME_CAUTIONS:
            t += " " + c.grey("(" + e(cau["text"].rstrip(". ").lower()) + ")")
        elif cau.get("severity", "warn") == "warn" and cau["code"] not in _AMOUNT_CAUTIONS:
            marks.append(c.note(cau["text"]))
    if marks:
        t += "\u00a0" + c.fmark(marks)          # ⚠ son sözcükten ayrılmasın
    return t


def _stock_cell(c: _Ctx, m: dict, d: dict) -> str:
    """"Elimizde" + altında küçük gri grup stoğu dökümü (`stock_note` —
    önizleme ve Excel AA ile aynı metin)."""
    t = e(d.get("stock_text"))
    note = stock_note(m)
    if note:
        t += "<br/>" + c.small(e(note))
    return t


def _amount_cell(c: _Ctx, m: dict) -> str:
    """Tutar + altında gri "stoktan X · yoldan Y"; ambalaj uyarısı varsa
    tutarın önünde kırmızı ⚠ + dipnot numarası (metin bölüm sonunda)."""
    t = c.money(m["amount"]) if m.get("amount") is not None else "—"
    w = amount_warn(m)
    if w:
        t = c.fmark(c.note(w)) + "\u00a0" + t
    cov = cover_text(m, c.cur)          # önizlemenin `ui.cover_text`'iyle aynı metin
    if cov:
        t += "<br/>" + c.small(e(cov))
    return t


def _extra_lines(c: _Ctx, m: dict) -> Tuple[List[str], List[str]]:
    """Ek A satırı: (diğer teklifler + fiyatsız teklif notları, sistemdeki
    kayıtlar) — satırda zaten görünen seçilen teklif / kırmızı "Fiyat yok" /
    fiyatsız satırın ilk ilişki satırı HARİÇ.  Kaçışlanmamış düz metin."""
    lines = offer_lines(m, c.cur)
    offers = [t for t, st in lines if st == "g"]
    shown_rel = relation_line(m) if not any(st == "b" for _, st in lines) else None
    recs = [t for t in relation_texts(m) if t != shown_rel]
    pkg = pkg_round_text(m, c.cur)          # önizleme / Excel "Not" ile aynı metin
    if pkg:
        recs.append(pkg)
    for cau in m.get("cautions") or []:
        if cau["code"] not in _NAME_CAUTIONS and cau.get("severity", "warn") != "warn":
            recs.append("Not: " + cau["text"])
    return offers, recs


def _sup_cell(c: _Ctx, m: dict) -> str:
    """Tedarikçi ve birim fiyat hücresi: seçilen teklif kalın (`offer_lines`,
    önizlemeyle aynı metin); fiyatsızsa kırmızı "Fiyat yok — teklif alınacak"
    + ilk ilişki satırı gri; kalan teklif/kayıtlar "+N teklif · ayrıntı Ek A"."""
    lines = offer_lines(m, c.cur)
    out = [c.bold(e(t)) for t, st in lines if st == "b"]
    if not out:
        out = [c.red(e(t)) for t, st in lines if st == "r"]
        rl = relation_line(m)
        if rl:
            out.append(c.grey(e(rl)))
    offers, recs = _extra_lines(c, m)
    if offers or recs:
        bits = ([f"+{len(offers)} teklif"] if offers else []) + ([f"+{len(recs)} kayıt"] if recs else [])
        out.append(c.small(" · ".join(bits) + " · ayrıntı Ek A"))
    return "<br/>".join(out)


def _material_table(c: _Ctx, rows: List[dict], kind: str):
    from reportlab.platypus import Paragraph

    S = c.S
    cols = ["name", "need", "stock"] + (["open"] if c.open_on else []) + ["buy", "sup", "amount", "got"]
    w = {"name": 62, "need": 25, "stock": 25, "open": 20, "buy": 26, "sup": 93, "amount": 24, "got": 14}
    if c.open_on:
        w["sup"] -= w["open"]
    head = {"name": KIND_HEADER.get(kind, "Malzeme"), "need": "Toplam gereken",
            "stock": "Elimizde (bilgi)" if c.gross else "Elimizde",
            "open": "Yolda (bilgi)" if c.gross else "Yolda", "buy": "Alınacak",
            "sup": "Tedarikçi ve birim fiyat", "amount": "Tutar", "got": "Alındı"}
    right = {"need", "stock", "open", "buy", "amount"}
    data = [[Paragraph(head[k], S["thr" if k in right else "th"]) for k in cols]]
    for m in rows:
        d = m.get("display") or {}
        cell = {"name": Paragraph(_name_cell(c, m), S["cell"]),
                "need": Paragraph(e(d.get("need_text")), S["num"]),
                "stock": Paragraph(_stock_cell(c, m, d), S["num"]),
                "open": Paragraph(e(d.get("open_text")), S["num"]),
                "buy": Paragraph(e(d.get("buy_text")), S["numb"]),
                "sup": Paragraph(_sup_cell(c, m), S["cell"]),
                "amount": Paragraph(_amount_cell(c, m), S["numb"]),
                "got": ""}
        data.append([cell[k] for k in cols])
    return c.table(data, [w[k] for k in cols])


# ─── Bölümler (detaylı rapor, yatay) ─────────────────────────────────────────

def _sec_key(sec: dict) -> str:
    return f"sec{sec['no']}"


def _heading_parts(c: _Ctx, sec: dict, *, page_break=False, subtitle: Optional[str] = None) -> Tuple[object, list]:
    """(sayfa sonu akışı, [yer imi, h2, alt başlık?]).  Çağıran bunları KENDİ
    KeepTogether'ına katar — iç içe KeepTogether YASAK: reportlab iç bloğu
    0xffffff yükseklik sayar, dış blok sayfa başında değilse hep yeni sayfaya
    atlar (eski "Tedarikçi bilgi eksikleri" %70 boş sayfa bırakıyordu)."""
    from reportlab.lib.units import mm
    from reportlab.platypus import CondPageBreak, PageBreak, Paragraph

    plain = f"{sec['no']}. {sec['title']}"
    title = f"{sec['no']}. {e(sec['title'])}" + (f" ({e(sec['summary'])})" if sec.get("summary") else "")
    sub = subtitle if subtitle is not None else (e(sec["subtitle"]) if sec.get("subtitle") else None)
    head = [Bookmark(_sec_key(sec), plain), Paragraph(title, c.S["h2"])]
    if sub:
        head.append(Paragraph(sub, c.S["sub"]))
    return (PageBreak() if page_break else CondPageBreak(45 * mm)), head


def _heading(c: _Ctx, sec: dict, *, page_break=False, subtitle: Optional[str] = None) -> list:
    from reportlab.platypus import KeepTogether, Spacer

    brk, head = _heading_parts(c, sec, page_break=page_break, subtitle=subtitle)
    return [brk, KeepTogether(head + [Spacer(1, 1)])]


# UI alt başlıkları "gri satırlar"dan söz eder; PDF'te o satırlar Ek A'da.
# (seçim kuralı açık, kapalı) çifti — `pricing.status_used`.
_PDF_SUBTITLE = {
    "raw_priced": ("Kalın yazılan tedarikçi seçilen teklif: önce malzeme tercihi ve “tercih edilen” firma, "
                   "sonra en ucuz; “bitirilecek” ve “bu malzemede alma” firmalar seçilmez. Tutar onunla "
                   "hesaplandı. Diğer teklifler ve sistemdeki kayıtlar Ek A’da; ⚠ numaraları bölüm "
                   "sonundaki dipnotlarda.",
                   "Kalın yazılan tedarikçi en ucuz olanı; tutar onunla hesaplandı. Diğer teklifler ve "
                   "sistemdeki kayıtlar Ek A’da; ⚠ numaraları bölüm sonundaki dipnotlarda."),
    "raw_unpriced": ("Satırda, sistemde bu malzeme için kayıtlı ilk firma (stok kartı, son alım, numune); "
                     "siparişler, aynı malzemenin diğer kartları ve bilgi notları Ek A’da.",) * 2,
}
# Ambalaj bölümleri aynı satır düzenini kullanır (UI'da alt başlıkları yok; PDF'te Ek A'ya yönlendirir)
_PDF_SUBTITLE["pkg_priced"] = _PDF_SUBTITLE["raw_priced"]
_PDF_SUBTITLE["pkg_unpriced"] = _PDF_SUBTITLE["raw_unpriced"]


def _sec_material(c: _Ctx, sec: dict) -> list:
    picked = bool((c.report.get("pricing") or {}).get("status_used"))
    pair = _PDF_SUBTITLE.get(sec["key"])
    sub = c.marks(e(pair[0] if picked else pair[1])) if pair else None
    return (_heading(c, sec, subtitle=sub) + [_material_table(c, sec["rows"], SECTION_KIND[sec["key"]])]
            + c.footnotes())


def _sec_new_items(c: _Ctx, sec: dict) -> list:
    from reportlab.platypus import Paragraph

    S = c.S
    labels = [r for r in sec["rows"] if r.get("type") == "label"]
    sub = ""
    if labels:
        title = labels[0].get("title") or "Yeni etiket"
        sub = f"Etiketler yeni basılacak ({e(title)}, toplam {tr_num(sum(r['total'] for r in labels))} adet). "
    sub += e(sec.get("subtitle") or "")
    data = [[Paragraph("Kalem", S["th"]), Paragraph("Alınacak", S["thr"]), Paragraph("Not", S["th"]),
             Paragraph("Alındı", S["th"])]]
    nop = c.red("Fiyat yok — teklif alınacak")
    for r in sec["rows"]:
        if r.get("type") == "label":
            qty = f"{tr_num(r['total'])} adet"
            note = nop + (f" · {e(r['face_text'])}" if r.get("face_text") else "")
        else:
            qty = e(r.get("qty_text") or "")
            note = nop + " · sistemde stok kaydı yok" + (f" · {e(r['note'])}" if r.get("note") else "")
        data.append([Paragraph(e(r["name"]), S["cell"]), Paragraph(qty, S["numb"]),
                     Paragraph(note, S["cell"]), ""])
    return _heading(c, sec, subtitle=sub) + [c.table(data, [100, 40, 115, 14])]


def _sec_suppliers(c: _Ctx, sec: dict) -> list:
    """Firma blokları — zorunlu sayfa sonu yok (koşullu); küçük bloklar
    başlık + tablo birlikte tutulur."""
    from reportlab.lib.units import mm
    from reportlab.platypus import CondPageBreak, KeepTogether, Paragraph

    S = c.S
    sub = e(sec.get("subtitle") or "") + " İletişim bilgileri sistemdeki tedarikçi kartlarından" + (
        f"; boş olanları {e(c.owner)} sisteme girecek." if c.owner else ".")
    out = _heading(c, sec, subtitle=sub)
    for b in sec["rows"]:
        blk = [Paragraph(f"{e(b['name'])} — {b['count']} kalem — {c.money(b['total'])}", S["h3"]),
               Paragraph(e(b["contact_line"]) if b.get("contact_line") else c.no_contact(), S["small"])]
        data = [[Paragraph("Malzeme", S["th"]), Paragraph("Alınacak", S["thr"]), Paragraph("Birim fiyat", S["thr"]),
                 Paragraph("Tutar", S["thr"])]]
        for it in b["items"]:
            data.append([Paragraph(e(it["name"]) + (" " + c.grey("(" + e(", ".join(it["reasons"])) + ")")
                                                     if it.get("reasons") else ""), S["cell"]),
                         Paragraph(e(it["buy_text"]), S["num"]),
                         Paragraph(f"{price_text(it['price'])} {c.sym}/{UNIT_TEXT.get(it['price_unit'], it['price_unit'])}"
                                   if it.get("price") is not None else "—", S["num"]),
                         Paragraph(c.money(it["amount"]) if it.get("amount") is not None else "—", S["numb"])])
        tb = c.table(data, [140, 40, 45, 44], pad=(2, 4))
        if len(b["items"]) <= 12:
            out.append(KeepTogether(blk + [tb]))
        else:
            out += [CondPageBreak(40 * mm)] + blk + [tb]
    return out


def _sec_candidates(c: _Ctx, sec: dict) -> list:
    """Aday firmalar — kompakt tablo (firma, kalem sayısı, iletişim, kalemler)."""
    from reportlab.platypus import Paragraph

    S = c.S
    data = [[Paragraph("Firma", S["th"]), Paragraph("Kalem", S["thr"]), Paragraph("İletişim", S["th"]),
             Paragraph("Teklif istenecek kalemler (alınacak; sistemdeki ilişki)", S["th"])]]
    for b in sec["rows"]:
        if b.get("none"):
            cl = e(b.get("contact_line") or "")
        else:
            cl = e(b["contact_line"]) if b.get("contact_line") else c.no_contact()
        txt = "; ".join(f"{e(it['name'])} ({e(it['buy_text'])}"
                        + (f"; {e(', '.join(it['reasons']))}" if it.get("reasons") else "") + ")"
                        for it in b["items"])
        data.append([Paragraph(e(b["name"]), S["cells"]), Paragraph(str(b["count"]), S["nums"]),
                     Paragraph(cl, S["cells"]), Paragraph(txt, S["cells"])])
    return _heading(c, sec) + [c.table(data, [58, 14, 62, 135], pad=(2.5, 4))]


def _sec_checklist(c: _Ctx, sec: dict) -> list:
    from reportlab.platypus import KeepTogether, Paragraph

    paras = []
    for r in sec["rows"]:
        head, sep, rest = r["text"].partition(": ")
        txt = (e(head) + sep + c.bold(e(rest))) if sep else e(r["text"])
        paras.append(Paragraph(txt, c.S["bullet"], bulletText="•"))
    brk, head = _heading_parts(c, sec)
    return [brk, KeepTogether(head + paras)]


def _sec_products(c: _Ctx, report: dict, sec: dict, cost_v: Optional[dict]) -> list:
    """Ürünler + (fiyatlı raporda) ürün başına maliyet TEK tabloda: No, Ürün,
    Marka, Adet, Üretilebilir, Kısıtlayan malzeme, 1 adet maliyeti, Toplam,
    Fiyatı eksik.  Bitmiş ürün stoğu düşülüyorsa Adet hücresinin altında
    "stok X → üretilecek Y"."""
    from reportlab.platypus import Paragraph

    S = c.S
    sub_on = bool(c.opts.get("subtract_finished_stock"))
    costs = {p["no"]: p for p in (cost_v or {}).get("products") or []}
    cols = [("no", "No", 10, "thr"), ("name", "Ürün", 0, "th"), ("brand", "Marka", 24, "th"),
            ("qty", "Hedef adet", 24, "thr"), ("cap", "Elimizdekiyle üretilebilir", 26, "thr"),
            ("lim", "Kısıtlayan malzeme", 46 if costs else 60, "th")]
    if costs:
        cols += [("unit_cost", "1 adet maliyeti", 24, "thr"), ("total", "Toplam", 26, "thr"),
                 ("miss", "Fiyatı eksik", 31, "th")]
    else:
        cols.append(("note", "Not", 50, "th"))
    fixed = sum(w for _, _, w, _ in cols)
    cols = [(k, h, (_PAGE_W - fixed) if k == "name" else w, st) for k, h, w, st in cols]
    data = [[Paragraph(h, S[st]) for _, h, _, st in cols]]
    fin_shown = False
    for p in sec["rows"]:
        name = e(_nb(p["name"]))
        cauts = p.get("cautions") or []
        # Bilgi düzeyi uyarılar (ör. "Tahmini hesap: reçete ×10,00 ölçekle
        # kullanıldı.") ⚠ almaz: fiyatlı raporda adın altında gri satır,
        # fiyatsız raporda "Not" sütununda — hiçbiri düşmez
        info = [x for x in cauts if x.get("severity", "warn") != "warn"]
        sub_lines = []
        merged = None
        if p.get("estimated") and p.get("recipe_name"):
            # Fiyatlı raporda "Tahmini hesap: … kullanıldı — <reçete>" TEK satır grubu
            # (ad sütunu dar; ayrı "Reçete:" + uyarı satırı hücreyi 6 satıra çıkarıyordu)
            merged = next((x for x in info if x["code"] == "estimated"), None) if costs else None
            if merged:
                sub_lines.append(e(_nb(merged["text"].rstrip(". "))) + " — " + e(_nb(p["recipe_name"])))
            else:
                sub_lines.append(f"Reçete: {e(_nb(p['recipe_name']))}"
                                 + (f" ×{tr_num(p['scale'], 2)}" if p.get("scale") not in (None, 1, 1.0) else ""))
        if costs:
            sub_lines += [e(_nb(x["text"])) for x in info if x is not merged]
        if sub_lines:
            name += "<br/>" + c.small("<br/>".join(sub_lines))
        # Dipnot metni DÜZ kalır (`footnotes()` bir kez kaçışlar); Not sütunu e() ile
        notes = [x["text"] for x in cauts if x.get("severity", "warn") == "warn"]
        if p.get("note"):
            notes.append("Not: " + p["note"])
        if notes and costs:
            name += "\u00a0" + c.fmark([c.note(n) for n in notes])
        if not costs:
            notes = [x["text"] for x in info] + notes
        cap = p.get("capacity") or {}
        lim = "<br/>".join(f"{e(_tight(x['name']))} ({e(_nb(_stock_amt(x['stock'], x['unit'])))})"
                           for x in cap.get("limiting") or [])
        qty = tr_num(p["qty"])
        fin = float(p.get("finished_stock") or 0.0)
        if sub_on:
            qty += "<br/>" + c.small(f"stok {tr_num(max(0.0, fin))} → üretilecek {tr_num(p.get('produce_qty') or 0)}")
        elif fin > 0:                     # bilgi: düşülmedi (alt başlık söyler) — kısa, tek satır
            qty += "<br/>" + c.small(f"stok {tr_num(fin)}")
            fin_shown = True
        pc = costs.get(p["no"]) or {}
        # Hücrede yalnız sayı (satır şişmesin); hangi malzemelerin fiyatı eksik → dipnot
        miss = " — ".join(e(x) for x in (pc.get("note"), pc.get("missing")) if x)
        if pc.get("missing_names"):
            miss += "\u00a0" + c.fmark(c.note("Fiyatı eksik: " + pc["missing_names"]))
        cell = {"no": Paragraph(str(p["no"]), S["num"]), "name": Paragraph(name, S["cell"]),
                "brand": Paragraph(e(p.get("brand") or "—"), S["cell"]),
                "qty": Paragraph(qty, S["numb"]),
                "cap": Paragraph(tr_num(cap["producible"]) if cap.get("producible") is not None else "—", S["num"]),
                "lim": Paragraph(lim or "—", S["cells"]),
                "unit_cost": Paragraph(e(pc.get("unit_cost") or "—"), S["num"]),
                "total": Paragraph(e(pc.get("total") or "—"), S["numb"]),
                "miss": Paragraph(miss, S["cells"]),
                "note": Paragraph(c.grey("<br/>".join(e(n) for n in notes)) if notes else "", S["cells"])}
        data.append([cell[k] for k, _, _, _ in cols])
    total_row = False
    if costs and cost_v:
        total_row = True
        row = {k: Paragraph("", S["cell"]) for k, _, _, _ in cols}
        row["name"] = Paragraph(c.bold("Toplam"), S["cell"])
        row["qty"] = Paragraph(c.bold(e(cost_v["units_total"])), S["numb"])
        row["total"] = Paragraph(c.bold(e(cost_v["products_total"])), S["numb"])
        data.append([row[k] for k, _, _, _ in cols])
    sub = ("“Elimizdekiyle üretilebilir”: bugünkü stokla (alım yapılmadan) o ürünün tek başına kaç adet "
           "üretilebileceği; kısıtlayan malzeme ilk biten."
           + (" 1 adet maliyeti = reçetedeki tüketim (fire dahil) × seçilen teklif; toplam = sipariş adedi kadar; "
              "fiyatı olmayan malzeme sayılır, toplanmaz." if costs else "")
           + (" Hedef adetin altındaki “stok”: elimizdeki bitmiş ürün — hedeften düşülmedi (seçenek kapalı)."
              if fin_shown else ""))
    return (_heading(c, sec, subtitle=sub)
            + [c.table(data, [w for _, _, w, _ in cols], total_row=total_row)] + c.footnotes())


def _sec_cost(c: _Ctx, report: dict, sec: dict, v: dict, prod_no: Optional[int]) -> list:
    """"Maliyet özeti": tür bazında maliyet tablosu + uyarılar, gereken
    miktarlar, ürün başına maliyete ve Ek B'ye yönlendirme, notlar — metinler
    `cost_overview_view` (önizleme ve Excel ile aynı)."""
    from reportlab.platypus import KeepTogether, Paragraph

    S = c.S
    out = _heading(c, sec)
    cols = [("label", "Tür", 52, "th"), ("buy", "Alınacak", 32, "thr")]
    if v["show_stock"]:
        cols.append(("stock", "Stoktan", 32, "thr"))
    if v["show_open"]:
        cols.append(("open", "Yoldan", 32, "thr"))
    cols += [("total", "Toplam", 32, "thr"), ("priced", "Fiyatlı", 22, "thr"), ("unpriced", "Fiyatsız", 22, "thr")]
    data = [[Paragraph(h, S[st]) for _, h, _, st in cols]]
    for r in v["categories"] + [v["total_row"]]:
        tot = r is v["total_row"]
        data.append([Paragraph(c.bold(e(r[k])) if tot or k == "total" else e(r[k]),
                               S["cell"] if k == "label" else S["num"]) for k, _, _, _ in cols])
    out.append(c.table(data, [w for _, _, w, _ in cols], total_row=True))
    for t in v["caveats"]:
        out.append(Paragraph(c.red(c.marks(e(t))), S["body"]))
    q = [[Paragraph(c.bold(e(x["label"])), S["cell"]), Paragraph(e(x["text"]), S["cell"])]
         for x in v["quantities"]]
    out.append(KeepTogether([Paragraph("Gereken miktarlar", S["h3"]), c.table(q, [45, 150], header=False)]))
    ref = (f"{prod_no}. bölümdeki ürün tablosunda" if prod_no else "ürün tablosunda")
    lines = [f"Ürün başına maliyet (1 adet maliyeti ve sipariş adedi kadar toplam) {ref}; ürün toplamlarının "
             f"toplamı {c.bold(e(v['products_total']))}, planın toplam malzeme maliyeti {c.bold(e(v['plan_total']))}."]
    if v["unpriced"]:
        lines.append(f"Fiyatı eksik kalemler ({v['unpriced_count']}): {_ekb_ref(len(v['unpriced']))}.")
    out += [Paragraph(t, S["body"]) for t in lines]
    out.append(KeepTogether([Paragraph("Notlar", S["h3"])]
                            + [Paragraph(e(t), S["bullet"], bulletText="•") for t in v["notes"]]))
    return out


def _value_cell(c: _Ctx, m: dict) -> str:
    """"Değer" hücresi: stoktan (+ yoldan) karşılanan değer."""
    v = cover_value(m)
    return c.money(v) if v is not None else "—"


def _basis_cell(c: _Ctx, m: dict) -> str:
    """Değerin dayanağı ("UMAYCHEM · 4,52 $/kg" / "Fiyat yok") + yoldan metni;
    altında bilgi düzeyindeki notlar (kullanıcı notu, numune lotu, grup stoğu,
    birleştirme) gri "Not: …" — yeterli satır Ek A'ya girmez, bilgi burada."""
    used = (m.get("stock_qty_used") or 0) > 0 or (m.get("open_qty_used") or 0) > 0
    bits = []
    if used:
        bits.append(e(value_basis_text(m, c.cur)))
    if m.get("open_value") is not None:
        bits.append(e(cover_text(m, c.cur)))
    lines = [" · ".join(bits)] if bits else []
    lines += ["Not: " + e(cau["text"]) for cau in m.get("cautions") or []
              if cau["code"] not in _NAME_CAUTIONS and cau.get("severity", "warn") != "warn"]
    return c.grey("<br/>".join(lines)) if lines else ""


def _sec_sufficient(c: _Ctx, sec: dict) -> list:
    """Satır başına tek satır (8,5 pt), değere göre sıralı; uyarılar dipnot."""
    from reportlab.platypus import Paragraph

    S = c.S
    cols = [("name", "Malzeme", 72, "th"), ("kind", "Tür", 22, "th"), ("need", "Toplam gereken", 30, "thr"),
            ("stock", "Elimizde", 30, "thr")]
    if c.open_on:
        cols.append(("open", "Yolda", 22, "thr"))
    cols += [("value", "Değer", 30, "thr"), ("basis", "Dayanağı", 0, "th")]
    fixed = sum(w for _, _, w, _ in cols)
    cols = [(k, h, (_PAGE_W - fixed) if k == "basis" else w, st) for k, h, w, st in cols]
    data = [[Paragraph(h, S[st]) for _, h, _, st in cols]]
    rows = sorted(sec["rows"], key=lambda m: (-(cover_value(m) or 0), (m["name"] or "").lower()))
    for m in rows:
        d = m.get("display") or {}
        cell = {"name": Paragraph(_name_cell(c, m), S["cells"]),
                "kind": Paragraph(KIND_HEADER.get(m["kind"], ""), S["cells"]),
                "need": Paragraph(e(d.get("need_text")), S["nums"]),
                "stock": Paragraph(_stock_cell(c, m, d), S["nums"]),
                "open": Paragraph(e(d.get("open_text")), S["nums"]),
                "value": Paragraph(_value_cell(c, m), S["nums"]),
                "basis": Paragraph(_basis_cell(c, m), S["cells"])}
        data.append([cell[k] for k, _, _, _ in cols])
    return _heading(c, sec) + [c.table(data, [w for _, _, w, _ in cols], pad=(2, 4))] + c.footnotes()


_NOTE_GROUPS = (
    ("Kaynaklar ve kur", ("Fiyat kaynağı", "Sistemde bu kalemler için fiyat kaydı yok", "Farklı para birimindeki")),
    ("Hesap kuralları", ("Sıvı malzemelerde", "Hammaddeye üretim firesi", "Alınacak miktarlar güvenli",
                         "Miktarlar en yakın", "Tutar = ", WARN_GLYPH, "Pasife alınmış",
                         "Stoktan karşılanan malzemenin değeri", "Açık siparişler", "Numune lotları",
                         "“Aynı malzeme”", "“Bitirilecek”", "Aynı malzeme grubundaki")),
)


def _group_notes(report: dict, lines: List[str]) -> List[Tuple[str, List[str]]]:
    """`notes_lines` düz listesini başlık altına toplar (sıra korunur)."""
    excl = {f"{x['name']} {x['need_text']} listede yok" for x in report.get("excluded") or []}
    held = {x["name"] for x in report.get("held") or []}
    groups: Dict[str, List[str]] = {"Kaynaklar ve kur": [], "Hesap kuralları": [], "Kapsam dışı kalemler": [],
                                    "Diğer": []}
    for t in lines:
        name = "Diğer"
        for g, prefixes in _NOTE_GROUPS:
            if any(t.startswith(p) for p in prefixes):
                name = g
                break
        if name == "Diğer" and (any(t.startswith(x) for x in excl)
                                or any(t.startswith(h + " (") for h in held)):
            name = "Kapsam dışı kalemler"
        groups[name].append(t)
    return [(g, ls) for g, ls in groups.items() if ls]


def _sec_notes(c: _Ctx, report: dict, sec: dict) -> list:
    from reportlab.lib.units import mm
    from reportlab.platypus import CondPageBreak, KeepTogether, Paragraph

    out = [CondPageBreak(45 * mm), KeepTogether([Bookmark(_sec_key(sec), f"{sec['no']}. {sec['title']}"),
                                                 Paragraph(f"{sec['no']}. {e(sec['title'])}", c.S["h2"])])]
    for g, ls in _group_notes(report, sec["rows"]):
        out.append(KeepTogether([Paragraph(g, c.S["h3"])]
                                + [Paragraph(c.marks(e(t)), c.S["bullet"], bulletText="•") for t in ls]))
    return out


# ─── Ekler ──────────────────────────────────────────────────────────────────

def _appendix_a(c: _Ctx, secs: List[dict]) -> list:
    """Ek A — satırda kısaltılan teklif ve ilişki satırları (malzeme başına bir kez)."""
    from reportlab.lib.units import mm
    from reportlab.platypus import CondPageBreak, KeepTogether, Paragraph

    S = c.S
    data = [[Paragraph("Malzeme", S["th"]), Paragraph("Diğer teklifler ve fiyatsız teklif notları", S["th"]),
             Paragraph("Sistemdeki kayıtlar (stok kartı, son alım, numune, sipariş, aynı malzeme, ambalaj katı, notlar)",
                       S["th"])]]
    for sec in secs:
        if sec["key"] not in SECTION_KIND:
            continue
        for m in sec["rows"]:
            offers, recs = _extra_lines(c, m)
            if not offers and not recs:
                continue
            data.append([Paragraph(e(m["name"]), S["cells"]),
                         Paragraph("<br/>".join(c.grey(e(t)) for t in offers), S["cells"]),
                         Paragraph("<br/>".join(c.grey(e(t)) for t in recs), S["cells"])])
    if len(data) == 1:
        return []
    title = "Ek A — Teklif ve ilişki detayı"
    head = [Bookmark("appA", title), Paragraph(e(title), S["h2"]),
            Paragraph("Alım tablolarında yalnız seçilen teklif yazıldı; diğer teklifler, kayıtlı firmalar ve "
                      "bilgi notları burada (önizlemedeki metinlerle aynı).", S["sub"])]
    # Satırlar kısa: sayfa sonunda satır bölünmesin (split_in_row kapalı)
    return [CondPageBreak(45 * mm), KeepTogether(head),
            c.table(data, [58, 100, 111], pad=(2.5, 4), split_in_row=False)]


def _ekb_ref(n: int, *, cap: bool = False) -> str:
    """Ek B'ye yönlendirme: liste sığıyorsa "tam liste Ek B'de", kırpılıyorsa
    nerede tamamı olduğu (Ek B'nin "+N kalem daha" satırıyla çelişmesin).
    `cap`: cümle başı (Türkçe büyük harf: "İlk", str.capitalize değil)."""
    if n <= COST_UNPRICED_MAX:
        return ("Tam" if cap else "tam") + " liste Ek B’de"
    return (("İlk" if cap else "ilk") + f" {COST_UNPRICED_MAX} kalem Ek B’de, tam liste Excel “Ürün maliyeti” "
            "sayfasında")


def _appendix_b(c: _Ctx, v: Optional[dict]) -> list:
    """Ek B — fiyatı eksik kalemler (iki sütun, ilk 120 + "+N")."""
    from reportlab.lib.units import mm
    from reportlab.platypus import CondPageBreak, KeepTogether, Paragraph

    S = c.S
    un = (v or {}).get("unpriced") or []
    if not un:
        return []
    shown = un[:COST_UNPRICED_MAX]
    # İki sütunlu yerleşim (satır başına iki kalem) — tek sütunda 3 sayfa
    # yerine ~1,5 sayfa; iç içe tablo bölünemezdi, bu yüzden tek tablo.
    # SATIR SIRASIYLA dolar (soldan sağa, yukarıdan aşağı): sütun sırası çok
    # sayfalı tabloda okuma sırasını sayfalar arasında zıplatıyor, tür grubunu
    # (Ambalaj) son sayfanın soluna ve ilk sayfanın sağına bölüyordu.
    head = [Paragraph("Kalem", S["th"]), Paragraph("Tür", S["th"]), Paragraph("İhtiyaç", S["thr"])]
    data = [head + [Paragraph("", S["th"])] + head]

    def cell(u):
        tag = " " + c.grey("(yeni kalem, kartı yok)") if u.get("new") else (
            " " + c.grey("(bitmiş ürün kartı)") if u.get("finished") else "")
        return [Paragraph(e(u["name"]) + tag, S["cells"]), Paragraph(e(u["kind"]), S["cells"]),
                Paragraph(e(_nb(u["need"])), S["nums"])]
    for i in range(0, len(shown), 2):
        right = cell(shown[i + 1]) if i + 1 < len(shown) else [Paragraph("", S["cells"])] * 3
        data.append(cell(shown[i]) + [""] + right)
    title = f"Ek B — Fiyatı eksik kalemler ({v['unpriced_count']})"
    out = [CondPageBreak(45 * mm),
           KeepTogether([Bookmark("appB", title), Paragraph(e(title), S["h2"]),
                         Paragraph("Fiyatı olmayan kalemler sayıldı, toplama girmedi; fiyatı Malzeme Fiyatları "
                                   "sayfasından girilir (“fiyatı olmayanlar” süzgeci). Yeni kalem ve bitmiş ürün "
                                   "kartı orada listelenmez.", S["sub"])]),
           # İhtiyaç 50 mm: "17.424 adet · alınacak 17.029 adet" (46,3 mm) tek satır
           c.table(data, [62, 20, 50, 5, 62, 20, 50], pad=(2, 4))]
    if len(un) > COST_UNPRICED_MAX:
        out.append(Paragraph(c.grey(f"+{len(un) - COST_UNPRICED_MAX} kalem daha (Excel “Ürün maliyeti” "
                                    "sayfasında tam liste)."), S["body"]))
    return out


# ─── Yönetici bölümü (dikey) ─────────────────────────────────────────────────

def _warning_text(c: _Ctx, report: dict) -> Optional[str]:
    """Uyarı kutusu: kur yedeği / bekletilen kalemler / reçetesiz ürünler /
    yükleyici uyarıları."""
    msgs: List[str] = []
    fx = (report.get("pricing") or {}).get("fx") or {}
    if fx.get("used") and (fx.get("warning") or fx.get("stale")):
        msgs.append("Döviz kuru: " + e(fx.get("warning") or "güncel kur alınamadı; son bilinen kur kullanıldı.")
                    + " Tutarlar yaklaşıktır.")
    held = report.get("held") or []
    if held:
        msgs.append(f"{len(held)} kalem bekletiliyor ("
                    + ", ".join(e(h["name"]) for h in held) + "); listeye ve toplama girmedi.")
    recl = [p for p in report.get("products") or []
            if any(x["code"] == "recipeless_product" for x in p.get("cautions") or [])]
    if recl:
        msgs.append("Sistemde reçetesi olmayan ürün: " + ", ".join(e(p["name"]) for p in recl)
                    + " — malzeme ihtiyacı hesaplanamadı, listeye katılmadı.")
    for w in (report.get("meta") or {}).get("warnings") or []:
        msgs.append(e(w))
    return "<br/>".join(msgs) if msgs else None


def _summary_text(c: _Ctx, report: dict, *, split: Optional[dict] = None) -> str:
    """Özet kutusu — iki çıktıda AYNI.  Tür bazındaki madde satırları yok:
    aynı sayılar KPI kutularında ve "Maliyet — tür bazında" tablosunda (Detaylı
    Rapor'un kapağındaki ayrı madde listesi kart / yeni kalem ayrımıyla 17 / 26 /
    27 gibi farklı "ambalaj fiyatı yok" sayıları üretiyordu).  Kalan: tutar
    (net modda maliyet bloğu — KPI ile aynı adlar; ayrıca "Fiyatı belli
    kalemlerin tutarı" YAZILMAZ, aynı sayı iki adla görünüyordu), teklif
    alınacak kalem sayısı, maliyet uyarıları.
    `split` (`summary_data.unpriced_split` {to_buy, new, stock}): KPI "Fiyatı
    eksik kalem" = üçünün toplamı (KPI notu mutabık kılar); kutu "alınacak"
    sayısını (to_buy + new) AYNI kaynaktan yazar, stoğu yeten fiyatsızları
    ayrıca söyler."""
    pr = report.get("pricing") or {}
    tot = pr.get("totals") or {}
    cnt = pr.get("counts") or {}
    b = c.bold
    rp, ru = cnt.get("raw_priced", 0), cnt.get("raw_unpriced", 0)
    pp, pu = cnt.get("packaging_priced", 0), cnt.get("packaging_unpriced", 0)
    lp, lu = cnt.get("label_priced", 0), cnt.get("label_unpriced", 0)
    new_lbl = report.get("labels_new") or []
    manual = report.get("manual_lines") or []
    unpriced_any = ru + pu + lu + len(new_lbl) + len(manual)
    if split:
        unpriced_any = split.get("to_buy", 0) + split.get("new", 0)
    unpriced_stock = (split or {}).get("stock") or 0
    # Stok değerine etkisi maliyet uyarısında ("Stoktan kısmen ya da tamamen
    # karşılanan N kalemden K…"); burada çıplak toplam yok — KPI notu zaten
    # "53 alınacak kart + 31 yeni kalem + 3 yeterli kalem" diye mutabık kılıyor
    stock_tail = (f" Stoğu yeten (alınmayacak) {unpriced_stock} kalemin{' de' if unpriced_any else ''} "
                  "fiyatı yok." if unpriced_stock else "")
    main, cav = _cost_lines(c, report)
    if not (rp + ru + pp + pu + lp + lu) and not (new_lbl or manual):
        return "<br/>".join([b("Alınacak kalem yok") + " — seçilen ürünler için elimizdeki stok yetiyor."
                             + stock_tail] + main + cav)
    lines = main or [b(f"Fiyatı belli kalemlerin tutarı: {c.money(tot.get('all', 0))}")]
    if unpriced_any:
        lines.append(f"{b(f'{unpriced_any} alınacak kalemin fiyatı yok')} — teklif alınacak; gerçek toplam, "
                     "fiyatı olmayan kalemler eklenince bundan yüksek olacak." + stock_tail)
    elif stock_tail:
        lines.append(stock_tail.strip())
    return "<br/>".join(lines + cav)


def _cost_lines(c: _Ctx, report: dict) -> Tuple[List[str], List[str]]:
    """Özet kutusunun maliyet bloğu (net mod): (alınacak · stoktan (· yoldan)
    satırı + kalın "planın toplam malzeme maliyeti", küçük uyarılar) —
    önizleme / Excel ile aynı metin.  Brüt modda ([], [])."""
    rows = cost_summary_lines(report)
    if not rows:
        return [], []
    out = [" · ".join(f"{e(r['label'])}: {e(r['text'])}" for r in rows if r["key"] != "plan")]
    out += [c.bold(f"{e(r['label'])}: {e(r['text'])}") for r in rows if r["key"] == "plan"]
    cav = cost_summary_caveats(report)
    return out, ([c.small(e(" ".join(cav)))] if cav else [])


def _raw_caption(c: _Ctx, blk: dict) -> list:
    """Birim bloğunun başlığı: h3 "Kilogram — N kalem" + gri "gereken · alınacak"."""
    from reportlab.platypus import Paragraph

    u = blk["unit"]
    uname = "Kilogram" if u == "kg" else "Litre"
    return [Paragraph(f"{uname} — {blk['count']} kalem", c.S["h3"]),
            Paragraph(f"gereken {_amt(blk['need_total'], u)} · alınacak "
                      + (_amt(blk['buy_total'], u) if blk['buy_total'] > 0 else "yok"), c.S["small"])]


def _raw_rows(c: _Ctx, blk: dict, *, compact: bool, name_pt: Optional[float] = None) -> list:
    """Tablo başlığı + satırlar (ilk 12 + "Diğer" gri) — [ad, gereken, alınacak].
    `name_pt` (yan yana tablo): ad TEK SATIRA kısaltılır (ölçülerek, "…") — iki
    taraf satır yüksekliğini paylaşır, bir taraftaki iki satırlık ad öbür
    tarafta boş satır gibi görünüyordu.  Tam ad Alım listesinde ve Excel'de."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import Paragraph

    S = c.S
    u = blk["unit"]
    cs, ns, nb = ("cells", "nums", "nums") if compact else ("cell", "num", "numb")
    data = [[Paragraph("Malzeme", S["th"]), Paragraph("Gereken" if compact else "Toplam gereken", S["thr"]),
             Paragraph("Alınacak", S["thr"])]]

    def fit(t: str) -> str:
        if name_pt is None or stringWidth(t, S[cs].fontName, S[cs].fontSize) <= name_pt:
            return t
        return next((truncate(t, j) for j in range(len(t) - 1, 0, -1)
                     if stringWidth(truncate(t, j), S[cs].fontName, S[cs].fontSize) <= name_pt), t[:1])
    for r in blk["rows"]:
        nm = c.grey(e(fit(r["name"]))) if r["other"] else e(fit(r["name"]))
        need, buy = _raw_texts(r, u)
        data.append([Paragraph(nm, S[cs]), Paragraph(e(need), S[ns]),
                     Paragraph(c.bold(e(buy)) if compact else e(buy), S[nb])])
    return data


def _raw_texts(r: dict, u: str) -> Tuple[str, str]:
    """(gereken, alınacak) hücre metni: satırın Alım listesi metni ("1.250
    gram" — `_amt` onu "1,3 kg" yazıyordu, aynı PDF'te iki yazım), "Diğer"
    satırında toplamın `_amt` yazımı; alınacak yoksa "—"."""
    need = r.get("need_text") or _amt(r["need"], u)
    buy = (r.get("buy_text") or _amt(r["buy"], u)) if r["buy"] else "—"
    return need, buy


def _raw_chart(c: _Ctx, blk: dict, width_mm: float, *, compact: bool) -> list:
    """Çubuk grafik (yalnız malzemeler, "Diğer" yok; aykırı en büyük değer —
    ikinciden 8 kat büyük — grafik dışında, altında yazılır) + küçük başlık."""
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer

    S = c.S
    u = blk["unit"]
    rows = [r for r in blk["rows"] if not r["other"]]
    srt = sorted((r["need"] for r in rows), reverse=True)
    out_rows = [r for r in rows if len(srt) > 2 and srt[0] > 8 * srt[1] and r["need"] == srt[0]]
    chart_rows = [r for r in rows if r not in out_rows]
    # 1 kg / 1 litre altındaki blok gram / ml ölçeğinde çizilir (tablo da öyle
    # yazar; eksen "0  0,05  0,1" yerine "0  50  100"); çubuk etiketi tablo
    # hücresinin AYNISI (`_amt`): "200 gram", "344,6 kg"
    k = 1000 if u in ("kg", "l") and chart_rows and max(r["need"] for r in chart_rows) < 1 else 1
    unit = ("gram" if u == "kg" else "ml") if k == 1000 else UNIT_TEXT.get(u, u)
    ch = c.hbar_chart([r["name"] for r in chart_rows], [("Toplam gereken", [r["need"] * k for r in chart_rows])],
                      width_mm=width_mm, label_fmt=lambda v: _amt(v / k, u),
                      label_w_mm=30 if compact else 52, max_chars=22 if compact else 34,
                      bar_mm=2.8 if compact else 3.8)
    if not ch:
        return []
    out = [Spacer(1, 2 * mm), Paragraph(c.grey(f"Toplam gereken ({unit})"), S["small"]), ch]
    if out_rows:
        out.append(Paragraph(c.grey("Grafik dışı (ölçek): " + " · ".join(
            f"{e(r['name'])} {_amt(r['need'], u)}" for r in out_rows)), S["small"]))
    return out


class _MinTailTable(_RLTable):
    """Sayfa sonunda bölünürken devam sayfasına `MIN_TAIL` veri satırından
    azını bırakmaz (Rusya özeti: 3. sayfaya 1 satır + "Diğer" düşüyor, tekrar
    eden başlıkla ikinci bir tablo gibi görünüyordu).  Kesim yukarı kayar; ilk
    parçaya da o kadar satır kalmıyorsa tablo bütün olarak sonraki sayfaya
    geçer (`[]`).  reportlab parçaları `self.__class__` ile kurar — devam
    parçası da aynı kurala uyar."""
    MIN_TAIL = 3

    def split(self, availWidth, availHeight):
        parts = _RLTable.split(self, availWidth, availHeight)
        if len(parts) != 2:
            return parts
        rep = self.repeatRows if isinstance(self.repeatRows, int) else len(self.repeatRows)
        tail = len(parts[1]._cellvalues) - rep
        if tail >= self.MIN_TAIL:
            return parts
        n = len(parts[0]._cellvalues) - (self.MIN_TAIL - tail)
        if n - rep < self.MIN_TAIL:
            return []
        return _RLTable.split(self, availWidth, sum(self._rowHeights[:n]) + 0.01)


def _raw_tables(c: _Ctx, blocks: List[dict], colw: float, gap: float) -> list:
    """kg ve litre tabloları YAN YANA, TEK tablo (7 sütun: ad · gereken ·
    alınacak · boşluk · ad · gereken · alınacak) — sayfa sonunda satır
    sınırından bölünür, başlık + sütun adları yeni sayfada tekrar eder.
    (İki ayrı tabloyu tek satırlık dış tabloya koymak bölünemez bir ≈115 mm
    blok yapıyordu; reportlab 4.2'nin satır içi bölmesi iç tabloda çöküyor.)
    Tek birim varsa tam genişlikte kendi tablosu."""
    from reportlab.lib.units import mm
    from reportlab.platypus import Spacer, TableStyle

    if len(blocks) == 1:
        data = _raw_rows(c, blocks[0], compact=False)
        return ([Spacer(1, 2 * mm)] + _raw_caption(c, blocks[0]) + [Spacer(1, 1.5 * mm)]
                + [c.table(data, [colw - 84, 42, 42])])
    # Sayı sütunları veriden: en geniş sayı (gereken düz, alınacak kalın) ya da
    # sütun adı + 2 × dolgu.  Litre tarafı dar kalır, ad sütunu genişler —
    # "MELEZ LAVANTA UÇUCU YAĞI" gibi adlar tek satıra iner (sayfa payı)
    from reportlab.pdfbase.pdfmetrics import stringWidth
    S = c.S
    pad = 2.5                                                # pt, yatay dolgu

    def wmm(t, st):
        return stringWidth(t, S[st].fontName, S[st].fontSize) / mm

    numw = []
    for blk in blocks:
        u = blk["unit"]
        texts = [_raw_texts(r, u) for r in blk["rows"]]
        need = [wmm(t[0], "nums") for t in texts]
        buy = [wmm(t[1], "nums") * 1.04 if r["buy"] else 0 for t, r in zip(texts, blk["rows"])]  # kalın ≈ +%4
        w = max(need + buy + [wmm("Gereken", "thr"), wmm("Alınacak", "thr")])
        # + hava (komşu sayılar yapışmasın); ad sütunu en az %40 kalsın
        numw.append(min(w + 2 * pad / mm + 1.8, 0.3 * colw))
    sides = [_raw_rows(c, blk, compact=True, name_pt=(colw - 2 * nw) * mm - 2 * pad - 0.5)
             for blk, nw in zip(blocks, numw)]
    n = max(len(rows) for rows in sides) - 1
    cap: list = []
    head: list = []
    for i, blk in enumerate(blocks):
        if i:
            cap.append("")
            head.append("")
        cap += [_raw_caption(c, blk), "", ""]
        head += sides[i][0]
    data = [cap, head]
    for j in range(1, n + 1):
        row: list = []
        for i, rows in enumerate(sides):
            if i:
                row.append("")
            row += rows[j] if j < len(rows) else ["", "", ""]
        data.append(row)
    navy, line, zebra = c.c["navy"], c.c["line"], c.c["zebra"]
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"),
          ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
          ("LEFTPADDING", (0, 0), (-1, -1), pad), ("RIGHTPADDING", (0, 0), (-1, -1), pad),
          ("VALIGN", (0, 1), (-1, 1), "BOTTOM")]
    for i, rows in enumerate(sides):
        c0, c1 = 4 * i, 4 * i + 2
        last = len(rows)                     # 0: başlık satırı (tabloda 1)
        st += [("SPAN", (c0, 0), (c1, 0)), ("LEFTPADDING", (c0, 0), (c1, 0), 0),
               ("TOPPADDING", (c0, 0), (c1, 0), 0), ("BOTTOMPADDING", (c0, 0), (c1, 0), 4),
               ("LINEBELOW", (c0, 1), (c1, 1), 0.8, navy), ("BOTTOMPADDING", (c0, 1), (c1, 1), 2.5)]
        if last > 1:
            st.append(("LINEBELOW", (c0, 2), (c1, last), 0.3, line))
        st += [("BACKGROUND", (c0, r), (c1, r), zebra) for r in range(3, last + 1, 2)]
    widths: List[float] = []
    for i, nw in enumerate(numw):
        widths += ([gap] if i else []) + [colw - 2 * nw, nw, nw]
    t = _MinTailTable(data, colWidths=[w * mm for w in widths], repeatRows=2)
    t.setStyle(TableStyle([c.font_cmd] + st))
    t.hAlign = "LEFT"
    return [Spacer(1, 2 * mm), t]


def _two_col(c: _Ctx, cells: List[list], colw: float, gap: float) -> list:
    """Bir ya da iki hücre listesi → tek satırlık akış (iki sütun yan yana;
    bölünmez — yalnız grafikler için)."""
    from reportlab.lib.units import mm
    from reportlab.platypus import Table, TableStyle

    if len(cells) == 1:
        return list(cells[0])
    t = Table([[cells[0], "", cells[1]]], colWidths=[colw * mm, gap * mm, colw * mm])
    t.setStyle(TableStyle([c.font_cmd, ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                           ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    t.hAlign = "LEFT"
    return [t]


def _exec_story(c: _Ctx, report: dict, sd: dict, *, full: bool) -> list:
    """Kapak bandı → KPI → kapsam / uyarı / özet → (içindekiler) → maliyet →
    tedarikçiler → hammadde → fiyatı eksikler → uyarılar."""
    from reportlab.lib.units import mm
    from reportlab.platypus import CondPageBreak, KeepTogether, Paragraph, Spacer

    S = c.S
    W = PORTRAIT_W_MM
    meta = report.get("meta") or {}
    pr = report.get("pricing") or {}
    story: list = []
    # Oluşturma saati yalnız sayfa altında (her sayfada): alt başlık tek satır
    # kalır — saat grubu sığmayınca "($) ·" satır sonunda asılı kalıyordu
    lines = [f"{VARIANT_TITLE[c.variant]} · stoklar {e(_long_date(meta.get('stock_as_of_tr')))}"
             + f" · fiyatlar {CURRENCY_NAME.get(c.cur, c.cur)} ({c.sym})"]
    if sd["brands"]:
        lines.append("Markalar: " + e(", ".join(sd["brands"])))
    story += c.cover_band(title=e(sd["title"]), kicker="Minerva 108 · Satın Alma Planı", lines=lines, width_mm=W)
    if full:
        story.append(Bookmark("exec", "Yönetici özeti"))
    kpi = c.kpi_row([dict(k, note=_nb_count(k["note"])) if k.get("note") else k for k in sd["kpis"]],
                    width_mm=W)
    if kpi:
        story += [kpi, Spacer(1, 4 * mm)]

    sub = e(meta.get("scope_text") or "")
    sub += (f" Fiyatlar {c.bold(CURRENCY_NAME.get(c.cur, c.cur) + f' ({c.sym})')}. "
            + ("Tutar = alınacak × seçilen teklifin birim fiyatı (önce malzeme tercihi ve “tercih edilen” "
               "firma, sonra en ucuz)." if pr.get("status_used") else
               "Tutar = alınacak × en ucuz birim fiyat."))
    if (pr.get("cost_summary") or {}).get("applies"):
        sub += (" Stoktan karşılananın değeri = bu planın kullanacağı stok × aynı teklifin bugünkü fiyatı "
                "(yenileme değeri, alış maliyeti değil).")
    story.append(Paragraph(sub, S["sub"]))
    # Kutular bütün (2 satırlık öksüz parça sonraki sayfaya sarkmasın; kısa kutular)
    warn = _warning_text(c, report)
    if warn:
        story += [KeepTogether([c.callout(c.marks(warn), width_mm=W, kind="warn")]), Spacer(1, 2 * mm)]
    story += [KeepTogether([c.callout(_summary_text(c, report, split=sd.get("unpriced_split")),
                                      width_mm=W)]), Spacer(1, 2 * mm)]
    # İçindekiler özet kutusunun ALTINDA: önce yönetici özeti, sonra detayın haritası
    # (kapak + KPI + kutular + içindekiler ilk sayfaya sığar; sığmazsa bütün taşınır)
    if full and c.toc_entries:
        story.append(KeepTogether([Paragraph("İçindekiler", S["h2"]), TocPanel(c, c.toc_entries, W)]))

    def h2(t, need_mm: float = 50):
        return [CondPageBreak(need_mm * mm), Paragraph(t, S["h2"])]

    def chart_after(ch, caption: str):
        """Grafik tablosunun altında; sığmazsa sonraki sayfada — başlık+tablo+grafik
        zinciri yarım sayfa boş bırakıyordu.  Kendi küçük gri başlığıyla BİRLİKTE
        taşınır (sayfa başında başlıksız, birimi belirsiz grafik kalmasın)."""
        if not ch:
            return []
        cap = Paragraph(c.grey(e(caption)), S["small"])
        return [Spacer(1, 2 * mm), KeepTogether([cap, ch])]

    # Maliyet — tür bazında (başlık + tablo birlikte; grafik ayrı akış)
    if sd["categories"]:
        blk = [Paragraph("Maliyet — tür bazında", S["h2"])]
        cols = [("label", "Tür", "th"), ("buy", "Alınacak", "thr")]
        if sd["show_stock"]:
            cols.append(("stock", "Stoktan", "thr"))
        if sd["show_open"]:
            cols.append(("open", "Yoldan", "thr"))
        cols += [("total", "Toplam", "thr"), ("priced", "Fiyatlı", "thr"), ("unpriced", "Fiyatsız", "thr")]
        widths = {"buy": 27, "stock": 27, "open": 27, "total": 27, "priced": 17, "unpriced": 17}
        widths["label"] = W - sum(widths[k] for k, _, _ in cols if k != "label")
        data = [[Paragraph(h, S[st]) for _, h, st in cols]]
        rows = list(sd["categories"]) + [dict(sd["categories_total"], label="Toplam")]
        for r in rows:
            tot = r is rows[-1]
            cells = []
            for k, _, _ in cols:
                if k == "label":
                    txt = e(r["label"])
                elif k in ("priced", "unpriced"):
                    txt = tr_num(r.get(k) or 0)
                else:
                    txt = c.money(r.get(k) or 0)
                cells.append(Paragraph(c.bold(txt) if (tot or k == "total") else txt,
                                       S["cell"] if k == "label" else S["num"]))
            data.append(cells)
        blk.append(c.table(data, [widths[k] for k, _, _ in cols], total_row=True))
        # Grafik yalnız tutarı olan türlerle; ikiden az tür kalırsa grafik yok —
        # tek çubuk grubu hemen üstteki tablonun tekrarı, boş "Ambalaj / Etiket"
        # satırları da bilgi taşımıyordu
        keys = ["buy"] + (["stock"] if sd["show_stock"] else []) + (["open"] if sd["show_open"] else [])
        cats = [r for r in sd["categories"] if any((r.get(k) or 0) > 0 for k in keys)]
        story += [CondPageBreak(50 * mm), KeepTogether(blk)]
        if len(cats) >= 2:
            series = [(n, [r[k] or 0 for r in cats])
                      for k, n in zip(keys, ["Alınacak"] + [{"stock": "Stoktan", "open": "Yoldan"}[k] for k in keys[1:]])]
            story += chart_after(c.hbar_chart([r["label"] for r in cats], series, width_mm=W, unit=c.sym,
                                              label_w_mm=26), f"Tutar ({c.sym}) — tür bazında")

    # Tedarikçiler (başlık + tablo birlikte; grafik ayrı akış)
    if sd["top_suppliers"]:
        tot = sum(b["total"] for b in sd["top_suppliers"])
        blk = [Paragraph(f"Alınacaklar — tedarikçi bazında ({sd['suppliers_count']} firma, toplam {c.money(tot)})",
                         S["h2"])]
        data = [[Paragraph("Firma", S["th"]), Paragraph("Kalem", S["thr"]), Paragraph("Tutar", S["thr"]),
                 Paragraph("İletişim", S["th"])]]
        for b in sd["top_suppliers"]:
            name = c.grey(e(b["name"])) if b["other"] else e(b["name"])
            contact = ("" if b["other"] else (e(b["contact_line"]) if b.get("contact_line") else c.no_contact()))
            data.append([Paragraph(name, S["cell"]), Paragraph(tr_num(b["count"]), S["num"]),
                         Paragraph(c.money(b["total"]), S["numb"]), Paragraph(contact, S["cells"])])
        blk.append(c.table(data, [52, 16, 28, W - 96]))
        # "Diğer" toplamı gri (firma değil, kalan)
        story += [CondPageBreak(50 * mm), KeepTogether(blk)]
        story += chart_after(c.hbar_chart([b["name"] for b in sd["top_suppliers"]],
                                          [("Tutar", [b["total"] for b in sd["top_suppliers"]])], width_mm=W,
                                          unit=c.sym, muted=[i for i, b in enumerate(sd["top_suppliers"]) if b["other"]]),
                             f"Tutar ({c.sym}) — tedarikçi bazında")

    # Hammadde ihtiyacı — kg ve litre tabloları YAN YANA (tek, bölünebilir
    # tablo; tek birim varsa tam genişlik), sonra grafikler yan yana (sığmazsa
    # tek başına sonraki sayfada; her grafiğin kendi küçük başlığı var).
    # Başlık + tablo AKAR: CondPageBreak başlık ve ilk satırlar için yer açar
    # (KeepTogether / keepWithNext KULLANMA — ≈115 mm'lik blok önceki sayfada
    # %40 boşluk bırakıyordu; keepWithNext de reportlab'da KeepTogether olur)
    if sd["raw_need"]:
        rb = sd["raw_to_buy"]
        buy_bits = [_amt(rb[u], u) for u in ("kg", "l", "adet") if rb.get(u)]
        blocks = sd["raw_need"][:2]
        gap = 6
        colw = W if len(blocks) == 1 else (W - gap) / 2
        story += [CondPageBreak(60 * mm), Paragraph("Hammadde ihtiyacı", S["h2"]),
                  Paragraph(c.bold("Alınacak hammadde: " + (" · ".join(buy_bits) if buy_bits else "yok"))
                            + c.grey(" — alım birimine göre toplam; stoktan karşılanan düşülmüş."), S["body"])]
        story += _raw_tables(c, blocks, colw, gap)
        bots = [b for b in (_raw_chart(c, blk, colw, compact=len(blocks) > 1) for blk in blocks) if b]
        if bots:
            h = max(sum(getattr(f, "height", 0) or 0 for f in bot) for bot in bots) + 12 * mm
            story += [CondPageBreak(h)] + _two_col(c, bots, colw, gap)

    # Fiyatı eksik kalemler
    if sd["unpriced_groups"]:
        story += h2(f"Fiyatı eksik kalemler ({sd['unpriced_count']})")
        data = [[Paragraph("Tür", S["th"]), Paragraph("Kalem", S["thr"]), Paragraph("Alınacak", S["thr"]),
                 Paragraph("Örnekler", S["th"])]]
        for g in sd["unpriced_groups"]:
            ex = ", ".join(e(n) for n in g["names"]) + (c.grey(f" +{g['more']} kalem daha") if g["more"] else "")
            data.append([Paragraph(e(g["label"]), S["cell"]), Paragraph(tr_num(g["count"]), S["num"]),
                         Paragraph(tr_num(g["to_buy"]), S["num"]), Paragraph(ex, S["cells"])])
        story += [c.table(data, [30, 18, 22, W - 70], pad=(2, 4)),
                  Paragraph(c.grey(e(sd["howto"]) + (f" {_ekb_ref(sd['unpriced_count'], cap=True)}." if full
                                                     else "")), S["body"])]

    # Uyarılar ve bekletilenler (kısa bölüm: başlık + ilk maddeler birlikte)
    if sd["warnings"] or sd["held"]:
        items = [Paragraph(c.marks(e(w)), S["bullet"], bulletText="•") for w in sd["warnings"]]
        for h in sd["held"]:
            t = f"{e(h['name'])} ({e(h['buy_text'])}) " + (
                f"bekletiliyor: {e((h.get('reason') or '').rstrip('. '))}" if h.get("reason") else "teyit bekliyor")
            if h.get("amount") is not None and h.get("supplier"):
                t += f" — alınırsa yaklaşık {c.bold(c.money(h['amount']))} ({e(h['supplier'])})"
            items.append(Paragraph(t, S["bullet"], bulletText="•"))
        if sd["held"] and sd["held_what_if"]:
            items.append(Paragraph(f"Bekletilenlerin tamamı alınırsa yaklaşık {c.bold(c.money(sd['held_what_if']))} "
                                   "eklenir.", S["body"]))
        story += [KeepTogether([Paragraph("Uyarılar ve bekletilenler", S["h2"])] + items[:3])] + items[3:]
    return story


# ─── Giriş ──────────────────────────────────────────────────────────────────

def render_pdf(report: dict, variant: str = "full") -> bytes:
    """Rapor dict'inden PDF: `variant` "summary" (Yönetici Özeti, dikey) ya da
    "full" (Detaylı Rapor: dikey yönetici bölümü + yatay operasyon bölümleri)."""
    from reportlab.platypus import NextPageTemplate, PageBreak

    if variant not in VARIANT_TITLE:
        raise ValueError(f"variant: {variant!r}")
    full = variant == "full"
    c = _Ctx(report, variant)
    sd = summary_data(report)
    meta = report.get("meta") or {}
    title = meta.get("title") or "Satın Alma Planı"
    secs = sections(report) if full else []
    cost_v = cost_overview_view(report) if full else None
    if full:
        c.toc_entries = [("exec", "Yönetici özeti", 0)]
        c.toc_entries += [(_sec_key(s), f"{s['no']}. {e(s['title'])}", 0) for s in secs]
        # Ekler: içerik varsa (aşağıdaki üreticilerle aynı koşul)
        if any(_extra_lines(c, m) != ([], []) for s in secs if s["key"] in SECTION_KIND for m in s["rows"]):
            c.toc_entries.append(("appA", "Ek A — Teklif ve ilişki detayı", 0))
        if (cost_v or {}).get("unpriced"):
            c.toc_entries.append(("appB", f"Ek B — Fiyatı eksik kalemler ({cost_v['unpriced_count']})", 0))

    story = _exec_story(c, report, sd, full=full)
    if full:
        story += [NextPageTemplate("ops"), PageBreak()]
        prod_no = next((s["no"] for s in secs if s["key"] == "products"), None)
        for sec in secs:
            k = sec["key"]
            if k in SECTION_KIND:
                story += _sec_material(c, sec)
            elif k == "new_items":
                story += _sec_new_items(c, sec)
            elif k == "suppliers":
                story += _sec_suppliers(c, sec)
            elif k == "candidates":
                story += _sec_candidates(c, sec)
            elif k == "checklist":
                story += _sec_checklist(c, sec)
            elif k == "products":
                story += _sec_products(c, report, sec, cost_v)
            elif k == "cost":
                if cost_v:
                    story += _sec_cost(c, report, sec, cost_v, prod_no)
            elif k == "sufficient":
                story += _sec_sufficient(c, sec)
            elif k == "notes":
                story += _sec_notes(c, report, sec)
        story += _appendix_a(c, secs)
        story += _appendix_b(c, cost_v)

    stock_date = _long_date(meta.get("stock_as_of_tr"))
    gen = _tr_stamp(meta.get("generated_at"))
    if gen:
        gen = f"{_long_date(gen)} {gen[11:]}".strip()
    buf = BytesIO()
    doc = make_doc(buf, c, title=f"{title} — {VARIANT_TITLE[variant]}",
                   band_title=f"Minerva 108 · Satın Alma Planı · {truncate(title, 80)}",
                   band_right=VARIANT_TITLE[variant],
                   footer_left=f"Stoklar {stock_date}" + (f" · oluşturma {gen}" if gen else ""))
    doc.multiBuild(story, maxPasses=4)
    return buf.getvalue()
