"""
CRM — Müşteri İlişkileri testleri.

  • /crm sayfası render (yetkili) + API auth/RBAC
  • firma/kişi CRUD + soft-delete
  • paylaşımlı zaman çizelgesi (aktivite ekle/pin/sil)
  • görevler (oluştur/tamamla/scope)
  • pipeline: fırsat oluştur → Kanban gruplama → taşı (aşama/durum) → kazanıldı
  • pano sayıları
  • RBAC: LabTech oluşturabilir ama silemez (403)
"""
from fastapi.testclient import TestClient

_H = {"Origin": "http://testserver"}


# ─── Yardımcılar ─────────────────────────────────────────────────────────────

def _company(ac, name="Acme A.Ş.", **extra):
    body = {"name": name}
    body.update(extra)
    r = ac.post("/api/crm/companies", json=body, headers=_H)
    assert r.status_code == 201, r.text
    return r.json()


def _contact(ac, name="Ali Veli", company_id=None, **extra):
    body = {"full_name": name, "company_id": company_id}
    body.update(extra)
    r = ac.post("/api/crm/contacts", json=body, headers=_H)
    assert r.status_code == 201, r.text
    return r.json()


def _deal(ac, title="Büyük Anlaşma", **extra):
    body = {"title": title}
    body.update(extra)
    r = ac.post("/api/crm/deals", json=body, headers=_H)
    assert r.status_code == 201, r.text
    return r.json()


# ─── Sayfa + auth ────────────────────────────────────────────────────────────

def test_crm_page_renders(authed_client: TestClient):
    r = authed_client.get("/crm")
    assert r.status_code == 200
    assert "CRM" in r.text


def test_api_requires_auth(client: TestClient):
    # Oturumsuz → 401
    assert client.get("/api/crm/companies").status_code == 401


def test_default_stages_seeded(authed_client: TestClient):
    stages = authed_client.get("/api/crm/stages").json()
    assert len(stages) == 6
    assert any(s["is_won"] for s in stages)
    assert any(s["is_lost"] for s in stages)


# ─── Firma + kişi CRUD ────────────────────────────────────────────────────────

def test_company_crud_and_soft_delete(authed_client: TestClient):
    c = _company(authed_client, name="Geven Ltd.", sector="Kozmetik", city="İstanbul")
    cid = c["id"]
    assert c["name"] == "Geven Ltd." and c["sector"] == "Kozmetik"

    # listede görünür
    rows = authed_client.get("/api/crm/companies").json()
    assert any(x["id"] == cid for x in rows)

    # güncelle
    r = authed_client.put(f"/api/crm/companies/{cid}",
                          json={"name": "Geven Kozmetik Ltd.", "city": "Ankara"}, headers=_H)
    assert r.status_code == 200 and r.json()["city"] == "Ankara"

    # detay
    d = authed_client.get(f"/api/crm/companies/{cid}").json()
    assert d["company"]["name"] == "Geven Kozmetik Ltd."

    # soft-delete → listeden düşer ama include_inactive ile görünür
    assert authed_client.delete(f"/api/crm/companies/{cid}", headers=_H).status_code == 200
    rows = authed_client.get("/api/crm/companies").json()
    assert not any(x["id"] == cid for x in rows)
    rows_all = authed_client.get("/api/crm/companies?include_inactive=true").json()
    assert any(x["id"] == cid for x in rows_all)


def test_contact_linked_to_company(authed_client: TestClient):
    c = _company(authed_client)
    ct = _contact(authed_client, name="Ayşe Yılmaz", company_id=c["id"],
                  whatsapp_number="+90 555 111 22 33", title="Satınalma")
    assert ct["company_id"] == c["id"]
    assert ct["wa_link"] and "905551112233" in ct["wa_link"]
    # firma detayında kişi listelenir
    d = authed_client.get(f"/api/crm/companies/{c['id']}").json()
    assert any(x["id"] == ct["id"] for x in d["contacts"])


# ─── Paylaşımlı zaman çizelgesi ───────────────────────────────────────────────

def test_shared_activity_timeline(authed_client: TestClient):
    c = _company(authed_client)
    r = authed_client.post("/api/crm/activities",
                           json={"type": "note", "body": "Müşteriyi aradım, ilgileniyor.",
                                 "company_id": c["id"]}, headers=_H)
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    assert r.json()["author_name"]  # yazar snapshot'ı dolu

    acts = authed_client.get(f"/api/crm/activities?company_id={c['id']}").json()
    assert any(a["id"] == aid for a in acts)

    # sabitleme aç/kapa
    p = authed_client.post(f"/api/crm/activities/{aid}/pin", headers=_H)
    assert p.status_code == 200 and p.json()["is_pinned"] is True


