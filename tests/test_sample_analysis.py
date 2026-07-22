"""
Numune Analiz Formu (FR.KK.01) testleri.

  • Sayfa: render + login redirect + qc.view yeterliliği
  • CRUD: create (NA- belge no) / list / detail / PUT / soft delete
  • RBAC: qc.approve olmayan (LabTech) yazamaz, okuyabilir; anonim reddedilir
  • Domain izolasyonu: panel değişince kayıtlar görünmez
  • Reçete snapshot'ı: recipe_name reçete silinse de belgede kalır
  • PDF: antetli, tüm sayfalar A4, içerik doğru
  • Audit: create audit satırı yazılır
"""
from io import BytesIO

from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy.orm import Session

from database import AdminAuditLog, Item, Recipe, RecipeIngredient, SampleAnalysis

_HDR = {"Origin": "http://testserver"}
_API = "/api/sample-analysis"
_A4_W, _A4_H = 595.276, 841.89


def _all_pages_a4(pdf_bytes: bytes) -> bool:
    pages = PdfReader(BytesIO(pdf_bytes)).pages
    assert len(pages) >= 1
    for pg in pages:
        b = pg.mediabox
        if not (abs(float(b.width) - _A4_W) < 2 and abs(float(b.height) - _A4_H) < 2):
            return False
    return True


def _payload(**over):
    p = {
        "bulk_name": "Saç Kremi Deneme Bulk",
        "production_date": "2026-07-18",
        "lot_number": "LOT-2407-A",
        "formulation_notes": "metil selüloz %3→%1, xanthan %1.7→%0.5",
        "properties": [
            {"key": "gorunum", "label": "GÖRÜNÜM", "spec": "Homojen krem", "found": "Homojen"},
            {"key": "koku", "label": "KOKU", "spec": "KARAKTERİSTİK", "found": "Uygun"},
            {"key": "renk", "label": "RENK", "spec": "Beyaz", "found": "Beyaz"},
            {"key": "ph", "label": "PH", "spec": "4.5-5.5", "found": "5.1"},
        ],
        "analyst_name": "Meltem Kaya",
        "result": "uygun_degil",
        "result_text": "Viskozitesi çok yoğun olduğundan onaylanmadı.",
        "notes": "Sonraki denemede xanthan %0.7.",
        "approved_by": "Işık Durak",
        "qa_representative": "Songül Akgül",
    }
    p.update(over)
    return p


def _recipe(db, name="MINERVA Test Krem 200ML"):
    tgt = Item(name=name, sku=f"sa-{name[:8]}", category="Bitmiş Ürün",
               unit="adet", current_stock=0, domain="cosmetics")
    raw = Item(name=f"Su ({name[:6]})", sku=f"sar-{name[:8]}", category="Hammadde",
               unit="g", current_stock=100, domain="cosmetics")
    db.add_all([tgt, raw]); db.flush()
    rec = Recipe(name=tgt.name, output_quantity=1, output_unit="adet",
                 target_item_id=tgt.id, domain="cosmetics")
    db.add(rec); db.flush()
    db.add(RecipeIngredient(recipe_id=rec.id, item_id=raw.id, quantity=10, unit="g"))
    db.commit()
    return rec.id, rec.name


# ─── Sayfa ───────────────────────────────────────────────────────────────────

def test_page_renders(authed_client: TestClient):
    r = authed_client.get("/numune-analiz")
    assert r.status_code == 200
    assert "Numune Analiz" in r.text and "FR.KK.01" in r.text


