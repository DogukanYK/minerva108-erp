"""
Ortak tüketim motoru (core/consumption.py) testleri.

  • Saf birim (DB'siz): fire yalnız hammaddede, ambalaj/etiket muaf, etiket
    kardeş çözümü + kardeş yoksa atlama, label_group reçete başına bir kez,
    scale yalnız hammadde, include_packaging=False, exclude/new modları
  • PARİTE: gerçek `POST /api/production` uç noktasının yazdığı Output
    transaction toplamları kalem başına `expand_recipe` brütüne eşit olmalı.
    start_production artık satırları core/production_plan üzerinden (o da
    expand_recipe ile) kurar; yazım tarafı değişirse bu test kırılır.
    P2 — bölünmüş üretimde (aynı malzeme grubundan ikinci kart) Output'lar
    iki karta dağılır; eşitlik o zaman tüketim dökümünün REÇETE KARTI
    (`recipe_item_id` ↔ `source_item_id`) toplamıyla aranır.
"""
from collections import defaultdict
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from core.consumption import (IngredientRec, ItemRec, RecipeRec, expand_recipe,
                              load_recipe_recs)
from database import (Inventory, Item, MaterialGroup, ProductionConsumption, Recipe,
                      RecipeIngredient, Transaction)

_HDR = {"Origin": "http://testserver"}


# ─── Saf birim — sentetik kayıtlar ──────────────────────────────────────────

RAW = ItemRec(id=1, name="Su", category="Hammadde", unit="ml")
OIL = ItemRec(id=2, name="Yağ", category="Hammadde", unit="g")
BOTTLE = ItemRec(id=3, name="Şişe", category="Ambalaj", unit="adet", pkg_type="şişe")
LBL_TR = ItemRec(id=4, name="Etiket TR", category="Ambalaj", unit="adet",
                 pkg_type="etiket", language="TR", label_group="G1")
LBL_EN = ItemRec(id=5, name="Etiket EN", category="Ambalaj", unit="adet",
                 pkg_type="etiket", language="EN", label_group="G1")
LBL_PLAIN = ItemRec(id=6, name="Dilsiz Etiket", category="Ambalaj", unit="adet",
                    pkg_type="etiket")
ITEMS = {i.id: i for i in (RAW, OIL, BOTTLE, LBL_TR, LBL_EN, LBL_PLAIN)}
SIBS = {"G1": {"TR": LBL_TR, "EN": LBL_EN}}


def _rec(*ings, output=1.0, waste=10.0):
    return RecipeRec(id=1, name="Krem", output_quantity=output, waste_percentage=waste,
                     ingredients=tuple(IngredientRec(item_id=i, quantity=q) for i, q in ings))


def _by_item(exp):
    return {ln.item_id: ln for ln in exp.lines}


def test_fire_on_raw_packaging_and_label_exempt():
    exp = expand_recipe(_rec((1, 10), (3, 1), (4, 1), output=1, waste=10), 100, ITEMS, SIBS)
    m = _by_item(exp)
    assert m[1].kind == "raw" and m[1].factor == pytest.approx(1.1)
    assert m[1].gross == pytest.approx(1100)          # 10 × 100 × 1.10
    assert m[1].net_per_unit == pytest.approx(10) and m[1].per_unit == pytest.approx(11)
    assert m[3].kind == "packaging" and m[3].factor == 1.0 and m[3].gross == pytest.approx(100)
    assert m[4].kind == "label" and m[4].factor == 1.0 and m[4].gross == pytest.approx(100)


def test_output_quantity_divides():
    # Reçete 50 adetlik parti için 500 ml → 1 adet = 10 ml (fire %0)
    exp = expand_recipe(_rec((1, 500), output=50, waste=0), 20, ITEMS, SIBS)
    assert _by_item(exp)[1].gross == pytest.approx(200)


def test_label_resolves_to_sibling_language():
    exp = expand_recipe(_rec((4, 1)), 10, ITEMS, SIBS, label_mode="EN")
    m = _by_item(exp)
    assert 4 not in m and m[5].gross == pytest.approx(10)
    assert m[5].source_item_id == 4 and m[5].label_group == "G1"


