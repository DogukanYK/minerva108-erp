"""Anonymous external manufacturing PDFs from the frozen, allowlisted packet DTO.

The renderer deliberately knows no database models. Callers must obtain the DTO
from ``outsourcing.external_packet``; unknown keys (including private snapshots)
are never rendered. Labels are one 100 x 70 mm page per physical container.
"""
from decimal import Decimal, InvalidOperation
from html import escape
from io import BytesIO

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

from core.monthly_report import _register_fonts


_TITLES = {
    "sheet": "Fason üretim föyü",
    "labels": "Fason kap etiketleri",
    "manifest": "Fason sevk listesi",
    "report": "Fason tüketim ve bakiye raporu",
}
_KINDS = {"raw": "Hammadde", "packaging": "Ambalaj", "label": "Etiket"}
_NAVY = colors.HexColor("#243653")
_GREY = colors.HexColor("#64748b")
_LABEL_SIZE = (100 * mm, 70 * mm)


def _number(value) -> Decimal:
    try:
        number = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError):
        raise ValueError("Geçersiz belge miktarı") from None
    if not number.is_finite():
        raise ValueError("Geçersiz belge miktarı")
    return number


def _quantity(value) -> str:
    return format(_number(value).quantize(Decimal("0.000001")), "f").rstrip("0").rstrip(".") or "0"


def _text(value) -> str:
    return str(value if value is not None else "")


def _validate(packet: dict, document: str):
    if document not in _TITLES:
        raise ValueError("Bilinmeyen fason belge türü")
    if packet.get("approved") is not True:
        raise ValueError("Fason belge için tüm onaylar gerekli")


def outsourcing_filename(packet: dict, document: str) -> str:
    """Fixed download name; never includes product, partner, material or lot names."""
    if document not in _TITLES:
        raise ValueError("Bilinmeyen fason belge türü")
    revision = int(packet.get("revision", 0))
    if revision < 0:
        raise ValueError("Geçersiz belge revizyonu")
    return f"fason_{document}_r{revision}.pdf"


def _styles():
    font, bold = _register_fonts()
    return {
        "title": ParagraphStyle("out-title", fontName=bold, fontSize=17, leading=21, textColor=_NAVY, spaceAfter=10),
        "heading": ParagraphStyle("out-heading", fontName=bold, fontSize=11, leading=14, textColor=_NAVY, spaceBefore=12, spaceAfter=6),
        "text": ParagraphStyle("out-text", fontName=font, fontSize=9, leading=12, spaceAfter=5, wordWrap="CJK"),
        "cell": ParagraphStyle("out-cell", fontName=font, fontSize=8, leading=11, wordWrap="CJK"),
        "header": ParagraphStyle("out-header", fontName=bold, fontSize=8, leading=10, textColor=colors.white, wordWrap="CJK"),
        "font": font,
        "bold": bold,
    }


def _paragraph(value, style):
    return Paragraph(escape(_text(value)).replace("\n", "<br/>"), style)


def _table(headers, rows, widths, styles):
    data = [[_paragraph(value, styles["header"]) for value in headers]]
    data.extend([[_paragraph(value, styles["cell"]) for value in row] for row in rows])
    table = LongTable(data, colWidths=[width * mm for width in widths], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, _NAVY),
    ]))
    return table


def _header(packet, document, styles):
    story = [_paragraph(_TITLES[document], styles["title"])]
    for label, key in [("İş", "job_reference"), ("Dış üretici", "partner_reference"),
                       ("Revizyon", "revision"), ("Ürün kodu / dış adı", "product_name")]:
        story.append(_paragraph(f"{label}: {_text(packet.get(key))}", styles["text"]))
    story.append(_paragraph(
        f"Üretim hedefi: {_quantity(packet.get('quantity'))} {_text(packet.get('unit'))}"
        f"  |  Etiket dili: {_text(packet.get('label_language'))}", styles["text"]))
    return story


