"""
Kargo / Sevkiyat PDF üreticileri — antetli A4 (core.delivery_note altyapısı).

  render_packing_list_pdf(view)     -> bytes   tek sevkiyat hazırlık/paketleme listesi
  render_master_packing_pdf(views)  -> bytes   tüm bekleyenler 'elime gelecekler' master listesi

Ortak A4/antetli/auto-fit altyapısı core.delivery_note'tan paylaşılır
(render_autofit + merge_letterhead + content_disposition + slug_part), Türkçe-uyumlu
fontlar core.monthly_report._register_fonts'tan. Sıfırdan reportlab kurulumu yok.
"""
from datetime import datetime
from html import escape

from core.delivery_note import (render_autofit, merge_letterhead, slug_part, _fmt)


def packing_filename(document_no, recipient=None, ext: str = "pdf") -> str:
    no = (str(document_no) or "sevkiyat").replace("/", "-").replace(" ", "_")
    who = slug_part(recipient)
    return f"kargo_hazirlik_{who + '_' if who else ''}{no}.{ext}"


def master_packing_filename(ext: str = "pdf") -> str:
    return f"kargo_bekleyenler_{datetime.utcnow():%Y%m%d}.{ext}"


# ─── Ortak stil + tablo yardımcıları (ölçeklenebilir) ────────────────────────

def _styles(s: float):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from core.monthly_report import _register_fonts
    font, font_b = _register_fonts()
    NAVY = colors.HexColor("#232E6E")
    return {
        "font": font, "font_b": font_b, "NAVY": NAVY,
        "LIGHT": colors.HexColor("#f5f0e8"), "GREY": colors.HexColor("#e5e7eb"),
        "h1": ParagraphStyle("h1", fontName=font_b, fontSize=15 * s, textColor=NAVY, spaceAfter=2 * s, leading=18 * s),
        "meta": ParagraphStyle("meta", fontName=font, fontSize=8 * s, textColor=colors.HexColor("#6b7280"), leading=11 * s),
        "h2": ParagraphStyle("h2", fontName=font_b, fontSize=10.5 * s, textColor=NAVY, spaceBefore=11 * s, spaceAfter=4 * s, leading=13 * s),
        "cell": ParagraphStyle("cell", fontName=font, fontSize=8.5 * s, textColor=colors.HexColor("#374151"), leading=11 * s),
        "hcell": ParagraphStyle("hcell", fontName=font_b, fontSize=8.5 * s, textColor=colors.white, leading=11 * s),
        "sig": ParagraphStyle("sig", fontName=font, fontSize=9 * s, textColor=colors.HexColor("#374151"), leading=14 * s),
    }


def _grid(st, headers, rows, widths, s):
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle
    head = [Paragraph(escape(str(h)), st["hcell"]) for h in headers]
    body = [[Paragraph(escape(str(c)), st["cell"]) for c in r] for r in rows]
    t = Table([head] + body, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), st["NAVY"]),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, st["LIGHT"]]),
        ("GRID", (0, 0), (-1, -1), 0.4, st["GREY"]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (2, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5 * s), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5 * s),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def _kv(st, rows, s):
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle
    from reportlab.lib.units import mm
    data = [[Paragraph(escape(str(k)), st["cell"]), Paragraph(escape(str(v)), st["cell"])] for k, v in rows]
    t = Table(data, colWidths=[44 * mm, 130 * mm])
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, st["GREY"]),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, st["LIGHT"]]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5 * s), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5 * s),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


# ─── 1) Tek sevkiyat hazırlık / paketleme listesi ────────────────────────────

