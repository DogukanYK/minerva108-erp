"""
Satın Alma Planı — KABUL testi (Rusya siparişi, birleşik 22 ürün).

Ortam değişkeniyle kapılı: `RUSYA_FIXTURE_DIR` yoksa ATLANIR (CI'da ve
deploy.sh test kapısında koşmaz — veri kullanıcının masaüstünde).

    RUSYA_FIXTURE_DIR=/Users/dogukan/Desktop/Claude/Rusya-Siparis \\
        MINERVA_TEST_DB=minerva_test_u .venv/bin/pytest tests/test_purchase_plan_acceptance.py -q -s

Ne yapar:
  1. `PlanInputs`'u Rusya betiklerinin girdilerinden kurar: bom.json (kartlar,
     reçeteler, etiket kardeşleri), fiyat/tedarikci.json (IMS tedarikçi
     kartları + kart tedarikçisi), fiyat/lots.json (lotlar, numuneden
     çevrilenler) ve STOK SON DURUM.xlsx (`parse_stok_son_durum`, USD/kg).
  2. docBirlesik22.json'u `PlanRequest`'e çevirir (satırlar, ek ambalaj,
     bekletilen 638, hariç su 64, Rusça etiket, koli/palet serbest satır,
     build.py EXPLICIT_MERGE → manual_merges).
  3. Motor + fiyat katmanının satır bazında gereken/stok/alınacak/fiyat/tutar
     değerlerini `Rusya_Birlesik_22_fiyat_debug.json` + `docBirlesik22.final.json`
     ile karşılaştırır, farkları BİLİNEN SEBEBE göre sınıflar ve tabloyu basar.
  4. Açıklanamayan sayısal fark SIFIR olmalı; fiyat listesinden fiyatlanan
     hammaddelerin toplamı açıklanmış kalemler dışında 38.862 $ ile tutmalı.
"""
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

import pytest

FIXTURE = os.environ.get("RUSYA_FIXTURE_DIR")
pytestmark = pytest.mark.skipif(not FIXTURE, reason="RUSYA_FIXTURE_DIR tanımlı değil (kabul testi)")

# Bilinen sebepler (spec "explained causes")
UNIT_FIX_IDS = {123, 586}          # TUZ reçetede kg/g karışık; PALMORASA kartı 'adet' — artık düzeltilmez, uyarılır
# UNIT_FIX farkı YALNIZ satır birim uyarısı taşıyorsa açıklanmış sayılır
# ("düzeltilmez, uyarılır" iddiasını kabul testi de doğrulasın)
UNIT_CAUTIONS = {"suspect_unit", "unit_mismatch"}
XLS_ALIAS_IDS = {637}              # PAPATYA HİDROSOL fiyatı başka satırdan (yalnız fiyat için takma ad — sonraki aşama)
# Harness eşlemesi: listedeki "ALEOVERA EKSTRAKTI" satırı adı yazım farkıyla hiçbir
# kartla birebir eşleşmiyor; referans betik onu aynı malzemenin İKİNCİ kartı (763
# ALOEVERA EKSTRAKTI) sayıp 247'ye bağlamıştı (XLS_ALIAS 247).  Burada fiyat
# satırı 763'e yazılır → motorun "teklifler birleşmiş üyelerin birleşimi"
# kuralı (ALEOVERA vakası) gerçekten sınanır.
XLS_ROW_TO_CARD = {"ALEOVERA EKSTRAKTI": 763}
EXPECTED_LIST_TOTAL = 38862
EXPECTED_LABEL_TOTAL = 119520


def _load(rel):
    with open(os.path.join(FIXTURE, rel), encoding="utf-8") as f:
        return json.load(f)


def _xls_path():
    src = open(os.path.join(FIXTURE, "fiyat", "fiyatli_liste.py"), encoding="utf-8").read()
    m = re.search(r'^XLS\s*=\s*"([^"]+)"', src, re.M)
    return m.group(1) if m else None


