"""
"Aynı malzeme" grup stoğu çekirdeği (core/group_stock.py, 13.10.2026).

Kilitlenen sözleşmeler:
  • Birim çevirme yalnız g↔kg, ml↔l (×1000); aynı unit_norm 1:1; g↔ml None.
  • `can_substitute` üretimin kuralı: aktif, aynı panel, aynı tür (bitmiş
    ürün asla), aynı tüketim türü, BİREBİR aynı unit_norm.
  • Taban birim en çok stoklu aileden; başka aile `aside` (g/ml toplanmaz).
  • Döküm "TEDARİKÇİ 180 g · TEDARİKÇİ 24,7 kg"; tedarikçisiz kart adıyla.
  • Grup minimumu = çevrilmiş minimumların EN BÜYÜĞÜ (toplam değil).
  • `load_groups`: aktif grup + aktif, aynı panel üye; negatif stok 0;
    Stoklar `classify` kovaları; kovada <2 üye → grup yok.
  • `low_stock_alerts`: ayar açıkken grup başına tek uyarı (toplam ≤ grup
    min), grubu yeterliyse sessiz; ayar kapalı → eski kart başı kural.
"""
import json

import pytest

from core import group_stock as gs
from core.consumption import ItemRec
from core.production_plan import CFG_ENABLED
from core.purchase_plan import _amount_near
from database import AppSetting, Item, MaterialGroup, Supplier


# ─── Saf yardımcılar ───────────────────────────────────────────────────────

@pytest.mark.parametrize("v,src,dst,want", [
    (5, "g", "g", 5.0), (5, "gr", "gram", 5.0), (1500, "g", "kg", 1.5), (2.5, "kg", "g", 2500.0),
    (750, "ml", "l", 0.75), (2, "lt", "ml", 2000.0), (2, "litre", "l", 2.0), (3, "adet", "adet", 3.0),
    (3, "paket", "adet", 3.0),                         # bilinmeyen birim = sayılan (unit_norm adet)
    (24687.81, "g", "kg", 24.68781),
    (5, "g", "ml", None), (5, "ml", "g", None), (5, "kg", "l", None), (5, "adet", "g", None),
    (5, "g", "adet", None), (None, "g", "kg", 0.0),
])
def test_convert_qty_matrix(v, src, dst, want):
    got = gs.convert_qty(v, src, dst)
    if want is None:
        assert got is None
    else:
        assert got == pytest.approx(want)


def _rec(**kw):
    base = dict(id=1, name="X", category="Hammadde", unit="g", is_active=True, domain="cosmetics")
    base.update(kw)
    return ItemRec(**base)


def test_can_substitute_is_production_rule():
    a = _rec(id=1)
    assert gs.can_substitute(a, _rec(id=2)) is True
    assert gs.can_substitute(a, _rec(id=2, unit="gr")) is True              # aynı unit_norm
    assert gs.can_substitute(a, _rec(id=2, unit="kg")) is False             # üretim çevirmez
    assert gs.can_substitute(a, _rec(id=2, unit="ml")) is False
    assert gs.can_substitute(a, _rec(id=2, is_active=False)) is False
    assert gs.can_substitute(_rec(id=1, is_active=False), _rec(id=2)) is True   # reçete kartı pasif olabilir
    assert gs.can_substitute(a, _rec(id=2, domain="supplement")) is False
    assert gs.can_substitute(a, _rec(id=2, category="Kimyasal")) is True    # ikisi de lot_kind raw
    assert gs.can_substitute(a, _rec(id=2, category="Ambalaj", unit="g")) is False
    fin = _rec(id=1, category="Bitmiş Ürün", unit="adet")
    assert gs.can_substitute(fin, _rec(id=2, category="Bitmiş Ürün", unit="adet")) is False
    bottle = _rec(id=1, category="Ambalaj", pkg_type="şişe", unit="adet")
    assert gs.can_substitute(bottle, _rec(id=2, category="Ambalaj", pkg_type="şişe", unit="adet")) is True
    label = _rec(id=2, category="Ambalaj", pkg_type="etiket", unit="adet")
    assert gs.can_substitute(bottle, label) is False                         # etiket şişeyi karşılamaz
    lang_label = _rec(id=3, category="Ambalaj", pkg_type="şişe", unit="adet", language="TR",
                      label_group="KRM")
    assert gs.can_substitute(bottle, lang_label) is False
    assert gs.can_substitute(None, a) is False and gs.can_substitute(a, None) is False


