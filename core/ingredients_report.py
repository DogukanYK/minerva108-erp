# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
İçindekiler Raporu — bitmiş ürünlerin reçete bileşimini dosyalanabilir rapora çevirir.

  list_report_products(db, domain) -> [dict]   seçim paneli verisi (marka + reçete durumu)
  assemble(db, item_ids, domain)   -> dict     rapor verisi (Hammadde/Ambalaj ayrımıyla)
  build_workbook(data)             -> bytes    4 sayfalık, A4-yazdırılabilir .xlsx
  render_pdf(data)                 -> bytes    antetli A4 PDF (ürün başına blok)

Excel sayfaları HER ZAMAN 4 tanedir (boşsa yalnız başlık satırı) ve veri sayfaları
%100 düzdür (birleşik hücre yok) — makinece işlenebilirlik için sayfa adları ve
sütun konumları deterministiktir. Rapor metadatası baskı üstbilgi/altbilgisindedir.

Maliyet BİLİNÇLİ olarak yoktur — bu rapor içerik dokümantasyonudur; finance
gating gerektirmez, `recipes:view` yeter (bileşim yine de ticari sırdır).
"""
from datetime import datetime

from sqlalchemy.orm import Session

from database import Item, Recipe, to_tr
from core.production_sim import brand_of
from core.items_report import tr_key
from core.supplier_prices import normalize as _tr_fold


def _canonical_brands(names: list) -> dict:
    """Marka yazım varyantlarını birleştirir: TR-katlanmış anahtar → en yaygın yazım.

    Ürün adları elle girildiğinden aynı marka 'Minerva' / 'MİNERVA' gibi
    varyantlarla yaşıyor (prod'da gerçek durum). Chip/gruplama tek markada
    toplanmalı; görüntü olarak en çok kullanılan yazım kazanır.
    """
    counts: dict = {}
    for nm in names:
        b = brand_of(nm)
        counts.setdefault(_tr_fold(b), {}).setdefault(b, 0)
        counts[_tr_fold(b)][b] += 1
    return {k: max(v, key=v.get) for k, v in counts.items()}


def _brand(name: str, canon: dict) -> str:
    return canon.get(_tr_fold(brand_of(name)), brand_of(name))


def report_filename(ext: str = "xlsx") -> str:
    """Saf ASCII dosya adı — Content-Disposition header'ına doğrudan girebilir."""
    return f"icindekiler_raporu_{datetime.utcnow():%Y%m%d}.{ext}"


def _abstract_parent_ids(db: Session) -> set:
    """Varyasyon ebeveyni (soyut) ürün id'leri — reçete taşıyamazlar."""
    return {pid for (pid,) in
            db.query(Item.parent_id).filter(Item.parent_id.isnot(None)).distinct()
            if pid}


def _recipe_map(db: Session, item_ids, domain: str) -> dict:
    """target_item_id -> aktif Recipe (aynı ürüne birden çoksa en yenisi kazanır)."""
    q = (db.query(Recipe)
         .filter(Recipe.is_active == True,          # noqa: E712
                 Recipe.domain == domain,
                 Recipe.target_item_id.in_(item_ids))
         .order_by(Recipe.id.asc()))
    return {r.target_item_id: r for r in q}


def _finished_items(db: Session, domain: str, item_ids=None):
    """Aktif paneldeki somut (soyut-ebeveyn olmayan) bitmiş ürünler."""
    parents = _abstract_parent_ids(db)
    q = (db.query(Item)
         .filter(Item.is_active == True,            # noqa: E712
                 Item.domain == domain,
                 Item.category == "Bitmiş Ürün"))
    if item_ids is not None:
        q = q.filter(Item.id.in_(item_ids))
    return [it for it in q.all() if it.id not in parents]


def list_report_products(db: Session, domain: str) -> list:
    """Seçim paneli: marka + reçete durumu ile tüm somut bitmiş ürünler."""
    items = _finished_items(db, domain)
    rmap = _recipe_map(db, [it.id for it in items], domain) if items else {}
    canon = _canonical_brands([it.name for it in items])
    parent_names = {}
    for it in items:
        if it.parent_id and it.parent_id not in parent_names:
            p = db.query(Item).filter(Item.id == it.parent_id).first()
            parent_names[it.parent_id] = p.name if p else ""
    out = []
    for it in items:
        rec = rmap.get(it.id)
        out.append({
            "item_id":        it.id,
            "name":           it.name,
            "name_tr":        it.name_tr or "",
            "sku":            it.sku or "",
            "brand":          _brand(it.name, canon),
            "unit":           it.unit or "adet",
            "barcode":        it.barcode or "",
            "variation_name": it.variation_name or "",
            "parent_name":    parent_names.get(it.parent_id, "") if it.parent_id else "",
            "has_recipe":     rec is not None,
            "recipe_id":      rec.id if rec else None,
        })
    out.sort(key=lambda x: (tr_key(x["brand"]), tr_key(x["name"])))
    return out


def assemble(db: Session, item_ids: list, domain: str) -> dict:
    """Seçilen ürünlerin içindekiler verisini toplar.

    Domain dışı / bitmiş-ürün olmayan / pasif id'ler sessizce düşer — domain
    izolasyonunun garantisi budur. Reçetesi olmayan ürünler `recipeless`e gider.
    """
    items = _finished_items(db, domain, item_ids=item_ids)
    rmap = _recipe_map(db, [it.id for it in items], domain) if items else {}
    canon = _canonical_brands([it.name for it in items])

    products, raw_rows, pkg_rows, recipeless = [], [], [], []
    for it in items:
        rec = rmap.get(it.id)
        base = {
            "brand":          _brand(it.name, canon),
            "product_name":   it.name,
            "product_name_tr": it.name_tr or "",
            "product_sku":    it.sku or "",
            "variation":      it.variation_name or "",
        }
        if not rec:
            recipeless.append({**base,
                               "barcode": it.barcode or "",
                               "unit":    it.unit or "adet"})
            continue

        waste_pct = float(rec.waste_percentage or 0.0)
        waste_factor = 1.0 + waste_pct / 100.0
        raws, pkgs = [], []
        for ing in rec.ingredients:
            item = ing.item
            if not item:
                continue
            entry = {
                **base,
                "recipe_id":   rec.id,
                "ing_name":    item.name,
                "ing_name_tr": item.name_tr or "",
                "ing_sku":     item.sku or "",
                "net":         float(ing.quantity or 0.0),
                "unit":        ing.unit or item.unit or "",
            }
            if item.category == "Ambalaj":   # fire muaf — brüt == net
                pkgs.append({**entry,
                             "pkg_type":    item.pkg_type or "",
                             "language":    item.language or "",
                             "label_group": item.label_group or ""})
            else:
                entry["phase"] = ing.phase or ""
                entry["waste_pct"] = waste_pct
                entry["gross"] = round(entry["net"] * waste_factor, 6)
                raws.append(entry)

        raw_total = sum(r["net"] for r in raws)
        for r in raws:
            r["pct"] = round(r["net"] / raw_total * 100.0, 4) if raw_total else 0.0
        # İçindekiler konvansiyonu: en yüksek paydan aşağı (etiket INCI düzeni gibi)
        raws.sort(key=lambda r: (-r["pct"], tr_key(r["ing_name"])))
        pkgs.sort(key=lambda r: (r["pkg_type"] or "~", tr_key(r["ing_name"])))

        products.append({
            **base,
            "barcode":        it.barcode or "",
            "recipe_id":      rec.id,
            "output_quantity": float(rec.output_quantity or 1.0),
            "output_unit":    rec.output_unit or "adet",
            "waste_pct":      waste_pct,
            "raw_count":      len(raws),
            "pkg_count":      len(pkgs),
            "raw_total":      round(raw_total, 6),
        })
        raw_rows.extend(raws)
        pkg_rows.extend(pkgs)

    # Satırlar ürün içinde sıralı (yukarıda); ürün blokları products sırasıyla
    # gezildiği için raw_rows/pkg_rows'a ayrıca global sıralama gerekmez.
    products.sort(key=lambda x: (tr_key(x["brand"]), tr_key(x["product_name"])))
    recipeless.sort(key=lambda x: (tr_key(x["brand"]), tr_key(x["product_name"])))

    return {
        "generated_at": to_tr(datetime.utcnow()).strftime("%d.%m.%Y %H:%M"),
        "domain":       domain,
        "products":     products,
        "raw_rows":     raw_rows,
        "pkg_rows":     pkg_rows,
        "recipeless":   recipeless,
        "summary": {
            "product_count":    len(products),
            "recipeless_count": len(recipeless),
            "raw_line_count":   len(raw_rows),
            "pkg_line_count":   len(pkg_rows),
        },
    }


# ─── Excel ───────────────────────────────────────────────────────────────────

def _setup_a4(ws, *, landscape: bool = True, title: str = "") -> None:
    """Sayfayı A4 baskıya hazırlar — repo'daki ilk Excel print-setup kullanımı.

    fitToWidth tek başına yetmez: sheet_properties.pageSetUpPr.fitToPage
    işaretlenmezse Excel fitTo* değerlerini yok sayar.
    """
    from openpyxl.worksheet.page import PageMargins
    from openpyxl.worksheet.properties import PageSetupProperties

    ws.page_setup.paperSize = ws.PAPERSIZE_A4              # == 9
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0                          # boyuna serbest sayfa
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.print_title_rows = "1:2"                            # rapor bandı her baskı sayfasında
    ws.page_margins = PageMargins(left=0.4, right=0.4, top=0.7, bottom=0.6,
                                  header=0.3, footer=0.3)
    ws.oddHeader.center.text = f"Minerva 108 — İçindekiler Raporu · {title}"
    ws.oddHeader.center.size = 8
    ws.oddFooter.left.text = "&D &T"
    ws.oddFooter.right.text = "Sayfa &P / &N"


_SHEET_RAW = "Hammaddeler"
_SHEET_PKG = "Ambalaj-Paketleme"
_SHEET_SUMMARY = "Özet"
_SHEET_RECIPELESS = "Reçetesiz Ürünler"

# Blok sayfaları: her ürün kendi bandı + tablosu + TOPLAM satırıyla ayrı bölüm.
_HDR_RAW = ["Faz", "Hammadde (EN)", "Hammadde (TR)", "SKU", "Net Miktar",
            "Fire %", "Brüt Miktar", "Birim", "Bileşim %"]
_W_RAW = [7, 36, 32, 15, 12, 8, 12, 8, 11]
_HDR_PKG = ["Bileşen (EN)", "Bileşen (TR)", "SKU", "Ambalaj Tipi",
            "Etiket Dili", "Etiket Grubu", "Miktar", "Birim"]
_W_PKG = [36, 32, 15, 13, 11, 18, 10, 8]
_HDR_SUMMARY = ["Marka", "Ürün (EN)", "Ürün (TR)", "SKU", "Varyasyon", "Barkod",
                "Reçete ID", "Batch Çıktısı", "Çıktı Birimi", "Fire %",
                "Hammadde Kalemi", "Ambalaj Kalemi", "Toplam Hammadde (Net)"]
_W_SUMMARY = [13, 36, 32, 13, 11, 15, 9, 12, 10, 8, 9, 9, 14]
_HDR_RECIPELESS = ["Marka", "Ürün (EN)", "Ürün (TR)", "SKU", "Varyasyon", "Barkod", "Birim"]
_W_RECIPELESS = [14, 38, 34, 14, 12, 16, 9]

_FMT_QTY, _FMT_PCT = "#,##0.####", "0.00"


def build_workbook(data: dict) -> bytes:
    """A4-yazdırılabilir, ürün-blok düzenli içindekiler Excel'i.

    Hammaddeler/Ambalaj sayfalarında HER ÜRÜN ayrı bölümdür: gold marka bandı →
    künye satırı → içerik tablosu (% azalan) → TOPLAM. Özet/Reçetesiz düz tablodur.
    """
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)
    wb.properties.title = "Minerva 108 — İçindekiler Raporu"
    wb.properties.creator = "Minerva 108 ERP"

    # Marka kimliği: navy + gold + krem (uygulama temasıyla aynı)
    NAVY, GOLD, GOLD_SOFT = "232E6E", "C9AC6F", "F3EAD3"
    CREAM, GREY_TX = "F5F0E8", "6B7280"
    F_TITLE = Font(bold=True, color="FFFFFF", size=12)
    F_META = Font(color=GREY_TX, size=9, italic=True)
    F_HEAD = Font(bold=True, color="FFFFFF", size=9)
    F_BAND = Font(bold=True, color=NAVY, size=10.5)
    F_CELL = Font(color="374151", size=9.5)
    F_TOTAL = Font(bold=True, color=NAVY, size=9.5)
    FILL_NAVY = PatternFill("solid", fgColor=NAVY)
    FILL_GOLD = PatternFill("solid", fgColor=GOLD)
    FILL_SOFT = PatternFill("solid", fgColor=GOLD_SOFT)
    FILL_CREAM = PatternFill("solid", fgColor=CREAM)
    THIN = Border(*([Side(style="thin", color="DDDDDD")] * 4))
    AL_L = Alignment(horizontal="left", vertical="center")
    AL_C = Alignment(horizontal="center", vertical="center")
    AL_R = Alignment(horizontal="right", vertical="center")

    sm = data["summary"]
    meta_txt = (f"Oluşturma: {data.get('generated_at') or ''} · {sm['product_count']} ürün"
                + (f" · {sm['recipeless_count']} reçetesiz" if sm["recipeless_count"] else ""))

    def _cell(ws, r, c, v, *, font=F_CELL, fill=None, align=AL_L, fmt=None, border=THIN):
        cell = ws.cell(row=r, column=c, value=v)
        cell.font = font
        if fill:
            cell.fill = fill
        cell.alignment = align
        if fmt:
            cell.number_format = fmt
        if border:
            cell.border = border
        return cell

    def _new_sheet(title, widths, *, landscape=True, label=None):
        """Sayfa + 2 satırlık rapor bandı (baskıda her sayfada tekrar eder)."""
        ws = wb.create_sheet(title=title[:31])  # Excel sayfa adı ≤ 31 karakter
        n = len(widths)
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n)
        _cell(ws, 1, 1, f"MİNERVA 108 — İÇİNDEKİLER RAPORU · {(label or title).upper()}",
              font=F_TITLE, fill=FILL_NAVY, border=None)
        ws.row_dimensions[1].height = 24
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=n)
        _cell(ws, 2, 1, meta_txt, font=F_META, fill=FILL_CREAM, border=None)
        _setup_a4(ws, landscape=landscape, title=title)
        return ws

    def _header_row(ws, r, headers):
        for c, h in enumerate(headers, 1):
            _cell(ws, r, c, h, font=F_HEAD, fill=FILL_NAVY, align=AL_C)
        ws.row_dimensions[r].height = 16

    def _band(ws, r, ncols, p):
        """Ürün bandı (gold) + künye satırı (krem)."""
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncols)
        name = f"{p['brand']} — {p['product_name']}"
        if p["product_name_tr"]:
            name += f"  /  {p['product_name_tr']}"
        _cell(ws, r, 1, name, font=F_BAND, fill=FILL_GOLD, border=None)
        ws.row_dimensions[r].height = 20
        ws.merge_cells(start_row=r + 1, start_column=1, end_row=r + 1, end_column=ncols)
        kunye = (f"Barkod: {p['barcode'] or '—'} · Varyasyon: {p['variation'] or '—'}"
                 f" · SKU: {p['product_sku'] or '—'}"
                 f" · Batch: {p['output_quantity']:g} {p['output_unit']}"
                 f" · Fire: %{p['waste_pct']:g} · Reçete #{p['recipe_id']}")
        _cell(ws, r + 1, 1, kunye, font=F_META, fill=FILL_CREAM, border=None)
        return r + 2

    by_recipe_raw, by_recipe_pkg = {}, {}
    for x in data["raw_rows"]:
        by_recipe_raw.setdefault(x["recipe_id"], []).append(x)
    for x in data["pkg_rows"]:
        by_recipe_pkg.setdefault(x["recipe_id"], []).append(x)

    # ── Özet (düz tablo) ──
    ws = _new_sheet(_SHEET_SUMMARY, _W_SUMMARY)
    _header_row(ws, 4, _HDR_SUMMARY)
    r = 5
    for i, p in enumerate(data["products"]):
        zebra = FILL_CREAM if i % 2 else None
        vals = [p["brand"], p["product_name"], p["product_name_tr"], p["product_sku"],
                p["variation"], p["barcode"], p["recipe_id"], p["output_quantity"],
                p["output_unit"], p["waste_pct"], p["raw_count"], p["pkg_count"],
                p["raw_total"]]
        for c, v in enumerate(vals, 1):
            fmt = {8: _FMT_QTY, 10: _FMT_PCT, 13: _FMT_QTY}.get(c)
            _cell(ws, r, c, v, fill=zebra, fmt=fmt,
                  align=AL_R if c in (7, 8, 10, 11, 12, 13) else AL_L)
        r += 1
    if not data["products"]:
        _cell(ws, 5, 1, "Kayıt yok.", font=F_META, border=None)
    ws.freeze_panes = "A5"

    # ── Hammaddeler (ürün blokları) ──
    ws = _new_sheet(_SHEET_RAW, _W_RAW)
    r = 4
    for p in data["products"]:
        raws = by_recipe_raw.get(p["recipe_id"], [])
        r = _band(ws, r, len(_HDR_RAW), p)
        _header_row(ws, r, _HDR_RAW)
        r += 1
        if not raws:
            _cell(ws, r, 1, "Bu reçetede hammadde satırı yok.", font=F_META, border=None)
            r += 2
            continue
        for i, x in enumerate(raws):
            zebra = FILL_CREAM if i % 2 else None
            _cell(ws, r, 1, x["phase"] or "—", fill=zebra, align=AL_C)
            _cell(ws, r, 2, x["ing_name"], fill=zebra)
            _cell(ws, r, 3, x["ing_name_tr"], fill=zebra)
            _cell(ws, r, 4, x["ing_sku"], fill=zebra)
            _cell(ws, r, 5, x["net"], fill=zebra, align=AL_R, fmt=_FMT_QTY)
            _cell(ws, r, 6, x["waste_pct"], fill=zebra, align=AL_R, fmt=_FMT_PCT)
            _cell(ws, r, 7, x["gross"], fill=zebra, align=AL_R, fmt=_FMT_QTY)
            _cell(ws, r, 8, x["unit"], fill=zebra, align=AL_C)
            _cell(ws, r, 9, x["pct"], fill=zebra, align=AL_R, fmt=_FMT_PCT)
            r += 1
        _cell(ws, r, 1, "", fill=FILL_SOFT)
        _cell(ws, r, 2, "TOPLAM", font=F_TOTAL, fill=FILL_SOFT)
        _cell(ws, r, 3, "", fill=FILL_SOFT)
        _cell(ws, r, 4, "", fill=FILL_SOFT)
        _cell(ws, r, 5, round(sum(x["net"] for x in raws), 6),
              font=F_TOTAL, fill=FILL_SOFT, align=AL_R, fmt=_FMT_QTY)
        _cell(ws, r, 6, "", fill=FILL_SOFT)
        _cell(ws, r, 7, round(sum(x["gross"] for x in raws), 6),
              font=F_TOTAL, fill=FILL_SOFT, align=AL_R, fmt=_FMT_QTY)
        _cell(ws, r, 8, "", fill=FILL_SOFT)
        _cell(ws, r, 9, round(sum(x["pct"] for x in raws), 2),
              font=F_TOTAL, fill=FILL_SOFT, align=AL_R, fmt=_FMT_PCT)
        r += 2                                              # blok arası boşluk
    if not data["products"]:
        _cell(ws, 4, 1, "Kayıt yok.", font=F_META, border=None)
    ws.freeze_panes = "A3"

    # ── Ambalaj-Paketleme (ürün blokları) ──
    ws = _new_sheet(_SHEET_PKG, _W_PKG)
    r = 4
    for p in data["products"]:
        pkgs = by_recipe_pkg.get(p["recipe_id"], [])
        r = _band(ws, r, len(_HDR_PKG), p)
        _header_row(ws, r, _HDR_PKG)
        r += 1
        if not pkgs:
            _cell(ws, r, 1, "Bu reçetede ambalaj satırı yok.", font=F_META, border=None)
            r += 2
            continue
        for i, x in enumerate(pkgs):
            zebra = FILL_CREAM if i % 2 else None
            _cell(ws, r, 1, x["ing_name"], fill=zebra)
            _cell(ws, r, 2, x["ing_name_tr"], fill=zebra)
            _cell(ws, r, 3, x["ing_sku"], fill=zebra)
            _cell(ws, r, 4, x["pkg_type"], fill=zebra, align=AL_C)
            _cell(ws, r, 5, x["language"], fill=zebra, align=AL_C)
            _cell(ws, r, 6, x["label_group"], fill=zebra)
            _cell(ws, r, 7, x["net"], fill=zebra, align=AL_R, fmt=_FMT_QTY)
            _cell(ws, r, 8, x["unit"], fill=zebra, align=AL_C)
            r += 1
        r += 1                                              # blok arası boşluk
    if not data["products"]:
        _cell(ws, 4, 1, "Kayıt yok.", font=F_META, border=None)
    ws.freeze_panes = "A3"

    # ── Reçetesiz Ürünler (düz tablo) ──
    ws = _new_sheet(_SHEET_RECIPELESS, _W_RECIPELESS, landscape=False)
    _header_row(ws, 4, _HDR_RECIPELESS)
    r = 5
    for i, p in enumerate(data["recipeless"]):
        zebra = FILL_CREAM if i % 2 else None
        vals = [p["brand"], p["product_name"], p["product_name_tr"], p["product_sku"],
                p["variation"], p["barcode"], p["unit"]]
        for c, v in enumerate(vals, 1):
            _cell(ws, r, c, v, fill=zebra)
        r += 1
    if not data["recipeless"]:
        _cell(ws, 5, 1, "Kayıt yok — seçilen tüm ürünlerin reçetesi var.",
              font=F_META, border=None)
    ws.freeze_panes = "A5"

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ─── PDF (antetli A4) ────────────────────────────────────────────────────────

