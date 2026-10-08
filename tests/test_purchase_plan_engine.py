"""
Satın Alma Planı hesap motoru (core/purchase_plan.py) testleri.

Büyük kısmı SAF (DB'siz): `ItemRec`/`RecipeRec` doğrudan kurulur, `compute()`
çağrılır.  conftest her testte tabloları yeniden kurduğu için DB yine de
gerekir — izole DB ile koşun:
    MINERVA_TEST_DB=minerva_test_u .venv/bin/pytest tests/test_purchase_plan_engine.py -q

Kapsam: net/brüt · açık sipariş · tür bazında alım firesi · etiket modları
(TR/EN/hariç/yeni + yüz) · sanal birleştirme (+ 'kept' engeli, farklı birim →
benzer kart) · "aynı malzeme" grubu (birleşmez, benzer-ad bastırılır,
alternatifler + group_alt_stock, ihtiyaç değişmez) · ikinci kart uyarısı ·
triple5 tablo · hariç/bekletilen ·
reçetesiz / ambalajsız / boy uyuşmazlığı / çift satır · kapasite · ürün
dökümü · bitmiş stok düşme · kapsam metni · DB yükleyici (domain kapsamı) ·
"bitirilecek" tedarikçi kartının grup stoğu (ihtiyaçtan düşülür, satırlar
arası ortak havuz, seçenek kapalı / brüt / planda kendi satırı → eski
davranış, "alma" tercihi, DB yükleyicide firma anahtarı düzeyinde durum).
"""
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from core.consumption import IngredientRec, ItemRec, RecipeRec
from core.purchase_plan import (DecisionRec, LotRec, OpenOrderRec, PlanInputError, PlanInputs,
                                compute, default_excluded_key, face_of, label_base, load_inputs,
                                material_group_members, merge_map, resolve_default_excluded, safe_triplet)
from core.purchase_plan_models import PlanRequest

NOW = datetime(2026, 10, 5, 9, 30)      # UTC → TR 12:30


# ─── Yardımcılar ────────────────────────────────────────────────────────────

def it(id, name, *, cat="Hammadde", unit="g", stock=0.0, pkg=None, lang=None, group=None,
       active=True, domain="cosmetics"):
    return ItemRec(id=id, name=name, category=cat, unit=unit, pkg_type=pkg, language=lang,
                   label_group=group, current_stock=stock, is_active=active, domain=domain)


def rec(id, name, ings, *, out=1.0, waste=0.0, target=None):
    return RecipeRec(id=id, name=name, output_quantity=out, waste_percentage=waste,
                     target_item_id=target,
                     ingredients=tuple(IngredientRec(item_id=i[0], quantity=i[1],
                                                     unit=(i[2] if len(i) > 2 else None))
                                       for i in ings))


def inputs(items, recipes, **kw):
    items = {i.id: i for i in items}
    recipes = {r.id: r for r in recipes}
    by_target = {r.target_item_id: r.id for r in sorted(recipes.values(), key=lambda r: r.id)
                 if r.target_item_id}
    kw.setdefault("stock_as_of", NOW)
    return PlanInputs(items=items, recipes=recipes, recipe_by_target=by_target, **kw)


def req(lines, **opts):
    return PlanRequest(lines=lines, options=opts)


def mat(res, item_id, where="materials"):
    for m in res[where]:
        if item_id in m["member_ids"]:
            return m
    raise AssertionError(f"{item_id} {where} içinde yok")


def codes(row):
    return [c["code"] for c in row["cautions"]]


# Ortak küçük katalog: 50 ml krem (Minerva), yağ + su + kavanoz + etiket
OIL = it(1, "Shea Butter", unit="g", stock=400)
WATER = it(2, "DİSTİLE SU", unit="ml", stock=10000)
JAR = it(3, "50 ML CAM KAVANOZ", cat="Ambalaj", unit="adet", stock=40, pkg="kavanoz")
LBL_TR = it(4, "Krem 50 ml Ön (TR)", cat="Ambalaj", unit="adet", stock=5, pkg="etiket", lang="TR", group="g-on")
LBL_EN = it(5, "Krem 50 ml Ön (EN)", cat="Ambalaj", unit="adet", stock=7, pkg="etiket", lang="EN", group="g-on")
CREAM = it(10, "Minerva 108 Krem 50 ml", cat="Bitmiş Ürün", unit="adet", stock=30)
R_CREAM = rec(100, "Minerva 108 Krem 50ml", [(1, 10), (2, 40), (3, 1), (4, 1)], target=10)
SIBS = {"g-on": {"TR": LBL_TR, "EN": LBL_EN}}


def base_inputs(**kw):
    return inputs([OIL, WATER, JAR, LBL_TR, LBL_EN, CREAM], [R_CREAM], label_siblings=SIBS, **kw)


# ─── safe_triplet = urunler.triple5 ─────────────────────────────────────────

def test_triple5_large_grams_round_up_need_down_stock():
    d = safe_triplet(107750, 400, "g")
    assert (d["need_text"], d["stock_text"], d["buy_text"]) == ("107,8 kg", "0,4 kg", "107,4 kg")
    assert d["buy_num"] == pytest.approx(107.4) and d["buy_unit"] == "kg"


def test_triple5_small_ml_stays_integer_ml_buy_in_litre():
    d = safe_triplet(229, 11, "ml")
    assert (d["need_text"], d["stock_text"], d["buy_text"]) == ("229 ml", "11 ml", "218 ml")
    assert d["buy_num"] == pytest.approx(0.218) and d["buy_unit"] == "l"


def test_triple5_kg_unit_and_need_minus_stock_below_1000():
    d = safe_triplet(12.34, 2.01, "kg")
    assert (d["need_text"], d["stock_text"], d["buy_text"], d["buy_num"]) == ("12,4 kg", "2,0 kg", "10,4 kg", 10.4)
    # gereken ≥1000 g ama eksik <1000 g → küçük birimde tamsayı (triple5 ile aynı)
    d = safe_triplet(5000, 4500.4, "g")
    assert (d["need_text"], d["stock_text"], d["buy_text"]) == ("5.000 gram", "4.500 gram", "500 gram")
    assert d["buy_num"] == pytest.approx(0.5)


def test_triple5_adet_ceil_need_floor_stock():
    d = safe_triplet(10.2, 3.7, "adet")
    assert (d["need_text"], d["stock_text"], d["buy_text"], d["buy_num"]) == ("11 adet", "3 adet", "8 adet", 8)
    d = safe_triplet(5, 9, "adet")
    assert d["buy_num"] == 0 and d["buy_text"] == "0 adet"


def test_triple5_negative_stock_clamped_and_no_stock_is_yok():
    d = safe_triplet(500, -20, "g")
    assert d["stock_text"] == "yok" and d["buy_text"] == "500 gram" and d["buy_num"] == pytest.approx(0.5)
    d = safe_triplet(2000, 0.4, "ml")
    assert d["stock_text"] == "yok" and d["need_text"] == "2,0 litre"


def test_plain_rounding_when_safe_off():
    d = safe_triplet(107750, 400, "g", safe=False)
    assert d["buy_num"] == pytest.approx(107.35)
    assert d["need_text"] == "107,8 kg"
    d = safe_triplet(10.2, 3.7, "adet", safe=False)
    assert d["buy_num"] == 7


# ─── Net / brüt, açık sipariş, fire ─────────────────────────────────────────

def test_net_vs_gross():
    inp = base_inputs(default_excluded=(2,))
    line = {"recipe_id": 100, "qty": 100}
    net = compute(inp, req([line]))
    m = mat(net, 1)
    assert m["need"] == pytest.approx(1000) and m["stock"] == pytest.approx(400)
    assert m["buy"] == pytest.approx(600) and m["status"] == "to_buy"
    assert (m["display"]["need_text"], m["display"]["stock_text"], m["display"]["buy_text"]) == \
        ("1.000 gram", "400 gram", "600 gram")
    gross = compute(inp, req([line], stock_mode="gross"))
    g = mat(gross, 1)
    assert g["buy"] == pytest.approx(1000)
    assert g["display"]["need_text"] == g["display"]["buy_text"] == "1,0 kg"
    assert g["display"]["stock_text"] == "400 gram"          # bilgi amaçlı, düşülmez
    # brüt modda stok yetse bile alınır; netde yeterli
    j_net, j_gross = mat(net, 3), mat(gross, 3)
    assert j_net["buy"] == pytest.approx(60) and j_gross["buy"] == pytest.approx(100)
    small = compute(inp, req([{"recipe_id": 100, "qty": 10}]))
    assert mat(small, 3)["status"] == "yeterli"
    assert mat(compute(inp, req([{"recipe_id": 100, "qty": 10}], stock_mode="gross")), 3)["status"] == "to_buy"