def build_inputs():
    from core.consumption import IngredientRec, ItemRec, RecipeRec
    from core.purchase_plan import LotRec, PlanInputs
    from core.purchase_pricing import OfferRec, PriceInputs, SupplierRec
    from core.supplier_prices import default_price_unit, normalize, parse_stok_son_durum

    B = _load("bom.json")
    T = _load("fiyat/tedarikci.json")
    L = _load("fiyat/lots.json")

    def rec(d):
        return ItemRec(id=int(d["id"]), name=d["name"] or "", category=d.get("category") or "",
                       unit=d.get("unit") or "", pkg_type=d.get("pkg"), language=d.get("lang"),
                       label_group=d.get("label_group"), current_stock=float(d.get("stock") or 0.0),
                       is_active=bool(d.get("active", True)), domain=d.get("domain") or "cosmetics")

    items = {}
    for d in B["all_items"]:
        items[int(d["id"])] = rec(d)
    for d in B["cards"]:
        items.setdefault(int(d["id"]), rec(d))
    for r in B["recipes"].values():
        for ing in r["ingredients"]:
            items.setdefault(int(ing["id"]), rec(ing))
    sibs = {}
    for grp, lst in B["label_siblings"].items():
        for d in sorted(lst, key=lambda x: x["id"]):
            it = items.setdefault(int(d["id"]), rec(d))
            if it.is_active and it.language:
                sibs.setdefault(grp, {}).setdefault(it.language, it)
    recipes = {}
    for rid, r in B["recipes"].items():
        recipes[int(rid)] = RecipeRec(
            id=int(rid), name=r["name"] or "", output_quantity=float(r["out_qty"] or 1.0),
            waste_percentage=float(r["waste"] or 0.0), target_item_id=r.get("target_item_id"),
            ingredients=tuple(IngredientRec(item_id=int(i["id"]), quantity=float(i["qty"] or 0.0),
                                            unit=i.get("unit"))
                              for i in r["ingredients"]))
    by_target = {}
    for r in sorted(recipes.values(), key=lambda r: r.id):
        if r.target_item_id:
            by_target[r.target_item_id] = r.id

    suppliers = {int(s["id"]): SupplierRec(id=int(s["id"]), name=s["name"] or "",
                                           contact_person=s.get("contact_person"), phone=s.get("phone"),
                                           email=s.get("email"), address=s.get("address"))
                 for s in T["suppliers"]}
    sup_by_norm = {}
    for s in sorted(suppliers.values(), key=lambda s: s.id):
        sup_by_norm.setdefault(normalize(s.name), s.id)
    lots = defaultdict(list)
    for l in L["lots"]:
        sid = sup_by_norm.get(normalize(l["sup"])) if l["sup"] else None
        lots[int(l["item"])].append(LotRec(
            item_id=int(l["item"]), lot_number=l["lot"] or "", supplier_id=sid, supplier_name=l["sup"],
            is_sample=bool(l["sample"]), created_at=datetime.fromisoformat(l["at"]) if l["at"] else None,
            quantity=float(l["qty"] or 0.0)))
    converted = {(int(t["item_id"]), t["lot_number"] or "") for t in L["tx"]
                 if "Numune stoğa çevrildi" in (t["notes"] or "")}

    pin_inputs = PlanInputs(
        items=items, recipes=recipes, label_siblings=sibs, recipe_by_target=by_target,
        lots=dict(lots), converted_lots=converted,
        stock_as_of=datetime(2026, 10, 2, 9, 0), domain="cosmetics", default_excluded=(64,))

    # Fiyat listesi → OfferRec (import_prices eşleme kuralı: Item.name normalize, ilk kart)
    offers = defaultdict(list)
    xls = _xls_path()
    unmatched = []
    if xls and os.path.exists(xls):
        rows = parse_stok_son_durum(open(xls, "rb").read())
        item_by_norm = {}
        for it in sorted(B["all_items"], key=lambda d: d["id"]):
            if it.get("active", True):
                item_by_norm.setdefault(normalize(it["name"]), int(it["id"]))
        for row in rows:
            iid = item_by_norm.get(normalize(row["material"]))
            if iid is None:
                iid = XLS_ROW_TO_CARD.get(row["material"].strip().upper())
            if iid is None:
                unmatched.append(row["material"])
                continue
            for s in row["suppliers"]:
                offers[iid].append(OfferRec(
                    item_id=iid, supplier_name=s["name"], supplier_id=sup_by_norm.get(normalize(s["name"])),
                    unit_price=s["price"], package_size=s["package"], currency="USD",
                    price_unit=default_price_unit(items[iid].unit, "kg"), source="stok_son_durum",
                    source_label="STOK SON DURUM (Haziran 2026)"))
    price_inputs = PriceInputs(
        offers=dict(offers), suppliers=suppliers,
        card_supplier={int(k): int(v) for k, v in T["item_supplier"].items() if v is not None},
        lots=dict(lots), converted_lots=converted)
    return pin_inputs, price_inputs, unmatched, bool(xls and os.path.exists(xls))


