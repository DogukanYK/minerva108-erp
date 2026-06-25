"""
CRM Faz C1b + C2 — global arama, kaydedilmiş görünümler, sorumlu filtresi, raporlar.
"""
from fastapi.testclient import TestClient

_H = {"Origin": "http://testserver"}


def _me_id(ac):
    for u in ac.get("/api/crm/users").json():
        if u["username"] == "dogukan":
            return u["id"]
    return None


def test_global_search(authed_client: TestClient):
    authed_client.post("/api/crm/companies", json={"name": "Arama Kozmetik"}, headers=_H)
    authed_client.post("/api/crm/contacts", json={"full_name": "Arama Kişisi"}, headers=_H)
    authed_client.post("/api/crm/deals", json={"title": "Arama Fırsatı"}, headers=_H)
    d = authed_client.get("/api/crm/search?q=Arama").json()
    assert any(c["label"] == "Arama Kozmetik" for c in d["companies"])
    assert any(c["label"] == "Arama Kişisi" for c in d["contacts"])
    assert any(x["label"] == "Arama Fırsatı" for x in d["deals"])


def test_reports_summary_shape(authed_client: TestClient):
    authed_client.post("/api/crm/deals", json={"title": "R1", "value": 1000}, headers=_H)
    d = authed_client.get("/api/crm/reports/summary").json()
    for key in ("totals", "funnel", "monthly", "forecast", "leaderboard"):
        assert key in d
    assert d["totals"]["open_count"] >= 1
    assert isinstance(d["monthly"], list) and len(d["monthly"]) == 6


def test_saved_views_crud(authed_client: TestClient):
    r = authed_client.post("/api/crm/views",
                           json={"entity": "companies", "name": "Meta firmalar",
                                 "criteria": {"source": "meta"}}, headers=_H)
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    views = authed_client.get("/api/crm/views?entity=companies").json()
    assert any(v["id"] == vid and v["criteria"]["source"] == "meta" for v in views)
    assert authed_client.delete(f"/api/crm/views/{vid}", headers=_H).status_code == 200
    assert not authed_client.get("/api/crm/views?entity=companies").json()


def test_owner_filter(authed_client: TestClient):
    me = _me_id(authed_client)
    authed_client.post("/api/crm/companies", json={"name": "Sahipli", "owner_user_id": me}, headers=_H)
    authed_client.post("/api/crm/companies", json={"name": "Sahipsiz"}, headers=_H)
    rows = authed_client.get(f"/api/crm/companies?owner={me}").json()
    names = [c["name"] for c in rows]
    assert "Sahipli" in names and "Sahipsiz" not in names


def test_search_requires_auth(client: TestClient):
    assert client.get("/api/crm/search?q=x").status_code == 401
