"""
Kommo entegrasyonu testleri (tek yön ayna) — httpx MOCK'lu, ağ yok.

  • RBAC: /status crm.view, /import admin.view
  • run_sync: Kommo → CrmCompany/Contact/Deal eşleme + idempotent (kommo_id)
  • webhook: secret eşleşmezse 403, eşleşirse 200 + handler tetiklenir
"""
from fastapi.testclient import TestClient

from core import kommo as K
from database import CrmCompany, CrmContact, CrmDeal, CrmActivity

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
    monkeypatch.setattr(K, "_sync_chat_events", lambda db, client, since: 0)  # ağ çağrısı yok

    counts = K.run_sync(db_session)
    assert counts == {"companies": 1, "contacts": 1, "leads": 1, "messages": 0}

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


def test_meta_source_from_fb_tag(db_session, monkeypatch):
    # 'fb…' etiketli lead → source 'meta'; etiketsiz → 'kommo'
    _setup_env(monkeypatch)
    meta_lead = {"id": 7777, "name": "Meta Lead", "price": 0, "status_id": None,
                 "_embedded": {"tags": [{"name": "fb2022990439093077"}]}}
    plain_lead = {"id": 7778, "name": "Düz Lead", "price": 0, "status_id": None,
                  "_embedded": {"tags": []}}

    def fake_iter(client, path, key, extra=None):
        return iter([meta_lead, plain_lead]) if key == "leads" else iter([])
    monkeypatch.setattr(K, "_iter_entities", fake_iter)
    monkeypatch.setattr(K, "_load_status_names", lambda c: {})

    K.run_sync(db_session)
    assert db_session.query(CrmDeal).filter(CrmDeal.kommo_id == 7777).one().source == "facebook"
    assert db_session.query(CrmDeal).filter(CrmDeal.kommo_id == 7778).one().source == "kommo"


def test_meta_propagates_to_contact_and_company(db_session, monkeypatch):
    # 'fb' etiketi Kommo'da yalnızca LEAD'de; bağlı kişi/firma da "meta" olmalı.
    _setup_env(monkeypatch)
    company = {"id": 9100, "name": "Meta Co"}
    contact = {"id": 8100, "name": "Meta Kişi", "_embedded": {"companies": [{"id": 9100}]}}
    lead = {"id": 7100, "name": "Meta Lead", "price": 0, "status_id": None,
            "_embedded": {"tags": [{"name": "fb999"}], "contacts": [{"id": 8100}],
                          "companies": [{"id": 9100}]}}

    def fake(client, path, key, extra=None):
        if key == "companies" and "companies" in path:
            return iter([company])
        if key == "contacts":
            return iter([contact])
        if key == "leads":
            return iter([lead])
        return iter([])
    monkeypatch.setattr(K, "_iter_entities", fake)
    monkeypatch.setattr(K, "_load_status_names", lambda c: {})
    monkeypatch.setattr(K, "_sync_chat_events", lambda db, c, s: 0)

    K.run_sync(db_session)
    assert db_session.query(CrmDeal).filter(CrmDeal.kommo_id == 7100).one().source == "facebook"
    assert db_session.query(CrmContact).filter(CrmContact.kommo_id == 8100).one().source == "facebook"
    assert db_session.query(CrmCompany).filter(CrmCompany.kommo_id == 9100).one().source == "facebook"


def test_chat_origin_sets_whatsapp_channel(db_session):
    # waba origin'li sohbet olayı → bağlı deal/kişi/firma kaynağı 'whatsapp'.
    co = CrmCompany(name="WA Co", kommo_id=9200, source="kommo")
    ct = CrmContact(full_name="WA Kişi", kommo_id=8200, company_id=None, source="kommo")
    db_session.add_all([co, ct]); db_session.flush()
    ct.company_id = co.id
    d = CrmDeal(title="WA Lead", kommo_id=7200, contact_id=ct.id, company_id=co.id, source="kommo")
    db_session.add(d); db_session.commit()
    ev = {"id": 990010, "type": "incoming_chat_message", "entity_type": "lead", "entity_id": 7200,
          "created_at": 1782200000, "value_after": [{"message": {"origin": "waba", "talk_id": 1}}]}
    K._ingest_chat_event(db_session, ev)
    db_session.commit()
    assert db_session.query(CrmDeal).filter(CrmDeal.kommo_id == 7200).one().source == "whatsapp"
    assert db_session.query(CrmContact).filter(CrmContact.kommo_id == 8200).one().source == "whatsapp"
    assert db_session.query(CrmCompany).filter(CrmCompany.kommo_id == 9200).one().source == "whatsapp"


def test_source_filter_companies(authed_client: TestClient, db_session):
    from database import CrmCompany
    db_session.add(CrmCompany(name="Meta Co", source="meta"))
    db_session.add(CrmCompany(name="Kommo Co", source="kommo"))
    db_session.commit()
    # manuel firma (UI ucundan) → source 'manual'
    authed_client.post("/api/crm/companies", json={"name": "Elle Co"}, headers=_H)

    meta = authed_client.get("/api/crm/companies?source=meta").json()
    assert [c["name"] for c in meta] == ["Meta Co"]
    assert meta[0]["source_label"] == "Meta"

    manual = authed_client.get("/api/crm/companies?source=manual").json()
    names = [c["name"] for c in manual]
    assert "Elle Co" in names and "Meta Co" not in names and "Kommo Co" not in names


def test_chat_event_ingest_creates_activity(db_session):
    # 'incoming_chat_message' olayı → WhatsApp aktivitesi (metin yok), deal'e bağlı; dedup.
    d = CrmDeal(title="Chat Deal", kommo_id=5555, source="meta")
    db_session.add(d); db_session.commit()
    ev = {"id": 99001, "type": "incoming_chat_message",
          "entity_type": "lead", "entity_id": 5555, "created_at": 1782200000}
    assert K._ingest_chat_event(db_session, ev) is True
    db_session.commit()
    a = db_session.query(CrmActivity).filter(CrmActivity.external_id == "kommo_evt_99001").one()
    assert a.type == "whatsapp" and a.deal_id == d.id and a.contact_id is None
    # aynı olay tekrar → çift kayıt yok
    assert K._ingest_chat_event(db_session, ev) is False
    assert db_session.query(CrmActivity).filter(CrmActivity.external_id == "kommo_evt_99001").count() == 1


def test_chat_event_unlinked_skipped(db_session):
    # eşleşen deal/contact yoksa atla
    ev = {"id": 99002, "type": "outgoing_chat_message",
          "entity_type": "lead", "entity_id": 88888, "created_at": 1782200000}
    assert K._ingest_chat_event(db_session, ev) is False


def test_webhook_id_parser():
    ids = K._entity_ids_from_webhook({
        "leads[add][0][id]": "1", "contacts[update][0][id]": "2",
        "companies[delete][0][id]": "3", "leads[status][0][id]": "4",
    })
    assert ids["leads"] == {1, 4}
    assert ids["contacts"] == {2}
    assert ids["del_companies"] == {3}
