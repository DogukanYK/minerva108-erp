# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Satın Alma Planı — Excel çıktısı.

    build_workbook(report) -> bytes

`report` = `compute()` + `attach()` çıktısı (PDF ile aynı sözlük).  Sayfalar:
  • "Alım listesi"  — bölüm sırası `purchase_pricing.sections()` ile AYNI
    (1–5: hammadde/ambalaj/etiket, fiyatlı → fiyatsız), sonra yeni etiket /
    koli / palet satırları.  Formüller:
        G (Alınacak) = MAX(0, ROUND(D−E−F, 3))      (brüt modda = D)
        K (Tutar)    = IF(J="", "", ROUND(G*J, 0))   (Excel ROUND = round_half_up)
    + bölüm ara toplamları ve GENEL TOPLAM (SUM).  Lab Excel'de fiyatı ya da
    elimizdekini değiştirince tutar kendiliğinden güncellenir.  "Not" (X)
    sonrasında Y "Aynı malzeme (diğer kartlar)" ve Z "Sipariş geçmişi".
  • "Tedarikçiler" — seçilen + aday firmalar, iletişim, kart adları, K
    "İlişki türleri" (stok kartı / alım / numune / sipariş / eşdeğer kart …).
  • "Ürünler"      — hedef adet, bitmiş stok, kapasite, kısıtlayan malzeme.
  • "Ürün detayı"  — ürün × malzeme ihtiyaç matrisi (kalem biriminde).
  • "Özet"         — kapsam, toplamlar, bölüm listesi, notlar.

Formül hücrelerinin HESAPLANMIŞ değeri `core.xlsx_cache.inject_cached_values`
ile gömülür: formül hesaplamayan önizlemelerde (Quick Look, WhatsApp, Drive)
"Tutar" boş görünmesin.  Gömülen değer Excel'in formülden bulacağı değerin
AYNISIDIR (G için Excel ROUND taklidi; tutmuyorsa — `safe_rounding` kapalı
gibi — G formül yerine raporun alınacak sayısı olarak yazılır ki PDF ile
Excel toplamı ayrışmasın).

Formül kalkanı: openpyxl "=" ile başlayan HER metni formül sayar.  Malzeme /
tedarikçi adı, not, iletişim gibi serbest metinler kullanıcıdan gelir
("=HYPERLINK(…)" ya da "=2 adet kırık" notu) → hepsi `_put_text`/
`_append_as_text` üzerinden düz metin + quotePrefix yazılır; formül yalnız
bu modülün kendi kurduğu hücrelerde bulunur (core/retention_report.py ile
aynı kalıp).  Aynı iki kapı XML'e yazılamayan kontrol karakterlerini
(\x00-\x08, \x0b, \x0c, \x0e-\x1f) de siler: openpyxl bunlarda
IllegalCharacterError atar ve adında tek bir \x01 olan kart bütün Excel
indirmesini 500'e düşürürdü (`clean_text` yalnız boşluk sınıfını temizler).
"""
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO
from typing import Dict, List, Optional

from core.purchase_plan import _amount_near
from core.purchase_pricing import (CURRENCY_SYMBOL, REL_TYPE_TEXT, alt_texts, contact_line, money, notes_lines,
                                   order_texts, round_half_up, sections, unpriced_offer_text)
from core.xlsx_cache import inject_cached_values

SHEET_LIST = "Alım listesi"
SHEET_SUP = "Tedarikçiler"
SHEET_PROD = "Ürünler"
SHEET_DETAIL = "Ürün detayı"
SHEET_SUMMARY = "Özet"

HEADER_FILL = "E8ECF6"
KIND_TEXT = {"raw": "Hammadde", "packaging": "Ambalaj", "label": "Etiket"}
GROUP_TEXT = {"list": "Fiyat listesinde var", "none": "Fiyat yok — teklif alınacak"}
MANUAL_SECTION_TEXT = {"label": "Etiket", "shipping": "Koli / palet", "other": "Diğer"}
MATERIAL_SECTIONS = ("raw_priced", "raw_unpriced", "pkg_priced", "pkg_unpriced", "labels")

# Alım listesi sütunları (spec C4 tablosu).  "Not" X'te (24) sabit; "Aynı
# malzeme (diğer kartlar)" Y ve "Sipariş geçmişi" Z onun SONRASINA eklendi —
# formüller D–K, eski sütunlar kaymaz.
COLS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
WIDTHS = (10, 22, 34, 12, 12, 10, 12, 7, 20, 11, 11, 24, 18, 9, 10, 18, 9, 18, 9, 22, 24, 24, 40, 60, 50, 40)


def _excel_round(x: float, digits: int) -> float:
    """Excel ROUND(x, n) — .5 sıfırdan uzağa (Decimal(repr) ile, bkz. round_half_up)."""
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(repr(float(x))).quantize(q, rounding=ROUND_HALF_UP))


def _clean(v):
    """XML'e yazılamayan kontrol karakterlerini atar (metin değilse aynen)."""
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    return ILLEGAL_CHARACTERS_RE.sub("", v) if isinstance(v, str) else v


