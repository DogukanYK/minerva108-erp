"""
Satın Alma Planı fiyat + tedarikçi katmanı (core/purchase_pricing.py) testleri.

Saf testler `compute()` çıktısını sentetik `PriceInputs` ile fiyatlandırır;
kur için `core.fx.Rates` doğrudan kurulur (ağa çıkılmaz).  Sondaki iki test
DB yükleyicisinin domain kapsamını sınar.
    MINERVA_TEST_DB=minerva_test_u .venv/bin/pytest tests/test_purchase_pricing.py -q

Kapsam: en ucuz önce / fiyatsız sonda · birim temeli (g→kg, ml→l, adet) ·
kur çevirisi (enjekte Rates) · round_half_up .5 · ambalaj katı · birleşik
kartların teklif birleşimi · ilişkiler (son alım numuneyi hariç tutar,
numuneler listelenir, atlanacak tedarikçiler) · supplier_key katlama ·
rehber + kontrol listesi · sections() numaralandırması ve notlar ·
firma dökümü (classify_input, lot sınıflaması: çevirmeyle birleşen alım
ALIM kalır, link_only çevrilmiş numune, taşınmış lotun alımı ilk kartın
Input'undan, aktarım), to_tr tarihleri (23:30 UTC = ertesi gün), sipariş
satırları ("+N"), analiz sonucu, "aynı malzeme" alternatifleri + aday
sebepleri, relation_texts / firms_view, DB yükleyicide girişler/siparişler/
analizler (domain kapsamı, eşdeğer kartın teklifi yüklenmez).
"""
from datetime import date, datetime

import pytest

from core.consumption import IngredientRec, ItemRec, RecipeRec
from core.fx import Rates
from core.purchase_plan import LotRec, PlanInputs, compute
from core.purchase_plan_models import PlanRequest
from core.purchase_pricing import (AnalysisRec, LotMetaRec, OfferRec, OrderRec, PriceInputs, ReceiptRec,
                                   SupplierIndex, SupplierRec, alt_texts, attach, classify_input, firms_view,
                                   load_price_inputs, money, order_texts, relation_texts, round_half_up,
                                   sections, supplier_key)

NOW = datetime(2026, 10, 5, 9, 0)


def it(id, name, *, cat="Hammadde", unit="g", stock=0.0, pkg=None):
    return ItemRec(id=id, name=name, category=cat, unit=unit, pkg_type=pkg, current_stock=stock)


def rec(id, ings, name="Ürün"):
    return RecipeRec(id=id, name=name, ingredients=tuple(IngredientRec(item_id=i, quantity=q) for i, q in ings))


def plan(items, ings, qty=1000, **opts):
    inp = PlanInputs(items={i.id: i for i in items}, recipes={1: rec(1, ings)}, stock_as_of=NOW)
    return compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": qty}], options=opts))


def offer(item_id, sup, price, *, pkg=None, cur="USD", unit="kg", sid=None, label="Stok Son Durum — Eylül"):
    return OfferRec(item_id=item_id, supplier_name=sup, supplier_id=sid, unit_price=price, package_size=pkg,
                    currency=cur, price_unit=unit, source="stok_son_durum", source_label=label,
                    quoted_at=date(2026, 9, 1))


def mat(res, item_id):
    return next(m for m in res["materials"] if item_id in m["member_ids"])


RATES = Rates(try_per={"USD": 40.0, "EUR": 44.0, "TRY": 1.0}, source="TCMB", as_of=date(2026, 10, 3),
              stale=False, warning=None)


# ─── round_half_up / biçim ──────────────────────────────────────────────────

@pytest.mark.parametrize("x,want", [(0.5, 1), (1.5, 2), (2.5, 3), (859.2, 859), (2.4999, 2),
                                    (107.4 * 8.0, 859), (0.145 * 100, 14), (-0.5, -1), (None, None)])
def test_round_half_up_matches_excel(x, want):
    assert round_half_up(x) == want


def test_money_format():
    assert money(7088, "USD") == "7.088 $" and money(12, "EUR") == "12 €" and money(None) == "—"


# ─── Teklifler ──────────────────────────────────────────────────────────────

def test_cheapest_first_unpriced_last_and_amount():
    res = plan([it(1, "SHEA BUTTER", unit="g")], [(1, 107.75)])     # 107.750 g gereken
    pin = PriceInputs(offers={1: [offer(1, "Pahalı", 9.0), offer(1, "Fiyatsız", None),
                                  offer(1, "Befchem", 8.0, pkg=25), offer(1, "Sıfır", 0)]})
    attach(res, pin, None, currency="USD")
    m = mat(res, 1)
    assert [o["name"] for o in m["offers"]][:2] == ["Befchem", "Pahalı"]
    assert all(o["price"] is None for o in m["offers"][2:])        # fiyatsız + 0 fiyat sonda
    assert m["group"] == "list" and m["price"] == 8.0 and m["supplier"] == "Befchem"
    assert m["display"]["buy_num"] == pytest.approx(107.8)
    assert m["amount"] == 862                                      # 107,8 × 8 = 862,4
    assert res["pricing"]["totals"]["raw"] == 862 and res["pricing"]["counts"]["raw_priced"] == 1


def test_unpriced_group_none_and_sufficient_rows_have_no_amount():
    res = plan([it(1, "A", unit="g"), it(2, "B", unit="g", stock=10 ** 6)], [(1, 1), (2, 1)])
    attach(res, PriceInputs(offers={2: [offer(2, "X", 5.0)]}), None)
    assert mat(res, 1)["group"] == "none" and mat(res, 1)["amount"] is None
    b = mat(res, 2)
    assert b["status"] == "yeterli" and b["group"] == "list" and b["amount"] is None


def test_basis_conversion_g_ml_adet():
    items = [it(1, "Yağ", unit="g"), it(2, "Hidrosol", unit="ml"), it(3, "Kapak", cat="Ambalaj", unit="adet"),
             it(4, "Sıvı kg", unit="kg")]
    res = plan(items, [(1, 2), (2, 3), (3, 1), (4, 0.004)])
    pin = PriceInputs(offers={1: [offer(1, "S", 10.0)], 2: [offer(2, "S", 4.0)],
                              3: [offer(3, "S", 2.0)],               # kg fiyatı adet kaleme → kullanılmaz
                              4: [offer(4, "S", 3.0, unit="l")]})
    attach(res, pin, None)
    g, ml, adet, kg = mat(res, 1), mat(res, 2), mat(res, 3), mat(res, 4)
    assert g["price"] == 10.0 and g["price_unit"] == "kg" and g["offers"][0]["note"] is None
    assert g["amount"] == round_half_up(2.0 * 10.0)
    assert ml["price"] == 4.0 and ml["price_unit"] == "l" and "1 l ≈ 1 kg" in ml["offers"][0]["note"]
    assert res["pricing"]["litre_kg_used"] is True
    assert adet["price"] is None and adet["group"] == "none" and "uyuşmuyor" in adet["offers"][0]["note"]
    assert kg["price"] == 3.0 and kg["price_unit"] == "kg"           # l fiyatı kg kaleme: 1 l ≈ 1 kg