@pytest.mark.parametrize("cat,pkg,want", [
    ("Hammadde", None, "hammadde"), ("Bitmiş Ürün", None, "finished"), ("Ambalaj", "şişe", "ambalaj"),
    ("Ambalaj", "ETIKET", "etiket"), ("Ambalaj", "etiket", "etiket"), ("Ambalaj", None, "ambalaj"),
    ("Kimyasal", None, "other"), (None, None, "other"), ("hammadde", None, "other"),   # ledger.html birebir
])
def test_classify_mirrors_stoklar(cat, pkg, want):
    assert gs.classify(cat, pkg) == want


def _m(i, unit, stock, mn=0.0, sup=None, name=None):
    return gs.GroupMember(id=i, name=name or f"Kart {i}", unit=unit, stock=max(0.0, stock),
                          raw_stock=stock, min_stock=mn, category="Hammadde", pkg_type=None,
                          supplier_id=None, supplier_name=sup)


def test_pick_base_unit_same_unit_xhantan():
    base, counted, aside = gs.pick_base_unit([_m(105, "g", 180.31), _m(636, "g", 24687.81)])
    assert base == "g" and [m.id for m in counted] == [105, 636] and aside == []


def test_pick_base_unit_g_kg_converts_within_family():
    # Canlıdaki #6: g kartları + stoksuz kg kartı → taban g, kg kartı da sayılır
    base, counted, aside = gs.pick_base_unit([_m(1, "g", 500), _m(2, "g", 100), _m(3, "kg", 0)])
    assert base == "g" and len(counted) == 3 and aside == []
    g = gs.build_group(6, "G6", "hammadde", [_m(1, "g", 500), _m(3, "kg", 2)])
    assert g.base_unit == "kg" and g.total == pytest.approx(2.5) and g.total_text == "2,5 kg"
    # Üye sayısı eşit → stoğu büyük birim; çoğunluk birimi kazanır
    assert gs.pick_base_unit([_m(1, "g", 500), _m(2, "kg", 3)])[0] == "kg"
    assert gs.pick_base_unit([_m(1, "g", 500), _m(2, "g", 1), _m(3, "kg", 3)])[0] == "g"
    assert gs.pick_base_unit([_m(1, "g", 0), _m(2, "kg", 0)])[0] == "g"     # eşitlikte küçük birim


def test_pick_base_unit_g_ml_kept_apart():
    # #12 GÜL HİDROSOLÜ g + ml → en çok stoklu aile (ml), g kartı ayrı
    base, counted, aside = gs.pick_base_unit([_m(1, "g", 100), _m(2, "ml", 5000)])
    assert base == "ml" and [m.id for m in counted] == [2] and [m.id for m in aside] == [1]
    # #13 GİNSENG g×3 + ml
    g3 = [_m(1, "g", 5), _m(2, "g", 5), _m(3, "g", 5)]
    base, counted, aside = gs.pick_base_unit(g3 + [_m(4, "ml", 10)])
    assert base == "g" and len(counted) == 3 and [m.id for m in aside] == [4]
    base, counted, aside = gs.pick_base_unit(g3 + [_m(4, "l", 1)])
    assert base == "l" and [m.id for m in counted] == [4] and len(aside) == 3
    # Hepsi stoksuz → üye sayısı fazla olan aile
    assert gs.pick_base_unit([_m(1, "g", 0), _m(2, "ml", 0), _m(3, "ml", 0)])[0] == "ml"
    # Negatif stok 0 sayılır
    assert gs.pick_base_unit([_m(1, "g", -500), _m(2, "ml", 1)])[0] == "ml"
    assert gs.pick_base_unit([]) == (None, [], [])
    # Sözlük ve ItemRec de kabul edilir
    assert gs.pick_base_unit([{"unit": "kg", "stock": 1}, {"unit": "ml", "current_stock": 2}])[0] == "kg"


def test_amount_short_and_near_default_unchanged():
    assert gs.amount_short(180.31, "g") == "180 g"
    assert gs.amount_short(24687.81, "g") == "24,7 kg"
    assert gs.amount_short(2.5, "kg") == "2,5 kg"
    assert gs.amount_short(750, "ml") == "750 ml"
    assert gs.amount_short(1200, "ml") == "1,2 l"
    assert gs.amount_short(12, "adet") == "12 adet"
    assert _amount_near(180.31, "g") == "180 gram" and _amount_near(1200, "ml") == "1,2 litre"