def _put_text(ws, row: int, col: int, value):
    """Serbest metni hücreye DAİMA metin olarak yazar ("=…" formüle dönmez)."""
    value = _clean(value)
    c = ws.cell(row=row, column=col, value=value)
    if isinstance(value, str) and c.data_type == "f":
        c.data_type = "s"
        c.quotePrefix = True
    return c


def _append_as_text(ws, values) -> int:
    """`ws.append` + formül kalkanı + kontrol karakteri temizliği; eklenen
    satır numarasını döner."""
    values = [_clean(v) for v in values]
    ws.append(values)
    r = ws.max_row
    for col, v in enumerate(values, start=1):
        if isinstance(v, str):
            c = ws.cell(row=r, column=col)
            if c.data_type == "f":
                c.data_type = "s"
                c.quotePrefix = True
    return r


def _style_header(ws, row: int = 1, wrap: bool = True):
    from openpyxl.styles import Alignment, Font, PatternFill

    fill = PatternFill("solid", fgColor=HEADER_FILL)
    for c in ws[row]:
        c.font = Font(bold=True)
        c.fill = fill
        c.alignment = Alignment(wrap_text=wrap, vertical="center")


def _qty_fmt(unit: Optional[str]) -> str:
    return "#,##0" if unit == "adet" else "#,##0.0##"


def _rel_texts(m: dict):
    rel = m.get("relations") or {}
    card = " / ".join(x["name"] for x in rel.get("card") or [])
    last = rel.get("last")
    last_t = (f"{last['name']} ({last['date']})" if last.get("date") else last["name"]) if last else ""
    samples = "; ".join(f"{s['name']} ({s['date']})" if s.get("date") else s["name"]
                        for s in rel.get("samples") or [])
    return card, last_t, samples


def _products_text(m: dict) -> str:
    return "; ".join(f"{p['no']}. {p['product']}" for p in m.get("per_product") or [])


def _note_text(m: dict, cur: str) -> str:
    """"Not" sütunu — PDF tedarikçi hücresindeki açıklamalarla aynı bilgi:
    kullanılamayan teklifin nedeni (birim uyuşmazlığı / kur yok), ambalaj
    katına yuvarlanmış miktar + tutar (seçenek açıkken), uyarılar."""
    parts = [unpriced_offer_text(o) + "." for o in m.get("offers") or []
             if o.get("price") is None and o.get("note")]
    if m.get("pkg_buy") is not None:
        parts.append(f"Ambalaj katına yuvarlanırsa: {_amount_near(m['pkg_buy'], m['price_unit'])}"
                     f" · {money(m.get('pkg_amount'), cur)}.")
    parts += [x["text"] for x in m.get("cautions") or []]
    return " ".join(parts)


# ─── Alım listesi ───────────────────────────────────────────────────────────

