"""
Teslim belgesi PDF üretici — hediye / numune teslimatları için imzalı belge.

Alıcı ad-soyadını sistemden giren kullanıcı belgeyi basar; teslim alan kişi
yalnızca imzalar. core/qc_report.py + monthly_report._register_fonts desenini
paylaşır (Türkçe-uyumlu font).
"""
from html import escape
from io import BytesIO


TYPE_LABELS = {"hediye": "Hediye", "numune": "Numune", "diger": "Diğer", "diğer": "Diğer"}
METHOD_LABELS = {"elden": "Elden Teslim", "kargo": "Kargo"}


def _fmt(n) -> str:
    """Miktarı sade göster: 4.0 → '4', 1.5 → '1.5'."""
    try:
        f = float(n)
        return str(int(f)) if f == int(f) else f"{f:g}"
    except (TypeError, ValueError):
        return str(n)


def delivery_doc_filename(document_no, ext: str = "pdf") -> str:
    no = (str(document_no) or "teslim").replace("/", "-").replace(" ", "_")
    return f"teslim_belgesi_{no}.{ext}"


def render_delivery_pdf(view: dict) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle)
    from core.monthly_report import _register_fonts

    font, font_b = _register_fonts()
    NAVY  = colors.HexColor("#232E6E")
    LIGHT = colors.HexColor("#f5f0e8")
    GREY  = colors.HexColor("#e5e7eb")

    st_h1    = ParagraphStyle("h1", fontName=font_b, fontSize=16, textColor=NAVY,
                              spaceAfter=2, leading=19)
    st_meta  = ParagraphStyle("meta", fontName=font, fontSize=8,
                              textColor=colors.HexColor("#6b7280"), leading=11)
    st_h2    = ParagraphStyle("h2", fontName=font_b, fontSize=11, textColor=NAVY,
                              spaceBefore=13, spaceAfter=5, leading=14)
    st_cell  = ParagraphStyle("cell", fontName=font, fontSize=8.5,
                              textColor=colors.HexColor("#374151"), leading=11)
    st_hcell = ParagraphStyle("hcell", fontName=font_b, fontSize=8.5,
                              textColor=colors.white, leading=11)
    st_note  = ParagraphStyle("note", fontName=font, fontSize=9,
                              textColor=colors.HexColor("#374151"), leading=13)
    st_sig   = ParagraphStyle("sig", fontName=font, fontSize=9,
                              textColor=colors.HexColor("#374151"), leading=14)
    st_sigb  = ParagraphStyle("sigb", fontName=font_b, fontSize=9.5, textColor=NAVY, leading=13)

    W = 174.0  # kullanılabilir içerik genişliği (mm)
    story = []

    def _kv_table(rows, widths):
        data = [[Paragraph(escape(str(k)), st_cell), Paragraph(escape(str(v)), st_cell)]
                for k, v in rows]
        t = Table(data, colWidths=widths)
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, GREY),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHT]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        return t

    def _grid_table(headers, rows, widths):
        head = [Paragraph(escape(str(h)), st_hcell) for h in headers]
        body = [[Paragraph(escape(str(c)), st_cell) for c in r] for r in rows]
        t = Table([head] + body, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
            ("GRID", (0, 0), (-1, -1), 0.4, GREY),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (2, 0), (-1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        return t

    # Başlık
    story.append(Paragraph("Minerva 108 — Teslim Belgesi", st_h1))
    story.append(Paragraph(
        f"Belge No: {escape(str(view.get('document_no') or '—'))}  ·  "
        f"Tarih: {escape(str(view.get('date') or '—'))}", st_meta))
    story.append(Spacer(1, 8))

    # Teslimat künyesi
    story.append(Paragraph("Teslimat Bilgisi", st_h2))
    info = [("Teslim Alan", view.get("recipient_name") or "—")]
    if view.get("recipient_org"):
        info.append(("Firma / Kurum", view["recipient_org"]))
    if view.get("recipient_phone"):
        info.append(("Telefon", view["recipient_phone"]))
    info.append(("Teslimat Türü", view.get("type_label") or "—"))
    info.append(("Yöntem", view.get("method_label") or "—"))
    info.append(("Teslim Eden", view.get("dispatched_by") or "—"))
    story.append(_kv_table(info, [W * 0.32 * mm, W * 0.68 * mm]))

    # Ürünler
    story.append(Paragraph("Teslim Edilen Ürünler", st_h2))
    rows = [[i + 1, it["item_name"], _fmt(it["quantity"]), (it.get("unit") or "")]
            for i, it in enumerate(view.get("items", []))]
    story.append(_grid_table(["#", "Ürün", "Miktar", "Birim"], rows,
                             [W * 0.08 * mm, W * 0.62 * mm, W * 0.16 * mm, W * 0.14 * mm]))

    # Not
    if view.get("note"):
        story.append(Paragraph("Not", st_h2))
        story.append(Paragraph(escape(str(view["note"])), st_note))

    # İmza alanı — teslim eden + teslim alan
    story.append(Spacer(1, 28))
    sig_left = [Paragraph("Teslim Eden", st_sigb),
                Paragraph(escape(str(view.get("dispatched_by") or "—")), st_cell),
                Spacer(1, 20), Paragraph("İmza: ____________________", st_sig)]
    sig_right = [Paragraph("Teslim Alan", st_sigb),
                 Paragraph(escape(str(view.get("recipient_name") or "—")), st_cell),
                 Spacer(1, 20), Paragraph("İmza: ____________________", st_sig)]
    sig = Table([[sig_left, sig_right]], colWidths=[W * 0.5 * mm, W * 0.5 * mm])
    sig.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), 14),
        ("LEFTPADDING", (1, 0), (1, 0), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(sig)

    story.append(Spacer(1, 16))
    story.append(Paragraph(
        "Bu belge Minerva 108 ERP teslimat kaydından üretilmiştir. "
        "Yukarıda belirtilen ürünler eksiksiz teslim alınmıştır.", st_meta))

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Teslim Belgesi — {view.get('document_no') or ''}", author="Minerva 108 ERP")
    doc.build(story)
    return buf.getvalue()
