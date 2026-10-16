# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Satın Alma Planı — "Tedarikçili ve Fiyatlı Alım Listesi" PDF'i.

    render_pdf(report) -> bytes

`report` = `core.purchase_plan.compute()` çıktısı + `core.purchase_pricing.
attach()` alanları (pricing, suppliers, malzeme fiyatları).  Bölüm sırası ve
numaraları `purchase_pricing.sections()`'tan gelir — UI ve Excel ile AYNI
numaralandırma; boş bölüm basılmaz.

Görünüm Rusya siparişi raporunun (`Rusya-Siparis/fiyat/fiyatli_liste.py`)
birebir uyarlaması: yatay A4, antet yok, sayfa altında "Minerva 108 · başlık ·
stoklar tarih" + "Sayfa N"; başta kapsam metni, turuncu uyarı kutusu, yeşil
özet kutusu; malzeme tablolarında seçilen teklif (tercih yoksa en ucuz) kalın, diğerleri gri
"(daha pahalı)", fiyatsızda kırmızı "Fiyat yok — teklif alınacak", altında
stok kartı / son alım / numune ilişkileri, "Sipariş:" ve "Aynı malzeme:"
(eşdeğer tedarikçi kartları) satırları ve uyarı notları; elle işaretlemek
için boş "Alındı" sütunu.  "Elimizde" hücresinin altında grup stoğu
sayıldıysa kart dökümü (`purchase_plan.stock_note`) küçük gri satırda.
Ambalaj uyarısı (16.10.2026) olan satırda Tutar'ın önünde kırmızı ⚠, metni
"Not:" satırında.  "Maliyet özeti" bölümü (16.10.2026) yeni sayfada: tür
bazında maliyet, gereken miktarlar, ürün başına maliyet, fiyatı eksik
kalemler (ilk 80 + "+N"), notlar (`purchase_pricing.cost_overview_view`).
Stoktan karşılanan değer (14.10.2026, yalnız net mod): alım tablolarında
Tutar'ın altında gri "stoktan X" / "yoldan Y"; "Yeterli olanlar"da "Değer"
sütunu (dayanağı — seçilen teklif ya da "Fiyat yok" — altında gri); yeşil
kutuda alınacak + stoktan (+ yoldan) = "Planın toplam malzeme maliyeti" ve
uyarıları (`purchase_pricing.cost_summary_lines` / `_caveats`, önizleme ve
Excel ile aynı metin).  Brüt modda hiçbiri basılmaz.

