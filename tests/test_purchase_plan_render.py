"""
Satın Alma Planı çıktıları — PDF (core/purchase_plan_pdf.py), Excel
(core/purchase_plan_xlsx.py) ve formül önbelleği (core/xlsx_cache.py).

    MINERVA_TEST_DB=minerva_test_u .venv/bin/pytest tests/test_purchase_plan_render.py -q

Rapor sözlüğü `compute()` + `attach()` ile sentetik girdilerden kurulur (DB'ye
yazılmaz).  Kapsam: PDF yatay + "Alındı" + bölüm başlıkları/numaraları + boş
bölüm basılmaz + kaçışlı ad + brüt/yolda sütun başlıkları; Excel sayfaları,
Tutar hücresinde formül VE gömülü `<v>`, `data_only` okuma, ara/genel toplam,
brüt modda G=D, serbest metin formül kalkanı ("=HYPERLINK…" adı / notu /
serbest satırı formül olarak yazılmaz); birim uyuşmazlığındaki teklifin
nedeni ("fiyat yazılmamış" değil) PDF + Excel'de, ambalaj katı Excel "Not"
sütununda, ondalıklı serbest miktar biçimi, kontrol karakterli ad Excel'i
düşürmez; xlsx_cache hata ve tür durumları; "aynı malzeme" + sipariş
satırları (PDF'te en çok 3 + "+N", Excel'de "Not"tan sonra Y/Z sütunları,
Tedarikçiler K "İlişki türleri"); grup stoğu dökümü PDF stok hücresinde ve
Excel AA (27.) sütununda (24–26 aynı); stoktan karşılanan değer —
"Stoktan karşılananlar" sayfası (formül + önbellek + TOPLAM, açık siparişte
L/M), Alım listesi GENEL TOPLAM'ı değişmez, Özet plan toplamı, PDF "Değer" /
"stoktan" / "Planın toplam malzeme maliyeti" (brüt modda hiçbiri); ambalaj
uyarısı (16.10.2026) Alım listesi K turuncu + hücre yorumu, PDF ⚠ + "Not:";
"Ürün maliyeti" sayfası (formül + önbellek, ara toplam, TOPLAM, tür bazında
maliyet, eksik kalemler) ve PDF "Maliyet özeti" bölümü.
Sondaki test `RUSYA_FIXTURE_DIR` varsa Rusya kabul senaryosunu iki çıktıya da
basar (toplamlar PDF ↔ Excel ↔ rapor aynı).
"""
import importlib.util
import io
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path

import openpyxl
import pytest
from pypdf import PdfReader

from core.consumption import IngredientRec, ItemRec, RecipeRec
from core.purchase_plan import LotRec, OpenOrderRec, PlanInputs, compute
from core.purchase_plan_models import PlanRequest
from core.purchase_plan_pdf import render_pdf
from core.purchase_plan_xlsx import SHEET_DETAIL, SHEET_LIST, SHEET_STOCK, SHEET_SUP, build_workbook
from core.purchase_pricing import (OfferRec, OrderRec, PriceInputs, ReceiptRec, SupplierRec, alt_texts, attach,
                                   money, order_texts, sections)
from core.xlsx_cache import inject_cached_values

NOW = datetime(2026, 10, 5, 9, 0)
EVIL_NAME = '=HYPERLINK("http://evil.example","tıkla")'


def _it(id, name, *, cat="Hammadde", unit="g", stock=0.0, pkg=None):
    return ItemRec(id=id, name=name, category=cat, unit=unit, pkg_type=pkg, current_stock=stock)


def _report(*, mode="net", open_orders=False, with_packaging=False, surya_preferred=False):
    items = [
        _it(1, "GLİSERİN", stock=500),
        _it(2, "SHEA & BUTTER <özel>"),
        _it(3, "GÜL ABSOLÜ", unit="ml"),
        _it(4, EVIL_NAME),
        _it(5, "KSANTAN GAM", stock=1_000_000),
        _it(6, "ALOE VERA TOZU"),
    ]
    ings = [(1, 10), (2, 5), (3, 2), (4, 1), (5, 3), (6, 1)]
    if with_packaging:
        items.append(_it(7, "50 ML KAVANOZ", cat="Ambalaj", unit="adet", pkg="kavanoz"))
        ings.append((7, 1))
    recipe = RecipeRec(id=1, name="Gece Kremi 50 ml",
                       ingredients=tuple(IngredientRec(item_id=i, quantity=q) for i, q in ings))
    oo = {1: [OpenOrderRec(item_id=1, quantity=2000, unit="g")]} if open_orders else {}
    inp = PlanInputs(items={i.id: i for i in items}, recipes={1: recipe}, open_orders=oo, stock_as_of=NOW)
    req = PlanRequest(
        title="Deneme Siparişi",
        lines=[{"recipe_id": 1, "qty": 1000, "display_name": "Gece Kremi 50 ml", "label_faces": 2}],
        manual_lines=[{"section": "shipping", "name": "=1+1 koli", "qty": 40, "unit": "adet",
                       "note": "=2 adet kırık"},
                      {"section": "shipping", "name": "Streç film", "qty_text": "2 palet için"}],
        options={"stock_mode": mode, "subtract_open_orders": open_orders, "label_mode": "new",
                 "new_label_title": "Rusça etiket", "excluded_item_ids": [],
                 "held_items": [{"item_id": 6, "reason": "Lab teyidi bekleniyor"}],
                 "checklist_owner": "Satın alma ekibi",
                 "notes": ["=cmd|' /C calc'!A0", "Normal bir not."]})
    res = compute(inp, req)
    pin = PriceInputs(
        offers={1: [OfferRec(item_id=1, supplier_name="UMAYCHEM", supplier_id=1, unit_price=4.52,
                             package_size=25, price_unit="kg", source="stok_son_durum",
                             source_label="Stok Son Durum — Eylül"),
                    OfferRec(item_id=1, supplier_name="SURYA KİMYA", unit_price=5.0, package_size=25,
                             price_unit="kg", source="stok_son_durum")],
                2: [OfferRec(item_id=2, supplier_name="BEFCHEM", unit_price=8.0, package_size=25,
                             price_unit="kg", source="stok_son_durum")]},
        suppliers={1: SupplierRec(id=1, name="UMAYCHEM", contact_person="Semih Bey", phone="05537953163",
                                  email="info@umaychem.example"),
                   2: SupplierRec(id=2, name="DOALİNN")},
        card_supplier={3: 2})
    if surya_preferred:
        pin.suppliers[3] = SupplierRec(id=3, name="SURYA KİMYA", status="preferred")
    return attach(res, pin, None, currency="USD")


