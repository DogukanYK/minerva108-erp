"""
Özelleştirilebilir rol etiketleri — SuperAdmin rollerin görünen adını değiştirir.

  • GET /api/admin/role-labels → 5 rol + varsayılan + güncel etiket
  • PUT ile değiştir → GET + resolver yansıtır; boş → varsayılana döner
  • rol ANAHTARI değişmez (yalnızca etiket); admin.edit gerekir
"""
import pytest
from fastapi.testclient import TestClient

from core.permissions import get_role_labels, invalidate_role_labels

_H = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def _reset_role_cache():
    invalidate_role_labels()   # her testten önce/sonra süreç-içi cache temiz
    yield
    invalidate_role_labels()


def test_list_role_labels_defaults(authed_client: TestClient):
    d = authed_client.get("/api/admin/role-labels").json()
    keys = {r["key"] for r in d["roles"]}
    assert {"SuperAdmin", "Manager", "LabLead", "LabTech", "Staff"} <= keys
    sa = next(r for r in d["roles"] if r["key"] == "SuperAdmin")
    assert sa["default"] == "Süper Yönetici" and sa["label"] == "Süper Yönetici"


def test_update_and_revert_role_label(authed_client: TestClient, db_session):
    r = authed_client.put("/api/admin/role-labels", headers=_H,
                          json={"labels": {"SuperAdmin": "Patron"}})
    assert r.status_code == 200
    assert any(x["key"] == "SuperAdmin" and x["label"] == "Patron" for x in r.json()["roles"])
    # GET yansıtır
    d = authed_client.get("/api/admin/role-labels").json()
    assert next(x for x in d["roles"] if x["key"] == "SuperAdmin")["label"] == "Patron"
    # resolver (db ile) yansıtır
    invalidate_role_labels()
    assert get_role_labels(db_session)["SuperAdmin"] == "Patron"
    # boş → varsayılana döner (override silinir)
    authed_client.put("/api/admin/role-labels", headers=_H, json={"labels": {"SuperAdmin": ""}})
    d2 = authed_client.get("/api/admin/role-labels").json()
    assert next(x for x in d2["roles"] if x["key"] == "SuperAdmin")["label"] == "Süper Yönetici"


def test_unknown_role_ignored(authed_client: TestClient):
    r = authed_client.put("/api/admin/role-labels", headers=_H,
                          json={"labels": {"Hacker": "x", "Manager": "Müdür"}})
    assert r.status_code == 200
    roles = {x["key"]: x["label"] for x in r.json()["roles"]}
    assert roles["Manager"] == "Müdür" and "Hacker" not in roles


def test_update_requires_admin_permission(labtech_client: TestClient):
    r = labtech_client.put("/api/admin/role-labels", headers=_H,
                           json={"labels": {"SuperAdmin": "X"}})
    assert r.status_code == 403
