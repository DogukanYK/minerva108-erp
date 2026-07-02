"""
Ürün iade belgesi PDF üretici — antetli A4 (core.delivery_note altyapısı).

  render_return_pdf(view) -> bytes    "ÜRÜN İADE BELGESİ" — künye + sağlam/hasarlı
                                      ayrımlı kalem tablosu + imza alanı.

Ortak A4/antetli/auto-fit altyapısı core.delivery_note'tan paylaşılır
(render_autofit + merge_letterhead + slug_part); Türkçe-uyumlu fontlar
core.monthly_report._register_fonts'tan.
"""
from html import escape

from core.delivery_note import render_autofit, merge_letterhead, slug_part, _fmt


def return_doc_filename(document_no, who=None, ext: str = "pdf") -> str:
    no = (str(document_no) or "iade").replace("/", "-").replace(" ", "_")
    w = slug_part(who)
    return f"iade_belgesi_{w + '_' if w else ''}{no}.{ext}"


def _return_story(view: dict, s: float = 1.0):
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle
    from core.monthly_report import _register_fonts

    font, font_b = _register_fonts()
    NAVY = colors.HexColor("#232E6E")
    LIGHT = colors.HexColor("#f5f0e8")
    GREY = colors.HexColor("#e5e7eb")
    GREEN = colors.HexColor("#15803d")
    RED = colors.HexColor("#b45309")

    st_h1 = ParagraphStyle("h1", fontName=font_b, fontSize=15 * s, textColor=NAVY,
                           spaceAfter=2 * s, leading=18 * s)
    st_meta = ParagraphStyle("meta", fontName=font, fontSize=8 * s,
                             textColor=colors.HexColor("#6b7280"), leading=11 * s)
    st_h2 = ParagraphStyle("h2", fontName=font_b, fontSize=10.5 * s, textColor=NAVY,
                           spaceBefore=11 * s, spaceAfter=4 * s, leading=13 * s)
    st_cell = ParagraphStyle("cell", fontName=font, fontSize=8.5 * s,
                             textColor=colors.HexColor("#374151"), leading=11 * s)
    st_ok = ParagraphStyle("ok", fontName=font_b, fontSize=8.5 * s, textColor=GREEN, leading=11 * s)
    st_bad = ParagraphStyle("bad", fontName=font_b, fontSize=8.5 * s, textColor=RED, leading=11 * s)
    st_hcell = ParagraphStyle("hcell", fontName=font_b, fontSize=8.5 * s,
                              textColor=colors.white, leading=11 * s)
    st_sig = ParagraphStyle("sig", fontName=font, fontSize=9 * s,
                            textColor=colors.HexColor("#374151"), leading=14 * s)

    pad = 3.5 * s
    W = 174.0
    story = [Paragraph("ÜRÜN İADE BELGESİ", st_h1),
             Paragraph(f"Belge No: {escape(str(view.get('document_no') or '—'))}  ·  "
                       f"Tarih: {escape(str(view.get('date') or '—'))}", st_meta),
             Spacer(1, 8 * s)]

    # Künye
    story.append(Paragraph("İade Bilgisi", st_h2))
    info = []
    if view.get("delivery_document_no"):
        info.append(("Bağlı Teslimat", view["delivery_document_no"]))
    info.append(("Kanal", view.get("channel_label") or "—"))
    if view.get("returned_by"):
        info.append(("İade Eden", view["returned_by"]))
    info.append(("Teslim Alan", view.get("received_by") or "—"))
    if view.get("reason"):
        info.append(("Gerekçe", view["reason"]))
    kv = Table([[Paragraph(escape(str(k)), st_cell), Paragraph(escape(str(v)), st_cell)]
                for k, v in info], colWidths=[44 * mm, 130 * mm])
    kv.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, GREY),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHT]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(kv)

    # Kalemler — sağlam/hasarlı ayrımlı
    story.append(Paragraph("İade Edilen Ürünler", st_h2))
    head = [Paragraph(escape(h), st_hcell)
            for h in ("#", "Ürün", "Miktar", "Birim", "Durum", "Stok")]
    body = []
    for i, it in enumerate(view.get("items", [])):
        ok = bool(it.get("restocked"))
        body.append([
            Paragraph(str(i + 1), st_cell),
            Paragraph(escape(str(it.get("item_name") or "—")), st_cell),
            Paragraph(_fmt(it.get("quantity")), st_cell),
            Paragraph(escape(str(it.get("unit") or "")), st_cell),
            Paragraph(escape(str(it.get("condition_label") or "—")), st_ok if ok else st_bad),
            Paragraph("Stoğa alındı" if ok else "Stoğa alınmadı — fire", st_ok if ok else st_bad),
        ])
    t = Table([head] + body, colWidths=[W * 0.05 * mm, W * 0.37 * mm, W * 0.11 * mm,
                                        W * 0.09 * mm, W * 0.14 * mm, W * 0.24 * mm],
              repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.4, GREY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t)

    # Özet
    story.append(Spacer(1, 4 * s))
    story.append(Paragraph(
        f"Stoğa dönen: {_fmt(view.get('restocked_qty') or 0)} · "
        f"Fire (stoğa alınmadı): {_fmt(view.get('damaged_qty') or 0)}", st_meta))

    if view.get("note"):
        story.append(Paragraph("Not", st_h2))
        story.append(Paragraph(escape(str(view["note"])), st_cell))

    story.append(Spacer(1, 22 * s))
    story.append(Paragraph("İadeyi Teslim Alan: ______________________________"
                           "&nbsp;&nbsp;&nbsp; İmza: ________________", st_sig))
    story.append(Spacer(1, 10 * s))
    story.append(Paragraph(
        "Bu belge Minerva 108 ERP iade kaydından üretilmiştir. Sağlam kalemler stoğa "
        "geri alınmış, hasarlı/açılmış kalemler stok dışı (fire) olarak kaydedilmiştir.", st_meta))
    return story


def render_return_pdf(view: dict) -> bytes:
    from reportlab.lib.units import mm
    content = render_autofit(
        lambda s: _return_story(view, s),
        margins=(20 * mm, 20 * mm, 48 * mm, 30 * mm),
        doc_kwargs={"title": f"İade Belgesi — {view.get('document_no') or ''}",
                    "author": "Minerva 108"})
    return merge_letterhead(content)