def test_label_missing_sibling_is_skipped_and_recorded():
    sibs = {"G1": {"TR": LBL_TR}}                      # EN kardeş yok / pasif
    exp = expand_recipe(_rec((1, 1), (4, 1)), 10, ITEMS, sibs, label_mode="EN")
    assert set(_by_item(exp)) == {1}
    assert len(exp.skipped_labels) == 1
    sk = exp.skipped_labels[0]
    assert sk["label"] == "Etiket TR" and sk["label_group"] == "G1" and sk["language"] == "EN"


def test_label_group_counted_once_per_recipe():
    # Reçetede TR + EN kardeşin ikisi de var → grup yalnız bir kez düşer
    exp = expand_recipe(_rec((4, 1), (5, 1)), 10, ITEMS, SIBS, label_mode="TR")
    m = _by_item(exp)
    assert set(m) == {4} and m[4].gross == pytest.approx(10)
    exp = expand_recipe(_rec((5, 1), (4, 1)), 10, ITEMS, SIBS, label_mode="TR")
    assert set(_by_item(exp)) == {4}


def test_same_item_twice_is_merged():
    exp = expand_recipe(_rec((1, 10), (1, 5), waste=0), 2, ITEMS, SIBS)
    ln = _by_item(exp)[1]
    assert ln.gross == pytest.approx(30) and ln.per_unit == pytest.approx(15) and ln.rows == 2


def test_scale_multiplies_raw_only():
    exp = expand_recipe(_rec((1, 10), (3, 1), (4, 1), waste=10), 100, ITEMS, SIBS, scale=2.0)
    m = _by_item(exp)
    assert m[1].gross == pytest.approx(2200) and m[1].net_per_unit == pytest.approx(20)
    assert m[3].gross == pytest.approx(100) and m[4].gross == pytest.approx(100)


def test_include_packaging_false_drops_packaging_and_labels():
    sibs = {"G1": {"TR": LBL_TR}}
    exp = expand_recipe(_rec((1, 10), (3, 1), (4, 1), (6, 1)), 10, ITEMS, sibs,
                        label_mode="EN", include_packaging=False)
    assert set(_by_item(exp)) == {1}
    assert exp.skipped_labels == ()                    # düşen etiket "atlandı" sayılmaz


def test_exclude_mode_drops_all_labels():
    exp = expand_recipe(_rec((1, 10), (3, 1), (4, 1), (6, 1)), 10, ITEMS, SIBS,
                        label_mode="exclude")
    assert set(_by_item(exp)) == {1, 3}
    assert exp.label_groups == () and exp.skipped_labels == ()


def test_new_mode_reports_label_groups_without_consuming():
    exp = expand_recipe(_rec((1, 10), (3, 1), (4, 1), (5, 1), (6, 2), output=2), 10, ITEMS, SIBS,
                        label_mode="new")
    assert set(_by_item(exp)) == {1, 3}
    groups = {g["label_group"] or g["name"]: g for g in exp.label_groups}
    assert set(groups) == {"G1", "Dilsiz Etiket"}     # TR+EN aynı grup → tek satır
    assert groups["G1"]["languages"] == ["EN", "TR"]
    assert groups["G1"]["gross"] == pytest.approx(5)          # 1 × 10 / 2
    assert groups["Dilsiz Etiket"]["per_unit"] == pytest.approx(1)


def test_missing_item_recorded():
    exp = expand_recipe(_rec((1, 1), (999, 1)), 1, ITEMS, SIBS)
    assert set(_by_item(exp)) == {1} and exp.missing_item_ids == (999,)


def test_invalid_label_mode_rejected():
    with pytest.raises(ValueError):
        expand_recipe(_rec((1, 1)), 1, ITEMS, SIBS, label_mode="DE")


# ─── DB yükleyici ────────────────────────────────────────────────────────────