def _pdf_text(pdf: bytes):
    rd = PdfReader(io.BytesIO(pdf))
    return rd, " ".join(" ".join((p.extract_text() or "") for p in rd.pages).split())


def _mat(res, name):
    return next(m for m in res["materials"] if m["name"] == name)


def _row_of(ws, name):
    return next(r for r in range(2, ws.max_row + 1) if ws.cell(r, 3).value == name)


# ─── PDF ────────────────────────────────────────────────────────────────────

def test_pdf_landscape_sections_and_alindi():
    res = _report()
    pdf = render_pdf(res)
    assert pdf.startswith(b"%PDF")
    rd, text = _pdf_text(pdf)
    assert len(rd.pages) >= 2
    for p in rd.pages:
        assert float(p.mediabox.width) > float(p.mediabox.height)
    assert "Alındı" in text
    assert "Deneme Siparişi" in text and "Sayfa 1" in text
    # Bölüm numaraları sections() ile: ambalaj/etiket bölümleri boş → atlanır, numara kaymaz
    for t in ("1. Hammadde — fiyat listesinde olanlar", "2. Hammadde — fiyatı olmayanlar, teklif alınacak",
              "3. Yeni etiket / koli / palet — teklif alınacak", "4. Kimlerle görüşeceğiz — tedarikçi bazında",
              "Fiyatı olmayanlar için teklif istenebilecek firmalar",
              "Tedarikçi bilgi eksikleri (Satın alma ekibi için)",
              "Ürünler, hedef adetler ve kapasite", "Yeterli olanlar", "Notlar ve kaynaklar"):
        assert t in text, t
    assert "Ambalaj — fiyatlı" not in text and "Ambalaj — fiyatı olmayanlar" not in text
    # Kutular + tedarikçi hücresi
    assert "Gerçek toplam, fiyatı olmayan kalemler eklenince bundan yüksek olacak." in text
    assert "bekletiliyor" in text and "ALOE VERA TOZU" in text           # turuncu uyarı kutusu
    assert "UMAYCHEM — 4,52 $/kg · 25 kg'lık ambalaj" in text
    assert "SURYA KİMYA — 5,00 $/kg · 25 kg'lık ambalaj (daha pahalı)" in text
    assert "Fiyat yok — teklif alınacak" in text
    assert "Stok kartında yazan: DOALİNN" in text
    assert "İletişim bilgisi sistemde yok — Satın alma ekibi girecek" in text
    # XML'e özel karakterli ad kaçışlanır (PDF düşmez, ad aynen görünür)
    assert "SHEA & BUTTER <özel>" in text


def test_pdf_subtitle_amount_basis_follows_selection_rule():
    """Başlık altındaki "Tutar = …" cümlesi notlar / bölüm alt başlıklarıyla
    çelişmesin: seçimi tercih değiştirdiyse "seçilen teklif" der."""
    _, text = _pdf_text(render_pdf(_report()))
    assert "Tutar = alınacak × en ucuz birim fiyat." in text
    res = _report(surya_preferred=True)
    assert _mat(res, "GLİSERİN")["supplier"] == "SURYA KİMYA" and res["pricing"]["status_used"] is True
    _, text = _pdf_text(render_pdf(res))
    assert "en ucuz birim fiyat" not in text
    assert "Tutar = alınacak × seçilen teklifin birim fiyatı (önce malzeme tercihi" in text


def test_pdf_empty_section_not_printed_and_packaging_when_present():
    _, text = _pdf_text(render_pdf(_report(with_packaging=True)))
    assert "Ambalaj — fiyatı olmayanlar" in text
    assert "Ambalaj — fiyatlı" not in text                    # fiyatlı ambalaj yok → bölüm yok
    assert "3. Ambalaj — fiyatı olmayanlar" in text


def test_pdf_gross_and_open_order_columns():
    _, text = _pdf_text(render_pdf(_report(mode="gross")))
    assert "Elimizde (bilgi)" in text
    assert "Yeterli olanlar" not in text                      # brüt modda bu bölüm yok
    _, text = _pdf_text(render_pdf(_report(open_orders=True)))
    assert "Yolda" in text and "Elimizde (bilgi)" not in text


# ─── Excel ──────────────────────────────────────────────────────────────────

def test_xlsx_sheets_formulas_and_cached_values():
    res = _report()
    data = build_workbook(res)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    # "Stoktan karşılananlar" (14.10.2026) net modda Alım listesi'nden hemen sonra;
    # "Ürün maliyeti" (16.10.2026) "Ürünler"den sonra
    assert wb.sheetnames == ["Alım listesi", "Stoktan karşılananlar", "Tedarikçiler", "Ürünler", "Ürün maliyeti",
                             "Ürün detayı", "Özet"]
    ws = wb[SHEET_LIST]
    assert ws.freeze_panes == "D2"
    r = _row_of(ws, "GLİSERİN")
    assert ws[f"G{r}"].value == f"=MAX(0,ROUND(D{r}-E{r}-F{r},3))"
    assert ws[f"K{r}"].value == f'=IF(J{r}="","",ROUND(G{r}*J{r},0))'

    # Tutar hücresinde formül VE gömülü <v> (XML'den)
    xml = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml").decode()
    gl = _mat(res, "GLİSERİN")
    cell = re.search(rf'<c r="K{r}"[^>]*>(.*?)</c>', xml).group(1)
    assert "<f>" in cell and f"<v>{gl['amount']}</v>" in cell
    assert not re.search(r"<f>[^<]*</f><v ?/>", xml)                  # önbelleksiz formül kalmadı

    wv = openpyxl.load_workbook(io.BytesIO(data), data_only=True)[SHEET_LIST]
    assert wv[f"K{r}"].value == gl["amount"]
    assert wv[f"G{r}"].value == pytest.approx(gl["display"]["buy_num"])
    ru = _row_of(ws, "GÜL ABSOLÜ")                                  # fiyatsız → boş metin sonucu
    assert wv[f"K{ru}"].value in (None, "")
    tot = next(x for x in range(2, ws.max_row + 1) if ws.cell(x, 3).value == "GENEL TOPLAM (fiyatı belli olanlar)")
    assert wv[f"K{tot}"].value == res["pricing"]["totals"]["all"] > 0
    sub = next(x for x in range(2, ws.max_row + 1)
               if str(ws.cell(x, 3).value or "").startswith("1. Hammadde — fiyat listesinde olanlar — toplam"))
    assert ws[f"K{sub}"].value.startswith("=SUM(K")
    assert wv[f"K{sub}"].value == res["pricing"]["totals"]["raw"]
    assert ws[f"J{r}"].number_format == "#,##0.00" and ws[f"K{r}"].number_format == "#,##0"

    # Ürün detayı: ürün × malzeme, SUM önbelleği
    wd = openpyxl.load_workbook(io.BytesIO(data), data_only=True)[SHEET_DETAIL]
    rd = next(x for x in range(2, wd.max_row + 1) if wd.cell(x, 1).value == "GLİSERİN")
    assert wd.cell(rd, 5).value == pytest.approx(gl["need_production"])


