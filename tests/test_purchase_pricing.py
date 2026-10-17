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
P1a inceleme (08.10.2026): pasif firma seçilmez (firma ölçütü — mükerrer
kartın pasif ikizi sayılmaz), elle fiyat aynı firmanın Excel teklifini
bastırır, fiyat kaynağı metni, pasif kopya kart iletişim/kopya uyarısı dışı.
P1b (07.10.2026): bitirilecek firmanın ucuz teklifi seçilmez (gri etiketli
kalır) · malzeme tercihi (rank) > tercih edilen firma > normal · atlanacaklar
listesi en iyi seçime uygulanır · eşdeğer kart teklifi yarışır + etiketlenir
(birimi farklı kartınki alınmaz) · adaylarda bitirilecek / "alma" yok, tercih
edilen yedek var · mükerrer kart durum çelişkisi · durum yoksa çıktı eskisiyle
aynı · DB yükleyicide durum, tercih (kart kapsamı grubu ezer), eşdeğer teklif.
Stoktan karşılanan değer (14.10.2026): yeterli satırda değer (tutar None
kalır), kısmi satırda kullanılan stok, fiyatsız sayılır/toplanmaz, bekletilen
ve brüt mod almaz, açık siparişte stok + yolda + alınacak = gereken, plan
toplamı `amount` ile (ambalaj katı değil), yeterli bölüm toplamı + nötr
"Fiyat yok".
Ambalaj uyarısı (16.10.2026): küçük / büyük, sınırlar (tam 0,5× yok, tam 2×
var), ambalajsız / yeterli / bekletilen / fiyatsız satır almaz, litre kart +
kg fiyatı, ambalaj katı açıkken "tam ambalaj" eki yok, idempotent, tutar ve
toplam aynı.  Plan maliyet özeti: tür toplamı = plan_total, birleşik kartlar,
iki ürünün paylaştığı malzeme, bitmiş stok düşümü, brüt mod, reçetesiz ürün,
eksik kalem bağlantıları, "cost" bölümü "products"tan sonra.
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
                    "checklist", "products", "cost", "sufficient", "notes"]
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
    res = attach(compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 1000}],
                                          options={"count_group_stock": False})),
                 PriceInputs(suppliers=STEARYL_SUP, card_supplier={2: 3}), None)
    assert res["suppliers"]["unrelated"] == []
    assert [b["name"] for b in res["suppliers"]["candidates"]] == ["VESER KİMYEVİ"]
    m = mat(res, 1)
    assert any(c["code"] == "group_alt_stock" for c in m["cautions"])
    assert m["display"]["buy_text"] == "1,0 kg"                      # eşdeğer kartın 500 g'ı düşülmedi
    assert alt_texts(m) == ["«KAOLİN KİLİ» (VESER KİMYEVİ, stok 500 gram)"]
    # Grup stoğu sayılırken (varsayılan) aynı birimli eşdeğer kartın stoğu düşülür
    res = attach(compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 1000}])),
                 PriceInputs(suppliers=STEARYL_SUP, card_supplier={2: 3}), None)
    m = mat(res, 1)
    assert m["display"]["buy_text"] == "500 gram" and "group_alt_stock" not in [c["code"] for c in m["cautions"]]
    assert alt_texts(m) == ["«KAOLİN KİLİ» (VESER KİMYEVİ, stok 500 gram) — stoğu sayıldı"]
    assert m["relations"]["alternatives"][0]["counted"] is True


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


# ─── Tedarikçi durumu + malzeme tercihi ile teklif seçimi (P1b) ─────────────

from core.purchase_pricing import offer_lines  # noqa: E402


def _sup(id, name, status="normal", reason=None):
    return SupplierRec(id=id, name=name, status=status, status_reason=reason)


def _kim(res):
    m = mat(res, 1)
    return [(o["name"], o["status"], o["eligible"]) for o in m["offers"]]


def test_cheap_phase_out_offer_not_chosen_but_listed_grey():
    sups = {1: _sup(1, "UCUZ KİMYA", "phase_out", "1 kg satıyor"), 2: _sup(2, "BEFCHEM")}
    offers = {1: [offer(1, "UCUZ KİMYA", 5.0, sid=1), offer(1, "BEFCHEM", 8.0, sid=2)]}
    res = attach(plan([it(1, "SHEA", unit="g")], [(1, 100)]), PriceInputs(suppliers=sups, offers=offers), None)
    m = mat(res, 1)
    assert (m["supplier"], m["price"], m["amount"]) == ("BEFCHEM", 8.0, 800)       # 100 kg × 8
    assert _kim(res) == [("BEFCHEM", "normal", True), ("UCUZ KİMYA", "phase_out", False)]
    assert offer_lines(m, "USD") == [("BEFCHEM — 8,00 $/kg", "b"),
                                     ("UCUZ KİMYA — 5,00 $/kg — bitirilecek, alma", "g")]
    assert res["pricing"]["status_used"] is True
    chk = res["suppliers"]["checklist"]
    assert chk["phase_out_firms"] == [{"key": "UCUZ", "name": "UCUZ KİMYA", "reason": "1 kg satıyor"}]
    secs = {s["key"]: s for s in sections(res)}
    assert secs["raw_priced"]["subtitle"].startswith("Kalın yazılan tedarikçi seçilen teklif")
    assert any(r["code"] == "phase_out_firms" and "UCUZ KİMYA (1 kg satıyor)" in r["text"]
               for r in secs["checklist"]["rows"])
    assert any(n.startswith("Tutar = alınacak × seçilen teklifin birim fiyatı") for n in secs["notes"]["rows"])
    # yalnız bitirilecek firmanın fiyatı varsa: fiyat seçilmez, satır "teklif alınacak"
    res = attach(plan([it(1, "SHEA", unit="g")], [(1, 100)]),
                 PriceInputs(suppliers=sups, offers={1: [offer(1, "UCUZ KİMYA", 5.0, sid=1)]}), None)
    m = mat(res, 1)
    assert m["group"] == "none" and m["amount"] is None
    assert offer_lines(m, "USD") == [("Fiyat yok — teklif alınacak", "r"),
                                     ("UCUZ KİMYA — 5,00 $/kg — bitirilecek, alma", "g")]
    assert res["suppliers"]["candidates"] == []                        # bitirilecek aday OLMAZ
    assert [u["name"] for u in res["suppliers"]["unrelated"]] == ["SHEA"]
    # seçenek kapalı → eski davranış: en ucuz (bitirilecek) seçilir
    res = attach(plan([it(1, "SHEA", unit="g")], [(1, 100)], respect_supplier_status=False),
                 PriceInputs(suppliers=sups, offers=offers), None)
    assert mat(res, 1)["supplier"] == "UCUZ KİMYA" and res["pricing"]["status_used"] is False
    assert "phase_out_firms" not in res["suppliers"]["checklist"]


def test_material_pref_rank_beats_cheaper_normal_and_preferred_firm():
    sups = {1: _sup(1, "AAA"), 2: _sup(2, "NATURALYA"), 3: _sup(3, "SURYA KİMYA", "preferred"),
            4: _sup(4, "UMAYCHEM"), 5: _sup(5, "ZZZ")}
    offers = {1: [offer(1, "AAA", 4.0, sid=1), offer(1, "NATURALYA DOĞAL ÜRÜNLER", 6.0),   # bağsız, anahtarla
                  offer(1, "SURYA KİMYA", 5.0, sid=3), offer(1, "UMAYCHEM", 7.0, sid=4),
                  offer(1, "ZZZ", 3.0, sid=5)]}
    prefs = {1: [(4, "preferred", 1), (2, "preferred", 2), (5, "avoid", 1)]}
    res = attach(plan([it(1, "ARGAN", unit="g")], [(1, 10)]),
                 PriceInputs(suppliers=sups, offers=offers, prefs=prefs), None)
    m = mat(res, 1)
    assert m["supplier"] == "UMAYCHEM" and m["price"] == 7.0 and m["supplier_status"] == "preferred"
    assert offer_lines(m, "USD") == [
        ("UMAYCHEM — 7,00 $/kg (tercih)", "b"),
        ("NATURALYA DOĞAL ÜRÜNLER — 6,00 $/kg (tercih 2. sıra, daha ucuz)", "g"),
        ("SURYA KİMYA — 5,00 $/kg (tercih edilen firma, daha ucuz)", "g"),
        ("AAA — 4,00 $/kg (daha ucuz)", "g"),
        ("ZZZ — 3,00 $/kg — bu malzemede alma", "g")]
    assert m["supplier_prefs"]["preferred"][0] == {"supplier_id": 4, "key": "UMAYCHEM", "name": "UMAYCHEM",
                                                   "rank": 1}
    blk = res["suppliers"]["chosen"][0]
    assert blk["name"] == "UMAYCHEM" and blk["items"][0]["reasons"] == ["tercih"]
    secs = {s["key"]: s for s in sections(res)}
    assert secs["suppliers"]["subtitle"].startswith("Her malzeme seçilen teklifi")


def test_skip_list_now_applies_to_best_offer():
    offers = {1: [offer(1, "MİNERVA 108", 1.0), offer(1, "BEFCHEM", 8.0)]}
    res = attach(plan([it(1, "Yağ", unit="g")], [(1, 1)]),
                 PriceInputs(offers=offers, skip_suppliers=("MİNERVA",)), None)
    m = mat(res, 1)
    assert m["supplier"] == "BEFCHEM"
    assert offer_lines(m, "USD")[1] == ("MİNERVA 108 — 1,00 $/kg — atlanacaklar listesinde", "g")
    res = attach(plan([it(1, "Yağ", unit="g")], [(1, 1)], respect_supplier_status=False),
                 PriceInputs(offers=offers, skip_suppliers=("MİNERVA",)), None)
    assert mat(res, 1)["supplier"] == "MİNERVA 108"                    # eski davranış