def test_activity_requires_a_scope(authed_client: TestClient):
    # firma/kişi/fırsat verilmezse 400
    r = authed_client.post("/api/crm/activities",
                           json={"type": "note", "body": "boşta"}, headers=_H)
    assert r.status_code == 400


# ─── Görevler ─────────────────────────────────────────────────────────────────

def test_task_create_and_complete(authed_client: TestClient):
    c = _company(authed_client)
    r = authed_client.post("/api/crm/tasks",
                           json={"title": "Teklif gönder", "company_id": c["id"]}, headers=_H)
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert r.json()["status"] == "open"

    done = authed_client.post(f"/api/crm/tasks/{tid}/complete", headers=_H)
    assert done.status_code == 200 and done.json()["status"] == "done"


# ─── Pipeline ─────────────────────────────────────────────────────────────────

def test_deal_pipeline_move_and_win(authed_client: TestClient):
    c = _company(authed_client)
    stages = authed_client.get("/api/crm/stages").json()
    won_stage = next(s for s in stages if s["is_won"])

    d = _deal(authed_client, title="Zincir Market Anlaşması", company_id=c["id"], value=50000)
    did = d["id"]
    assert d["status"] == "open"

    # Kanban: açık fırsat bir aşamada görünür
    pipe = authed_client.get("/api/crm/pipeline").json()
    all_deal_ids = [dl["id"] for lst in pipe["deals_by_stage"].values() for dl in lst]
    assert did in all_deal_ids

    # 'Kazanıldı' aşamasına taşı → durum otomatik 'won'
    mv = authed_client.post(f"/api/crm/deals/{did}/move",
                            json={"stage_id": won_stage["id"], "sort_order": 0}, headers=_H)
    assert mv.status_code == 200 and mv.json()["status"] == "won"

    # artık açık pipeline'da yok
    pipe2 = authed_client.get("/api/crm/pipeline").json()
    open_ids = [dl["id"] for lst in pipe2["deals_by_stage"].values() for dl in lst]
    assert did not in open_ids


def test_deal_close_endpoint(authed_client: TestClient):
    d = _deal(authed_client, title="Kaybedilecek")
    r = authed_client.post(f"/api/crm/deals/{d['id']}/close",
                           json={"result": "lost", "lost_reason": "Bütçe yok"}, headers=_H)
    assert r.status_code == 200 and r.json()["status"] == "lost"


# ─── Pano ─────────────────────────────────────────────────────────────────────

def test_dashboard_counts(authed_client: TestClient):
    _company(authed_client, name="X")
    _company(authed_client, name="Y")
    _deal(authed_client, title="D1", value=1000)
    d = authed_client.get("/api/crm/dashboard").json()
    assert d["counts"]["companies"] >= 2
    assert d["counts"]["open_deals"] >= 1
    assert "pipeline_by_stage" in d and "recent_activities" in d


# ─── RBAC ─────────────────────────────────────────────────────────────────────

def test_crm_access_off_by_default(labtech_client: TestClient):
    # CRM varsayılan KAPALI — yalnızca SuperAdmin + yetki matrisinden açılanlar girer.
    # LabTech (meltem) hiçbir CRM yetkisine sahip değil → 403.
    assert labtech_client.get("/api/crm/companies").status_code == 403
    assert labtech_client.post("/api/crm/companies",
                               json={"name": "X"}, headers=_H).status_code == 403


def test_granting_crm_permission_enables_access(labtech_client: TestClient, db_session):
    # Yetki matrisinden (admin) CRM erişimi verilince kullanıcı girebilir.
    import json
    from database import User
    u = db_session.query(User).filter(User.username == "meltem").first()
    u.permissions = json.dumps({"crm": {"view": True, "create": True, "edit": False, "delete": False}})
    db_session.commit()

    assert labtech_client.get("/api/crm/companies").status_code == 200
    c = labtech_client.post("/api/crm/companies", json={"name": "İzinli Firma"}, headers=_H)
    assert c.status_code == 201
    # delete yetkisi verilmedi → 403
    assert labtech_client.delete(f"/api/crm/companies/{c.json()['id']}", headers=_H).status_code == 403


def test_crm_users_lists_only_crm_enabled(authed_client: TestClient):
    # /users yalnızca CRM yetkili kullanıcıları döndürür (lab kullanıcıları değil).
    users = authed_client.get("/api/crm/users").json()
    names = [u["username"] for u in users]
    assert "dogukan" in names            # SuperAdmin — her zaman CRM erişimi
    assert "meltem" not in names         # LabTech — varsayılan CRM kapalı