Bütün dinamik metin (malzeme/tedarikçi adı, not, iletişim) reportlab
Paragraph'ına `escape()` ile girer — adında "&" ya da "<" geçen bir kart
PDF üretimini düşürmesin / biçim etiketi enjekte edemesin.
"""
from datetime import datetime
from io import BytesIO
from typing import List, Optional
from xml.sax.saxutils import escape

from core.purchase_plan import _amount_near, stock_note, tr_num
from core.purchase_pricing import (CURRENCY_SYMBOL, PKG_SUFFIX, UNIT_TEXT, _qty,  # noqa: F401  (PKG_SUFFIX/_qty re-export)
                                   amount_warn, cost_overview_view, cost_summary_caveats, cost_summary_lines,
                                   cover_text, cover_value, money, offer_lines, pkg_round_text, price_text,
                                   relation_texts, sections, value_basis_text)

CURRENCY_NAME = {"USD": "ABD doları", "EUR": "avro", "TRY": "Türk lirası"}
KIND_HEADER = {"raw": "Hammadde", "packaging": "Ambalaj", "label": "Etiket"}
SECTION_KIND = {"raw_priced": "raw", "raw_unpriced": "raw", "pkg_priced": "packaging",
                "pkg_unpriced": "packaging", "labels": "label"}
COST_UNPRICED_MAX = 80        # "Maliyet özeti" — fiyatı eksik kalemler en çok 80 + "+N"
# ⚠ (U+26A0) her fontta yok (macOS Arial'da yok, DejaVu Sans'ta var): ana
# fontta yoksa bu adaylardan biri yalnız işaret için kaydedilir; hiçbiri
# yoksa kalın kırmızı "!" basılır.
_WARN_GLYPH = "\u26a0"
_SYMBOL_FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Apple Symbols.ttf")
TR_MONTHS = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos",
             "Eylül", "Ekim", "Kasım", "Aralık")

# Rusya raporunun renkleri
_NAVY, _LINE, _HEAD, _ALT = "#1f2a5c", "#9aa3b5", "#e8ecf6", "#f6f7fb"
_GREY, _WARN = "#555e70", "#9a3412"

# Malzeme tablosu sütunları (mm) — toplam 267 = yatay A4 − 2×15 mm kenar.
# "Yolda" sütunu (açık siparişler) yalnız seçenek açıkken; genişliği tedarikçi
# sütunundan alınır ki tablo sayfaya sığsın.
_W_MAT = {"name": 55, "need": 26, "stock": 23, "open": 20, "buy": 27, "sup": 97, "amount": 22, "got": 17}
_PAGE_W = 267
# Bu uyarılar ayrı "Not:" satırı yerine adın yanında kısa işaret olarak basılır.
_NAME_CAUTIONS = {"estimated"}


def _e(v) -> str:
    return escape("" if v is None else str(v))


def _warn_font(font: str) -> Optional[str]:
    """⚠ işaretini basabilen font adı (ana font, yoksa kayıtlı sembol fontu)
    ya da None."""
    import os

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    def has(name):
        try:
            face = getattr(pdfmetrics.getFont(name), "face", None)
            return face is not None and ord(_WARN_GLYPH) in getattr(face, "charToGlyph", {})
        except Exception:
            return False
    if has(font):
        return font
    if "RPT-SYM" in pdfmetrics.getRegisteredFontNames() and has("RPT-SYM"):
        return "RPT-SYM"
    for path in _SYMBOL_FONTS:
        if os.path.isfile(path):
            try:
                pdfmetrics.registerFont(TTFont("RPT-SYM", path))
            except Exception:
                continue
            if has("RPT-SYM"):
                return "RPT-SYM"
    return None


def _long_date(stamp: Optional[str]) -> str:
    """'02.10.2026 12:00' → '2 Ekim 2026' (sayfa altı)."""
    try:
        d = datetime.strptime((stamp or "")[:10], "%d.%m.%Y")
    except ValueError:
        return stamp or ""
    return f"{d.day} {TR_MONTHS[d.month - 1]} {d.year}"


class _Ctx:
    """Font/stil/renk + rapor geneli değerler (para birimi, seçenekler)."""

    def __init__(self, report: dict):
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle

        from core.monthly_report import _register_fonts

        self.font, self.font_b = _register_fonts()
        f, fb = self.font, self.font_b
        self.colors = colors
        self.NAVY, self.LINE = colors.HexColor(_NAVY), colors.HexColor(_LINE)
        self.HEAD, self.ALT = colors.HexColor(_HEAD), colors.HexColor(_ALT)
        P = ParagraphStyle
        self.S = {
            "h1": P("h1", fontName=fb, fontSize=21, leading=26, textColor=self.NAVY, spaceAfter=3),
            "sub": P("sub", fontName=f, fontSize=12.5, leading=17, spaceAfter=6),
            "h2": P("h2", fontName=fb, fontSize=16, leading=21, textColor=self.NAVY, spaceBefore=10, spaceAfter=3),
            "h2s": P("h2s", fontName=f, fontSize=11.5, leading=15, spaceAfter=5),
            "h3": P("h3", fontName=fb, fontSize=14, leading=18, textColor=self.NAVY, spaceBefore=8, spaceAfter=2),
            "cell": P("cell", fontName=f, fontSize=11.5, leading=14.5),
            "num": P("num", fontName=fb, fontSize=11.5, leading=14.5, alignment=2),
            "val": P("val", fontName=f, fontSize=11.5, leading=14.5, alignment=2),
            "th": P("th", fontName=fb, fontSize=11.5, leading=14.5, textColor=self.NAVY),
            "note": P("note", fontName=f, fontSize=11.5, leading=16),
            "box": P("box", fontName=f, fontSize=13, leading=19, backColor=colors.HexColor("#eef6ee"),
                     borderPadding=8, spaceBefore=4, spaceAfter=12),
            "warn": P("warn", fontName=fb, fontSize=11.5, leading=16, textColor=colors.HexColor(_WARN),
                      backColor=colors.HexColor("#fff4e5"), borderPadding=6, spaceAfter=10),
        }
        meta = report.get("meta") or {}
        self.opts = meta.get("options") or {}
        pr = report.get("pricing") or {}
        self.cur = pr.get("currency") or self.opts.get("currency") or "USD"
        self.sym = CURRENCY_SYMBOL.get(self.cur, self.cur)
        self.gross = self.opts.get("stock_mode") == "gross"
        self.open_on = bool(self.opts.get("subtract_open_orders"))
        self.owner = " ".join(str(self.opts.get("checklist_owner") or "").split()) or None
        wf = _warn_font(f)
        self.warn_mark = (f"<font name='{wf}' color='{_WARN}'>{_WARN_GLYPH}</font>" if wf
                          else f"<font name='{fb}' color='{_WARN}'>!</font>")

    # ── metin parçaları ────────────────────────────────────────────────────
    def bold(self, t: str) -> str:
        return f"<font name='{self.font_b}'>{t}</font>"

    def grey(self, t: str) -> str:
        return f"<font color='{_GREY}'>{t}</font>"

    def red(self, t: str) -> str:
        return f"<font color='{_WARN}' name='{self.font_b}'>{t}</font>"

    def money(self, v) -> str:
        return money(v, self.cur)

    def marks(self, t: str) -> str:
        """Kaçışlanmış metindeki ⚠ işaretini basılabilir işaretle değiştirir."""
        return t.replace(_WARN_GLYPH, self.warn_mark)

    def no_contact(self) -> str:
        return self.red("İletişim bilgisi sistemde yok" + (f" — {_e(self.owner)} girecek" if self.owner else ""))

    # ── tablo ──────────────────────────────────────────────────────────────
    def table(self, data, widths_mm, *, header=True, zebra=True, split_in_row=True):
        from reportlab.lib.units import mm
        from reportlab.platypus import Table, TableStyle

        st = [("GRID", (0, 0), (-1, -1), 0.6, self.LINE), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
              ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5)]
        if header:
            st.append(("BACKGROUND", (0, 0), (-1, 0), self.HEAD))
        if zebra:
            st += [("BACKGROUND", (0, i), (-1, i), self.ALT) for i in range(2, len(data), 2)]
        # splitInRow: notları çok uzun tek bir satır sayfadan taşarsa reportlab
        # "Flowable too large" atıp bütün PDF'i düşürürdü; satır bölünsün.
        t = Table(data, colWidths=[w * mm for w in widths_mm], repeatRows=1 if header else 0,
                  splitInRow=1 if split_in_row else 0)
        t.setStyle(TableStyle(st))
        return t


# ─── Malzeme satırı ─────────────────────────────────────────────────────────

def _sup_cell(c: _Ctx, m: dict) -> str:
    """Tedarikçi ve birim fiyat hücresi — fiyatli_liste.sup_cell sırası:
    seçilen teklif kalın · diğerleri gri ("daha pahalı" / "tercih" / "eşdeğer
    kart «X»" / " — bitirilecek, alma"; `offer_lines`, önizlemeyle aynı) ·
    fiyatsızsa kırmızı ·
    ilişkiler (stok kartı / son alım / numune; "Sipariş:" ve "Aynı malzeme:"
    satırları en çok 3 + "+N" — `relation_texts`, önizlemeyle aynı) · notlar."""
    style = {"b": c.bold, "g": c.grey, "r": c.red}
    out: List[str] = [style[st](_e(t)) for t, st in offer_lines(m, c.cur)]
    out += [c.grey(_e(t)) for t in relation_texts(m)]
    pkg = pkg_round_text(m, c.cur)          # önizleme / Excel "Not" ile aynı metin
    if pkg:
        out.append(c.grey(_e(pkg)))
    for cau in m.get("cautions") or []:
        if cau["code"] not in _NAME_CAUTIONS:
            out.append(c.grey("Not: " + _e(cau["text"])))
    return "<br/>".join(out)


def _name_cell(c: _Ctx, m: dict) -> str:
    """Malzeme adı + "tahmini" işareti (her satıra ayrı bir "Not:" satırı
    yazmak yerine adın yanında; ölçekli/başka ürünün reçetesi kullanıldı)."""
    t = _e(m["name"])
    for cau in m.get("cautions") or []:
        if cau["code"] in _NAME_CAUTIONS:
            t += " " + c.grey("(" + _e(cau["text"].rstrip(". ").lower()) + ")")
    return t


def _stock_cell(c: _Ctx, m: dict, d: dict) -> str:
    """"Elimizde" + altında küçük gri grup stoğu dökümü (`stock_note` —
    önizleme ve Excel AA ile aynı metin)."""
    t = _e(d.get("stock_text"))
    note = stock_note(m)
    if note:
        t += "<br/>" + c.grey(f"<font size='8.5'>{_e(note)}</font>")
    return t


def _small_grey(c: _Ctx, t: str) -> str:
    return c.grey(f"<font size='8.5'>{_e(t)}</font>")


def _amount_cell(c: _Ctx, m: dict) -> str:
    """Tutar + altında gri "stoktan X · yoldan Y" (satırın stoktan / yoldan
    karşılanan kısmının değeri; tutara ve GENEL TOPLAM'a girmez).  Ambalaj
    uyarısı varsa tutarın önünde kırmızı ⚠ (metni tedarikçi hücresinin
    "Not:" satırında)."""
    t = c.money(m["amount"]) if m.get("amount") is not None else "—"
    if amount_warn(m):
        t = c.warn_mark + " " + t
    cov = cover_text(m, c.cur)          # önizlemenin `ui.cover_text`'iyle aynı metin
    if cov:
        t += "<br/>" + _small_grey(c, cov)
    return t


def _material_table(c: _Ctx, rows: List[dict], kind: str):
    from reportlab.platypus import Paragraph

    S = c.S
    cols = ["name", "need", "stock"] + (["open"] if c.open_on else []) + ["buy", "sup", "amount", "got"]
    w = dict(_W_MAT)
    if c.open_on:
        w["sup"] -= w["open"]
    head = {"name": KIND_HEADER.get(kind, "Malzeme"), "need": "Toplam gereken",
            "stock": "Elimizde (bilgi)" if c.gross else "Elimizde",
            "open": "Yolda (bilgi)" if c.gross else "Yolda", "buy": "Alınacak",
            "sup": "Tedarikçi ve birim fiyat", "amount": "Tutar", "got": "Alındı"}
    data = [[Paragraph(head[k], S["th"]) for k in cols]]
    for m in rows:
        d = m.get("display") or {}
        cell = {"name": Paragraph(_name_cell(c, m), S["cell"]),
                "need": Paragraph(_e(d.get("need_text")), S["val"]),
                "stock": Paragraph(_stock_cell(c, m, d), S["val"]),
                "open": Paragraph(_e(d.get("open_text")), S["val"]),
                "buy": Paragraph(_e(d.get("buy_text")), S["num"]),
                "sup": Paragraph(_sup_cell(c, m), S["cell"]),
                "amount": Paragraph(_amount_cell(c, m), S["num"]),
                "got": ""}
        data.append([cell[k] for k in cols])
    return c.table(data, [w[k] for k in cols])


# ─── Bölümler ───────────────────────────────────────────────────────────────

def _heading(c: _Ctx, sec: dict, *, page_break=False, subtitle: Optional[str] = None) -> list:
    from reportlab.lib.units import mm
    from reportlab.platypus import CondPageBreak, KeepTogether, PageBreak, Paragraph, Spacer

    title = f"{sec['no']}. {_e(sec['title'])}" + (f" ({_e(sec['summary'])})" if sec.get("summary") else "")
    sub = subtitle if subtitle is not None else (_e(sec["subtitle"]) if sec.get("subtitle") else None)
    head = [Paragraph(title, c.S["h2"])] + ([Paragraph(sub, c.S["h2s"])] if sub else [])
    brk = PageBreak() if page_break else CondPageBreak(55 * mm)
    return [brk, KeepTogether(head + [Spacer(1, 2)])]


def _sec_new_items(c: _Ctx, sec: dict) -> list:
    from reportlab.platypus import Paragraph

    S = c.S
    labels = [r for r in sec["rows"] if r.get("type") == "label"]
    sub = ""
    if labels:
        title = labels[0].get("title") or "Yeni etiket"
        sub = f"Etiketler yeni basılacak ({_e(title)}, toplam {tr_num(sum(r['total'] for r in labels))} adet). "
    sub += _e(sec.get("subtitle") or "")
    data = [[Paragraph(h, S["th"]) for h in ("Kalem", "Alınacak", "Not", "Alındı")]]
    nop = c.red("Fiyat yok — teklif alınacak")
    for r in sec["rows"]:
        if r.get("type") == "label":
            qty = f"{tr_num(r['total'])} adet"
            note = nop + (f" · {_e(r['face_text'])}" if r.get("face_text") else "")
        else:
            qty = _e(r.get("qty_text") or "")
            note = nop + " · sistemde stok kaydı yok" + (f" · {_e(r['note'])}" if r.get("note") else "")
        data.append([Paragraph(_e(r["name"]), S["cell"]), Paragraph(qty, S["num"]),
                     Paragraph(note, S["cell"]), ""])
    return _heading(c, sec, subtitle=sub) + [c.table(data, [95, 40, 115, 17])]


def _sec_suppliers(c: _Ctx, sec: dict) -> list:
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, Table, TableStyle

    S = c.S
    sub = _e(sec.get("subtitle") or "") + " İletişim bilgileri sistemdeki tedarikçi kartlarından" + (
        f"; boş olanları {_e(c.owner)} sisteme girecek." if c.owner else ".")
    out = _heading(c, sec, page_break=True, subtitle=sub)
    for b in sec["rows"]:
        blk = [Paragraph(f"{_e(b['name'])} — {b['count']} kalem — {c.money(b['total'])}", S["h3"]),
               Paragraph(_e(b["contact_line"]) if b.get("contact_line") else c.no_contact(), S["note"])]
        data = [[Paragraph(_e(it["name"]) + (" " + c.grey("(" + _e(", ".join(it["reasons"])) + ")")
                                             if it.get("reasons") else ""), S["cell"]),
                 Paragraph(_e(it["buy_text"]), S["val"]),
                 Paragraph(f"{price_text(it['price'])} {c.sym}/{UNIT_TEXT.get(it['price_unit'], it['price_unit'])}"
                           if it.get("price") is not None else "—", S["val"]),
                 Paragraph(c.money(it["amount"]) if it.get("amount") is not None else "—", S["num"])]
                for it in b["items"]]
        tb = Table(data, colWidths=[110 * mm, 35 * mm, 35 * mm, 30 * mm])
        tb.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.4, c.LINE),
                                ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                                ("LEFTPADDING", (0, 0), (-1, -1), 4)]))
        out.append(KeepTogether(blk + [tb]))
    return out


def _sec_candidates(c: _Ctx, sec: dict) -> list:
    from reportlab.platypus import KeepTogether, Paragraph

    S = c.S
    out = _heading(c, sec, page_break=True)
    for b in sec["rows"]:
        if b.get("none"):
            title = f"{_e(b['name'])} — {b['count']} kalem"
            cl = _e(b.get("contact_line") or "")
        else:
            title = f"{_e(b['name'])} — {b['count']} kalem"
            cl = _e(b["contact_line"]) if b.get("contact_line") else c.no_contact()
        txt = ", ".join(f"{_e(it['name'])} ({_e(it['buy_text'])}"
                        + (f"; {_e(', '.join(it['reasons']))}" if it.get("reasons") else "") + ")"
                        for it in b["items"])
        out.append(KeepTogether([Paragraph(title, S["h3"]), Paragraph(cl, S["note"]),
                                 Paragraph(txt, S["cell"])]))
    return out


def _sec_checklist(c: _Ctx, sec: dict) -> list:
    from reportlab.platypus import KeepTogether, Paragraph

    paras = []
    for r in sec["rows"]:
        head, sep, rest = r["text"].partition(": ")
        txt = (_e(head) + sep + c.bold(_e(rest))) if sep else _e(r["text"])
        paras.append(Paragraph(txt, c.S["note"]))
    h = _heading(c, sec)
    return [h[0], KeepTogether([h[1]] + paras)]


def _sec_products(c: _Ctx, sec: dict) -> list:
    from reportlab.platypus import Paragraph

    S = c.S
    sub_on = bool(c.opts.get("subtract_finished_stock"))
    cols = [("no", "No", 11), ("name", "Ürün", 58), ("brand", "Marka", 26), ("qty", "Hedef adet", 24),
            ("fin", "Bitmiş stok", 22)]
    if sub_on:
        cols.append(("prod", "Üretilecek", 23))
    cols += [("cap", "Elimizdekiyle üretilebilir", 30), ("lim", "Kısıtlayan malzeme", 42)]
    cols.append(("note", "Not", _PAGE_W - sum(w for _, _, w in cols)))
    data = [[Paragraph(h, S["th"]) for _, h, _ in cols]]
    for p in sec["rows"]:
        name = _e(p["name"])
        if p.get("estimated") and p.get("recipe_name"):
            name += "<br/>" + c.grey(f"Reçete: {_e(p['recipe_name'])}"
                                     + (f" ×{tr_num(p['scale'], 2)}" if p.get("scale") not in (None, 1, 1.0) else ""))
        cap = p.get("capacity") or {}
        lim = "<br/>".join(f"{_e(x['name'])} ({_e(_amount_near(x['stock'], x['unit']))})"
                           for x in cap.get("limiting") or [])
        notes = [_e(x["text"]) for x in p.get("cautions") or []]
        if p.get("note"):
            notes.append("Not: " + _e(p["note"]))
        cell = {"no": Paragraph(str(p["no"]), S["val"]), "name": Paragraph(name, S["cell"]),
                "brand": Paragraph(_e(p.get("brand") or "—"), S["cell"]),
                "qty": Paragraph(tr_num(p["qty"]), S["num"]),
                "fin": Paragraph(tr_num(max(0.0, p.get("finished_stock") or 0.0)), S["val"]),
                "prod": Paragraph(tr_num(p.get("produce_qty") or 0), S["val"]),
                "cap": Paragraph(tr_num(cap["producible"]) if cap.get("producible") is not None else "—", S["val"]),
                "lim": Paragraph(lim or "—", S["cell"]),
                "note": Paragraph(c.grey("<br/>".join(notes)) if notes else "", S["cell"])}
        data.append([cell[k] for k, _, _ in cols])
    sub = ("“Elimizdekiyle üretilebilir”: bugünkü stokla (alım yapılmadan) o ürünün tek başına kaç adet "
           "üretilebileceği; kısıtlayan malzeme ilk biten.")
    return _heading(c, sec, subtitle=sub) + [c.table(data, [w for _, _, w in cols])]


def _sec_sufficient(c: _Ctx, sec: dict) -> list:
    from reportlab.platypus import Paragraph

    S = c.S
    cols = [("name", "Malzeme", 75), ("kind", "Tür", 30), ("need", "Toplam gereken", 30), ("stock", "Elimizde", 30)]
    if c.open_on:
        cols.append(("open", "Yolda", 24))
    # "Değer" (planın kullanacağı stok × seçilen teklif) — genişliği Not'tan
    cols.append(("value", "Değer", 38))
    cols.append(("note", "Not", _PAGE_W - sum(w for _, _, w in cols)))
    data = [[Paragraph(h, S["th"]) for _, h, _ in cols]]
    for m in sec["rows"]:
        d = m.get("display") or {}
        notes = "<br/>".join(_e(x["text"]) for x in m.get("cautions") or [] if x["code"] not in _NAME_CAUTIONS)
        cell = {"name": Paragraph(_name_cell(c, m), S["cell"]),
                "kind": Paragraph(KIND_HEADER.get(m["kind"], ""), S["cell"]),
                "need": Paragraph(_e(d.get("need_text")), S["val"]),
                "stock": Paragraph(_stock_cell(c, m, d), S["val"]),
                "open": Paragraph(_e(d.get("open_text")), S["val"]),
                "value": Paragraph(_value_cell(c, m), S["num"]),
                "note": Paragraph(c.grey(notes) if notes else "", S["cell"])}
        data.append([cell[k] for k, _, _ in cols])
    return _heading(c, sec) + [c.table(data, [w for _, _, w in cols])]


def _sec_cost(c: _Ctx, report: dict, sec: dict) -> list:
    """"Maliyet özeti" (yeni sayfada): tür bazında maliyet tablosu + uyarılar,
    gereken miktarlar, ürün başına maliyet (1 adet + toplam), fiyatı eksik
    kalemler (ilk 80 + "+N"), notlar — metinler `cost_overview_view`
    (önizleme ve Excel ile aynı)."""
    from reportlab.lib.units import mm
    from reportlab.platypus import CondPageBreak, KeepTogether, Paragraph

    S = c.S
    v = cost_overview_view(report)
    if not v:
        return []
    out = _heading(c, sec, page_break=True)
    cols = [("label", "Tür", 60), ("buy", "Alınacak", 36)]
    if v["show_stock"]:
        cols.append(("stock", "Stoktan", 36))
    if v["show_open"]:
        cols.append(("open", "Yoldan", 36))
    cols += [("total", "Toplam", 36), ("priced", "Fiyatlı", 24), ("unpriced", "Fiyatsız", 24)]
    data = [[Paragraph(h, S["th"]) for _, h, _ in cols]]
    for r in v["categories"] + [v["total_row"]]:
        tot = r is v["total_row"]
        data.append([Paragraph(c.bold(_e(r[k])) if tot or k == "total" else _e(r[k]),
                               S["cell"] if k == "label" else S["val"]) for k, _, _ in cols])
    out.append(c.table(data, [w for _, _, w in cols]))
    for t in v["caveats"]:
        out.append(Paragraph(c.red(c.marks(_e(t))), S["note"]))
    # Gereken miktarlar
    q = [[Paragraph(c.bold(_e(x["label"])), S["cell"]), Paragraph(_e(x["text"]), S["cell"])]
         for x in v["quantities"]]
    out.append(KeepTogether([Paragraph("Gereken miktarlar", S["h3"]), c.table(q, [45, 150], header=False)]))
    # Ürün başına maliyet
    pcols = [("no", "No", 11), ("name", "Ürün", 100), ("qty", "Adet", 25), ("unit_cost", "1 adet maliyeti", 32),
             ("total", "Toplam", 30)]
    pcols.append(("miss", "Fiyatı eksik", _PAGE_W - sum(w for _, _, w in pcols)))
    pdata = [[Paragraph(h, S["th"]) for _, h, _ in pcols]]
    for p in v["products"]:
        miss = " — ".join(_e(x) for x in (p["note"], p["missing"]) if x)
        if p["missing_names"]:
            miss += ("<br/>" if miss else "") + _small_grey(c, p["missing_names"])
        pdata.append([Paragraph(str(p["no"]), S["val"]), Paragraph(_e(p["name"]), S["cell"]),
                      Paragraph(_e(p["qty"]), S["val"]), Paragraph(_e(p["unit_cost"]), S["val"]),
                      Paragraph(_e(p["total"]), S["num"]), Paragraph(miss, S["cell"])])
    pdata.append([Paragraph("", S["cell"]), Paragraph(c.bold("Toplam"), S["cell"]),
                  Paragraph(c.bold(_e(v["units_total"])), S["val"]), Paragraph("", S["val"]),
                  Paragraph(c.bold(_e(v["products_total"])), S["num"]), Paragraph("", S["cell"])])
    out += [CondPageBreak(40 * mm), Paragraph("Ürün başına maliyet", S["h3"]),
            c.table(pdata, [w for _, _, w in pcols])]
    # Fiyatı eksik kalemler
    un = v["unpriced"]
    if un:
        udata = [[Paragraph(h, S["th"]) for h in ("Kalem", "Tür", "İhtiyaç")]]
        for u in un[:COST_UNPRICED_MAX]:
            udata.append([Paragraph(_e(u["name"]) + (" " + c.grey("(yeni kalem, kartı yok)") if u["new"] else ""),
                                    S["cell"]),
                          Paragraph(_e(u["kind"]), S["cell"]), Paragraph(_e(u["need"]), S["val"])])
        out += [CondPageBreak(40 * mm), Paragraph(f"Fiyatı eksik kalemler ({v['unpriced_count']})", S["h3"]),
                c.table(udata, [150, 35, 82])]
        if len(un) > COST_UNPRICED_MAX:
            out.append(Paragraph(c.grey(f"+{len(un) - COST_UNPRICED_MAX} kalem daha (Excel “Ürün maliyeti” "
                                        "sayfasında tam liste)."), S["note"]))
    out.append(KeepTogether([Paragraph("Notlar", S["h3"])]
                            + [Paragraph("• " + _e(t), S["note"]) for t in v["notes"]]))
    return out


def _value_cell(c: _Ctx, m: dict) -> str:
    """"Değer" hücresi: stoktan (+ yoldan) karşılanan değer, altında gri
    dayanağı ("UMAYCHEM · 4,52 $/kg" / "Fiyat yok") — önizlemeyle aynı."""
    v = cover_value(m)
    t = c.money(v) if v is not None else "—"
    used = (m.get("stock_qty_used") or 0) > 0 or (m.get("open_qty_used") or 0) > 0
    if used:
        t += "<br/>" + _small_grey(c, value_basis_text(m, c.cur))
    if m.get("open_value") is not None:
        t += "<br/>" + _small_grey(c, cover_text(m, c.cur))
    return t


def _sec_notes(c: _Ctx, sec: dict) -> list:
    from reportlab.lib.units import mm
    from reportlab.platypus import CondPageBreak, KeepTogether, Paragraph

    paras = [Paragraph(f"{sec['no']}. {_e(sec['title'])}", c.S["h2"])]
    paras += [Paragraph("• " + c.marks(_e(t)), c.S["note"]) for t in sec["rows"]]
    return [CondPageBreak(55 * mm), KeepTogether(paras)]


# ─── Başlık kutuları ────────────────────────────────────────────────────────

def _warning_text(c: _Ctx, report: dict) -> Optional[str]:
    """Turuncu kutu: kur yedeği / bekletilen kalemler / reçetesiz ürünler /
    yükleyici uyarıları."""
    msgs: List[str] = []
    fx = (report.get("pricing") or {}).get("fx") or {}
    if fx.get("used") and (fx.get("warning") or fx.get("stale")):
        msgs.append("Döviz kuru: " + _e(fx.get("warning") or "güncel kur alınamadı; son bilinen kur kullanıldı.")
                    + " Tutarlar yaklaşıktır.")
    held = report.get("held") or []
    if held:
        msgs.append(f"{len(held)} kalem bekletiliyor ("
                    + ", ".join(_e(h["name"]) for h in held) + "); listeye ve toplama girmedi.")
    recl = [p for p in report.get("products") or []
            if any(x["code"] == "recipeless_product" for x in p.get("cautions") or [])]
    if recl:
        msgs.append("Sistemde reçetesi olmayan ürün: " + ", ".join(_e(p["name"]) for p in recl)
                    + " — malzeme ihtiyacı hesaplanamadı, listeye katılmadı.")
    for w in (report.get("meta") or {}).get("warnings") or []:
        msgs.append(_e(w))
    return "<br/>".join(msgs) if msgs else None


def _summary_text(c: _Ctx, report: dict) -> str:
    pr = report.get("pricing") or {}
    tot = pr.get("totals") or {}
    cnt = pr.get("counts") or {}
    b = c.bold
    lines: List[str] = []
    rp, ru = cnt.get("raw_priced", 0), cnt.get("raw_unpriced", 0)
    pp, pu = cnt.get("packaging_priced", 0), cnt.get("packaging_unpriced", 0)
    lp, lu = cnt.get("label_priced", 0), cnt.get("label_unpriced", 0)
    new_lbl = report.get("labels_new") or []
    manual = report.get("manual_lines") or []
    unpriced_any = ru + pu + lu + len(new_lbl) + len(manual)
    cost = _cost_lines(c, report)
    if not (rp + ru + pp + pu + lp + lu) and not (new_lbl or manual):
        return "<br/>".join([b("Alınacak kalem yok") + " — seçilen ürünler için elimizdeki stok yetiyor."] + cost)
    lines.append(b(f"Fiyatı belli kalemlerin tutarı: {c.money(tot.get('all', 0))}"))
    if rp:
        lines.append(f"• {rp} hammadde fiyat listesinden: {c.money(tot.get('raw', 0))}")
    if ru:
        lines.append(f"• {b(f'{ru} hammaddenin fiyatı yok')} — teklif alınacak")
    for lbl, p_, u_, k in (("Ambalaj", pp, pu, "packaging"), ("Etiket", lp, lu, "label")):
        if not (p_ + u_):
            continue
        if p_ and u_:
            lines.append(f"• {lbl}: {p_ + u_} kalemden {p_} tanesinin fiyatı var ({c.money(tot.get(k, 0))}); "
                         f"kalan {u_} kalem için teklif alınacak.")
        elif p_:
            lines.append(f"• {lbl}: {p_} kalem, {c.money(tot.get(k, 0))}")
        else:
            lines.append(f"• {lbl}ın ({u_} kalem) fiyatı yok; teklif alınacak." if lbl == "Ambalaj"
                         else f"• {lbl}in ({u_} kalem) fiyatı yok; teklif alınacak.")
    extra = []
    if new_lbl:
        extra.append(f"{_e(new_lbl[0].get('title') or 'Yeni etiket')} "
                     f"({tr_num(sum(r['total'] for r in new_lbl))} adet)")
    if manual:
        extra.append(", ".join(_e(m["name"]) for m in manual[:3]) + (" …" if len(manual) > 3 else ""))
    if extra:
        lines.append(f"• {' · '.join(extra)}: fiyat yok, teklif alınacak.")
    if unpriced_any:
        lines.append("Gerçek toplam, fiyatı olmayan kalemler eklenince bundan yüksek olacak.")
    return "<br/>".join(lines + cost)


def _cost_lines(c: _Ctx, report: dict) -> List[str]:
    """Yeşil kutunun maliyet bloğu (net mod): alınacak + stoktan (+ yoldan) =
    planın toplam malzeme maliyeti, sonra uyarılar — önizleme / Excel ile aynı
    metin.  Brüt modda boş."""
    rows = cost_summary_lines(report)
    if not rows:
        return []
    # Kısa tutulur (bileşenler tek satır + kalın toplam + uyarılar): kutu
    # uzarsa ilk tablo sayfa sonunda bölünür
    out = [" · ".join(f"{_e(r['label'])}: {_e(r['text'])}" for r in rows if r["key"] != "plan")]
    out += [c.bold(f"{_e(r['label'])}: {_e(r['text'])}") for r in rows if r["key"] == "plan"]
    cav = cost_summary_caveats(report)
    if cav:
        out.append(c.grey(f"<font size='10.5'>{_e(' '.join(cav))}</font>"))
    return out


# ─── Giriş ──────────────────────────────────────────────────────────────────

def render_pdf(report: dict) -> bytes:
    """Rapor dict'inden yatay A4 "Tedarikçili ve Fiyatlı Alım Listesi" PDF'i."""
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    c = _Ctx(report)
    S = c.S
    meta = report.get("meta") or {}
    title = meta.get("title") or "Satın Alma Planı"
    # Sayfa altındaki başlık "Sayfa N" ile çakışmasın
    foot_title = title if len(title) <= 90 else title[:89] + "…"
    stock_date = _long_date(meta.get("stock_as_of_tr"))

    sub = _e(meta.get("scope_text") or "")
    sub += (f" Fiyatlar {c.bold(CURRENCY_NAME.get(c.cur, c.cur) + f' ({c.sym})')}. "
            + ("Tutar = alınacak × seçilen teklifin birim fiyatı (önce malzeme tercihi ve “tercih edilen” "
               "firma, sonra en ucuz)." if (report.get("pricing") or {}).get("status_used") else
               "Tutar = alınacak × en ucuz birim fiyat."))
    if ((report.get("pricing") or {}).get("cost_summary") or {}).get("applies"):
        sub += (" Stoktan karşılananın değeri = bu planın kullanacağı stok × aynı teklifin bugünkü fiyatı "
                "(yenileme değeri, alış maliyeti değil).")
    story = [Paragraph(_e(title), S["h1"]), Paragraph(sub, S["sub"])]
    warn = _warning_text(c, report)
    if warn:
        story.append(Paragraph(warn, S["warn"]))
    story.append(Paragraph(_summary_text(c, report), S["box"]))

    for sec in sections(report):
        k = sec["key"]
        if k in SECTION_KIND:
            story += _heading(c, sec) + [_material_table(c, sec["rows"], SECTION_KIND[k])]
        elif k == "new_items":
            story += _sec_new_items(c, sec)
        elif k == "suppliers":
            story += _sec_suppliers(c, sec)
        elif k == "candidates":
            story += _sec_candidates(c, sec)
        elif k == "checklist":
            story += _sec_checklist(c, sec)
        elif k == "products":
            story += _sec_products(c, sec)
        elif k == "cost":
            story += _sec_cost(c, report, sec)
        elif k == "sufficient":
            story += _sec_sufficient(c, sec)
        elif k == "notes":
            story += _sec_notes(c, sec)

    def footer(cv, doc):
        cv.saveState()
        cv.setFont(c.font, 10)
        cv.drawString(15 * mm, 9 * mm, f"Minerva 108 · {foot_title} · stoklar {stock_date}")
        cv.drawRightString(282 * mm, 9 * mm, f"Sayfa {doc.page}")
        cv.restoreState()

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=12 * mm, bottomMargin=15 * mm, title=title, author="Minerva 108")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
