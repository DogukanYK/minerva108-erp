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


def test_dedupe_endpoint(authed_client: TestClient):
    _company(authed_client, name="Minerva Kozmetik", phone="+90 212 555 44 33", email="info@minerva.com")
    # kısmi ad (fold) eşleşmesi
    r = authed_client.get("/api/crm/dedupe?entity=companies&name=minerva").json()
    assert any(x["match"] == "name" for x in r)
    # telefon format farkı eşleşir (son 10 hane)
    r2 = authed_client.get("/api/crm/dedupe?entity=companies&phone=02125554433").json()
    assert any(x["match"] == "phone" for x in r2)
    # eşleşmeyen → boş
    assert authed_client.get("/api/crm/dedupe?entity=companies&name=zzz").json() == []


def test_dedupe_requires_crm_view(labtech_client: TestClient):
    assert labtech_client.get("/api/crm/dedupe?entity=companies&name=a").status_code == 403


def test_source_filter_import_excluded_from_manual(authed_client: TestClient, db_session):
    from database import CrmCompany
    db_session.add(CrmCompany(name="İthal Co", source="import"))
    db_session.add(CrmCompany(name="Referans Co", source="referral"))
    db_session.commit()
    manual = [c["name"] for c in authed_client.get("/api/crm/companies?source=manual").json()]
    assert "Referans Co" in manual and "İthal Co" not in manual
    imported = [c["name"] for c in authed_client.get("/api/crm/companies?source=import").json()]
    assert imported == ["İthal Co"]


def test_update_contact_without_source_preserves_it(authed_client: TestClient):
    # PUT gövdesinde source yoksa mevcut sınıflama (örn. referral/whatsapp) silinmez.
    ct = _contact(authed_client, name="Kaynaklı Kişi", source="referral")
    r = authed_client.put(f"/api/crm/contacts/{ct['id']}",
                          json={"full_name": "Kaynaklı Kişi", "title": "Müdür"}, headers=_H)
    assert r.status_code == 200
    assert r.json()["source"] == "referral"
    # source açıkça yollanırsa değişir
    r2 = authed_client.put(f"/api/crm/contacts/{ct['id']}",
                           json={"full_name": "Kaynaklı Kişi", "source": "manual"}, headers=_H)
    assert r2.json()["source"] == "manual"


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

def test_move_deal_stamps_and_logs(authed_client: TestClient):
    # Aşama taşıma: zaman çizelgesine 'stage' aktivitesi + audit (Geçmiş) kaydı düşer.
    stages = authed_client.get("/api/crm/stages").json()
    d = _deal(authed_client, title="Taşınan")
    target = next(s for s in stages if s["id"] != d["stage_id"] and not s["is_won"] and not s["is_lost"])
    r = authed_client.post(f"/api/crm/deals/{d['id']}/move",
                           json={"stage_id": target["id"], "sort_order": 0}, headers=_H)
    assert r.status_code == 200
    acts = authed_client.get(f"/api/crm/activities?deal_id={d['id']}").json()
    stage_acts = [a for a in acts if a["type"] == "stage"]
    assert stage_acts and "Aşama:" in stage_acts[0]["body"] and target["name"] in stage_acts[0]["body"]
    hist = authed_client.get(f"/api/crm/deal/{d['id']}/history").json()
    assert "crm.deal.move" in [h["action"] for h in hist]
    # days_in_stage taşımayla sıfırlanır
    detail = authed_client.get(f"/api/crm/deals/{d['id']}").json()
    assert detail["deal"]["days_in_stage"] == 0


