"""
Teslim belgesi PDF üretici — hediye / numune teslimatları için imzalı belge.

Alıcı ad-soyadını sistemden giren kullanıcı belgeyi basar; teslim alan kişi
yalnızca imzalar. core/qc_report.py + monthly_report._register_fonts desenini
paylaşır (Türkçe-uyumlu font).
"""
import re
from html import escape
from io import BytesIO
from pathlib import Path


TYPE_LABELS = {"hediye": "Hediye", "numune": "Numune", "diger": "Diğer", "diğer": "Diğer",
               "proforma": "Proforma Fatura"}
METHOD_LABELS = {"elden": "Elden Teslim", "kargo": "Kargo"}

# Antetli kağıt (logo + firma bilgisi).  Belgeler bunun üzerine bindirilir.
LETTERHEAD = Path(__file__).resolve().parent.parent / "static" / "letterhead.pdf"


def merge_letterhead(content: bytes) -> bytes:
    """reportlab içeriğini antetli kağıdın üzerine bindir (pypdf).  Antetli yoksa
    ya da hata olursa düz içeriği döndürür (belge yine üretilir)."""
    try:
        from pypdf import PdfReader, PdfWriter
        from reportlab.lib.pagesizes import A4
        if not LETTERHEAD.is_file():
            return content
        a4w, a4h = A4
        overlay = PdfReader(BytesIO(content))
        writer = PdfWriter()
        for pg in overlay.pages:
            base = PdfReader(str(LETTERHEAD)).pages[0]   # her sayfaya taze antetli
            base.scale_to(a4w, a4h)                      # antetliyi A4'e normalize et (Letter→A4)
            base.merge_page(pg)                          # A4 içerik antetlinin ÜSTÜNE
            writer.add_page(base)
        out = BytesIO()
        writer.write(out)
        return out.getvalue()
    except Exception:
        return content


# A4 nokta (pt) boyutu — pypdf/reportlab ile aynı.
_A4_PT = (595.2755905511812, 841.8897637795277)


def _count_pages(pdf_bytes: bytes) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(BytesIO(pdf_bytes)).pages)
    except Exception:
        return 1


def render_autofit(build_story, *, margins, doc_kwargs=None,
                   min_scale=0.85, steps=(0.96, 0.92, 0.88, 0.85)) -> bytes:
    """Tek-sayfa-öncelikli PDF üretir.

    Politika (kullanıcı kararı): içerik bir sayfaya sığıyorsa tam boyda tek sayfa.
    Taşıyorsa, **en fazla %15** küçülterek (min_scale=0.85) tek sayfaya sığdırmayı
    dener — en küçük gereken küçültmeyi seçer (100%'e en yakın).  %15 küçültme de
    yetmiyorsa gerçekten 2. sayfaya taşar (tam boyda, çok sayfa).  Böylece 2. sayfada
    tek-satır 'öksüz' içerik oluşmaz: küçük taşmalar küçültmeyle yutulur.

    build_story(scale) -> list[flowable];  margins=(left,right,top,bottom) pt.
    """
    from reportlab.platypus import SimpleDocTemplate
    l, r, t, b = margins

    def _build(scale: float) -> bytes:
        buf = BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=_A4_PT,
            leftMargin=l, rightMargin=r, topMargin=t, bottomMargin=b,
            **(doc_kwargs or {}))
        doc.build(build_story(scale))
        return buf.getvalue()

    full = _build(1.0)
    if _count_pages(full) == 1:
        return full
    # Bir sayfayı aşıyor → %15 bütçesiyle (en az küçültme) sığdırmayı dene
    for s in steps:
        if s < min_scale:
            break
        cand = _build(s)
        if _count_pages(cand) == 1:
            return cand
    # Gerçekten 2. sayfa gerekiyor → tam boyda, çok sayfalı
    return full


def _fmt(n) -> str:
    """Miktarı sade göster: 4.0 → '4', 1.5 → '1.5'."""
    try:
        f = float(n)
        return str(int(f)) if f == int(f) else f"{f:g}"
    except (TypeError, ValueError):
        return str(n)


def slug_part(s, maxlen: int = 60) -> str:
    """Dosya adına gömülecek serbest metin (alıcı adı) — yol/kontrol/tırnak temizlenir."""
    s = re.sub(r'[\\/\r\n"]+', " ", str(s or "")).strip()
    s = re.sub(r"\s+", "_", s)
    return s[:maxlen].strip("_")


def content_disposition(filename, inline: bool = False) -> str:
    """ASCII-GÜVENLİ Content-Disposition (RFC 5987).

    Türkçe adlar (Ü/ş/ğ/ı/İ) header'a doğrudan konunca latin-1 dışına çıkıp 500
    veriyordu.  Burada ASCII fallback + `filename*` (UTF-8 yüzde-kodlu) üretilir;
    modern tarayıcı tam Türkçe adı, eski tarayıcı ASCII'yi kullanır.
    """
    from urllib.parse import quote
    disp = "inline" if inline else "attachment"
    ascii_fb = (str(filename).encode("ascii", "ignore").decode().strip() or "belge")
    return "%s; filename=\"%s\"; filename*=UTF-8''%s" % (disp, ascii_fb, quote(str(filename)))


