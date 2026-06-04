"""
Faz 1 — QC formu izlenebilirlikte görüntüleme + PDF/Excel indirme testleri.

Bir Inventory satırına qc_form_data set edip:
  • /api/qc/{id}/form/export?format=pdf|excel  → 200 + doğru magic byte
  • form yoksa / kayıt yoksa                    → 404
  • /api/traceability/lot/{lot}                  → qc_form bloğu (etiketli)
  • auth yoksa                                   → 200 DEĞİL
"""
import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from database import Item, Inventory


_FORM = {
    "checklist": {"q01": "Evet", "q02": "Hayır", "q05": "Yok"},
    "lab_ml": 50.0, "lab_density": 0.98, "lab_color": "Şeffaf",
    "notes": "Numune kontrol edildi.", "status": "APPROVED",
    "submitted_at": "2026-05-20T11:30:00",
}


def _make_lot(db: Session, *, lot="PRD-20260520-113000", with_form=True) -> int:
    """Bir Item + Inventory yaratır; with_form=True ise qc_form_data doldurur. inv.id döner."""
    item = Item(name="Test Ürün", sku=f"SKU-{lot}", category="Bitmiş Ürün", unit="adet")
    db.add(item)
    db.flush()
    inv = Inventory(
        item_id=item.id, lot_number=lot, quantity=120, status="APPROVED",
        qc_approved_by="Songül Arslan",
        qc_form_data=(json.dumps(_FORM, ensure_ascii=False) if with_form else None),
    )
    db.add(inv)
    db.commit()
    return inv.id


def test_qc_form_export_pdf(authed_client: TestClient, db_session: Session):
    inv_id = _make_lot(db_session)
    r = authed_client.get(f"/api/qc/{inv_id}/form/export?format=pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:4] == b"%PDF"
    assert "qc_formu_" in r.headers.get("content-disposition", "")


def test_qc_form_export_excel(authed_client: TestClient, db_session: Session):
    inv_id = _make_lot(db_session, lot="PRD-X2")
    r = authed_client.get(f"/api/qc/{inv_id}/form/export?format=excel")
    assert r.status_code == 200
    assert r.content[:2] == b"PK"            # xlsx = zip arşivi


def test_qc_form_export_no_form_404(authed_client: TestClient, db_session: Session):
    inv_id = _make_lot(db_session, lot="PRD-NOFORM", with_form=False)
    r = authed_client.get(f"/api/qc/{inv_id}/form/export?format=pdf")
    assert r.status_code == 404


def test_qc_form_export_missing_inventory_404(authed_client: TestClient):
    r = authed_client.get("/api/qc/999999/form/export?format=pdf")
    assert r.status_code == 404


def test_trace_lot_includes_qc_form(authed_client: TestClient, db_session: Session):
    _make_lot(db_session, lot="PRD-TRACE1")
    r = authed_client.get("/api/traceability/lot/PRD-TRACE1")
    assert r.status_code == 200
    body = r.json()
    qc = body.get("qc_form")
    assert qc is not None
    assert qc["is_approved"] is True
    assert qc["status_label"] == "Onaylandı"
    # Checklist soru metinleriyle etiketlenmeli (q01 → gerçek soru metni)
    assert len(qc["checklist"]) >= 10
    assert qc["checklist"][0]["text"].startswith("Ürün kokusu")
    assert qc["checklist"][0]["answer"] == "Evet"


def test_trace_lot_without_form_qc_null(authed_client: TestClient, db_session: Session):
    _make_lot(db_session, lot="PRD-TRACE2", with_form=False)
    r = authed_client.get("/api/traceability/lot/PRD-TRACE2")
    assert r.status_code == 200
    assert r.json().get("qc_form") is None


def test_qc_form_export_requires_auth(client: TestClient, db_session: Session):
    inv_id = _make_lot(db_session, lot="PRD-AUTH")
    r = client.get(f"/api/qc/{inv_id}/form/export?format=pdf")
    assert r.status_code != 200       # giriş yok → 401/403
