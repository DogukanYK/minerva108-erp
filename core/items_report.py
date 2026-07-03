"""
Ürün listesi PDF üretici — Ürünler sayfasının sekme/filtre bazlı antetli A4 çıktısı.

  render_items_pdf(view) -> bytes

view = {
  "tab": "hammadde|ambalaj|bitmis_urun|numune",
  "tab_label": "Hammadde", "pkg_label": None|"Etiket", "q": None|"arama",
  "finance": bool,                     # Maliyet kolonu yalnız True ise (sunucuda gate'li)
  "date": "dd.mm.yyyy HH:MM",
  "rows": [...],                       # sekmeye göre satır sözlükleri (aşağıda)
}

Sekme satır biçimleri:
  hammadde / ambalaj: {name, unit, stock, min, supplier, cost, pkg_label, language}
  bitmis_urun: {kind: parent|variation|standalone, name, name_tr, unit, stock, min, cost}
  numune: {item_name, supplier, lot, qty_unit, expiry, received}

Ortak A4/antetli/auto-fit altyapısı core.delivery_note'tan; stil/grid
core.shipment_note'tan paylaşılır. Uzun listeler doğal çok-sayfa (repeatRows=1).
"""
from datetime import datetime
from html import escape

from core.delivery_note import render_autofit, merge_letterhead, slug_part, _fmt
from core.shipment_note import _styles, _grid

TAB_LABELS = {"hammadde": "Hammadde", "ambalaj": "Ambalaj",
              "bitmis_urun": "Bitmiş Ürün", "numune": "Numune"}

# Türkçe alfabetik sıralama — locale modülü platform bağımlı olduğundan order-map.
_TR = "abcçdefgğhıijklmnoöpqrsştuüvwxyz0123456789"
_ORD = {c: i for i, c in enumerate(_TR)}


def tr_key(s):
    """Türkçe sıralama anahtarı. 'İ'.lower() combining-dot ürettiği için önce translate."""
    s = str(s or "").translate({ord("İ"): "i", ord("I"): "ı"}).lower()
    return [_ORD.get(ch, 200 + ord(ch)) for ch in s]


def items_filename(tab, pkg=None, q=None, ext: str = "pdf") -> str:
    parts = [f"urun_listesi_{tab}"]
    if pkg:
        parts.append(slug_part(pkg, 30))
    if q:
        parts.append(slug_part(q, 30))
    parts.append(f"{datetime.utcnow():%Y%m%d}")
    return "_".join(p for p in parts if p) + f".{ext}"


def _title(view) -> str:
    t = f"ÜRÜN LİSTESİ — {view.get('tab_label') or ''}"
    if view.get("pkg_label"):
        t += f" — Alt-tip: {view['pkg_label']}"
    return t