def test_open_orders_on_off_and_missing_quantity():
    oo = {1: [OpenOrderRec(item_id=1, quantity=300, unit="g", supplier_name="Befchem")],
          3: [OpenOrderRec(item_id=3, quantity=None)]}
    inp = base_inputs(open_orders=oo, default_excluded=(2,))
    line = {"recipe_id": 100, "qty": 100}
    off = compute(inp, req([line]))
    assert mat(off, 1)["open_orders"] == 0 and mat(off, 1)["buy"] == pytest.approx(600)
    assert "open_order_no_qty" not in codes(mat(off, 3))
    assert mat(off, 1)["open_orders_info"][0]["supplier"] == "Befchem"
    on = compute(inp, req([line], subtract_open_orders=True))
    m = mat(on, 1)
    assert m["open_orders"] == pytest.approx(300) and m["available"] == pytest.approx(700)
    assert m["buy"] == pytest.approx(300)
    j = mat(on, 3)
    assert "open_order_no_qty" in codes(j) and j["open_orders"] == 0
    assert "yoldaki" in on["meta"]["scope_text"]
    # "Elimizde" yalnız eldeki stok; yoldaki ayrı sütun — yazılan sayılar tam çıkar
    d = m["display"]
    assert (d["need_text"], d["stock_text"], d["open_text"], d["buy_text"]) == \
        ("1.000 gram", "400 gram", "300 gram", "300 gram")
    assert round(d["need_num"] - d["stock_num"] - d["open_num"], 3) == pytest.approx(d["buy_num"]) == 0.3
    assert mat(off, 1)["display"]["open_text"] == "yok"
    g = mat(compute(inp, req([line], stock_mode="gross", subtract_open_orders=True)), 1)["display"]
    assert (g["need_text"], g["stock_text"], g["open_text"], g["buy_text"]) == \
        ("1,0 kg", "400 gram", "300 gram", "1,0 kg")


@pytest.mark.parametrize("need,stock,open_,unit,texts,buy", [
    (107750, 400, 2350, "g", ("107,8 kg", "0,4 kg", "2,3 kg", "105,1 kg"), 105.1),
    (229, 11, 7.6, "ml", ("229 ml", "11 ml", "7 ml", "211 ml"), 0.211),
    (10.2, 3.7, 2.9, "adet", ("11 adet", "3 adet", "2 adet", "6 adet"), 6),
    (5000, 4000, 2000, "g", ("5.000 gram", "4.000 gram", "2.000 gram", "0 gram"), 0),
])
def test_safe_triplet_open_orders_separate_and_never_below_shortage(need, stock, open_, unit, texts, buy):
    d = safe_triplet(need, stock, unit, open_qty=open_)
    assert (d["need_text"], d["stock_text"], d["open_text"], d["buy_text"]) == texts
    assert d["buy_num"] == pytest.approx(buy)
    assert max(0, round(d["need_num"] - d["stock_num"] - d["open_num"], 3)) == pytest.approx(d["buy_num"])
    k = 1000 if unit in ("g", "ml") else 1
    assert d["buy_num"] * k >= max(0.0, need - stock - open_) - 1e-9          # gerçek eksiğin altına inmez
    # open_qty=0 → triple5 ile birebir (eski davranış)
    assert {kk: v for kk, v in safe_triplet(need, stock, unit).items()
            if kk in ("need_text", "stock_text", "buy_text", "buy_num")} == \
        {kk: v for kk, v in safe_triplet(need, stock, unit, open_qty=0).items()
         if kk in ("need_text", "stock_text", "buy_text", "buy_num")}


def test_extra_waste_per_kind():
    inp = base_inputs(default_excluded=(2,))
    res = compute(inp, req([{"recipe_id": 100, "qty": 100}],
                           extra_waste_pct={"raw": 10, "packaging": 5, "label": 20}))
    assert mat(res, 1)["need_production"] == pytest.approx(1000)
    assert mat(res, 1)["need"] == pytest.approx(1100)
    assert mat(res, 3)["need"] == pytest.approx(105)
    lbl = mat(res, 4)
    assert lbl["kind"] == "label" and lbl["need"] == pytest.approx(120)
    assert "ek alım firesi: hammadde %10, ambalaj %5, etiket %20" in res["meta"]["scope_text"]


def test_recipe_waste_only_on_raw():
    r = rec(101, "Krem fire", [(1, 10), (3, 1)], waste=10, target=10)
    inp = inputs([OIL, JAR, CREAM], [r])
    res = compute(inp, req([{"recipe_id": 101, "qty": 10}]))
    assert mat(res, 1)["need"] == pytest.approx(110) and mat(res, 3)["need"] == pytest.approx(10)


# ─── Etiket modları ─────────────────────────────────────────────────────────

def test_label_modes_tr_en_exclude():
    inp = base_inputs(default_excluded=(2,))
    line = [{"recipe_id": 100, "qty": 10}]
    tr = compute(inp, req(line, label_mode="TR"))
    assert mat(tr, 4)["need"] == pytest.approx(10) and not tr["labels_new"]
    en = compute(inp, req(line, label_mode="EN"))
    assert mat(en, 5)["need"] == pytest.approx(10)
    assert all(4 not in m["member_ids"] for m in en["materials"])
    ex = compute(inp, req(line, label_mode="exclude"))
    assert all(m["kind"] != "label" for m in ex["materials"]) and not ex["labels_new"]


def test_label_mode_en_missing_sibling_skipped_with_caution():
    sibs = {"g-on": {"TR": LBL_TR}}
    inp = inputs([OIL, WATER, JAR, LBL_TR, CREAM], [R_CREAM], label_siblings=sibs, default_excluded=(2,))
    res = compute(inp, req([{"recipe_id": 100, "qty": 10}], label_mode="EN"))
    assert "labels_skipped" in codes(res["products"][0])
    assert res["skipped_labels"][0]["language"] == "EN"


def test_recipe_packaging_off_keeps_product_labels_in_tr_en():
    """⚙ "reçete ambalajını kullan" kapalı + ek ambalaj = kavanoz değişti;
    etiket ürünündür, TR/EN modunda listeden SESSİZCE düşmemeli."""
    line = [{"recipe_id": 100, "qty": 10, "use_recipe_packaging": False,
             "extra_packaging": [{"item_id": 3}]}]
    tr = compute(base_inputs(default_excluded=(2,)), req(line, label_mode="TR"))
    assert mat(tr, 4)["need"] == pytest.approx(10) and mat(tr, 4)["kind"] == "label"
    assert mat(tr, 3)["need"] == pytest.approx(10)              # yalnız ek ambalaj (reçetedeki düştü)
    en = compute(base_inputs(default_excluded=(2,)), req(line, label_mode="EN"))
    assert mat(en, 5)["need"] == pytest.approx(10)
    ex = compute(base_inputs(default_excluded=(2,)), req(line, label_mode="exclude"))
    assert all(m["kind"] != "label" for m in ex["materials"])
    # kardeşi olmayan dil → yine atlanır ama UYARIYLA
    only_tr = inputs([OIL, WATER, JAR, LBL_TR, CREAM], [R_CREAM], label_siblings={"g-on": {"TR": LBL_TR}},
                     default_excluded=(2,))
    res = compute(only_tr, req(line, label_mode="EN"))
    assert "labels_skipped" in codes(res["products"][0]) and res["skipped_labels"][0]["language"] == "EN"


def test_label_mode_new_faces_from_groups_siblings_and_override():
    back = it(6, "Krem 50 ml Arka (EN)", cat="Ambalaj", unit="adet", pkg="etiket", lang="EN", group="g-arka")
    inp = inputs([OIL, WATER, JAR, LBL_TR, LBL_EN, back, CREAM], [R_CREAM], label_siblings=SIBS,
                 default_excluded=(2,))
    res = compute(inp, req([{"recipe_id": 100, "qty": 100}], label_mode="new", new_label_title="Rusça etiket"))
    assert all(m["kind"] != "label" for m in res["materials"])
    lab = res["labels_new"][0]
    # reçetede yalnız 'Ön' var; aynı ürünün 'Arka' kartı sistemde → 2 yüz
    assert lab["faces"] == 2 and lab["face_text"] == "ön + arka" and lab["faces_source"] == "siblings"
    assert lab["total"] == 200 and lab["name"] == "Rusça etiket — Minerva 108 Krem 50 ml"
    over = compute(inp, req([{"recipe_id": 100, "qty": 100, "label_faces": 3}], label_mode="new"))
    assert over["labels_new"][0]["faces"] == 3 and over["labels_new"][0]["total"] == 300
    waste = compute(inp, req([{"recipe_id": 100, "qty": 100}], label_mode="new", extra_waste_pct={"label": 10}))
    assert waste["labels_new"][0]["total"] == 220
    assert waste["meta"]["counts"]["labels_new_total"] == 220