def test_fx_conversion_with_injected_rates_and_missing_rates():
    res = plan([it(1, "Yağ", unit="g")], [(1, 2)])
    pin = PriceInputs(offers={1: [offer(1, "Avrupa", 10.0, cur="EUR"), offer(1, "Yerli", 450.0, cur="TRY")]})
    attach(res, pin, RATES, currency="USD")
    m = mat(res, 1)
    assert m["price"] == pytest.approx(11.0)                       # 10 € × 44 / 40
    assert m["offers"][1]["price"] == pytest.approx(11.25)         # 450 ₺ / 40
    assert m["offers"][0]["orig_currency"] == "EUR" and m["offers"][0]["currency"] == "USD"
    fx = res["pricing"]["fx"]
    assert fx["used"] and fx["currencies"] == ["EUR", "TRY"] and fx["try_per"]["USD"] == 40.0
    notes = sections(res)[-1]["rows"]
    assert any("1 EUR = 44,0000 ₺" in n and "TCMB" in n and "03.10.2026" in n for n in notes)
    res2 = plan([it(1, "Yağ", unit="g")], [(1, 2)])
    attach(res2, pin, None, currency="USD")
    assert mat(res2, 1)["group"] == "none" and "kur yok" in mat(res2, 1)["offers"][0]["note"]
    assert res2["pricing"]["fx"] is None


def test_round_to_package():
    res = plan([it(1, "Yağ", unit="g")], [(1, 107.75)], round_to_package=True)
    attach(res, PriceInputs(offers={1: [offer(1, "Befchem", 8.0, pkg=25)]}), None, round_to_package=True)
    m = mat(res, 1)
    assert m["pkg_buy"] == 125 and m["pkg_amount"] == 1000 and m["amount"] == 862
    assert res["pricing"]["round_to_package"] is True


def test_offers_union_over_merged_members():
    a = it(1, "ALOE VERA EKSTRAKTI", unit="ml")
    b = it(2, "ALOEVERA EKSTRAKTI", unit="ml")
    inp = PlanInputs(items={1: a, 2: b}, recipes={1: rec(1, [(1, 4.2)])}, stock_as_of=NOW)
    res = compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 1000}],
                                   options={"manual_merges": [[2, 1]]}))
    attach(res, PriceInputs(offers={2: [offer(2, "NATURALYA", 10.32), offer(2, "SURYA KİMYA", 16.34)]}), None)
    m = mat(res, 1)
    assert m["member_ids"] == [1, 2] and m["price"] == 10.32 and m["amount"] == 43   # 4,2 l × 10,32


def test_held_rows_priced_as_what_if_not_in_totals():
    res = plan([it(1, "Toz", unit="g")], [(1, 233.2)], held_items=[{"item_id": 1, "reason": "teyit"}])
    attach(res, PriceInputs(offers={1: [offer(1, "NATURALYA", 10.32, pkg=25)]}), None)
    h = res["held"][0]
    assert h["amount"] == 2407 and res["pricing"]["totals"]["all"] == 0
    assert res["pricing"]["held_what_if"] == 2407
    assert any("yaklaşık 2.407 $ tutar" in n for n in sections(res)[-1]["rows"])


# ─── İlişkiler ──────────────────────────────────────────────────────────────

def test_relations_last_purchase_excludes_samples_and_converted():
    res = plan([it(1, "Yağ", unit="g")], [(1, 1)])
    sup = {1: SupplierRec(id=1, name="TATLIDİLİMLER"), 2: SupplierRec(id=2, name="Naturalya"),
           3: SupplierRec(id=3, name="BİLİNMEYEN"), 4: SupplierRec(id=4, name="Doğasa")}
    lots = {1: [LotRec(item_id=1, lot_number="A", supplier_id=1, created_at=datetime(2026, 5, 5)),
                LotRec(item_id=1, lot_number="S", supplier_id=2, is_sample=True, created_at=datetime(2026, 8, 24)),
                LotRec(item_id=1, lot_number="C", supplier_id=4, created_at=datetime(2026, 9, 1)),
                LotRec(item_id=1, lot_number="U", supplier_id=3, created_at=datetime(2026, 9, 20))]}
    pin = PriceInputs(suppliers=sup, card_supplier={1: 3}, lots=lots, converted_lots={(1, "C")})
    attach(res, pin, None)
    rel = mat(res, 1)["relations"]
    assert rel["card"] == []                                        # BİLİNMEYEN atlanır
    assert rel["last"] == {"name": "TATLIDİLİMLER", "key": "TATLIDILIMLER", "date": "05.05.2026"}
    assert [(s["name"], s["date"]) for s in rel["samples"]] == [("Doğasa", "01.09.2026"),
                                                                ("Naturalya", "24.08.2026")]
    cand = res["suppliers"]["candidates"]
    reasons = {b["name"]: b["items"][0]["reasons"] for b in cand}
    assert reasons == {"TATLIDİLİMLER": ["son alım 05.05.2026"], "Naturalya": ["numune 24.08.2026"],
                       "Doğasa": ["numune 01.09.2026"]}


def test_skip_list_from_price_inputs():
    res = plan([it(1, "Yağ", unit="g")], [(1, 1)])
    pin = PriceInputs(suppliers={1: SupplierRec(id=1, name="MİNERVA 108")}, card_supplier={1: 1},
                      skip_suppliers=("MİNERVA",))
    attach(res, pin, None)
    assert mat(res, 1)["relations"]["card"] == []
    assert [u["name"] for u in res["suppliers"]["unrelated"]] == ["Yağ"]
    pin.skip_suppliers = ()
    attach(res, pin, None)
    assert mat(res, 1)["relations"]["card"][0]["name"] == "MİNERVA 108"


# ─── supplier_key ───────────────────────────────────────────────────────────