def build_request(label_faces=None):
    from core.purchase_plan_models import PlanRequest

    O = _load("docBirlesik22.json")
    lines = []
    for l in O["lines"]:
        extras = []
        for ex in l.get("pkg_extra", []):
            if "id" in ex:
                extras.append({"item_id": ex["id"], "per_unit": ex.get("per", 1)})
            else:
                extras.append({"new_name": ex["new"], "pkg_type": ex.get("pkg"), "note": ex.get("note"),
                               "per_unit": ex.get("per", 1)})
        ln = {"recipe_id": l["recipe"], "qty": l["qty"], "scale": l.get("scale", 1.0),
              "use_recipe_packaging": l.get("use_pkg", True), "extra_packaging": extras,
              "display_name": l["name"]}
        if label_faces and l["name"] in label_faces:
            ln["label_faces"] = label_faces[l["name"]]
        lines.append(ln)
    held_reason = "Laboratuvar hangi aloe olduğunu (toz / su / jel) söyleyince alınacak."
    return PlanRequest(**{
        "title": "Rusya Siparişi — Birleşik 22 ürün",
        "lines": lines,
        "manual_lines": [{"section": "shipping", "name": s["name"], "qty_text": s["qty"]} for s in O["shipping"]],
        "options": {
            "stock_mode": "net", "label_mode": "new", "new_label_title": "Rusça etiket",
            "excluded_item_ids": [64], "held_items": [{"item_id": i, "reason": held_reason} for i in O["hold_ids"]],
            "manual_merges": [[586, 218], [251, 243], [763, 247]],   # build.py EXPLICIT_MERGE
            "currency": "USD", "checklist_owner": "Satın alma ekibi",
        },
    })


def run_plan(label_faces=None):
    from core.purchase_plan import compute
    from core.purchase_pricing import attach

    pin_inputs, price_inputs, unmatched, has_xls = build_inputs()
    req = build_request(label_faces)
    res = compute(pin_inputs, req)
    attach(res, price_inputs, None, currency="USD")
    return res, pin_inputs, unmatched, has_xls


def _label_layout():
    sys.path.insert(0, FIXTURE)
    try:
        import urunler  # noqa: WPS433 (fixture betiği)
        return {k: (1 if v[0] == ["tek"] else len(v[0])) for k, v in urunler.LABEL_LAYOUT.items()}
    finally:
        sys.path.remove(FIXTURE)


# ─── Karşılaştırma ──────────────────────────────────────────────────────────

