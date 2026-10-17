# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Ortak rapor tasarım sistemi (reportlab) — 16.10.2026, Satın Alma Planı'nın
yeniden tasarımıyla geldi; başka raporlar da kullanabilir.

    Theme()                 palet + Liberation Sans + paragraf stilleri + yardımcılar
    Theme.table()           GRID'siz tablo: başlık altında 0,8 pt lacivert çizgi,
                            satır arası kıl çizgi, zebra, dolgu 3/4, repeatRows=1,
                            splitInRow=1
    Theme.cover_band()      logo + rapor adı + senaryo + stok tarihi + para birimi
    Theme.kpi_row()         3×2 kutu (küçük etiket + 15 pt değer), sol altın şerit
    Theme.hbar_chart()      yatay çubuk (HorizontalBarChart): çubuk ≤ 7 mm, kenar
                            çizgisi yok, yalnız değer ekseninde kıl ızgara, TR sayı
                            biçimi, ≥2 seride lejant, yalnız en büyük çubukta etiket
                            (çok seride her serinin en büyüğünde), metin mürekkep
                            renginde; kategori etiketi sütunu veriden
                            ölçülür (etiket çubuğun soluna taşmaz); çok seride ilk
                            seri grubun EN ÜST çubuğu (lejant sırasıyla aynı)
    Theme.callout()         sol şeritli vurgu kutusu (özet / uyarı)
    Bookmark                sıfır yükseklikli akış: bookmarkPage + addOutlineEntry
                            (PDF yer imleri; panel yalnız yer imi olan belgede
                            açık) + içindekiler için sayfa kaydı
    TocPanel                içindekiler paneli — sayfa numaraları bir önceki
                            derleme geçişinden, satırlar `#anahtar` bağlantılı
    ReportDoc               BaseDocTemplate + çok geçişli derleme (multiBuild):
                            "Sayfa X / Y" toplamı ve içindekiler sayfa numaraları
                            önceki geçişten gelir, sonuç değişmeyince durur
                            (genelde 2 geçiş)
    make_doc()              dikey "exec" + yatay "ops" sayfa şablonlu belge;
                            `NextPageTemplate("ops")` + `PageBreak()` ile geçilir
    page_decorator()        her sayfada üstte altın kıl çizgi + "Minerva 108 ·
                            rapor adı", altta sol metin + sağda "Sayfa X / Y"
                            (karışık yönlerde canvas'ın kendi `_pagesize`'ı)

Yazı tipi: `proforma_template._fonts()` → depodaki Liberation Sans (Mac, test ve
canlı aynı; Arial ölçülü).  ⚠ (U+26A0) Liberation'da yok → `warn_font()` yalnız
işaret için DejaVu / Apple Symbols kaydeder, hiçbiri yoksa kalın kırmızı "!".

Tipografi: gövde 9,5/13, küçük 8,5/11, h1 16/20, h2 13/17, h3 11/14 — en fazla
üç başlık düzeyi, başlık kalın (büyük harf değil), sayılar sağa yaslı, tek yazı
tipi ailesi, cömert kenar boşluğu (dikey 18 mm, yatay 14 mm).

Palet doğrulaması (dataviz `scripts/validate_palette.js`, `--mode light`,
16.10.2026):
  • Marka üçlüsü lacivert #232E6E · altın #C9AC6F · gri #8a8f9c → FAILED:
    lacivert L 0,33 (bant 0,43–0,77 dışı), altın C 0,086 ve gri C 0,02 (kroma
    tabanı 0,10 altı), altın yüzey kontrastı 2,13.
  • Koyu altın #B08A4A ile → FAILED: kroma 0,094, normal görüş ΔE 11,4 < 15.
  • CHART_SERIES = #3B5BC4 (lacivert tonu) · #BD8A10 (altın tonu) · #2A9D8F
    (yeşil-mavi) → ALL CHECKS PASS: L bandı ✓, kroma ✓, CVD ΔE 12,8 (protan) ✓,
    normal görüş ΔE 18,7 ✓, yüzey kontrastı üçü de ≥ 3:1 ✓.
  Marka laciverti ve altını bantlarda, başlıklarda ve KPI kutularında (veri
  işareti olmayan yerlerde) kalır; gri yalnız vurgu dışı çubuk için.
