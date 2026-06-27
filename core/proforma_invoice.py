"""
Proforma fatura PDF üretici — antetli kağıt üzerine (core.delivery_note.merge_letterhead).

Düzen sağlanan `PROFORMA boş.xlsx` ile aynı: müşteri bloğu + iki-dilli (EN/TR) başlıklı
kalem tablosu (PRODUCT NAME · WEIGHT(ml) · PIECES · UNIT PRICE · TOTAL) + genel toplam.
Türkçe-uyumlu fontlar core.monthly_report._register_fonts ile paylaşılır. Ürün adı,
teslimat oluşturulurken belge diline göre snapshot edilmiştir (DeliveryItem.item_name).
"""
from html import escape
from io import BytesIO

from core.delivery_note import merge_letterhead, render_autofit, slug_part


def proforma_filename(document_no, recipient=None, ext: str = "pdf") -> str:
    base = (document_no or "proforma").replace("/", "-").replace(" ", "_")
    who = slug_part(recipient)
    return f"{base}{'_' + who if who else ''}.{ext}"


def _money(v, cur) -> str:
    try:
        return f"{float(v):,.2f} {cur}"
    except (TypeError, ValueError):
        return "—"


def _num(v) -> str:
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else f"{f:g}"
    except (TypeError, ValueError):
        return "—" if v in (None, "") else str(v)


def render_proforma_pdf(view: dict) -> bytes:
    from reportlab.lib.units import mm
    content = render_autofit(
        lambda s: _proforma_story(view, s),
        margins=(20 * mm, 20 * mm, 48 * mm, 30 * mm),
        doc_kwargs={"title": f"Proforma — {view.get('document_no') or ''}", "author": "Minerva 108"})
    return merge_letterhead(content)


def _proforma_story(view: dict, s: float = 1.0):
    """Proforma flowable listesi.  `s` = ölçek (1.0 = tam boy; <1 = küçült)."""
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import (Paragraph, Spacer, Table, TableStyle)
    from core.monthly_report import _register_fonts

    font, font_b = _register_fonts()
    NAVY = colors.HexColor("#1f2d52")
    GREY = colors.HexColor("#6b7280")
    LINE = colors.HexColor("#d1d5db")

    st_title = ParagraphStyle("t", fontName=font_b, fontSize=15 * s, textColor=NAVY, leading=18 * s, spaceAfter=2 * s)
    st_sub   = ParagraphStyle("s", fontName=font, fontSize=8.5 * s, textColor=GREY, spaceAfter=10 * s)
    st_lbl   = ParagraphStyle("l", fontName=font_b, fontSize=8.5 * s, textColor=NAVY, leading=10 * s)
    st_val   = ParagraphStyle("v", fontName=font, fontSize=9 * s, textColor=colors.black, leading=11 * s)
    st_hcell = ParagraphStyle("hc", fontName=font_b, fontSize=8 * s, textColor=colors.white, leading=9 * s, alignment=1)
    st_cell  = ParagraphStyle("c", fontName=font, fontSize=8.5 * s, leading=10 * s)
    st_cellr = ParagraphStyle("cr", fontName=font, fontSize=8.5 * s, leading=10 * s, alignment=2)
    st_total = ParagraphStyle("tot", fontName=font_b, fontSize=10 * s, textColor=NAVY, alignment=2)

    fs_sub = f"{7 * s:.2f}"   # iç <font size> etiketleri de ölçeklenir
    pad = 4 * s

    def esc(x):
        return escape(str(x if x is not None else ""))

    cur = view.get("currency") or "USD"
    story = []
    story.append(Paragraph("PROFORMA INVOICE / PROFORMA FATURA", st_title))
    meta = f"No: {esc(view.get('document_no'))}"
    if view.get("date"):
        meta += f" &nbsp;·&nbsp; DATE / TARİH: {esc(view.get('date'))}"
    story.append(Paragraph(meta, st_sub))

    # Müşteri bloğu — iki-dilli etiketler
    def row(en, tr, val):
        return [Paragraph(f"{en}<br/><font size={fs_sub} color='#6b7280'>{tr}</font>", st_lbl),
                Paragraph(esc(val) if (val not in (None, "")) else "—", st_val)]
    cust = [
        row("COMPANY NAME", "ŞİRKET ADI", view.get("recipient_org") or view.get("recipient_name")),
        row("CONTACT", "YETKİLİ", view.get("recipient_name")),
        row("ADDRESS", "ADRES", view.get("customer_address")),
        row("COUNTRY", "ÜLKE", view.get("customer_country")),
        row("TEL / FAX", "TEL / FAKS", view.get("recipient_phone")),
    ]
    ct = Table(cust, colWidths=[42 * mm, 128 * mm])
    ct.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(ct)
    story.append(Spacer(1, 8 * mm * s))

    # Kalem tablosu — iki-dilli başlık
    head = [Paragraph("PRODUCT NAME<br/>ÜRÜN ADI", st_hcell),
            Paragraph("WEIGHT (ml)<br/>AĞIRLIK", st_hcell),
            Paragraph("PIECES<br/>ADET", st_hcell),
            Paragraph("UNIT PRICE<br/>BİRİM FİYAT", st_hcell),
            Paragraph("TOTAL<br/>TUTAR", st_hcell)]
    data = [head]
    grand = 0.0
    for it in view.get("items", []):
        qty = float(it.get("quantity") or 0)
        up = it.get("unit_price")
        line = (float(up) * qty) if up is not None else 0.0
        grand += line
        data.append([
            Paragraph(esc(it.get("item_name")), st_cell),
            Paragraph(_num(it.get("weight_ml")), st_cellr),
            Paragraph(_num(qty), st_cellr),
            Paragraph(_money(up, cur) if up is not None else "—", st_cellr),
            Paragraph(_money(line, cur), st_cellr),
        ])
    tbl = Table(data, colWidths=[78 * mm, 24 * mm, 18 * mm, 26 * mm, 26 * mm], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.4, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8f9fb")]),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 4 * mm * s))
    story.append(Paragraph(f"GENEL TOPLAM / GRAND TOTAL: {_money(grand, cur)}", st_total))

    if view.get("note"):
        story.append(Spacer(1, 5 * mm * s))
        story.append(Paragraph(f"<font color='#6b7280'>NOTE / NOT:</font> {esc(view.get('note'))}", st_val))

    return story
