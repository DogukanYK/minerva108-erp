"""
Malzeme anahtarı + öneri motoru (core/material_groups.material_key / suggest)
— SAF, DB'siz.

Lab aynı malzemenin her tedarikçisini ayrı kart tutuyor; adlar TR/EN karışık:
126 SETİL STEARİL ALKOL (g, Tatlıdilimler), 599 CETYL STEARYL ALCOHOL (adet,
Yiğitoğlu), 593 CETEARYL ALCOHOL (adet, Veser).  Anahtar üçünü aynı yere
getirmeli; öneri birim uyuşmazlığını işaretlemeli.  E ve C vitamini, PEG-40
ve PEG-60 ASLA aynı/benzer sayılmamalı.
"""
import pytest

from core.material_groups import MatRec, material_key, suggest
from core.purchase_pricing import supplier_key

STEARYL = ("ALCOHOL", "CETYL", "STEARYL")


# ─── material_key ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "SETİL STEARİL ALKOL", "CETYL STEARYL ALCOHOL", "CETEARYL ALCOHOL",
    "Setearil Alkol", "cetostearyl alcohol", "Setil Stearil Alkolü",
    "SETİL STEARİL ALKOL (NUMUNE)", "%100 Saf Cetearyl Alcohol",
])
def test_stearyl_variants_share_one_key(name):
    assert material_key(name) == STEARYL


def test_sample_parentheses_dropped():
    assert material_key("JOJOBA YAĞI (NUMUNE)") == material_key("JOJOBA YAGI") == ("JOJOBA", "OIL")
    assert material_key("Jojoba Oil [Sample]") == ("JOJOBA", "OIL")
    # NUMUNE geçmeyen parantez içeriği korunur
    assert material_key("JOJOBA YAĞI (ALTIN)") == ("ALTIN", "JOJOBA", "OIL")


def test_firm_prefix_and_suffix_dropped():
    assert material_key("PERA GmbH – Golden Jojoba Oil") == ("GOLDEN", "JOJOBA", "OIL")
    assert material_key("Akdeniz Kimya Ltd. Şti. - Gliserin") == ("GLYCERIN",)
    assert material_key("UMAYCHEM — Coco Glucoside") == ("COCO", "GLUCOSIDE")
    # Çevirme penceresinin önerdiği "<AD> — <TEDARİKÇİ>" son eki: tedarikçi
    # anahtarı verilirse atılır, verilmezse (firma işareti yok) kalır
    keys = {supplier_key("NATURALYA DOĞAL ÜRÜNLER")}
    assert material_key("JOJOBA YAĞI — NATURALYA", keys) == ("JOJOBA", "OIL")
    assert material_key("JOJOBA YAĞI — NATURALYA") == ("JOJOBA", "NATURALYA", "OIL")
    # Firma olmayan ön ek atılmaz; ad hiç boşalmaz
    assert material_key("LAVANTA - FRANSIZ") == ("FRANSIZ", "LAVENDER")
    assert material_key("PERA GmbH") == ("GMBH", "PERA")


def test_oil_extract_hydrosol_glycerin_glucoside_synonyms():
    assert material_key("BADEM YAĞI") == material_key("Almond Oil") == ("ALMOND", "OIL")
    assert material_key("PAPATYA EKSTRAKTI") == material_key("Papatya Extract") \
        == material_key("PAPATYA EKSTRESİ") == ("EXTRACT", "PAPATYA")
    assert material_key("GÜL HİDROSOLÜ") == material_key("Gül Hydrosol") \
        == material_key("GÜL HİDROLAT") == ("GUL", "HYDROSOL")
    assert material_key("GLİSERİN") == material_key("Glycerine") == ("GLYCERIN",)
    assert material_key("LAURYL GLUKOZİT") == material_key("Lauryl Glucoside") == ("GLUCOSIDE", "LAURYL")
    assert material_key("PORTAKAL") == material_key("Orange") == ("ORANGE",)