def _packing_story(view: dict, s: float = 1.0):
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer
    st = _styles(s)
    W = 174.0
    story = [Paragraph("KARGO HAZIRLIK / PAKETLEME LİSTESİ", st["h1"]),
             Paragraph(f"Belge No: {escape(str(view.get('document_no') or '—'))}  ·  "
                       f"Tarih: {escape(str(view.get('date') or '—'))}", st["meta"]),
             Spacer(1, 8 * s)]

    story.append(Paragraph("Sevkiyat Bilgisi", st["h2"]))
    info = []
    if (view.get("recipient_name") or "").strip():
        info.append(("Alıcı", view["recipient_name"]))
    if view.get("recipient_org"):
        info.append(("Firma / Kurum", view["recipient_org"]))
    if view.get("recipient_phone"):
        info.append(("Telefon", view["recipient_phone"]))
    info.append(("Durum", view.get("status_label") or "—"))
    if view.get("tracking_no"):
        info.append(("Kargo Takip No", view["tracking_no"]))
    if view.get("carrier"):
        info.append(("Taşıyıcı", view["carrier"]))
    info.append(("Hazırlayan", view.get("dispatched_by") or "—"))
    if not [k for k, _ in info if k == "Alıcı"]:
        info.insert(0, ("Alıcı", "— (belirtilmedi)"))
    story.append(_kv(st, info, s))

    story.append(Paragraph("Toplanacak / Paketlenecek Ürünler", st["h2"]))
    rows = [[i + 1, it["item_name"], _fmt(it["quantity"]), (it.get("unit") or ""), "☐"]
            for i, it in enumerate(view.get("items", []))]
    story.append(_grid(st, ["#", "Ürün", "Miktar", "Birim", "Toplandı"], rows,
                       [W * 0.07 * mm, W * 0.55 * mm, W * 0.14 * mm, W * 0.11 * mm, W * 0.13 * mm], s))

    if view.get("note"):
        story.append(Paragraph("Not", st["h2"]))
        story.append(Paragraph(escape(str(view["note"])), st["cell"]))

    story.append(Spacer(1, 22 * s))
    story.append(Paragraph("Hazırlayan / Paketleyen: ______________________________"
                           "&nbsp;&nbsp;&nbsp; İmza: ________________", st["sig"]))
    return story


def render_packing_list_pdf(view: dict) -> bytes:
    from reportlab.lib.units import mm
    content = render_autofit(
        lambda s: _packing_story(view, s),
        margins=(20 * mm, 20 * mm, 48 * mm, 40 * mm),
        doc_kwargs={"title": f"Kargo Hazırlık — {view.get('document_no') or ''}", "author": "Minerva 108"})
    return merge_letterhead(content)


# ─── 2) Master toplama listesi (tüm bekleyen kargolar) ───────────────────────

def _master_story(views, s: float = 1.0):
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer
    st = _styles(s)
    W = 174.0
    n = len(views)
    total_lines = sum(len(v.get("items", [])) for v in views)
    story = [Paragraph("BEKLEYEN KARGOLAR — TOPLAMA LİSTESİ", st["h1"]),
             Paragraph(f"{n} sevkiyat · {total_lines} kalem  ·  "
                       f"Üretim tarihi: {datetime.utcnow():%d.%m.%Y}", st["meta"]),
             Spacer(1, 6 * s)]

    if not views:
        story.append(Paragraph("Bekleyen kargo sevkiyatı yok.", st["cell"]))
        return story

    # Bölüm A — ürün-bazlı toplam (depodan toplanacak)
    agg = {}
    order = []
    for v in views:
        for it in v.get("items", []):
            key = (it.get("item_name") or "—", it.get("unit") or "")
            if key not in agg:
                agg[key] = 0.0
                order.append(key)
            agg[key] += float(it.get("quantity") or 0)
    story.append(Paragraph("Toplanacak Ürünler (toplam)", st["h2"]))
    arows = [[i + 1, name, _fmt(agg[(name, unit)]), unit, "☐"]
             for i, (name, unit) in enumerate(order)]
    story.append(_grid(st, ["#", "Ürün", "Toplam", "Birim", "Toplandı"], arows,
                       [W * 0.07 * mm, W * 0.55 * mm, W * 0.14 * mm, W * 0.11 * mm, W * 0.13 * mm], s))

    # Bölüm B — sevkiyat-bazında kırılım
    story.append(Paragraph("Sevkiyat Kırılımı", st["h2"]))
    for v in views:
        who = (v.get("recipient_name") or "").strip() or (v.get("recipient_org") or "").strip() or "— (alıcı belirtilmedi)"
        story.append(Paragraph(
            f"<b>{escape(str(v.get('document_no') or '—'))}</b> · {escape(who)} · "
            f"{escape(str(v.get('date') or ''))}", st["cell"]))
        rows = [[i + 1, it["item_name"], _fmt(it["quantity"]), (it.get("unit") or "")]
                for i, it in enumerate(v.get("items", []))]
        story.append(_grid(st, ["#", "Ürün", "Miktar", "Birim"], rows,
                           [W * 0.07 * mm, W * 0.62 * mm, W * 0.16 * mm, W * 0.15 * mm], s))
        story.append(Spacer(1, 5 * s))
    return story


def render_master_packing_pdf(views) -> bytes:
    from reportlab.lib.units import mm
    content = render_autofit(
        lambda s: _master_story(list(views), s),
        margins=(20 * mm, 20 * mm, 48 * mm, 40 * mm),
        doc_kwargs={"title": "Kargo Bekleyenler — Master Liste", "author": "Minerva 108"})
    return merge_letterhead(content)
