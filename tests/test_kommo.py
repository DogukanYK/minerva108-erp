"""
Kommo entegrasyonu testleri (tek yön ayna) — httpx MOCK'lu, ağ yok.

  • RBAC: /status crm.view, /import admin.view
  • run_sync: Kommo → CrmCompany/Contact/Deal eşleme + idempotent (kommo_id)
  • webhook: secret eşleşmezse 403, eşleşirse 200 + handler tetiklenir
"""
from fastapi.testclient import TestClient

from core import kommo as K
from database import CrmCompany, CrmContact, CrmDeal

_H = {"Origin": "http://testserver"}


# ─── Sahte Kommo verisi ──────────────────────────────────────────────────────

_COMPANY = {"id": 9001, "name": "Kommo Firma A.Ş.",
            "custom_fields_values": [
                {"field_code": "PHONE", "values": [{"value": "+90 212 000 00 00"}]},
                {"field_code": "EMAIL", "values": [{"value": "info@kommofirma.com"}]}]}
_CONTACT = {"id": 8001, "name": "Kommo Kişi",
            "custom_fields_values": [
                {"field_code": "PHONE", "values": [{"value": "+90 555 111 22 33"}]}],
            "_embedded": {"companies": [{"id": 9001}]}}
_LEAD = {"id": 7001, "name": "Kommo Fırsat", "price": 12345, "status_id": None,
         "_embedded": {"contacts": [{"id": 8001}]}}


def _fake_iter(client, path, key, extra=None):
    if key == "companies" and "companies" in path:
        return iter([_COMPANY])
    if key == "contacts":
        return iter([_CONTACT])
    if key == "leads":
        return iter([_LEAD])
    return iter([])


def _setup_env(monkeypatch):
    monkeypatch.setenv("KOMMO_SUBDOMAIN", "minerva")
    monkeypatch.setenv("KOMMO_TOKEN", "test-long-lived-token")
    monkeypatch.setenv("KOMMO_WEBHOOK_SECRET", "s3cr3t-webhook")


# ─── RBAC ─────────────────────────────────────────────────────────────────────

def test_status_requires_crm_view(labtech_client: TestClient):
    # LabTech (CRM kapalı) → 403
    assert labtech_client.get("/api/crm/integrations/kommo/status").status_code == 403


def test_status_ok_for_superadmin_unconfigured(authed_client: TestClient):
    r = authed_client.get("/api/crm/integrations/kommo/status")
    assert r.status_code == 200
    assert r.json()["configured"] is False


def test_import_requires_admin(labtech_client: TestClient):
    assert labtech_client.post("/api/crm/integrations/kommo/import", headers=_H).status_code == 403


# ─── run_sync eşleme + idempotency ────────────────────────────────────────────

def test_run_sync_maps_and_idempotent(db_session, monkeypatch):
    _setup_env(monkeypatch)
    monkeypatch.setattr(K, "_iter_entities", _fake_iter)
    monkeypatch.setattr(K, "_load_status_names", lambda client: {})

    counts = K.run_sync(db_session)
    assert counts == {"companies": 1, "contacts": 1, "leads": 1}

    co = db_session.query(CrmCompany).filter(CrmCompany.kommo_id == 9001).one()
    assert co.name == "Kommo Firma A.Ş." and co.phone == "+90 212 000 00 00" and co.source == "kommo"
    ct = db_session.query(CrmContact).filter(CrmContact.kommo_id == 8001).one()
    assert ct.company_id == co.id and ct.whatsapp_number == "+90 555 111 22 33"
    dl = db_session.query(CrmDeal).filter(CrmDeal.kommo_id == 7001).one()
    assert dl.contact_id == ct.id and dl.value == 12345 and dl.stage_id is not None

    # tekrar çalıştır → çift kayıt YOK (idempotent)
    K.run_sync(db_session)
    assert db_session.query(CrmCompany).filter(CrmCompany.kommo_id == 9001).count() == 1
    assert db_session.query(CrmContact).filter(CrmContact.kommo_id == 8001).count() == 1
    assert db_session.query(CrmDeal).filter(CrmDeal.kommo_id == 7001).count() == 1


# ─── Webhook secret koruması ──────────────────────────────────────────────────

def test_webhook_rejects_bad_secret(client: TestClient, monkeypatch):
    _setup_env(monkeypatch)
    calls = []
    monkeypatch.setattr(K, "handle_webhook", lambda payload: calls.append(payload))
    r = client.post("/api/crm/integrations/kommo/webhook/yanlis",
                    data={"leads[add][0][id]": "7001"})
    assert r.status_code == 403
    assert calls == []


def test_webhook_accepts_good_secret(client: TestClient, monkeypatch):
    _setup_env(monkeypatch)
    calls = []
    monkeypatch.setattr(K, "handle_webhook", lambda payload: calls.append(payload))
    r = client.post("/api/crm/integrations/kommo/webhook/s3cr3t-webhook",
                    data={"leads[add][0][id]": "7001"})
    assert r.status_code == 200 and r.json()["ok"] is True
    # arka plan görevi (TestClient'ta yanıttan önce senkron koşar) tetiklendi
    assert len(calls) == 1
    assert calls[0].get("leads[add][0][id]") == "7001"


def test_webhook_id_parser():
    ids = K._entity_ids_from_webhook({
        "leads[add][0][id]": "1", "contacts[update][0][id]": "2",
        "companies[delete][0][id]": "3", "leads[status][0][id]": "4",
    })
    assert ids["leads"] == {1, 4}
    assert ids["contacts"] == {2}
    assert ids["del_companies"] == {3}