def test_group_alternative_card_offer_competes_and_is_labelled():
    sups = {1: _sup(1, "TATLIDİLİMLER", "phase_out", "1 kg satıyor"), 2: _sup(2, "YİĞİTOGLU KİMYA")}
    offers = {126: [offer(126, "TATLIDİLİMLER", 3.0, sid=1)], 599: [offer(599, "YİĞİTOGLU KİMYA", 4.0, sid=2)]}
    # durum yok: eşdeğer kartın teklifi gri bilgi olarak yazılır (seçilmez);
    # birimi farklı kartınki (599 "adet") hiç alınmaz
    res = _stearyl(offers={**offers, 600: [offer(600, "BEFCHEM", 4.5)]})
    m = mat(res, 126)
    assert [(o["name"], o["via_item_id"], o["via_name"], o["status"]) for o in m["offers"]] == [
        ("TATLIDİLİMLER", None, None, "normal"), ("BEFCHEM", 600, "STEARİL ALKOL ESKİ", "equivalent")]
    assert offer_lines(m, "USD")[1] == (
        "BEFCHEM — 4,50 $/kg (eşdeğer kart «STEARİL ALKOL ESKİ», daha pahalı)", "g")
    # TATLIDİLİMLER bitirilecek → seçilen eşdeğer kartın teklifi
    items = [it(126, "SETİL STEARİL ALKOL", unit="g", stock=0), it(599, "CETYL STEARYL ALCOHOL", unit="g")]
    inp = PlanInputs(items={i.id: i for i in items}, recipes={1: rec(1, [(126, 40)])}, stock_as_of=NOW,
                     mgroup_of={126: 7, 599: 7}, mgroup_names={7: "Setil"})
    res = attach(compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 1000}])),
                 PriceInputs(suppliers=sups, offers=offers, card_supplier={126: 1, 599: 2}), None)
    m = mat(res, 126)
    assert (m["supplier"], m["supplier_via"], m["price"]) == ("YİĞİTOGLU KİMYA", "CETYL STEARYL ALCOHOL", 4.0)
    assert offer_lines(m, "USD") == [
        ("YİĞİTOGLU KİMYA — 4,00 $/kg (eşdeğer kart «CETYL STEARYL ALCOHOL»)", "b"),
        ("TATLIDİLİMLER — 3,00 $/kg — bitirilecek, alma", "g")]
    assert res["pricing"]["via_used"] is True
    assert any("eşdeğer kart «…»" in n for n in sections(res)[-1]["rows"])
    assert res["suppliers"]["chosen"][0]["items"][0]["reasons"] == ["eşdeğer kart «CETYL STEARYL ALCOHOL»"]


def _shea_group(prefs=None, sups=None, **opts):
    """SHEA (kart 1) + aynı gruptaki SHEA BUTTER - X (kart 2): kendi kartında
    BEFCHEM 8 $/kg, eşdeğer kartta XKIM 3 $/kg; ihtiyaç 100 kg."""
    items = [it(1, "SHEA", unit="g"), it(2, "SHEA BUTTER - X", unit="g")]
    inp = PlanInputs(items={i.id: i for i in items}, recipes={1: rec(1, [(1, 100)])}, stock_as_of=NOW,
                     mgroup_of={1: 5, 2: 5}, mgroup_names={5: "Shea"})
    res = compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 1000}], options=opts))
    pin = PriceInputs(suppliers=sups or {1: _sup(1, "BEFCHEM"), 2: _sup(2, "XKIM")},
                      offers={1: [offer(1, "BEFCHEM", 8.0, sid=1)],
                              2: [offer(2, "XKIM", 3.0, sid=2, label="XKIM teklifi")]},
                      card_supplier={1: 1, 2: 2}, prefs=prefs or {})
    return attach(res, pin, None)


def test_equivalent_offer_without_status_is_info_only_and_selection_unchanged():
    """Durum/tercih yokken eşdeğer kartın DAHA UCUZ teklifi seçilmez: seçilen
    firma, tutar, toplamlar, firma dökümü, adaylar, fiyat kaynakları ve
    bölümler kural kapalıyken (eşdeğer teklif hiç yüklenmez = HEAD davranışı)
    ile aynı; eşdeğer teklif yalnız gri bilgi + not satırı."""
    new, old = _shea_group(), _shea_group(respect_supplier_status=False)
    a, b = mat(new, 1), mat(old, 1)
    assert (a["supplier"], a["price"], a["amount"]) == (b["supplier"], b["price"], b["amount"]) == ("BEFCHEM", 8.0, 800)
    assert [o["name"] for o in b["offers"]] == ["BEFCHEM"]                 # kapalı: eşdeğer teklif YOK
    assert offer_lines(b, "USD") == [("BEFCHEM — 8,00 $/kg", "b")]
    assert offer_lines(a, "USD") == [("BEFCHEM — 8,00 $/kg", "b"),
                                     ("XKIM — 3,00 $/kg (eşdeğer kart «SHEA BUTTER - X», daha ucuz)", "g")]
    assert a["offers"][1]["status"] == "equivalent" and a["offers"][1]["eligible"] is False
    assert a["relations"]["firms"] == b["relations"]["firms"]           # bilgi teklifi firma dökümüne girmez
    assert new["pricing"]["sources"] == old["pricing"]["sources"]       # "XKIM teklifi" kaynak sayılmaz
    assert new["pricing"]["totals"] == old["pricing"]["totals"] and new["pricing"]["status_used"] is False
    assert new["suppliers"]["chosen"] == old["suppliers"]["chosen"]
    assert new["suppliers"]["candidates"] == old["suppliers"]["candidates"]
    strip = lambda secs: [(s["key"], s["title"], s["subtitle"], s["summary"]) for s in secs]   # noqa: E731
    assert strip(sections(new)) == strip(sections(old))
    assert any("öbürleri gri, yalnız bilgi" in n for n in sections(new)[-1]["rows"])
    assert not any("eşdeğer kart «…»" in n for n in sections(old)[-1]["rows"])
    # fiyatsız satırda da bilgi teklifi seçilmez, "fiyat listesinde (fiyatsız)" adayı olmaz
    items = [it(1, "SHEA", unit="g"), it(2, "SHEA BUTTER - X", unit="g")]
    inp = PlanInputs(items={i.id: i for i in items}, recipes={1: rec(1, [(1, 100)])}, stock_as_of=NOW,
                     mgroup_of={1: 5, 2: 5}, mgroup_names={5: "Shea"})
    res = attach(compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 1000}])),
                 PriceInputs(suppliers={2: _sup(2, "XKIM")}, offers={2: [offer(2, "XKIM", 3.0, sid=2)]}), None)
    m = mat(res, 1)
    assert m["group"] == "none" and offer_lines(m, "USD")[0] == ("Fiyat yok — teklif alınacak", "r")
    assert not any("fiyat listesinde" in r for b_ in res["suppliers"]["candidates"]
                   for i_ in b_["items"] for r in i_["reasons"])


def test_equivalent_offer_competes_when_preferred_or_own_offers_blocked():
    # malzeme tercihi (gruptan kartlara açılmış hâli) → eşdeğer kartın teklifi seçilir
    res = _shea_group(prefs={1: [(2, "preferred", 1)], 2: [(2, "preferred", 1)]})
    m = mat(res, 1)
    assert (m["supplier"], m["supplier_via"], m["amount"]) == ("XKIM", "SHEA BUTTER - X", 300)
    assert offer_lines(m, "USD")[0] == ("XKIM — 3,00 $/kg (tercih, eşdeğer kart «SHEA BUTTER - X»)", "b")
    # "tercih edilen" firma → yarışır
    res = _shea_group(sups={1: _sup(1, "BEFCHEM"), 2: _sup(2, "XKIM", "preferred")})
    assert mat(res, 1)["supplier"] == "XKIM"
    # kartın kendi teklifi "alma" → eşdeğer (normal) teklif yedek olarak seçilir
    res = _shea_group(prefs={1: [(1, "avoid", 1)]})
    m = mat(res, 1)
    assert (m["supplier"], m["supplier_via"]) == ("XKIM", "SHEA BUTTER - X")
    assert offer_lines(m, "USD") == [("XKIM — 3,00 $/kg (eşdeğer kart «SHEA BUTTER - X»)", "b"),
                                     ("BEFCHEM — 8,00 $/kg — bu malzemede alma", "g")]


def test_candidates_skip_phase_out_and_avoid_add_preferred_backup():
    sups = {1: _sup(1, "HAMMADDE SEPETİ", "phase_out", "küçük"), 2: _sup(2, "NATURALYA"),
            3: _sup(3, "KRK GIDA"), 4: _sup(4, "SURYA KİMYA")}
    lots = {1: [LotRec(item_id=1, lot_number="L1", supplier_id=3, created_at=datetime(2026, 8, 1, 9),
                       quantity=5, inventory_id=1)]}
    pin = PriceInputs(suppliers=sups, card_supplier={1: 1}, lots=lots,
                      prefs={1: [(2, "preferred", 1), (4, "preferred", 2), (3, "avoid", 1)]})
    res = attach(plan([it(1, "Kil", unit="g")], [(1, 1)]), pin, None)
    reasons = {b["name"]: it_["reasons"] for b in res["suppliers"]["candidates"] for it_ in b["items"]}
    assert reasons == {"NATURALYA": ["tercih edilen yedek"],
                       "SURYA KİMYA": ["tercih edilen yedek (2. sıra)"]}   # bitirilecek + "alma" yok
    assert [f["name"] for f in res["suppliers"]["checklist"]["phase_out_firms"]] == ["HAMMADDE SEPETİ"]
    # seçenek kapalı: eski aday listesi (kart + son alım), tercih yedeği yok
    res = attach(plan([it(1, "Kil", unit="g")], [(1, 1)], respect_supplier_status=False), pin, None)
    assert {b["name"] for b in res["suppliers"]["candidates"]} == {"HAMMADDE SEPETİ", "KRK GIDA"}