def _sheet_list(wb, report: dict, secs: List[dict], cache: Dict[str, object]) -> None:
    from openpyxl.styles import Font

    meta = report.get("meta") or {}
    opts = meta.get("options") or {}
    pr = report.get("pricing") or {}
    cur = pr.get("currency") or opts.get("currency") or "USD"
    sym = CURRENCY_SYMBOL.get(cur, cur)
    gross = opts.get("stock_mode") == "gross"
    open_on = bool(opts.get("subtract_open_orders"))
    bold = Font(bold=True)

    ws = wb.active
    ws.title = SHEET_LIST
    ws.append(["Bölüm", "Fiyat durumu", "Malzeme", "Gereken",
               "Elimizde (bilgi)" if gross else "Elimizde", "Yolda (bilgi)" if gross else "Yolda",
               "Alınacak", "Birim", "Seçilen tedarikçi", f"Birim fiyat ({sym})", f"Tutar ({sym})",
               "Fiyat kaynağı", "T1", f"Fiyat 1 ({sym})", "Ambalaj 1", "T2", f"Fiyat 2 ({sym})",
               "T3", f"Fiyat 3 ({sym})", "Stok kartında yazan", "Son alım", "Numune",
               "Kullanıldığı ürünler", "Not", "Aynı malzeme (diğer kartlar)", "Sipariş geçmişi"])
    _style_header(ws)

    subtotal_cells: List[str] = []
    for sec in secs:
        if sec["key"] not in MATERIAL_SECTIONS:
            continue
        first = None
        amounts: List[int] = []
        for m in sec["rows"]:
            d = m.get("display") or {}
            unit = d.get("buy_unit") or "adet"
            offers = [o for o in m.get("offers") or []][:3]
            o = offers + [None] * (3 - len(offers))
            card, last_t, samples = _rel_texts(m)
            D = float(d.get("need_num") or 0.0)
            E = float(d.get("stock_num") or 0.0)
            F = float(d.get("open_num") or 0.0) if open_on else None
            buy = float(d.get("buy_num") or 0.0)
            price = m.get("price")
            vals = [KIND_TEXT.get(m["kind"], m["kind"]), GROUP_TEXT.get(m.get("group") or "none"),
                    m["name"], D, E, F, None, unit, m.get("supplier") or "", price, None,
                    m.get("price_source") or "",
                    o[0]["name"] if o[0] else "", o[0]["price"] if o[0] else None,
                    o[0]["package"] if o[0] else None,
                    o[1]["name"] if o[1] else "", o[1]["price"] if o[1] else None,
                    o[2]["name"] if o[2] else "", o[2]["price"] if o[2] else None,
                    card, last_t, samples, _products_text(m), _note_text(m, cur),
                    "; ".join(alt_texts(m)), "; ".join(order_texts(m))]
            r = _append_as_text(ws, vals)
            first = first or r
            # G — Alınacak
            if gross:
                g_formula, g_val = f"=D{r}", D
            else:
                g_formula, g_val = f"=MAX(0,ROUND(D{r}-E{r}-F{r},3))", max(0.0, _excel_round(D - E - (F or 0.0), 3))
            if abs(g_val - buy) <= 1e-9:
                ws.cell(r, 7).value = g_formula
                cache[f"G{r}"] = g_val
            else:
                ws.cell(r, 7).value = buy     # düz yuvarlama: formül raporla ayrışırdı
            # K — Tutar
            ws.cell(r, 11).value = f'=IF(J{r}="","",ROUND(G{r}*J{r},0))'
            amount = round_half_up(buy * price) if price is not None else None
            cache[f"K{r}"] = amount if amount is not None else ""
            if amount is not None:
                amounts.append(amount)
            fmt = _qty_fmt(unit)
            for col in (4, 5, 6, 7):
                ws.cell(r, col).number_format = fmt
            for col in (10, 14, 17, 19):
                v = ws.cell(r, col).value
                if isinstance(v, (int, float)):
                    ws.cell(r, col).number_format = "0.0000" if v < 1 else "#,##0.00"
            ws.cell(r, 11).number_format = "#,##0"
        if first and any(m.get("price") is not None for m in sec["rows"]):
            last = ws.max_row
            r = _append_as_text(ws, ["", "", f"{sec['no']}. {sec['title']} — toplam"])
            ws.cell(r, 11).value = f"=SUM(K{first}:K{last})"
            cache[f"K{r}"] = sum(amounts)
            ws.cell(r, 3).font = bold
            ws.cell(r, 11).font = bold
            ws.cell(r, 11).number_format = "#,##0"
            subtotal_cells.append(f"K{r}")

    ws.append([])
    r = _append_as_text(ws, ["", "", "GENEL TOPLAM (fiyatı belli olanlar)"])
    if subtotal_cells:
        ws.cell(r, 11).value = "=" + "+".join(subtotal_cells)
        cache[f"K{r}"] = sum(cache[c] for c in subtotal_cells)
    else:
        ws.cell(r, 11).value = 0
    ws.cell(r, 3).font = bold
    ws.cell(r, 11).font = bold
    ws.cell(r, 11).number_format = "#,##0"

    # Yeni etiket / koli / palet
    new_sec = next((s for s in secs if s["key"] == "new_items"), None)
    if new_sec:
        ws.append([])
        r = _append_as_text(ws, ["", GROUP_TEXT["none"],
                                 f"{new_sec['no']}. {new_sec['title']} (sistemde fiyat ve stok kaydı yok)"])
        ws.cell(r, 3).font = bold
        for row in new_sec["rows"]:
            if row.get("type") == "label":
                vals = ["Etiket", GROUP_TEXT["none"], row["name"], None, None, None, row["total"], "adet"]
                vals += [""] * 15 + [row.get("face_text") or ""]
            else:
                # Sayı yoksa serbest miktar metni ("22 palet için") — birim o zaman metnin içinde
                num = row.get("qty") is not None
                vals = [MANUAL_SECTION_TEXT.get(row.get("section"), "Diğer"), GROUP_TEXT["none"], row["name"],
                        None, None, None, row["qty"] if num else (row.get("qty_text") or ""),
                        (row.get("unit") or "") if num else ""]
                vals += [""] * 15 + ["Sistemde stok kaydı yok" + (f" · {row['note']}" if row.get("note") else "")]
            r = _append_as_text(ws, vals)
            q = ws.cell(r, 7).value
            # Serbest satır miktarı ondalıklı olabilir (2,5 rulo) — PDF gibi göster
            ws.cell(r, 7).number_format = ("#,##0.0#" if isinstance(q, float) and not q.is_integer()
                                           else "#,##0")

    for col, w in zip(COLS, WIDTHS):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "D2"


