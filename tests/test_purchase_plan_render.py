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
Tedarikçiler K "İlişki türleri").
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
from core.purchase_plan_xlsx import SHEET_DETAIL, SHEET_LIST, SHEET_SUP, build_workbook
from core.purchase_pricing import (OfferRec, OrderRec, PriceInputs, ReceiptRec, SupplierRec, alt_texts, attach,
                                   money, order_texts)
from core.xlsx_cache import inject_cached_values

NOW = datetime(2026, 10, 5, 9, 0)
EVIL_NAME = '=HYPERLINK("http://evil.example","tıkla")'


def _it(id, name, *, cat="Hammadde", unit="g", stock=0.0, pkg=None):
    return ItemRec(id=id, name=name, category=cat, unit=unit, pkg_type=pkg, current_stock=stock)


def _report(*, mode="net", open_orders=False, with_packaging=False):
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
    assert wb.sheetnames == ["Alım listesi", "Tedarikçiler", "Ürünler", "Ürün detayı", "Özet"]
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
    allowed = re.compile(r'^(MAX\(0,ROUND\(D\d+-E\d+-F\d+,3\)\)|D\d+|IF\(J\d+="","",ROUND\(G\d+\*J\d+,0\)\)'
                         r'|SUM\([A-Z]+\d+:[A-Z]+\d+\)|K\d+(\+K\d+)*)$')
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
    assert ws.cell(_row_of(ws, "GLİSERİN"), 24).value == "Ambalaj katına yuvarlanırsa: 25,0 kg · 109 $."
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