def _container_rows(containers):
    return [[row.get("code"), row.get("container_uid"), row.get("external_lot"),
             _quantity(row.get("quantity")), row.get("unit"), row.get("expiry_date")]
            for row in containers]


def _sheet(packet, styles):
    story = [_paragraph("Onaylı reçete hedefleri", styles["heading"])]
    rows = []
    for material in packet.get("materials", []):
        parts = material.get("parts") or [{"phase": material.get("phase"), "quantity": material.get("quantity")}]
        for part in parts:
            rows.append([material.get("code"), _KINDS.get(material.get("kind"), "Malzeme"),
                         part.get("phase") or "—", _quantity(part.get("quantity")), material.get("unit")])
    story.append(_table(["Kod", "Tür", "Faz", "Hedef miktar", "Birim"], rows, [39, 35, 45, 36, 25], styles))
    story.append(_paragraph("Hedefler sevk fazlasıyla artırılmaz. Kap miktarları sevk listesinde ayrıca gösterilir.", styles["text"]))
    actual = {}
    for container in packet.get("containers", []):
        code = container.get("code")
        actual[code] = actual.get(code, Decimal(0)) + _number(container.get("quantity"))
    extras = []
    for material in packet.get("materials", []):
        dispatch_quantity = _number(material.get("dispatch_quantity")) if material.get("dispatch_quantity") is not None else actual.get(material.get("code"), Decimal(0))
        extra = dispatch_quantity - _number(material.get("quantity"))
        if extra > 0:
            extras.append([material.get("code"), _quantity(extra), material.get("unit")])
    if extras:
        story.append(_paragraph("Hedef üstü sevk miktarları", styles["heading"]))
        story.append(_table(["Kod", "Sevk fazlası", "Birim"], extras, [85, 65, 30], styles))
    safety = [[material.get("code"), material["safety_instructions"]]
              for material in packet.get("materials", []) if material.get("safety_instructions")]
    if safety:
        story.append(_paragraph("Kodlanmış güvenlik talimatları", styles["heading"]))
        story.append(_table(["Kod", "Doğrulanmış güvenlik talimatı"], safety, [39, 141], styles))
    if packet.get("instructions"):
        story.append(_paragraph("Kodlanmış üretim talimatı", styles["heading"]))
        story.append(_paragraph(packet["instructions"], styles["text"]))
    return story


def _manifest(packet, styles):
    story = [_paragraph("Fiili kap miktarları", styles["heading"])]
    shipments = packet.get("shipments", [])
    if shipments:
        for shipment in shipments:
            story.append(_paragraph(
                f"Sevk: {_text(shipment.get('reference'))}  |  Tarih: {_text(shipment.get('dispatched_at'))}",
                styles["text"]))
            story.append(_table(["Kod", "Kap kimliği", "Dış lot", "Miktar", "Birim", "SKT"],
                                _container_rows(shipment.get("containers", [])), [27, 49, 38, 23, 16, 27], styles))
    else:
        story.append(_table(["Kod", "Kap kimliği", "Dış lot", "Miktar", "Birim", "SKT"],
                            _container_rows(packet.get("containers", [])), [27, 49, 38, 23, 16, 27], styles))
    return story


def _report(packet, styles):
    targets = {row.get("code"): _number(row.get("quantity")) for row in packet.get("materials", [])}
    rows = []
    for row in packet.get("balance", []):
        target = targets.get(row.get("code"), Decimal(0))
        used = _number(row.get("consumed"))
        waste = _number(row.get("waste"))
        rows.append([row.get("code"), row.get("unit"), _quantity(target), _quantity(row.get("dispatched")),
                     _quantity(used), _quantity(waste), _quantity(row.get("returned")),
                     _quantity(row.get("outstanding")), _quantity(used + waste - target)])
    return [
        _paragraph("Reçete karşılaştırması ve fiziksel bakiye", styles["heading"]),
        _table(["Kod", "Birim", "Hedef", "Sevk", "Kullanım", "Fire", "İade", "Dışarıda", "Hedef farkı"],
               rows, [30, 15, 19, 19, 21, 18, 18, 20, 20], styles),
        _paragraph("Hedef farkı = kullanım + fire − onaylı reçete hedefi. İade, teslim alınan fiziksel miktardır; kalite onayı kullanılabilir stoktan ayrı izlenir.", styles["text"]),
    ]