# ─── Diğer sayfalar ─────────────────────────────────────────────────────────

def _sheet_suppliers(wb, report: dict, cur: str) -> None:
    ws = wb.create_sheet(SHEET_SUP)
    sym = CURRENCY_SYMBOL.get(cur, cur)
    ws.append(["Firma", "Kalem (en ucuz olduğu)", f"Tutar ({sym})", "Yetkili", "Telefon", "E-posta",
               "Adres / şehir", "Bilgi kaynağı", "Sistemdeki kart adı", "Fiyatı olmayan kalemlerde aday",
               "İlişki türleri"])
    _style_header(ws)
    sup = report.get("suppliers") or {}
    chosen = {b["key"]: b for b in sup.get("chosen") or []}
    cands = {b["key"]: b for b in sup.get("candidates") or []}
    keys = list(chosen) + [k for k in cands if k not in chosen]
    for k in keys:
        b = chosen.get(k) or cands[k]
        c = b.get("contact") or {}
        if c and contact_line(c):
            src = "IMS tedarikçi kartı"
        elif c:
            src = "IMS kartında bilgi yok — girilecek"
        else:
            src = "Sistemde kart yok — girilecek"
        cand_items = cands.get(k, {}).get("items") or []
        r = _append_as_text(ws, [
            b["name"], chosen[k]["count"] if k in chosen else 0,
            chosen[k]["total"] if k in chosen else None,
            c.get("person") or "", c.get("phone") or "", c.get("email") or "", c.get("address") or "",
            src, " / ".join(c.get("cards") or []),
            ", ".join(f"{it['name']} ({', '.join(it.get('reasons') or [])})" if it.get("reasons") else it["name"]
                      for it in cand_items),
            ", ".join(REL_TYPE_TEXT.get(t, t) for t in (b.get("types") or []))])
        ws.cell(r, 3).number_format = "#,##0"
    unrelated = sup.get("unrelated") or []
    if unrelated:
        _append_as_text(ws, ["Sistemde hiçbir firmayla ilişkisi yok", 0, None, "", "", "", "",
                             "Yeni tedarikçi bulunmalı", "", ", ".join(u["name"] for u in unrelated), ""])
    for col, w in zip("ABCDEFGHIJK", (26, 12, 12, 20, 16, 28, 28, 26, 34, 90, 40)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "B2"


def _sheet_products(wb, report: dict) -> None:
    ws = wb.create_sheet(SHEET_PROD)
    ws.append(["No", "Ürün", "Marka", "Reçete", "Ölçek", "Hedef adet", "Bitmiş stok", "Üretilecek",
               "Reçetede ambalaj", "Tahmini", "Elimizdekiyle üretilebilir", "Kısıtlayan malzeme",
               "Uyarılar", "Not"])
    _style_header(ws)
    for p in report.get("products") or []:
        cap = p.get("capacity") or {}
        r = _append_as_text(ws, [
            p["no"], p["name"], p.get("brand") or "", p.get("recipe_name") or "", p.get("scale"),
            p["qty"], p.get("finished_stock"), p.get("produce_qty"),
            "Var" if p.get("has_packaging") else "Yok", "Evet" if p.get("estimated") else "",
            cap.get("producible"),
            "; ".join(f"{x['name']} ({_amount_near(x['stock'], x['unit'])})" for x in cap.get("limiting") or []),
            " ".join(x["text"] for x in p.get("cautions") or []), p.get("note") or ""])
        for col in (6, 7, 8, 11):
            ws.cell(r, col).number_format = "#,##0"
    for col, w in zip("ABCDEFGHIJKLMN", (6, 40, 14, 40, 8, 11, 11, 11, 10, 9, 14, 40, 60, 30)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "C2"


def _sheet_detail(wb, report: dict, cache: Dict[str, object]) -> None:
    from openpyxl.styles import Alignment
    from openpyxl.utils import get_column_letter

    ws = wb.create_sheet(SHEET_DETAIL)
    prods = report.get("products") or []
    col_of = {p["no"]: 4 + i for i, p in enumerate(prods)}
    tot_col = 4 + len(prods)
    _append_as_text(ws, ["Malzeme", "Tür", "Birim"] + [f"{p['no']}. {p['name']}" for p in prods]
                    + ["Toplam (üretim)", "Gereken (alım firesiyle)", "Durum"])
    _style_header(ws)
    ws.row_dimensions[1].height = 60
    for c in ws[1][3:]:
        c.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
    rows = [(m, "Alınacak" if m["status"] == "to_buy" else "Yeterli") for m in report.get("materials") or []]
    rows += [(h, "Bekletiliyor") for h in report.get("held") or []]
    for m, status in rows:
        vals = [m["name"], KIND_TEXT.get(m["kind"], m["kind"]), m.get("unit") or ""] + [None] * len(prods)
        for pp in m.get("per_product") or []:
            if pp["no"] in col_of:
                vals[col_of[pp["no"]] - 1] = round(float(pp["need"]), 3)
        written = [v for v in vals[3:3 + len(prods)] if v is not None]     # sütun sırası
        vals += [None, round(float(m.get("need") or 0.0), 3), status]
        r = _append_as_text(ws, vals)
        if prods:
            a, b = get_column_letter(4), get_column_letter(tot_col - 1)
            ws.cell(r, tot_col).value = f"=SUM({a}{r}:{b}{r})"
            total = 0.0
            for v in written:          # Excel'in soldan sağa toplamıyla aynı sıra
                total += v
            cache[f"{get_column_letter(tot_col)}{r}"] = total
        fmt = "#,##0" if m.get("unit") == "adet" else "#,##0.###"
        for col in range(4, tot_col + 2):
            ws.cell(r, col).number_format = fmt
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 11
    ws.column_dimensions["C"].width = 7
    for i in range(len(prods)):
        ws.column_dimensions[get_column_letter(4 + i)].width = 14
    for col, w in ((tot_col, 14), (tot_col + 1, 14), (tot_col + 2, 12)):
        ws.column_dimensions[get_column_letter(col)].width = w
    ws.freeze_panes = "D2"


def _sheet_summary(wb, report: dict, secs: List[dict], cur: str) -> None:
    from openpyxl.styles import Alignment, Font

    from database import to_tr

    ws = wb.create_sheet(SHEET_SUMMARY)
    meta = report.get("meta") or {}
    pr = report.get("pricing") or {}
    tot = pr.get("totals") or {}
    cnt = pr.get("counts") or {}
    sym = CURRENCY_SYMBOL.get(cur, cur)
    bold = Font(bold=True)
    ws.append(["Alan", "Değer"])
    _style_header(ws)
    gen = meta.get("generated_at")
    try:
        gen_tr = to_tr(datetime.fromisoformat(gen)).strftime("%d.%m.%Y %H:%M") if gen else ""
    except ValueError:
        gen_tr = gen or ""
    c = meta.get("counts") or {}
    rows = [
        ("Başlık", meta.get("title") or ""),
        ("Kapsam", meta.get("scope_text") or ""),
        ("Stoklar", meta.get("stock_as_of_tr") or ""),
        ("Oluşturulma", gen_tr),
        ("Para birimi", f"{cur} ({sym})"),
        ("Ürün sayısı", c.get("products")),
        ("Toplam adet", c.get("units")),
        (f"Hammadde — fiyatı belli tutar ({sym})", tot.get("raw")),
        (f"Ambalaj — fiyatı belli tutar ({sym})", tot.get("packaging")),
        (f"Etiket — fiyatı belli tutar ({sym})", tot.get("label")),
        (f"GENEL TOPLAM — fiyatı belli ({sym})", tot.get("all")),
        ("Fiyatlı / fiyatsız hammadde", f"{cnt.get('raw_priced', 0)} / {cnt.get('raw_unpriced', 0)}"),
        ("Fiyatlı / fiyatsız ambalaj", f"{cnt.get('packaging_priced', 0)} / {cnt.get('packaging_unpriced', 0)}"),
        ("Fiyatlı / fiyatsız etiket", f"{cnt.get('label_priced', 0)} / {cnt.get('label_unpriced', 0)}"),
        ("Yeni etiket (adet)", c.get("labels_new_total")),
        ("Yeterli kalem", c.get("sufficient")),
        ("Bekletilen kalem", c.get("held")),
        ("Hariç kalem", c.get("excluded")),
    ]
    for k, v in rows:
        r = _append_as_text(ws, [k, v])
        if isinstance(v, (int, float)):
            ws.cell(r, 2).number_format = "#,##0"
            ws.cell(r, 2).alignment = Alignment(horizontal="left")
    ws.append([])
    r = _append_as_text(ws, ["Bölümler", ""])
    ws.cell(r, 1).font = bold
    for s in secs:
        _append_as_text(ws, [f"{s['no']}. {s['title']}", s.get("summary") or ""])
    ws.append([])
    r = _append_as_text(ws, ["Notlar ve kaynaklar", ""])
    ws.cell(r, 1).font = bold
    for t in notes_lines(report):
        _append_as_text(ws, ["", t])
    for row in ws.iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top", horizontal="left")
    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 120


# ─── Giriş ──────────────────────────────────────────────────────────────────

def build_workbook(report: dict) -> bytes:
    """Rapor dict'inden formüllü + önbellek değerli xlsx baytları."""
    from openpyxl import Workbook

    meta = report.get("meta") or {}
    cur = (report.get("pricing") or {}).get("currency") or (meta.get("options") or {}).get("currency") or "USD"
    secs = sections(report)
    wb = Workbook()
    list_cache: Dict[str, object] = {}
    detail_cache: Dict[str, object] = {}
    _sheet_list(wb, report, secs, list_cache)
    _sheet_suppliers(wb, report, cur)
    _sheet_products(wb, report)
    _sheet_detail(wb, report, detail_cache)
    _sheet_summary(wb, report, secs, cur)
    wb.properties.title = _clean(meta.get("title")) or "Satın Alma Planı"
    wb.properties.creator = "Minerva 108"
    buf = BytesIO()
    wb.save(buf)
    return inject_cached_values(buf.getvalue(), {SHEET_LIST: list_cache, SHEET_DETAIL: detail_cache})