def test_move_deal_reindexes_target_stage(authed_client: TestClient):
    # sort_order = ekleme indeksi; hedef sütun kompakt 0..n numaralanır (kalıcı sıra).
    a = _deal(authed_client, title="Sıra A")
    b = _deal(authed_client, title="Sıra B")
    c = _deal(authed_client, title="Sıra C")
    d = _deal(authed_client, title="Sıra D")
    stage_id = a["stage_id"]
    r = authed_client.post(f"/api/crm/deals/{d['id']}/move",
                           json={"stage_id": stage_id, "sort_order": 1}, headers=_H)
    assert r.status_code == 200
    pipe = authed_client.get("/api/crm/pipeline").json()
    col = pipe["deals_by_stage"][str(stage_id)] if str(stage_id) in pipe["deals_by_stage"] else pipe["deals_by_stage"][stage_id]
    titles = [x["title"] for x in col]
    # başlangıç sırası (hepsi sort 0 → created_at desc): C, B, A; D indeks 1'e girer
    assert titles == ["Sıra C", "Sıra D", "Sıra B", "Sıra A"]
    assert [x["sort_order"] for x in col] == [0, 1, 2, 3]


def test_close_deal_writes_stage_activity(authed_client: TestClient):
    d = _deal(authed_client, title="Kapatılan")
    authed_client.post(f"/api/crm/deals/{d['id']}/close",
                       json={"result": "lost", "lost_reason": "Bütçe yok"}, headers=_H)
    acts = authed_client.get(f"/api/crm/activities?deal_id={d['id']}").json()
    stage_acts = [a for a in acts if a["type"] == "stage"]
    assert stage_acts and "Kaybedildi: Bütçe yok" in stage_acts[0]["body"]


def test_stage_type_not_user_creatable(authed_client: TestClient):
    # 'stage' tipi yalnızca sunucu içi — kullanıcı POST'u 'note'a düşer.
    c = _company(authed_client, name="Stage Guard")
    r = authed_client.post("/api/crm/activities",
                           json={"type": "stage", "body": "sahte", "company_id": c["id"]}, headers=_H)
    assert r.status_code == 201 and r.json()["type"] == "note"


def test_pipeline_days_in_stage_and_tags(authed_client: TestClient):
    d = _deal(authed_client, title="Etiketli Fırsat")
    t = authed_client.post("/api/crm/tags", json={"name": "Sıcak", "color": "#ef4444"}, headers=_H).json()
    authed_client.put(f"/api/crm/deal/{d['id']}/tags", json={"tag_ids": [t["id"]]}, headers=_H)
    pipe = authed_client.get("/api/crm/pipeline").json()
    row = next(x for rows in pipe["deals_by_stage"].values() for x in rows if x["id"] == d["id"])
    assert row["days_in_stage"] == 0
    assert [tg["name"] for tg in row["tags"]] == ["Sıcak"]


def test_pipeline_owner_filter(authed_client: TestClient):
    me = next(u["id"] for u in authed_client.get("/api/crm/users").json() if u["username"] == "dogukan")
    mine = _deal(authed_client, title="Benim Fırsat", owner_user_id=me)
    other = _deal(authed_client, title="Sahipsiz Fırsat")
    pipe = authed_client.get(f"/api/crm/pipeline?owner={me}").json()
    ids = [x["id"] for rows in pipe["deals_by_stage"].values() for x in rows]
    assert mine["id"] in ids and other["id"] not in ids


def test_deal_soft_delete_hides_from_lists(authed_client: TestClient, db_session):
    d = _deal(authed_client, title="Arşivlik", value=750)
    r = authed_client.delete(f"/api/crm/deals/{d['id']}", headers=_H)
    assert r.status_code == 200 and "arşiv" in r.json()["message"].lower()
    # listeler + pipeline + panodan düşer
    assert d["id"] not in [x["id"] for x in authed_client.get("/api/crm/deals").json()]
    pipe = authed_client.get("/api/crm/pipeline").json()
    all_ids = [x["id"] for rows in pipe["deals_by_stage"].values() for x in rows]
    assert d["id"] not in all_ids
    assert authed_client.get("/api/crm/dashboard").json()["counts"]["open_deals"] == 0
    # kayıt DB'de duruyor (arşiv) ve detayı hâlâ açılabilir (geçmiş bağlantıları)
    from database import CrmDeal
    row = db_session.query(CrmDeal).filter(CrmDeal.id == d["id"]).one()
    assert row.is_active is False
    assert authed_client.get(f"/api/crm/deals/{d['id']}").status_code == 200


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