def test_label_mode_new_single_face_hint_and_no_label_default():
    inp = base_inputs(default_excluded=(2,))
    res = compute(inp, req([{"recipe_id": 100, "qty": 5}], label_mode="new"))
    assert res["labels_new"][0]["faces"] == 1
    assert "ikinci etiket" in res["labels_new"][0]["face_text"]
    r = rec(102, "Etiketsiz krem", [(1, 10)], target=10)
    res = compute(inputs([OIL, CREAM], [r]), req([{"recipe_id": 102, "qty": 5}], label_mode="new"))
    assert res["labels_new"][0]["faces"] == 1 and res["labels_new"][0]["faces_source"] == "default"


def test_face_helpers():
    assert face_of("MİNERVA-108 SHOWER GEL 400 ml Ön (EN)") == "ön"
    assert face_of("Minerva-108 Intense Haır Mask 100ml Etiket Üst") == "üst"
    assert face_of("Minerva-108 Hair Conditioner 200ml Etiket") is None
    assert label_base("Body Lotion Lavanta Ön (EN) 200 ml") == label_base("Body Lotion Lavanta Arka (EN)200 ml")


def test_label_faces_language_sets_do_not_double_count():
    # EN takımı üst+yan, TR takımı ön+arka adlandırılmış: reçetedeki EN 'üst' → 2 yüz (4 değil)
    up = it(20, "Mask 200ml üst", cat="Ambalaj", unit="adet", pkg="etiket", lang="EN", group="m-ust")
    side = it(21, "Mask 200ml yan", cat="Ambalaj", unit="adet", pkg="etiket", lang="EN", group="m-yan")
    front = it(22, "Mask 200ml Ön(TR)", cat="Ambalaj", unit="adet", pkg="etiket", lang="TR", group="m-on")
    back = it(23, "Mask 200ml Arka (TR)", cat="Ambalaj", unit="adet", pkg="etiket", lang="TR", group="m-arka")
    r = rec(103, "Mask 200ml", [(1, 10), (20, 1)], target=None)
    inp = inputs([OIL, up, side, front, back], [r], label_siblings={"m-ust": {"EN": up}})
    res = compute(inp, req([{"recipe_id": 103, "qty": 10}], label_mode="new"))
    assert res["labels_new"][0]["faces"] == 2 and res["labels_new"][0]["face_text"] == "üst + yan"


# ─── Sanal birleştirme ──────────────────────────────────────────────────────

GLY_A = it(30, "GLİSERİN", unit="g", stock=100)
GLY_B = it(31, "GLISERIN ", unit="g", stock=5000)
GLY_ML = it(32, "Gliserin", unit="ml", stock=900)
R_GLY = rec(110, "Losyon 200 ml", [(30, 20)], target=None)


def test_merge_same_folded_name_and_unit_sums_stock():
    inp = inputs([GLY_A, GLY_B, GLY_ML], [R_GLY])
    res = compute(inp, req([{"recipe_id": 110, "qty": 100}]))
    m = mat(res, 30)
    assert m["member_ids"] == [30, 31] and m["item_id"] == 30
    assert m["stock"] == pytest.approx(5100) and m["stock_used_cards"] == pytest.approx(100)
    assert m["stock_other_cards"] == pytest.approx(5000)
    assert "duplicate_merged" in codes(m)
    # farklı birim birleşmez → benzer kart uyarısı
    assert 32 not in m["member_ids"]
    look = next(c for c in m["cautions"] if c["code"] == "lookalike")
    assert "“Gliserin” 900 ml" in look["text"]


def test_kept_decision_blocks_merge_and_suppresses_lookalike():
    dec = [DecisionRec(item_ids=frozenset({30, 31}), status="kept", title="Gliserin")]
    inp = inputs([GLY_A, GLY_B], [R_GLY], decisions=dec)
    res = compute(inp, req([{"recipe_id": 110, "qty": 100}]))
    m = mat(res, 30)
    assert m["member_ids"] == [30] and m["stock"] == pytest.approx(100)
    assert "lookalike" not in codes(m) and "duplicate_merged" not in codes(m)


def test_merge_off_and_manual_merge_root_is_recipe_member():
    a = it(40, "ALOEVERA EKSTRAKTI", unit="ml", stock=0)
    b = it(41, "ALOE VERA EKSTRAKTI", unit="ml", stock=5)
    r = rec(111, "Jel", [(41, 10)])
    inp = inputs([a, b], [r])
    root_of, members = merge_map(inp, req([{"recipe_id": 111, "qty": 1}], manual_merges=[[40, 41]]).options)
    assert members == {41: [40, 41]} and root_of[40] == 41      # reçetedeki kart kök (en küçük id değil)
    res = compute(inp, req([{"recipe_id": 111, "qty": 1}], manual_merges=[[40, 41]]))
    assert mat(res, 41)["item_id"] == 41 and mat(res, 41)["member_ids"] == [40, 41]
    res = compute(inputs([GLY_A, GLY_B], [R_GLY]), req([{"recipe_id": 110, "qty": 1}], merge_duplicates=False))
    assert mat(res, 30)["member_ids"] == [30]


def test_pending_decision_caution():
    dec = [DecisionRec(item_ids=frozenset({30, 99}), status="pending", title="Gliserin kümesi")]
    inp = inputs([GLY_A], [R_GLY], decisions=dec)
    m = mat(compute(inp, req([{"recipe_id": 110, "qty": 1}])), 30)
    assert "pending_duplicate_decision" in codes(m)


@pytest.mark.parametrize("other_stock,word", [(20000, "tamamı"), (19500, "neredeyse tamamı"), (12000, "bir kısmı")])
def test_other_card_stock_wording_and_bulunmazsa(other_stock, word):
    used = it(50, "LAURYL GLUCOSİDE", unit="g", stock=20000 - other_stock if word != "tamamı" else 0)
    other = it(51, "LAURYL GLUCOSIDE", unit="g", stock=other_stock)
    r = rec(112, "Şampuan", [(50, 300)])
    res = compute(inputs([used, other], [r]), req([{"recipe_id": 112, "qty": 100}]))
    c = next(c for c in mat(res, 50)["cautions"] if c["code"] == "other_card_stock")
    assert f"Stoğun {word} (" in c["text"] and "Bulunmazsa alınacak" in c["text"]
    # brüt modda "Bulunmazsa" cümlesi yok
    g = compute(inputs([used, other], [r]), req([{"recipe_id": 112, "qty": 100}], stock_mode="gross"))
    c = next(c for c in mat(g, 50)["cautions"] if c["code"] == "other_card_stock")
    assert "Bulunmazsa" not in c["text"]


def test_other_card_stock_below_threshold_silent():
    used = it(50, "LAURYL GLUCOSİDE", unit="g", stock=100)
    other = it(51, "LAURYL GLUCOSIDE", unit="g", stock=9000)
    r = rec(112, "Şampuan", [(50, 300)])
    m = mat(compute(inputs([used, other], [r]), req([{"recipe_id": 112, "qty": 100}])), 50)
    assert "other_card_stock" not in codes(m) and "duplicate_merged" in codes(m)


def test_lookalike_guards_short_token_and_size_digits():
    e_vit = it(60, "E VİTAMİNİ", unit="g", stock=10)
    c_vit = it(61, "C VİTAMİNİ", unit="g", stock=30)
    j100 = it(62, "100 ML PET ŞEFFAF KAVANOZ", cat="Ambalaj", unit="adet", stock=10)
    j150 = it(63, "150 ML PET ŞEFFAF KAVANOZ", cat="Ambalaj", unit="adet", stock=50)
    dp = it(64, "D-PANTHENOL", unit="g", stock=1)
    dp2 = it(65, "DEPANTHANOL", unit="g", stock=4700)
    r = rec(113, "Serum", [(60, 1), (62, 1), (64, 1)])
    res = compute(inputs([e_vit, c_vit, j100, j150, dp, dp2], [r]), req([{"recipe_id": 113, "qty": 1}]))
    assert "lookalike" not in codes(mat(res, 60))
    assert "lookalike" not in codes(mat(res, 62))
    assert "lookalike" in codes(mat(res, 64))
    off = compute(inputs([e_vit, dp, dp2], [rec(114, "S", [(64, 1)])]),
                  req([{"recipe_id": 114, "qty": 1}], lookalike_check=False))
    assert "lookalike" not in codes(mat(off, 64))


# ─── Uyarılar ───────────────────────────────────────────────────────────────

