# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Numune Analiz Formu PDF üretici — FR.KK.01 kağıt formunun antetli A4 replikası.

  render_sample_pdf(view) -> bytes   künye + özellik tablosu + sonuç rozeti +
                                     SONUÇ/açıklama + iki imza bloğu + form kodu

Ortak A4/antetli/auto-fit altyapısı core.delivery_note'tan (render_autofit +
merge_letterhead + slug_part); Türkçe fontlar core.monthly_report._register_fonts.
Alt marj 50mm — antetli kağıdın 4 satırlık footer bloğu ~48mm'ye çıkıyor
(raster ölçümlü; return_note'un 40mm'sini KOPYALAMA).
"""
from html import escape

from core.delivery_note import merge_letterhead, render_autofit, slug_part


def sample_doc_filename(document_no, lot_or_bulk=None, ext: str = "pdf") -> str:
    no = (str(document_no) or "numune").replace("/", "-").replace(" ", "_")
    w = slug_part(lot_or_bulk, 40)
    return f"numune_analiz_{w + '_' if w else ''}{no}.{ext}"


def _sample_story(view: dict, s: float = 1.0):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

    from core.monthly_report import _register_fonts

    font, font_b = _register_fonts()
    NAVY = colors.HexColor("#232E6E")
    LIGHT = colors.HexColor("#f5f0e8")
    GREY = colors.HexColor("#e5e7eb")
    GREEN = colors.HexColor("#15803d")
    RED = colors.HexColor("#b91c1c")
    MUTED = colors.HexColor("#6b7280")

    st_h1 = ParagraphStyle("h1", fontName=font_b, fontSize=15 * s, textColor=NAVY,
                           spaceAfter=2 * s, leading=18 * s)
    st_meta = ParagraphStyle("meta", fontName=font, fontSize=8 * s,
                             textColor=MUTED, leading=11 * s)
    st_h2 = ParagraphStyle("h2", fontName=font_b, fontSize=10.5 * s, textColor=NAVY,
                           spaceBefore=11 * s, spaceAfter=4 * s, leading=13 * s)
    st_cell = ParagraphStyle("cell", fontName=font, fontSize=8.5 * s,
                             textColor=colors.HexColor("#374151"), leading=11 * s)
    st_hcell = ParagraphStyle("hcell", fontName=font_b, fontSize=8.5 * s,
                              textColor=colors.white, leading=11 * s)
    st_sig = ParagraphStyle("sig", fontName=font, fontSize=9 * s,
                            textColor=colors.HexColor("#374151"), leading=15 * s)
    st_sig_b = ParagraphStyle("sigb", fontName=font_b, fontSize=9 * s, textColor=NAVY,
                              leading=13 * s, spaceAfter=3 * s)

    result = view.get("result")
    badge_color = GREEN if result == "uygun" else (RED if result == "uygun_degil" else MUTED)
    st_badge = ParagraphStyle("badge", fontName=font_b, fontSize=11 * s,
                              textColor=badge_color, leading=14 * s)

    pad = 3.5 * s
    W = 174.0

    def kv(rows):
        t = Table([[Paragraph(escape(str(k)), st_cell), Paragraph(escape(str(v)), st_cell)]
                   for k, v in rows], colWidths=[52 * mm, 122 * mm])
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, GREY),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, LIGHT]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        return t

    story = [Paragraph("NUMUNE ANALİZ FORMU", st_h1),
             Paragraph(f"Form No: {escape(str(view.get('document_no') or '—'))}  ·  "
                       f"Kayıt: {escape(str(view.get('created_at') or '—'))}", st_meta),
             Spacer(1, 8 * s)]

    # ── Künye ──
    info = [("Bulk Adı", view.get("bulk_name") or "—"),
            ("Üretim Tarihi", view.get("production_date") or "—"),
            ("Lot Numarası", view.get("lot_number") or "—")]
    if view.get("recipe_name"):
        info.append(("Çalışılan Formülasyon", view["recipe_name"]))
    if view.get("formulation_notes"):
        info.append(("Formülasyon / Sapma Notu", view["formulation_notes"]))
    story.append(kv(info))

    # ── Özellik tablosu ──
    story.append(Paragraph("Bakılan Özellikler", st_h2))
    head = [Paragraph(escape(h), st_hcell)
            for h in ("BAKILAN ÖZELLİKLER", "SPESİFİKASYONLAR", "BULUNAN DEĞER")]
    body = [[Paragraph(escape(str(p.get("label") or "—")), st_cell),
             Paragraph(escape(str(p.get("spec") or "—")), st_cell),
             Paragraph(escape(str(p.get("found") or "—")), st_cell)]
            for p in (view.get("properties") or [])]
    t = Table([head] + body,
              colWidths=[W * 0.30 * mm, W * 0.35 * mm, W * 0.35 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.4, GREY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t)

    # ── Analiz yapan + sonuç rozeti ──
    story.append(Spacer(1, 6 * s))
    story.append(kv([("Analiz Yapan Kişi / Kalite Kontrol Sorumlusu",
                      view.get("analyst_name") or "—")]))
    story.append(Spacer(1, 8 * s))
    story.append(Paragraph(f"● ANALİZ SONUCU: {escape(str(view.get('result_label') or '—'))}",
                           st_badge))

    # ── SONUÇ + Açıklamalar ──
    if view.get("result_text"):
        story.append(Paragraph("Sonuç", st_h2))
        story.append(Paragraph(escape(str(view["result_text"])), st_cell))
    if view.get("notes"):
        story.append(Paragraph("Açıklamalar", st_h2))
        story.append(Paragraph(escape(str(view["notes"])), st_cell))

    # ── İmza blokları ──
    story.append(Spacer(1, 18 * s))
    line = "İmza: ______________________"
    sig = Table([[
        [Paragraph("ONAYLAYAN", st_sig_b),
         Paragraph(escape(str(view.get("approved_by") or "—")), st_sig),
         Paragraph(line, st_sig)],
        [Paragraph("AR-GE &amp; KALİTE KONTROL YÖNETİM TEMSİLCİSİ", st_sig_b),
         Paragraph(escape(str(view.get("qa_representative") or "—")), st_sig),
         Paragraph(line, st_sig)],
    ]], colWidths=[87 * mm, 87 * mm])
    sig.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(sig)

    # ── Form kodu ──
    story.append(Spacer(1, 10 * s))
    story.append(Paragraph(
        escape(str(view.get("form_code") or "")) +
        "  ·  Bu form Minerva 108 ERP numune analiz kaydından üretilmiştir.", st_meta))
    return story


def render_sample_pdf(view: dict) -> bytes:
    from reportlab.lib.units import mm

    content = render_autofit(
        lambda s: _sample_story(view, s),
        margins=(20 * mm, 20 * mm, 48 * mm, 50 * mm),
        doc_kwargs={"title": f"Numune Analiz Formu — {view.get('document_no') or ''}",
                    "author": "Minerva 108"})
    return merge_letterhead(content)