def test_amount_exact_matrix():
    """Kesin metin: taban birim korunur (g→kg geçişi yok), kg/l ≤3, g/ml/adet ≤2
    ondalık, sondaki sıfır yok, TR ayraçları; pozitif miktar asla "0"."""
    cases = [
        (999.6, "g", "999,6 g"), (1000, "g", "1.000 g"), (1000 - 999.6, "g", "0,4 g"),
        (24687.81, "g", "24.687,81 g"), (1234567.891, "g", "1.234.567,89 g"),
        (180.31, "gr", "180,31 g"), (0.004, "g", "0,01 g"), (0.001, "ml", "0,01 ml"),
        (2.5, "kg", "2,5 kg"), (1.23456, "kg", "1,235 kg"), (1200.5, "kg", "1.200,5 kg"),
        (0.0004, "kg", "0,001 kg"), (0.0004, "l", "0,001 l"), (1.2, "Lt", "1,2 l"),
        (750, "ml", "750 ml"), (1200, "ml", "1.200 ml"), (12, "adet", "12 adet"),
        (0.5, "adet", "0,5 adet"), (0, "g", "0 g"), (None, "kg", "0 kg"), (1e-12, "g", "0 g"),
        (-3.5, "g", "-3,5 g"),
    ]
    for v, u, want in cases:
        assert gs.amount_exact(v, u) == want, (v, u)
    # amount_short'un kaybettiği fark burada kalır
    assert (gs.amount_short(999.6, "g"), gs.amount_short(0.4, "g")) == ("1.000 g", "0 g")
    assert gs.amount_exact(999.6, "g") != gs.amount_exact(1000, "g")


def test_breakdown_text_labels():
    parts = [_m(105, "g", 180.31, sup="HAMMADDESEPETİ"), _m(636, "g", 24687.81, sup="YİĞİTOGLU KİMYA")]
    assert gs.breakdown_text(parts) == "HAMMADDESEPETİ 180 g · YİĞİTOGLU KİMYA 24,7 kg"
    # Tedarikçisiz kart adıyla; aynı tedarikçinin iki kartı kart adıyla ayrılır
    assert gs.breakdown_text([_m(1, "g", 5, name="XANTHAN  GUM")]) == "XANTHAN GUM 5 g"
    two = [_m(1, "kg", 1, sup="A", name="Kart Bir"), _m(2, "kg", 2, sup="A", name="Kart İki"),
           _m(3, "kg", 3, sup="B")]
    assert gs.breakdown_text(two) == "Kart Bir 1,0 kg · Kart İki 2,0 kg · B 3,0 kg"
    assert gs.breakdown_text([]) == ""


def test_group_min_is_largest_converted_member_min():
    g = gs.build_group(1, "G", "hammadde", [_m(1, "g", 100, mn=500), _m(2, "kg", 1, mn=1), _m(3, "g", 0)])
    assert gs.convert_qty(g.min_stock, g.base_unit, "g") == pytest.approx(1000)
    assert gs.group_min(g) == g.min_stock
    assert gs.group_min([_m(1, "g", 0, mn=200), _m(2, "g", 0, mn=300)], "kg") == pytest.approx(0.3)
    assert gs.group_min([_m(1, "g", 0), _m(2, "g", 0)], "g") == 0.0
    # Ayrı ailedeki kartın minimumu sayılmaz
    g = gs.build_group(1, "G", "hammadde", [_m(1, "ml", 5000, mn=100), _m(2, "g", 1, mn=9999)])
    assert g.base_unit == "ml" and g.min_stock == pytest.approx(100)


# ─── DB: load_groups ───────────────────────────────────────────────────────

def _grp(db, name, domain="cosmetics", active=True):
    g = MaterialGroup(name=name, domain=domain, is_active=active)
    db.add(g)
    db.flush()
    return g


