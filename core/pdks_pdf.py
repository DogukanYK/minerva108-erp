# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
PDKS aylık puantaj ÇİZELGESİ (tik'li grid) PDF üreticisi — reportlab.

Patronun istediği tek sayfalık klasik form (Ağustos 2026'da elle üretilen
taslağın sistemleştirilmişi): satır = ayın günü, sütun = personel, hücre =
✓ / R / İ / D / ! / — işareti.  Süre dökümü İSTENMEDİ — o detay Excel
raporunda (core/pdks_report.py) yaşamaya devam eder.

Girdi: routers/pdks._report_data() çıktısı (Excel ile aynı sözleşme).
Font: core/monthly_report._register_fonts() — Türkçe + ✓ glifi için DejaVu
(prod Ubuntu) / Arial (mac dev) TTF'i şart, WinAnsi Helvetica'da İ/ğ/✓ yok.
"""
from datetime import date

from core.monthly_report import _register_fonts
from core.pdks import WEEKDAY_LABELS


def puantaj_grid_filename(year: int, month: int) -> str:
    return f"puantaj_cizelge_{year}-{month:02d}.pdf"


# Hücre işaretleri — öncelik sırası render_puantaj_grid_pdf._mark içinde.
LEGEND = ("✓ geldi · R raporlu · İ izinli · D devamsız · ! çıkış eksik · "
          "T resmi tatil · — işe girişten önce / ayrılıştan sonra · "
          "gri satır = hafta tatili. İzin satırı yalnız programlı iş günlerini "
          "sayar; hafta sonuna taşan izin, izin hakkından düşmez.")


def render_puantaj_grid_pdf(data: dict) -> bytes:
    import io

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)

    font, font_b = _register_fonts()
    NAVY = colors.HexColor("#232E6E")
    GREEN = colors.HexColor("#15803d")
    RED = colors.HexColor("#b91c1c")
    ORANGE = colors.HexColor("#b45309")
    TEAL = colors.HexColor("#0e7490")
    GREY_BG = colors.HexColor("#eceef4")
    LIGHT = colors.HexColor("#eef0f8")
    MUTED = colors.HexColor("#9ca3af")
    R_BG = colors.HexColor("#fde8d4")
    I_BG = colors.HexColor("#d6f0ef")
    BORDER = colors.HexColor("#c8cdda")

    year, month = data["year"], data["month"]
    employees = data["employees"]
    ndays = max(len(e["month"]["days"]) for e in employees) if employees else 30

    # ── Hücre işareti: (metin, renk, zemin|None) ────────────────────────────
    def _mark(day):
        st = day["status"]
        if day["worked_minutes"] > 0:
            return "✓", GREEN, None
        if st == "eksik_cikis":
            return "!", ORANGE, None
        if st == "izinli":
            if day.get("leave_type") == "raporlu":
                return "R", ORANGE, R_BG
            return "İ", TEAL, I_BG
        if st in ("ayrildi", "baslamadi"):
            return "—", MUTED, None
        if st == "resmi_tatil":
            return "T", MUTED, None
        if st == "devamsiz":
            return "D", RED, None
        return "", MUTED, None          # hafta_tatili / programsiz / bekliyor

    # ── Tablo gövdesi ───────────────────────────────────────────────────────
    st_head = ParagraphStyle("th", fontName=font_b, fontSize=8,
                             textColor=colors.white, alignment=1, leading=9.5)
    st_head_sub = ParagraphStyle("ths", parent=st_head, fontName=font,
                                 fontSize=6.4)
    header = [Paragraph("Tarih", st_head)]
    for e in employees:
        emp = e["employee"]
        parts = (emp["full_name"] or "").split()
        note = ""
        if emp.get("end_date_label"):
            note = f"ayrıldı {emp['end_date_label'][:5]}"
        elif emp.get("start_date") and emp["start_date"] >= f"{year}-{month:02d}-02":
            note = f"başladı {emp['start_date_label'][:5]}"
        header.append([Paragraph(parts[0] if parts else "—", st_head),
                       Paragraph(" ".join(parts[1:]) or " ", st_head_sub)]
                      + ([Paragraph(note, st_head_sub)] if note else []))

    rows = [header]
    style = [
        ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME", (0, 0), (-1, -1), font),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("LEFTPADDING", (0, 1), (0, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
    ]

    for i in range(ndays):
        d = date.fromisoformat(employees[0]["month"]["days"][i]["date"]) \
            if employees else date(year, month, i + 1)
        wd = d.weekday()
        r = len(rows)
        row = [f"{d.day:02d}.{month:02d}  {WEEKDAY_LABELS[wd][:3]}"]
        if wd >= 5:
            style.append(("BACKGROUND", (0, r), (-1, r), GREY_BG))
            style.append(("TEXTCOLOR", (0, r), (-1, r), MUTED))
        else:
            style.append(("TEXTCOLOR", (0, r), (0, r), colors.HexColor("#1c2333")))
        for c, e in enumerate(employees, start=1):
            day = e["month"]["days"][i]
            if wd >= 5:
                # Hafta sonu — izne/işarete boyanmaz (izin hakkından sayılmıyor);
                # tatilde fiilen çalışıldıysa ✓ yine görünür.
                txt, col, bg = ("✓", GREEN, None) \
                    if day["worked_minutes"] > 0 else ("", MUTED, None)
            else:
                txt, col, bg = _mark(day)
            row.append(txt)
            if txt:
                style.append(("TEXTCOLOR", (c, r), (c, r), col))
                style.append(("FONTNAME", (c, r), (c, r), font_b))
            if bg is not None:
                style.append(("BACKGROUND", (c, r), (c, r), bg))
        rows.append(row)

    # ── Toplam satırları ────────────────────────────────────────────────────
    r = len(rows)
    gun_row = ["Toplam Gün"] + [
        sum(1 for day in e["month"]["days"] if day["worked_minutes"] > 0)
        for e in employees]
    izin_row = ["İzin"] + [
        sum(e["month"]["totals"]["izin_gunleri"].values()) or "—"
        for e in employees]
    rows.append(gun_row)
    rows.append(izin_row)
    style += [
        ("BACKGROUND", (0, r), (-1, r), LIGHT),
        ("FONTNAME", (0, r), (-1, r + 1), font_b),
        ("LINEABOVE", (0, r), (-1, r), 1.2, NAVY),
        ("BACKGROUND", (0, r + 1), (-1, r + 1), colors.HexColor("#f6f7fb")),
        ("TEXTCOLOR", (0, r + 1), (-1, r + 1), colors.HexColor("#445566")),
    ]

    # ── Belge ───────────────────────────────────────────────────────────────
    ncols = len(employees)
    col_w = [24 * mm] + [min(28.0, 166.0 / max(1, ncols)) * mm] * ncols
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, topMargin=11 * mm, bottomMargin=11 * mm,
        leftMargin=10 * mm, rightMargin=10 * mm,
        title=f"Puantaj {data['month_label']}")
    st_h1 = ParagraphStyle("h1", fontName=font_b, fontSize=15, textColor=NAVY,
                           leading=18, spaceAfter=1)
    st_sub = ParagraphStyle("sub", fontName=font, fontSize=8.2,
                            textColor=colors.HexColor("#667788"), spaceAfter=6)
    st_leg = ParagraphStyle("leg", fontName=font, fontSize=8,
                            textColor=colors.HexColor("#445566"),
                            leading=11.5, spaceBefore=8)
    story = [
        Paragraph(f"Puantaj — {data['month_label']}", st_h1),
        Paragraph("Minerva 108 Kozmetik · IMS Personel Devam Takip Sistemi",
                  st_sub),
        Table(rows, colWidths=col_w, style=TableStyle(style), repeatRows=1),
        Paragraph(LEGEND, st_leg),
        Spacer(1, 1),
    ]
    doc.build(story)
    return buf.getvalue()