def test_supplier_key_folding():
    assert supplier_key("ULUDAĞ HERBAL") == supplier_key("ULUDAG HERBAL")
    assert supplier_key("NATURALYA DOĞAL ÜRÜNLER SAN. TİC. LTD. ŞTİ.") == supplier_key("Naturalya") == "NATURALYA"
    assert supplier_key("NİLKİM TEKNİK KİMYA") == supplier_key("NILKIM") == "NILKIM"
    assert supplier_key("UMAYCHEM, BAŞAK ORGANİK") == supplier_key("umaychem")
    assert supplier_key("SURYA KİMYA") == supplier_key("SURYA")
    assert supplier_key("VESER KİMYEVİ") == supplier_key("VESER KİMYA")
    assert supplier_key("İSTANBUL KİMYA") != supplier_key("İSTANBUL AMBALAJ")
    assert supplier_key("") == "" and supplier_key(None) == ""
    ix = SupplierIndex(["HAMMADDESEPETİ", "HAMMADDE SEPETİ", "HAMMADDELER.COM", "KRK GIDA", "KRK GIDA (HAYAT)"])
    assert ix.key("HAMMADDESEPETİ") == ix.key("Hammadde Sepeti")
    assert ix.key("HAMMADDELER.COM") != ix.key("HAMMADDE SEPETİ")
    assert ix.key("KRK GIDA") != ix.key("KRK GIDA (HAYAT)")


# ─── Rehber + kontrol listesi ───────────────────────────────────────────────

def test_directory_and_checklist():
    items = [it(1, "Shea", unit="g"), it(2, "Coco", unit="g"), it(3, "Kil", unit="g"), it(4, "Tuz", unit="g")]
    res = plan(items, [(1, 10), (2, 20), (3, 1), (4, 1)], checklist_owner="Satın alma ekibi")
    sup = {1: SupplierRec(id=1, name="UMAYCHEM", phone="05537953163", email="info@umaychem.com",
                          contact_person="Semih"),
           2: SupplierRec(id=2, name="UMAYCHEM, BAŞAK ORGANİK", address="İstanbul"),
           3: SupplierRec(id=3, name="ROSECE")}
    pin = PriceInputs(offers={1: [offer(1, "UMAYCHEM", 2.0)], 2: [offer(2, "Befchem", 1.0)]},
                      suppliers=sup, card_supplier={3: 3})
    attach(res, pin, None)
    d = res["suppliers"]
    # eşit tutarda ada göre: Befchem < UMAYCHEM
    assert [(b["name"], b["count"], b["total"]) for b in d["chosen"]] == [("Befchem", 1, 20), ("UMAYCHEM", 1, 20)]
    um = next(b for b in d["chosen"] if b["name"] == "UMAYCHEM")
    assert um["contact_line"] == "Semih · 0553 795 31 63 · info@umaychem.com · İstanbul"
    assert [b["name"] for b in d["candidates"]] == ["ROSECE"]
    assert d["candidates"][0]["items"][0]["reasons"] == ["stok kartında yazan"]
    assert [u["name"] for u in d["unrelated"]] == ["Tuz"]
    chk = d["checklist"]
    assert {s["name"] for s in chk["missing_contact"]} == {"Befchem", "ROSECE"}
    assert [s["name"] for s in chk["no_card"]] == ["Befchem"]           # yalnız fiyat listesinde serbest metin
    assert chk["duplicate_cards"] == [{"key": "UMAYCHEM", "name": "UMAYCHEM",
                                       "cards": ["UMAYCHEM", "UMAYCHEM, BAŞAK ORGANİK"]}]


# ─── sections() ─────────────────────────────────────────────────────────────

def test_sections_order_numbering_and_skip_empty():
    items = [it(1, "Shea", unit="g"), it(2, "Kil", unit="g"),
             it(3, "Kavanoz", cat="Ambalaj", unit="adet", pkg="kavanoz", stock=5),
             it(4, "Yeterli", unit="g", stock=10 ** 7)]
    inp = PlanInputs(items={i.id: i for i in items}, recipes={1: rec(1, [(1, 10), (2, 1), (3, 1), (4, 1)])},
                     stock_as_of=NOW)
    r = PlanRequest(lines=[{"recipe_id": 1, "qty": 100}],
                    manual_lines=[{"name": "Euro palet", "qty": 2}],
                    options={"checklist_owner": "Ekip", "notes": ["Fiyatlar teyit edilecek."],
                             "label_mode": "new", "new_label_title": "Rusça etiket"})
    res = compute(inp, r)
    attach(res, PriceInputs(offers={1: [offer(1, "Befchem", 8.0)]},
                            suppliers={9: SupplierRec(id=9, name="Befchem")}), None)
    secs = sections(res)
    keys = [s["key"] for s in secs]
    assert keys == ["raw_priced", "raw_unpriced", "pkg_unpriced", "new_items", "suppliers", "candidates",
                    "checklist", "products", "sufficient", "notes"]
    assert [s["no"] for s in secs] == list(range(1, len(secs) + 1))
    assert secs[0]["title"] == "Hammadde — fiyat listesinde olanlar" and secs[0]["total"] == 8
    assert secs[0]["summary"] == "1 kalem, toplam 8 $"
    new = next(s for s in secs if s["key"] == "new_items")
    assert [r_["type"] for r_ in new["rows"]] == ["label", "manual"]
    assert new["rows"][1]["qty_text"] == "2 adet"
    chk = next(s for s in secs if s["key"] == "checklist")
    assert chk["title"] == "Tedarikçi bilgi eksikleri (Ekip için)"
    notes = secs[-1]["rows"]
    assert notes[0].startswith("Fiyat kaynağı: Stok Son Durum — Eylül (01.09.2026); birim fiyatlar $/kg.")
    assert "Fiyatlar teyit edilecek." in notes
    assert any("güvenli yuvarlandı" in n for n in notes)
    # brüt modda 'Yeterli olanlar' yok
    g = compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 100}], options={"stock_mode": "gross"}))
    attach(g, PriceInputs(), None)
    assert "sufficient" not in [s["key"] for s in sections(g)]


def test_sections_raw_priced_sorted_by_amount_unpriced_by_buy():
    items = [it(1, "Az", unit="g"), it(2, "Çok", unit="g"), it(3, "Fiyatsız küçük", unit="g"),
             it(4, "Fiyatsız büyük", unit="g")]
    res = plan(items, [(1, 1), (2, 5), (3, 1), (4, 9)])
    attach(res, PriceInputs(offers={1: [offer(1, "S", 100.0)], 2: [offer(2, "S", 1.0)]}), None)
    secs = {s["key"]: s for s in sections(res)}
    assert [m["name"] for m in secs["raw_priced"]["rows"]] == ["Az", "Çok"]        # 100 $ > 5 $
    assert [m["name"] for m in secs["raw_unpriced"]["rows"]] == ["Fiyatsız büyük", "Fiyatsız küçük"]


def test_sections_without_pricing_still_numbered():
    res = plan([it(1, "A", unit="g")], [(1, 1)])
    secs = sections(res)
    assert secs[0]["key"] == "raw_unpriced" and secs[0]["no"] == 1
    assert secs[-1]["key"] == "notes"


# ─── DB yükleyici ───────────────────────────────────────────────────────────