def test_status_conflict_between_duplicate_cards_reported_and_phase_out_wins():
    sups = {1: _sup(1, "TATLIDİLİMLER", "phase_out", "1 kg"), 2: _sup(2, "TATLİDİLİMLER", "preferred"),
            3: _sup(3, "BEFCHEM")}
    offers = {1: [offer(1, "TATLİDİLİMLER", 2.0, sid=2), offer(1, "BEFCHEM", 9.0, sid=3)]}
    res = attach(plan([it(1, "Setil", unit="g")], [(1, 1)]), PriceInputs(suppliers=sups, offers=offers), None)
    assert mat(res, 1)["supplier"] == "BEFCHEM"                         # aynı firma anahtarı → bitirilecek
    conf = res["suppliers"]["checklist"]["status_conflicts"]
    assert conf == [{"key": "TATLIDILIMLER", "name": "TATLIDİLİMLER",
                     "names_by_status": {"phase_out": ["TATLIDİLİMLER"], "preferred": ["TATLİDİLİMLER"]}}]
    row = next(r for r in next(s for s in sections(res) if s["key"] == "checklist")["rows"]
               if r["code"] == "status_conflicts")
    assert row["text"] == ("Aynı firmanın kartları farklı işaretlenmiş (durumu birleştirin ya da eşitleyin): "
                           "TATLIDİLİMLER: “TATLIDİLİMLER” bitirilecek / “TATLİDİLİMLER” tercih edilen.")


def test_without_status_output_identical_to_cheapest_first():
    def run(**o):
        res = plan([it(1, "SHEA BUTTER", unit="g"), it(2, "Kil", unit="g")], [(1, 107.75), (2, 1)], **o)
        pin = PriceInputs(suppliers={1: _sup(1, "BEFCHEM"), 2: _sup(2, "SURYA")}, card_supplier={2: 2},
                          offers={1: [offer(1, "Pahalı", 9.0), offer(1, "Fiyatsız", None),
                                      offer(1, "BEFCHEM", 8.0, pkg=25, sid=1)]})
        return attach(res, pin, None)
    new, old = run(), run(respect_supplier_status=False)
    assert new["pricing"]["status_used"] is False
    for a, b in zip(new["materials"], old["materials"]):
        assert a["offers"] == b["offers"] and offer_lines(a, "USD") == offer_lines(b, "USD")
        assert (a["supplier"], a["price"], a["amount"]) == (b["supplier"], b["price"], b["amount"])
    strip = lambda secs: [(s["key"], s["title"], s["subtitle"], s["summary"]) for s in secs]   # noqa: E731
    assert strip(sections(new)) == strip(sections(old))
    assert sections(new)[-1]["rows"] == sections(old)[-1]["rows"]
    assert {k: v for k, v in new["suppliers"]["checklist"].items()
            if k not in ("phase_out_firms", "status_conflicts")} == old["suppliers"]["checklist"]
    assert new["suppliers"]["checklist"]["phase_out_firms"] == [] == new["suppliers"]["checklist"]["status_conflicts"]
    m = mat(new, 1)
    assert offer_lines(m, "USD")[:2] == [("BEFCHEM — 8,00 $/kg · 25 kg'lık ambalaj", "b"),
                                         ("Pahalı — 9,00 $/kg (daha pahalı)", "g")]
    assert "Kalın yazılan tedarikçi en ucuz olanı" in next(s for s in sections(new)
                                                           if s["key"] == "raw_priced")["subtitle"]


def test_load_price_inputs_status_prefs_and_alt_offers(db_session):
    from database import Item, MaterialGroup, MaterialSupplierPref, Supplier, SupplierPrice
    tat = Supplier(name="TATLIDİLİMLER", domain="cosmetics", purchase_status="phase_out", status_reason="1 kg")
    yk = Supplier(name="YİĞİTOĞLU", domain="cosmetics", purchase_status="preferred")
    other = Supplier(name="TAKVİYE", domain="supplement")
    db_session.add_all([tat, yk, other])
    g = MaterialGroup(name="Setil", domain="cosmetics")
    dead = MaterialGroup(name="Eski", domain="cosmetics", is_active=False)
    db_session.add_all([g, dead])
    db_session.flush()
    a = Item(name="SETİL", category="Hammadde", unit="g", supplier_id=tat.id, material_group_id=g.id,
             domain="cosmetics")
    b = Item(name="CETYL", category="Hammadde", unit="g", supplier_id=yk.id, material_group_id=g.id,
             domain="cosmetics")
    c = Item(name="ESKİ", category="Hammadde", unit="g", material_group_id=dead.id, domain="cosmetics")
    db_session.add_all([a, b, c])
    db_session.flush()
    db_session.add_all([
        SupplierPrice(item_id=b.id, supplier_id=yk.id, supplier_name="YİĞİTOĞLU", unit_price=4.0,
                      price_unit="kg", currency="USD", domain="cosmetics"),
        MaterialSupplierPref(domain="cosmetics", material_group_id=g.id, supplier_id=yk.id,
                             preference="preferred", rank=1),
        MaterialSupplierPref(domain="cosmetics", material_group_id=g.id, supplier_id=tat.id, preference="avoid"),
        MaterialSupplierPref(domain="cosmetics", item_id=a.id, supplier_id=tat.id, preference="preferred",
                             rank=3),                                       # kart kapsamı grubu ezer
        MaterialSupplierPref(domain="cosmetics", material_group_id=dead.id, supplier_id=yk.id,
                             preference="avoid")])                          # pasif grup okunmaz
    db_session.commit()
    pin = load_price_inputs(db_session, [a.id, c.id], "cosmetics", extra_item_ids=[b.id], offer_extra_ids=[b.id])
    assert [o.supplier_name for o in pin.offers[b.id]] == ["YİĞİTOĞLU"]   # eşdeğer kartın teklifi yüklendi
    assert pin.suppliers[tat.id].status == "phase_out" and pin.suppliers[tat.id].status_reason == "1 kg"
    assert pin.suppliers[yk.id].status == "preferred" and other.id not in pin.suppliers
    assert pin.prefs[a.id] == [(yk.id, "preferred", 1), (tat.id, "preferred", 3)]
    assert pin.prefs[b.id] == [(yk.id, "preferred", 1), (tat.id, "avoid", 1)]
    assert c.id not in pin.prefs
    pin = load_price_inputs(db_session, [a.id], "cosmetics", extra_item_ids=[b.id])
    assert b.id not in pin.offers                                       # yalnız ilişki: teklif yok


# ─── P1a inceleme düzeltmeleri (08.10.2026) ─────────────────────────────────
# Pasif (yumuşak silinmiş) firma · elle fiyatın Excel teklifini bastırması ·
# fiyat kaynağı metni · pasif mükerrer kartın iletişim/kopya-kart dışı kalması.

def _inactive(id, name, **kw):
    return SupplierRec(id=id, name=name, is_active=False, **kw)


def test_inactive_firm_offer_not_chosen_listed_grey_even_without_status_rule():
    """Lab "Eski Firma"yı sildi (pasif): ucuz teklifi seçilmez, gri kalır."""
    sups = {1: _inactive(1, "ESKİ FİRMA"), 2: _sup(2, "YENİ FİRMA")}
    offers = {1: [offer(1, "ESKİ FİRMA", 5.0, sid=1), offer(1, "YENİ FİRMA", 9.0, sid=2)]}
    for opts in ({}, {"respect_supplier_status": False}):
        res = attach(plan([it(1, "JOJOBA", unit="g")], [(1, 100)], **opts),
                     PriceInputs(suppliers=sups, offers=offers), None)
        m = mat(res, 1)
        assert (m["supplier"], m["price"], m["amount"]) == ("YENİ FİRMA", 9.0, 900)
        assert _kim(res) == [("YENİ FİRMA", "normal", True), ("ESKİ FİRMA", "inactive", False)]
        assert offer_lines(m, "USD") == [("YENİ FİRMA — 9,00 $/kg", "b"),
                                         ("ESKİ FİRMA — 5,00 $/kg — pasif tedarikçi", "g")]
        assert res["pricing"]["inactive_listed"] is True and res["pricing"]["status_used"] is False
        notes = next(s for s in sections(res) if s["key"] == "notes")["rows"]
        assert any("Pasife alınmış" in n for n in notes)
    # yalnız pasif firmanın fiyatı: "teklif alınacak", pasif firma aday da olmaz
    res = attach(plan([it(1, "JOJOBA", unit="g")], [(1, 100)]),
                 PriceInputs(suppliers=sups, card_supplier={1: 1},
                             offers={1: [offer(1, "ESKİ FİRMA", 5.0, sid=1)]}), None)
    assert mat(res, 1)["group"] == "none" and res["suppliers"]["candidates"] == []


