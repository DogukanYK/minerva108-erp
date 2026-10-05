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
rehber + kontrol listesi · sections() numaralandırması ve notlar.
"""
from datetime import date, datetime

import pytest

from core.consumption import IngredientRec, ItemRec, RecipeRec
from core.fx import Rates
from core.purchase_plan import LotRec, PlanInputs, compute
from core.purchase_plan_models import PlanRequest
from core.purchase_pricing import (OfferRec, PriceInputs, SupplierIndex, SupplierRec, attach,
                                   load_price_inputs, money, round_half_up, sections, supplier_key)

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