def test_load_price_inputs_domain_scoped(db_session):
    from database import AppSetting, Inventory, Item, Supplier, SupplierPrice, Transaction
    s1 = Supplier(name="Befchem", phone="0212", domain="cosmetics")
    s2 = Supplier(name="Takviye AŞ", domain="supplement")
    db_session.add_all([s1, s2])
    db_session.flush()
    i1 = Item(name="Shea", category="Hammadde", unit="g", supplier_id=s1.id, domain="cosmetics")
    i2 = Item(name="Kapsül", category="Hammadde", unit="adet", supplier_id=s2.id, domain="supplement")
    db_session.add_all([i1, i2])
    db_session.flush()
    db_session.add_all([
        SupplierPrice(item_id=i1.id, supplier_id=s1.id, supplier_name="Befchem", unit_price=8.0,
                      currency="USD", price_unit="kg", domain="cosmetics"),
        SupplierPrice(item_id=i1.id, supplier_name="Sızıntı", unit_price=1.0, domain="supplement"),
        Inventory(item_id=i1.id, supplier_id=s1.id, lot_number="L1", quantity=1, domain="cosmetics"),
        Inventory(item_id=i1.id, supplier_id=s1.id, lot_number="N1", quantity=1, is_sample=True,
                  domain="cosmetics"),
        Transaction(item_id=i1.id, lot_number="N1", transaction_type="Input", quantity=1,
                    notes="Numune stoğa çevrildi — Lot: N1"),
        AppSetting(key="purchase_plan.skip_suppliers", value="FOO, BAR"),
    ])
    db_session.commit()
    pin = load_price_inputs(db_session, [i1.id, i2.id], "cosmetics")
    assert [o.supplier_name for o in pin.offers[i1.id]] == ["Befchem"]
    assert set(pin.suppliers) == {s1.id}
    assert pin.card_supplier == {i1.id: s1.id}                     # panel dışı kart yok
    assert {l.lot_number for l in pin.lots[i1.id]} == {"L1", "N1"}
    assert (i1.id, "N1") in pin.converted_lots
    assert pin.skip_suppliers == ("FOO", "BAR")


def test_load_price_inputs_default_skip_and_empty_ids(db_session):
    pin = load_price_inputs(db_session, [], "cosmetics")
    assert pin.offers == {} and pin.skip_suppliers == ("BİLİNMEYEN", "MİNERVA", "NUMUNE GÖNDERİM")


# ─── Firma dökümü + lot sınıflaması ─────────────────────────────────────────

def test_classify_input_kinds():
    assert classify_input("Mal kabul — Lot: A, Konum: Raf 3", "A") == "purchase"
    assert classify_input("Numune stoğa çevrildi — Lot: N | Tedarikçi: X | Kart: A → B", "N") == "converted"
    assert classify_input("Excel içe aktarım — SKU: 12", "IMP-20260101-000000-5") == "import"
    assert classify_input("Excel Bulk Import — Sheet: HAMMADDE · Raw: '5'", "XLS-20260101-5") == "import"
    assert classify_input("Bulk Import — stok.xlsx", None) == "import"
    assert classify_input("Stok girişi", "IMP-1") == "import"
    assert classify_input("İade (IR-2026-1) ← Mağaza | Sağlam", None) == "return"
    assert classify_input("Üretim çıktısı — Reçete: Krem | Dil: TR | Lot: MNR1", "MNR1") == "production"
    assert classify_input("Lot taşındı ← «JOJOBA» (id 185) | Lot: MİNERVA", "MİNERVA") == "move"
    assert classify_input(None, None) == "other" and classify_input("Elle giriş", "L") == "other"


JOJ_SUP = {1: SupplierRec(id=1, name="KRK GIDA"), 2: SupplierRec(id=2, name="NATURALYA"),
           3: SupplierRec(id=3, name="DOĞASA"), 4: SupplierRec(id=4, name="ESKİ FİRMA"),
           5: SupplierRec(id=5, name="AKTARIM AŞ"), 6: SupplierRec(id=6, name="BİLİNMEYEN")}


def _jojoba_pin(**kw):
    lots = {1: [
        # gerçek alım; 05.10'da aynı lot no'lu numune çevrilip BİRLEŞTİ (converted Input da var)
        LotRec(item_id=1, lot_number="MİNERVA", supplier_id=1, created_at=datetime(2026, 8, 1, 7), quantity=5710,
               inventory_id=10),
        LotRec(item_id=1, lot_number="NUM-1", supplier_id=2, created_at=datetime(2026, 8, 24, 9), quantity=50,
               inventory_id=11),                                   # çevirme Input'u
        LotRec(item_id=1, lot_number="NUM-2", supplier_id=3, created_at=datetime(2026, 9, 10, 9), quantity=20,
               inventory_id=12),                                   # link_only: yalnız sample_converted_at
        LotRec(item_id=1, lot_number="OLD", supplier_id=4, created_at=datetime(2026, 3, 3, 9), quantity=1,
               inventory_id=13),                                   # Input'u yok → eski alım
        LotRec(item_id=1, lot_number="IMP-20260101-1", supplier_id=5, created_at=datetime(2026, 1, 1, 9),
               inventory_id=14),
        LotRec(item_id=1, lot_number="X", supplier_id=6, created_at=datetime(2026, 9, 30, 9), inventory_id=15),
    ]}
    rc = {1: [ReceiptRec(1, "MİNERVA", 5000, datetime(2026, 8, 1, 8), "purchase"),
              ReceiptRec(1, "MİNERVA", 710, datetime(2026, 9, 2, 8), "purchase"),
              ReceiptRec(1, "MİNERVA", 50, datetime(2026, 10, 5, 5, 40), "converted"),
              ReceiptRec(1, "NUM-1", 50, datetime(2026, 10, 5, 5, 41), "converted"),
              ReceiptRec(1, "IMP-20260101-1", 100, datetime(2026, 1, 1, 9), "import")]}
    kw.setdefault("lot_meta", {12: LotMetaRec(sample_converted_at=datetime(2026, 10, 5, 9))})
    return PriceInputs(suppliers=JOJ_SUP, card_supplier={1: 1}, lots=lots, receipts=rc,
                       converted_lots={(1, "MİNERVA"), (1, "NUM-1")}, **kw)


