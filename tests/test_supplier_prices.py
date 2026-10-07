"""
Tedarikçi fiyat listesi — Işık Hanım'ın "Stok Son Durum" Excel'inden satın alma
raporunu otomatik dolduran modülün testleri.

  • parse_stok_son_durum  — onun sütun düzenini (Ted-1 paket ADından ÖNCE) doğru okur
  • import_prices         — malzeme/tedarikçi eşler, eşleşmeyeni raporlar, replace eder
  • prices_for_items      — en ucuzdan, None sona, limit
  • build_workbook(prices)— Satın Alma sayfası tedarikçi sütunlarıyla genişler
  • uçlar                 — içe aktar (finans), liste (reports.view), sil; yetki
"""
import io

from fastapi.testclient import TestClient
from openpyxl import Workbook

from api_main import app
from database import Item, Supplier, SupplierPrice

_H = {"Origin": "http://testserver"}
_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _make_xlsx(rows) -> bytes:
    """rows = [(material, kategori, birim, [(ad,paket,fiyat), …]), …] → Işık'ın düzeni."""
    wb = Workbook()
    ws = wb.active
    ws.append(["Malzeme", "Kategori", "Birim", "Toplam Gereken (fireli)", "Mevcut Stok",
               "ALINACAK (eksik)", "ALINABİLECEK MİKTAR (KG)", "TEDARİKÇİ-1", "BİRİM FİYAT ",
               "TEDARİKÇİ-2", "ALINABİLECEK MİKTAR ", "BİRİM FİYAT ",
               "TEDARİKÇİ-3", "ALINABİLECEK MİKTAR ", "BİRİM FİYAT "])
    for material, kat, birim, sups in rows:
        r = [material, kat, birim, 100, 10, 90]
        s1 = sups[0] if len(sups) > 0 else (None, None, None)
        s2 = sups[1] if len(sups) > 1 else (None, None, None)
        s3 = sups[2] if len(sups) > 2 else (None, None, None)
        r += [s1[1], s1[0], s1[2]]   # Ted-1: paket, ad, fiyat
        r += [s2[0], s2[1], s2[2]]   # Ted-2: ad, paket, fiyat
        r += [s3[0], s3[1], s3[2]]   # Ted-3: ad, paket, fiyat
        ws.append(r)
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def _seed_items_suppliers(db):
    for nm in ["ARGAN YAĞI", "COCO BETAİNE%30"]:
        db.add(Item(name=nm, sku="SKU-" + nm[:6], category="Hammadde", unit="g",
                    current_stock=10, domain="cosmetics"))
    for sp in ["ULUDAĞ HERBAL", "NATURALYA", "UMAYCHEM"]:
        db.add(Supplier(name=sp, domain="cosmetics"))
    db.commit()


# ─── parse ──────────────────────────────────────────────────────────────────

def test_parse_layout():
    from core.supplier_prices import parse_stok_son_durum
    data = _make_xlsx([
        ("ARGAN YAĞI", "Hammadde", "g", [("ULUDAĞ HERBAL ", 25, 119.46),
                                         ("SURYA KİMYA ", 25, 22.0),
                                         ("NATURALYA ", 25, 25.8)]),
        ("COCO BETAİNE%30", "Hammadde", "g", [("UMAYCHEM ", 220, 1.37)]),
        ("TUZ", "Hammadde", "kg", []),
    ])
    rows = parse_stok_son_durum(data)
    assert len(rows) == 3
    argan = next(r for r in rows if r["material"] == "ARGAN YAĞI")
    assert [s["name"] for s in argan["suppliers"]] == ["ULUDAĞ HERBAL", "SURYA KİMYA", "NATURALYA"]
    assert argan["suppliers"][0]["package"] == 25 and argan["suppliers"][0]["price"] == 119.46
    coco = next(r for r in rows if r["material"].startswith("COCO"))
    assert len(coco["suppliers"]) == 1 and coco["suppliers"][0]["name"] == "UMAYCHEM"
    tuz = next(r for r in rows if r["material"] == "TUZ")
    assert tuz["suppliers"] == []


# ─── import ─────────────────────────────────────────────────────────────────

def test_import_matches_and_reports_unmatched(db_session):
    from core.supplier_prices import parse_stok_son_durum, import_prices
    _seed_items_suppliers(db_session)
    data = _make_xlsx([
        ("ARGAN YAĞI", "Hammadde", "g", [("NATURALYA", 25, 25.8), ("ULUDAĞ HERBAL", 25, 119.46)]),
        ("COCO BETAİNE%30", "Hammadde", "g", [("UMAYCHEM", 220, 1.37)]),
        ("BİLİNMEYEN MALZEME", "Hammadde", "g", [("GİZEM TEDARİK", 1, 5.0)]),  # eşleşmeyen malzeme
    ])
    rows = parse_stok_son_durum(data)
    summ = import_prices(db_session, rows, "cosmetics")
    assert summ["items_updated"] == 2
    assert summ["prices_inserted"] == 3
    assert "BİLİNMEYEN MALZEME" in summ["unmatched_materials"]
    assert "GİZEM TEDARİK" not in summ["unmatched_suppliers"]  # eşleşmeyen malzeme → satır hiç eklenmedi
    argan = db_session.query(Item).filter(Item.name == "ARGAN YAĞI").first()
    prices = db_session.query(SupplierPrice).filter(SupplierPrice.item_id == argan.id).all()
    assert len(prices) == 2
    assert all(p.supplier_id is not None for p in prices)   # ikisi de eşleşti