def _card(db, name, group, *, stock, unit="g", mn=0.0, sup=None, category="Hammadde",
          pkg_type=None, domain="cosmetics", active=True):
    it = Item(name=name, sku=f"gs-{name}", category=category, unit=unit, current_stock=stock,
              min_stock_level=mn, supplier_id=sup.id if sup else None, pkg_type=pkg_type,
              material_group_id=group.id if group else None, domain=domain, is_active=active)
    db.add(it)
    db.flush()
    return it


def _sup(db, name, domain="cosmetics"):
    s = Supplier(name=name, domain=domain)
    db.add(s)
    db.flush()
    return s


def test_load_groups_xhantan_total_and_breakdown(db_session):
    db = db_session
    g = _grp(db, "XHANTAN GUM")
    a = _card(db, "XHANTAN GUM", g, stock=180.31, sup=_sup(db, "HAMMADDESEPETİ"))
    b = _card(db, "XANTHAN GUM", g, stock=24687.81, sup=_sup(db, "YİĞİTOGLU KİMYA"))
    db.commit()
    groups = gs.load_groups(db, "cosmetics")
    assert list(groups) == [(g.id, "hammadde")]
    x = groups[(g.id, "hammadde")]
    assert x.base_unit == "g" and x.total == pytest.approx(24868.12)
    assert x.total_text == "24,9 kg" and x.aggregates
    assert x.breakdown_text == "HAMMADDESEPETİ 180 g · YİĞİTOGLU KİMYA 24,7 kg"
    assert x.counted_ids == [a.id, b.id] and x.aside == [] and x.aside_text == ""
    d = x.as_dict()
    json.dumps(d)
    assert d["total_text"] == "24,9 kg" and [m["supplier_name"] for m in d["members"]] == [
        "HAMMADDESEPETİ", "YİĞİTOGLU KİMYA"]
    assert gs.index_by_item(groups)[b.id] is x


def test_load_groups_inactive_negative_domain_and_buckets(db_session):
    db = db_session
    g = _grp(db, "Karışık")
    h1 = _card(db, "H1", g, stock=-50)                                   # negatif → 0
    h2 = _card(db, "H2", g, stock=30)
    _card(db, "H3 pasif", g, stock=999, active=False)                    # pasif üye yok sayılır
    _card(db, "H4 başka panel", g, stock=999, domain="supplement")       # başka panel üye yok
    _card(db, "Şişe tek", g, stock=10, unit="adet", category="Ambalaj", pkg_type="şişe")  # kovada tek
    e1 = _card(db, "Etiket 1", g, stock=5, unit="adet", category="Ambalaj", pkg_type="etiket")
    e2 = _card(db, "Etiket 2", g, stock=7, unit="adet", category="Ambalaj", pkg_type="Etiket")
    o1 = _card(db, "Kimyasal 1", g, stock=1, category="Kimyasal")
    o2 = _card(db, "Kimyasal 2", g, stock=2, category=None)
    gp = _grp(db, "Pasif grup", active=False)
    _card(db, "P1", gp, stock=1)
    _card(db, "P2", gp, stock=1)
    gsup = _grp(db, "Takviye grubu", domain="supplement")
    _card(db, "S1", gsup, stock=1, domain="supplement")
    _card(db, "S2", gsup, stock=1, domain="supplement")
    lone = _grp(db, "Tek üyeli")
    _card(db, "L1", lone, stock=1)
    _card(db, "L2 pasif", lone, stock=1, active=False)
    f = _grp(db, "Bitmiş")
    _card(db, "F1", f, stock=3, unit="adet", category="Bitmiş Ürün")
    _card(db, "F2", f, stock=4, unit="adet", category="Bitmiş Ürün")
    db.commit()
    groups = gs.load_groups(db, "cosmetics")
    assert set(groups) == {(g.id, "hammadde"), (g.id, "etiket"), (g.id, "other"), (f.id, "finished")}
    h = groups[(g.id, "hammadde")]
    assert h.counted_ids == [h1.id, h2.id] and h.total == pytest.approx(30)
    assert h.members[0].stock == 0 and h.members[0].raw_stock == -50
    assert groups[(g.id, "etiket")].counted_ids == [e1.id, e2.id]
    assert groups[(g.id, "etiket")].total == pytest.approx(12)
    assert groups[(g.id, "other")].counted_ids == [o1.id, o2.id]
    assert groups[(f.id, "finished")].total == pytest.approx(7)
    sup_groups = gs.load_groups(db, "supplement")
    assert list(sup_groups) == [(gsup.id, "hammadde")]
    assert gs.load_groups(db, "cosmetics", group_ids=[f.id]).keys() == {(f.id, "finished")}
    assert gs.load_groups(db, "cosmetics", group_ids=[]) == {}


