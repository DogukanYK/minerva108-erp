"""
CRM Faz C3 + C4 — etiketler, özel alanlar, dosya ekleri, toplu işlemler, geçmiş.
"""
from fastapi.testclient import TestClient

_H = {"Origin": "http://testserver"}


def _company(ac, name="C", **extra):
    r = ac.post("/api/crm/companies", json={"name": name, **extra}, headers=_H)
    assert r.status_code == 201, r.text
    return r.json()


# ─── Etiketler (C3) ──────────────────────────────────────────────────────────

def test_tags_crud_and_assign(authed_client: TestClient):
    t = authed_client.post("/api/crm/tags", json={"name": "VIP", "color": "#ff0000"}, headers=_H).json()
    assert t["name"] == "VIP"
    # idempotent (aynı ad → aynı id)
    t2 = authed_client.post("/api/crm/tags", json={"name": "vip"}, headers=_H).json()
    assert t2["id"] == t["id"]
    co = _company(authed_client, name="Etiketli")
    r = authed_client.put(f"/api/crm/company/{co['id']}/tags", json={"tag_ids": [t["id"]]}, headers=_H)
    assert r.status_code == 200
    got = authed_client.get(f"/api/crm/company/{co['id']}/tags").json()
    assert [x["name"] for x in got] == ["VIP"]


def test_tag_create_requires_edit(labtech_client: TestClient):
    assert labtech_client.post("/api/crm/tags", json={"name": "X"}, headers=_H).status_code == 403


def test_tag_filter_on_list(authed_client: TestClient):
    t = authed_client.post("/api/crm/tags", json={"name": "FiltreTag"}, headers=_H).json()
    a = _company(authed_client, name="Tagli")
    _company(authed_client, name="Tagsiz")
    authed_client.put(f"/api/crm/company/{a['id']}/tags", json={"tag_ids": [t["id"]]}, headers=_H)
    rows = authed_client.get(f"/api/crm/companies?tag={t['id']}").json()
    names = [c["name"] for c in rows]
    assert "Tagli" in names and "Tagsiz" not in names


# ─── Özel alanlar (C3) ───────────────────────────────────────────────────────

def test_custom_fields(authed_client: TestClient):
    f = authed_client.post("/api/crm/fields",
                           json={"entity": "company", "label": "Sektör Kodu", "field_type": "text"},
                           headers=_H).json()
    assert "id" in f
    defs = authed_client.get("/api/crm/fields?entity=company").json()
    assert any(x["label"] == "Sektör Kodu" for x in defs)
    co = _company(authed_client, name="AlanlıFirma")
    fid = defs[0]["id"]
    r = authed_client.put(f"/api/crm/company/{co['id']}/fields",
                          json={"values": {str(fid): "K-42"}}, headers=_H)
    assert r.status_code == 200
    got = authed_client.get(f"/api/crm/company/{co['id']}/fields").json()
    assert got[0]["value"] == "K-42"


def test_field_create_requires_admin(labtech_client: TestClient):
    r = labtech_client.post("/api/crm/fields",
                            json={"entity": "company", "label": "X", "field_type": "text"}, headers=_H)
    assert r.status_code == 403


# ─── Dosya ekleri (C4) ───────────────────────────────────────────────────────

def test_attachment_upload_missing_entity_404(authed_client: TestClient, db_session):
    # Var olmayan kayda dosya eklenemez (yetim ek koruması).
    from database import CrmAttachment
    r = authed_client.post("/api/crm/company/999999/attachments",
                           files={"file": ("x.pdf", b"X", "application/pdf")}, headers=_H)
    assert r.status_code == 404
    assert db_session.query(CrmAttachment).filter(CrmAttachment.entity_id == 999999).count() == 0


def test_tags_and_fields_put_missing_entity_404(authed_client: TestClient):
    t = authed_client.post("/api/crm/tags", json={"name": "Hayalet"}, headers=_H).json()
    assert authed_client.put("/api/crm/company/999999/tags",
                             json={"tag_ids": [t["id"]]}, headers=_H).status_code == 404
    assert authed_client.put("/api/crm/company/999999/fields",
                             json={"values": {}}, headers=_H).status_code == 404