def test_xlsx_gross_mode_g_equals_need():
    res = _report(mode="gross")
    ws = openpyxl.load_workbook(io.BytesIO(build_workbook(res)))[SHEET_LIST]
    r = _row_of(ws, "GLİSERİN")
    assert ws[f"G{r}"].value == f"=D{r}"
    assert ws["E1"].value == "Elimizde (bilgi)"


def test_xlsx_free_text_never_becomes_formula():
    data = build_workbook(_report())
    allowed = re.compile(r'^(MAX\(0,ROUND\(D\d+-E\d+-F\d+,3\)\)|D\d+|IF\(J\d+="","",ROUND\([GL]\d+\*J\d+,0\)\)'
                         r'|SUM\([A-Z]+\d+:[A-Z]+\d+\)|K\d+(\+K\d+)*'
                         r'|MIN\(D\d+,E\d+\)|MIN\(F\d+,ROUND\(D\d+-G\d+,6\)\)'    # + Stoktan karşılananlar
                         r'|H\d+|IF\(G\d+="","",[FJ]\d+\*G\d+\)|F\d+\*I\d+'       # + Ürün maliyeti
                         r'|ROUND\(SUM\(K\d+:K\d+\),0\))$')
    z = zipfile.ZipFile(io.BytesIO(data))
    for n in z.namelist():
        if n.startswith("xl/worksheets/sheet"):
            for f in re.findall(r"<f>([^<]*)</f>", z.read(n).decode()):
                assert allowed.match(f), (n, f)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    ws = wb[SHEET_LIST]
    r = _row_of(ws, EVIL_NAME)
    assert ws.cell(r, 3).data_type == "s" and ws.cell(r, 3).value == EVIL_NAME
    r = _row_of(ws, "=1+1 koli")
    assert ws.cell(r, 3).data_type == "s"
    assert ws.cell(r, 24).value == "Sistemde stok kaydı yok · =2 adet kırık"
    texts = [c.value for row in wb["Özet"].iter_rows() for c in row if isinstance(c.value, str)]
    assert "=cmd|' /C calc'!A0" in texts


def _edge_report():
    """Adet kartına kg fiyatı (birim uyuşmazlığı), ambalaj katı açık, ondalıklı
    serbest satır, adında/başlıkta kontrol karakteri."""
    items = [_it(1, "GLİSERİN", stock=500), _it(2, "KAPSÜL\x01 KABUĞU", unit="adet")]
    recipe = RecipeRec(id=1, name="Krem", ingredients=(IngredientRec(item_id=1, quantity=10),
                                                       IngredientRec(item_id=2, quantity=2)))
    inp = PlanInputs(items={i.id: i for i in items}, recipes={1: recipe}, open_orders={}, stock_as_of=NOW)
    req = PlanRequest(title="Deneme\x02 Siparişi", lines=[{"recipe_id": 1, "qty": 1000}],
                      manual_lines=[{"section": "shipping", "name": "Koli bandı", "qty": 2.5, "unit": "rulo"},
                                    {"section": "shipping", "name": "Koli", "qty": 40, "unit": "adet"}],
                      options={"round_to_package": True})
    pin = PriceInputs(offers={1: [OfferRec(item_id=1, supplier_name="UMAYCHEM", unit_price=4.37, package_size=25,
                                           price_unit="kg", source="stok_son_durum")],
                              2: [OfferRec(item_id=2, supplier_name="FİRMA D", unit_price=2.0, price_unit="kg",
                                           source="stok_son_durum")]})
    return attach(compute(inp, req), pin, None, currency="USD", round_to_package=True)


def test_unit_mismatch_offer_shows_reason_not_missing_price():
    res = _edge_report()
    _, text = _pdf_text(render_pdf(res))
    why = "FİRMA D — fiyat listesinde, fiyat birimi (kg) malzemenin alım birimiyle (adet) uyuşmuyor"
    assert why in text and "FİRMA D — fiyat listesinde, fiyat yazılmamış" not in text
    ws = openpyxl.load_workbook(io.BytesIO(build_workbook(res)))[SHEET_LIST]
    assert why in ws.cell(_row_of(ws, "KAPSÜL KABUĞU"), 24).value


def test_xlsx_package_rounding_written_and_decimal_manual_qty():
    res = _edge_report()
    gl = _mat(res, "GLİSERİN")
    assert gl["pkg_buy"] == 25 and gl["pkg_amount"] == 109
    wb = openpyxl.load_workbook(io.BytesIO(build_workbook(res)))
    ws = wb[SHEET_LIST]
    # Not: ambalaj katı satırı + ambalaj uyarısı (9,5 kg < 25 kg'ın yarısı; seçenek açık →
    # "tam ambalaj alınırsa" eki yok, yuvarlanmış satır zaten yazıyor)
    assert ws.cell(_row_of(ws, "GLİSERİN"), 24).value == (
        "Ambalaj katına yuvarlanırsa: 25,0 kg · 109 $. Bu fiyat 25 kg'lık ambalaj için; alınacak 9,5 kg. "
        "Bu miktarda birim fiyat daha yüksek olabilir — bu miktara özel fiyat alınmalı.")
    r = _row_of(ws, "Koli bandı")
    assert ws.cell(r, 7).value == 2.5 and ws.cell(r, 7).number_format == "#,##0.0#"
    assert ws.cell(_row_of(ws, "Koli"), 7).number_format == "#,##0"
    # Özet'teki "ayrıca gösterildi" cümlesi artık doğru
    assert any("Ambalaj katına yuvarlanmış" in str(c.value) for row in wb["Özet"].iter_rows() for c in row)