"""
import os
import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from reportlab.platypus import Flowable
from reportlab.platypus.doctemplate import BaseDocTemplate

from core.proforma_template import LOGO, _fonts
from core.purchase_plan import tr_num

PALETTE = {
    "navy": "#232E6E", "gold": "#C9AC6F", "ink": "#1b1f2a", "muted": "#5b6170",
    "line": "#d9dde6", "zebra": "#f5f6f9", "warn": "#9a3412", "warn_bg": "#fdf3ea",
    "ok_bg": "#f3f6f1", "tint": "#f7f5ef", "white": "#ffffff",
}
# Grafik serileri — doğrulama sonucu modül başlığında
CHART_SERIES = ("#3B5BC4", "#BD8A10", "#2A9D8F")
CHART_MUTED = "#c3c7d1"
WARN_GLYPH = "⚠"
_SYMBOL_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Apple Symbols.ttf")

# Kenar boşlukları (mm): sol, sağ, üst, alt
PORTRAIT_MARGINS_MM = (18, 18, 22, 18)
LANDSCAPE_MARGINS_MM = (14, 14, 20, 16)
PORTRAIT_W_MM = 210 - PORTRAIT_MARGINS_MM[0] - PORTRAIT_MARGINS_MM[1]      # 174
LANDSCAPE_W_MM = 297 - LANDSCAPE_MARGINS_MM[0] - LANDSCAPE_MARGINS_MM[1]   # 269


_APOSTROPHE = re.compile(r"(?<=\w)'(?=\w)")


def e(v) -> str:
    """reportlab Paragraph'ına giren her dinamik metin kaçışlanır; iki harf
    arasındaki düz kesme işareti tipografik olur ("kg'lık" → "kg’lık" — veri
    ortak metinden gelir, önizleme / Excel düz kalır; PDF tek tip)."""
    return escape(_APOSTROPHE.sub("\u2019", "" if v is None else str(v)))


def fonts() -> Tuple[str, str]:
    """(normal, kalın) — depodaki Liberation Sans ("PFA" / "PFA-B")."""
    return _fonts()


def warn_font(font: str) -> Optional[str]:
    """⚠ işaretini basabilen font adı (ana font, yoksa kayıtlı sembol fontu)
    ya da None."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    def has(name):
        try:
            face = getattr(pdfmetrics.getFont(name), "face", None)
            return face is not None and ord(WARN_GLYPH) in getattr(face, "charToGlyph", {})
        except Exception:
            return False
    if has(font):
        return font
    if "RPT-SYM" in pdfmetrics.getRegisteredFontNames() and has("RPT-SYM"):
        return "RPT-SYM"
    for path in _SYMBOL_FONTS:
        if os.path.isfile(path):
            try:
                pdfmetrics.registerFont(TTFont("RPT-SYM", path))
            except Exception:
                continue
            if has("RPT-SYM"):
                return "RPT-SYM"
    return None


def axis_places(ticks: Sequence[float], most: int = 3) -> int:
    """Eksen işaretlerinin hepsini TAM yazan en az ondalık hane (0–`most`):
    [0, 50, 100] → 0; [0, 0.5, 1] → 1; [0, 0.05, 0.1] → 2."""
    for d in range(most + 1):
        if all(abs(t * 10 ** d - round(t * 10 ** d)) < 1e-6 for t in ticks):
            return d
    return most