def _items_story(view: dict, s: float = 1.0):
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle

    st = _styles(s)
    W = 174.0
    tab = view.get("tab") or "hammadde"
    fin = bool(view.get("finance"))
    rows = view.get("rows") or []

    meta = f"Tarih: {escape(str(view.get('date') or ''))} · Toplam: {len(rows)} kayıt"
    if view.get("q"):
        meta += f' · Arama: "{escape(str(view["q"]))}"'
    story = [Paragraph(escape(_title(view)), st["h1"]),
             Paragraph(meta, st["meta"]),
             Spacer(1, 6 * s)]

    if not rows:
        story.append(Paragraph("Bu filtreyle eşleşen kayıt yok.", st["cell"]))
        return story

    def pct(*vals):
        return [W * v / 100.0 * mm for v in vals]

    if tab == "numune":
        body = [[i + 1, r.get("item_name") or "—", r.get("supplier") or "—",
                 r.get("lot") or "—", r.get("qty_unit") or "—",
                 r.get("expiry") or "—", r.get("received") or "—"]
                for i, r in enumerate(rows)]
        story.append(_grid(st, ["#", "Hammadde", "Tedarikçi", "Lot / Parti",
                                "Miktar", "SKT", "Giriş"],
                           body, pct(4, 28, 20, 16, 13, 9.5, 9.5), s))
        return story

    if tab == "bitmis_urun":
        # Hiyerarşik: ana ürün bold, varyasyon girintili. Bold için ayrı Paragraph stili.
        from reportlab.lib import colors
        from reportlab.platypus import Table, TableStyle
        st_bold = ParagraphStyle("cellb", parent=st["cell"], fontName=st["font_b"])
        headers = ["#", "Ürün", "Türkçe Ad", "Birim", "Stok", "Min"] + (["Maliyet"] if fin else [])
        widths = pct(5, 32, 25, 8, 10, 8, 12) if fin else pct(5, 36, 29, 9, 11, 10)
        head = [Paragraph(escape(h), st["hcell"]) for h in headers]
        table_rows = [head]
        n = 0
        for r in rows:
            kind = r.get("kind") or "standalone"
            if kind == "variation":
                num = "·"
                name = "   ↳ " + (r.get("name") or "—")
                cell_style = st["cell"]
            else:
                n += 1
                num = str(n)
                name = r.get("name") or "—"
                cell_style = st_bold if kind == "parent" else st["cell"]
            cells = [num, name, r.get("name_tr") or "—", r.get("unit") or "",
                     _fmt(r.get("stock") if r.get("stock") is not None else "—"),
                     _fmt(r.get("min") if r.get("min") is not None else "—")]
            if fin:
                cp = r.get("cost")
                cells.append(f"{float(cp):,.2f}" if cp not in (None, "") else "—")
            table_rows.append([Paragraph(escape(str(c)), cell_style) for c in cells])
        t = Table(table_rows, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), st["NAVY"]),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, st["LIGHT"]]),
            ("GRID", (0, 0), (-1, -1), 0.4, st["GREY"]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (3, 0), (-1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 3.5 * s), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5 * s),
            ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(t)
        return story

    # hammadde / ambalaj — düz liste
    if tab == "ambalaj":
        headers = ["#", "Ürün", "Alt-Tip", "Dil", "Birim", "Stok", "Min", "Tedarikçi"] \
                  + (["Maliyet"] if fin else [])
        widths = pct(4, 28, 10, 6, 8, 10, 8, 14, 12) if fin else pct(4, 33, 11, 7, 9, 11, 9, 16)
        body = []
        for i, r in enumerate(rows):
            cells = [i + 1, r.get("name") or "—", r.get("pkg_label") or "—",
                     r.get("language") or "—", r.get("unit") or "",
                     _fmt(r.get("stock") or 0), _fmt(r.get("min") or 0),
                     r.get("supplier") or "—"]
            if fin:
                cp = r.get("cost")
                cells.append(f"{float(cp):,.2f}" if cp not in (None, "") else "—")
            body.append(cells)
    else:
        headers = ["#", "Ürün", "Birim", "Stok", "Min", "Tedarikçi"] + (["Maliyet"] if fin else [])
        widths = pct(4, 36, 8, 10, 8, 22, 12) if fin else pct(4, 42, 9, 11, 9, 25)
        body = []
        for i, r in enumerate(rows):
            cells = [i + 1, r.get("name") or "—", r.get("unit") or "",
                     _fmt(r.get("stock") or 0), _fmt(r.get("min") or 0),
                     r.get("supplier") or "—"]
            if fin:
                cp = r.get("cost")
                cells.append(f"{float(cp):,.2f}" if cp not in (None, "") else "—")
            body.append(cells)
    story.append(_grid(st, headers, body, widths, s))
    return story


def render_items_pdf(view: dict) -> bytes:
    from reportlab.lib.units import mm
    content = render_autofit(
        lambda s: _items_story(view, s),
        margins=(20 * mm, 20 * mm, 48 * mm, 30 * mm),
        doc_kwargs={"title": _title(view), "author": "Minerva 108"})
    return merge_letterhead(content)