def test_xlsx_control_characters_stripped_not_500():
    wb = openpyxl.load_workbook(io.BytesIO(build_workbook(_edge_report())))
    ws = wb[SHEET_LIST]
    assert _row_of(ws, "KAPSÜL KABUĞU")
    assert wb.properties.title == "Deneme Siparişi"


# ─── xlsx_cache ─────────────────────────────────────────────────────────────

def _small_book():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sayfa & 1"
    ws["A1"], ws["B1"], ws["C1"], ws["D1"] = 2, "=A1*2", '=IF(A1=2,"","x")', '=IF(A1=2,"a<b","")'
    wb.create_sheet("İkinci")["A1"] = "=1+1"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_cache_types_and_lookup_by_sheet_name():
    out = inject_cached_values(_small_book(), {"Sayfa & 1": {"B1": 4, "C1": "", "D1": "a<b"},
                                               "İkinci": {"A1": 2.5}})
    z = zipfile.ZipFile(io.BytesIO(out))
    x1 = z.read("xl/worksheets/sheet1.xml").decode()
    assert re.search(r'<c r="B1"[^>]*><f>A1\*2</f><v>4</v></c>', x1)
    assert re.search(r'<c r="C1"[^>]*t="str"[^>]*>.*?<v></v></c>', x1)
    assert re.search(r'<c r="D1"[^>]*t="str"[^>]*>.*?<v>a&lt;b</v></c>', x1)
    wv = openpyxl.load_workbook(io.BytesIO(out), data_only=True)
    assert wv["Sayfa & 1"]["B1"].value == 4 and wv["Sayfa & 1"]["D1"].value == "a<b"
    assert wv["İkinci"]["A1"].value == 2.5
    wf = openpyxl.load_workbook(io.BytesIO(out))
    assert wf["Sayfa & 1"]["B1"].value == "=A1*2"                     # formül korunur


def test_xlsx_cache_rejects_unknown_sheet_or_missing_formula_cell():
    raw = _small_book()
    with pytest.raises(ValueError):
        inject_cached_values(raw, {"Yok": {"A1": 1}})
    with pytest.raises(ValueError):
        inject_cached_values(raw, {"Sayfa & 1": {"A1": 1}})          # A1 formül değil
    with pytest.raises(ValueError):
        inject_cached_values(raw, {"Sayfa & 1": {"Z9": 1}})
    assert inject_cached_values(raw, {}) == raw


# ─── "Aynı malzeme" + sipariş geçmişi ───────────────────────────────────────

def _stearyl_report():
    """Prod'daki stearil alkol: 126 Tatlıdilimler (g, stok) reçetede; 599
    Yiğitoğlu ve 593 Veser (adet) aynı malzeme grubunda; 4 sipariş işareti."""
    items = [_it(126, "SETİL STEARİL ALKOL", stock=3944), _it(599, "CETYL STEARYL ALCOHOL", unit="adet"),
             _it(593, "CETEARYL ALCOHOL", unit="adet")]
    recipe = RecipeRec(id=1, name="Krem", ingredients=(IngredientRec(item_id=126, quantity=40),))
    inp = PlanInputs(items={i.id: i for i in items}, recipes={1: recipe}, stock_as_of=NOW,
                     mgroup_of={126: 7, 599: 7, 593: 7}, mgroup_names={7: "Setil stearil alkol"})
    res = compute(inp, PlanRequest(title="Stearil", lines=[{"recipe_id": 1, "qty": 1000}]))
    sup = {1: SupplierRec(id=1, name="TATLIDİLİMLER"), 2: SupplierRec(id=2, name="YİĞİTOGLU KİMYA"),
           3: SupplierRec(id=3, name="VESER KİMYEVİ")}
    orders = {126: [OrderRec(126, supplier_id=1, supplier_name="TATLIDİLİMLER", quantity=25000, unit="g",
                             ordered_at=datetime(2026, 10, 1, 7), expected_date=datetime(2026, 10, 10).date())]
              + [OrderRec(126, supplier_id=1, supplier_name="TATLIDİLİMLER", quantity=10000, unit="g",
                          ordered_at=datetime(2026, m, 1, 7), closed_at=datetime(2026, m, 3),
                          closed_reason="received") for m in (8, 6, 4)]}
    pin = PriceInputs(suppliers=sup, card_supplier={126: 1, 599: 2, 593: 3},
                      lots={126: [LotRec(item_id=126, lot_number="TD-1", supplier_id=1, quantity=3944,
                                         created_at=datetime(2026, 8, 12, 7), inventory_id=1)]},
                      receipts={126: [ReceiptRec(126, "TD-1", 25000, datetime(2026, 8, 12, 7), "purchase")]},
                      orders=orders)
    return attach(res, pin, None, currency="USD")


def test_pdf_same_material_and_order_lines_capped():
    res = _stearyl_report()
    _, text = _pdf_text(render_pdf(res))
    assert "Stok kartında yazan: TATLIDİLİMLER (son alım 12.08.2026)" in text
    assert "Sipariş: TATLIDİLİMLER 01.10.2026 · 25,0 kg · açık · beklenen 10.10.2026" in text
    assert "Sipariş: TATLIDİLİMLER 01.06.2026 · 10,0 kg · teslim alındı" in text
    assert "01.04.2026" not in text and "+1 sipariş daha" in text            # en çok 3 + "+N"
    assert "Aynı malzeme: «CETEARYL ALCOHOL» (VESER KİMYEVİ, stok yok, birimi adet)" in text
    assert "Aynı malzeme: «CETYL STEARYL ALCOHOL» (YİĞİTOGLU KİMYA, stok yok, birimi adet)" in text
    # eşdeğer kartların tedarikçileri fiyatsız kalem için aday firma
    assert "eşdeğer kart «CETYL STEARYL ALCOHOL»" in text