def test_load_groups_g_ml_aside(db_session):
    db = db_session
    g = _grp(db, "GİNSENG")
    _card(db, "G1", g, stock=5)
    _card(db, "G2", g, stock=5)
    _card(db, "G3", g, stock=5)
    ml = _card(db, "G ml", g, stock=4, unit="ml", sup=_sup(db, "Sıvı Firma"))
    db.commit()
    x = gs.load_groups(db, "cosmetics")[(g.id, "hammadde")]
    assert x.base_unit == "g" and x.total == pytest.approx(15)
    assert [m.id for m in x.aside] == [ml.id] and x.aside_text == "Sıvı Firma 4 ml"
    assert [m["counted"] for m in x.as_dict()["members"]] == [True, True, True, False]


# ─── Düşük stok bildirimi ──────────────────────────────────────────────────

def _set(db, value):
    row = db.query(AppSetting).filter_by(key=CFG_ENABLED).first()
    if row is None:
        db.add(AppSetting(key=CFG_ENABLED, value=value))
    else:
        row.value = value
    db.commit()


def test_low_stock_alerts_group_rule_and_switch(db_session):
    db = db_session
    g = _grp(db, "XHANTAN GUM")
    a = _card(db, "XHANTAN GUM", g, stock=180, mn=500, sup=_sup(db, "HAMMADDESEPETİ"))
    b = _card(db, "XANTHAN GUM", g, stock=24687.81, mn=1000)
    solo = _card(db, "Grupsuz", None, stock=3, mn=5, unit="kg")
    fine = _card(db, "Grupsuz yeterli", None, stock=9, mn=5)
    nomin = _card(db, "Minsiz", None, stock=-1)
    db.commit()
    # Ayar satırı yok = açık: grubu yeterli kart sessiz, grupsuz eski kural
    assert gs.group_stock_enabled(db) is True
    out = gs.low_stock_alerts(db, [a, a, solo, fine, nomin], "cosmetics")
    assert out == [("Grupsuz", 3.0, 5.0, "kg")]
    # Ayar kapalı → kart başı eski kural
    _set(db, "0")
    assert gs.group_stock_enabled(db) is False
    out = gs.low_stock_alerts(db, [a, b, solo], "cosmetics")
    assert out == [("XHANTAN GUM", 180.0, 500.0, "g"), ("Grupsuz", 3.0, 5.0, "kg")]
    # Ayar açık + grup toplamı ≤ grup minimumu (en büyük min = 1000 g) → TEK uyarı
    _set(db, "1")
    db.get(Item, b.id).current_stock = 700
    db.commit()
    out = gs.low_stock_alerts(db, [a, b], "cosmetics")
    assert out == [("XHANTAN GUM (aynı malzeme, 2 kart)", 880.0, 1000.0, "g")]
    # Başka panelde çağrılırsa grup bulunmaz → eski kural
    assert gs.low_stock_alerts(db, [a], "supplement") == [("XHANTAN GUM", 180.0, 500.0, "g")]


def test_low_stock_alerts_aside_and_lone_cards_use_card_rule(db_session):
    db = db_session
    g = _grp(db, "GÜL HİDROSOLÜ")
    big = _card(db, "Gül ml", g, stock=5000, unit="ml", mn=100)
    small = _card(db, "Gül g", g, stock=10, mn=50)                     # ayrı aile → kart kuralı
    lone = _grp(db, "Tek")
    l1 = _card(db, "Tek kart", lone, stock=1, mn=2)
    _card(db, "Tek pasif", lone, stock=100, active=False)
    db.commit()
    out = gs.low_stock_alerts(db, [big, small, l1], "cosmetics")
    assert out == [("Gül g", 10.0, 50.0, "g"), ("Tek kart", 1.0, 2.0, "g")]
    # Grubun minimumu yoksa grup uyarısı da yok
    g2 = _grp(db, "Minsiz grup")
    m1 = _card(db, "M1", g2, stock=0)
    m2 = _card(db, "M2", g2, stock=0)
    db.commit()
    assert gs.low_stock_alerts(db, [m1, m2], "cosmetics") == []