def test_material_cautions_negative_unit_adet_samples_note_mismatch():
    neg = it(70, "TUZ", unit="kg", stock=-3)
    adet = it(71, "PALMORASA UÇUCU YAGI", unit="adet", stock=0)
    r = rec(115, "Peeling 150 ml", [(70, 0.27, "g"), (71, 0.5)])
    lots = {71: [LotRec(item_id=71, lot_number="N1", is_sample=True, quantity=50, supplier_name="X")]}
    res = compute(inputs([neg, adet], [r], lots=lots),
                  req([{"recipe_id": 115, "qty": 10}], item_notes={70: "Reçete birimi kontrol edilecek."}))
    t = mat(res, 70)
    assert t["stock"] == 0 and "negative_stock" in codes(t)
    assert "unit_mismatch" in codes(t)
    assert "item_note" in codes(t)
    p = mat(res, 71)
    assert "raw_unit_adet" in codes(p) and "samples_available" in codes(p)


def test_suspect_unit_on_material_row_tuz_case():
    """TUZ vakası: kart 'kg', reçetede 269,5 yazılmış (gram demek istenmiş).
    Reçete birimi kayıtta daima kart birimi olduğu için unit_mismatch bunu
    göremez — boyla kıyas MALZEME satırına uyarı düşmeli."""
    salt = it(90, "TUZ", unit="kg", stock=0)
    oil = it(91, "Badem Yağı", unit="g", stock=0)
    scrub = it(92, "Minerva 108 Body Scrub 350 ml", cat="Bitmiş Ürün", unit="adet")
    r = rec(130, "Minerva 108 Body Scrub 350 ml", [(90, 269.5, "kg"), (91, 80, "g")], target=92)
    res = compute(inputs([salt, oil, scrub], [r]), req([{"recipe_id": 130, "qty": 1629}]))
    t = mat(res, 90)
    su = next(c for c in t["cautions"] if c["code"] == "suspect_unit")
    assert su["severity"] == "warn"
    assert "“Minerva 108 Body Scrub 350 ml”" in su["text"] and "269,5 kg" in su["text"]
    assert "ürün boyu 350 ml" in su["text"] and "1 adet için 269,5 g" in su["text"]
    assert "unit_mismatch" not in codes(t)                      # birimler kayıtta aynı
    assert "suspect_unit" not in codes(mat(res, 90 + 1))        # 80 g / 350 ml makul
    assert "recipe_total_vs_size" in codes(res["products"][0])  # ürün uyarısı da duruyor
    # yoğun ama makul tek bileşen (tuz peelingi: 300 g / 350 ml) uyarılmaz
    ok = rec(131, "Minerva 108 Body Scrub 350 ml", [(90, 0.3, "kg"), (91, 80, "g")], target=92)
    assert "suspect_unit" not in codes(mat(compute(inputs([salt, oil, scrub], [ok]),
                                                    req([{"recipe_id": 131, "qty": 1}])), 90))
    # boy bilinmiyorsa kıyas yapılmaz (yanlış alarm yok)
    nosize = rec(132, "Peeling", [(90, 269.5, "kg")])
    assert "suspect_unit" not in codes(mat(compute(inputs([salt], [nosize]),
                                                    req([{"recipe_id": 132, "qty": 1}])), 90))


def test_product_cautions_recipeless_no_packaging_size_and_duplicate_row():
    nameless = it(11, "Minerva 108 Yeni Serum 30 ml", cat="Bitmiş Ürün", unit="adet")
    r_bad = rec(116, "Minerva 108 Krem 50 ml", [(1, 10), (1, 10)], target=10)   # çift satır, 20 g ≠ 50 ml
    inp = inputs([OIL, CREAM, nameless], [r_bad])
    res = compute(inp, req([{"target_item_id": 11, "qty": 5}, {"recipe_id": 116, "qty": 10}]))
    p1, p2 = res["products"]
    assert codes(p1) == ["recipeless_product"] and p1["capacity"]["producible"] is None
    assert res["meta"]["counts"]["recipeless"] == 1
    assert "duplicate_ingredient_row" in codes(p2)
    assert "no_packaging_in_recipe" in codes(p2) and p2["has_packaging"] is False
    assert "recipe_total_vs_size" in codes(p2)
    assert p2["size_ml"] == 50
    assert mat(res, 1)["need"] == pytest.approx(200)     # çift satır toplandı


def test_recipe_total_matches_size_with_scale_no_caution():
    r = rec(117, "SPF 30 10 ml", [(1, 10)], target=None)
    res = compute(inputs([OIL], [r]), req([{"recipe_id": 117, "qty": 1, "scale": 10,
                                            "display_name": "Güneş Kremi 100 ml"}]))
    p = res["products"][0]
    assert "recipe_total_vs_size" not in codes(p) and p["estimated"] is True
    assert "estimated" in codes(p)
    assert next(c for c in mat(res, 1)["cautions"] if c["code"] == "estimated")["text"] == "Tahmini."


def test_extra_packaging_existing_and_new_item():
    r = rec(118, "Maske 200 ml", [(1, 10), (3, 1)], target=None)
    res = compute(inputs([OIL, JAR], [r]), req([{
        "recipe_id": 118, "qty": 10, "use_recipe_packaging": False,
        "extra_packaging": [{"item_id": 3, "per_unit": 2},
                            {"new_name": "250 ML PET KAVANOZ", "pkg_type": "kavanoz", "note": "Kart yok."}]}]))
    jar = mat(res, 3)
    assert jar["need"] == pytest.approx(20) and jar["per_product"][0]["extra"] is True
    new = next(m for m in res["materials"] if m["is_new_item"])
    assert new["key"] == "new:250mlpetkavanoz" and new["stock"] == 0 and new["need"] == pytest.approx(10)
    assert codes(new) == ["new_item"] and new["cautions"][0]["text"] == "Kart yok."
    p = res["products"][0]
    assert p["has_packaging"] is True and "no_packaging_in_recipe" not in codes(p)
    assert p["capacity"]["producible"] == 0
    assert p["capacity"]["limiting"][0]["name"] == "250 ML PET KAVANOZ"


def test_use_recipe_packaging_off_without_extras_warns():
    res = compute(base_inputs(default_excluded=(2,)), req([{"recipe_id": 100, "qty": 1,
                                                           "use_recipe_packaging": False}]))
    p = res["products"][0]
    assert all(m["kind"] != "packaging" for m in res["materials"])
    assert mat(res, 4)["kind"] == "label"                       # etiket ürünündür, kalır
    assert "no_packaging_in_recipe" in codes(p)


# ─── Hariç / bekletilen ─────────────────────────────────────────────────────

def test_excluded_default_and_held():
    inp = base_inputs(default_excluded=(2,))
    res = compute(inp, req([{"recipe_id": 100, "qty": 100}],
                           held_items=[{"item_id": 1, "reason": "Lab teyidi bekleniyor"}]))
    assert all(2 not in m["member_ids"] for m in res["materials"])
    ex = res["excluded"][0]
    assert ex["item_id"] == 2 and ex["need"] == pytest.approx(4000)
    assert ex["need_text"] == "4 litre" and ex["reason"] == "kendi üretimimiz"
    assert all(1 not in m["member_ids"] for m in res["materials"])
    h = mat(res, 1, "held")
    assert h["reason"] == "Lab teyidi bekleniyor" and "held" in codes(h)
    assert h["display"]["buy_text"] == "600 gram"
    assert res["meta"]["counts"]["held"] == 1 and res["meta"]["counts"]["excluded"] == 1


def test_excluded_explicit_list_overrides_default():
    res = compute(base_inputs(default_excluded=(2,)), req([{"recipe_id": 100, "qty": 1}], excluded_item_ids=[3]))
    assert mat(res, 2)["need"] == pytest.approx(40)
    assert res["excluded"][0]["item_id"] == 3 and res["excluded"][0]["reason"] == "hariç tutuldu"


# ─── Kapasite, döküm, bitmiş stok ───────────────────────────────────────────

def test_capacity_floor_ties_and_negative_stock():
    a = it(80, "A", unit="g", stock=95)
    b = it(81, "B", unit="g", stock=190)
    c = it(82, "C", unit="g", stock=-5)
    r1 = rec(120, "P1", [(80, 10), (81, 20)])
    r2 = rec(121, "P2", [(80, 1), (82, 1)])
    res = compute(inputs([a, b, c], [r1, r2]), req([{"recipe_id": 120, "qty": 1}, {"recipe_id": 121, "qty": 1}]))
    p1, p2 = res["products"]
    assert p1["capacity"]["producible"] == 9
    assert [l["name"] for l in p1["capacity"]["limiting"]] == ["A", "B"]       # eşit kısıt, ikisi de
    assert p2["capacity"]["producible"] == 0 and p2["capacity"]["limiting"][0]["name"] == "C"


