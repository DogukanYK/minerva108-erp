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