def test_inactive_duplicate_card_price_stays_valid_via_active_twin():
    """Ölçüt firma: TATLİDİLİMLER kartı pasif ama TATLIDİLİMLER aktif → aynı
    firma, pasif karta bağlı (ucuz) fiyat geçerli kalır; serbest metin satır da."""
    sups = {3: _inactive(3, "TATLİDİLİMLER"), 9: _sup(9, "TATLIDİLİMLER"), 5: _sup(5, "BEFCHEM")}
    offers = {1: [offer(1, "TATLİDİLİMLER", 4.0, sid=3), offer(1, "BEFCHEM", 8.0, sid=5)]}
    res = attach(plan([it(1, "SETİL", unit="g")], [(1, 10)]), PriceInputs(suppliers=sups, offers=offers), None)
    m = mat(res, 1)
    assert m["supplier"] == "TATLİDİLİMLER" and m["price"] == 4.0
    assert res["pricing"]["inactive_listed"] is False
    # kartsız serbest metin teklif, adı yalnız pasif bir kartla eşleşirse pasif
    sups = {3: _inactive(3, "ESKİ FİRMA"), 5: _sup(5, "BEFCHEM")}
    offers = {1: [offer(1, "ESKI FIRMA", 4.0), offer(1, "BEFCHEM", 8.0, sid=5)]}
    res = attach(plan([it(1, "SETİL", unit="g")], [(1, 10)]), PriceInputs(suppliers=sups, offers=offers), None)
    assert mat(res, 1)["supplier"] == "BEFCHEM"
    assert ("ESKI FIRMA", "inactive", False) in _kim(res)


def test_offer_bound_to_active_card_is_never_inactive_even_if_name_key_differs():
    """Birleştirmede kazanana taşınan fiyat: `supplier_name` kaybedenin adı
    (KRK GIDA(HAYAT) → anahtar KRKGIDAHAYAT), bağlı kart KRK GIDA (KRKGIDA)
    aktif.  Anahtarlar farklı olsa da teklif seçilir; Raporlar paneli kuralı
    (aynı `FirmActivity`) aynı satıra aynı sonucu verir; fiyatsız teklifte
    firma aday listesinden düşmez."""
    from core.purchase_pricing import firm_activity, supplier_index
    assert supplier_key("KRK GIDA(HAYAT)") != supplier_key("KRK GIDA")       # ön koşul
    sups = {1: _inactive(1, "KRK GIDA(HAYAT)"), 2: _sup(2, "KRK GIDA"), 5: _sup(5, "BEFCHEM")}
    pin = PriceInputs(suppliers=sups, offers={1: [offer(1, "KRK GIDA(HAYAT)", 4.0, sid=2),
                                                  offer(1, "BEFCHEM", 8.0, sid=5)]})
    for opts in ({}, {"respect_supplier_status": False}):
        res = attach(plan([it(1, "SETİL", unit="g")], [(1, 10)], **opts), pin, None)
        m = mat(res, 1)
        assert (m["supplier"], m["price"]) == ("KRK GIDA(HAYAT)", 4.0)
        assert _kim(res) == [("KRK GIDA(HAYAT)", "normal", True), ("BEFCHEM", "normal", True)]
        assert res["pricing"]["inactive_listed"] is False
    firms = firm_activity(pin, supplier_index(pin))
    assert firms.is_inactive(2, "KRK GIDA(HAYAT)") is False
    assert firms.is_inactive(None, "KRK GIDA(HAYAT)") is True               # kartsız: yalnız pasif kart
    pin = PriceInputs(suppliers=sups, offers={1: [offer(1, "KRK GIDA(HAYAT)", None, sid=2)]})
    res = attach(plan([it(1, "SETİL", unit="g")], [(1, 10)]), pin, None)
    assert mat(res, 1)["group"] == "none"
    assert [b["key"] for b in res["suppliers"]["candidates"]] == [supplier_key("KRK GIDA(HAYAT)")]


def test_merged_card_name_is_alias_of_live_survivor():
    """Birleştirmeden sonra liste eski yazımla gelip satır kartsız kalsa da
    (`merged_into_id` = kazanan) kaybedenin adı takma addır: firma pasif
    DEĞİL.  Kazanan da sonradan pasife alındıysa zincir ölü → pasif;
    birleştirilmemiş pasif kartın adı eskisi gibi pasif."""
    def run(sups):
        offers = {1: [offer(1, "KRK GIDA(HAYAT)", 4.0), offer(1, "BEFCHEM", 8.0, sid=5)]}
        return attach(plan([it(1, "SETİL", unit="g")], [(1, 10)]), PriceInputs(suppliers=sups, offers=offers),
                      None)
    sups = {1: _inactive(1, "KRK GIDA(HAYAT)", merged_into_id=2), 2: _sup(2, "KRK GIDA"),
            5: _sup(5, "BEFCHEM")}
    assert mat(run(sups), 1)["supplier"] == "KRK GIDA(HAYAT)"
    sups[2] = _inactive(2, "KRK GIDA")
    res = run(sups)
    assert mat(res, 1)["supplier"] == "BEFCHEM" and ("KRK GIDA(HAYAT)", "inactive", False) in _kim(res)
    sups = {1: _inactive(1, "KRK GIDA(HAYAT)"), 2: _sup(2, "KRK GIDA"), 5: _sup(5, "BEFCHEM")}
    assert mat(run(sups), 1)["supplier"] == "BEFCHEM"


def test_contacts_skip_inactive_duplicate_card_but_keep_inactive_only_firm():
    """Pasife alınan kopya kart (id 3, eski telefon) firmanın adını/telefonunu
    vermez, kopya-kart uyarısına girmez; yalnız pasif kartı olan firma
    "kartı yok" alarmına düşmez."""
    sups = {3: _inactive(3, "TATLİDİLİMLER", phone="02121111111"),
            9: SupplierRec(id=9, name="TATLIDİLİMLER", phone="05321112233"),
            7: _inactive(7, "KAPANAN KİMYA", phone="02122222222")}
    pin = PriceInputs(suppliers=sups, card_supplier={2: 7},
                      offers={1: [offer(1, "TATLIDİLİMLER", 3.0, sid=9)]})
    res = attach(plan([it(1, "SETİL", unit="g"), it(2, "KİL", unit="g")], [(1, 10), (2, 1)]), pin, None)
    d = res["suppliers"]
    b = next(x for x in d["chosen"] if x["key"] == "TATLIDILIMLER")
    assert b["name"] == "TATLIDİLİMLER" and b["contact_line"] == "0532 111 22 33"
    assert b["contact"]["cards"] == ["TATLIDİLİMLER"]
    assert d["checklist"]["duplicate_cards"] == []
    assert all(x["name"] != "KAPANAN KİMYA" for x in d["checklist"]["no_card"])
    assert d["candidates"] == []                         # pasif firma (kart ilişkisi) aday değil


def test_manual_price_supersedes_same_firm_excel_offer_in_plan():
    """prices_for_items kuralı planda da: aynı kartta firmanın elle fiyatı
    varsa (birimi farklı olsa da) o firmanın Excel teklifi sayılmaz."""
    def manual(item_id, sup, price, *, unit="kg", sid=None):
        return OfferRec(item_id=item_id, supplier_name=sup, supplier_id=sid, unit_price=price,
                        currency="USD", price_unit=unit, source="manual")
    # (a) aynı kart id'si: Excel 10 $/kg, elle 12 $/l
    pin = PriceInputs(suppliers={5: _sup(5, "FOO KİMYA")},
                      offers={1: [offer(1, "FOO KİMYA", 10.0, sid=5), manual(1, "FOO KİMYA", 12.0, unit="l", sid=5)]})
    res = attach(plan([it(1, "SHEA", unit="g")], [(1, 100)]), pin, None)
    m = mat(res, 1)
    assert [(o["name"], o["price"]) for o in m["offers"]] == [("FOO KİMYA", 12.0)]
    assert (m["price"], m["amount"]) == (12.0, 1200)
    assert m["price_source"] == "Elle girilen fiyat"                 # "Fiyat listesi" DEĞİL
    # (b) Excel satırı karta bağlanamamış (supplier_id NULL), aynı ad
    pin = PriceInputs(suppliers={5: _sup(5, "FOO KİMYA")},
                      offers={1: [offer(1, "Foo Kimya", 10.0), manual(1, "FOO KİMYA", 12.0, sid=5)]})
    m = mat(attach(plan([it(1, "SHEA", unit="g")], [(1, 100)]), pin, None), 1)
    assert [(o["name"], o["price"]) for o in m["offers"]] == [("FOO KİMYA", 12.0)]
    # başka kartın elle fiyatı bu kartın Excel teklifini bastırmaz (kart başına)
    pin = PriceInputs(offers={1: [offer(1, "FOO KİMYA", 10.0, sid=5)], 2: [manual(2, "FOO KİMYA", 12.0, sid=5)]})
    res = attach(plan([it(1, "SHEA", unit="g"), it(2, "KİL", unit="g")], [(1, 1), (2, 1)]), pin, None)
    assert mat(res, 1)["price"] == 10.0


def test_price_source_text_matches_footnote():
    """Excel "Fiyat kaynağı" sütunu ile Notlar dipnotu aynı metni kullanır."""
    from core.purchase_pricing import notes_lines
    o = OfferRec(item_id=1, supplier_name="X", unit_price=5.0, currency="USD", price_unit="kg", source="manual")
    res = attach(plan([it(1, "SHEA", unit="g")], [(1, 1)]), PriceInputs(offers={1: [o]}), None)
    assert mat(res, 1)["price_source"] == "Elle girilen fiyat"
    assert any(n.startswith("Fiyat kaynağı: Elle girilen fiyat;") for n in notes_lines(res))
    o = offer(1, "X", 5.0, label=None)                              # Excel, etiketsiz
    res = attach(plan([it(1, "SHEA", unit="g")], [(1, 1)]), PriceInputs(offers={1: [o]}), None)
    assert mat(res, 1)["price_source"] == "Stok Son Durum fiyat listesi"
    assert any(n.startswith("Fiyat kaynağı: Stok Son Durum fiyat listesi (") for n in notes_lines(res))
    res = attach(plan([it(1, "SHEA", unit="g")], [(1, 1)]), PriceInputs(offers={1: [offer(1, "X", 5.0)]}), None)
    assert mat(res, 1)["price_source"] == "Stok Son Durum — Eylül"   # etiket varsa o


# ─── KDV (lab fiyat notları, 08.10.2026) ────────────────────────────────────