@pytest.mark.parametrize("mode", ["net", "gross"])
def test_capacity_ignores_excluded_water_but_counts_held(mode):
    # Kendi ürettiğimiz su kartı 0 stokta durur; kısıt sayılırsa her ürün "0 üretilebilir, sebep su" olurdu.
    water0 = it(2, "DİSTİLE SU", unit="ml", stock=0)
    oil = it(1, "Shea Butter", unit="g", stock=300)
    inp = inputs([oil, water0, JAR, LBL_TR, LBL_EN, CREAM], [R_CREAM], label_siblings=SIBS,
                 default_excluded=(2,))
    res = compute(inp, req([{"recipe_id": 100, "qty": 1000}], stock_mode=mode, label_mode="exclude",
                           held_items=[{"item_id": 1, "reason": "Lab teyidi"}]))
    cap = res["products"][0]["capacity"]
    assert res["excluded"][0]["item_id"] == 2
    assert cap["producible"] == 30                                   # bekletilen yağ: 300 g / 10 g
    assert [l["name"] for l in cap["limiting"]] == ["Shea Butter"]


def test_per_product_breakdown_sums_to_need():
    r2 = rec(122, "Krem 2", [(1, 7)], target=None)
    inp = inputs([OIL, WATER, JAR, LBL_TR, LBL_EN, CREAM], [R_CREAM, r2], label_siblings=SIBS,
                 default_excluded=(2,))
    res = compute(inp, req([{"recipe_id": 100, "qty": 30}, {"recipe_id": 122, "qty": 11}],
                           extra_waste_pct={"raw": 10}))
    m = mat(res, 1)
    assert [p["no"] for p in m["per_product"]] == [1, 2]
    assert sum(p["need"] for p in m["per_product"]) == pytest.approx(m["need_production"])
    assert m["need"] == pytest.approx(m["need_production"] * 1.1)


def test_subtract_finished_stock():
    inp = base_inputs(default_excluded=(2,))
    res = compute(inp, req([{"recipe_id": 100, "qty": 100}], subtract_finished_stock=True))
    p = res["products"][0]
    assert p["finished_stock"] == 30 and p["produce_qty"] == 70
    assert mat(res, 1)["need"] == pytest.approx(700)
    assert "üretilecek 70 adet" in res["meta"]["scope_text"]
    neg = inputs([OIL, it(10, "Minerva 108 Krem 50 ml", cat="Bitmiş Ürün", unit="adet", stock=-4)],
                 [rec(100, "Krem", [(1, 10)], target=10)])
    p = compute(neg, req([{"recipe_id": 100, "qty": 10}], subtract_finished_stock=True))["products"][0]
    assert p["produce_qty"] == 10


def test_target_only_line_uses_target_recipe_and_estimated_when_borrowed():
    other = it(12, "Minerva 108 Krem 100 ml", cat="Bitmiş Ürün", unit="adet")
    inp = base_inputs(default_excluded=(2,))
    inp.items[12] = other
    res = compute(inp, req([{"target_item_id": 10, "qty": 1},
                            {"recipe_id": 100, "target_item_id": 12, "qty": 1}]))
    p1, p2 = res["products"]
    assert p1["recipe_id"] == 100 and p1["estimated"] is False and p1["brand"] == "Minerva 108"
    assert p2["recipe_id"] == 100 and p2["estimated"] is True and p2["name"] == "Minerva 108 Krem 100 ml"


# ─── Kapsam metni + meta ────────────────────────────────────────────────────

def test_scope_text_and_meta():
    res = compute(base_inputs(default_excluded=(2,)), req([{"recipe_id": 100, "qty": 1234}], label_mode="new",
                                                          new_label_title="Rusça etiket"))
    s = res["meta"]["scope_text"]
    assert s.startswith("1 ürün (Minerva 108), toplam 1.234 adet.")
    assert "“Alınacak” = toplam gereken − elimizde" in s
    assert "Etiket: yeni basılacak (Rusça etiket)" in s
    assert "Üretim firesi reçeteden; ambalaj/etiket firesiz" in s
    assert "Stoklar 05.10.2026 12:30 sistem kaydı." in s          # UTC 09:30 → TR 12:30
    g = compute(base_inputs(), req([{"recipe_id": 100, "qty": 1}], stock_mode="gross", label_mode="EN"))
    assert "stoktan bağımsız" in g["meta"]["scope_text"]
    assert "Etiket: İngilizce etiket kartları" in g["meta"]["scope_text"]
    c = res["meta"]["counts"]
    assert c["products"] == 1 and c["units"] == 1234 and c["materials"] == len(res["materials"])
    assert res["meta"]["options"]["label_mode"] == "new"


# ─── İstek modeli doğrulaması ───────────────────────────────────────────────

def test_request_validation():
    with pytest.raises(ValidationError):
        PlanRequest(lines=[{"qty": 5}])                      # reçete/ürün yok
    with pytest.raises(ValidationError):
        PlanRequest(lines=[])
    with pytest.raises(ValidationError):
        PlanRequest(lines=[{"recipe_id": 1, "qty": 0}])
    with pytest.raises(ValidationError):
        PlanRequest(lines=[{"recipe_id": 1, "qty": 1, "extra_packaging": [{"per_unit": 1}]}])
    with pytest.raises(ValidationError):
        PlanRequest(lines=[{"recipe_id": 1, "qty": 1}], options={"extra_waste_pct": {"raw": 150}})
    r = PlanRequest(lines=[{"recipe_id": 1, "qty": 1}], options={"item_notes": {"5": "not"}})
    assert r.options.item_notes == {5: "not"} and r.options.excluded_item_ids is None


# ─── DB yükleyici: domain kapsamı ───────────────────────────────────────────

def _seed(db):
    from database import (AppSetting, DuplicateItemDecision, Inventory, Item, Recipe,
                          RecipeIngredient, StockOrderFlag, Supplier, Transaction)
    sup = Supplier(name="Befchem", domain="cosmetics")
    db.add(sup)
    oil = Item(name="Shea Butter", category="Hammadde", unit="g", current_stock=400, domain="cosmetics")
    oil2 = Item(name="SHEA BUTTER", category="Hammadde", unit="g", current_stock=50, domain="cosmetics")
    water = Item(name="Distile Su", category="Hammadde", unit="ml", current_stock=1000, domain="cosmetics")
    cream = Item(name="Minerva 108 Krem 50 ml", category="Bitmiş Ürün", unit="adet", domain="cosmetics")
    sup_item = Item(name="Kapsül", category="Hammadde", unit="adet", domain="supplement")
    db.add_all([oil, oil2, water, cream, sup_item])
    db.flush()
    old = Recipe(name="Eski krem", target_item_id=cream.id, domain="cosmetics",
                 created_at=datetime.utcnow() - timedelta(days=30))
    new = Recipe(name="Yeni krem", target_item_id=cream.id, domain="cosmetics")
    passive = Recipe(name="Pasif", domain="cosmetics", is_active=False)
    sup_r = Recipe(name="Takviye", domain="supplement")
    db.add_all([old, new, passive, sup_r])
    db.flush()
    db.add_all([RecipeIngredient(recipe_id=new.id, item_id=oil.id, quantity=10, unit="g"),
                RecipeIngredient(recipe_id=new.id, item_id=water.id, quantity=40, unit="ml"),
                RecipeIngredient(recipe_id=old.id, item_id=oil.id, quantity=99, unit="g")])
    db.add(StockOrderFlag(item_id=oil.id, supplier_id=sup.id, quantity=100, unit="g", domain="cosmetics"))
    db.add(StockOrderFlag(item_id=water.id, quantity=5, domain="cosmetics",
                          closed_at=datetime.utcnow(), closed_reason="received"))
    db.add(DuplicateItemDecision(cluster_key="k1", title="Shea", item_ids=f"[{oil.id}, {oil2.id}]",
                                 status="kept"))
    db.add(DuplicateItemDecision(cluster_key="k2", title="Karışık", item_ids=f"[{oil.id}, {sup_item.id}]",
                                 status="pending"))
    db.add(Inventory(item_id=oil.id, supplier_id=sup.id, lot_number="L1", quantity=5, is_sample=True,
                     domain="cosmetics"))
    db.add(Inventory(item_id=oil.id, supplier_id=sup.id, lot_number="L2", quantity=50, domain="cosmetics"))
    db.add(Transaction(item_id=oil.id, lot_number="L2", transaction_type="Input", quantity=50,
                       notes="Numune stoğa çevrildi — Lot: L2"))
    db.commit()
    return {"oil": oil.id, "oil2": oil2.id, "water": water.id, "cream": cream.id, "sup_item": sup_item.id,
            "old": old.id, "new": new.id, "passive": passive.id, "sup_r": sup_r.id, "AppSetting": AppSetting}