def test_import_unmatched_supplier_kept_as_text(db_session):
    from core.supplier_prices import parse_stok_son_durum, import_prices
    _seed_items_suppliers(db_session)
    data = _make_xlsx([("ARGAN YAĞI", "Hammadde", "g", [("YENİ TEDARİKÇİ A.Ş.", 25, 10.0)])])
    summ = import_prices(db_session, parse_stok_son_durum(data), "cosmetics")
    assert "YENİ TEDARİKÇİ A.Ş." in summ["unmatched_suppliers"]
    row = db_session.query(SupplierPrice).first()
    assert row.supplier_id is None and row.supplier_name == "YENİ TEDARİKÇİ A.Ş."


def test_import_replaces_existing(db_session):
    from core.supplier_prices import parse_stok_son_durum, import_prices
    _seed_items_suppliers(db_session)
    import_prices(db_session, parse_stok_son_durum(
        _make_xlsx([("ARGAN YAĞI", "Hammadde", "g", [("NATURALYA", 25, 25.8)])])), "cosmetics")
    # ikinci içe aktarma aynı malzemenin eski satırını DEĞİŞTİRİR
    import_prices(db_session, parse_stok_son_durum(
        _make_xlsx([("ARGAN YAĞI", "Hammadde", "g", [("ULUDAĞ HERBAL", 25, 99.0)])])), "cosmetics")
    rows = db_session.query(SupplierPrice).all()
    assert len(rows) == 1 and rows[0].supplier_name == "ULUDAĞ HERBAL" and rows[0].unit_price == 99.0


# ─── lookup ─────────────────────────────────────────────────────────────────

def test_prices_for_items_cheapest_first(db_session):
    from core.supplier_prices import prices_for_items
    _seed_items_suppliers(db_session)
    argan = db_session.query(Item).filter(Item.name == "ARGAN YAĞI").first()
    db_session.add_all([
        SupplierPrice(item_id=argan.id, supplier_name="PAHALI", unit_price=119.0, package_size=25, domain="cosmetics"),
        SupplierPrice(item_id=argan.id, supplier_name="UCUZ", unit_price=22.0, package_size=25, domain="cosmetics"),
        SupplierPrice(item_id=argan.id, supplier_name="FİYATSIZ", unit_price=None, package_size=1, domain="cosmetics"),
    ])
    db_session.commit()
    out = prices_for_items(db_session, [argan.id], "cosmetics")
    names = [s["supplier_name"] for s in out[argan.id]]
    assert names == ["UCUZ", "PAHALI", "FİYATSIZ"]   # ucuzdan; fiyatsız sona


# ─── rapor (saf fonksiyon) ──────────────────────────────────────────────────

def test_build_workbook_adds_supplier_columns():
    import openpyxl
    from core.production_sim import build_workbook
    rep = {"summary": {"products": 1, "quantity": 250, "total_units": 250, "language": "TR",
                       "materials": 1, "short_count": 1, "ok_count": 0, "total_raw": 1.0},
           "materials": [], "producible": [],
           "purchase": [{"item_id": 7, "name": "ARGAN YAĞI", "category": "Hammadde",
                         "unit": "g", "used": 100, "current": 10, "shortfall": 90}],
           "skipped_labels": []}
    prices = {7: [{"supplier_name": "UCUZ", "package_size": 25, "unit_price": 22.0},
                  {"supplier_name": "PAHALI", "package_size": 25, "unit_price": 119.0}]}
    wb = openpyxl.load_workbook(io.BytesIO(build_workbook(rep, "x", prices=prices)))
    ws = wb["Satın Alma Listesi"]
    headers = [c.value for c in ws[1]]
    assert "TEDARİKÇİ-1" in headers and "TEDARİKÇİ-3" in headers
    row2 = [c.value for c in ws[2]]
    assert "UCUZ" in row2 and 22.0 in row2
    # prices boş → eski sade düzen
    ws2 = openpyxl.load_workbook(io.BytesIO(build_workbook(rep, "x", prices={})))["Satın Alma Listesi"]
    assert "TEDARİKÇİ-1" not in [c.value for c in ws2[1]]


# ─── uçlar ──────────────────────────────────────────────────────────────────