def _vat_offer(item_id, sup, price, *, inc=None, rate=None, cur="EUR", sid=None):
    return OfferRec(item_id=item_id, supplier_name=sup, supplier_id=sid, unit_price=price, package_size=25,
                    currency=cur, price_unit="kg", source="lab_notu",
                    source_label="Lab fiyat notları 07.10.2026 — s.2", quoted_at=date(2026, 6, 23),
                    vat_included=inc, vat_rate=rate)


def test_vat_included_offer_competes_on_net_price_and_amount():
    """Pharmaterm 82,56 € KDV %20 dahil = 68,80 € net → 70 €'luk Uludağ'dan ucuz;
    tutar net fiyatla, metinde brüt liste fiyatı + KDV etiketi."""
    from core.purchase_pricing import offer_lines
    res = plan([it(1, "VANİLYA UÇUCU YAĞI", unit="ml")], [(1, 10)])            # 10 l
    pin = PriceInputs(offers={1: [_vat_offer(1, "ULUDAĞ HERBAL", 70.0),
                                  _vat_offer(1, "PHARMATERM", 82.56, inc=True, rate=20.0)]})
    attach(res, pin, None, currency="EUR")
    m = mat(res, 1)
    assert m["supplier"] == "PHARMATERM" and m["price"] == pytest.approx(68.8)
    assert m["amount"] == round_half_up(10.0 * 68.8) == 688
    lines = offer_lines(m, "EUR")
    assert lines[0][1] == "b" and lines[0][0].startswith("PHARMATERM — 68,80 €/l")
    assert "(82,56 €/kg)" in lines[0][0] and "KDV %20 dahil fiyattan düşüldü" in lines[0][0]
    assert lines[1][0].startswith("ULUDAĞ HERBAL — 70,00 €/l") and "KDV" not in lines[1][0]
    # Tedarikçi rehberinde KDV etiketi "neden bu firma" listesine girmez
    chosen = res["suppliers"]["chosen"][0]
    assert chosen["name"] == "PHARMATERM" and "reasons" not in chosen["items"][0]
    # Firma dökümü: net fiyat + etiket
    from core.purchase_pricing import firm_lines
    ph = next(f for f in m["relations"]["firms"] if f["name"] == "PHARMATERM")
    assert any("KDV %20 dahil fiyattan düşüldü" in t for t in firm_lines(ph, "EUR"))


def test_vat_included_without_rate_uses_gross_and_says_so():
    from core.purchase_pricing import offer_lines
    res = plan([it(1, "X", unit="g")], [(1, 1000)])                           # 1 kg
    pin = PriceInputs(offers={1: [_vat_offer(1, "PHARMATERM", 12.0, inc=True),
                                  _vat_offer(1, "B", 11.0)]})
    m = mat(attach(res, pin, None, currency="EUR"), 1)
    assert m["supplier"] == "B" and m["price"] == 11.0                   # brüt 12 > 11
    assert any("KDV dahil, oran yok" in t for t, _ in offer_lines(m, "EUR"))


def test_vat_fields_empty_or_excluded_identical_to_legacy():
    """KDV alanları boş (None) ya da "hariç" (False + oran) iken seçim, sıra,
    tutar, metinler ve teklif sözlükleri KDV öncesiyle birebir aynı."""
    from core.purchase_pricing import offer_lines

    def run(**vat):
        res = plan([it(1, "SHEA BUTTER", unit="g"), it(2, "Kil", unit="g")], [(1, 107.75), (2, 1)])
        offers = [OfferRec(item_id=1, supplier_name=n, supplier_id=sid, unit_price=p, package_size=pk,
                           currency=c, price_unit="kg", source="stok_son_durum", source_label="L",
                           quoted_at=date(2026, 9, 1), **vat)
                  for n, sid, p, pk, c in (("Pahalı", None, 9.0, None, "USD"), ("Fiyatsız", None, None, None, "USD"),
                                           ("BEFCHEM", 1, 8.0, 25, "USD"), ("EURO", None, 7.0, 5, "EUR"))]
        pin = PriceInputs(suppliers={1: _sup(1, "BEFCHEM")}, offers={1: offers})
        return attach(res, pin, RATES)
    legacy = run()
    for vat in ({"vat_included": None, "vat_rate": None}, {"vat_included": False, "vat_rate": 20.0},
                {"vat_included": None, "vat_rate": 1.0}):
        new = run(**vat)
        for a, b in zip(new["materials"], legacy["materials"]):
            assert a["offers"] == b["offers"] and offer_lines(a, "USD") == offer_lines(b, "USD")
            assert (a["supplier"], a["price"], a["amount"]) == (b["supplier"], b["price"], b["amount"])
        assert new["pricing"] == legacy["pricing"] and new["suppliers"] == legacy["suppliers"]


def test_vat_net_used_after_fx_and_in_xlsx_name():
    """KDV dahil EUR teklif USD rapora: net € çevrilir; Excel T1 adında etiket."""
    from core.purchase_plan_xlsx import _offer_name
    res = plan([it(1, "X", unit="g")], [(1, 1000)])
    pin = PriceInputs(offers={1: [_vat_offer(1, "PHARMATERM", 120.0, inc=True, rate=20.0)]})
    m = mat(attach(res, pin, RATES, currency="USD"), 1)
    assert m["price"] == pytest.approx(100.0 * 44.0 / 40.0)               # 100 € net → 110 $
    assert m["offers"][0]["orig_price"] == 120.0                          # brüt liste fiyatı
    assert _offer_name(m["offers"][0]) == "PHARMATERM (KDV %20 dahil fiyattan düşüldü)"


def test_load_price_inputs_carries_vat(db_session):
    from database import Item, SupplierPrice
    i1 = Item(name="VANİLYA", category="Hammadde", unit="ml", domain="cosmetics")
    db_session.add(i1)
    db_session.flush()
    db_session.add_all([
        SupplierPrice(item_id=i1.id, supplier_name="PHARMATERM", unit_price=82.56, currency="EUR",
                      price_unit="kg", vat_included=True, vat_rate=20.0, source="lab_notu", domain="cosmetics"),
        SupplierPrice(item_id=i1.id, supplier_name="ULUDAĞ", unit_price=47.58, currency="EUR",
                      price_unit="kg", domain="cosmetics"),
    ])
    db_session.commit()
    pin = load_price_inputs(db_session, [i1.id], "cosmetics")
    got = {o.supplier_name: (o.vat_included, o.vat_rate) for o in pin.offers[i1.id]}
    assert got == {"PHARMATERM": (True, 20.0), "ULUDAĞ": (None, None)}


# ─── Stoktan karşılanan değer + planın toplam malzeme maliyeti (14.10.2026) ─

def _cover_plan(**opts):
    """Kısmi (500 g stok, 10 kg gerek), yeterli fiyatlı (2,5 kg), yeterli
    fiyatsız (1 kg) ve fiyatsız alınacak satır."""
    items = [it(1, "Kısmi", stock=500), it(2, "Yeterli", stock=10 ** 6), it(3, "Fiyatsız", stock=10 ** 6),
             it(4, "Alınacak fiyatsız")]
    res = plan(items, [(1, 10), (2, 2.5), (3, 1), (4, 1)], **opts)
    return attach(res, PriceInputs(offers={1: [offer(1, "UMAYCHEM", 4.52)], 2: [offer(2, "BEFCHEM", 4.52)]}),
                  None, round_to_package=bool(opts.get("round_to_package")))


def test_stock_value_sufficient_and_partial_rows_amount_unchanged():
    from core.purchase_pricing import cost_summary_caveats, cost_summary_lines, cover_value, value_basis_text
    res = _cover_plan()
    part, suff, nop, buy = mat(res, 1), mat(res, 2), mat(res, 3), mat(res, 4)
    # Yeterli satır: değer = min(gereken, elimizde) × fiyat; tutar YİNE None
    assert suff["status"] == "yeterli" and suff["amount"] is None
    assert suff["stock_qty_used"] == pytest.approx(2.5) and suff["stock_value"] == 11       # 2,5 × 4,52 = 11,3
    assert cover_value(suff) == 11 and value_basis_text(suff) == "BEFCHEM · 4,52 $/kg"
    # Kısmi satır: kullanılan stok = elimizdeki; stoktan + alınacak = gereken
    d = part["display"]
    assert part["status"] == "to_buy" and part["stock_qty_used"] == pytest.approx(0.5)
    assert part["stock_qty_used"] + d["buy_num"] == pytest.approx(d["need_num"])
    assert part["amount"] == 43 and part["stock_value"] == 2                               # 9,5 × 4,52 / 0,5 × 4,52
    assert part["open_qty_used"] == 0 and part["open_value"] is None
    # Fiyatsız: miktar var, değer yok (sayılır, toplanmaz)
    assert nop["stock_qty_used"] == pytest.approx(1.0) and nop["stock_value"] is None
    assert value_basis_text(nop) == "Fiyat yok" and cover_value(nop) is None
    assert buy["stock_qty_used"] == 0 and buy["stock_value"] is None
    pr = res["pricing"]
    assert pr["totals"] == {"raw": 43, "packaging": 0, "label": 0, "all": 43}             # değişmedi
    assert pr["stock_totals"] == {"raw": 13, "packaging": 0, "label": 0, "all": 13}
    assert pr["cost_summary"] == {
        "applies": True, "buy_total": 43, "stock_total": 13, "open_total": 0, "plan_total": 56,
        "buy_rows": 2, "buy_unpriced": 1, "stock_rows": 3, "stock_unpriced": 1, "open_rows": 0,
        "open_unpriced": 0, "new_items": 0}
    assert [(x["key"], x["text"]) for x in cost_summary_lines(res)] == [
        ("buy", "43 $"), ("stock", "13 $"), ("plan", "56 $")]
    cav = cost_summary_caveats(res)
    # "kısmen ya da tamamen": sayı yeterli + stoğu kısmen kullanılan alınacak satırları kapsar
    assert cav[0] == ("Stoktan kısmen ya da tamamen karşılanan 3 kalemden 1 tanesinin fiyatı yok; bu stoğun "
                      "değeri toplama girmedi.")
    assert cav[1] == "Fiyatı olmayan 1 alınacak kalem planın toplam maliyetine de girmedi."
    assert "yenileme değeri" in cav[-1] and "alış maliyeti değil" in cav[-1]