def compare(res, inputs):
    F = _load("docBirlesik22.final.json")
    D = _load("Rusya_Birlesik_22_fiyat_debug.json")
    dbg = {r["id"]: r for r in D["rows"]}
    ref_rows = {m["id"]: m for m in F["raw_short"] + F["raw_ok"] + F["packaging"] + F["held"]}
    ours = res["materials"] + res["held"]
    by_member = {}
    for m in ours:
        for i in m["member_ids"]:
            by_member[i] = m
        if m["is_new_item"]:
            by_member[m["name"].upper()] = m
    items = inputs.items
    diffs = []
    matched = set()

    def add(rid, name, field, ref, got, cause):
        diffs.append({"id": rid, "name": name, "field": field, "ref": ref, "got": got, "cause": cause})

    for rid, rm in ref_rows.items():
        m = by_member.get(rid) if rid > 0 else by_member.get(rm["name"].upper())
        if m is None:
            add(rid, rm["name"], "row", "var", "YOK", None)
            continue
        matched.add(m["key"])
        members = set(m["member_ids"])
        neg = any((items[i].current_stock or 0) < 0 for i in members if i in items)
        unitfix = bool(members & UNIT_FIX_IDS) and any(c["code"] in UNIT_CAUTIONS for c in m["cautions"])
        # gereken (ham)
        if abs(rm["need"] - m["need"]) > 1e-6 * max(1.0, abs(rm["need"])):
            add(rid, rm["name"], "need", rm["need"], m["need"], "UNIT_FIX" if unitfix else None)
        if abs(rm["stock"] - m["stock"]) > 1e-6 * max(1.0, abs(rm["stock"])):
            add(rid, rm["name"], "stock", rm["stock"], m["stock"],
                "NEG_CLAMP" if neg else ("UNIT_FIX" if unitfix else None))
        d = dbg.get(rid)
        if d is None:
            ref_buy = rm.get("short", max(0.0, rm["need"] - rm["stock"]))
            if (ref_buy > 0) != (m["status"] == "to_buy") and rid not in {h["id"] for h in F["held"]}:
                add(rid, rm["name"], "status", ref_buy, m["status"],
                    "NEG_CLAMP" if neg else ("UNIT_FIX" if unitfix else None))
            continue
        disp = m["display"]
        cause_q = "NEG_CLAMP" if neg else ("UNIT_FIX" if unitfix else None)
        for f_ref, f_got in (("g", "need_text"), ("e", "stock_text"), ("a", "buy_text")):
            if d[f_ref] != disp[f_got]:
                add(rid, rm["name"], f_got, d[f_ref], disp[f_got], cause_q)
        if abs(float(d["q"]) - float(disp["buy_num"])) > 1e-9:
            add(rid, rm["name"], "buy_num", d["q"], disp["buy_num"], cause_q)
        cause_p = ("XLS_ALIAS" if rid in XLS_ALIAS_IDS else "PARASUT_B" if d["group"] == "B"
                   else cause_q)
        rp, gp = d["price"], m.get("price")
        if (rp is None) != (gp is None) or (rp is not None and abs(rp - gp) > 1e-9):
            add(rid, rm["name"], "price", rp, gp, cause_p)
        if d["amount"] != m.get("amount"):
            add(rid, rm["name"], "amount", d["amount"], m.get("amount"), cause_p)

    for m in ours:
        if m["key"] not in matched and m["status"] == "to_buy":
            add(m["item_id"], m["name"], "row", "YOK", "var", None)

    # Ürün kapasitesi (build.py "üretilebilir" sütunu)
    for p, rp in zip(res["products"], F["products"]):
        want = int(str(rp["can_make"]).split()[0].replace(".", "")) if rp["can_make"][0].isdigit() else None
        if want != p["capacity"]["producible"]:
            add(f"ürün {p['no']}", p["name"], "producible", want, p["capacity"]["producible"], None)

    # Saf su (hariç) — referans litre
    water = next((e for e in res["excluded"] if 64 in e["member_ids"]), None)
    if water is None or abs(water["need"] / 1000 - F["water_l"]) > 1e-6:
        add(64, "DİSTİLE SU", "excluded", F["water_l"], water and water["need"] / 1000, None)

    # Etiket satırları (Rusça, yüz sayısı build.py LABEL_LAYOUT'tan elle verilmişti)
    layout = _label_layout()
    for lab in res["labels_new"]:
        want = layout.get(lab["product"])
        if want is not None and want != lab["faces"]:
            add(f"etiket {lab['no']}", lab["product"], "label_faces", want, lab["faces"], "LABEL_FACES")
    return diffs