def test_essential_oil_bigram():
    assert material_key("LAVANTA UÇUCU YAĞI") == material_key("Lavender Essential Oil") \
        == ("ESSENTIALOIL", "LAVENDER")
    assert material_key("MELEZ LAVANTA UÇUCU YAĞ") == ("ESSENTIALOIL", "LAVENDER", "MELEZ")
    # Uçucu yağ ≠ sabit yağ
    assert material_key("LAVANTA YAĞI") == ("LAVENDER", "OIL")
    assert material_key("PORTAKAL UÇUCU YAĞI (NUMUNE)") == ("ESSENTIALOIL", "ORANGE")


def test_stop_words_and_percent_dropped_digits_kept():
    assert material_key("Organik %100 Saf Doğal Badem Yağı") == ("ALMOND", "OIL")
    assert material_key("Pure Natural Almond Oil 100%") == ("ALMOND", "OIL")
    assert material_key("PEG-40 HYDROGENATED CASTOR OIL") == ("40", "CASTOR", "HYDROGENATED", "OIL", "PEG")
    assert material_key("PEG-40 HYDROGENATED CASTOR OIL") != material_key("PEG-60 Hydrogenated Castor Oil")
    assert material_key("POLYSORBATE 20") != material_key("POLYSORBATE 80")


def test_vitamins_stay_distinct():
    assert material_key("E VİTAMİNİ") == material_key("Vitamin E") == ("E", "VITAMIN")
    assert material_key("C VİTAMİNİ") == ("C", "VITAMIN")
    assert material_key("E VİTAMİNİ") != material_key("C VİTAMİNİ")


def test_empty_names():
    assert material_key("") == ()
    assert material_key(None) == ()
    assert material_key("(NUMUNE)") == ()


# ─── suggest ────────────────────────────────────────────────────────────────

def _r(i, name, unit="g", **kw):
    return MatRec(id=i, name=name, unit=unit, **kw)


def _by_ids(out):
    return {tuple(s["item_ids"]): s for s in out}


def test_stearyl_trio_same_key_with_unit_mismatch():
    items = [_r(126, "SETİL STEARİL ALKOL", "g", supplier_name="TATLIDİLİMLER", stock=3944),
             _r(599, "CETYL STEARYL ALCOHOL", "adet", supplier_name="YİĞİTOGLU KİMYA"),
             _r(593, "CETEARYL ALCOHOL", "adet", supplier_name="VESER KİMYEVİ"),
             _r(7, "GLİSERİN")]
    out = suggest(items, {})
    s = _by_ids(out)[(126, 593, 599)]
    assert s["kind"] == "same_key" and s["score"] == 1.0 and s["source"] == "name"
    assert s["unit_mismatch"] is True and s["units"] == ["adet", "g"]
    assert s["action"] == "create" and s["group"] is None
    assert s["title"] == "SETİL STEARİL ALKOL" and s["key"] == "ALCOHOL CETYL STEARYL"
    assert {i["supplier_name"] for i in s["items"]} == {"TATLIDİLİMLER", "YİĞİTOGLU KİMYA",
                                                        "VESER KİMYEVİ"}
    assert len(out) == 1


def test_same_group_and_dismissed_excluded():
    items = [_r(1, "SETİL STEARİL ALKOL", material_group_id=50),
             _r(2, "CETYL STEARYL ALCOHOL", material_group_id=50),
             _r(3, "CETEARYL ALCOHOL")]
    out = suggest(items, {50: "Stearil alkol"})
    s = out[0]
    assert s["item_ids"] == [1, 2, 3] and s["action"] == "join"
    assert s["group"] == {"id": 50, "name": "Stearil alkol"} and s["title"] == "Stearil alkol"
    # hepsi gruptaysa öneri yok
    items[2].material_group_id = 50
    assert suggest(items, {50: "Stearil alkol"}) == []
    # pasif gruba bakan bağ yok sayılır
    assert len(suggest(items, {})) == 1
    # "farklı" denen çift(ler)
    items = [_r(1, "JOJOBA YAĞI"), _r(2, "JOJOBA YAGI (NUMUNE)")]
    assert len(suggest(items, {})) == 1
    assert suggest(items, {}, dismissed=[[1, 2]]) == []