def test_load_inputs_domain_scoped(db_session):
    ids = _seed(db_session)
    r = PlanRequest(lines=[{"target_item_id": ids["cream"], "qty": 10}])
    inp = load_inputs(db_session, r, "cosmetics")
    assert ids["sup_item"] not in inp.items                     # panel dışı kalem yüklenmez
    assert inp.recipe_by_target[ids["cream"]] == ids["new"]     # en yeni aktif reçete
    assert ids["passive"] not in inp.recipes and ids["sup_r"] not in inp.recipes
    assert [o.quantity for o in inp.open_orders[ids["oil"]]] == [100]
    assert inp.open_orders[ids["oil"]][0].supplier_name == "Befchem"
    assert ids["water"] not in inp.open_orders                  # kapalı bayrak yok
    assert [d.status for d in inp.decisions] == ["kept"]        # panel dışı üyeli küme 2'den aza düştü
    assert {l.lot_number for l in inp.lots[ids["oil"]]} == {"L1", "L2"}
    assert (ids["oil"], "L2") in inp.converted_lots
    assert inp.default_excluded == (ids["water"],)              # AppSetting yok → ada göre
    res = compute(inp, r)
    m = mat(res, ids["oil"])
    assert m["member_ids"] == [ids["oil"]]                      # 'kept' kararı birleştirmeyi engelledi
    assert m["need"] == pytest.approx(100)


def test_load_inputs_rejects_foreign_and_passive_ids(db_session):
    ids = _seed(db_session)
    with pytest.raises(PlanInputError, match="Bu panelde değil"):
        load_inputs(db_session, PlanRequest(lines=[{"recipe_id": ids["sup_r"], "qty": 1}]), "cosmetics")
    with pytest.raises(PlanInputError, match="Bu panelde değil"):
        load_inputs(db_session, PlanRequest(lines=[{"target_item_id": ids["sup_item"], "qty": 1}]), "cosmetics")
    with pytest.raises(PlanInputError, match="Bu panelde değil"):
        load_inputs(db_session, PlanRequest(lines=[{"recipe_id": ids["new"], "qty": 1}],
                                            options={"held_items": [{"item_id": ids["sup_item"]}]}), "cosmetics")
    with pytest.raises(PlanInputError, match="pasif"):
        load_inputs(db_session, PlanRequest(lines=[{"recipe_id": ids["passive"], "qty": 1}]), "cosmetics")
    with pytest.raises(PlanInputError, match="Bu panelde değil"):
        load_inputs(db_session, PlanRequest(lines=[{"recipe_id": ids["new"], "qty": 1}]), "supplement")


def test_resolve_default_excluded_per_panel_and_global_fallback():
    items = {2: WATER, 1: OIL,
             200: it(200, "SAF SU", unit="ml", domain="supplement"),
             201: it(201, "Kapsül", unit="adet", domain="supplement")}
    # anahtar yok → ad kuralı (panel başına)
    assert resolve_default_excluded(None, None, items, "cosmetics") == [2]
    assert resolve_default_excluded(None, None, items, "supplement") == [200]
    # genel anahtar yalnız kozmetik id'leri içeriyor → supplement suyunu KAYBETMEZ
    assert resolve_default_excluded(None, "[2, 1]", items, "cosmetics") == [2, 1]
    assert resolve_default_excluded(None, "[2, 1]", items, "supplement") == [200]
    # genel anahtar bilinçli boş → hariç yok
    assert resolve_default_excluded(None, "[]", items, "supplement") == []
    # panel anahtarı önceliklidir; boş liste de açık karardır
    assert resolve_default_excluded("[201]", "[2]", items, "supplement") == [201]
    assert resolve_default_excluded("[]", "[2]", items, "cosmetics") == []
    # panel dışı id panel anahtarında da süzülür; bozuk JSON yok sayılır
    assert resolve_default_excluded("[2, 200]", None, items, "supplement") == [200]
    assert resolve_default_excluded("bozuk", "{x", items, "cosmetics") == [2]
    assert default_excluded_key("supplement") == "purchase_plan.default_excluded.supplement"


def test_load_inputs_default_excluded_from_appsetting(db_session):
    ids = _seed(db_session)
    db_session.add(ids["AppSetting"](key="purchase_plan.default_excluded", value=f"[{ids['oil']}]"))
    db_session.commit()
    inp = load_inputs(db_session, PlanRequest(lines=[{"recipe_id": ids["new"], "qty": 1}]), "cosmetics")
    assert inp.default_excluded == (ids["oil"],)
    # panel anahtarı genel anahtarı ezer
    db_session.add(ids["AppSetting"](key=default_excluded_key("cosmetics"), value=f"[{ids['water']}]"))
    db_session.commit()
    inp = load_inputs(db_session, PlanRequest(lines=[{"recipe_id": ids["new"], "qty": 1}]), "cosmetics")
    assert inp.default_excluded == (ids["water"],)


# ─── "Aynı malzeme" grupları ────────────────────────────────────────────────

# Prod'daki stearil alkol kartları: lab her tedarikçiyi ayrı kart tutuyor
STEARYL_TD = it(126, "SETİL STEARİL ALKOL", unit="g", stock=3944)       # TATLIDİLİMLER
STEARYL_YK = it(599, "CETYL STEARYL ALCOHOL", unit="adet", stock=0)     # YİĞİTOĞLU KİMYA
STEARYL_VS = it(593, "CETEARYL ALCOHOL", unit="adet", stock=0)          # VESER KİMYEVİ
STEARYL_GROUP = {126: 7, 599: 7, 593: 7}


def test_group_same_name_cards_not_merged_need_never_drops():
    grp = {30: 5, 31: 5}
    plain = compute(inputs([GLY_A, GLY_B], [R_GLY]), req([{"recipe_id": 110, "qty": 100}]))
    res = compute(inputs([GLY_A, GLY_B], [R_GLY], mgroup_of=grp, mgroup_names={5: "Gliserin"}),
                  req([{"recipe_id": 110, "qty": 100}]))
    a, m = mat(plain, 30), mat(res, 30)
    assert a["member_ids"] == [30, 31]                      # grupsuz: aynı ad + birim birleşir
    assert m["member_ids"] == [30] and m["stock"] == pytest.approx(100)   # grupta: birleşmez
    assert m["need"] == pytest.approx(a["need"])            # ihtiyaç aynı
    assert m["buy"] >= a["buy"]                             # gruptaki stok düşülmez → alım AZALMAZ
    assert "duplicate_merged" not in codes(m) and "lookalike" not in codes(m)
    assert m["material_group"] == {"id": 5, "name": "Gliserin", "alts": [
        {"item_id": 31, "name": "GLISERIN", "unit": "g", "unit_mismatch": False, "stock": 5000.0,
         "in_plan": False}]}
    c = next(c for c in m["cautions"] if c["code"] == "group_alt_stock")
    assert c["severity"] == "info"
    assert c["text"] == ("Aynı malzeme grubundaki «GLISERIN» kartında stok var (5,0 kg); "
                         "lab ayrı ürün saydığı için ihtiyaçtan düşülmedi.")
    # manual_merges açık kullanıcı kararıdır — grup onu engellemez
    mm = compute(inputs([GLY_A, GLY_B], [R_GLY], mgroup_of=grp),
                 req([{"recipe_id": 110, "qty": 100}], manual_merges=[[30, 31]]))
    assert mat(mm, 30)["member_ids"] == [30, 31] and mat(mm, 30)["material_group"]["alts"] == []


def test_group_suppresses_lookalike_and_flags_unit_mismatch():
    used = it(50, "LAURYL GLUCOSİDE", unit="g", stock=0)
    other = it(51, "LAURYL GLUCOSIDE", unit="adet", stock=12)
    r = rec(112, "Şampuan", [(50, 3)])
    res = compute(inputs([used, other], [r]), req([{"recipe_id": 112, "qty": 100}]))
    assert "lookalike" in codes(mat(res, 50))               # grupsuz: farklı birim → benzer kart uyarısı
    assert mat(res, 50)["material_group"] is None
    res = compute(inputs([used, other], [r], mgroup_of={50: 1, 51: 1}, mgroup_names={1: "Lauryl"}),
                  req([{"recipe_id": 112, "qty": 100}]))
    m = mat(res, 50)
    assert "lookalike" not in codes(m)
    assert m["material_group"]["alts"][0]["unit_mismatch"] is True
    c = next(c for c in m["cautions"] if c["code"] == "group_alt_stock")
    assert "«LAURYL GLUCOSIDE» kartında stok var (12 adet; birimi farklı)" in c["text"]
    assert m["need"] == pytest.approx(300) and m["buy"] == pytest.approx(300)