def test_page_redirects_without_login(client: TestClient):
    r = client.get("/numune-analiz", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["location"]


def test_labtech_can_open_page(labtech_client: TestClient):
    r = labtech_client.get("/numune-analiz")
    assert r.status_code == 200                      # qc.view yeter
    # form kartı yalnız qc.approve'a render edilir (JS'teki id string'i sayılmaz)
    assert 'id="formCard"' not in r.text


# ─── CRUD ────────────────────────────────────────────────────────────────────

def test_create_and_list(authed_client: TestClient):
    r = authed_client.post(_API, json=_payload(), headers=_HDR)
    assert r.status_code == 201, r.text
    doc = r.json()["document_no"]
    assert doc.startswith("NA-")
    d = authed_client.get(_API).json()
    assert d["count"] == 1 and d["analyses"][0]["document_no"] == doc
    assert d["analyses"][0]["result_label"] == "UYGUN DEĞİLDİR"


def test_create_requires_qc_approve(labtech_client: TestClient):
    r = labtech_client.post(_API, json=_payload(), headers=_HDR)
    assert r.status_code == 403


def test_labtech_can_view_list(authed_client: TestClient, client: TestClient, db_session: Session):
    authed_client.post(_API, json=_payload(), headers=_HDR)
    r = client.post("/api/login", json={"username": "meltem", "password": "minerva123"}, headers=_HDR)
    assert r.status_code == 200
    d = client.get(_API).json()
    assert d["count"] == 1


def test_detail_and_edit(authed_client: TestClient):
    rid = authed_client.post(_API, json=_payload(), headers=_HDR).json()["id"]
    x = authed_client.get(f"{_API}/{rid}").json()
    assert x["bulk_name"] == "Saç Kremi Deneme Bulk" and x["production_date"] == "18.07.2026"
    up = _payload(result="uygun", result_text="Revize formülasyon uygun bulundu.")
    up["properties"][3]["found"] = "5.0"
    r = authed_client.put(f"{_API}/{rid}", json=up, headers=_HDR)
    assert r.status_code == 200
    x2 = authed_client.get(f"{_API}/{rid}").json()
    assert x2["result_label"] == "UYGUNDUR"
    assert x2["properties"][3]["found"] == "5.0"


def test_soft_delete(authed_client: TestClient, db_session: Session):
    rid = authed_client.post(_API, json=_payload(), headers=_HDR).json()["id"]
    r = authed_client.delete(f"{_API}/{rid}", headers=_HDR)
    assert r.status_code == 200
    assert authed_client.get(_API).json()["count"] == 0
    assert authed_client.get(f"{_API}/{rid}").status_code == 404
    row = db_session.query(SampleAnalysis).filter(SampleAnalysis.id == rid).one()
    assert row.is_active is False                    # hard delete DEĞİL


def test_invalid_payload_422(authed_client: TestClient):
    bad = _payload(); bad.pop("bulk_name")
    assert authed_client.post(_API, json=bad, headers=_HDR).status_code == 422
    assert authed_client.post(_API, json=_payload(properties=[]), headers=_HDR).status_code == 422
    assert authed_client.post(_API, json=_payload(result="belki"), headers=_HDR).status_code == 400


# ─── Domain + snapshot ───────────────────────────────────────────────────────

def test_domain_isolation(authed_client: TestClient):
    authed_client.post(_API, json=_payload(), headers=_HDR)
    r = authed_client.post("/api/domain/switch", json={"domain": "supplement"}, headers=_HDR)
    assert r.status_code == 200
    assert authed_client.get(_API).json()["count"] == 0
    authed_client.post("/api/domain/switch", json={"domain": "cosmetics"}, headers=_HDR)
    assert authed_client.get(_API).json()["count"] == 1


def test_recipe_snapshot(authed_client: TestClient, db_session: Session):
    rec_id, rec_name = _recipe(db_session)
    r = authed_client.post(_API, json=_payload(recipe_id=rec_id), headers=_HDR)
    assert r.status_code == 201, r.text
    x = authed_client.get(f"{_API}/{r.json()['id']}").json()
    assert x["recipe_id"] == rec_id and x["recipe_name"] == rec_name
    # yabancı/olmayan reçete → 404
    assert authed_client.post(_API, json=_payload(recipe_id=999999), headers=_HDR).status_code == 404


# ─── PDF + audit ─────────────────────────────────────────────────────────────

def test_pdf_is_a4_letterhead(authed_client: TestClient):
    rid = authed_client.post(_API, json=_payload(), headers=_HDR).json()["id"]
    r = authed_client.get(f"{_API}/{rid}/pdf")
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"
    assert "numune_analiz" in r.headers["content-disposition"]
    r.headers["content-disposition"].encode("latin-1")      # header-safe
    assert _all_pages_a4(r.content)
    text = "".join(pg.extract_text() for pg in PdfReader(BytesIO(r.content)).pages)
    assert "NUMUNE ANAL" in text and "LOT-2407-A" in text and "UYGUN DE" in text
    assert "FR.KK.01" in text


def test_audit_row_written(authed_client: TestClient, db_session: Session):
    doc = authed_client.post(_API, json=_payload(), headers=_HDR).json()["document_no"]
    rows = (db_session.query(AdminAuditLog)
            .filter(AdminAuditLog.action == "sample_analysis.create").all())
    assert any(r.target_name == doc for r in rows)