def test_xlsx_same_material_and_order_history_after_note():
    res = _stearyl_report()
    m = res["materials"][0]
    wb = openpyxl.load_workbook(io.BytesIO(build_workbook(res)))
    ws = wb[SHEET_LIST]
    assert (ws.cell(1, 24).value, ws.cell(1, 25).value, ws.cell(1, 26).value) == (
        "Not", "Aynı malzeme (diğer kartlar)", "Sipariş geçmişi")
    r = _row_of(ws, "SETİL STEARİL ALKOL")
    assert ws.cell(r, 25).value == "; ".join(alt_texts(m)) and "«CETYL STEARYL ALCOHOL»" in ws.cell(r, 25).value
    assert ws.cell(r, 26).value == "; ".join(order_texts(m))                  # Excel'de sınır yok (4 sipariş)
    assert ws.cell(r, 26).value.count("TATLIDİLİMLER") == 4
    assert ws[f"K{r}"].value == f'=IF(J{r}="","",ROUND(G{r}*J{r},0))'         # formüller kaymadı
    sp = wb[SHEET_SUP]
    assert sp.cell(1, 11).value == "İlişki türleri"
    types = {sp.cell(x, 1).value: sp.cell(x, 11).value for x in range(2, sp.max_row + 1)}
    assert types["TATLIDİLİMLER"] == "stok kartı, alım, sipariş"
    assert types["YİĞİTOGLU KİMYA"] == types["VESER KİMYEVİ"] == "eşdeğer kart"


# ─── Stoktan karşılanan değer (14.10.2026) ──────────────────────────────────

def _sheet_rows(ws):
    return {ws.cell(r, 3).value: r for r in range(2, ws.max_row + 1) if ws.cell(r, 3).value}


def test_xlsx_stock_sheet_formulas_cache_and_total():
    """GLİSERİN: 10 kg gerek, 0,5 kg stok (kısmi, 4,52 $) · KSANTAN: yeterli,
    fiyatsız.  Yeni sayfa: önce yeterli, sonra kısmi; G = MIN(D,E), K =
    IF(J="","",ROUND(G*J,0)) + önbellek; TOPLAM = stoktan değer.  Alım
    listesi GENEL TOPLAM'ı değişmez; Özet plan toplamını gösterir."""
    res = _report()
    cs = res["pricing"]["cost_summary"]
    assert (cs["buy_total"], cs["stock_total"], cs["plan_total"]) == (83, 2, 85)
    data = build_workbook(res)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    wv = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    ws, sv = wb[SHEET_STOCK], wv[SHEET_STOCK]
    assert [ws.cell(1, c).value for c in range(1, 14)] == [
        "Durum", "Tür", "Malzeme", "Gereken", "Elimizde", "Yolda", "Stoktan kullanılan", "Birim",
        "Seçilen tedarikçi", "Birim fiyat ($)", "Değer ($)", "Fiyat kaynağı", "Not"]
    rows = _sheet_rows(ws)
    assert list(rows)[:2] == ["KSANTAN GAM", "GLİSERİN"]                      # önce yeterli, sonra kısmi
    k, g = rows["KSANTAN GAM"], rows["GLİSERİN"]
    assert ws.cell(k, 1).value == "Yeterli" and ws.cell(g, 1).value == "Kısmen stoktan"
    assert ws[f"G{g}"].value == f"=MIN(D{g},E{g})" and ws[f"K{g}"].value == f'=IF(J{g}="","",ROUND(G{g}*J{g},0))'
    assert sv[f"G{g}"].value == pytest.approx(0.5) and sv[f"K{g}"].value == 2 == _mat(res, "GLİSERİN")["stock_value"]
    assert sv[f"G{k}"].value == pytest.approx(3.0) and sv[f"K{k}"].value in (None, "")    # fiyatsız
    assert "Fiyat yok" in ws.cell(k, 13).value
    tot = rows["TOPLAM (fiyatı belli olanlar)"]
    assert ws[f"K{tot}"].value == f"=SUM(K2:K{tot - 1})" and sv[f"K{tot}"].value == cs["stock_total"] == 2
    xml = zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet2.xml").decode()
    assert not re.search(r"<f>[^<]*</f><v ?/>", xml)                          # önbelleksiz formül yok
    # Alım listesi GENEL TOPLAM'ı birebir aynı (stok değeri girmez)
    lv = wv[SHEET_LIST]
    gt = next(x for x in range(2, lv.max_row + 1) if lv.cell(x, 3).value == "GENEL TOPLAM (fiyatı belli olanlar)")
    assert lv[f"K{gt}"].value == res["pricing"]["totals"]["all"] == 83
    # Özet: GENEL TOPLAM'ın altında stoktan değer + PLANIN TOPLAM MALZEME MALİYETİ + sayılar
    oz = {r[0].value: r[1].value for r in wv["Özet"].iter_rows(min_row=2) if r[0].value}
    labels = [r[0].value for r in wv["Özet"].iter_rows(min_row=2)]
    i = labels.index("GENEL TOPLAM — fiyatı belli ($)")
    assert labels[i + 1:i + 4] == ["Stoktan karşılananların değeri — fiyatı belli ($)",
                                   "PLANIN TOPLAM MALZEME MALİYETİ ($)", "Fiyatlı / fiyatsız stoktan karşılanan"]
    assert oz["PLANIN TOPLAM MALZEME MALİYETİ ($)"] == 85 and oz["Fiyatlı / fiyatsız stoktan karşılanan"] == "1 / 1"
    assert any("yenileme değeri" in str(r[1].value) for r in wv["Özet"].iter_rows(min_row=2))


