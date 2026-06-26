"""
Minerva Drive — Quick Look önizleme (indirmeden).

  • metin / xlsx-tablo → meta'da içerik gömülü; ham dosya gitmez
  • foto / PDF → /preview/raw inline + güvenlik başlıkları (nosniff, inline)
  • güvenli olmayan tür /preview/raw → 415 (XSS guard korunur)
  • önizleme uçları login ister
"""
import base64
import io

from fastapi.testclient import TestClient
from openpyxl import Workbook

from api_main import app

_H = {"Origin": "http://testserver"}

# 1x1 şeffaf PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _up(ac, name, content, ctype="application/octet-stream"):
    r = ac.post("/api/drive/upload", files={"file": (name, content, ctype)}, headers=_H)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _xlsx_bytes():
    wb = Workbook(); ws = wb.active
    ws.append(["Ürün", "Adet"]); ws.append(["Argan", 5]); ws.append(["Lavanta", 3])
    b = io.BytesIO(); wb.save(b); return b.getvalue()


def test_preview_text_inline_in_meta(authed_client: TestClient):
    fid = _up(authed_client, "notlar.txt", "merhaba\nikinci satır".encode("utf-8"), "text/plain")
    d = authed_client.get(f"/api/drive/files/{fid}/preview/meta").json()
    assert d["mode"] == "text" and "merhaba" in d["content"]
    # metin ham olarak inline SERVE EDİLMEZ (XSS guard) → 415
    assert authed_client.get(f"/api/drive/files/{fid}/preview/raw").status_code == 415


def test_preview_xlsx_returns_table_rows(authed_client: TestClient):
    fid = _up(authed_client, "liste.xlsx", _xlsx_bytes(),
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    d = authed_client.get(f"/api/drive/files/{fid}/preview/meta").json()
    assert d["mode"] == "table"
    assert d["rows"][0] == ["Ürün", "Adet"]
    assert ["Argan", "5"] in d["rows"]
    # xlsx de ham inline değil
    assert authed_client.get(f"/api/drive/files/{fid}/preview/raw").status_code == 415


def test_preview_image_raw_has_safe_headers(authed_client: TestClient):
    fid = _up(authed_client, "kare.png", _PNG, "image/png")
    meta = authed_client.get(f"/api/drive/files/{fid}/preview/meta").json()
    assert meta["mode"] == "image" and meta["raw_url"].endswith("/preview/raw")
    r = authed_client.get(f"/api/drive/files/{fid}/preview/raw")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers["content-disposition"].startswith("inline")


def test_preview_pdf_raw_inline(authed_client: TestClient):
    fid = _up(authed_client, "belge.pdf", b"%PDF-1.4\n%minimal\n", "application/pdf")
    meta = authed_client.get(f"/api/drive/files/{fid}/preview/meta").json()
    assert meta["mode"] == "pdf"
    r = authed_client.get(f"/api/drive/files/{fid}/preview/raw")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.headers.get("x-content-type-options") == "nosniff"


def test_preview_unknown_type_is_none(authed_client: TestClient):
    fid = _up(authed_client, "arsiv.zip", b"PK\x03\x04zipdata", "application/zip")
    d = authed_client.get(f"/api/drive/files/{fid}/preview/meta").json()
    assert d["mode"] == "none"
    assert authed_client.get(f"/api/drive/files/{fid}/preview/raw").status_code == 415


def test_preview_requires_login(authed_client: TestClient):
    fid = _up(authed_client, "gizli.txt", b"x", "text/plain")
    pub = TestClient(app)                       # auth YOK (taze jar)
    assert pub.get(f"/api/drive/files/{fid}/preview/meta").status_code in (401, 403)
    assert pub.get(f"/api/drive/files/{fid}/preview/raw").status_code in (401, 403)
