"""
Türkçe ad toplu içe aktarma — /api/items/import-names.

İki format desteklenir:
  • Barkod formatı (fuar listeleri): BARCODE | BRAND | PRODUCT NAME (TR) | NOMINAL QUANTITY
    → ürünü BARKOD ile eşleştirir; PRODUCT NAME Türkçe addır; ölçü ada eklenir.
  • SKU/İngilizce-ad formatı: kod/ad → 'Türkçe Ad' kolonu.
RBAC: items.import gerektirir.
"""
import io

from openpyxl import Workbook
from fastapi.testclient import TestClient

from database import Item

_H = {"Origin": "http://testserver"}
_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(headers, rows) -> bytes:
    wb = Workbook(); ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    b = io.BytesIO(); wb.save(b)
    return b.getvalue()


def _mk_item(db, name, barcode, sku, domain="minerva"):
    it = Item(name=name, sku=sku, category="Bitmiş Ürün", unit="adet",
              current_stock=0, barcode=barcode, is_active=True, domain=domain)
    db.add(it); db.commit(); db.refresh(it)
    return it


def test_import_names_by_barcode_appends_size(authed_client: TestClient, db_session):
    a = _mk_item(db_session, "Evanira Anti-Dark Spot Cream 100ml", "8690000000011", "ev-100")
    b = _mk_item(db_session, "Evanira Anti-Dark Spot Cream 500ml", "8690000000028", "ev-500")
    data = _xlsx(
        ["BARCODE", "BRAND NAME", "PRODUCT NAME", "NOMINAL QUANTITY", "UST OF CONTENTS"],
        [
            [8690000000011, "EVANIRA", "KIRMIZI YONCA LEKE KARŞITI KREM", "100 ML", "Aqua..."],
            [8690000000028, "EVANIRA", "KIRMIZI YONCA LEKE KARŞITI KREM", "500 ML", "Aqua..."],
            [9999999999999, "EVANIRA", "SİSTEMDE OLMAYAN ÜRÜN", "200 ML", "Aqua..."],
        ],
    )
    r = authed_client.post("/api/items/import-names",
                           files={"file": ("EVANIRA_TR.xlsx", data, _MIME)}, headers=_H)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["match_mode"] == "barcode"
    assert out["updated"] == 2
    assert out["unmatched_count"] == 1
    db_session.refresh(a); db_session.refresh(b)
    # ölçü ada eklenir → aynı ürün adının varyantları ayrışır
    assert a.name_tr == "KIRMIZI YONCA LEKE KARŞITI KREM 100 ML"
    assert b.name_tr == "KIRMIZI YONCA LEKE KARŞITI KREM 500 ML"


def test_import_names_by_sku_and_english(authed_client: TestClient, db_session):
    it = _mk_item(db_session, "Serenida Shower Gel", "8690000000035", "sg-1")
    # SKU + açık 'Türkçe Ad' kolonu
    data = _xlsx(["SKU", "Türkçe Ad"], [["sg-1", "DUŞ JELİ"], ["yok-sku", "EŞLEŞMEZ"]])
    r = authed_client.post("/api/items/import-names",
                           files={"file": ("tr.xlsx", data, _MIME)}, headers=_H)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["match_mode"] == "ident" and out["updated"] == 1 and out["unmatched_count"] == 1
    db_session.refresh(it)
    assert it.name_tr == "DUŞ JELİ"


def test_import_names_requires_permission(labtech_client: TestClient):
    data = _xlsx(["BARCODE", "PRODUCT NAME"], [[123, "X"]])
    r = labtech_client.post("/api/items/import-names",
                            files={"file": ("x.xlsx", data, _MIME)}, headers=_H)
    assert r.status_code == 403