def _parity_recipe(db: Session, *, waste=7.5, output=4, domain="cosmetics"):
    """Hammadde (lotlu + lotsuz) + ambalaj + TR/EN etiket çifti + dilsiz etiket."""
    raw_lot = Item(name="Parite Yağ", sku="par-raw1", category="Hammadde", unit="g",
                   current_stock=10_000, domain=domain)
    raw_nolot = Item(name="Parite Su", sku="par-raw2", category="Hammadde", unit="ml",
                     current_stock=10_000, domain=domain)
    bottle = Item(name="Parite Şişe", sku="par-amb", category="Ambalaj", pkg_type="şişe",
                  unit="adet", current_stock=10_000, domain=domain)
    lbl_tr = Item(name="Parite Etiket TR", sku="par-ltr", category="Ambalaj", pkg_type="etiket",
                  language="TR", label_group="PAR", unit="adet", current_stock=10_000, domain=domain)
    lbl_en = Item(name="Parite Etiket EN", sku="par-len", category="Ambalaj", pkg_type="etiket",
                  language="EN", label_group="PAR", unit="adet", current_stock=10_000, domain=domain)
    lbl_plain = Item(name="Parite Kutu Etiketi", sku="par-lpl", category="Ambalaj",
                     pkg_type="etiket", unit="adet", current_stock=10_000, domain=domain)
    tgt = Item(name="Minerva 108 Parite Krem", sku="par-tgt", category="Bitmiş Ürün",
               unit="adet", current_stock=0, domain=domain)
    db.add_all([raw_lot, raw_nolot, bottle, lbl_tr, lbl_en, lbl_plain, tgt]); db.flush()
    # Hammadde lotları — iki lot (FIFO ikisine bölünsün) ama toplamı brütten
    # (3.3 × 6/4 × 1.075 ≈ 5.32) az: tahsis + 'uncovered' artık yolu da çalışsın.
    for i, (lot, q) in enumerate((("L-ESKI", 2.0), ("L-YENI", 1.5))):
        db.add(Inventory(item_id=raw_lot.id, lot_number=lot, quantity=q, status="APPROVED",
                         created_at=datetime.utcnow() - timedelta(days=10 - i), domain=domain))
    rec = Recipe(name="Parite Krem", output_quantity=output, output_unit="adet",
                 target_item_id=tgt.id, waste_percentage=waste, domain=domain)
    db.add(rec); db.flush()
    for it, q in ((raw_lot, 3.3), (raw_nolot, 17.25), (bottle, 4), (lbl_tr, 4), (lbl_en, 4),
                  (lbl_plain, 8)):
        db.add(RecipeIngredient(recipe_id=rec.id, item_id=it.id, quantity=q, unit=it.unit))
    db.commit()
    return rec.id, {"raw_lot": raw_lot.id, "raw_nolot": raw_nolot.id, "bottle": bottle.id,
                    "lbl_tr": lbl_tr.id, "lbl_en": lbl_en.id, "lbl_plain": lbl_plain.id}


def test_loader_builds_records_and_siblings(db_session: Session):
    rid, ids = _parity_recipe(db_session)
    recs, items, sibs = load_recipe_recs(db_session, [rid], "cosmetics")
    assert len(recs) == 1 and len(recs[0].ingredients) == 6
    assert recs[0].output_quantity == 4 and recs[0].waste_percentage == 7.5
    assert sibs["PAR"]["TR"].id == ids["lbl_tr"] and sibs["PAR"]["EN"].id == ids["lbl_en"]
    assert recs[0].target_item_id in items
    # domain kapsamı reçete seviyesinde
    assert load_recipe_recs(db_session, [rid], "supplement") == ([], {}, {})


def test_loader_ignores_inactive_sibling(db_session: Session):
    rid, ids = _parity_recipe(db_session)
    en = db_session.get(Item, ids["lbl_en"]); en.is_active = False; db_session.commit()
    recs, items, sibs = load_recipe_recs(db_session, [rid], "cosmetics")
    assert "EN" not in sibs["PAR"]
    exp = expand_recipe(recs[0], 8, items, sibs, label_mode="EN")
    assert [s["label"] for s in exp.skipped_labels] == ["Parite Etiket TR"]


# ─── PARİTE — gerçek üretim ucu ile ─────────────────────────────────────────

def _outputs_by_item(db: Session) -> dict:
    out = defaultdict(float)
    for tx in db.query(Transaction).filter(Transaction.transaction_type == "Output").all():
        out[tx.item_id] += tx.quantity
    return dict(out)


def _consumed_by_recipe_item(db: Session) -> dict:
    """Tüketim dökümü reçete kartı başına — bölünmüş üretimde de expand_recipe
    satırının (`source_item_id`) brütüne eşit olmalı."""
    out = defaultdict(float)
    for pc in (db.query(ProductionConsumption)
               .filter(ProductionConsumption.kind != "output").all()):
        out[pc.recipe_item_id] += pc.quantity
    return dict(out)