def test_lot_classification_and_firm_types():
    res = plan([it(1, "JOJOBA YAĞI", unit="g")], [(1, 1)])
    attach(res, _jojoba_pin(analyses={1: [AnalysisRec(item_id=1, inventory_id=11, lot="NUM-1", result="uygun",
                                                       document_no="NA-2026-00012",
                                                       at=datetime(2026, 9, 1))]}), None)
    rel = mat(res, 1)["relations"]
    # eski şekil korunur; çevirmeyle birleşen gerçek alım NUMUNE sayılmaz
    assert rel["card"] == [{"name": "KRK GIDA", "key": "KRKGIDA", "supplier_id": 1}]
    assert rel["last"] == {"name": "KRK GIDA", "key": "KRKGIDA", "date": "02.09.2026"}
    assert [(x["name"], x["date"]) for x in rel["samples"]] == [("DOĞASA", "10.09.2026"),
                                                                ("NATURALYA", "24.08.2026")]
    firms = {f["name"]: f for f in rel["firms"]}
    assert [f["name"] for f in rel["firms"]] == ["KRK GIDA", "ESKİ FİRMA", "DOĞASA", "NATURALYA", "AKTARIM AŞ"]
    krk = firms["KRK GIDA"]
    assert krk["types"] == ["card", "purchase", "sample"]
    assert krk["purchases"] == {"count": 1, "last_date": "02.09.2026", "last_qty_text": "5,7 kg"}
    assert krk["samples"] == [{"lot": "MİNERVA", "date": "05.10.2026", "qty_text": "50 gram", "converted": True,
                               "analysis": None}]
    nat = firms["NATURALYA"]["samples"][0]
    assert nat["converted"] is True and nat["qty_text"] == "50 gram"
    assert nat["analysis"] == {"result": "uygun", "result_text": "uygun", "doc_no": "NA-2026-00012"}
    assert firms["DOĞASA"]["samples"][0]["converted"] is True             # link_only → çevrilmiş numune
    assert firms["ESKİ FİRMA"]["purchases"] == {"count": 1, "last_date": "03.03.2026", "last_qty_text": ""}
    assert firms["AKTARIM AŞ"]["types"] == ["import"]
    assert firms["AKTARIM AŞ"]["imports"] == {"count": 1, "last_date": "01.01.2026"}
    assert "BİLİNMEYEN" not in firms                                      # skip listesi korunur
    # aday sebepleri: son alım + önceki alım firması + aktarım
    reasons = {b["name"]: it_["reasons"] for b in res["suppliers"]["candidates"] for it_ in b["items"]}
    assert reasons["KRK GIDA"] == ["stok kartında yazan", "son alım 02.09.2026"]
    assert reasons["ESKİ FİRMA"] == ["alım 03.03.2026"]
    assert reasons["AKTARIM AŞ"] == ["stok aktarımı 01.01.2026"]
    assert reasons["NATURALYA"] == ["numune 24.08.2026"]
    assert relation_texts(mat(res, 1))[0] == (
        "Stok kartında yazan: KRK GIDA (son alım 02.09.2026) · Numune gönderdi: DOĞASA (10.09.2026) · "
        "Numune gönderdi: NATURALYA (24.08.2026, analiz uygun)")


def test_dates_are_tr_local_and_sample_analysis_by_lot_snapshot():
    res = plan([it(1, "Yağ", unit="ml")], [(1, 1)])
    lots = {1: [LotRec(item_id=1, lot_number="S1", supplier_id=2, is_sample=True, quantity=30,
                       created_at=datetime(2026, 10, 5, 23, 30), inventory_id=5)]}
    an = {1: [AnalysisRec(item_id=1, inventory_id=None, lot="S1", result=None, document_no="NA-2026-00020")]}
    attach(res, PriceInputs(suppliers=JOJ_SUP, lots=lots, analyses=an), None)
    rel = mat(res, 1)["relations"]
    assert rel["samples"] == [{"name": "NATURALYA", "key": "NATURALYA", "date": "06.10.2026"}]   # 23:30 UTC
    s = rel["firms"][0]["samples"][0]
    assert s == {"lot": "S1", "date": "06.10.2026", "qty_text": "30 ml", "converted": False,
                 "analysis": {"result": None, "result_text": "beklemede", "doc_no": "NA-2026-00020"}}


def test_moved_lot_purchase_found_on_origin_card():
    res = plan([it(2, "JOJOBA YAĞI — NATURALYA", unit="g")], [(2, 1)])
    lots = {2: [LotRec(item_id=2, lot_number="L9", supplier_id=2, created_at=datetime(2026, 8, 24, 9),
                       quantity=400, inventory_id=20)]}
    rc = {1: [ReceiptRec(1, "L9", 1000, datetime(2026, 8, 24, 10), "purchase")]}
    meta = {20: LotMetaRec(moved_from_item_id=1)}
    attach(res, PriceInputs(suppliers=JOJ_SUP, lots=lots, receipts=rc, lot_meta=meta), None)
    f = mat(res, 2)["relations"]["firms"][0]
    assert f["types"] == ["purchase"] and f["purchases"]["last_qty_text"] == "1,0 kg"
    # iz yoksa Input'u bilinmeyen eski alım (created_at tarihli)
    attach(res, PriceInputs(suppliers=JOJ_SUP, lots=lots, receipts=rc), None)
    f = mat(res, 2)["relations"]["firms"][0]
    assert f["purchases"] == {"count": 1, "last_date": "24.08.2026", "last_qty_text": ""}


