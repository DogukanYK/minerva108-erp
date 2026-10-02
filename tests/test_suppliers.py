"""Tedarikçi düzenleme (PUT /api/suppliers/{id}) — sayfada daha önce yalnız ekle/sil vardı."""
from fastapi.testclient import TestClient

ORIGIN = {"Origin": "http://testserver"}


def _create(c: TestClient, name="Argan Kimya") -> int:
    r = c.post("/api/suppliers", json={"name": name, "phone": "0212 000 00 00"}, headers=ORIGIN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _get(c: TestClient, sid: int) -> dict:
    return next(s for s in c.get("/api/suppliers").json() if s["id"] == sid)


def test_update_supplier_changes_fields(authed_client: TestClient):
    sid = _create(authed_client)
    r = authed_client.put(f"/api/suppliers/{sid}", headers=ORIGIN, json={
        "name": "  Argan Kimya A.Ş.  ", "contact_person": "Ayşe Yılmaz", "email": "ayse@argan.com",
        "phone": "0216 111 11 11", "notes": "30 gün vade"})
    assert r.status_code == 200, r.text
    s = _get(authed_client, sid)
    assert s["name"] == "Argan Kimya A.Ş."            # baş/son boşluk kırpılır
    assert (s["contact_person"], s["email"], s["phone"], s["notes"]) == \
        ("Ayşe Yılmaz", "ayse@argan.com", "0216 111 11 11", "30 gün vade")


def test_update_supplier_clears_optional_fields_and_rejects_blank_name(authed_client: TestClient):
    sid = _create(authed_client)
    r = authed_client.put(f"/api/suppliers/{sid}", headers=ORIGIN, json={"name": "Argan", "phone": "  "})
    assert r.status_code == 200
    assert _get(authed_client, sid)["phone"] is None
    r = authed_client.put(f"/api/suppliers/{sid}", headers=ORIGIN, json={"name": "   "})
    assert r.status_code == 400
    assert _get(authed_client, sid)["name"] == "Argan"


def test_update_supplier_other_domain_is_404(authed_client: TestClient):
    sid = _create(authed_client, "Kozmetik Tedarikçi")
    authed_client.post("/api/domain/switch", json={"domain": "supplement"}, headers=ORIGIN)
    r = authed_client.put(f"/api/suppliers/{sid}", headers=ORIGIN, json={"name": "Değişti"})
    assert r.status_code == 404
    authed_client.post("/api/domain/switch", json={"domain": "cosmetics"}, headers=ORIGIN)
    assert _get(authed_client, sid)["name"] == "Kozmetik Tedarikçi"


def test_update_supplier_unknown_is_404(authed_client: TestClient):
    r = authed_client.put("/api/suppliers/999999", headers=ORIGIN, json={"name": "X"})
    assert r.status_code == 404


def test_update_supplier_requires_items_edit(client: TestClient):
    # Önce SuperAdmin bir tedarikçi açar, sonra LabTech (items.edit kapalı) düzenlemeye çalışır.
    client.post("/api/login", json={"username": "dogukan", "password": "minerva123"}, headers=ORIGIN)
    sid = _create(client)
    client.post("/api/logout", headers=ORIGIN)
    client.post("/api/login", json={"username": "meltem", "password": "minerva123"}, headers=ORIGIN)
    r = client.put(f"/api/suppliers/{sid}", headers=ORIGIN, json={"name": "Yetkisiz"})
    assert r.status_code == 403