def test_similar_pairs_and_guards():
    items = [_r(1, "JOJOBA YAĞI (NUMUNE)"), _r(2, "PERA GmbH – Golden Jojoba Oil"),
             _r(3, "LAVANTA UÇUCU YAĞI", "ml"), _r(4, "MELEZ LAVANTA UÇUCU YAĞ", "ml"),
             _r(5, "E VİTAMİNİ"), _r(6, "C VİTAMİNİ"),
             _r(7, "PEG-40 HYDROGENATED CASTOR OIL"), _r(8, "PEG-60 HYDROGENATED CASTOR OIL"),
             _r(9, "ZINC OXIDE"), _r(10, "ZINC OXID"),
             _r(11, "KAKAO YAĞI DEODORİZE"), _r(12, "KAKAO YAĞI DEODORİZE PASTİL")]
    got = _by_ids(suggest(items, {}))
    assert got[(9, 10)]["kind"] == "similar" and got[(9, 10)]["score"] >= 0.9     # yazım hatası
    assert got[(11, 12)]["kind"] == "similar" and got[(11, 12)]["score"] == pytest.approx(0.75)
    # 2/3 örtüşme (tek belirteç eksik iki belirteçli ad) benzer DEĞİL: altın
    # jojoba / melez lavanta ayrı kart kalır, lab isterse elle gruplar
    assert (1, 2) not in got and (3, 4) not in got
    assert (5, 6) not in got and (7, 8) not in got
    assert set(got) == {(9, 10), (11, 12)}


@pytest.mark.parametrize("a, b", [
    ("SETİL STEARİL ALKOL", "SETİL ALKOL"),            # setearil ≠ setil alkol (Jaccard 2/3)
    ("SETİL STEARİL ALKOL", "STEARİL ALKOL"),
    ("SETİL ALKOL", "STEARİL ALKOL"),
    ("BADEM YAĞI", "ACI BADEM YAĞI"),
    ("E VİTAMİNİ", "VİTAMİN E ASETAT"),
    ("SODYUM LAURİL SÜLFAT", "SODYUM LAURET SÜLFAT"),  # SLS ≠ SLES (difflib 0,9)
])
def test_different_materials_not_similar(a, b):
    assert suggest([_r(1, a), _r(2, b)], {}) == []


@pytest.mark.parametrize("a, b", [
    ("SETİL STERAİL ALKOL", "SETİL STEARİL ALKOL"),    # komşu harf yer değiştirmesi
    ("LAURYL GLUCOSIDE", "LAURİL GLUKOZİT"),           # Y ↔ İ yazım farkı
    ("SODYUM LAURİL SÜLFAT", "SODIUM LAURYL SULFAT"),
])
def test_typos_still_similar(a, b):
    out = suggest([_r(1, a), _r(2, b)], {})
    assert [(s["kind"], s["item_ids"]) for s in out] == [("similar", [1, 2])]


def test_dismissed_pair_splits_same_key_component():
    """126 ↔ 593 "farklı" denince üçlü öneri geri gelmez: union geçişli
    olduğundan 599 üzerinden yine tek bileşen oluyordu."""
    items = [_r(126, "SETİL STEARİL ALKOL"), _r(599, "CETYL STEARYL ALCOHOL", "adet"),
             _r(593, "CETEARYL ALCOHOL", "adet")]
    out = suggest(items, {}, dismissed=[(126, 593)])
    assert [(s["kind"], s["item_ids"]) for s in out] == [("same_key", [126, 599])]
    # 599 de 126'dan farklıysa kalan çift 593–599
    out = suggest(items, {}, dismissed=[(126, 593), (126, 599)])
    assert [s["item_ids"] for s in out] == [[593, 599]]
    assert suggest(items, {}, dismissed=[(126, 593), (126, 599), (593, 599)]) == []