def test_xlsx_stock_sheet_open_order_columns():
    res = _report(open_orders=True)
    cs = res["pricing"]["cost_summary"]
    assert (cs["buy_total"], cs["stock_total"], cs["open_total"], cs["plan_total"]) == (74, 2, 9, 85)
    data = build_workbook(res)
    ws = openpyxl.load_workbook(io.BytesIO(data))[SHEET_STOCK]
    sv = openpyxl.load_workbook(io.BytesIO(data), data_only=True)[SHEET_STOCK]
    assert (ws["L1"].value, ws["M1"].value, ws["N1"].value) == ("Yoldan kullanılan", "Yoldan değer ($)",
                                                                "Fiyat kaynağı")
    rows = _sheet_rows(ws)
    g = rows["GLİSERİN"]
    assert ws.cell(g, 1).value == "Kısmen stoktan + yoldan"
    assert ws[f"L{g}"].value == f"=MIN(F{g},ROUND(D{g}-G{g},6))"
    assert ws[f"M{g}"].value == f'=IF(J{g}="","",ROUND(L{g}*J{g},0))'
    assert sv[f"L{g}"].value == pytest.approx(2.0) and sv[f"M{g}"].value == 9
    tot = rows["TOPLAM (fiyatı belli olanlar)"]
    assert sv[f"K{tot}"].value == 2 and sv[f"M{tot}"].value == cs["open_total"] == 9
    oz = {r[0].value: r[1].value for r in openpyxl.load_workbook(io.BytesIO(data), data_only=True)["Özet"]
          .iter_rows(min_row=2) if r[0].value}
    assert oz["Yoldan karşılananların değeri (açık sipariş) — fiyatı belli ($)"] == 9
    assert oz["PLANIN TOPLAM MALZEME MALİYETİ ($)"] == 85


def test_pdf_stock_value_lines_and_gross_mode_has_none():
    _, text = _pdf_text(render_pdf(_report()))
    assert "Değer" in text and "stoktan 2 $" in text                          # Tutar altı + Yeterli sütunu
    assert "Planın toplam malzeme maliyeti: 85 $" in text
    assert "Stoktan karşılananların değeri: 2 $" in text
    assert "Stoktan karşılanan 2 kalemden 1 tanesinin fiyatı yok; değeri toplama girmedi." in text
    assert "Yeterli olanlar (1 kalem — fiyatı yok)" in text
    assert "yenileme değeri" in text
    _, text = _pdf_text(render_pdf(_report(open_orders=True)))
    assert "yoldan 9 $" in text and "Yoldan karşılananların değeri (açık sipariş): 9 $" in text
    # Brüt mod: hiçbiri
    g = _report(mode="gross")
    _, text = _pdf_text(render_pdf(g))
    assert "Değer" not in text and "Planın toplam malzeme maliyeti" not in text
    assert not re.search(r"stoktan [\d.]+ \$", text) and "Stoktan karşılanan" not in text
    assert "yenileme değeri" not in text
    wb = openpyxl.load_workbook(io.BytesIO(build_workbook(g)))
    assert SHEET_STOCK not in wb.sheetnames
    assert not any("PLANIN" in str(c.value) for row in wb["Özet"].iter_rows() for c in row)


def test_xlsx_stock_sheet_unsafe_rounding_writes_number_not_min():
    """Düz yuvarlamada adet satırının stoktan kullanılanı gereken − alınacak
    (10,6 − 8 = 2,6); MIN(D,E) = 3 rapordan ayrışırdı → G sayı yazılır (Alım
    listesi G'sinin koruması gibi), K formülü kalır, TOPLAM = cost_summary."""
    items = [_it(1, "100 ML ŞİŞE", cat="Ambalaj", unit="adet", stock=3, pkg="şişe")]
    recipe = RecipeRec(id=1, name="Tonik", ingredients=(IngredientRec(item_id=1, quantity=1),))
    inp = PlanInputs(items={1: items[0]}, recipes={1: recipe}, stock_as_of=NOW)
    res = compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 10}], options={
        "safe_rounding": False, "extra_waste_pct": {"raw": 0, "packaging": 6, "label": 0}}))
    attach(res, PriceInputs(offers={1: [OfferRec(item_id=1, supplier_name="CAM", unit_price=10.0,
                                                 price_unit="adet", source="stok_son_durum")]}), None)
    m = _mat(res, "100 ML ŞİŞE")
    assert (m["stock_qty_used"], m["stock_value"]) == (pytest.approx(2.6), 26)
    data = build_workbook(res)
    ws = openpyxl.load_workbook(io.BytesIO(data))[SHEET_STOCK]
    sv = openpyxl.load_workbook(io.BytesIO(data), data_only=True)[SHEET_STOCK]
    r = _sheet_rows(ws)["100 ML ŞİŞE"]
    assert ws[f"G{r}"].value == pytest.approx(2.6)                              # sayı, MIN(D,E) değil
    assert ws[f"K{r}"].value == f'=IF(J{r}="","",ROUND(G{r}*J{r},0))' and sv[f"K{r}"].value == 26
    tot = _sheet_rows(ws)["TOPLAM (fiyatı belli olanlar)"]
    assert sv[f"K{tot}"].value == res["pricing"]["cost_summary"]["stock_total"] == 26


def test_cover_text_same_in_preview_and_pdf():
    """"stoktan X · yoldan Y" tek yardımcıdan (`cover_text`): önizlemenin
    `ui.cover_text`'i PDF'teki metinle birebir aynı."""
    from core.purchase_pricing import cover_text
    from routers.purchase_plan import _preview_view
    res = _report(open_orders=True)
    assert cover_text(_mat(res, "GLİSERİN"), "USD") == "stoktan 2 $ · yoldan 9 $"
    assert cover_text(_mat(res, "SHEA & BUTTER <özel>"), "USD") is None
    _, text = _pdf_text(render_pdf(res))
    assert "stoktan 2 $ · yoldan 9 $" in text and "stoktan: " not in text
    view = _preview_view(_report(open_orders=True))
    ui = _mat(view, "GLİSERİN")["ui"]
    assert ui["cover_text"] == "stoktan 2 $ · yoldan 9 $"
    assert (ui["stock_value"], ui["open_value"]) == ("2 $", "9 $")


# ─── Rusya kabul senaryosu (ortam değişkeniyle kapılı) ──────────────────────