def _report_story(data: dict, s: float = 1.0):
    from html import escape

    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, Spacer

    from core.delivery_note import _fmt
    from core.shipment_note import _grid, _styles

    st = _styles(s)
    W = 174.0

    def pct(*vals):
        return [W * v / 100.0 * mm for v in vals]

    sm = data["summary"]
    meta = (f"Tarih: {escape(str(data.get('generated_at') or ''))}"
            f" · {sm['product_count']} ürün"
            + (f" · {sm['recipeless_count']} reçetesiz" if sm["recipeless_count"] else ""))
    story = [Paragraph("İÇİNDEKİLER RAPORU", st["h1"]),
             Paragraph(meta, st["meta"]),
             Spacer(1, 6 * s)]

    if not data["products"] and not data["recipeless"]:
        story.append(Paragraph("Seçimle eşleşen ürün yok.", st["cell"]))
        return story

    raw_by, pkg_by = {}, {}
    for r in data["raw_rows"]:
        raw_by.setdefault(r["recipe_id"], []).append(r)
    for r in data["pkg_rows"]:
        pkg_by.setdefault(r["recipe_id"], []).append(r)

    for p in data["products"]:
        title = f"{p['brand']} — {p['product_name']}"
        if p["product_name_tr"]:
            title += f" / {p['product_name_tr']}"
        head_meta = (f"Barkod: {p['barcode'] or '—'} · Varyasyon: {p['variation'] or '—'}"
                     f" · Batch: {_fmt(p['output_quantity'])} {p['output_unit']}"
                     f" · Fire: %{_fmt(p['waste_pct'])}")
        # Başlık + meta sayfa dibinde öksüz kalmasın; tablolar serbest bölünür.
        story.append(KeepTogether([Paragraph(escape(title), st["h2"]),
                                   Paragraph(escape(head_meta), st["meta"])]))

        raws = raw_by.get(p["recipe_id"], [])
        if raws:
            story.append(Spacer(1, 3 * s))
            story.append(_grid(
                st, ["Faz", "Hammadde", "Türkçe Ad", "Net", "Brüt", "Birim", "Bileşim %"],
                [[r["phase"] or "—", r["ing_name"], r["ing_name_tr"] or "—",
                  _fmt(r["net"]), _fmt(r["gross"]), r["unit"], f"{r['pct']:.2f}"]
                 for r in raws],
                pct(8, 30, 24, 10, 10, 8, 10), s))

        pkgs = pkg_by.get(p["recipe_id"], [])
        if pkgs:
            story.append(Spacer(1, 4 * s))
            story.append(_grid(
                st, ["Ambalaj / Bileşen", "Tip", "Dil", "Miktar", "Birim"],
                [[r["ing_name"], r["pkg_type"] or "—", r["language"] or "—",
                  _fmt(r["net"]), r["unit"]]
                 for r in pkgs],
                pct(46, 16, 10, 14, 14), s))
        story.append(Spacer(1, 8 * s))

    if data["recipeless"]:
        story.append(Paragraph("Reçetesiz Ürünler", st["h2"]))
        story.append(Paragraph("Bu ürünlerin sistemde aktif reçetesi yok — içerik dökümü verilemedi.",
                               st["meta"]))
        story.append(Spacer(1, 3 * s))
        story.append(_grid(
            st, ["Marka", "Ürün", "Türkçe Ad", "Varyasyon", "Barkod"],
            [[r["brand"], r["product_name"], r["product_name_tr"] or "—",
              r["variation"] or "—", r["barcode"] or "—"]
             for r in data["recipeless"]],
            pct(14, 30, 26, 14, 16), s))
    return story