def test_cost_summary_caveats_wording_matches_kpi_terms():
    """"Alınacak kalem" = alınacak kart + yeni kalem (KPI "53 alınacak kart +
    31 yeni kalem", özet kutusu "84 alınacak kalemin fiyatı yok"); stok satırı
    "kısmen ya da tamamen" der — KPI "Fiyatı eksik kalem" ile karışmasın."""
    from core.purchase_pricing import cost_summary_caveats

    cs = {"applies": True, "buy_unpriced": 53, "new_items": 31, "stock_rows": 87, "stock_unpriced": 48}
    cav = cost_summary_caveats({"pricing": {"cost_summary": cs}})
    assert cav[0] == ("Stoktan kısmen ya da tamamen karşılanan 87 kalemden 48 tanesinin fiyatı yok; bu stoğun "
                      "değeri toplama girmedi.")
    assert cav[1] == ("Fiyatı olmayan 84 alınacak kalem (53 kart, 31 yeni etiket / koli / palet kalemi) planın "
                      "toplam maliyetine de girmedi.")
    only_new = cost_summary_caveats({"pricing": {"cost_summary": {"applies": True, "new_items": 2}}})
    assert only_new == ["Fiyatı olmayan 2 yeni etiket / koli / palet kalemi planın toplam maliyetine de girmedi."]


def test_sufficient_section_total_and_neutral_offer_line():
    res = _cover_plan()
    secs = {s["key"]: s for s in sections(res)}
    suff = secs["sufficient"]
    assert [m["name"] for m in suff["rows"]] == ["Fiyatsız", "Yeterli"]
    assert suff["total"] == 11 and suff["summary"] == "2 kalem, stoktaki değer 11 $ — 1 kalemin fiyatı yok"
    assert "yenileme değeri" in suff["subtitle"]
    assert secs["raw_priced"]["total"] == 43                                              # alım bölümü aynı
    # yeterli satırda alım yok → nötr gri "Fiyat yok"; alınacakta kırmızı kalır
    assert offer_lines(mat(res, 3), "USD") == [("Fiyat yok", "g")]
    assert offer_lines(mat(res, 4), "USD")[0] == ("Fiyat yok — teklif alınacak", "r")
    assert any("yenileme değeri" in n for n in secs["notes"]["rows"])
    # hiçbiri fiyatsız / tek kalem fiyatsız özetleri (iç parantez yok — PDF başlığı parantezle sarar)
    one = attach(plan([it(1, "Tek", stock=10 ** 6)], [(1, 1)]), PriceInputs(), None)
    s1 = next(s for s in sections(one) if s["key"] == "sufficient")
    assert s1["summary"] == "1 kalem — fiyatı yok" and s1["total"] is None


def test_held_row_and_gross_mode_get_no_stock_value():
    from core.purchase_pricing import cost_summary_caveats, cost_summary_lines
    res = plan([it(1, "Toz", stock=100), it(2, "Kil", stock=10 ** 6)], [(1, 233.2), (2, 1)],
               held_items=[{"item_id": 1, "reason": "teyit"}])
    attach(res, PriceInputs(offers={1: [offer(1, "NATURALYA", 10.32)], 2: [offer(2, "S", 2.0)]}), None)
    h = res["held"][0]
    assert h["amount"] == 2406 and h["stock_qty_used"] is None and h["stock_value"] is None
    cs = res["pricing"]["cost_summary"]
    assert cs["stock_total"] == 2 and cs["stock_rows"] == 1 and cs["plan_total"] == 2       # yalnız Kil
    # Brüt mod: alanlar None, maliyet satırı / uyarı / not yok
    items = [it(1, "Kısmi", stock=500), it(2, "Bol", stock=10 ** 6)]
    g = plan(items, [(1, 10), (2, 1)], stock_mode="gross")
    attach(g, PriceInputs(offers={1: [offer(1, "U", 4.52)], 2: [offer(2, "S", 2.0)]}), None)
    assert all(m["stock_qty_used"] is None and m["stock_value"] is None and m["open_value"] is None
               for m in g["materials"])
    assert g["pricing"]["cost_summary"]["applies"] is False
    assert g["pricing"]["stock_totals"]["all"] == 0
    assert cost_summary_lines(g) == [] and cost_summary_caveats(g) == []
    assert not any("yenileme değeri" in n for n in sections(g)[-1]["rows"])


def test_open_orders_stock_plus_open_plus_buy_equals_need():
    from core.purchase_plan import OpenOrderRec
    from core.purchase_pricing import cost_summary_lines, cover_value
    items = [it(1, "Kısmi", stock=500), it(2, "Yoldan yeten", stock=300)]
    oo = {1: [OpenOrderRec(item_id=1, quantity=2000, unit="g")],
          2: [OpenOrderRec(item_id=2, quantity=800, unit="g")]}
    inp = PlanInputs(items={i.id: i for i in items}, recipes={1: rec(1, [(1, 10), (2, 1)])}, open_orders=oo,
                     stock_as_of=NOW)
    res = compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 1000}], options={"subtract_open_orders": True}))
    attach(res, PriceInputs(offers={1: [offer(1, "U", 4.52)], 2: [offer(2, "S", 10.0)]}), None)
    for m in res["materials"]:
        d = m["display"]
        assert m["stock_qty_used"] + m["open_qty_used"] + d["buy_num"] == pytest.approx(d["need_num"])
    a, b = mat(res, 1), mat(res, 2)
    assert (a["stock_qty_used"], a["open_qty_used"]) == (pytest.approx(0.5), pytest.approx(2.0))
    assert (a["stock_value"], a["open_value"], a["amount"]) == (2, 9, 34)                 # 0,5/2/7,5 × 4,52
    assert b["status"] == "yeterli" and b["amount"] is None
    assert (b["stock_qty_used"], b["open_qty_used"]) == (pytest.approx(0.3), pytest.approx(0.7))
    assert (b["stock_value"], b["open_value"], cover_value(b)) == (3, 7, 10)
    cs = res["pricing"]["cost_summary"]
    assert (cs["open_rows"], cs["open_total"], cs["stock_total"], cs["plan_total"]) == (2, 16, 5, 55)
    assert [x["key"] for x in cost_summary_lines(res)] == ["buy", "stock", "open", "plan"]
    suff = next(s for s in sections(res) if s["key"] == "sufficient")
    assert suff["total"] == 10 and suff["summary"] == "1 kalem, stoktaki ve yoldaki değer 10 $"


def test_plan_total_uses_amount_not_package_rounding():
    res = _cover_plan(round_to_package=True)
    m = mat(res, 1)
    res2 = plan([it(1, "Yağ", stock=500)], [(1, 107.75)], round_to_package=True)
    attach(res2, PriceInputs(offers={1: [offer(1, "Befchem", 8.0, pkg=25)]}), None, round_to_package=True)
    y = mat(res2, 1)
    assert y["amount"] == 858 and y["pkg_amount"] == 1000 and y["stock_value"] == 4    # 107,3 / 0,5 × 8
    cs = res2["pricing"]["cost_summary"]
    assert cs["buy_total"] == res2["pricing"]["totals"]["all"] == 858
    assert cs["plan_total"] == 862                                                      # 858 + 4, pkg_amount değil
    assert m["stock_value"] == 2 and res["pricing"]["cost_summary"]["plan_total"] == 56


def test_cover_values_unsafe_rounding_never_exceeds_need():
    """Düz yuvarlamada (`safe_rounding=False`) adet satırının alınacağı
    gereken'den bağımsız tama yuvarlanır: 10,6 gereken, 3 stok → 8 alınacak.
    Stoktan = gereken − alınacak (2,6) — MIN(gereken, elimizde) = 3 olsaydı
    11 birim değerlenirdi; kesirli stok (3,6 → "4") da olmayan birimi saymaz."""
    def run(stock, waste, **kw):
        res = plan([it(1, "Şişe", cat="Ambalaj", unit="adet", stock=stock, pkg="şişe")], [(1, 1)], qty=10,
                   extra_waste_pct={"raw": 0, "packaging": waste, "label": 0}, **kw)
        attach(res, PriceInputs(offers={1: [offer(1, "X", 10.0, unit="adet")]}), None)
        return res, mat(res, 1)

    res, m = run(3, 6, safe_rounding=False)
    d = m["display"]
    assert (d["need_num"], d["stock_num"], d["buy_num"]) == (pytest.approx(10.6), 3, 8)
    assert m["amount"] == 80 and res["pricing"]["totals"]["all"] == 80                    # tutar değişmez
    assert m["stock_qty_used"] == pytest.approx(2.6) and m["stock_value"] == 26
    assert m["stock_qty_used"] + d["buy_num"] == pytest.approx(d["need_num"])
    assert res["pricing"]["cost_summary"]["plan_total"] == 106
    res, m = run(3.6, 4, safe_rounding=False)
    assert (m["display"]["stock_num"], m["display"]["buy_num"]) == (4, 7)
    assert m["stock_qty_used"] == pytest.approx(3.4) and m["stock_value"] == 34           # 10,4 − 7
    # Güvenli yuvarlamada (varsayılan) aynı plan: stoktan = elimizde = MIN(D,E)
    res, m = run(3.6, 4)
    d = m["display"]
    assert (d["need_num"], d["stock_num"], d["buy_num"]) == (11, 3, 8)
    assert m["stock_qty_used"] == min(d["need_num"], d["stock_num"]) == 3 and m["stock_value"] == 30