@pytest.mark.skipif(not os.environ.get("RUSYA_FIXTURE_DIR"), reason="RUSYA_FIXTURE_DIR tanımlı değil (kabul testi)")
def test_rusya_scenario_renders_with_matching_totals():
    spec = importlib.util.spec_from_file_location(
        "_rusya_acceptance", Path(__file__).with_name("test_purchase_plan_acceptance.py"))
    acc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acc)
    res, _, _, has_xls = acc.run_plan(label_faces=acc._label_layout())
    if not has_xls:
        pytest.skip("STOK SON DURUM.xlsx yok")
    total = res["pricing"]["totals"]["all"]
    rd, text = _pdf_text(render_pdf(res))
    assert all(float(p.mediabox.width) > float(p.mediabox.height) for p in rd.pages)
    assert f"Fiyatı belli kalemlerin tutarı: {money(total)}" in text
    assert "Rusça etiket (119.520 adet)" in text
    wb = openpyxl.load_workbook(io.BytesIO(build_workbook(res)), data_only=True)[SHEET_LIST]
    tot = next(x for x in range(2, wb.max_row + 1) if wb.cell(x, 3).value == "GENEL TOPLAM (fiyatı belli olanlar)")
    assert wb[f"K{tot}"].value == total


# ─── "Aynı malzeme" grup stoğu (13.10.2026) ─────────────────────────────────

def _xhantan_report(qty=100):
    """Grup #5: 105 HAMMADDESEPETİ 180,31 g (reçetede) + 636 YİĞİTOGLU KİMYA
    24.687,81 g — grup stoğu sayılır, döküm stok hücresinin altında."""
    items = [_it(105, "XHANTAN GUM", stock=180.31), _it(636, "XANTHAN GUM", stock=24687.81)]
    recipe = RecipeRec(id=1, name="Jel", ingredients=(IngredientRec(item_id=105, quantity=2),))
    inp = PlanInputs(items={i.id: i for i in items}, recipes={1: recipe}, stock_as_of=NOW,
                     mgroup_of={105: 5, 636: 5}, mgroup_names={5: "XHANTAN GUM"}, card_supplier={105: 1, 636: 2},
                     supplier_names={1: "HAMMADDESEPETİ", 2: "YİĞİTOGLU KİMYA"})
    res = compute(inp, PlanRequest(title="Grup", lines=[{"recipe_id": 1, "qty": qty}]))
    sup = {1: SupplierRec(id=1, name="HAMMADDESEPETİ"), 2: SupplierRec(id=2, name="YİĞİTOGLU KİMYA")}
    return attach(res, PriceInputs(suppliers=sup, card_supplier={105: 1, 636: 2}), None, currency="USD")


def test_pdf_group_stock_breakdown_under_stock_cell():
    res = _xhantan_report()
    assert res["materials"][0]["status"] == "yeterli"
    _, text = _pdf_text(render_pdf(res))
    assert "24.868 gram" in text and "YİĞİTOGLU KİMYA 24,7 kg" in text          # "Yeterli olanlar" tablosu
    assert "Aynı malzeme: «XANTHAN GUM» (YİĞİTOGLU KİMYA, stok 24,7 kg) — stoğu sayıldı" not in text  # bölümde tedarikçi hücresi yok
    big = _xhantan_report(qty=20000)
    _, text = _pdf_text(render_pdf(big))
    assert "15,2 kg" in text and "YİĞİTOGLU KİMYA 24,7 kg" in text              # alım tablosu
    assert "Aynı malzeme: «XANTHAN GUM» (YİĞİTOGLU KİMYA, stok 24,7 kg) — stoğu sayıldı" in text


def test_xlsx_group_stock_column_aa_after_existing_columns():
    res = _xhantan_report(qty=20000)
    m = res["materials"][0]
    wb = openpyxl.load_workbook(io.BytesIO(build_workbook(res)))
    ws = wb[SHEET_LIST]
    assert [ws.cell(1, c).value for c in (24, 25, 26, 27)] == [
        "Not", "Aynı malzeme (diğer kartlar)", "Sipariş geçmişi", "Grup stoğu (kart dökümü)"]
    r = _row_of(ws, "XHANTAN GUM")
    assert ws.cell(r, 27).value == m["group_stock"]["note"] == "HAMMADDESEPETİ 180 g · YİĞİTOGLU KİMYA 24,7 kg"
    assert ws.cell(r, 25).value == "«XANTHAN GUM» (YİĞİTOGLU KİMYA, stok 24,7 kg) — stoğu sayıldı"
    assert ws.cell(r, 5).value == pytest.approx(24.8)                          # E = elimizde (grup dahil)
    assert ws[f"G{r}"].value == f"=MAX(0,ROUND(D{r}-E{r}-F{r},3))"            # formüller kaymadı
    assert ws.column_dimensions["AA"].width == 50


# ─── Ambalaj uyarısı + "Ürün maliyeti" / "Maliyet özeti" (16.10.2026) ───────

def test_xlsx_amount_warn_k_cell_orange_with_comment_total_unchanged():
    """GLİSERİN 9,5 kg ve SHEA 5 kg alınacak, fiyatlar 25 kg ambalaj için →
    K turuncu + uyarı hücre yorumu; formül ve GENEL TOPLAM aynı."""
    from core.purchase_plan_xlsx import WARN_COLOR
    from core.purchase_pricing import amount_warn
    res = _report()
    gl = _mat(res, "GLİSERİN")
    assert amount_warn(gl).startswith("Bu fiyat 25 kg'lık ambalaj için; alınacak 9,5 kg.")
    data = build_workbook(res)
    ws = openpyxl.load_workbook(io.BytesIO(data))[SHEET_LIST]
    wv = openpyxl.load_workbook(io.BytesIO(data), data_only=True)[SHEET_LIST]
    r = _row_of(ws, "GLİSERİN")
    k = ws[f"K{r}"]
    assert k.value == f'=IF(J{r}="","",ROUND(G{r}*J{r},0))' and wv[f"K{r}"].value == gl["amount"]
    assert k.font.color.rgb.endswith(WARN_COLOR) and k.comment.text == amount_warn(gl)
    assert amount_warn(gl) in ws.cell(r, 24).value                         # X "Not" da yazar
    ru = _row_of(ws, "GÜL ABSOLÜ")                                          # fiyatsız → işaret yok
    assert ws[f"K{ru}"].comment is None
    tot = next(x for x in range(2, ws.max_row + 1) if ws.cell(x, 3).value == "GENEL TOPLAM (fiyatı belli olanlar)")
    assert wv[f"K{tot}"].value == res["pricing"]["totals"]["all"] == 83