def _label_text(pdf, text, x, top, width, height, font, size=9, bold=False):
    """Fit complete trace identifiers; refuse illegible/overflowing labels."""
    for font_size in [size, size - 1, size - 2, 6]:
        style = ParagraphStyle("out-label", fontName=font, fontSize=max(font_size, 6),
                               leading=max(font_size, 6) * 1.2, wordWrap="CJK",
                               textColor=_NAVY if bold else colors.black)
        paragraph = _paragraph(text, style)
        _, needed = paragraph.wrap(width, height)
        if needed <= height:
            paragraph.drawOn(pdf, x, top - needed)
            return
    raise ValueError("Kap etiketi metni fiziksel alana sığmıyor")


def _labels(packet, styles):
    containers = packet.get("containers", [])
    if not containers:
        raise ValueError("Kap etiketi için en az bir kap gerekli")
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=_LABEL_SIZE)
    _metadata(pdf, "labels")
    for row in containers:
        uid = _text(row.get("container_uid"))
        if not uid:
            raise ValueError("Kap kimliği gerekli")
        _label_text(pdf, "FASON KAP ETİKETİ", 5 * mm, 65 * mm, 90 * mm, 7 * mm, styles["bold"], 10, True)
        _label_text(pdf, row.get("code"), 5 * mm, 56 * mm, 90 * mm, 10 * mm, styles["bold"], 16, True)
        details = [
            f"Miktar: {_quantity(row.get('quantity'))} {_text(row.get('unit'))}",
            f"Dış lot: {_text(row.get('external_lot'))}",
            f"SKT: {_text(row.get('expiry_date')) or '—'}",
            f"İş: {_text(packet.get('job_reference'))} · R{_text(packet.get('revision'))}",
        ]
        _label_text(pdf, "\n".join(details), 5 * mm, 44 * mm, 57 * mm, 27 * mm, styles["font"], 9)
        qr = QrCodeWidget(uid, barBorder=4)
        x0, y0, x1, y1 = qr.getBounds()
        size = 29 * mm
        drawing = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), -x0 * size / (x1 - x0), -y0 * size / (y1 - y0)])
        drawing.add(qr)
        renderPDF.draw(drawing, pdf, 66 * mm, 18 * mm)
        _label_text(pdf, uid, 5 * mm, 12 * mm, 90 * mm, 9 * mm, styles["font"], 8)
        pdf.showPage()
    pdf.save()
    return output.getvalue()


def _metadata(pdf, document):
    pdf.setTitle(_TITLES[document])
    pdf.setAuthor("Minerva108")
    pdf.setSubject("Anonim fason operasyon belgesi")
    pdf.setCreator("Minerva108 IMS")


def render_packet_pdf(packet: dict, document: str) -> bytes:
    """Render sheet/labels/manifest/report without reading private packet fields."""
    _validate(packet, document)
    styles = _styles()
    if document == "labels":
        return _labels(packet, styles)
    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=16 * mm, bottomMargin=17 * mm)
    story = _header(packet, document, styles)
    story.append(Spacer(1, 3 * mm))
    story.extend({"sheet": _sheet, "manifest": _manifest, "report": _report}[document](packet, styles))

    def page_footer(pdf, _doc):
        _metadata(pdf, document)
        pdf.saveState()
        pdf.setFont(styles["font"], 7)
        pdf.setFillColor(_GREY)
        pdf.drawString(15 * mm, 9 * mm, _TITLES[document])
        pdf.drawRightString(A4[0] - 15 * mm, 9 * mm, f"{_doc.page}")
        pdf.restoreState()

    doc.build(story, onFirstPage=page_footer, onLaterPages=page_footer)
    return output.getvalue()
