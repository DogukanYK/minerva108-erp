# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""B2B sipariş proforması — İMZALI ticari sürümden (canlı veriden DEĞİL).

Görünüm core/proforma_invoice.py ile aynı aile (antetli kağıt, iki dilli
başlıklar, tek-sayfa-öncelikli autofit).  İçerik yalnız ticari bilgidir:
müşteri, kalemler, toplamlar, ödeme koşulu, seçilen banka hesabı ve yönetim
onayı (ad, tarih, imza görüntüsü, belge özeti).  Reçete, alım maliyeti ve
teknik değerlendirme BU BELGEYE GİRMEZ.
"""
import base64
from html import escape
from io import BytesIO

from core.delivery_note import merge_letterhead, render_autofit, slug_part

_TERMS_EN = {
    "prepaid": "100% payment before production.",
    "advance": "{pct:g}% advance payment before production, balance before shipment.",
    "net": "Payment as per agreed terms.",
}
_TERMS_TR = {
    "prepaid": "Üretimden önce %100 ödeme.",
    "advance": "Üretimden önce %{pct:g} avans, kalan sevkiyattan önce.",
    "net": "Ödeme, anlaşılan vadeye göre.",
}


def proforma_filename(quote_number, customer=None) -> str:
    base = (quote_number or "proforma").replace("/", "-").replace(" ", "_")
    who = slug_part(customer)
    return f"Proforma_{base}{'_' + who if who else ''}.pdf"


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


def render_order_proforma(com: dict, signature: dict) -> bytes:
    """`com` = imzalı ticari snapshot; `signature` = {signer_name, signed_at,
    document_hash, revision, png_b64}."""
    from reportlab.lib.units import mm
    content = render_autofit(
        lambda s: _story(com, signature, s),
        margins=(20 * mm, 20 * mm, 48 * mm, 40 * mm),
        doc_kwargs={"title": f"Proforma — {com.get('quote_number') or ''}", "author": "Minerva 108"})
    return merge_letterhead(content)


def _story(com, signature, s=1.0):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, Spacer, Table, TableStyle
    from core.monthly_report import _register_fonts

    font, font_b = _register_fonts()
    NAVY = colors.HexColor("#1f2d52")
    GREY = colors.HexColor("#6b7280")
    LINE = colors.HexColor("#d1d5db")
    st_title = ParagraphStyle("t", fontName=font_b, fontSize=15 * s, textColor=NAVY, leading=18 * s, spaceAfter=2 * s)
    st_sub = ParagraphStyle("s", fontName=font, fontSize=8.5 * s, textColor=GREY, spaceAfter=10 * s)
    st_lbl = ParagraphStyle("l", fontName=font_b, fontSize=8.5 * s, textColor=NAVY, leading=10 * s)
    st_val = ParagraphStyle("v", fontName=font, fontSize=9 * s, leading=11 * s)
    st_small = ParagraphStyle("sm", fontName=font, fontSize=7.5 * s, textColor=GREY, leading=9 * s)
    st_hcell = ParagraphStyle("hc", fontName=font_b, fontSize=8 * s, textColor=colors.white, leading=9 * s, alignment=1)
    st_cell = ParagraphStyle("c", fontName=font, fontSize=8.5 * s, leading=10 * s)
    st_cellr = ParagraphStyle("cr", fontName=font, fontSize=8.5 * s, leading=10 * s, alignment=2)
    st_tot = ParagraphStyle("tot", fontName=font, fontSize=9 * s, leading=11 * s, alignment=2)
    st_grand = ParagraphStyle("gr", fontName=font_b, fontSize=10.5 * s, textColor=NAVY, leading=13 * s, alignment=2)
    fs_sub = f"{7 * s:.2f}"
    pad = 4 * s

    def esc(x):
        return escape(str(x if x is not None else ""))

    cur = com.get("currency") or "USD"
    cust = com.get("customer") or {}
    story = [Paragraph("PROFORMA INVOICE / PROFORMA FATURA", st_title)]
    meta = (f"No: {esc(com.get('quote_number'))} &nbsp;·&nbsp; DATE / TARİH: {esc(signature.get('signed_at'))}"
            f" &nbsp;·&nbsp; REV: {esc(signature.get('revision'))}")
    if com.get("target_date"):
        meta += f" &nbsp;·&nbsp; TARGET / HEDEF: {esc(com['target_date'])}"
    story.append(Paragraph(meta, st_sub))

    def row(en, tr, val):
        return [Paragraph(f"{en}<br/><font size={fs_sub} color='#6b7280'>{tr}</font>", st_lbl),
                Paragraph(esc(val) if val not in (None, "") else "—", st_val)]
    ct = Table([row("COMPANY NAME", "ŞİRKET ADI", cust.get("name")),
                row("CONTACT", "YETKİLİ", cust.get("contact")),
                row("ADDRESS", "ADRES", cust.get("address")),
                row("COUNTRY", "ÜLKE", cust.get("country")),
                row("TEL / E-MAIL", "TEL / E-POSTA",
                    " · ".join(x for x in (cust.get("phone"), cust.get("email")) if x)),
                row("VAT / TAX NO", "VERGİ NO", cust.get("vat"))],
               colWidths=[42 * mm, 128 * mm])
    ct.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
                            ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
                            ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [ct, Spacer(1, 7 * mm * s)]

    data = [[Paragraph("PRODUCT NAME<br/>ÜRÜN ADI", st_hcell), Paragraph("PIECES<br/>ADET", st_hcell),
             Paragraph("UNIT PRICE<br/>BİRİM FİYAT", st_hcell), Paragraph("TOTAL<br/>TUTAR", st_hcell)]]
    for l in com.get("lines", []):
        data.append([Paragraph(esc(l.get("name")), st_cell), Paragraph(_num(l.get("quantity")), st_cellr),
                     Paragraph(_money(l.get("unit_price"), cur), st_cellr),
                     Paragraph(_money(l.get("line_total"), cur), st_cellr)])
    tbl = Table(data, colWidths=[94 * mm, 20 * mm, 28 * mm, 30 * mm], repeatRows=1)
    tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), NAVY), ("GRID", (0, 0), (-1, -1), 0.4, LINE),
                             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                             ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
                             ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                             ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8f9fb")])]))
    story += [tbl, Spacer(1, 3 * mm * s)]
    story.append(Paragraph(f"SUBTOTAL / ARA TOPLAM: {_money(com.get('subtotal'), cur)}", st_tot))
    if float(com.get("shipping") or 0):
        story.append(Paragraph(f"SHIPPING / NAKLİYE: {_money(com.get('shipping'), cur)}", st_tot))
    if float(com.get("tax_percentage") or 0):
        story.append(Paragraph(f"VAT / KDV %{_num(com.get('tax_percentage'))}: "
                               f"{_money(com.get('tax_amount'), cur)}", st_tot))
    story.append(Paragraph(f"GRAND TOTAL / GENEL TOPLAM: {_money(com.get('total'), cur)}", st_grand))
    story.append(Spacer(1, 6 * mm * s))

    terms = com.get("payment_terms") or "prepaid"
    pct = float(com.get("advance_percent") or 0)
    story.append(Paragraph(f"<b>PAYMENT TERMS / ÖDEME KOŞULU:</b> {esc(_TERMS_EN[terms].format(pct=pct))} "
                           f"<font color='#6b7280'>({esc(_TERMS_TR[terms].format(pct=pct))})</font>", st_val))
    if com.get("notes"):
        story.append(Spacer(1, 2 * mm * s))
        story.append(Paragraph(f"<font color='#6b7280'>NOTE / NOT:</font> {esc(com['notes'])}", st_val))
    story.append(Spacer(1, 5 * mm * s))

    bank = com.get("bank") or {}
    bt = Table([row("ACCOUNT OWNER", "HESAP SAHİBİ", bank.get("account_holder")),
                row("BANK / BRANCH", "BANKA / ŞUBE",
                    " — ".join(x for x in (bank.get("bank_name"), bank.get("branch")) if x)),
                row("SWIFT", "SWIFT", bank.get("swift")),
                row(f"IBAN ({esc(bank.get('currency'))})", "IBAN", bank.get("iban"))],
               colWidths=[42 * mm, 128 * mm])
    bt.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (0, 0), (-1, -1), 0.6, NAVY),
                            ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
                            ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
                            ("LEFTPADDING", (0, 0), (-1, -1), 6)]))
    story += [bt, Spacer(1, 6 * mm * s)]

    approval = [Paragraph(f"APPROVED BY / ONAYLAYAN: <b>{esc(signature.get('signer_name'))}</b>"
                          f" &nbsp;·&nbsp; {esc(signature.get('signed_at'))}", st_val)]
    png = signature.get("png_b64")
    if png:
        try:
            approval.append(Image(BytesIO(base64.b64decode(png)), width=48 * mm * s, height=18 * mm * s,
                                  kind="proportional"))
        except Exception:
            pass
    approval.append(Paragraph(f"Document hash / Belge özeti: {esc((signature.get('document_hash') or '')[:16])}…",
                              st_small))
    story += approval
    return story
