# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""Proforma fatura — TEK ÇİZİCİ (beğenilen boş şablon `Proforma_Sablon_Bos`, 09.10.2026).

Üç belge bunu kullanır: B2B sipariş proforması (imzalı ticari sürümden),
Teslimat proforması (PRF-) ve kayıtlı teklifin PDF'i.  Teklifler sayfasının
canlı HTML önizlemesi aynı düzeni CSS ile çizer; firma bilgisi oraya da
`COMPANY`'den gider (iki kopya tutulmaz).

Düzen şablondan ölçüldü (mm, sayfa sol-üstünden): logo 46×21.8 sol üstte, firma
bloğu x=58'den; kutulu başlık; müşteri bloğu (değerler alt çizgili); NO · ÜRÜN ·
AĞIRLIK (ml) · ADET · BİRİM FİYAT · TOPLAM tablosu (#D9D9D9 başlık, 24 numaralı
satır); TOTAL satırı; kalın şartlar (TIME OF DELIVERY #9A3412); "BANK ACCOUNT
INFO" tablosu (1–3 banka sütunu); "Best Regards.".  Antetli kâğıt KULLANILMAZ —
başlık şablondaki gibi belgenin kendisinde.

`doc` sözleşmesi:
    number, date, currency, notes
    customer {name, address, country, phone}
    lines [{name, weight_ml, quantity, unit_price, line_total}]
    totals {subtotal, shipping, tax_percentage, tax_amount, total}
    terms {transportation, payment, shipment, delivery_type, loading_days}
    banks [core.bank_accounts.bank_snapshot(...), …]   (1–3)
    signature {name, date, png_b64, document_hash, revision} | None
"""
import base64
import math
import os
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Optional, Union

from pydantic import BaseModel, Field

LOGO = Path(__file__).resolve().parent.parent / "static" / "images" / "proforma_logo.png"

COMPANY = {
    "name": "MİNERVA 108 YÖNETİM & DANIŞMANLIK ANONİM ŞİRKETİ",
    "account_holder": "MİNERVA 108 YÖNETİM & DANIŞMANLIK A.Ş.",
    "address": "Kireçburnu Mah. Raif Bey Sk. No:8/A Sarıyer İstanbul Türkiye",
    "phones": ("Tel: +90 212 299 20 70 - 71", "Tel: +90 542 471 74 19",
               "Tel: +90 555 759 19 14", "Tel: +90 544 510 45 49"),
    "web": ("www.minerva108cosmetics.com / www.serenidacosmetics.com /",
            "www.evaniracosmetics.com · info@minerva108cosmetics.com"),
}

DEFAULT_PAYMENT = "% 100 IN ADVANCE"
DEFAULT_TERMS = {
    "transportation": "EXCLUDING",
    "shipment": "- BY SEA - BY AIR",
    "delivery_type": "EX-FACTORY",
    "loading_days": None,
}
STANDARD_NOTES = (
    "These prices are calculated for partial loading.",
    "If you need any further details or informations, do not hesitate to contact us.",
    "* Above prices are valid for only this offer, not valid for our general price list.",
)
TEMPLATE_ROWS = 24
_TERM_TEXT_MAX = 120
#: Teklifler formunun 09.10.2026'ya kadarki varsayılan "Notlar / Şartlar"
#: metni.  Şartlar artık yapılandırılmış basıldığı için bu metin aynen
#: duruyorsa NOTE satırı olarak TEKRAR basılmaz (ödeme koşulu farklıysa
#: belge kendisiyle çelişirdi).
LEGACY_DEFAULT_NOTES = ("Payment Terms: 100% IN ADVANCE · Type of Delivery: EX-FACTORY · Time of Delivery: "
                        "2 months after receiving payment · Shipment: BY SEA / BY AIR · Transportation: "
                        "EXCLUDING. These prices are calculated for partial loading. Above prices are valid "
                        "for only this offer.")


def printable_notes(notes) -> str:
    text = " ".join(str(notes or "").split())
    return "" if text == LEGACY_DEFAULT_NOTES else text


class ProformaTermsIn(BaseModel):
    """API gövdesi — boş alan şablon varsayılanına düşer, gün 0–365
    (`clean_terms` denetler → 400, Pydantic 422 değil).  `payment` yalnız teklif
    ve teslimat proformasında serbest metindir; B2B siparişinde ödeme koşulundan
    türetilir ve bu alan yok sayılır."""
    transportation: Optional[str] = Field(None, max_length=_TERM_TEXT_MAX)
    payment: Optional[str] = Field(None, max_length=_TERM_TEXT_MAX)
    shipment: Optional[str] = Field(None, max_length=_TERM_TEXT_MAX)
    delivery_type: Optional[str] = Field(None, max_length=_TERM_TEXT_MAX)
    loading_days: Union[int, float, str, None] = None


def payment_terms_text(terms, advance_percent=None):
    """Sipariş ödeme koşulu → proforma satırı (şablon dili İngilizce)."""
    if terms == "advance":
        try:
            pct = f"{float(advance_percent):g}"
        except (TypeError, ValueError):
            pct = "—"
        return f"% {pct} IN ADVANCE, BALANCE BEFORE SHIPMENT"
    if terms == "net":
        return "AS AGREED"
    return DEFAULT_PAYMENT


def clean_terms(data, *, with_payment=False):
    """Kullanıcı girdisi → saklanacak şartlar.  Boş alan şablon varsayılanına düşer;
    yükleme günü 0–365 tam sayı ya da boş."""
    data = data if isinstance(data, dict) else {}
    out = {}
    for key in ("transportation", "shipment", "delivery_type") + (("payment",) if with_payment else ()):
        val = str(data.get(key) or "").strip()[:_TERM_TEXT_MAX]
        out[key] = val or DEFAULT_TERMS.get(key) or ""
    days = data.get("loading_days")
    if days in (None, ""):
        out["loading_days"] = None
    else:
        try:
            d = int(float(days))
        except (TypeError, ValueError):
            raise ValueError("Yükleme süresi gün sayısı olmalıdır.")
        if not 0 <= d <= 365:
            raise ValueError("Yükleme süresi 0–365 gün olmalıdır.")
        out["loading_days"] = d
    return out


def default_terms():
    """Teklif / teslimat formunun boş hâli (ödeme satırı serbest metin)."""
    return {**DEFAULT_TERMS, "payment": DEFAULT_PAYMENT}


def merged_terms(stored, payment_text):
    """Saklı şartlar + ödeme satırı → çiziciye giden şartlar."""
    terms = {**DEFAULT_TERMS, **(stored or {})}
    terms["payment"] = (stored or {}).get("payment") or payment_text or DEFAULT_PAYMENT
    return terms


def proforma_filename(number, customer=None) -> str:
    from core.delivery_note import slug_part
    base = (number or "proforma").replace("/", "-").replace(" ", "_")
    who = slug_part(customer)
    return f"Proforma_{base}{'_' + who if who else ''}.pdf"


# ─── Biçim yardımcıları ──────────────────────────────────────────────────────

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "" if v in (None, "") else str(v)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _amount(v):
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return ""


_FONT_DIR = Path(__file__).resolve().parent / "fonts"
_FONT_CANDIDATES = (
    # Depoda: Liberation Sans 2.1 (Arial ile birebir ölçülü; SIL OFL 1.1 —
    # core/fonts/LiberationSans-LICENSE.txt).  Mac, test ve sunucu AYNI fontla
    # basar; sistem fontları yalnız dosya eksikse yedek.
    (str(_FONT_DIR / "LiberationSans-Regular.ttf"), str(_FONT_DIR / "LiberationSans-Bold.ttf")),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
)


def _fonts():
    """Şablon Arial'dir — depodaki Liberation Sans (Arial ölçülü), yoksa sistemdeki
    Arial / Liberation, o da yoksa raporların Türkçe uyumlu font çifti."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for reg, bold in _FONT_CANDIDATES:
        if os.path.isfile(reg) and os.path.isfile(bold):
            try:
                pdfmetrics.registerFont(TTFont("PFA", reg))
                pdfmetrics.registerFont(TTFont("PFA-B", bold))
                return "PFA", "PFA-B"
            except Exception:
                continue
    from core.monthly_report import _register_fonts
    return _register_fonts()


# ─── Çizim ───────────────────────────────────────────────────────────────────

_MARGINS_MM = (6, 5.6, 6, 6)          # sol, sağ, üst, alt — şablonun 0.3" kenarı


def render_proforma(doc: dict) -> bytes:
    """≤24 kalem: şablondaki gibi numaralı boş satırlarla tek sayfa, TAM BOYDA —
    ek bloklar (navlun/KDV, RUB, not, imza, 3. banka) sığmazsa önce boş satır
    eksilir; kalemler bile sığmıyorsa render_autofit küçültür ya da sayfalar."""
    from reportlab.lib.units import mm
    from core.delivery_note import render_autofit
    rows = _rows_that_fit(doc)
    return render_autofit(
        lambda s: _story(doc, s, rows=rows),
        margins=tuple(m * mm for m in _MARGINS_MM),
        doc_kwargs={"title": f"Proforma — {doc.get('number') or ''}", "author": "Minerva 108"})


def _rows_that_fit(doc) -> int:
    """Tek sayfaya tam boyda sığan en fazla `TEMPLATE_ROWS` satır (yükseklik ölçülür)."""
    from reportlab.lib.units import mm
    n = len(doc.get("lines") or [])
    if n >= TEMPLATE_ROWS:
        return n
    pad = 12                                      # SimpleDocTemplate çerçeve iç boşluğu (2 × 6 pt)
    avail_w = (210 - _MARGINS_MM[0] - _MARGINS_MM[1]) * mm - pad
    avail_h = (297 - _MARGINS_MM[2] - _MARGINS_MM[3]) * mm - pad

    def height(rows):
        return sum(f.wrap(avail_w, avail_h)[1] for f in _story(doc, 1.0, rows=rows))
    full = height(TEMPLATE_ROWS)
    if full <= avail_h:
        return TEMPLATE_ROWS
    per_row = (full - height(n)) / (TEMPLATE_ROWS - n) if TEMPLATE_ROWS > n else 0
    if per_row <= 0:
        return n
    return max(n, TEMPLATE_ROWS - math.ceil((full - avail_h) / per_row))


def _story(doc, s=1.0, rows=None):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Image, Paragraph, Spacer, Table, TableStyle

    font, bold = _fonts()
    BLACK = colors.black
    GRAY = colors.HexColor("#D9D9D9")
    RUST = colors.HexColor("#9A3412")
    CW = 198.4 * mm

    def st(name, size, *, b=False, align=0, color=BLACK, lead=None):
        return ParagraphStyle(name, fontName=bold if b else font, fontSize=size * s,
                              leading=(lead or size * 1.18) * s, alignment=align, textColor=color)

    def esc(x):
        return escape(str(x if x is not None else ""))

    p_co_b, p_co = st("cob", 11, b=True), st("co", 9.5)
    p_title = st("ti", 15, b=True, align=1)
    p_lbl, p_val = st("lb", 10, b=True), st("va", 10)
    p_th = st("th", 9.5, b=True, align=1, lead=10.6)
    p_td, p_tdc, p_tdr = st("td", 10), st("tdc", 10, align=1), st("tdr", 10, align=2)
    p_tdrb = st("tdrb", 10, b=True, align=2)
    p_tot, p_totc, p_totr = st("tt", 10.5, b=True, align=2), st("ttc", 10.5, b=True, align=1), \
        st("ttr", 10.5, b=True, align=2)
    p_term, p_time, p_note = st("te", 9.5, b=True), st("tm", 10, b=True, color=RUST), st("no", 9.5)
    p_bh, p_bl, p_bv, p_bn = st("bh", 11, b=True, align=1), st("bl", 9, b=True), st("bv", 9.5), \
        st("bn", 11, b=True, align=1)
    p_small = st("sm", 7, color=colors.HexColor("#6b7280"))
    pad = 1.6 * s

    banks = [b for b in (doc.get("banks") or []) if b][:3]
    lines = doc.get("lines") or []
    totals = doc.get("totals") or {}
    terms = doc.get("terms") or {}
    signature = doc.get("signature") or None
    cur = (doc.get("currency") or "").upper()
    shipping = float(totals.get("shipping") or 0)
    tax_pct = float(totals.get("tax_percentage") or 0)
    has_rub = any((b.get("ibans") or {}).get("RUB") for b in banks)
    notes = printable_notes(doc.get("notes"))

    # Şablon 24 numaralı satırla tek sayfa; `rows` render_proforma'da ölçülür (ek
    # bloklar sığmazsa boş satır eksilir).  Ölçümsüz çağrıda kaba tahmin.
    if rows is None:
        extra = (1 if shipping else 0) + (1 if tax_pct else 0) + (1 if (shipping or tax_pct) else 0)
        extra += (1 if has_rub else 0) + (5 if signature else 0) + (2 if notes else 0)
        rows = max(len(lines), TEMPLATE_ROWS - extra) if len(lines) <= TEMPLATE_ROWS else len(lines)
    rows_total = max(rows, len(lines))

    story = []

    # ── Başlık: logo + firma bloğu ──
    logo = ""
    if LOGO.is_file():
        logo = Image(str(LOGO), width=46 * mm * s, height=21.8 * mm * s)
    phones = Table([[Paragraph(esc(COMPANY["phones"][0]), p_co), Paragraph(esc(COMPANY["phones"][1]), p_co)],
                    [Paragraph(esc(COMPANY["phones"][2]), p_co), Paragraph(esc(COMPANY["phones"][3]), p_co)]],
                   colWidths=[62 * mm * s, 60 * mm * s])
    phones.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0.4 * s)]))
    block = [Paragraph(esc(COMPANY["name"]), p_co_b), Paragraph(esc(COMPANY["address"]), p_co), phones] + \
        [Paragraph(esc(w), p_co) for w in COMPANY["web"]]
    head = Table([[logo, block]], colWidths=[52 * mm, CW - 52 * mm])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 2 * mm * s)]

    # ── Kutulu başlık ──
    title = Table([[Paragraph("PROFORMA INVOICE / PROFORMA FATURA", p_title)]], colWidths=[CW])
    title.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 1, BLACK), ("TOPPADDING", (0, 0), (-1, -1), 1.4 * s),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2 * s)]))
    story += [title, Spacer(1, 2.2 * mm * s)]

    # ── Müşteri bloğu (değer hücreleri alt çizgili) ──
    cust = doc.get("customer") or {}

    def v(x):
        return Paragraph(esc(x) if x not in (None, "") else "", p_val)
    crow = [[Paragraph("COMPANY NAME / ŞİRKET ADI:", p_lbl), v(cust.get("name")), "",
             Paragraph("DATE:", p_lbl), v(doc.get("date"))],
            [Paragraph("ADDRESS / ADRES:", p_lbl), v(cust.get("address")), "",
             Paragraph("TEL/FAX:", p_lbl), v(cust.get("phone"))],
            [Paragraph("COUNTRY / ÜLKE:", p_lbl), v(cust.get("country")), "",
             Paragraph("NO:", p_lbl), v(doc.get("number"))]]
    ctab = Table(crow, colWidths=[58 * mm, 70 * mm, 4 * mm, 22 * mm, CW - 154 * mm])
    ctab.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LINEBELOW", (1, 0), (1, -1), 0.6, BLACK), ("LINEBELOW", (4, 0), (4, -1), 0.6, BLACK),
        ("LEFTPADDING", (0, 0), (-1, -1), 0.6 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 0.6 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 0.8 * s), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.0 * s)]))
    story += [ctab, Spacer(1, 2.4 * mm * s)]

    # ── Kalem tablosu ──
    cur_line = f" ({esc(cur)})" if cur else ""          # son satıra eklenir: başlık 3 satır kalsın
    head_row = [Paragraph("NO", p_th), Paragraph("PRODUCT NAME<br/>ÜRÜN ADI", p_th),
                Paragraph("WEIGHT / AĞIRLIK<br/>(ml)", p_th), Paragraph("PIECES<br/>ADET", p_th),
                Paragraph(f"UNIT PRICE<br/>BİRİM FİYATI{cur_line}", p_th),
                Paragraph(f"TOTAL PRICE<br/>TOPLAM TUTAR{cur_line}", p_th)]
    widths = [7.8 * mm, 103.5 * mm, 17.1 * mm, 18.0 * mm, 25.1 * mm, 26.9 * mm]
    data = [head_row]
    pieces = 0.0
    for i in range(rows_total):
        if i < len(lines):
            ln = lines[i]
            try:
                pieces += float(ln.get("quantity") or 0)
            except (TypeError, ValueError):
                pass
            data.append([Paragraph(str(i + 1), p_tdc), Paragraph(esc(ln.get("name")), p_td),
                         Paragraph(_num(ln.get("weight_ml")), p_tdc), Paragraph(_num(ln.get("quantity")), p_tdc),
                         Paragraph(_amount(ln.get("unit_price")), p_tdr),
                         Paragraph(_amount(ln.get("line_total")), p_tdrb)])
        else:
            data.append([Paragraph(str(i + 1), p_tdc), "", "", "", "", ""])
    items = Table(data, colWidths=widths, repeatRows=1)
    items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), GRAY), ("GRID", (0, 0), (-1, -1), 0.5, BLACK),
        ("BOX", (0, 0), (-1, -1), 1, BLACK), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), pad * 0.4), ("BOTTOMPADDING", (0, 0), (-1, -1), pad * 0.75),
        ("LEFTPADDING", (0, 0), (-1, -1), 1.2 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 1.2 * mm),
        ("LEFTPADDING", (0, 0), (0, -1), 0.4 * mm), ("RIGHTPADDING", (0, 0), (0, -1), 0.4 * mm),
        ("LEFTPADDING", (2, 0), (2, -1), 0.4 * mm), ("RIGHTPADDING", (2, 0), (2, -1), 0.4 * mm)]))
    story.append(items)

    # ── Toplamlar: sağdaki üç sütunun altında ──
    tot_rows = [["", "", Paragraph(_num(pieces) if pieces else "", p_totc), Paragraph("TOTAL", p_tot),
                 Paragraph(_amount(totals.get("subtotal")), p_totr)]]
    if shipping or tax_pct:
        p_totl = st("ttl", 9.5, b=True, align=2)         # tek satırda kalsın
        if shipping:
            tot_rows.append(["", Paragraph("FREIGHT / NAVLUN", p_totl), "", "", Paragraph(_amount(shipping), p_totr)])
        if tax_pct:
            tot_rows.append(["", Paragraph(f"VAT / KDV %{_num(tax_pct)}", p_totl), "", "",
                             Paragraph(_amount(totals.get("tax_amount")), p_totr)])
        tot_rows.append(["", Paragraph("GRAND TOTAL / GENEL TOPLAM", p_totl), "", "",
                         Paragraph(_amount(totals.get("total")), p_totr)])
    tw = [CW - 87.1 * mm, 17.1 * mm, 18.0 * mm, 25.1 * mm, 26.9 * mm]
    tstyle = [("BOX", (2, 0), (-1, 0), 1, BLACK), ("INNERGRID", (2, 0), (-1, 0), 0.5, BLACK),
              ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad * 1.2),
              ("LEFTPADDING", (0, 0), (-1, -1), 1.2 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 1.2 * mm)]
    if len(tot_rows) > 1:
        tstyle += [("BOX", (1, 1), (-1, -1), 1, BLACK), ("INNERGRID", (1, 1), (-1, -1), 0.5, BLACK)]
    for r in range(1, len(tot_rows)):
        tstyle.append(("SPAN", (1, r), (3, r)))
    ttab = Table(tot_rows, colWidths=tw)
    ttab.setStyle(TableStyle(tstyle))
    story += [ttab, Spacer(1, 1.6 * mm * s)]

    # ── Şartlar ──
    days = terms.get("loading_days")
    days_txt = str(days) if days not in (None, "") else "____"
    term_lines = [Paragraph(f"TRANSPORTATION: {esc(terms.get('transportation') or DEFAULT_TERMS['transportation'])}", p_term),
                  Paragraph(f"PAYMENT TERMS: {esc(terms.get('payment') or DEFAULT_PAYMENT)}", p_term),
                  Paragraph(f"SHIPMENT: {esc(terms.get('shipment') or DEFAULT_TERMS['shipment'])}", p_term),
                  Paragraph(f"TYPE OF DELIVERY: {esc(terms.get('delivery_type') or DEFAULT_TERMS['delivery_type'])}",
                            p_term),
                  Paragraph(f"TIME OF DELIVERY: LOADING WITHIN {esc(days_txt)} DAYS AFTER THE PAYMENT IS "
                            "RECEIVED IN OUR ACCOUNT", p_time)]
    term_lines += [Paragraph(esc(t), p_note) for t in STANDARD_NOTES]
    if notes:
        term_lines.append(Paragraph(f"NOTE: {esc(notes)}", p_note))
    tblock = Table([[t] for t in term_lines], colWidths=[CW])
    tblock.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 2.1 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                                ("TOPPADDING", (0, 0), (-1, -1), 0.25 * s),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 0.25 * s)]))
    story += [tblock, Spacer(1, 1.4 * mm * s)]

    # ── Banka tablosu ──
    if banks:
        n = len(banks)
        if n >= 3:
            p_bv = st("bv3", 8.0)
        lw = 35 * mm
        bw = (CW - lw) / n
        holders = {b.get("account_holder") or COMPANY["account_holder"] for b in banks}
        brows = [[Paragraph("BANK ACCOUNT INFO", p_bh)] + [""] * n]
        style = [("SPAN", (0, 0), (-1, 0)), ("BACKGROUND", (0, 0), (-1, 0), GRAY)]
        if len(holders) == 1:
            brows.append([Paragraph("ACCOUNT OWNER:", p_bl), Paragraph(esc(holders.pop()), p_bl)] + [""] * (n - 1))
            style.append(("SPAN", (1, 1), (-1, 1)))
        else:
            brows.append([Paragraph("ACCOUNT OWNER:", p_bl)] +
                         [Paragraph(esc(b.get("account_holder") or ""), p_bl) for b in banks])
        brows.append([""] + [Paragraph(str(i + 1), p_bn) for i in range(n)])
        fields = [("BANK:", lambda b: b.get("bank_name")), ("BRANCH:", lambda b: b.get("branch")),
                  ("SWIFT CODE:", lambda b: b.get("swift")),
                  ("USD IBAN NO:", lambda b: (b.get("ibans") or {}).get("USD")),
                  ("EURO IBAN NO:", lambda b: (b.get("ibans") or {}).get("EUR")),
                  ("TRY IBAN NO:", lambda b: (b.get("ibans") or {}).get("TRY"))]
        if has_rub:
            fields.append(("RUB IBAN NO:", lambda b: (b.get("ibans") or {}).get("RUB")))
        for label, get in fields:
            brows.append([Paragraph(label, p_bl)] + [Paragraph(esc(get(b) or ""), p_bv) for b in banks])
        btab = Table(brows, colWidths=[lw] + [bw] * n)
        btab.setStyle(TableStyle(style + [
            ("BOX", (0, 0), (-1, -1), 1, BLACK), ("INNERGRID", (0, 0), (-1, -1), 0.5, BLACK),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), pad * 0.3), ("BOTTOMPADDING", (0, 0), (-1, -1), pad * 0.65),
            ("LEFTPADDING", (0, 0), (-1, -1), 1.2 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 1.2 * mm)]))
        story += [btab, Spacer(1, 1.2 * mm * s)]

    # ── Kapanış + (B2B) yönetim imzası ──
    closing = [Paragraph("Best Regards.", p_note)]
    if signature:
        png = signature.get("png_b64")
        if png:
            try:
                closing.append(Image(BytesIO(base64.b64decode(png)), width=42 * mm * s, height=14 * mm * s,
                                     kind="proportional", hAlign="LEFT"))
            except Exception:
                pass
        closing.append(Paragraph(f"<b>{esc(signature.get('name'))}</b> &nbsp;·&nbsp; {esc(signature.get('date'))}",
                                 p_note))
        ref = (signature.get("document_hash") or "")[:16]
        if ref:
            closing.append(Paragraph(f"Ref: {esc(ref)}… · Rev {esc(signature.get('revision'))}", p_small))
    ctab2 = Table([[c] for c in closing], colWidths=[CW])
    ctab2.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 2.1 * mm), ("TOPPADDING", (0, 0), (-1, -1), 0.3 * s),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 0.3 * s)]))
    story.append(ctab2)
    return story