@pytest.mark.parametrize("lang", ["TR", "EN"])
def test_parity_with_real_production_endpoint(authed_client: TestClient, db_session: Session, lang):
    rid, ids = _parity_recipe(db_session)
    qty = 6
    recs, items, sibs = load_recipe_recs(db_session, [rid], "cosmetics")
    exp = expand_recipe(recs[0], qty, items, sibs, label_mode=lang)
    expected = {ln.item_id: ln.gross for ln in exp.lines}

    r = authed_client.post("/api/production", json={
        "recipe_id": rid, "produced_quantity": qty, "label_language": lang,
    }, headers=_HDR)
    assert r.status_code == 201, r.text

    db_session.expire_all()
    actual = _outputs_by_item(db_session)
    assert set(actual) == set(expected), (actual, expected)
    for item_id, gross in expected.items():
        assert actual[item_id] == pytest.approx(gross, abs=1e-5), item_id
    # Lotlu hammadde: iki lot + lot dışı artık = 3 ayrı Output, toplamı brüt
    n_raw = (db_session.query(Transaction)
             .filter(Transaction.item_id == ids["raw_lot"],
                     Transaction.transaction_type == "Output").count())
    assert n_raw == 3
    # Etiket dili gerçekten çözüldü: diğer dilin kardeşinden hiç düşülmedi
    other = ids["lbl_en"] if lang == "TR" else ids["lbl_tr"]
    assert other not in actual
    # Hammadde fireli, ambalaj firesiz
    assert expected[ids["raw_nolot"]] == pytest.approx(17.25 * qty / 4 * 1.075)
    assert expected[ids["bottle"]] == pytest.approx(4 * qty / 4)
    # current_stock aynı miktarda düştü
    for item_id, gross in expected.items():
        it = db_session.get(Item, item_id)
        assert it.current_stock == pytest.approx(10_000 - gross, abs=1e-5)


def test_parity_split_production_by_recipe_item(authed_client: TestClient, db_session: Session):
    """P2 — lotlu hammadde aynı malzeme grubundaki ikinci karttan kısmen
    karşılanır: Output'lar iki karta dağılır, reçete kartı toplamı yine brüt."""
    rid, ids = _parity_recipe(db_session)
    raw = db_session.get(Item, ids["raw_lot"])
    g = MaterialGroup(name="Parite grubu", domain="cosmetics")
    db_session.add(g); db_session.flush()
    alt = Item(name="Parite Yağ (2. firma)", sku="par-raw1b", category="Hammadde", unit="g",
               current_stock=100, domain="cosmetics", material_group_id=g.id)
    raw.material_group_id = g.id
    db_session.add(alt); db_session.commit()
    qty = 6
    recs, items, sibs = load_recipe_recs(db_session, [rid], "cosmetics")
    exp = expand_recipe(recs[0], qty, items, sibs, label_mode="TR")
    expected = {ln.source_item_id: ln.gross for ln in exp.lines}
    gross_raw = expected[ids["raw_lot"]]

    r = authed_client.post("/api/production", json={
        "recipe_id": rid, "produced_quantity": qty, "label_language": "TR",
        "ingredient_sources": {str(ids["raw_lot"]): [
            {"item_id": alt.id, "quantity": 2.0},
            {"item_id": ids["raw_lot"], "quantity": gross_raw - 2.0}]},
    }, headers=_HDR)
    assert r.status_code == 201, r.text

    db_session.expire_all()
    by_recipe = _consumed_by_recipe_item(db_session)
    assert set(by_recipe) == set(expected)
    for rec_item, gross in expected.items():
        assert by_recipe[rec_item] == pytest.approx(gross, abs=1e-5), rec_item
    outs = _outputs_by_item(db_session)
    assert outs[alt.id] == pytest.approx(2.0)
    assert outs[ids["raw_lot"]] == pytest.approx(gross_raw - 2.0, abs=1e-5)
    # Defter = döküm (kart düzeyinde)
    by_card = defaultdict(float)
    for pc in db_session.query(ProductionConsumption).filter(ProductionConsumption.kind != "output"):
        by_card[pc.item_id] += pc.quantity
    assert {k: round(v, 6) for k, v in by_card.items()} == {k: round(v, 6) for k, v in outs.items()}