def test_own_card_conversion_beats_unrelated_origin_purchase():
    """185'te KRK'nın 'MİNERVA' alım lotu var; Naturalya numunesi de 'MİNERVA'
    lot no'suyla 185'e girilip hedef 920 seçilerek çevrildi (moved_from=185,
    çevirme Input'u 920'de).  Kendi karttaki çevirme kanıtı ilk karttaki
    ilgisiz alımdan önce gelir → NUMUNE; "Son alım: NATURALYA" yazılmaz."""
    res = plan([it(920, "JOJOBA YAĞI (NUMUNE)", unit="g")], [(920, 1)])
    conv_at = datetime(2026, 10, 5, 5, 40)
    lots = {920: [LotRec(item_id=920, lot_number="MİNERVA", supplier_id=2, created_at=datetime(2026, 8, 24, 9),
                         quantity=50, inventory_id=30)]}
    rc = {185: [ReceiptRec(185, "MİNERVA", 5000, datetime(2026, 6, 1, 8), "purchase")],
          920: [ReceiptRec(920, "MİNERVA", 50, conv_at, "converted")]}
    meta = {30: LotMetaRec(moved_from_item_id=185, sample_converted_at=conv_at)}
    attach(res, PriceInputs(suppliers=JOJ_SUP, lots=lots, receipts=rc, lot_meta=meta), None)
    rel = mat(res, 920)["relations"]
    assert rel["last"] is None
    assert [f["name"] for f in rel["firms"]] == ["NATURALYA"]
    nat = rel["firms"][0]
    assert nat["types"] == ["sample"] and nat["purchases"]["count"] == 0
    assert nat["samples"] == [{"lot": "MİNERVA", "date": "24.08.2026", "qty_text": "50 gram",
                               "converted": True, "analysis": None}]
    # Tam taşınmış birleşik lot (ilk kartta alım + çevirme, kendi kartta Input yok) yine ALIM
    lots2 = {920: [LotRec(item_id=920, lot_number="MİNERVA", supplier_id=1, created_at=datetime(2026, 6, 1, 8),
                          quantity=5050, inventory_id=31)]}
    rc2 = {185: [ReceiptRec(185, "MİNERVA", 5000, datetime(2026, 6, 1, 8), "purchase"),
                 ReceiptRec(185, "MİNERVA", 50, conv_at, "converted")]}
    meta2 = {31: LotMetaRec(moved_from_item_id=185, sample_converted_at=conv_at)}
    attach(res, PriceInputs(suppliers=JOJ_SUP, lots=lots2, receipts=rc2, lot_meta=meta2), None)
    rel = mat(res, 920)["relations"]
    assert rel["last"] == {"name": "KRK GIDA", "key": "KRKGIDA", "date": "01.06.2026"}
    krk = rel["firms"][0]
    assert krk["types"] == ["purchase", "sample"] and krk["purchases"]["last_qty_text"] == "5,0 kg"
    # Taşımada lot no değişti (MİNERVA → MİNERVA-2): (kart, lot) Input'u bulunmaz,
    # iz `sample_converted_at`'te → yine çevrilmiş numune, "eski alım" değil
    lots3 = {920: [LotRec(item_id=920, lot_number="MİNERVA-2", supplier_id=2,
                          created_at=datetime(2026, 8, 24, 9), quantity=10, inventory_id=32)]}
    rc3 = {221: [ReceiptRec(221, "MİNERVA", 10, conv_at, "converted")]}
    meta3 = {32: LotMetaRec(moved_from_item_id=221, sample_converted_at=conv_at)}
    attach(res, PriceInputs(suppliers=JOJ_SUP, lots=lots3, receipts=rc3, lot_meta=meta3), None)
    rel = mat(res, 920)["relations"]
    assert rel["last"] is None and rel["firms"][0]["types"] == ["sample"]
    assert rel["firms"][0]["samples"][0]["converted"] is True


def test_orders_firms_texts_and_cap():
    res = plan([it(1, "SETİL STEARİL ALKOL", unit="g")], [(1, 1)])
    orders = {1: [
        OrderRec(1, supplier_id=1, supplier_name="KRK GIDA", quantity=25000, unit="g",
                 ordered_at=datetime(2026, 10, 1, 7), expected_date=date(2026, 10, 10)),
        OrderRec(1, supplier_id=1, supplier_name="KRK GIDA", quantity=10000, unit="g",
                 ordered_at=datetime(2026, 8, 1, 7), closed_at=datetime(2026, 8, 5), closed_reason="received"),
        OrderRec(1, supplier_id=3, supplier_name="DOĞASA", ordered_at=datetime(2026, 7, 1, 7),
                 closed_at=datetime(2026, 7, 2), closed_reason="manual"),
        OrderRec(1, ordered_at=datetime(2026, 6, 1, 7), closed_at=datetime(2026, 6, 2), closed_reason="manual"),
        OrderRec(1, supplier_id=6, supplier_name="BİLİNMEYEN", ordered_at=datetime(2026, 5, 1, 7),
                 closed_at=datetime(2026, 5, 2), closed_reason="received")]}
    attach(res, PriceInputs(suppliers=JOJ_SUP, orders=orders), None)
    m = mat(res, 1)
    rel = m["relations"]
    assert [o["status"] for o in rel["orders"]] == ["open", "received", "manual", "manual", "received"]
    assert order_texts(m)[:4] == ["KRK GIDA 01.10.2026 · 25,0 kg · açık · beklenen 10.10.2026",
                                  "KRK GIDA 01.08.2026 · 10,0 kg · teslim alındı",
                                  "DOĞASA 01.07.2026 · kapatıldı",
                                  "firma yazılmamış 01.06.2026 · kapatıldı"]
    assert relation_texts(m) == ["Sipariş: " + t for t in order_texts(m)[:3]] + ["+2 sipariş daha"]
    assert len(relation_texts(m, cap=None)) == 5
    firms = {f["name"]: f for f in rel["firms"]}
    assert set(firms) == {"KRK GIDA", "DOĞASA"}                       # firmasız + BİLİNMEYEN firma olmaz
    assert firms["KRK GIDA"]["types"] == ["order"] and len(firms["KRK GIDA"]["orders"]) == 2
    reasons = {b["name"]: it_["reasons"] for b in res["suppliers"]["candidates"] for it_ in b["items"]}
    assert reasons == {"KRK GIDA": ["sipariş 01.10.2026"], "DOĞASA": ["sipariş 01.07.2026"]}
    assert res["suppliers"]["unrelated"] == []


STEARYL_SUP = {1: SupplierRec(id=1, name="TATLIDİLİMLER"), 2: SupplierRec(id=2, name="YİĞİTOGLU KİMYA"),
               3: SupplierRec(id=3, name="VESER KİMYEVİ"), 4: SupplierRec(id=4, name="BİLİNMEYEN")}


def _stearyl(used=126, offers=None):
    items = [it(126, "SETİL STEARİL ALKOL", unit="g", stock=3944),
             it(599, "CETYL STEARYL ALCOHOL", unit="adet"), it(593, "CETEARYL ALCOHOL", unit="adet"),
             it(600, "STEARİL ALKOL ESKİ", unit="g", stock=0)]
    inp = PlanInputs(items={i.id: i for i in items}, recipes={1: rec(1, [(used, 40)])}, stock_as_of=NOW,
                     mgroup_of={126: 7, 599: 7, 593: 7, 600: 7}, mgroup_names={7: "Setil stearil alkol"})
    res = compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 1000}]))
    lots = {126: [LotRec(item_id=126, lot_number="TD-1", supplier_id=1, created_at=datetime(2026, 8, 12, 7),
                         quantity=3944, inventory_id=1)],
            599: [LotRec(item_id=599, lot_number="YK-1", supplier_id=2, created_at=datetime(2026, 7, 20, 7),
                         inventory_id=2)],
            593: [LotRec(item_id=593, lot_number="VS-N", supplier_id=3, is_sample=True, quantity=1,
                         created_at=datetime(2026, 9, 15, 7), inventory_id=3)]}
    rc = {126: [ReceiptRec(126, "TD-1", 25000, datetime(2026, 8, 12, 7), "purchase")],
          599: [ReceiptRec(599, "YK-1", 10, datetime(2026, 7, 20, 7), "purchase")]}
    pin = PriceInputs(suppliers=STEARYL_SUP, card_supplier={126: 1, 599: 2, 593: 3, 600: 4}, lots=lots,
                      receipts=rc, offers=offers or {})
    return attach(res, pin, None)