def _print_table(diffs, res, totals):
    by = Counter(d["cause"] or "AÇIKLANAMAYAN" for d in diffs)
    print("\n── Satın Alma Planı kabul farkları (Rusya birleşik 22) ──")
    print(f"{'id':>10}  {'malzeme':<38} {'alan':<11} {'referans':>16} {'motor':>16}  sebep")
    for d in sorted(diffs, key=lambda d: (d["cause"] is not None, str(d["cause"]), str(d["id"]))):
        print(f"{str(d['id']):>10}  {str(d['name'])[:38]:<38} {d['field']:<11} "
              f"{str(d['ref'])[:16]:>16} {str(d['got'])[:16]:>16}  {d['cause'] or 'AÇIKLANAMAYAN'}")
    print("Özet:", dict(by))
    print("Toplamlar:", totals)


def test_rusya_birlesik_22_acceptance():
    res, inputs, unmatched, has_xls = run_plan()
    if not has_xls:
        pytest.skip("STOK SON DURUM.xlsx yok (fiyatli_liste.py XLS yolu)")
    diffs = compare(res, inputs)

    # 42 fiyat listesi hammaddesi = 38.862 $ (açıklanmış kalemler hariç)
    D = _load("Rusya_Birlesik_22_fiyat_debug.json")
    ref_a = [r for r in D["rows"] if not r["pkg"] and r["group"] == "A"]
    explained = [r for r in ref_a if r["id"] in XLS_ALIAS_IDS]
    ours_list = [m for m in res["materials"] if m["kind"] == "raw" and m["status"] == "to_buy"
                 and m.get("group") == "list"]
    ours_total = sum(m["amount"] for m in ours_list)
    totals = {"ref_A_count": len(ref_a), "ref_A_total": sum(r["amount"] for r in ref_a),
              "ours_list_count": len(ours_list), "ours_list_total": ours_total,
              "explained_out": [(r["id"], r["amount"]) for r in explained],
              "labels_new_total": res["meta"]["counts"]["labels_new_total"],
              "unmatched_xls_rows": unmatched}
    _print_table(diffs, res, totals)

    unexplained = [d for d in diffs if d["cause"] is None]
    assert not unexplained, f"Açıklanamayan {len(unexplained)} fark: {unexplained[:10]}"
    assert len(ref_a) == 42 and sum(r["amount"] for r in ref_a) == EXPECTED_LIST_TOTAL
    assert ours_total == EXPECTED_LIST_TOTAL - sum(r["amount"] for r in explained)
    assert len(ours_list) == 42 - len(explained)
    # Fiyatsız hammadde: referansın 33 C kalemi + Paraşüt (B) hammaddeleri + takma ad kalemi
    ref_b_raw = [r for r in D["rows"] if not r["pkg"] and r["group"] == "B"]
    pc = res["pricing"]["counts"]
    assert pc["raw_unpriced"] == 33 + len(ref_b_raw) + len(explained)
    assert pc["packaging_priced"] + pc["packaging_unpriced"] == 17
    assert len(res["held"]) == 1 and res["held"][0]["display"]["buy_text"] == "233,2 kg"
    # TUZ (123): 350 ml reçetesinde 269,5 'kg', satır 17'de ×0,43 ölçekle 150 ml'ye
    # uyarlanmış → motor düzeltmez, MALZEME satırında uyarır
    tuz = next(m for m in res["materials"] if 123 in m["member_ids"])
    su = next(c for c in tuz["cautions"] if c["code"] == "suspect_unit")
    assert "“Vücut Peelingi 150 ml”" in su["text"] and "ürün boyu 150 ml" in su["text"]
    assert "1 adet için 115,5 g" in su["text"]
    # PALMORASA (586, 'adet') + PALMAROSA (218, 'ml') birleşik satırı birim farkını uyarır
    palma = next(m for m in res["materials"] + res["held"] if 586 in m["member_ids"])
    assert "unit_mismatch" in [c["code"] for c in palma["cautions"]]


def test_rusya_label_total_with_layout_faces():
    """Etiket yüzleri build.py LABEL_LAYOUT'taki gibi verilirse 22 satır = 119.520 adet."""
    res, *_ = run_plan(label_faces=_label_layout())
    assert len(res["labels_new"]) == 22
    assert res["meta"]["counts"]["labels_new_total"] == EXPECTED_LABEL_TOTAL