# ─── Ambalaj uyarısı (16.10.2026) ───────────────────────────────────────────

def _pkg_codes(m):
    return [c["code"] for c in m["cautions"] if c["code"] in ("package_small", "package_large")]


def _pkg_text(m):
    return next(c["text"] for c in m["cautions"] if c["code"] in ("package_small", "package_large"))


def test_package_small_and_large_cautions_amount_unchanged():
    """TIBBI LAVANTA vakası: 26 g × 40,5 $/kg (25 kg ambalaj) → 1 $; uyarı
    "birim fiyat daha yüksek olabilir" + tam ambalaj tutarı.  60 kg alınacak
    25 kg ambalaj fiyatıyla → "daha düşük olabilir".  Tutar / toplam aynı."""
    from core.purchase_pricing import amount_warn, package_caution
    items = [it(1, "TIBBI LAVANTA UÇUCU YAĞI"), it(2, "GLİSERİN"), it(3, "ORTA")]
    res = plan(items, [(1, 0.026), (2, 60), (3, 30)])
    pin = PriceInputs(offers={1: [offer(1, "AROMA", 40.5, pkg=25)], 2: [offer(2, "UMAYCHEM", 2.0, pkg=25)],
                              3: [offer(3, "S", 2.0, pkg=25)]})
    attach(res, pin, None)
    lav, gli, orta = mat(res, 1), mat(res, 2), mat(res, 3)
    assert lav["amount"] == 1 and lav["price_package"] == 25 and lav["price_package_unit"] == "kg"
    assert _pkg_codes(lav) == ["package_small"]
    assert _pkg_text(lav) == ("Bu fiyat 25 kg'lık ambalaj için; alınacak 26 gram. Bu miktarda birim fiyat daha "
                              "yüksek olabilir — bu miktara özel fiyat alınmalı (tam ambalaj alınırsa 1.013 $).")
    assert amount_warn(lav) == _pkg_text(lav)
    assert _pkg_codes(gli) == ["package_large"] and gli["amount"] == 120
    assert _pkg_text(gli) == ("Bu fiyat 25 kg'lık ambalaj için; alınacak 60,0 kg. Bu miktarda birim fiyat daha "
                              "düşük olabilir — bu miktar için fiyat istenmeli.")
    assert _pkg_codes(orta) == [] and amount_warn(orta) is None          # 30 kg: 0,5×–2× arası
    assert next(c for c in lav["cautions"] if c["code"] == "package_small")["severity"] == "warn"
    assert res["pricing"]["totals"]["all"] == 1 + 120 + 60 and res["pricing"]["package_warned"] == 2
    assert res["summary"]["warnings"] == sum(1 for r in res["materials"] + res["products"] for c in r["cautions"]
                                             if c["severity"] == "warn")
    notes = next(s for s in sections(res) if s["key"] == "notes")["rows"]
    assert sum("ambalaj uyarısı, 2 satır" in n for n in notes) == 1
    # idempotent: yeniden attach / package_caution tek uyarı bırakır
    attach(res, pin, None)
    package_caution(lav)
    assert _pkg_codes(lav) == ["package_small"] and _pkg_codes(gli) == ["package_large"]


def test_package_caution_boundaries_half_and_double():
    """Tam 0,5× → uyarı YOK; tam 2× → uyarı VAR (20 kg ambalaj: 10 kg / 40 kg)."""
    res = plan([it(1, "Yarım"), it(2, "İki kat"), it(3, "Az altı")], [(1, 10), (2, 40), (3, 9.9)])
    attach(res, PriceInputs(offers={i: [offer(i, "S", 1.0, pkg=20)] for i in (1, 2, 3)}), None)
    assert mat(res, 1)["display"]["buy_num"] == pytest.approx(10.0) and _pkg_codes(mat(res, 1)) == []
    assert mat(res, 2)["display"]["buy_num"] == pytest.approx(40.0) and _pkg_codes(mat(res, 2)) == ["package_large"]
    assert _pkg_codes(mat(res, 3)) == ["package_small"]


def test_package_caution_skips_no_package_sufficient_held_and_unpriced():
    items = [it(1, "Ambalajsız"), it(2, "Yeterli", stock=10 ** 6), it(3, "Bekletilen"), it(4, "Fiyatsız"),
             it(5, "Sıfır ambalaj")]
    res = plan(items, [(1, 0.01), (2, 0.01), (3, 0.01), (4, 0.01), (5, 0.01)],
               held_items=[{"item_id": 3, "reason": "teyit"}])
    pin = PriceInputs(offers={1: [offer(1, "S", 9.0)], 2: [offer(2, "S", 9.0, pkg=25)],
                              3: [offer(3, "S", 9.0, pkg=25)], 4: [offer(4, "S", None, pkg=25)],
                              5: [offer(5, "S", 9.0, pkg=0)]})
    attach(res, pin, None)
    for i in (1, 2, 4, 5):
        assert _pkg_codes(mat(res, i)) == [], i
    assert [c["code"] for c in res["held"][0]["cautions"] if c["code"].startswith("package")] == []
    assert res["pricing"]["package_warned"] == 0
    assert not any("ambalaj uyarısı" in n for n in sections(res)[-1]["rows"])


def test_package_caution_litre_card_with_kg_price_and_round_to_package():
    """ml kartı + kg fiyatı (1 l ≈ 1 kg, `pkg_buy` ile aynı kural): 500 ml <
    12,5 → uyarı.  Ambalaj katı seçeneği açıkken "tam ambalaj" eki yazılmaz
    (yuvarlanmış satır zaten görünüyor)."""
    from core.purchase_pricing import pkg_round_text
    res = plan([it(1, "Hidrosol", unit="ml")], [(1, 0.5)])
    attach(res, PriceInputs(offers={1: [offer(1, "S", 4.0, pkg=25)]}), None)
    m = mat(res, 1)
    assert m["price_unit"] == "l" and m["price_package_unit"] == "kg"
    assert _pkg_text(m) == ("Bu fiyat 25 kg'lık ambalaj için; alınacak 500 ml. Bu miktarda birim fiyat daha "
                            "yüksek olabilir — bu miktara özel fiyat alınmalı (tam ambalaj alınırsa 100 $).")
    assert pkg_round_text(m) is None
    res = plan([it(1, "Hidrosol", unit="ml")], [(1, 0.5)], round_to_package=True)
    attach(res, PriceInputs(offers={1: [offer(1, "S", 4.0, pkg=25)]}), None, round_to_package=True)
    m = mat(res, 1)
    assert m["pkg_buy"] == 25 and "tam ambalaj" not in _pkg_text(m)
    assert _pkg_text(m).endswith("bu miktara özel fiyat alınmalı.")
    assert pkg_round_text(m) == "Ambalaj katına yuvarlanırsa: 25,0 litre · 100 $"


# ─── Plan maliyet özeti (16.10.2026) ────────────────────────────────────────

def _co(res):
    return res["pricing"]["cost_overview"]


def test_cost_overview_categories_sum_to_plan_total_and_unpriced_links():
    from core.purchase_pricing import cost_overview_view
    items = [it(1, "Kısmi", stock=500), it(2, "Yeterli", stock=10 ** 6), it(3, "Fiyatsız yeterli", stock=10 ** 6),
             it(4, "Alınacak fiyatsız"), it(5, "Kavanoz", cat="Ambalaj", unit="adet", pkg="kavanoz", stock=200),
             it(6, "Etiket TR", cat="Ambalaj", unit="adet", pkg="etiket")]
    res = plan(items, [(1, 10), (2, 2.5), (3, 1), (4, 1), (5, 1), (6, 1)],
               held_items=[{"item_id": 6, "reason": "teyit"}])
    pin = PriceInputs(offers={1: [offer(1, "UMAYCHEM", 4.52)], 2: [offer(2, "BEFCHEM", 4.52)],
                              5: [offer(5, "CAM", 0.5, unit="adet")]})
    attach(res, pin, None)
    co, cs = _co(res), res["pricing"]["cost_summary"]
    cats = {c["kind"]: c for c in co["categories"]}
    assert sum(c["total"] for c in co["categories"]) == co["plan_total"] == cs["plan_total"]
    assert cats["raw"] == {"kind": "raw", "label": "Hammadde", "buy": 43, "stock": 13, "open": 0, "total": 56,
                           "priced": 2, "unpriced": 2}
    assert cats["packaging"]["buy"] == 400 and cats["packaging"]["stock"] == 100     # 800 + 200 adet × 0,5
    assert cats["label"]["total"] == 0 and cats["label"]["priced"] + cats["label"]["unpriced"] == 0  # bekletilen
    assert co["total"]["total"] == cs["plan_total"] == 556
    names = [u["name"] for u in co["unpriced"]]
    assert names == ["Alınacak fiyatsız", "Fiyatsız yeterli"]
    assert co["unpriced"][0]["link"] == "/fiyatlar?q=Al%C4%B1nacak%20fiyats%C4%B1z"
    v = cost_overview_view(res)
    assert v["unpriced_count"] == 2 and v["total_row"]["total"] == "556 $" and v["plan_total"] == "556 $"
    assert v["caveats"][0] == ("2 kalemin fiyatı yok (2 hammadde) — toplama girmedi; gerçek maliyet bundan "
                               "yüksek olacak.")
    assert any("(1)" in n and "Bekletilen" in n for n in v["notes"])
    sec = next(s for s in sections(res) if s["key"] == "cost")
    assert sec["title"] == "Maliyet özeti" and sec["summary"] == "1 ürün, 1.000 adet — malzeme maliyeti 556 $"
    keys = [s["key"] for s in sections(res)]
    assert keys.index("cost") == keys.index("products") + 1
    # amount / totals / GENEL TOPLAM değişmedi
    assert res["pricing"]["totals"] == {"raw": 43, "packaging": 400, "label": 0, "all": 443}