def test_alternatives_from_material_group_and_candidate_reasons():
    res = _stearyl()
    m = mat(res, 126)
    assert m["need"] == pytest.approx(40000) and m["display"]["buy_text"] == "36,1 kg"   # ihtiyaç değişmez
    rel = m["relations"]
    assert rel["group"] == {"id": 7, "name": "Setil stearil alkol"}
    alts = {a["item_id"]: a for a in rel["alternatives"]}
    assert [a["item_id"] for a in rel["alternatives"]] == [593, 599, 600]
    assert alts[599]["supplier"] == "YİĞİTOGLU KİMYA" and alts[599]["unit_mismatch"] is True
    assert alts[599]["stock_text"] == "yok" and alts[599]["last"]["date"] == "20.07.2026"
    assert alts[593]["sample_text"] == "1 adet numune"
    assert alts[593]["samples"] == [{"name": "VESER KİMYEVİ", "key": "VESER", "date": "15.09.2026"}]
    assert alts[600]["supplier"] is None and alts[600]["unit_mismatch"] is False   # BİLİNMEYEN ilişki değil
    assert alt_texts(m) == [
        "«CETEARYL ALCOHOL» (VESER KİMYEVİ, stok yok, birimi adet, numune 15.09.2026, elde 1 adet numune)",
        "«CETYL STEARYL ALCOHOL» (YİĞİTOGLU KİMYA, stok yok, birimi adet, son alım 20.07.2026)",
        "«STEARİL ALKOL ESKİ» (tedarikçi yazılmamış, stok yok)"]
    assert relation_texts(m) == ["Stok kartında yazan: TATLIDİLİMLER (son alım 12.08.2026)"] + [
        "Aynı malzeme: " + t for t in alt_texts(m)]
    reasons = {b["name"]: it_["reasons"] for b in res["suppliers"]["candidates"] for it_ in b["items"]}
    assert reasons == {"TATLIDİLİMLER": ["stok kartında yazan", "son alım 12.08.2026"],
                       "YİĞİTOGLU KİMYA": ["eşdeğer kart «CETYL STEARYL ALCOHOL»",
                                           "eşdeğer kartta son alım 20.07.2026"],
                       "VESER KİMYEVİ": ["eşdeğer kart «CETEARYL ALCOHOL»", "eşdeğer kartta numune 15.09.2026"]}
    types = {b["name"]: b["types"] for b in res["suppliers"]["candidates"]}
    assert types == {"TATLIDİLİMLER": ["card", "purchase"], "YİĞİTOGLU KİMYA": ["equivalent"],
                     "VESER KİMYEVİ": ["equivalent"]}
    secs = {s["key"]: s for s in sections(res)}
    assert "eşdeğer kart" in secs["candidates"]["subtitle"]
    assert any(n.startswith("“Aynı malzeme” grubundaki diğer tedarikçi kartları") for n in secs["notes"]["rows"])


def test_material_with_only_group_alternative_is_not_unrelated():
    res = attach(plan([it(1, "Kil", unit="g")], [(1, 1)]), PriceInputs(suppliers=STEARYL_SUP), None)
    assert [u["name"] for u in res["suppliers"]["unrelated"]] == ["Kil"]
    items = [it(1, "Kil", unit="g"), it(2, "KAOLİN KİLİ", unit="g", stock=500)]
    inp = PlanInputs(items={i.id: i for i in items}, recipes={1: rec(1, [(1, 1)])}, stock_as_of=NOW,
                     mgroup_of={1: 3, 2: 3}, mgroup_names={3: "Kil"})
    res = attach(compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 1000}])),
                 PriceInputs(suppliers=STEARYL_SUP, card_supplier={2: 3}), None)
    assert res["suppliers"]["unrelated"] == []
    assert [b["name"] for b in res["suppliers"]["candidates"]] == ["VESER KİMYEVİ"]
    m = mat(res, 1)
    assert any(c["code"] == "group_alt_stock" for c in m["cautions"])
    assert m["display"]["buy_text"] == "1,0 kg"                      # eşdeğer kartın 500 g'ı düşülmedi


def test_firms_view_for_preview():
    res = _stearyl(offers={126: [offer(126, "TATLIDİLİMLER", 3.2, sid=1)]})
    v = firms_view(mat(res, 126))
    assert v["title"] == "Tedarikçiler (3): TATLIDİLİMLER · VESER KİMYEVİ · YİĞİTOGLU KİMYA"
    assert v["count"] == 3
    assert v["firms"] == [{"t": "TATLIDİLİMLER", "d": ["stok kartında yazan", "alım 12.08.2026 (25,0 kg)",
                                                       "fiyat listesi: 3,20 $/kg"]}]
    assert v["alts_title"] == "Aynı malzeme — diğer kartlar («Setil stearil alkol» grubu)"
    a = {x["t"]: x for x in v["alts"]}
    assert a["«CETYL STEARYL ALCOHOL» — YİĞİTOGLU KİMYA"]["w"] is True
    assert a["«CETYL STEARYL ALCOHOL» — YİĞİTOGLU KİMYA"]["d"] == [
        "stok yok", "birimi adet, bu satır g — miktarlar doğrudan karşılaştırılamaz",
        "son alım 20.07.2026"]
    bare = attach(plan([it(1, "Kil", unit="g")], [(1, 1)]), PriceInputs(), None)
    assert firms_view(bare["materials"][0]) is None


def test_firms_view_counts_equivalent_card_lot_firms():
    """Eşdeğer kartın kart tedarikçisi yoksa (canlıda yaygın) ya da lotları
    başka firmadansa: alt satırdaki son alım / numune firmaları başlıktaki
    "Tedarikçiler (N)" sayısına ve ad listesine de girer."""
    items = [it(126, "SETİL STEARİL ALKOL", unit="g", stock=3944), it(599, "CETYL STEARYL ALCOHOL", unit="adet")]
    inp = PlanInputs(items={i.id: i for i in items}, recipes={1: rec(1, [(126, 40)])}, stock_as_of=NOW,
                     mgroup_of={126: 7, 599: 7}, mgroup_names={7: "Setil stearil alkol"})
    res = compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 1000}]))
    sup = {**STEARYL_SUP, 5: SupplierRec(id=5, name="UMAYCHEM")}
    lots = {599: [LotRec(item_id=599, lot_number="U-1", supplier_id=5, created_at=datetime(2026, 7, 20, 7),
                         inventory_id=2),
                  LotRec(item_id=599, lot_number="V-N", supplier_id=3, is_sample=True, quantity=1,
                         created_at=datetime(2026, 9, 1, 7), inventory_id=3)]}
    rc = {599: [ReceiptRec(599, "U-1", 10, datetime(2026, 7, 20, 7), "purchase")]}
    attach(res, PriceInputs(suppliers=sup, card_supplier={126: 1}, lots=lots, receipts=rc), None)
    v = firms_view(mat(res, 126))
    assert v["title"] == "Tedarikçiler (3): TATLIDİLİMLER · UMAYCHEM · VESER KİMYEVİ"
    assert v["count"] == 3
    assert v["alts"][0]["t"] == "«CETYL STEARYL ALCOHOL»"                 # kartında tedarikçi yok
    assert "son alım UMAYCHEM 20.07.2026" in v["alts"][0]["d"]
    assert "numune VESER KİMYEVİ 01.09.2026" in v["alts"][0]["d"]
    # Yalnız eşdeğer kart varsa ve onda da kart tedarikçisi yoksa başlık 0 değil
    attach(res, PriceInputs(suppliers=sup, lots=lots, receipts=rc), None)
    assert firms_view(mat(res, 126))["count"] == 2


