# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""B2B iç teknik föy — sipariş başına İÇ belge (spec §2; MÜŞTERİYE GÖNDERİLMEZ).

Müşteri proformasından ayrı: sipariş satırlarının stoktan / üretimden
karşılanması, ONAYLANAN reçete bileşimi (teknik değerlendirme anındaki
snapshot; eski sürümlerde güncel reçete, notla), malzeme ihtiyacı ve eksikler,
alım satırları, üretim partileri + kaynak lot / tedarikçi tüketimi, onay durumu.

Satış fiyatı, banka ve ödeme TUTARI hiç basılmaz.  Malzeme alım fiyatı / tutarı
yalnız `b2b.view` sahibine (çağıran `show_prices` ile söyler — `order_detail`
zaten teknik görünümde bu alanları siler).
"""
import json
from html import escape
from io import BytesIO

from core import b2b_orders as b2b
from database import B2BOrderBatch, Item, Recipe, tr_now

STATUS = {"SUBMITTED": "Teknik değerlendirme bekliyor", "TECH_REVIEWED": "Yönetim onayı bekliyor",
          "APPROVED": "Onaylı — ödeme / hazırlık / üretim", "SHIPPED": "Sevk edildi",
          "CANCELLED": "İptal edildi"}
BATCH = {"STARTED": "Başladı (malzeme düştü)", "COMPLETED": "Tamamlandı", "CANCELLED": "İptal"}
PURCHASE = {"open": "Açık", "ordered": "Sipariş verildi", "received": "Teslim alındı", "cancelled": "İptal"}
KIND = {"raw": "Hammadde", "packaging": "Ambalaj", "label": "Etiket"}


def technical_sheet_filename(reference) -> str:
    from core.delivery_note import slug_part
    return f"Teknik_Foy_{slug_part(reference) or 'siparis'}.pdf"


def sheet_doc(db, order, detail, *, show_prices, generated_by=""):
    """order_detail (yetkiye göre süzülmüş) + teknik snapshot reçeteleri +
    parti tüketim satırları → çizici sözleşmesi."""
    _, tech = b2b._revision(db, order, "technical")
    recipes = []
    for line in (tech or {}).get("lines", []):
        if not line.get("recipe_id") or (line.get("produce") or 0) <= b2b.EPS:
            continue
        view, note = line.get("recipe"), ""
        if not view:                                       # 09.10.2026 öncesi teknik sürüm
            rec = db.query(Recipe).filter(Recipe.id == line["recipe_id"]).first()
            view = b2b.recipe_view(db, rec) if rec else None
            note = "Teknik değerlendirme anındaki bileşim kaydedilmemiş — güncel reçete gösteriliyor."
        if b2b._recipe_changed(db, line):
            note = (note + " " if note else "") + ("DİKKAT: reçete teknik değerlendirmeden sonra değişti; "
                                                   "parti başlatılamaz, değerlendirme yenilenmeli.")
        recipes.append({"product": line["name"], "produce": line["produce"], "unit": line.get("unit"),
                        "recipe": view, "note": note})
    names = {}
    batches = []
    for b in db.query(B2BOrderBatch).filter_by(order_id=order.id).order_by(B2BOrderBatch.id).all():
        snap = json.loads(b.plan_snapshot or "{}")
        returned = next((x for x in detail.get("batches", []) if x["id"] == b.id), {})
        ret = {c["item_id"]: c.get("returned") or 0 for c in returned.get("consumed", [])}
        batches.append({
            "lot_number": b.lot_number, "status": BATCH.get(b.status, b.status), "item_id": b.item_id,
            "recipe_name": snap.get("recipe_name") or "", "planned": b.planned_quantity,
            "produced": b.produced_quantity, "witness": b.witness_quantity,
            "started": f"{b.started_by or ''} · {b2b._when(b.started_at)}",
            "completed": f"{b.completed_by or ''} · {b2b._when(b.completed_at)}" if b.completed_at else "",
            "cancel_reason": b.cancel_reason or "",
            "rows": [{"item_id": r["item_id"], "kind": r.get("kind"), "lot_number": r.get("lot_number") or "",
                      "supplier": r.get("supplier_name") or "", "quantity": r.get("quantity"),
                      "unit": r.get("unit") or "", "phase": r.get("phase") or ""} for r in snap.get("rows", [])],
            "returned": ret,
        })
    if batches:
        ids = {b["item_id"] for b in batches} | {r["item_id"] for b in batches for r in b["rows"]}
        names = {i.id: i.name for i in db.query(Item).filter(Item.id.in_(list(ids)))}
    o = detail["order"]
    return {
        "reference": o.get("reference"), "status": STATUS.get(o.get("status"), o.get("status")),
        "customer": detail.get("customer") or {}, "label_language": o.get("label_language"),
        "target_date": o.get("target_date"), "commercial_revision": o.get("commercial_revision"),
        "technical_revision": o.get("technical_revision"),
        "people": {"owner": o.get("owner"), "technical": o.get("technical_user"), "signer": o.get("signer")},
        "lines": detail.get("lines") or [], "technical": detail.get("technical"),
        "recipes": recipes, "purchase_lines": detail.get("purchase_lines") or [],
        "batches": batches, "names": names, "signature": detail.get("signature"),
        "payment": {k: (detail.get("payment") or {}).get(k) for k in ("production_ok", "shipment_ok")},
        "prep": {"at": o.get("prep_confirmed_at"), "by": o.get("prep_confirmed_by")},
        "show_prices": bool(show_prices),
        "generated": f"{tr_now().strftime('%d.%m.%Y %H:%M')} · {generated_by}".strip(" ·"),
    }


def _mm(*cols):
    """Sütun genişlikleri (mm, toplam 182 = A4 − 2×14 mm) → oran."""
    total = float(sum(cols))
    return [c / total for c in cols]


def _num(v, digits=3):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—" if v in (None, "") else str(v)
    if abs(f - round(f)) < 1e-9:
        return f"{int(round(f)):,}".replace(",", ".")
    return f"{f:,.{digits}f}".rstrip("0").rstrip(".").replace(",", "X").replace(".", ",").replace("X", ".")


def render_technical_sheet(doc: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from core.monthly_report import _register_fonts

    font, bold = _register_fonts()
    NAVY, GREY, RED, LINE = (colors.HexColor("#1f2d52"), colors.HexColor("#6b7280"),
                             colors.HexColor("#b91c1c"), colors.HexColor("#d1d5db"))
    HEAD = colors.HexColor("#eef0f5")

    def st(name, size, *, b=False, color=colors.black, align=0):
        return ParagraphStyle(name, fontName=bold if b else font, fontSize=size, leading=size * 1.25,
                              textColor=color, alignment=align)
    p_title, p_h, p_txt = st("t", 15, b=True, color=NAVY), st("h", 11, b=True, color=NAVY), st("x", 8.6)
    p_small, p_warn = st("s", 7.6, color=GREY), st("w", 8.6, color=RED)
    p_th, p_td, p_tdr = st("th", 7.8, b=True), st("td", 8.2), st("tdr", 8.2, align=2)
    W = A4[0] - 28 * mm

    def esc(x):
        return escape(str(x if x is not None else ""))

    def table(head, rows, widths, right=()):
        data = [[Paragraph(esc(h), p_th) for h in head]]
        for r in rows:
            data.append([Paragraph(esc(c), p_tdr if i in right else p_td) for i, c in enumerate(r)])
        t = Table(data, colWidths=[w * W for w in widths], repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), HEAD), ("GRID", (0, 0), (-1, -1), 0.4, LINE),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
                               ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
        return t

    story = [Paragraph("İÇ BELGE — müşteriye gönderilmez", st("ib", 9, b=True, color=RED)),
             Paragraph(f"B2B Sipariş Teknik Föyü · {esc(doc['reference'])}", p_title), Spacer(1, 2 * mm)]
    cust = doc["customer"]
    meta = [["Müşteri", f"{cust.get('name') or ''} · {cust.get('country') or ''}".strip(" ·"),
             "Durum", doc["status"]],
            ["Etiket dili", "İngilizce" if doc["label_language"] == "EN" else "Türkçe",
             "Hedef tarih", doc["target_date"] or "—"],
            ["Sürüm", f"Ticari r{doc['commercial_revision']} · Teknik r{doc['technical_revision'] or 0}",
             "Sorumlular", f"Sipariş {doc['people']['owner'] or '—'} · teknik {doc['people']['technical'] or '—'} "
                           f"· imza {doc['people']['signer'] or '—'}"]]
    mt = Table([[Paragraph(esc(c), p_th if i % 2 == 0 else p_txt) for i, c in enumerate(r)] for r in meta],
               colWidths=[0.13 * W, 0.37 * W, 0.13 * W, 0.37 * W])
    mt.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, LINE), ("BACKGROUND", (0, 0), (0, -1), HEAD),
                            ("BACKGROUND", (2, 0), (2, -1), HEAD), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [mt, Spacer(1, 4 * mm)]

    # 1. Satırlar
    story.append(Paragraph("1. Sipariş satırları — stoktan / üretim", p_h))
    rows = [[l["name"], f"{_num(l['quantity'])} {l.get('unit') or ''}", _num(l.get("use_stock")),
             _num(l.get("produce")), (l.get("recipe_name") or "—") + (" (DEĞİŞTİ)" if l.get("recipe_changed") else ""),
             _num(l.get("released_stock")), _num(l.get("customer_quantity")), _num(l.get("remaining_to_start"))]
            for l in doc["lines"]]
    story += [table(["Ürün", "Sipariş", "Stoktan", "Üretilecek", "Reçete", "Sevke uygun stok", "Müşteriye üretilen",
                     "Başlatılacak"], rows, _mm(37, 17, 15, 19, 34, 19, 19, 22), right=(2, 3, 5, 6, 7)),
              Spacer(1, 4 * mm)]

    # 2. Onaylı reçete bileşimi
    story.append(Paragraph("2. Onaylanan reçete bileşimi", p_h))
    if not doc["recipes"]:
        story.append(Paragraph("Üretilecek satır yok ya da teknik değerlendirme henüz yapılmadı.", p_small))
    for r in doc["recipes"]:
        v = r["recipe"]
        block = [Paragraph(f"<b>{esc(r['product'])}</b> — {_num(r['produce'])} {esc(r['unit'] or '')} üretilecek · "
                           f"reçete «{esc(v['name'] if v else '—')}»"
                           + (f" · çıktı {_num(v['output_quantity'])} {esc(v['output_unit'])} · fire %{_num(v['waste_percentage'])}"
                              if v else ""), p_txt)]
        if r["note"]:
            block.append(Paragraph(esc(r["note"]), p_warn))
        if v:
            block.append(table(["Malzeme", "Tür", "Faz", "Miktar (çıktı başına)", "Birim"],
                               [[i["name"], i["category"], i["phase"] or "—", _num(i["quantity"], 4), i["unit"]]
                                for i in v["ingredients"]], [0.44, 0.16, 0.08, 0.2, 0.12], right=(3,)))
        story += [KeepTogether(block), Spacer(1, 2.5 * mm)]
    story.append(Spacer(1, 1.5 * mm))

    # 3. Malzeme ihtiyacı (teknik değerlendirme anında)
    tech = doc["technical"]
    story.append(Paragraph("3. Malzeme ihtiyacı ve eksikler (teknik değerlendirme anında)", p_h))
    if tech:
        story.append(Paragraph(f"Değerlendiren: {esc(tech.get('created_by'))} · {esc(tech.get('created_at'))}"
                               + (" · ESKİ (ticari kapsam değişti)" if tech.get("stale") else ""), p_small))
        head = ["Malzeme", "Tür", "İhtiyaç", "Stok", "Alınacak", "Birim", "Tedarikçi"]
        widths = _mm(52, 19, 18, 18, 18, 13, 44)
        right = (2, 3, 4)
        if doc["show_prices"]:
            head += ["Alım tutarı"]
            widths = _mm(46, 19, 16, 16, 17, 12, 34, 22)
            right = (2, 3, 4, 7)
        rows = []
        for m in tech.get("materials") or []:
            row = [m.get("name"), KIND.get(m.get("kind"), m.get("kind") or ""), _num(m.get("need")),
                   _num(m.get("stock")), _num(m.get("buy")), m.get("unit") or "", m.get("supplier") or "—"]
            if doc["show_prices"]:
                row.append(_num(m.get("amount"), 2) if m.get("amount") is not None else "—")
            rows.append(row)
        story.append(table(head, rows, widths, right=right) if rows else Paragraph("Malzeme satırı yok.", p_small))
        for wtext in tech.get("warnings") or []:
            story.append(Paragraph(f"• {esc(wtext)}", p_warn))
        if tech.get("note"):
            story.append(Paragraph(f"Not: {esc(tech['note'])}", p_txt))
    else:
        story.append(Paragraph("Teknik değerlendirme henüz yapılmadı.", p_small))
    story.append(Spacer(1, 4 * mm))

    # 4. Alım satırları
    story.append(Paragraph("4. Siparişe bağlı alım satırları", p_h))
    pls = [p for p in doc["purchase_lines"] if p.get("status") != "cancelled"]
    story.append(table(["Malzeme", "İhtiyaç", "Sipariş verilen", "Teslim alınan", "Birim", "Durum", "Tedarikçi"],
                       [[p["item_name"], _num(p["need_quantity"]), _num(p.get("ordered_quantity")),
                         _num(p.get("received_quantity")), p.get("unit") or "",
                         PURCHASE.get(p["status"], p["status"]), p.get("supplier_name") or "—"] for p in pls],
                       [0.28, 0.1, 0.12, 0.12, 0.07, 0.13, 0.18], right=(1, 2, 3))
                 if pls else Paragraph("Alım satırı yok — eksik malzeme çıkmadı.", p_small))
    story.append(Spacer(1, 4 * mm))

    # 5. Partiler + kaynak lot tüketimi
    story.append(Paragraph("5. Üretim partileri ve kaynak lotlar", p_h))
    if not doc["batches"]:
        story.append(Paragraph("Henüz parti başlatılmadı.", p_small))
    names = doc["names"]
    for b in doc["batches"]:
        block = [Paragraph(f"<b>Lot {esc(b['lot_number'])}</b> — {esc(names.get(b['item_id'], ''))} · "
                           f"{esc(b['status'])} · plan {_num(b['planned'])} · üretilen {_num(b['produced'])} · "
                           f"şahit {_num(b['witness'])}", p_txt),
                 Paragraph(f"Başlatan: {esc(b['started'])}" + (f" · Tamamlayan: {esc(b['completed'])}"
                                                               if b["completed"] else "")
                           + (f" · İptal gerekçesi: {esc(b['cancel_reason'])}" if b["cancel_reason"] else ""),
                           p_small)]
        rows = [[names.get(r["item_id"], f"#{r['item_id']}"), r["phase"] or "—", r["lot_number"] or "lotsuz",
                 r["supplier"] or "—", _num(r["quantity"], 4), r["unit"]] for r in b["rows"]]
        if rows:
            block.append(table(["Malzeme", "Faz", "Kaynak lot", "Tedarikçi", "Tüketilen", "Birim"], rows,
                               [0.33, 0.07, 0.17, 0.23, 0.12, 0.08], right=(4,)))
        back = {k: v for k, v in b["returned"].items() if v}
        if back:
            block.append(Paragraph("Fiziksel iade: " + ", ".join(f"{esc(names.get(k, k))} {_num(v, 4)}"
                                                              for k, v in back.items()), p_small))
        story += [KeepTogether(block), Spacer(1, 2.5 * mm)]
    story.append(Spacer(1, 1.5 * mm))

    # 6. Onay ve kapılar
    story.append(Paragraph("6. Onay durumu ve üretim kapıları", p_h))
    sig = doc["signature"]
    pay = doc["payment"]
    lines = [f"Yönetim onayı: {esc(sig['signer_name'])} · {esc(sig['signed_at'])} · belge özeti "
             f"{esc((sig.get('document_hash') or '')[:16])}…" if sig else "Yönetim onayı: bekleniyor",
             "Ödeme koşulu (üretim): " + ("sağlandı" if pay.get("production_ok") else "bekleniyor")
             + " · (sevkiyat): " + ("sağlandı" if pay.get("shipment_ok") else "bekleniyor"),
             "Son hazırlık kontrolü: " + (f"{esc(doc['prep']['by'])} · {esc(doc['prep']['at'])}"
                                          if doc["prep"]["at"] else "bekleniyor")]
    story += [Paragraph(x, p_txt) for x in lines]

    out = BytesIO()
    pdf_doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                                topMargin=14 * mm, bottomMargin=15 * mm,
                                title=f"Teknik föy — {doc['reference']}", author="Minerva 108",
                                subject="İç belge — müşteriye gönderilmez")

    def deco(canvas, d):
        canvas.saveState()
        canvas.setFont(font, 7)
        canvas.setFillColor(GREY)
        canvas.drawString(14 * mm, 8 * mm, f"İÇ BELGE · B2B teknik föy · {doc['reference']} · {doc['generated']}")
        canvas.drawRightString(A4[0] - 14 * mm, 8 * mm, f"Sayfa {d.page}")
        canvas.restoreState()

    pdf_doc.build(story, onFirstPage=deco, onLaterPages=deco)
    return out.getvalue()