def test_product_costs_shared_material_and_new_labels_without_link():
    """İki ürünün paylaştığı malzeme iki satıra ayrılır (birim tüketim × fiyat
    × sipariş adedi); yeni etiket satırı fiyatsız sayılır, bağlantısız ve
    kendi ürününün eksik kalemine yazılır (ürün maliyeti sessizce tam görünmez)."""
    from core.purchase_pricing import cost_overview_view, product_cost_lines
    items = [it(1, "Shea"), it(2, "Kavanoz", cat="Ambalaj", unit="adet", pkg="kavanoz")]
    inp = PlanInputs(items={i.id: i for i in items},
                     recipes={1: rec(1, [(1, 5), (2, 1)], name="Krem"), 2: rec(2, [(1, 2.5)], name="Losyon")},
                     stock_as_of=NOW)
    res = compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 1000}, {"recipe_id": 2, "qty": 400}],
                                   options={"label_mode": "new"}))
    attach(res, PriceInputs(offers={1: [offer(1, "BEFCHEM", 8.0)]}), None)
    shea = mat(res, 1)
    assert [(p["no"], p["per_unit"]) for p in shea["per_product"]] == [(1, 5.0), (2, 2.5)]
    lines = product_cost_lines(res)
    assert [(x["no"], x["name"]) for x in lines] == [(1, "Shea"), (1, "Kavanoz"), (2, "Shea")]
    a, jar, b = lines
    assert a["per_unit"] == pytest.approx(0.005) and a["unit"] == "kg" and a["unit_cost"] == pytest.approx(0.04)
    assert a["total_qty"] == pytest.approx(5.0) and a["total"] == pytest.approx(40.0)
    assert jar["price"] is None and jar["unit_cost"] is None and jar["per_unit"] == 1
    assert b["total"] == pytest.approx(8.0)
    co = _co(res)
    p1, p2 = co["products"]
    assert (p1["unit_cost"], p1["total"], p1["missing"], p1["missing_names"]) == (
        pytest.approx(0.04), 40, 2, ["Kavanoz", "Yeni etiket — Krem"])
    assert (p2["unit_cost"], p2["total"], p2["missing"], p2["missing_names"]) == (
        pytest.approx(0.02), 8, 1, ["Yeni etiket — Losyon"])
    assert co["products_total"] == 48 == co["plan_total"]
    assert [u["name"] for u in co["unpriced"]] == ["Kavanoz", "Yeni etiket — Krem", "Yeni etiket — Losyon"]
    assert [u["link"] for u in co["unpriced"]] == ["/fiyatlar?q=Kavanoz", None, None]
    assert {c["kind"]: c["unpriced"] for c in co["categories"]} == {"raw": 0, "packaging": 1, "label": 2}
    q = co["quantities"]
    assert (q["products"], q["units"], q["raw"]["kg"], q["packaging"]["adet"]) == (2, 1400, pytest.approx(6.0), 1000)
    assert (q["labels_new"], q["labels_new_rows"]) == (1400, 2)
    v = cost_overview_view(res)
    assert [x["unit_cost"] for x in v["products"]] == ["0,04 $", "0,02 $"]
    assert v["products"][0]["missing"] == "eksik: 2 kalem" and v["products"][1]["missing"] == "eksik: 1 kalem"
    assert v["products"][1]["missing_names"] == "Yeni etiket — Losyon"
    assert v["products"][1]["note"] == ""   # Shea fiyatlı: "hiçbirinin fiyatı yok" değil
    assert not any("ambalaj yuvarlamasından" in n for n in v["notes"])
    assert any("ambalaj katına yuvarlanmış tutar iki toplama da girmez" in n for n in v["notes"])
    assert [(x["label"], x["text"]) for x in v["quantities"]] == [
        ("Ürün", "2 ürün, 1.400 adet"), ("Hammadde", "6,0 kg"), ("Ambalaj", "1.000 adet"),
        ("Yeni etiket", "1.400 adet (2 kalem)")]


def test_cost_overview_finished_kit_ingredient_has_no_price_link():
    """Kit bileşeni bitmiş ürün kartı plan için "hammadde"dir ama Malzeme
    Fiyatları'nda listelenmez → "Fiyat gir →" bağlantısı yok, notu ayrı; Kimyasal
    gibi "diğer" kategorili kart bağlantılı kalır (sayfa onu aramada gösterir)."""
    from core.purchase_pricing import UNPRICED_NOTE, cost_overview_view
    items = [it(1, "Shea"), it(2, "Mini Krem 10 ml", cat="Bitmiş Ürün", unit="adet"),
             it(3, "Sitrik Asit", cat="Kimyasal")]
    res = plan(items, [(1, 5), (2, 1), (3, 0.5)], qty=100)
    attach(res, PriceInputs(offers={1: [offer(1, "BEFCHEM", 8.0)]}), None)
    co = _co(res)
    by = {u["name"]: u for u in co["unpriced"]}
    assert by["Mini Krem 10 ml"]["link"] is None and by["Mini Krem 10 ml"]["finished"] is True
    assert by["Mini Krem 10 ml"]["new"] is False and by["Mini Krem 10 ml"]["kind"] == "raw"
    assert by["Sitrik Asit"]["link"] == "/fiyatlar?q=Sitrik%20Asit" and by["Sitrik Asit"]["finished"] is False
    v = {u["name"]: u for u in cost_overview_view(res)["unpriced"]}
    assert v["Mini Krem 10 ml"]["note"] == UNPRICED_NOTE["finished"]
    assert v["Sitrik Asit"]["note"] == UNPRICED_NOTE["card"]


def test_cost_overview_merged_cards_finished_stock_recipeless_and_gross():
    from core.purchase_pricing import cost_overview_view
    # Birleştirilmiş kartlar: teklif ikinci kartta, ürün maliyeti birleşik satırdan
    a, b = it(1, "ALOE VERA EKSTRAKTI", unit="ml"), it(2, "ALOEVERA EKSTRAKTI", unit="ml")
    tgt = ItemRec(id=9, name="Tonik", category="Bitmiş Ürün", unit="adet", current_stock=5000)
    inp = PlanInputs(items={1: a, 2: b, 9: tgt}, recipes={1: RecipeRec(
        id=1, name="Tonik", target_item_id=9, ingredients=(IngredientRec(item_id=1, quantity=4.2),))},
        stock_as_of=NOW)
    pin = PriceInputs(offers={2: [offer(2, "NATURALYA", 10.32)]})
    res = compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 1000}], options={"manual_merges": [[2, 1]]}))
    attach(res, pin, None)
    p = _co(res)["products"][0]
    assert p["unit_cost"] == pytest.approx(0.043344) and p["total"] == 43 and p["missing"] == 0
    assert _co(res)["plan_total"] == 43
    # Bitmiş stok siparişi tümüyle karşılıyor: ihtiyaç 0 ama 1 adet maliyeti hesaplanır,
    # toplam sipariş adedi üzerinden; plan toplamı 0 (alınacak / stoktan yok)
    res = compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 1000}],
                                   options={"manual_merges": [[2, 1]], "subtract_finished_stock": True}))
    attach(res, pin, None)
    assert res["products"][0]["produce_qty"] == 0 and res["materials"][0]["need"] == 0
    co = _co(res)
    assert co["products"][0]["unit_cost"] == pytest.approx(0.043344) and co["products"][0]["total"] == 43
    assert co["plan_total"] == 0 and co["total"]["priced"] == 0 and co["unpriced"] == []
    assert any("Bitmiş ürün stoğu düşüldüğü için" in n for n in cost_overview_view(res)["notes"])
    # Reçetesiz ürün: satırı var, maliyeti yok
    items = [it(1, "Shea"), ItemRec(id=8, name="Reçetesiz Losyon", category="Bitmiş Ürün", unit="adet")]
    inp2 = PlanInputs(items={i.id: i for i in items}, recipes={1: rec(1, [(1, 5)], name="Krem")}, stock_as_of=NOW)
    res = compute(inp2, PlanRequest(lines=[{"recipe_id": 1, "qty": 100}, {"target_item_id": 8, "qty": 50}]))
    attach(res, PriceInputs(offers={1: [offer(1, "S", 8.0)]}), None)
    r1, r2 = _co(res)["products"]
    assert r1["total"] == 4 and r2["recipeless"] is True and r2["unit_cost"] is None and r2["total"] is None
    v = cost_overview_view(res)
    assert v["products"][1]["note"] == "reçetesi yok" and v["products"][1]["unit_cost"] == "—"
    # Brüt mod: stok değerlenmez, Σ tür = totals.all
    items = [it(1, "Kısmi", stock=500), it(2, "Bol", stock=10 ** 6)]
    g = plan(items, [(1, 10), (2, 1)], stock_mode="gross")
    attach(g, PriceInputs(offers={1: [offer(1, "U", 4.52)], 2: [offer(2, "S", 2.0)]}), None)
    co = _co(g)
    assert co["net"] is False and co["plan_total"] == g["pricing"]["totals"]["all"] == 47      # 45 + 2
    assert sum(c["total"] for c in co["categories"]) == 47 and co["total"]["stock"] == 0
    v = cost_overview_view(g)
    assert v["show_stock"] is False and not any("yenileme değeri" in n for n in v["notes"])
    sec = next(s for s in sections(g) if s["key"] == "cost")
    assert "Planın toplam malzeme maliyeti" not in sec["subtitle"]


def test_cost_overview_absent_without_pricing():
    res = plan([it(1, "A")], [(1, 1)])
    assert "cost" not in [s["key"] for s in sections(res)]