def test_stearyl_group_alternatives_and_need_unchanged():
    r = rec(120, "Krem", [(126, 40)])
    base = compute(inputs([STEARYL_TD, STEARYL_YK, STEARYL_VS], [r]), req([{"recipe_id": 120, "qty": 1000}]))
    inp = inputs([STEARYL_TD, STEARYL_YK, STEARYL_VS], [r], mgroup_of=STEARYL_GROUP,
                 mgroup_names={7: "Setil stearil alkol"})
    m = mat(compute(inp, req([{"recipe_id": 120, "qty": 1000}])), 126)
    assert m["need"] == mat(base, 126)["need"] and m["buy"] == mat(base, 126)["buy"]
    assert m["display"]["buy_text"] == mat(base, 126)["display"]["buy_text"]
    mg = m["material_group"]
    assert (mg["id"], mg["name"]) == (7, "Setil stearil alkol")
    assert [(a["item_id"], a["unit_mismatch"], a["stock"]) for a in mg["alts"]] == [(593, True, 0.0),
                                                                                    (599, True, 0.0)]
    assert "group_alt_stock" not in codes(m)                # alternatiflerde stok yok
    # 'adet' kartı kullanılırsa 126'nın stoğu bilgi olarak çıkar, ihtiyaç düşmez
    r2 = rec(121, "Krem B", [(599, 2)])
    m2 = mat(compute(inputs([STEARYL_TD, STEARYL_YK, STEARYL_VS], [r2], mgroup_of=STEARYL_GROUP,
                            mgroup_names={7: "Setil stearil alkol"}),
                     req([{"recipe_id": 121, "qty": 10}])), 599)
    assert m2["need"] == pytest.approx(20) and m2["buy"] == pytest.approx(20)
    c = next(c for c in m2["cautions"] if c["code"] == "group_alt_stock")
    assert "«SETİL STEARİL ALKOL» kartında stok var (3,9 kg; birimi farklı)" in c["text"]


def test_group_members_both_in_plan_marked_in_plan_and_no_stock_caution():
    r = rec(122, "İkili", [(126, 1), (599, 1)])
    yk = it(599, "CETYL STEARYL ALCOHOL", unit="adet", stock=5)
    res = compute(inputs([STEARYL_TD, yk], [r], mgroup_of={126: 7, 599: 7}),
                  req([{"recipe_id": 122, "qty": 10}]))
    for iid, other in ((126, 599), (599, 126)):
        m = mat(res, iid)
        assert [(a["item_id"], a["in_plan"]) for a in m["material_group"]["alts"]] == [(other, True)]
        assert "group_alt_stock" not in codes(m)             # o stok kendi satırında kullanılıyor


def test_group_alts_skip_inactive_and_foreign_domain_members():
    gone = it(127, "STEARIL ALKOL ESKİ", unit="g", stock=999, active=False)
    foreign = it(128, "STEARYL ALCOHOL", unit="g", stock=50, domain="supplement")
    r = rec(123, "Krem", [(126, 1)])
    inp = inputs([STEARYL_TD, STEARYL_VS, gone, foreign], [r],
                 mgroup_of={126: 7, 593: 7, 127: 7, 128: 7})
    assert material_group_members(inp) == {7: [126, 127, 593]}      # panel dışı kart yok
    m = mat(compute(inp, req([{"recipe_id": 123, "qty": 1}])), 126)
    assert [a["item_id"] for a in m["material_group"]["alts"]] == [593]
    assert m["material_group"]["name"] == "Grup #7"                 # ad yoksa yer tutucu


def test_load_inputs_material_groups_active_and_domain_scoped(db_session):
    from database import Item, MaterialGroup
    ids = _seed(db_session)
    g = MaterialGroup(name="Shea", domain="cosmetics")
    dead = MaterialGroup(name="Eski", domain="cosmetics", is_active=False)
    sup = MaterialGroup(name="Takviye", domain="supplement")
    db_session.add_all([g, dead, sup])
    db_session.flush()
    oil, oil2, water, sup_item = (db_session.get(Item, ids[k]) for k in ("oil", "oil2", "water", "sup_item"))
    oil.material_group_id = oil2.material_group_id = g.id
    water.material_group_id = dead.id
    sup_item.material_group_id = sup.id
    db_session.commit()
    inp = load_inputs(db_session, PlanRequest(lines=[{"recipe_id": ids["new"], "qty": 1}]), "cosmetics")
    assert inp.mgroup_of == {ids["oil"]: g.id, ids["oil2"]: g.id}     # pasif grup + öbür panel yok
    assert inp.mgroup_names == {g.id: "Shea"}
    m = mat(compute(inp, PlanRequest(lines=[{"recipe_id": ids["new"], "qty": 1}])), ids["oil"])
    assert [a["item_id"] for a in m["material_group"]["alts"]] == [ids["oil2"]]


# ─── "Bitirilecek" tedarikçi kartlarının stoğu (count_phase_out_stock) ──────

# Jojoba: KRK kartı (normal), Hammadde Sepeti kartı (bitirilecek — 1 kg
# satıyor), Naturalya kartı (normal), eski kg kartı (bitirilecek ama birimi
# farklı → sayılmaz).
JJ_KRK = it(201, "JOJOBA YAĞI", unit="g", stock=100)
JJ_SEPET = it(202, "JOJOBA YAĞI — HAMMADDE SEPETİ", unit="g", stock=500)
JJ_NAT = it(203, "JOJOBA OIL", unit="g", stock=0)
JJ_KG = it(204, "JOJOBA ESKİ", unit="kg", stock=3)
JJ = [JJ_KRK, JJ_SEPET, JJ_NAT, JJ_KG]
JJ_PO = dict(mgroup_of={201: 9, 202: 9, 203: 9, 204: 9}, mgroup_names={9: "Jojoba"},
             card_supplier={201: 1, 202: 2, 203: 3, 204: 2},
             supplier_status={1: "normal", 2: "phase_out", 3: "normal"},
             supplier_names={1: "KRK GIDA", 2: "HAMMADDE SEPETİ", 3: "NATURALYA"})
R_JJ_KRK = rec(301, "Krem", [(201, 4)])           # 100 adet → 400 g
R_JJ_NAT = rec(302, "Serum", [(203, 4)])          # 100 adet → 400 g


def test_phase_out_alt_stock_reduces_buy_with_caution():
    res = compute(inputs(JJ, [R_JJ_KRK], **JJ_PO), req([{"recipe_id": 301, "qty": 100}]))
    m = mat(res, 201)
    assert m["need"] == pytest.approx(400)
    assert m["stock_phase_out"] == pytest.approx(300) and m["stock"] == pytest.approx(400)   # 100 + 300
    assert m["buy"] == pytest.approx(0) and m["status"] == "yeterli"
    assert m["phase_out_from"] == [{"item_id": 202, "name": "JOJOBA YAĞI — HAMMADDE SEPETİ", "supplier_id": 2,
                                    "supplier": "HAMMADDE SEPETİ", "reason": "phase_out", "qty": 300.0,
                                    "qty_text": "300 gram"}]
    c = next(c for c in m["cautions"] if c["code"] == "phase_out_stock")
    assert c["severity"] == "warn"
    assert c["text"] == ("«JOJOBA YAĞI — HAMMADDE SEPETİ» (HAMMADDE SEPETİ, bitirilecek tedarikçi) kartındaki "
                         "300 gram önce kullanılacak — ihtiyaçtan düşüldü.")
    g = next(c for c in m["cautions"] if c["code"] == "group_alt_stock")
    assert "HAMMADDE SEPETİ»" not in g["text"] and "«JOJOBA ESKİ» kartında stok var (3,0 kg; birimi farklı)" in g["text"]


def test_phase_out_label_stock_does_not_cover_packaging_need():
    bottle = it(221, "50 ML ŞİŞE", cat="Ambalaj", unit="adet", stock=2, pkg="şişe")
    label = it(222, "50 ML ŞİŞE ÖN ETİKET", cat="Ambalaj", unit="adet", stock=50, pkg="etiket")
    po = dict(mgroup_of={221: 7, 222: 7}, mgroup_names={7: "Şişe"}, card_supplier={222: 2},
              supplier_status={2: "phase_out"}, supplier_names={2: "Eski tedarikçi"})
    m = mat(compute(inputs([bottle, label], [rec(312, "Serum", [(221, 1)])], **po),
                    req([{"recipe_id": 312, "qty": 10}])), 221)
    assert m["kind"] == "packaging" and m["need"] == 10
    assert m["stock"] == 2 and m["stock_phase_out"] == 0
    assert m["buy"] == 8 and m["status"] == "to_buy"
    assert m["phase_out_from"] == [] and "phase_out_stock" not in codes(m)


def test_phase_out_pool_shared_between_rows_counted_once():
    res = compute(inputs(JJ, [R_JJ_KRK, R_JJ_NAT], **JJ_PO),
                  req([{"recipe_id": 301, "qty": 100}, {"recipe_id": 302, "qty": 100}]))
    krk, nat = mat(res, 201), mat(res, 203)
    assert krk["stock_phase_out"] == pytest.approx(300) and krk["buy"] == pytest.approx(0)
    assert nat["stock_phase_out"] == pytest.approx(200)                 # 500 − 300: kalan havuz
    assert nat["buy"] == pytest.approx(200) and nat["display"]["buy_text"] == "200 gram"
    assert krk["stock_phase_out"] + nat["stock_phase_out"] == pytest.approx(JJ_SEPET.current_stock)
    # planda kendi satırı olan kart (KRK ↔ NATURALYA) bitirilecek havuza girmez
    assert [a["item_id"] for a in nat["phase_out_from"]] == [202]