def test_dismissed_pair_applies_through_group():
    """126 ile 599 gruptaysa ve 126 ↔ 593 "farklı" denmişse 593 gruba
    (599 üzerinden) önerilmez — gruba katılmak 126'yla aynı grupta olmaktır."""
    items = [_r(126, "SETİL STEARİL ALKOL", material_group_id=5),
             _r(599, "CETYL STEARYL ALCOHOL", "adet", material_group_id=5),
             _r(593, "CETEARYL ALCOHOL", "adet"), _r(700, "CETEARYL ALCOHOL NF")]
    out = suggest(items, {5: "Stearil"}, dismissed=[(126, 593)])
    assert not any(593 in s["item_ids"] and {126, 599} & set(s["item_ids"]) for s in out)
    assert all(s["action"] != "join" or 593 not in s["item_ids"] for s in out)
    assert any(700 in s["item_ids"] and s["action"] == "join" for s in out)   # benzer kart yine önerilir
    # red yokken 593 gruba katılma önerisi
    out = suggest(items, {5: "Stearil"})
    assert any(s["item_ids"] == [126, 593, 599] and s["action"] == "join" for s in out)


def test_only_active_raw_cards_same_domain():
    items = [_r(1, "GLİSERİN"), _r(2, "Glycerine", is_active=False),
             _r(3, "GLYCERIN", category="Ambalaj"), _r(4, "Glycerin", category="Bitmiş Ürün"),
             _r(5, "GLİSERİN", category="Kimyasal", domain="supplement")]
    assert suggest(items, {}) == []
    items.append(_r(6, "Gliserin", category="Yardımcı"))
    assert [s["item_ids"] for s in suggest(items, {})] == [[1, 6]]


def test_name_tr_bridges_cards():
    items = [_r(1, "Shea Butter", name_tr="SHEA YAĞI"), _r(2, "SHEA YAGI")]
    assert [s["item_ids"] for s in suggest(items, {})] == [[1, 2]]


def test_similar_to_group_collapses_to_one_suggestion():
    items = [_r(1, "SETİL STEARİL ALKOL", material_group_id=9),
             _r(2, "CETYL STEARYL ALCOHOL", material_group_id=9),
             _r(3, "CETEARYL ALCOHOL NF")]                     # Jaccard 3/4 her üyeyle
    out = suggest(items, {9: "Stearil"})
    assert len(out) == 1 and out[0]["kind"] == "similar"
    assert 3 in out[0]["item_ids"] and out[0]["group"] == {"id": 9, "name": "Stearil"}
    assert out[0]["action"] == "join"


def test_pending_decision_clusters():
    items = [_r(1, "LAURYL GLUCOSIDE"), _r(2, "LAURYL GLUKOZİT"), _r(3, "LAURİL GLUKOZİT"),
             _r(4, "BAŞKA"), _r(5, "DİĞER")]
    out = suggest(items, {}, pending=[(77, "Lauryl glucoside", [1, 2, 3]),
                                      (78, "Tek aktif", [4, 99]),
                                      (79, "Farklı denmiş", [4, 5])],
                  dismissed=[[4, 5]])
    kinds = [(s["kind"], s["item_ids"], s["decision_id"]) for s in out]
    assert ("pending_decision", [1, 2, 3], 77) in kinds
    # küme içinde kalan ad önerisi tekrar gelmez
    assert not any(k == "same_key" and set(ids) <= {1, 2, 3} for k, ids, _ in kinds)
    assert all(d not in (78, 79) for _, _, d in kinds)
    pend = next(s for s in out if s["kind"] == "pending_decision")
    assert pend["title"] == "Lauryl glucoside" and pend["source"] == "pending_decision"