def test_attachments(authed_client: TestClient):
    co = _company(authed_client, name="Ekli")
    up = authed_client.post(f"/api/crm/company/{co['id']}/attachments",
                            files={"file": ("teklif.pdf", b"PDFDATA", "application/pdf")}, headers=_H)
    assert up.status_code == 201, up.text
    aid = up.json()["id"]
    lst = authed_client.get(f"/api/crm/company/{co['id']}/attachments").json()
    assert any(a["id"] == aid and a["name"] == "teklif.pdf" for a in lst)
    dl = authed_client.get(f"/api/crm/attachments/{aid}/download")
    assert dl.status_code == 200 and dl.content == b"PDFDATA"
    assert authed_client.delete(f"/api/crm/attachments/{aid}", headers=_H).status_code == 200


# ─── Toplu işlemler (C4) ─────────────────────────────────────────────────────

def test_bulk_source_and_delete(authed_client: TestClient):
    a = _company(authed_client, name="Bulk A")
    b = _company(authed_client, name="Bulk B")
    r = authed_client.post("/api/crm/bulk",
                           json={"entity": "companies", "ids": [a["id"], b["id"]],
                                 "action": "source", "value": "kommo"}, headers=_H)
    assert r.status_code == 200 and r.json()["count"] == 2
    rows = authed_client.get("/api/crm/companies?source=kommo").json()
    assert {a["id"], b["id"]} <= {c["id"] for c in rows}
    # toplu sil (soft) → listeden düşer
    authed_client.post("/api/crm/bulk",
                       json={"entity": "companies", "ids": [a["id"]], "action": "delete"}, headers=_H)
    ids = {c["id"] for c in authed_client.get("/api/crm/companies").json()}
    assert a["id"] not in ids


def test_bulk_deals_soft_delete(authed_client: TestClient):
    # Fırsatlar artık toplu arşivlenebilir (soft delete).
    did = authed_client.post("/api/crm/deals", json={"title": "Bulk Fırsat"}, headers=_H).json()["id"]
    br = authed_client.post("/api/crm/bulk",
                            json={"entity": "deals", "ids": [did], "action": "delete"}, headers=_H)
    assert br.status_code == 200 and br.json()["count"] == 1
    assert did not in [x["id"] for x in authed_client.get("/api/crm/deals").json()]


def test_bulk_assign(authed_client: TestClient):
    me = next(u["id"] for u in authed_client.get("/api/crm/users").json() if u["username"] == "dogukan")
    a = _company(authed_client, name="Atama")
    authed_client.post("/api/crm/bulk", json={"entity": "companies", "ids": [a["id"]],
                                              "action": "assign", "value": str(me)}, headers=_H)
    rows = authed_client.get(f"/api/crm/companies?owner={me}").json()
    assert any(c["id"] == a["id"] for c in rows)


# ─── WhatsApp şablonları ─────────────────────────────────────────────────────

def test_wa_templates_crud(authed_client: TestClient):
    r = authed_client.post("/api/crm/wa-templates",
                           json={"name": "Tanışma", "body": "Merhaba {ad}, Minerva 108'den yazıyorum."},
                           headers=_H)
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    rows = authed_client.get("/api/crm/wa-templates").json()
    assert any(t["id"] == tid and "{ad}" in t["body"] for t in rows)
    assert authed_client.delete(f"/api/crm/wa-templates/{tid}", headers=_H).status_code == 200
    assert authed_client.get("/api/crm/wa-templates").json() == []


def test_wa_templates_rbac(labtech_client: TestClient):
    assert labtech_client.get("/api/crm/wa-templates").status_code == 403
    assert labtech_client.post("/api/crm/wa-templates",
                               json={"name": "X", "body": "Y"}, headers=_H).status_code == 403


# ─── Geçmiş (C4) ─────────────────────────────────────────────────────────────

def test_history(authed_client: TestClient):
    co = _company(authed_client, name="Geçmişli")
    authed_client.put(f"/api/crm/companies/{co['id']}",
                      json={"name": "Geçmişli 2", "city": "Bursa"}, headers=_H)
    h = authed_client.get(f"/api/crm/company/{co['id']}/history").json()
    actions = [r["action"] for r in h]
    assert "crm.company.create" in actions and "crm.company.update" in actions