def truncate(text: str, n: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= n else text[:max(1, n - 1)].rstrip() + "…"


class Theme:
    """Font + stil + renk; rapor modülü bunu alt sınıflar ya da sarar."""

    def __init__(self):
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle as P

        self.font, self.font_b = fonts()
        f, fb = self.font, self.font_b
        self.colors = colors
        self.c = {k: colors.HexColor(v) for k, v in PALETTE.items()}
        # Tablo hücresindeki düz metin ("" dahil) reportlab'ın varsayılanı
        # Helvetica ile yazılır; her TableStyle bunu başa alır (tek yazı tipi ailesi)
        self.font_cmd = ("FONTNAME", (0, 0), (-1, -1), f)
        navy, ink, muted, warn = self.c["navy"], self.c["ink"], self.c["muted"], self.c["warn"]
        self.S: Dict[str, P] = {
            "h1": P("h1", fontName=fb, fontSize=16, leading=20, textColor=navy, spaceAfter=3),
            "h2": P("h2", fontName=fb, fontSize=13, leading=17, textColor=navy, spaceBefore=9, spaceAfter=3),
            "h3": P("h3", fontName=fb, fontSize=11, leading=14, textColor=navy, spaceBefore=6, spaceAfter=2),
            "kicker": P("kicker", fontName=f, fontSize=8.5, leading=11, textColor=muted),
            "sub": P("sub", fontName=f, fontSize=9.5, leading=13, textColor=muted, spaceAfter=4),
            "body": P("body", fontName=f, fontSize=9.5, leading=13, textColor=ink),
            "small": P("small", fontName=f, fontSize=8.5, leading=11, textColor=muted),
            "cell": P("cell", fontName=f, fontSize=9.5, leading=13, textColor=ink),
            "cells": P("cells", fontName=f, fontSize=8.5, leading=11, textColor=ink),
            "num": P("num", fontName=f, fontSize=9.5, leading=13, textColor=ink, alignment=2),
            "numb": P("numb", fontName=fb, fontSize=9.5, leading=13, textColor=ink, alignment=2),
            "nums": P("nums", fontName=f, fontSize=8.5, leading=11, textColor=ink, alignment=2),
            "th": P("th", fontName=fb, fontSize=9, leading=12, textColor=navy),
            "thr": P("thr", fontName=fb, fontSize=9, leading=12, textColor=navy, alignment=2),
            "bullet": P("bullet", fontName=f, fontSize=9.5, leading=13, textColor=ink, leftIndent=9,
                        bulletIndent=0, spaceAfter=1.5, bulletFontName=f, bulletFontSize=9.5),
            "foot": P("foot", fontName=f, fontSize=8, leading=10.5, textColor=muted, leftIndent=9,
                      bulletIndent=0),
            "kpi_label": P("kpi_label", fontName=f, fontSize=8.5, leading=11, textColor=muted),
            "kpi_value": P("kpi_value", fontName=fb, fontSize=15, leading=18, textColor=navy, spaceBefore=2),
            "kpi_note": P("kpi_note", fontName=f, fontSize=8, leading=10, textColor=muted, spaceBefore=1),
            # İçindekiler sık dizilir (9/11,5, dolgu 1,2): kapak sayfasına sığsın
            "toc": P("toc", fontName=f, fontSize=9, leading=11.5, textColor=ink),
            "toc1": P("toc1", fontName=f, fontSize=8.5, leading=11, textColor=muted, leftIndent=10),
            "tocn": P("tocn", fontName=f, fontSize=9, leading=11.5, textColor=ink, alignment=2),
            "warn": P("warn", fontName=fb, fontSize=9.5, leading=13, textColor=warn),
        }
        wf = warn_font(f)
        self.warn_mark = (f"<font name='{wf}' color='{PALETTE['warn']}'>{WARN_GLYPH}</font>" if wf
                          else f"<font name='{fb}' color='{PALETTE['warn']}'>!</font>")

    # ── metin parçaları (girdi önceden kaçışlanmış olmalı) ────────────────
    def bold(self, t: str) -> str:
        return f"<font name='{self.font_b}'>{t}</font>"

    def grey(self, t: str) -> str:
        return f"<font color='{PALETTE['muted']}'>{t}</font>"

    def red(self, t: str) -> str:
        return f"<font color='{PALETTE['warn']}' name='{self.font_b}'>{t}</font>"

    def small(self, t: str) -> str:
        return f"<font size='8' color='{PALETTE['muted']}'>{t}</font>"

    def marks(self, t: str) -> str:
        """Kaçışlanmış metindeki ⚠ işaretini basılabilir işaretle değiştirir."""
        return t.replace(WARN_GLYPH, self.warn_mark)

    # ── tablo ──────────────────────────────────────────────────────────────
    def table(self, data, widths_mm: Sequence[float], *, header: bool = True, zebra: bool = True,
              split_in_row: bool = True, right_cols: Sequence[int] = (), total_row: bool = False,
              valign: str = "TOP", pad: Tuple[float, float] = (3, 4)):
        from reportlab.lib.units import mm
        from reportlab.platypus import Table, TableStyle

        tb, lr = pad
        st = [("VALIGN", (0, 0), (-1, -1), valign),
              ("TOPPADDING", (0, 0), (-1, -1), tb), ("BOTTOMPADDING", (0, 0), (-1, -1), tb),
              ("LEFTPADDING", (0, 0), (-1, -1), lr), ("RIGHTPADDING", (0, 0), (-1, -1), lr)]
        first = 1 if header else 0
        if header:
            st += [("LINEBELOW", (0, 0), (-1, 0), 0.8, self.c["navy"]), ("BOTTOMPADDING", (0, 0), (-1, 0), tb + 1),
                   ("VALIGN", (0, 0), (-1, 0), "BOTTOM")]
        if len(data) > first:
            st.append(("LINEBELOW", (0, first), (-1, -1), 0.3, self.c["line"]))
        if zebra:
            st += [("BACKGROUND", (0, i), (-1, i), self.c["zebra"]) for i in range(first + 1, len(data), 2)]
        if total_row and len(data) > first:
            st += [("LINEABOVE", (0, -1), (-1, -1), 0.8, self.c["navy"]),
                   ("LINEBELOW", (0, -1), (-1, -1), 0, self.c["white"]),
                   ("BACKGROUND", (0, -1), (-1, -1), self.c["white"])]
            if len(data) > first + 1:
                # Toplam satırı tek başına yeni sayfaya düşmesin (son veri satırıyla birlikte)
                st.append(("NOSPLIT", (0, -2), (-1, -1)))
        for col in right_cols:
            st.append(("ALIGN", (col, 0), (col, -1), "RIGHT"))
        # splitInRow: notları çok uzun tek bir satır sayfadan taşarsa reportlab
        # "Flowable too large" atıp bütün PDF'i düşürürdü; satır bölünsün.
        t = Table(data, colWidths=[w * mm for w in widths_mm], repeatRows=1 if header else 0,
                  splitInRow=1 if split_in_row else 0)
        t.setStyle(TableStyle([self.font_cmd] + st))
        t.hAlign = "LEFT"
        return t

    # ── bloklar ────────────────────────────────────────────────────────────
    def cover_band(self, *, title: str, kicker: str, lines: Sequence[str], width_mm: float,
                   logo=LOGO, logo_w_mm: float = 36) -> list:
        """Logo (solda) + kısa üst satır + h1 başlık + bilgi satırları; altında
        0,8 pt altın çizgi.  `title`/`lines` kaçışlanmış (biçim etiketli) metin."""
        from reportlab.lib.units import mm
        from reportlab.platypus import Image, Paragraph, Spacer, Table, TableStyle

        right = [Paragraph(kicker, self.S["kicker"]), Paragraph(title, self.S["h1"])]
        right += [Paragraph(ln, self.S["sub"]) for ln in lines if ln]
        left: list = []
        if logo and os.path.isfile(str(logo)):
            # proforma_logo.png 637×302
            left = [Image(str(logo), width=logo_w_mm * mm, height=logo_w_mm * 302 / 637 * mm)]
        lw = (logo_w_mm + 8) * mm if left else 0
        t = Table([[left, right]], colWidths=[lw, width_mm * mm - lw])
        t.setStyle(TableStyle([self.font_cmd, ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                               ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                               ("LINEBELOW", (0, 0), (-1, 0), 0.8, self.c["gold"])]))
        t.hAlign = "LEFT"
        return [t, Spacer(1, 5 * mm)]

    def kpi_row(self, items: Sequence[dict], *, width_mm: float, cols: int = 3):
        """[{label, text, note?}] → `cols` sütunlu kutu ızgarası (3×2 için 6 öğe).
        Kutu: açık zemin, solda 1,5 pt altın şerit, küçük etiket + 15 pt değer."""
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, Table, TableStyle

        items = list(items)
        if not items:
            return None
        gap = 3 * mm
        box = (width_mm * mm - gap * (cols - 1)) / cols
        rows, style = [], [("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                           ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]
        widths: List[float] = []
        for c in range(cols):
            widths.append(box)
            if c < cols - 1:
                widths.append(gap)
        for r0 in range(0, len(items), cols):
            chunk = items[r0:r0 + cols]
            row: list = []
            for c in range(cols):
                if c < len(chunk):
                    it = chunk[c]
                    cell = [Paragraph(e(it["label"]), self.S["kpi_label"]),
                            Paragraph(e(it["text"]), self.S["kpi_value"])]
                    if it.get("note"):
                        cell.append(Paragraph(e(it["note"]), self.S["kpi_note"]))
                    row.append(cell)
                    ri = len(rows)
                    style += [("BACKGROUND", (2 * c, ri), (2 * c, ri), self.c["tint"]),
                              ("LINEBEFORE", (2 * c, ri), (2 * c, ri), 1.5, self.c["gold"]),
                              ("LEFTPADDING", (2 * c, ri), (2 * c, ri), 7), ("RIGHTPADDING", (2 * c, ri), (2 * c, ri), 6),
                              ("TOPPADDING", (2 * c, ri), (2 * c, ri), 6), ("BOTTOMPADDING", (2 * c, ri), (2 * c, ri), 6)]
                else:
                    row.append("")
                if c < cols - 1:
                    row.append("")
            rows.append(row)
            if r0 + cols < len(items):
                rows.append([""] * len(widths))
                style.append(("TOPPADDING", (0, len(rows) - 1), (-1, len(rows) - 1), 0))
        # boşluk satırları: yükseklik
        heights = []
        for i, r in enumerate(rows):
            heights.append(gap if all(x == "" for x in r) else None)
        t = Table(rows, colWidths=widths, rowHeights=heights)
        t.setStyle(TableStyle([self.font_cmd] + style))
        t.hAlign = "LEFT"
        return t

    def callout(self, text: str, *, width_mm: float, kind: str = "ok", style: Optional[str] = None):
        """Sol şeritli kutu: `kind` ok (altın şerit, krem zemin) | warn (turuncu)."""
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, Table, TableStyle

        warn = kind == "warn"
        p = Paragraph(text, self.S[style or ("warn" if warn else "body")])
        # Uzun kutu sayfa sonunda bölünebilsin (şerit ve zemin her parçada kalır)
        t = Table([[p]], colWidths=[width_mm * mm], splitInRow=1)
        t.setStyle(TableStyle([self.font_cmd, ("BACKGROUND", (0, 0), (-1, -1), self.c["warn_bg" if warn else "tint"]),
                               ("LINEBEFORE", (0, 0), (0, -1), 2, self.c["warn" if warn else "gold"]),
                               ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                               ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        t.hAlign = "LEFT"
        return t

    def hbar_chart(self, categories: Sequence[str], series: Sequence[Tuple[str, Sequence[float]]], *,
                   width_mm: float, fmt: Optional[Callable[[float], str]] = None, unit: str = "",
                   label_w_mm: float = 52, bar_mm: float = 3.8, max_chars: int = 34,
                   muted: Sequence[int] = (), label_fmt: Optional[Callable[[float], str]] = None):
        """Yatay çubuk grafik (Drawing).  `series` = [(ad, [değer…]), …]; ≥2 seride
        lejant, tek seride yok (başlık söyler).  Yalnız en büyük çubuk etiketli
        (çok seride her serinin en büyüğü; `barLabelFormat` seri başına liste),
        değer ekseni TR sayı biçiminde, ondalık hanesi eksen ADIMINDAN
        (`axis_places`: "0  50  100", "0  0,25  0,50", sıfır "0"; `fmt` verilirse
        o kullanılır), kıl ızgara; çubuklarda ve lejant örneğinde kenar çizgisi
        yok, 2 px yüzey boşluğu.  `label_fmt` (varsayılan `fmt`, o da yoksa tam
        sayı) çubuk etiketinin biçimi — tablo hücresiyle aynı yazım ("344,6 kg",
        "200 gram") olsun diye eksenden ayrı.  `muted` = gri basılacak kategori sıraları
        (toplam / "Diğer" gibi veri olmayan çubuklar — vurgu dışı).  Veri yoksa None.

        Seri sırası: reportlab grupta 0. seriyi EN ALTA çizer (reverseDirection'a
        bakmaz), lejant ise 0. seriyi önce yazar → veriler ters sırayla verilir,
        lejant verilen sırada kalır; böylece lejantın ilk girdisi grubun üst
        çubuğudur.  Kategori etiketi sütunu (`bc.x`) en geniş etiketten ölçülür
        (en az `label_w_mm`, en çok genişliğin %45'i); sığmayan etiket kısaltılır
        — etiket çerçevenin/komşu sütunun içine taşmaz."""
        from reportlab.graphics.charts.barcharts import HorizontalBarChart
        from reportlab.graphics.charts.legends import Legend
        from reportlab.graphics.shapes import Drawing
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.pdfbase.pdfmetrics import stringWidth

        cats = list(categories)
        ser = [(n, [float(v or 0) for v in vals]) for n, vals in series]
        n, k = len(cats), len(ser)
        if not n or not k or not any(v > 0 for _, vals in ser for v in vals):
            return None
        axis_fmt = fmt
        bar = min(bar_mm, 7) * mm
        gap, ggap = 1.5, max(1.6 * mm, 0.6 * bar)      # grup arası çubukla orantılı (≥ 1,6 mm)
        plot_h = n * (k * bar + (k - 1) * gap + ggap)
        legend_h = 6 * mm if k >= 2 else 1.5 * mm
        axis_h = 6.5 * mm
        W, H = width_mm * mm, plot_h + legend_h + axis_h
        d = Drawing(W, H)
        d.hAlign = "LEFT"
        d.initialFontName, d.initialFontSize = self.font, 8      # Times-Roman varsayılanı yerine
        bc = HorizontalBarChart()
        lab_font, lab_size, lab_dx = self.font, 8, 3
        labels = [truncate(c, max_chars) for c in cats]
        need = max(stringWidth(t, lab_font, lab_size) for t in labels) + lab_dx + 1
        bc.x = min(max(label_w_mm * mm, need), 0.45 * W)
        room = bc.x - lab_dx - 1
        labels = [t if stringWidth(t, lab_font, lab_size) <= room else
                  next(truncate(t, j) for j in range(len(t) - 1, 0, -1)
                       if stringWidth(truncate(t, j), lab_font, lab_size) <= room) for t in labels]
        bc.y = axis_h
        bc.width, bc.height = W - bc.x - 20 * mm, plot_h
        order = list(range(k - 1, -1, -1))              # grafik serisi i → verilen seri order[i]
        bc.data = [ser[j][1] for j in order]
        bc.useAbsolute = 1
        bc.barWidth, bc.barSpacing, bc.groupSpacing = bar, gap, ggap
        bc.strokeColor = None
        bc.fillColor = None
        bc.bars.strokeWidth = 0
        bc.bars.strokeColor = None
        for i, j in enumerate(order):
            bc.bars[i].fillColor = colors.HexColor(CHART_SERIES[j % len(CHART_SERIES)])
            for m in muted:
                if 0 <= m < n:
                    bc.bars[(i, m)].fillColor = colors.HexColor(CHART_MUTED)
        ca = bc.categoryAxis
        ca.categoryNames = labels
        ca.reverseDirection = 1
        ca.labels.fontName, ca.labels.fontSize = lab_font, lab_size
        ca.labels.fillColor = self.c["ink"]
        ca.labels.textAnchor, ca.labels.boxAnchor, ca.labels.dx = "end", "e", -lab_dx
        ca.strokeColor, ca.strokeWidth = self.c["line"], 0.5
        ca.tickLeft = ca.tickRight = 0
        va = bc.valueAxis
        va.valueMin = 0
        va.labels.fontName, va.labels.fontSize = self.font, 7.5
        va.labels.fillColor = self.c["muted"]
        if axis_fmt is None:
            def axis_fmt(v, _va=va):
                # Ondalık hanesi eksenin ADIMINDAN: 0,05 adımlı eksen "0 0,1 0,1 0,2"
                # diye tekrar etmesin, 0,25 "0,2" diye yanlış basılmasın; aynı eksende
                # aynı hane ("0,50  1,00"), sıfır "0"
                ticks = list(getattr(_va, "_tickValues", None) or []) or [v]
                return "0" if v == 0 else tr_num(v, axis_places(ticks))
        va.labelTextFormat = axis_fmt
        va.visibleGrid, va.gridStrokeColor, va.gridStrokeWidth = 1, self.c["line"], 0.4
        va.visibleAxis = 0
        va.tickDown = 0
        va.maximumTicks = 6
        mx = max(v for _, vals in ser for v in vals)

        lfmt = label_fmt or fmt or (lambda v: tr_num(v))

        def label_for(top: float):
            def label(v):
                return (lfmt(v) + (f" {unit}" if unit else "")) if (top > 0 and v == top) else ""
            return label
        # Tek seride yalnız en büyük çubuk; çok seride HER serinin en büyüğü
        # (ikinci seri — "Stoktan" — 10.000'lik eksenden okunmak zorunda kalmasın)
        bc.barLabelFormat = (label_for(mx) if k == 1 else
                             [label_for(max(ser[j][1])) for j in order])
        bc.barLabels.fontName, bc.barLabels.fontSize = self.font, 7.5
        bc.barLabels.fillColor = self.c["ink"]
        bc.barLabels.boxAnchor, bc.barLabels.nudge = "w", 3
        d.add(bc)
        if k >= 2:
            lg = Legend()
            lg.x, lg.y = bc.x, H - 1.5 * mm
            lg.boxAnchor, lg.alignment = "nw", "right"
            lg.fontName, lg.fontSize = self.font, 7.5
            lg.fillColor = self.c["ink"]
            lg.dx = lg.dy = 6
            lg.dxTextSpace, lg.deltax, lg.deltay = 4, 14, 0
            # Örnek kutusu çubuk gibi kenarsız: strokeWidth 0 PDF'te "en ince
            # çizgi" demek (siyah kıl kenar basılıyordu) — rengi de kaldır
            lg.columnMaximum, lg.variColumn, lg.strokeWidth, lg.strokeColor = 1, 1, 0, None
            lg.colorNamePairs = [(colors.HexColor(CHART_SERIES[i % len(CHART_SERIES)]), name)
                                 for i, (name, _) in enumerate(ser)]
            d.add(lg)
        return d


# ─── Yer imi, içindekiler, belge ─────────────────────────────────────────────

class Bookmark(Flowable):
    """Sıfır yükseklikli akış: sayfaya yer imi + PDF ana hat girdisi; belge
    (`ReportDoc.afterFlowable`) içindekiler için sayfa numarasını kaydeder."""

    def __init__(self, key: str, title: str, level: int = 0):
        Flowable.__init__(self)
        self.key, self.title, self.level = key, title, level
        self.width = self.height = 0

    def wrap(self, aW, aH):
        return 0, 0

    def draw(self):
        self.canv.bookmarkPage(self.key)
        try:
            self.canv.addOutlineEntry(self.title, self.key, level=self.level, closed=False)
            # Yer imi paneli yalnız yer imi olan belgede açılır (boş panel yok)
            self.canv.showOutline()
        except Exception:        # düzey atlaması vb. — ana hat kaybı PDF'i düşürmesin
            pass


class TocPanel(Flowable):
    """İçindekiler: [(key, title, level)] satırları + sayfa numarası.  Sayfa
    numaraları `ReportDoc.toc_pages`'ten (önceki geçiş) — ilk geçişte "—";
    satır sayısı sabit olduğu için yükseklik geçişler arasında değişmez."""

    def __init__(self, theme: Theme, entries: Sequence[Tuple[str, str, int]], width_mm: float):
        Flowable.__init__(self)
        self.theme, self.entries, self.width_mm = theme, list(entries), width_mm
        self._t = None

    def _build(self):
        from reportlab.platypus import Paragraph
        pages = getattr(getattr(self.canv, "_doctemplate", None), "toc_pages", None) or {}
        S = self.theme.S
        data = [[Paragraph(f'<a href="#{e(k)}">{t}</a>', S["toc1" if lvl else "toc"]),
                 Paragraph(str(pages.get(k, "—")), S["tocn"])] for k, t, lvl in self.entries]
        self._t = self.theme.table(data, [self.width_mm - 14, 14], header=False, zebra=False,
                                   split_in_row=False, pad=(1.2, 3))

    def wrap(self, aW, aH):
        self._build()
        self.width, self.height = self._t.wrapOn(self.canv, aW, aH)
        return self.width, self.height

    def split(self, aW, aH):
        if self._t is None:
            self._build()
        return self._t.split(aW, aH)

    def draw(self):
        self._t.drawOn(self.canv, 0, 0)


class ReportDoc(BaseDocTemplate):
    """Çok geçişli derleme: `multiBuild` her geçişten sonra `_allSatisfied`'ı
    sorar; toplam sayfa ve yer imi sayfaları bir önceki geçişle aynıysa biter."""

    def __init__(self, *args, **kwargs):
        BaseDocTemplate.__init__(self, *args, **kwargs)
        self.total_pages = 0
        self.toc_pages: Dict[str, int] = {}
        self._pass_pages: Dict[str, int] = {}

    def afterFlowable(self, flowable):
        if isinstance(flowable, Bookmark):
            self._pass_pages[flowable.key] = self.page

    def _allSatisfied(self):
        ok = BaseDocTemplate._allSatisfied(self)
        changed = self.total_pages != self.page or self.toc_pages != self._pass_pages
        self.total_pages = self.page
        self.toc_pages = dict(self._pass_pages)
        self._pass_pages = {}
        return ok and not changed


def page_decorator(theme: Theme, band_title: str, footer_left: str = "", *, band_right: str = ""):
    """`onPage(canv, doc)`: üstte altın kıl çizgi + "Minerva 108 · …" (sağda
    `band_right`), altta `footer_left` + "Sayfa X / Y" (toplam önceki geçişten;
    ilk geçişte "…")."""
    from reportlab.lib.units import mm

    def deco(cv, doc):
        w, h = cv._pagesize
        land = w > h
        ml, mr = (LANDSCAPE_MARGINS_MM if land else PORTRAIT_MARGINS_MM)[:2]
        cv.saveState()
        cv.setFont(theme.font, 7.5)
        cv.setFillColor(theme.c["muted"])
        cv.setStrokeColor(theme.c["gold"])
        cv.setLineWidth(0.6)
        y = h - 11 * mm
        cv.drawString(ml * mm, y, band_title)
        if band_right:
            cv.drawRightString(w - mr * mm, y, band_right)
        cv.line(ml * mm, y - 2.2 * mm, w - mr * mm, y - 2.2 * mm)
        total = doc.total_pages if getattr(doc, "total_pages", 0) else None
        cv.drawString(ml * mm, 8 * mm, footer_left)
        cv.drawRightString(w - mr * mm, 8 * mm, f"Sayfa {doc.page} / {total if total else '…'}")
        cv.restoreState()
    return deco


def make_doc(buf, theme: Theme, *, title: str, band_title: str, footer_left: str = "",
             band_right: str = "", first: str = "exec") -> ReportDoc:
    """Dikey "exec" + yatay "ops" sayfa şablonlu belge; `first` ilk şablon."""
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.platypus import Frame, PageTemplate

    pm, lm = PORTRAIT_MARGINS_MM, LANDSCAPE_MARGINS_MM
    doc = ReportDoc(buf, pagesize=A4, leftMargin=pm[0] * mm, rightMargin=pm[1] * mm, topMargin=pm[2] * mm,
                    bottomMargin=pm[3] * mm, title=title, author="Minerva 108",
                    initialFontName=theme.font, initialFontSize=9.5, initialLeading=13)
    deco = page_decorator(theme, band_title, footer_left, band_right=band_right)
    pw, ph = A4
    lw, lh = landscape(A4)
    fp = Frame(pm[0] * mm, pm[3] * mm, pw - (pm[0] + pm[1]) * mm, ph - (pm[2] + pm[3]) * mm, id="p",
               leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    fl = Frame(lm[0] * mm, lm[3] * mm, lw - (lm[0] + lm[1]) * mm, lh - (lm[2] + lm[3]) * mm, id="l",
               leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    tpls = [PageTemplate(id="exec", frames=[fp], pagesize=A4, onPage=deco),
            PageTemplate(id="ops", frames=[fl], pagesize=landscape(A4), onPage=deco)]
    if first != "exec":
        tpls.reverse()
    doc.addPageTemplates(tpls)
    return doc