def test_endpoints_import_list_delete(authed_client: TestClient, db_session):
    _seed_items_suppliers(db_session)
    xls = _make_xlsx([("ARGAN YAĞI", "Hammadde", "g", [("NATURALYA", 25, 25.8), ("ULUDAĞ HERBAL", 25, 119.46)])])
    r = authed_client.post("/api/supplier-prices/import",
                           files={"file": ("STOK SON DURUM.xlsx", xls, _MIME)}, headers=_H)
    assert r.status_code == 200, r.text
    assert r.json()["items_updated"] == 1 and r.json()["prices_inserted"] == 2

    lst = authed_client.get("/api/supplier-prices")
    assert lst.status_code == 200
    body = lst.json()
    assert body["total_items"] == 1 and body["total_prices"] == 2
    item = body["items"][0]
    assert item["material"] == "ARGAN YAĞI"
    assert item["suppliers"][0]["supplier_name"] == "NATURALYA"   # en ucuz önce

    pid = item["suppliers"][0]["id"]
    d = authed_client.delete(f"/api/supplier-prices/{pid}", headers=_H)
    assert d.status_code == 200
    assert authed_client.get("/api/supplier-prices").json()["total_prices"] == 1


def test_import_requires_finance_role(labtech_client: TestClient, db_session):
    _seed_items_suppliers(db_session)
    xls = _make_xlsx([("ARGAN YAĞI", "Hammadde", "g", [("NATURALYA", 25, 25.8)])])
    r = labtech_client.post("/api/supplier-prices/import",
                            files={"file": ("x.xlsx", xls, _MIME)}, headers=_H)
    assert r.status_code == 403
    # ama listeyi görebilir (reports.view)
    assert labtech_client.get("/api/supplier-prices").status_code == 200


# ─── fiyat temeli (C1): para birimi + birim + kaynak ────────────────────────

def _seed_unit_items(db):
    """g / ml / adet birimli üç malzeme + bir tedarikçi."""
    db.add_all([
        Item(name="ARGAN YAĞI", sku="SKU-ARG", category="Hammadde", unit="g", current_stock=0, domain="cosmetics"),
        Item(name="GÜL SUYU", sku="SKU-GUL", category="Hammadde", unit="ml", current_stock=0, domain="cosmetics"),
        Item(name="POMPA 24/410", sku="SKU-POM", category="Ambalaj", unit="adet", current_stock=0, domain="cosmetics"),
        Supplier(name="NATURALYA", domain="cosmetics"),
    ])
    db.commit()


def _basis_xlsx():
    return _make_xlsx([
        ("ARGAN YAĞI", "Hammadde", "g", [("NATURALYA", 25, 25.8)]),
        ("GÜL SUYU", "Hammadde", "ml", [("NATURALYA", 30, 4.2)]),
        ("POMPA 24/410", "Ambalaj", "adet", [("NATURALYA", 1000, 0.11)]),
    ])


def _rows_by_item(db):
    out = {}
    for sp in db.query(SupplierPrice).all():
        out[db.get(Item, sp.item_id).name] = sp
    return out