def delivery_doc_filename(document_no, recipient=None, ext: str = "pdf") -> str:
    """Teslim belgesi dosya adı — alıcıya göre: teslim_belgesi_<Alıcı>_<BelgeNo>.pdf"""
    no = (str(document_no) or "teslim").replace("/", "-").replace(" ", "_")
    who = slug_part(recipient)
    return f"teslim_belgesi_{who + '_' if who else ''}{no}.{ext}"


def render_delivery_pdf(view: dict) -> bytes:
    from reportlab.lib.units import mm
    content = render_autofit(
        lambda s: _delivery_story(view, s),
        margins=(20 * mm, 20 * mm, 48 * mm, 30 * mm),
        doc_kwargs={"title": f"Teslim Belgesi — {view.get('document_no') or ''}",
                    "author": "Minerva 108 ERP"})
    return merge_letterhead(content)   # antetli kağıt üzerine bindir (her sayfa)


def _delivery_story(view: dict, s: float = 1.0):
    """Teslim belgesi flowable listesi.  `s` = ölçek (1.0 = tam boy; <1 = küçült)."""
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle
    from core.monthly_report import _register_fonts

    font, font_b = _register_fonts()
    NAVY  = colors.HexColor("#232E6E")
    LIGHT = colors.HexColor("#f5f0e8")
    GREY  = colors.HexColor("#e5e7eb")

    st_h1    = ParagraphStyle("h1", fontName=font_b, fontSize=16 * s, textColor=NAVY,
                              spaceAfter=2 * s, leading=19 * s)
    st_meta  = ParagraphStyle("meta", fontName=font, fontSize=8 * s,
                              textColor=colors.HexColor("#6b7280"), leading=11 * s)
    st_h2    = ParagraphStyle("h2", fontName=font_b, fontSize=11 * s, textColor=NAVY,
                              spaceBefore=13 * s, spaceAfter=5 * s, leading=14 * s)
    st_cell  = ParagraphStyle("cell", fontName=font, fontSize=8.5 * s,
                              textColor=colors.HexColor("#374151"), leading=11 * s)
    st_hcell = ParagraphStyle("hcell", fontName=font_b, fontSize=8.5 * s,
                              textColor=colors.white, leading=11 * s)
    st_note  = ParagraphStyle("note", fontName=font, fontSize=9 * s,
                              textColor=colors.HexColor("#374151"), leading=13 * s)
    st_sig   = ParagraphStyle("sig", fontName=font, fontSize=9 * s,
                              textColor=colors.HexColor("#374151"), leading=14 * s)
    st_sigb  = ParagraphStyle("sigb", fontName=font_b, fontSize=9.5 * s, textColor=NAVY, leading=13 * s)

    pad = 3.5 * s
    W = 174.0  # kullanılabilir içerik genişliği (mm) — yatay sabit, yalnız dikey küçülür
    story = []

    def _kv_table(rows, widths):
        data = [[Paragraph(escape(str(k)), st_cell), Paragraph(escape(str(v)), st_cell)]
                for k, v in rows]
        t = Table(data, colWidths=widths)
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, GREY),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHT]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
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
            ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        return t

    # Başlık
    story.append(Paragraph("TESLİM BELGESİ", st_h1))
    story.append(Paragraph(
        f"Belge No: {escape(str(view.get('document_no') or '—'))}  ·  "
        f"Tarih: {escape(str(view.get('date') or '—'))}", st_meta))
    story.append(Spacer(1, 8 * s))

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
    story.append(Spacer(1, 28 * s))
    sig_left = [Paragraph("Teslim Eden", st_sigb),
                Paragraph(escape(str(view.get("dispatched_by") or "—")), st_cell),
                Spacer(1, 20 * s), Paragraph("İmza: ____________________", st_sig)]
    sig_right = [Paragraph("Teslim Alan", st_sigb),
                 Paragraph(escape(str(view.get("recipient_name") or "—")), st_cell),
                 Spacer(1, 20 * s), Paragraph("İmza: ____________________", st_sig)]
    sig = Table([[sig_left, sig_right]], colWidths=[W * 0.5 * mm, W * 0.5 * mm])
    sig.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0), ("RIGHTPADDING", (0, 0), (0, 0), 14),
        ("LEFTPADDING", (1, 0), (1, 0), 14),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(sig)

    story.append(Spacer(1, 16 * s))
    story.append(Paragraph(
        "Bu belge Minerva 108 ERP teslimat kaydından üretilmiştir. "
        "Yukarıda belirtilen ürünler eksiksiz teslim alınmıştır.", st_meta))
    return story