def test_xlsx_cost_sheet_formulas_cached_values_and_totals():
    from core.purchase_plan_xlsx import SHEET_COST
    from core.purchase_pricing import product_cost_lines
    res = _report()
    co = res["pricing"]["cost_overview"]
    p = co["products"][0]
    # Eksik: fiyatsız malzemeler + ürünün yeni etiketi (label_mode="new")
    assert p["total"] == 85 and p["unit_cost"] == pytest.approx(0.0852) and p["missing"] == 4
    assert p["missing_names"][-1].endswith("— Gece Kremi 50 ml")
    assert co["plan_total"] == res["pricing"]["cost_summary"]["plan_total"] == 85
    data = build_workbook(res)
    wb = openpyxl.load_workbook(io.BytesIO(data))
    wv = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    assert wb.sheetnames.index(SHEET_COST) == wb.sheetnames.index("Ürünler") + 1
    ws, sv = wb[SHEET_COST], wv[SHEET_COST]
    assert ws.freeze_panes == "C1"          # yalnız No / Ürün: iki blokta da aynı anlam
    assert [ws.cell(1, c).value for c in range(1, 8)] == ["No", "Ürün", "Adet", "1 adet maliyeti ($)", "Toplam ($)",
                                                          "Fiyatı eksik kalem", "Eksik kalemler"]
    assert (ws["A2"].value, ws["B2"].value, ws["C2"].value) == (1, "Gece Kremi 50 ml", 1000)
    sub = next(r for r in range(2, ws.max_row + 1) if ws.cell(r, 3).value == "1. Gece Kremi 50 ml — ara toplam")
    assert ws["D2"].value == f"=H{sub}" and ws["E2"].value == f"=K{sub}"
    assert sv["D2"].value == pytest.approx(p["unit_cost"]) and sv["E2"].value == 85
    assert ws["B3"].value == "TOPLAM" and ws["E3"].value == "=SUM(E2:E2)" and sv["E3"].value == co["products_total"]
    assert ws["G2"].value == ", ".join(p["missing_names"])
    # Döküm: H = IF(G="","",F*G), J = F*I, K = IF(G="","",J*G) — önbellek product_cost_lines ile aynı
    lines = product_cost_lines(res)
    rows = [r for r in range(2, sub) if ws.cell(r, 3).value in {x["name"] for x in lines} and ws.cell(r, 9).value]
    assert len(rows) == len(lines)
    for r, ln in zip(rows, lines):
        assert ws[f"H{r}"].value == f'=IF(G{r}="","",F{r}*G{r})' and ws[f"J{r}"].value == f"=F{r}*I{r}"
        assert ws[f"K{r}"].value == f'=IF(G{r}="","",J{r}*G{r})'
        assert ws[f"F{r}"].value == pytest.approx(ln["per_unit"]) and ws[f"I{r}"].value == 1000
        assert sv[f"J{r}"].value == pytest.approx(ln["total_qty"])
        if ln["total"] is None:                                                  # fiyatsız → boş sonuç
            assert sv[f"K{r}"].value in (None, "") and sv[f"H{r}"].value in (None, "")
        else:
            assert sv[f"K{r}"].value == pytest.approx(ln["total"])
    gl = next(r for r, ln in zip(rows, lines) if ln["name"] == "GLİSERİN")
    assert sv[f"H{gl}"].value == pytest.approx(0.0452) and sv[f"K{gl}"].value == pytest.approx(45.2)
    assert ws[f"H{sub}"].value == f"=SUM(H{rows[0]}:H{rows[-1]})"
    assert ws[f"K{sub}"].value == f"=ROUND(SUM(K{rows[0]}:K{rows[-1]}),0)" and sv[f"K{sub}"].value == 85
    # Tür bazında maliyet: TOPLAM = planın toplam malzeme maliyeti; eksik kalemler tam liste
    tr = next(r for r in range(2, ws.max_row + 1) if ws.cell(r, 2).value == "TOPLAM (planın toplam malzeme maliyeti)")
    assert ws.cell(tr, 7).value == 85 and ws.cell(tr, 9).value == co["total"]["unpriced"]
    texts = [ws.cell(r, 2).value for r in range(1, ws.max_row + 1)]
    assert f"Fiyatı eksik kalemler ({len(co['unpriced'])})" in texts and "=1+1 koli" in texts
    assert ws.cell(texts.index("=1+1 koli") + 1, 2).data_type == "s"           # serbest metin formül olmaz
    for n in zipfile.ZipFile(io.BytesIO(data)).namelist():                       # önbelleksiz formül yok
        if n.startswith("xl/worksheets/sheet"):
            assert not re.search(r"<f>[^<]*</f><v ?/>", zipfile.ZipFile(io.BytesIO(data)).read(n).decode()), n
    # Özet: yalnız yeni satırlar (eski sıra aynı), Alım listesi GENEL TOPLAM aynı
    oz = {r[0].value: r[1].value for r in wv["Özet"].iter_rows(min_row=2) if r[0].value}
    assert oz["Ürün başına maliyet toplamı ($)"] == 85 and oz["Ambalaj uyarılı satır"] == 2
    assert oz["Fiyatı eksik kalem (maliyet özeti)"] == co["total"]["unpriced"]
    labels = [r[0].value for r in wv["Özet"].iter_rows(min_row=2)]
    assert labels.index("Hariç kalem") < labels.index("Ürün başına maliyet toplamı ($)")


def test_pdf_cost_section_and_amount_warn_mark():
    res = _report()
    _, text = _pdf_text(render_pdf(res))
    assert "Maliyet özeti (1 ürün, 1.000 adet — malzeme maliyeti 85 $)" in text
    assert "Ürün başına maliyet" in text and "Gereken miktarlar" in text and "Fiyatı eksik kalemler (6)" in text
    assert "Gece Kremi 50 ml 1.000 0,09 $ 85 $" in text                      # 1 adet 0,0852 → 0,09
    assert "Bu fiyat 25 kg'lık ambalaj için; alınacak 9,5 kg." in text      # "Not:" satırı
    secs = [s["key"] for s in sections(res)]
    no = secs.index("cost") + 1
    assert f"{no}. Maliyet özeti" in text and f"{no - 1}. Ürünler, hedef adetler ve kapasite" in text
    # Brüt modda da bölüm var, stok değeri dili yok
    _, gtext = _pdf_text(render_pdf(_report(mode="gross")))
    assert "Maliyet özeti" in gtext and "Stoktan" not in gtext.split("Maliyet özeti", 1)[1].split("Notlar")[0]