def test_import_endpoint_defaults_usd_kg_and_adet(authed_client: TestClient, db_session):
    _seed_unit_items(db_session)
    r = authed_client.post("/api/supplier-prices/import",
                           files={"file": ("STOK SON DURUM.xlsx", _basis_xlsx(), _MIME)}, headers=_H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["currency"] == "USD" and body["price_unit"] == "kg"
    assert body["source_label"] == "STOK SON DURUM.xlsx"       # etiket boş → dosya adı
    db_session.expire_all()
    rows = _rows_by_item(db_session)
    assert rows["ARGAN YAĞI"].currency == "USD" and rows["ARGAN YAĞI"].price_unit == "kg"
    assert rows["GÜL SUYU"].price_unit == "kg"                 # liste USD/kg; ml kalemi de kg fiyatı
    assert rows["POMPA 24/410"].price_unit == "adet"           # adet birimli → daima adet
    assert all(sp.source == "stok_son_durum" for sp in rows.values())
    assert all(sp.quoted_at is None for sp in rows.values())


def test_import_endpoint_form_sets_currency_unit_label_date(authed_client: TestClient, db_session):
    import datetime as dt
    _seed_unit_items(db_session)
    r = authed_client.post("/api/supplier-prices/import",
                           files={"file": ("x.xlsx", _basis_xlsx(), _MIME)},
                           data={"currency": "eur", "price_unit": "l",
                                 "label": "  Ekim   teklifleri ", "quoted_at": "2026-10-01"},
                           headers=_H)
    assert r.status_code == 200, r.text
    assert r.json()["currency"] == "EUR" and r.json()["quoted_at"] == "2026-10-01"
    db_session.expire_all()
    rows = _rows_by_item(db_session)
    assert rows["GÜL SUYU"].currency == "EUR" and rows["GÜL SUYU"].price_unit == "l"
    assert rows["ARGAN YAĞI"].price_unit == "l"                # kullanıcının seçtiği temel
    assert rows["POMPA 24/410"].price_unit == "adet"
    assert rows["ARGAN YAĞI"].source_label == "Ekim teklifleri"
    assert rows["ARGAN YAĞI"].quoted_at == dt.date(2026, 10, 1)

    lst = authed_client.get("/api/supplier-prices").json()
    by_name = {it["material"]: it["suppliers"][0] for it in lst["items"]}
    assert by_name["GÜL SUYU"]["currency"] == "EUR" and by_name["GÜL SUYU"]["price_unit"] == "l"
    assert by_name["POMPA 24/410"]["price_unit"] == "adet"
    assert by_name["ARGAN YAĞI"]["quoted_at"] == "2026-10-01"
    assert by_name["ARGAN YAĞI"]["source_label"] == "Ekim teklifleri"


def test_import_endpoint_rejects_bad_basis(authed_client: TestClient, db_session):
    _seed_unit_items(db_session)
    for data, frag in (({"currency": "GBP"}, "Para birimi"),
                       ({"price_unit": "ton"}, "Fiyat birimi"),
                       # liste birimi 'adet' olamaz: her g/kg/ml/lt fiyatını
                       # satın alma planında kullanılamaz yapardı
                       ({"price_unit": "adet"}, "Fiyat birimi"),
                       ({"quoted_at": "01.10.2026"}, "tarih"),
                       ({"label": "x" * 121}, "Etiket")):
        r = authed_client.post("/api/supplier-prices/import",
                               files={"file": ("x.xlsx", _basis_xlsx(), _MIME)}, data=data, headers=_H)
        assert r.status_code == 400, (data, r.text)
        assert frag in r.json()["detail"]
    assert db_session.query(SupplierPrice).count() == 0       # hiçbiri yazılmadı


def test_import_endpoint_writes_audit_row(authed_client: TestClient, db_session):
    import json
    from database import AdminAuditLog
    _seed_unit_items(db_session)
    r = authed_client.post("/api/supplier-prices/import",
                           files={"file": ("liste.xlsx", _basis_xlsx(), _MIME)},
                           data={"currency": "USD", "price_unit": "kg", "label": "Eylül"}, headers=_H)
    assert r.status_code == 200, r.text
    logs = db_session.query(AdminAuditLog).filter(AdminAuditLog.action == "supplier_prices.import").all()
    assert len(logs) == 1
    det = json.loads(logs[0].details)
    assert logs[0].target_name == "Eylül"
    assert det["currency"] == "USD" and det["price_unit"] == "kg"
    assert det["items_updated"] == 3 and det["prices_inserted"] == 3
    assert det["filename"] == "liste.xlsx" and det["domain"] == "cosmetics"


def test_import_prices_core_kwargs_and_validation(db_session):
    import datetime as dt
    import pytest
    from core.supplier_prices import parse_stok_son_durum, import_prices, prices_for_items
    _seed_unit_items(db_session)
    rows = parse_stok_son_durum(_basis_xlsx())
    summ = import_prices(db_session, rows, "cosmetics", currency="TRY", price_unit="kg",
                         source_label="Elle", quoted_at=dt.date(2026, 9, 30))
    assert summ["prices_inserted"] == 3
    pump = db_session.query(Item).filter(Item.name == "POMPA 24/410").first()
    argan = db_session.query(Item).filter(Item.name == "ARGAN YAĞI").first()
    out = prices_for_items(db_session, [pump.id, argan.id], "cosmetics")
    assert out[pump.id][0]["price_unit"] == "adet" and out[pump.id][0]["currency"] == "TRY"
    assert out[argan.id][0]["price_unit"] == "kg"
    assert out[argan.id][0]["source_label"] == "Elle"
    assert out[argan.id][0]["quoted_at"] == dt.date(2026, 9, 30)
    with pytest.raises(ValueError):
        import_prices(db_session, rows, "cosmetics", currency="GBP")
    with pytest.raises(ValueError):
        import_prices(db_session, rows, "cosmetics", price_unit="ton")
    with pytest.raises(ValueError):
        import_prices(db_session, rows, "cosmetics", price_unit="adet")


# ─── tek seferlik geri doldurma (sentinel'li) ───────────────────────────────

def test_backfill_supplier_price_units_runs_once(db_session):
    from database import AppSetting, _backfill_supplier_price_units
    SENT = "backfill.supplier_price_units.v1"
    _seed_unit_items(db_session)
    # init_db (conftest) boş tabloda sentinel'i zaten yazdı → eski DB'yi taklit et
    db_session.query(AppSetting).filter(AppSetting.key == SENT).delete()
    items = {it.name: it for it in db_session.query(Item).all()}
    db_session.add_all([   # eski satırlar: model varsayılanı TRY, birimsiz, kaynaksız
        SupplierPrice(item_id=items["ARGAN YAĞI"].id, supplier_name="A", unit_price=25.8, domain="cosmetics"),
        SupplierPrice(item_id=items["GÜL SUYU"].id, supplier_name="B", unit_price=4.2, domain="cosmetics"),
        SupplierPrice(item_id=items["POMPA 24/410"].id, supplier_name="C", unit_price=0.11, domain="cosmetics"),
        # zaten temeli olan satıra DOKUNULMAZ
        SupplierPrice(item_id=items["ARGAN YAĞI"].id, supplier_name="D", unit_price=30.0, currency="EUR",
                      price_unit="l", source="manual", domain="cosmetics"),
    ])
    db_session.commit()
    assert db_session.query(SupplierPrice).filter(SupplierPrice.supplier_name == "A").one().currency == "TRY"

    _backfill_supplier_price_units()
    db_session.expire_all()
    got = {sp.supplier_name: sp for sp in db_session.query(SupplierPrice).all()}
    assert (got["A"].currency, got["A"].price_unit, got["A"].source) == ("USD", "kg", "stok_son_durum")
    assert (got["B"].currency, got["B"].price_unit) == ("USD", "kg")
    assert (got["C"].currency, got["C"].price_unit) == ("USD", "adet")
    assert (got["D"].currency, got["D"].price_unit, got["D"].source) == ("EUR", "l", "manual")
    sent = db_session.query(AppSetting).filter(AppSetting.key == SENT).one()
    assert sent.value == "3"

    # İkinci çağrı no-op: sentinel var → yeni birimsiz satır olduğu gibi kalır
    db_session.add(SupplierPrice(item_id=items["ARGAN YAĞI"].id, supplier_name="E",
                                 unit_price=1.0, domain="cosmetics"))
    db_session.commit()
    _backfill_supplier_price_units()
    db_session.expire_all()
    e = db_session.query(SupplierPrice).filter(SupplierPrice.supplier_name == "E").one()
    assert e.price_unit is None and e.currency == "TRY"


# ─── fiyatın alım birimine çevrilmesi ───────────────────────────────────────

def test_price_per_purchase_unit_cases():
    from core.supplier_prices import price_per_purchase_unit as ppu, LITRE_KG_NOTE
    kg = {"unit_price": 66.0, "price_unit": "kg"}
    lt = {"unit_price": 5.0, "price_unit": "l"}
    ad = {"unit_price": 0.11, "price_unit": "adet"}
    # g/kg kalem → kg temeli
    assert ppu(kg, "g") == (66.0, "kg", None)
    assert ppu(kg, "kg") == (66.0, "kg", None)
    assert ppu(kg, " KG ") == (66.0, "kg", None)
    # ml/l kalem → l temeli; kg fiyatı uygulanınca not düşülür
    assert ppu(kg, "ml") == (66.0, "l", LITRE_KG_NOTE)
    assert LITRE_KG_NOTE == "1 l ≈ 1 kg kabulüyle"
    assert ppu(lt, "ml") == (5.0, "l", None)
    assert ppu(lt, "lt") == (5.0, "l", None)
    assert ppu(lt, "g") == (5.0, "kg", LITRE_KG_NOTE)          # simetrik
    # adet
    assert ppu(ad, "adet") == (0.11, "adet", None)
    assert ppu(ad, "kutu") == (0.11, "adet", None)             # sayılan birim
    # çevrilemeyen temel → fiyat kullanılmaz, not açıklar
    price, unit, note = ppu(kg, "adet")
    assert price is None and unit == "adet" and "uyuşmuyor" in note
    price, unit, note = ppu(ad, "g")
    assert price is None and unit == "kg" and "uyuşmuyor" in note
    # fiyatsız teklif
    assert ppu({"unit_price": None, "price_unit": "kg"}, "g") == (None, "kg", None)
    # birimsiz eski satır → varsayılan kural (adet kalemde adet, diğerinde kg)
    assert ppu({"unit_price": 2.0, "price_unit": None}, "adet") == (2.0, "adet", None)
    assert ppu({"unit_price": 2.0}, "ml") == (2.0, "l", LITRE_KG_NOTE)
    # SupplierPrice nesnesi de kabul edilir
    assert ppu(SupplierPrice(unit_price=7.5, price_unit="kg"), "g") == (7.5, "kg", None)


# ─── Elle fiyat (lab — suppliers.prices, 07.10.2026) ────────────────────────

import json  # noqa: E402

from database import AdminAuditLog  # noqa: E402


def _login(c: TestClient, username: str) -> TestClient:
    c.post("/api/logout", headers=_H)
    r = c.post("/api/login", json={"username": username, "password": "minerva123"}, headers=_H)
    assert r.status_code == 200, r.text
    return c


def _ids(db):
    db.expire_all()
    items = {i.name: i.id for i in db.query(Item).all()}
    sups = {s.name: s.id for s in db.query(Supplier).all()}
    return items, sups


def _audit_actions(db):
    db.expire_all()
    return [a.action for a in db.query(AdminAuditLog).order_by(AdminAuditLog.id).all()]


def test_lablead_manual_price_crud(client: TestClient, db_session):
    _seed_unit_items(db_session)
    items, sups = _ids(db_session)
    _login(client, "songul")                                   # LabLead
    r = client.post("/api/supplier-prices", headers=_H, json={
        "item_id": items["ARGAN YAĞI"], "supplier_id": sups["NATURALYA"], "unit_price": 24.5,
        "currency": "eur", "price_unit": "kg", "package_size": 25, "quoted_at": "2026-10-07",
        "note": "telefonla teyit"})
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["source"] == "manual" and row["created_by"] and row["currency"] == "EUR"
    assert row["material"] == "ARGAN YAĞI" and row["quoted_at"] == "2026-10-07"
    pid = row["id"]
    r = client.put(f"/api/supplier-prices/{pid}", headers=_H, json={"unit_price": 23.0})
    assert r.status_code == 200, r.text
    assert r.json()["unit_price"] == 23.0 and r.json()["package_size"] == 25   # kısmi
    assert r.json()["updated_by"]
    lst = client.get("/api/supplier-prices").json()
    s = lst["items"][0]["suppliers"][0]
    assert s["source"] == "manual" and s["note"] == "telefonla teyit" and s["supplier_status"] == "normal"
    assert client.delete(f"/api/supplier-prices/{pid}", headers=_H).status_code == 200
    acts = _audit_actions(db_session)
    for a in ("supplier_prices.create", "supplier_prices.update", "supplier_prices.delete"):
        assert a in acts
    db_session.expire_all()
    dl = db_session.query(AdminAuditLog).filter(AdminAuditLog.action == "supplier_prices.delete").one()
    d = json.loads(dl.details)
    assert d["unit_price"] == 23.0 and d["supplier_name"] == "NATURALYA" and d["source"] == "manual"


def test_labtech_cannot_write_prices(client: TestClient, db_session):
    _seed_unit_items(db_session)
    items, sups = _ids(db_session)
    sp = SupplierPrice(item_id=items["ARGAN YAĞI"], supplier_id=sups["NATURALYA"],
                       supplier_name="NATURALYA", unit_price=1, price_unit="kg", domain="cosmetics")
    db_session.add(sp); db_session.commit()
    _login(client, "meltem")                                   # LabTech
    assert client.post("/api/supplier-prices", headers=_H, json={
        "item_id": items["ARGAN YAĞI"], "supplier_id": sups["NATURALYA"], "unit_price": 2,
        "price_unit": "kg"}).status_code == 403
    assert client.put(f"/api/supplier-prices/{sp.id}", headers=_H,
                      json={"unit_price": 3}).status_code == 403
    assert client.delete(f"/api/supplier-prices/{sp.id}", headers=_H).status_code == 403


def test_manual_price_validation(authed_client: TestClient, db_session):
    _seed_unit_items(db_session)
    items, sups = _ids(db_session)
    base = {"supplier_id": sups["NATURALYA"], "unit_price": 1.5}
    # Birim ailesi: adetli kart → yalnız adet; g kart → kg|l
    r = authed_client.post("/api/supplier-prices", headers=_H,
                           json={**base, "item_id": items["POMPA 24/410"], "price_unit": "kg"})
    assert r.status_code == 400
    r = authed_client.post("/api/supplier-prices", headers=_H,
                           json={**base, "item_id": items["ARGAN YAĞI"], "price_unit": "adet"})
    assert r.status_code == 400
    for bad in ({"unit_price": 0}, {"unit_price": -1}, {"currency": "GBP"},
                {"quoted_at": "07.10.2026"}, {"package_size": 0}):
        r = authed_client.post("/api/supplier-prices", headers=_H,
                               json={**base, "item_id": items["ARGAN YAĞI"], "price_unit": "kg", **bad})
        assert r.status_code == 400, bad
    r = authed_client.post("/api/supplier-prices", headers=_H,
                           json={**base, "item_id": items["POMPA 24/410"], "price_unit": "adet"})
    assert r.status_code == 201
    # Mükerrer (malzeme + tedarikçi + birim) → 409 + mevcut id
    r2 = authed_client.post("/api/supplier-prices", headers=_H,
                            json={**base, "item_id": items["POMPA 24/410"], "price_unit": "adet"})
    assert r2.status_code == 409
    assert r2.json()["code"] == "price_exists" and r2.json()["id"] == r.json()["id"]
    # Pasif tedarikçiye fiyat girilmez
    authed_client.delete(f"/api/suppliers/{sups['NATURALYA']}", headers=_H)
    r = authed_client.post("/api/supplier-prices", headers=_H,
                           json={**base, "item_id": items["GÜL SUYU"], "price_unit": "l"})
    assert r.status_code == 400


def test_manual_price_other_domain_rejected(authed_client: TestClient, db_session):
    _seed_unit_items(db_session)
    items, sups = _ids(db_session)
    authed_client.post("/api/domain/switch", json={"domain": "supplement"}, headers=_H)
    r = authed_client.post("/api/supplier-prices", headers=_H, json={
        "item_id": items["ARGAN YAĞI"], "supplier_id": sups["NATURALYA"], "unit_price": 2,
        "price_unit": "kg"})
    assert r.status_code == 404


def test_import_keeps_manual_rows(authed_client: TestClient, db_session):
    _seed_items_suppliers(db_session)
    items, sups = _ids(db_session)
    r = authed_client.post("/api/supplier-prices", headers=_H, json={
        "item_id": items["ARGAN YAĞI"], "supplier_id": sups["NATURALYA"], "unit_price": 30,
        "price_unit": "kg"})
    assert r.status_code == 201
    manual_id = r.json()["id"]
    xls = _make_xlsx([("ARGAN YAĞI", "Hammadde", "g", [("NATURALYA", 25, 25.8),
                                                     ("ULUDAĞ HERBAL", 25, 119.46)])])
    r = authed_client.post("/api/supplier-prices/import",
                           files={"file": ("x.xlsx", xls, _MIME)}, headers=_H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kept_manual"] == 1 and body["skipped_manual"] == 1
    db_session.expire_all()
    rows = db_session.query(SupplierPrice).all()
    assert len(rows) == 2                                        # elle NATURALYA + Excel ULUDAĞ
    m = db_session.get(SupplierPrice, manual_id)
    assert m is not None and m.unit_price == 30 and m.source == "manual"
    # Tekrar içe aktarma da elle satıra dokunmaz
    authed_client.post("/api/supplier-prices/import",
                       files={"file": ("x.xlsx", xls, _MIME)}, headers=_H)
    db_session.expire_all()
    assert db_session.get(SupplierPrice, manual_id).unit_price == 30


def test_edited_import_row_becomes_manual_and_survives(authed_client: TestClient, db_session):
    _seed_items_suppliers(db_session)
    xls = _make_xlsx([("ARGAN YAĞI", "Hammadde", "g", [("NATURALYA", 25, 25.8)])])
    authed_client.post("/api/supplier-prices/import",
                       files={"file": ("x.xlsx", xls, _MIME)}, headers=_H)
    db_session.expire_all()
    pid = db_session.query(SupplierPrice).one().id
    r = authed_client.put(f"/api/supplier-prices/{pid}", headers=_H, json={"unit_price": 21.0})
    assert r.status_code == 200 and r.json()["source"] == "manual"
    authed_client.post("/api/supplier-prices/import",
                       files={"file": ("x.xlsx", xls, _MIME)}, headers=_H)
    db_session.expire_all()
    rows = db_session.query(SupplierPrice).all()
    assert len(rows) == 1 and rows[0].id == pid and rows[0].unit_price == 21.0


def test_import_matches_only_active_suppliers(db_session):
    from core.supplier_prices import parse_stok_son_durum, import_prices
    db_session.add(Item(name="ARGAN YAĞI", category="Hammadde", unit="g", current_stock=0,
                        domain="cosmetics"))
    old = Supplier(name="TATLIDİLİMLER", domain="cosmetics", is_active=False)
    db_session.add(old); db_session.commit()
    new = Supplier(name="TATLİDİLİMLER", domain="cosmetics")
    db_session.add(new); db_session.commit()
    import_prices(db_session, parse_stok_son_durum(
        _make_xlsx([("ARGAN YAĞI", "Hammadde", "g", [("Tatlıdilimler", 1, 9.0)])])), "cosmetics")
    assert db_session.query(SupplierPrice).one().supplier_id == new.id


def test_prices_for_items_manual_hides_same_firm_import(db_session):
    from core.supplier_prices import prices_for_items
    _seed_items_suppliers(db_session)
    items, sups = _ids(db_session)
    iid = items["ARGAN YAĞI"]
    db_session.add_all([
        SupplierPrice(item_id=iid, supplier_id=sups["NATURALYA"], supplier_name="NATURALYA",
                      unit_price=25.8, source="stok_son_durum", domain="cosmetics"),
        SupplierPrice(item_id=iid, supplier_id=sups["NATURALYA"], supplier_name="NATURALYA",
                      unit_price=30.0, source="manual", domain="cosmetics"),
        SupplierPrice(item_id=iid, supplier_id=sups["UMAYCHEM"], supplier_name="UMAYCHEM",
                      unit_price=40.0, source="stok_son_durum", domain="cosmetics"),
    ])
    db_session.commit()
    out = prices_for_items(db_session, [iid], "cosmetics")
    assert [(s["supplier_name"], s["unit_price"]) for s in out[iid]] == \
        [("NATURALYA", 30.0), ("UMAYCHEM", 40.0)]


def test_price_unit_ok_rules():
    from core.supplier_prices import price_unit_ok
    assert price_unit_ok("adet", "adet") and not price_unit_ok("adet", "kg")
    assert price_unit_ok("kutu", "adet")
    assert price_unit_ok("g", "kg") and price_unit_ok("g", "l") and not price_unit_ok("g", "adet")
    assert price_unit_ok("ml", "l") and price_unit_ok("lt", "kg")


# ─── Pasif tedarikçi + düzenlenen Excel satırının etiketi (08.10.2026) ──────
# Yumuşak silme (P1a) sonrası pasif firmanın fiyatı en ucuz seçilmeye devam
# ediyordu: Excel'lerde Tedarikçi-1, panelde yeşil "en ucuz".

def _inactive_case(db):
    """Jojoba: Eski Firma (pasif) 5 $, Yeni Firma 9 $, TATLİDİLİMLER kopyası
    pasif ama TATLIDİLİMLER aktif (aynı firma) 7 $, kartsız serbest metin
    "Kapanan kimya" 4 $ (adı yalnız pasif KAPANAN KİMYA kartında)."""
    it = Item(name="JOJOBA", category="Hammadde", unit="g", domain="cosmetics")
    old = Supplier(name="ESKİ FİRMA", domain="cosmetics", is_active=False)
    new = Supplier(name="YENİ FİRMA", domain="cosmetics")
    dup = Supplier(name="TATLİDİLİMLER", domain="cosmetics", is_active=False)
    twin = Supplier(name="TATLIDİLİMLER", domain="cosmetics")
    gone = Supplier(name="KAPANAN KİMYA", domain="cosmetics", is_active=False)
    db.add_all([it, old, new, dup, twin, gone])
    db.flush()
    db.add_all([
        SupplierPrice(item_id=it.id, supplier_id=old.id, supplier_name="ESKİ FİRMA", unit_price=5.0,
                      currency="USD", price_unit="kg", source="manual", domain="cosmetics"),
        SupplierPrice(item_id=it.id, supplier_id=new.id, supplier_name="YENİ FİRMA", unit_price=9.0,
                      currency="USD", price_unit="kg", source="manual", domain="cosmetics"),
        SupplierPrice(item_id=it.id, supplier_id=dup.id, supplier_name="TATLİDİLİMLER", unit_price=7.0,
                      currency="USD", price_unit="kg", source="stok_son_durum", domain="cosmetics"),
        SupplierPrice(item_id=it.id, supplier_id=None, supplier_name="Kapanan kimya", unit_price=4.0,
                      currency="USD", price_unit="kg", source="stok_son_durum", domain="cosmetics"),
    ])
    db.commit()
    return it.id


def test_prices_for_items_inactive_firm_last_and_flagged(db_session):
    from core.supplier_prices import prices_for_items
    iid = _inactive_case(db_session)
    out = prices_for_items(db_session, [iid], "cosmetics", limit=5)
    assert [(s["supplier_name"], s["unit_price"], s["inactive"]) for s in out[iid]] == [
        ("TATLİDİLİMLER", 7.0, False),          # pasif kopya kart, aktif ikiz → geçerli
        ("YENİ FİRMA", 9.0, False),
        ("Kapanan kimya", 4.0, True),           # serbest metin, adı yalnız pasif kartta
        ("ESKİ FİRMA", 5.0, True)]
    assert [s["supplier_name"] for s in prices_for_items(db_session, [iid], "cosmetics")[iid]][:2] == \
        ["TATLİDİLİMLER", "YENİ FİRMA"]


def test_supplier_price_panel_marks_inactive_firm(authed_client: TestClient, db_session):
    iid = _inactive_case(db_session)
    r = authed_client.get("/api/supplier-prices")
    assert r.status_code == 200
    g = next(x for x in r.json()["items"] if x["item_id"] == iid)
    assert [(s["supplier_name"], s["supplier_inactive"]) for s in g["suppliers"]] == [
        ("TATLİDİLİMLER", False), ("YENİ FİRMA", False), ("Kapanan kimya", True), ("ESKİ FİRMA", True)]


def test_excel_inactive_supplier_grey_and_labelled():
    import openpyxl
    from core.production_sim import build_workbook
    rep = {"summary": {"products": 1, "quantity": 1, "total_units": 1, "language": "TR",
                       "materials": 1, "short_count": 1, "ok_count": 0, "total_raw": 1.0},
           "materials": [], "producible": [],
           "purchase": [{"item_id": 7, "name": "JOJOBA", "category": "Hammadde",
                         "unit": "g", "used": 100, "current": 10, "shortfall": 90}],
           "skipped_labels": []}
    prices = {7: [{"supplier_name": "ESKİ FİRMA", "package_size": 25, "unit_price": 5.0, "inactive": True}]}
    ws = openpyxl.load_workbook(io.BytesIO(build_workbook(rep, "x", prices=prices)))["Satın Alma Listesi"]
    c = ws.cell(2, 7)
    assert c.value == "ESKİ FİRMA (pasif tedarikçi)"
    assert not c.font.bold and c.font.color.rgb.endswith("9CA3AF")


def test_edited_import_row_drops_stale_list_label(authed_client: TestClient, db_session):
    """Excel satırı elle düzenlenince eski liste etiketi düşer — satın alma
    planı elle fiyatı "<dosya adı>" kaynağıyla yazmasın; etiket gönderilirse o."""
    _seed_items_suppliers(db_session)
    xls = _make_xlsx([("ARGAN YAĞI", "Hammadde", "g", [("NATURALYA", 25, 25.8)])])
    authed_client.post("/api/supplier-prices/import", files={"file": ("liste.xlsx", xls, _MIME)}, headers=_H)
    db_session.expire_all()
    sp = db_session.query(SupplierPrice).one()
    assert sp.source_label == "liste.xlsx"
    r = authed_client.put(f"/api/supplier-prices/{sp.id}", headers=_H, json={"unit_price": 21.0})
    assert r.status_code == 200
    assert (r.json()["source"], r.json()["source_label"]) == ("manual", None)
    r = authed_client.put(f"/api/supplier-prices/{sp.id}", headers=_H,
                          json={"unit_price": 22.0, "source_label": "Telefon teklifi"})
    assert r.json()["source_label"] == "Telefon teklifi"
    r = authed_client.put(f"/api/supplier-prices/{sp.id}", headers=_H, json={"unit_price": 23.0})
    assert r.json()["source_label"] == "Telefon teklifi"          # zaten elle: etiketine dokunulmaz