def _merge_letterhead_shared(content: bytes) -> bytes:
    """Çok sayfalı rapor için verimli antet: delivery_note.merge_letterhead her
    sayfada antedi YENİDEN okuyup gömer (~830 KB × sayfa → 30 MB'lık dosyalar,
    yaşandı). Burada antet TEK nesne olarak okunur, her içerik sayfasının ALTINA
    (over=False) damgalanır — görüntü XObject'i dosyaya bir kez yazılır.
    Antetli okunamzsa içerik antetsiz döner (merge_letterhead ile aynı politika)."""
    try:
        from io import BytesIO
        from pypdf import PdfReader, PdfWriter
        from core.delivery_note import LETTERHEAD, _A4_PT

        lh = PdfReader(LETTERHEAD).pages[0]
        lh.scale_to(*_A4_PT)                     # US Letter antet → A4 (HANDOFF #2)
        writer = PdfWriter()
        writer.append(PdfReader(BytesIO(content)))
        for pg in writer.pages:
            pg.merge_page(lh, over=False)        # antet içeriğin ALTINA
        writer.compress_identical_objects(remove_identicals=True, remove_orphans=True)
        buf = BytesIO()
        writer.write(buf)
        return buf.getvalue()
    except Exception:
        return content


def render_pdf(data: dict) -> bytes:
    from reportlab.lib.units import mm

    from core.delivery_note import render_autofit

    # Çok ürünlü rapor tek sayfaya zaten sığmaz — autofit'in 4 ölçek denemesi
    # koca dokümanı 5 kez kurup "hazırlanıyor"da bekletiyordu (yaşandı).
    # 1-2 üründe tek-sayfa cilası kalsın; fazlasında tek build → doğrudan çok sayfa.
    few = (len(data["products"]) + len(data["recipeless"])) <= 2
    # Alt marj 50mm: bu antetin 4 satırlık footer bloğu ~48mm'ye çıkıyor (raster
    # doğrulamayla ölçüldü); HANDOFF #2'nin 40mm tabanı burada tabloya değiyordu.
    content = render_autofit(
        lambda s: _report_story(data, s),
        margins=(20 * mm, 20 * mm, 48 * mm, 50 * mm),
        doc_kwargs={"title": "İçindekiler Raporu", "author": "Minerva 108"},
        steps=(0.96, 0.92, 0.88, 0.85) if few else ())
    return _merge_letterhead_shared(content)