def test_load_price_inputs_receipts_orders_analyses(db_session):
    from database import (Inventory, Item, SampleAnalysis, SampleAnalysisIngredient, StockOrderFlag, Supplier,
                          SupplierPrice, Transaction)
    krk = Supplier(name="KRK GIDA", domain="cosmetics")
    nat = Supplier(name="NATURALYA", domain="cosmetics")
    db_session.add_all([krk, nat])
    db_session.flush()
    a = Item(name="JOJOBA YAĞI", category="Hammadde", unit="g", supplier_id=krk.id, domain="cosmetics")
    b = Item(name="JOJOBA YAĞI — NATURALYA", category="Hammadde", unit="g", supplier_id=nat.id,
             domain="cosmetics")
    origin = Item(name="JOJOBA ESKİ KART", category="Hammadde", unit="g", domain="cosmetics")
    sup_item = Item(name="Kapsül", category="Hammadde", unit="adet", domain="supplement")
    db_session.add_all([a, b, origin, sup_item])
    db_session.flush()
    moved = Inventory(item_id=a.id, supplier_id=krk.id, lot_number="L9", quantity=400, domain="cosmetics",
                      moved_from_item_id=origin.id)
    smp = Inventory(item_id=b.id, supplier_id=nat.id, lot_number="MİNERVA", quantity=50, is_sample=True,
                    domain="cosmetics")
    linked = Inventory(item_id=a.id, supplier_id=nat.id, lot_number="NUMUNE", quantity=20, domain="cosmetics",
                       sample_converted_at=datetime(2026, 10, 5, 9))
    db_session.add_all([moved, smp, linked])
    db_session.flush()
    db_session.add_all([
        Transaction(item_id=origin.id, lot_number="L9", transaction_type="Input", quantity=1000,
                    notes="Mal kabul — Lot: L9", timestamp=datetime(2026, 8, 24, 10)),
        Transaction(item_id=a.id, lot_number="L9", transaction_type="Adjustment", quantity=400,
                    notes="Lot taşındı ← «JOJOBA ESKİ KART» (id 1) | Lot: L9"),
        Transaction(item_id=a.id, lot_number="NUMUNE", transaction_type="Input", quantity=20,
                    notes="Numune stoğa çevrildi — Lot: NUMUNE | Tedarikçi: NATURALYA"),
        Transaction(item_id=sup_item.id, lot_number="K1", transaction_type="Input", quantity=5,
                    notes="Mal kabul — Lot: K1"),
        StockOrderFlag(item_id=a.id, supplier_id=krk.id, quantity=1000, unit="g", domain="cosmetics"),
        StockOrderFlag(item_id=a.id, supplier_id=krk.id, quantity=500, unit="g", domain="cosmetics",
                       closed_at=datetime(2026, 9, 1), closed_reason="received"),
        StockOrderFlag(item_id=sup_item.id, quantity=1, domain="supplement"),
        SupplierPrice(item_id=b.id, supplier_name="NATURALYA", unit_price=9.0, price_unit="kg",
                      currency="EUR", domain="cosmetics"),
    ])
    ok = SampleAnalysis(document_no="NA-2026-00031", bulk_name="Deneme", result="uygun", domain="cosmetics")
    gone = SampleAnalysis(document_no="NA-2026-00032", bulk_name="Silinmiş", domain="cosmetics", is_active=False)
    other = SampleAnalysis(document_no="NA-2026-00033", bulk_name="Takviye", domain="supplement")
    db_session.add_all([ok, gone, other])
    db_session.flush()
    for an in (ok, gone, other):
        db_session.add(SampleAnalysisIngredient(analysis_id=an.id, item_id=b.id, item_name=b.name, source="sample",
                                                inventory_id=smp.id, lot_number="MİNERVA"))
    db_session.commit()

    pin = load_price_inputs(db_session, [a.id], "cosmetics", extra_item_ids=[b.id])
    assert pin.offers == {}                                        # eşdeğer kartın teklifi yüklenmez (kur da çekilmez)
    assert pin.card_supplier == {a.id: krk.id, b.id: nat.id}
    assert {l.lot_number for l in pin.lots[a.id]} == {"L9", "NUMUNE"} and pin.lots[b.id][0].is_sample
    assert pin.lot_meta[moved.id].moved_from_item_id == origin.id
    assert pin.lot_meta[linked.id].sample_converted_at == datetime(2026, 10, 5, 9)
    kinds = {(r.item_id, r.lot): r.kind for lst in pin.receipts.values() for r in lst}
    assert kinds == {(origin.id, "L9"): "purchase", (a.id, "NUMUNE"): "converted"}   # Adjustment / öbür panel yok
    assert pin.converted_lots == {(a.id, "NUMUNE")}
    assert sorted((o.quantity, o.closed_reason) for o in pin.orders[a.id]) == [(500, "received"), (1000, None)]
    assert set(pin.orders) == {a.id}
    assert [x.document_no for x in pin.analyses[b.id]] == ["NA-2026-00031"]

    # uçtan uca: taşınmış lot ilk kartın Mal kabul'üyle ALIM, link_only lot çevrilmiş numune
    items = {a.id: it(a.id, a.name, unit="g"), b.id: it(b.id, b.name, unit="g")}
    inp = PlanInputs(items=items, recipes={1: rec(1, [(a.id, 1)])}, stock_as_of=NOW,
                     mgroup_of={a.id: 1, b.id: 1}, mgroup_names={1: "Jojoba"})
    res = attach(compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 10}])), pin, None)
    rel = mat(res, a.id)["relations"]
    assert rel["last"] == {"name": "KRK GIDA", "key": "KRKGIDA", "date": "24.08.2026"}
    firms = {f["name"]: f for f in rel["firms"]}
    assert firms["KRK GIDA"]["types"] == ["card", "purchase", "order"]
    assert firms["NATURALYA"]["samples"][0]["converted"] is True
    alt = rel["alternatives"][0]
    assert alt["supplier"] == "NATURALYA" and alt["sample_text"] == "50 gram numune"
