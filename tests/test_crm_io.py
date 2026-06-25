"""
CRM Faz C1 — Excel/CSV içe/dışa aktarma testleri.

  • export: firma/kişi/fırsat → xlsx (başlık + veri)
  • import: xlsx'ten firma oluştur (source='import'); kişiyi firmaya eşle
  • şablon indir; RBAC (import crm.create ister)
"""
import io

from openpyxl import Workbook, load_workbook
from fastapi.testclient import TestClient

_H = {"Origin": "http://testserver"}
_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(headers, rows) -> bytes:
    wb = Workbook(); ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    b = io.BytesIO(); wb.save(b)
    return b.getvalue()


def test_export_companies_xlsx(authed_client: TestClient):
    authed_client.post("/api/crm/companies", json={"name": "Export Co", "city": "İzmir"}, headers=_H)
    r = authed_client.get("/api/crm/export?entity=companies")
    assert r.status_code == 200
    assert "spreadsheet" in r.headers["content-type"]
    ws = load_workbook(io.BytesIO(r.content)).active
    headers = [c.value for c in ws[1]]
    assert "Firma" in headers and "Kaynak" in headers
    names = [ws.cell(row=i, column=1).value for i in range(2, ws.max_row + 1)]
    assert "Export Co" in names


def test_import_companies_creates_rows(authed_client: TestClient):
    data = _xlsx(["Firma", "Telefon", "Şehir"],
                 [["İçe Firma A", "0555 111 22 33", "Bursa"], ["İçe Firma B", "", "Ankara"]])
    r = authed_client.post("/api/crm/import", data={"entity": "companies"},
                           files={"file": ("c.xlsx", data, _MIME)}, headers=_H)
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 2
    rows = authed_client.get("/api/crm/companies?source=manual").json()
    imported = [c for c in rows if c["name"].startswith("İçe Firma")]
    assert len(imported) == 2
    assert all(c["source"] == "import" for c in imported)


def test_import_contacts_links_company(authed_client: TestClient):
    co = authed_client.post("/api/crm/companies", json={"name": "Bağlı Firma"}, headers=_H).json()
    data = _xlsx(["Ad Soyad", "Firma", "Telefon"], [["Veli İçe", "Bağlı Firma", "0500 000 00 00"]])
    r = authed_client.post("/api/crm/import", data={"entity": "contacts"},
                           files={"file": ("k.xlsx", data, _MIME)}, headers=_H)
    assert r.status_code == 200 and r.json()["created"] == 1
    ct = authed_client.get("/api/crm/contacts?q=Veli").json()
    assert ct and ct[0]["company_id"] == co["id"]


def test_import_csv_companies(authed_client: TestClient):
    csv_bytes = "Firma;Şehir\nCSV Firma;Konya\n".encode("utf-8")
    r = authed_client.post("/api/crm/import", data={"entity": "companies"},
                           files={"file": ("c.csv", csv_bytes, "text/csv")}, headers=_H)
    assert r.status_code == 200 and r.json()["created"] == 1


def test_import_unknown_headers_rejected(authed_client: TestClient):
    data = _xlsx(["Kolon1", "Kolon2"], [["a", "b"]])
    r = authed_client.post("/api/crm/import", data={"entity": "companies"},
                           files={"file": ("x.xlsx", data, _MIME)}, headers=_H)
    assert r.status_code == 400


def test_import_requires_create(labtech_client: TestClient):
    data = _xlsx(["Firma"], [["X"]])
    r = labtech_client.post("/api/crm/import", data={"entity": "companies"},
                            files={"file": ("c.xlsx", data, _MIME)}, headers=_H)
    assert r.status_code == 403


def test_template_download(authed_client: TestClient):
    r = authed_client.get("/api/crm/import/template?entity=contacts")
    assert r.status_code == 200 and "spreadsheet" in r.headers["content-type"]
    ws = load_workbook(io.BytesIO(r.content)).active
    headers = [c.value for c in ws[1]]
    assert "Ad Soyad" in headers