def test_phase_out_option_off_or_gross_or_alt_in_plan_keeps_old_behaviour():
    base = mat(compute(inputs(JJ, [R_JJ_KRK]), req([{"recipe_id": 301, "qty": 100}])), 201)
    off = mat(compute(inputs(JJ, [R_JJ_KRK], **JJ_PO),
                      req([{"recipe_id": 301, "qty": 100}], count_phase_out_stock=False)), 201)
    assert off["buy"] == base["buy"] == pytest.approx(300) and off["stock_phase_out"] == 0
    assert "phase_out_stock" not in codes(off)
    assert "«JOJOBA YAĞI — HAMMADDE SEPETİ» (500 gram)" in next(
        c for c in off["cautions"] if c["code"] == "group_alt_stock")["text"]
    gross = mat(compute(inputs(JJ, [R_JJ_KRK], **JJ_PO),
                        req([{"recipe_id": 301, "qty": 100}], stock_mode="gross")), 201)
    assert gross["stock_phase_out"] == 0 and gross["buy"] == pytest.approx(400)
    both = compute(inputs(JJ, [rec(303, "İkili", [(201, 4), (202, 1)])], **JJ_PO),
                   req([{"recipe_id": 303, "qty": 100}]))
    assert mat(both, 201)["stock_phase_out"] == 0 and mat(both, 201)["buy"] == pytest.approx(300)
    assert mat(both, 202)["buy"] == pytest.approx(0)                   # kendi satırında kullanılıyor


def test_avoid_pref_card_counts_like_phase_out():
    po = dict(JJ_PO, supplier_status={1: "normal", 2: "normal", 3: "normal"}, avoid={201: {2}})
    m = mat(compute(inputs(JJ, [R_JJ_KRK], **po), req([{"recipe_id": 301, "qty": 100}])), 201)
    assert m["stock_phase_out"] == pytest.approx(300)
    assert "(HAMMADDE SEPETİ, bu malzemede alınmayacak tedarikçi)" in next(
        c for c in m["cautions"] if c["code"] == "phase_out_stock")["text"]
    # "alma" kararı yalnız o satırın kartları için: NATURALYA satırında 202 normal
    n = mat(compute(inputs(JJ, [R_JJ_NAT], **po), req([{"recipe_id": 302, "qty": 100}])), 203)
    assert n["stock_phase_out"] == 0 and n["buy"] == pytest.approx(400)


def test_load_inputs_supplier_status_by_firm_and_avoid_expanded(db_session):
    from database import Item, MaterialGroup, MaterialSupplierPref, Supplier
    ids = _seed(db_session)
    a = Supplier(name="TATLIDİLİMLER", domain="cosmetics")
    a2 = Supplier(name="TATLİDİLİMLER", domain="cosmetics", purchase_status="phase_out", status_reason="1 kg")
    nat = Supplier(name="NATURALYA", domain="cosmetics")
    db_session.add_all([a, a2, nat])
    g = MaterialGroup(name="Shea", domain="cosmetics")
    db_session.add(g)
    db_session.flush()
    oil, oil2 = db_session.get(Item, ids["oil"]), db_session.get(Item, ids["oil2"])
    oil.material_group_id = oil2.material_group_id = g.id
    oil.supplier_id, oil2.supplier_id = nat.id, a.id
    db_session.add_all([
        MaterialSupplierPref(domain="cosmetics", material_group_id=g.id, supplier_id=nat.id, preference="avoid"),
        # kartın kendi kararı grubu ezer: oil2'de NATURALYA "alma" değil
        MaterialSupplierPref(domain="cosmetics", item_id=oil2.id, supplier_id=nat.id, preference="preferred")])
    db_session.commit()
    inp = load_inputs(db_session, PlanRequest(lines=[{"recipe_id": ids["new"], "qty": 1}]), "cosmetics")
    assert inp.card_supplier[ids["oil"]] == nat.id and inp.card_supplier[ids["oil2"]] == a.id
    assert inp.supplier_status[a.id] == inp.supplier_status[a2.id] == "phase_out"   # aynı firma anahtarı
    assert inp.supplier_status[nat.id] == "normal"
    assert inp.avoid == {ids["oil"]: {nat.id}}
    m = mat(compute(inp, PlanRequest(lines=[{"recipe_id": ids["new"], "qty": 50}])), ids["oil"])
    assert m["need"] == pytest.approx(500) and m["stock_phase_out"] == pytest.approx(50)   # oil2 bitirilecek
    assert m["buy"] == pytest.approx(50)                                # 500 − 400 − 50


def test_held_row_does_not_take_from_phase_out_pool():
    res = compute(inputs(JJ, [R_JJ_KRK, R_JJ_NAT], **JJ_PO),
                  req([{"recipe_id": 301, "qty": 100}, {"recipe_id": 302, "qty": 100}],
                      held_items=[{"item_id": 201, "reason": "teyit"}]))
    held, nat = mat(res, 201, "held"), mat(res, 203)
    assert held["stock_phase_out"] == 0                                 # bekletilen pay almaz
    assert nat["stock_phase_out"] == pytest.approx(400) and nat["buy"] == pytest.approx(0)


def test_phase_out_pool_covering_need_leaves_nothing_to_buy_after_safe_rounding():
    """Havuz eksiği tam karşılıyorsa güvenli yuvarlamalı gösterimde de
    alınacak 0 olmalı (gereken yukarı / elimizde aşağı yuvarlanınca tam eksik
    kadar alım "yeterli" satırda "1 gram" bırakıyordu)."""
    gli = it(211, "GLİ", unit="g", stock=0)
    gli_po = it(212, "GLİ PO", unit="g", stock=5000)
    po = dict(mgroup_of={211: 8, 212: 8}, mgroup_names={8: "Gliserin"}, card_supplier={212: 2},
              supplier_status={2: "phase_out"}, supplier_names={2: "HAMMADDE SEPETİ"})
    m = mat(compute(inputs([gli, gli_po], [rec(311, "Krem", [(211, 1.2345)])], **po),
                    req([{"recipe_id": 311, "qty": 1000}])), 211)
    assert m["need"] == pytest.approx(1234.5) and m["status"] == "yeterli"
    assert m["stock_phase_out"] == pytest.approx(1235)                 # tam birime yukarı
    assert (m["display"]["buy_text"], m["display"]["buy_num"]) == ("0 gram", 0)
    assert m["display"]["need_text"] == m["display"]["stock_text"] == "1.235 gram"
    # adet: 10,5 → 11 adet alınır havuzdan, alınacak 0
    cap = it(221, "KAPAK", cat="Ambalaj", unit="adet", stock=0)
    cap_po = it(222, "KAPAK PO", cat="Ambalaj", unit="adet", stock=50)
    po = dict(po, mgroup_of={221: 7, 222: 7}, mgroup_names={7: "Kapak"}, card_supplier={222: 2})
    m = mat(compute(inputs([cap, cap_po], [rec(312, "Krem", [(221, 0.0105)])], **po),
                    req([{"recipe_id": 312, "qty": 1000}])), 221)
    assert m["status"] == "yeterli" and m["stock_phase_out"] == pytest.approx(11)
    assert m["display"]["buy_text"] == "0 adet"
    # kg kartı: 0,001 kg'a (1 g) yukarı
    oil = it(231, "YAĞ", unit="kg", stock=0.3)
    oil_po = it(232, "YAĞ PO", unit="kg", stock=4)
    po = dict(po, mgroup_of={231: 6, 232: 6}, mgroup_names={6: "Yağ"}, card_supplier={232: 2})
    m = mat(compute(inputs([oil, oil_po], [rec(313, "Krem", [(231, 0.0012345)])], **po),
                    req([{"recipe_id": 313, "qty": 1000}])), 231)
    assert m["status"] == "yeterli" and m["stock_phase_out"] == pytest.approx(0.935)
    assert m["display"]["buy_num"] == 0
    # havuz yetmiyorsa hepsi alınır, kalan eksik normal yuvarlanır
    gli_po2 = it(212, "GLİ PO", unit="g", stock=1000)
    m = mat(compute(inputs([gli, gli_po2], [rec(311, "Krem", [(211, 1.2345)])],
                           mgroup_of={211: 8, 212: 8}, mgroup_names={8: "Gliserin"}, card_supplier={212: 2},
                           supplier_status={2: "phase_out"}, supplier_names={2: "HAMMADDE SEPETİ"}),
                    req([{"recipe_id": 311, "qty": 1000}])), 211)
    assert m["stock_phase_out"] == pytest.approx(1000) and m["display"]["buy_text"] == "235 gram"
