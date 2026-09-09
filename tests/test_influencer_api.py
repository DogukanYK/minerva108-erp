"""
Influencer programı — API testleri (plan B9 Faz 1).

  • RBAC: login yok → 401, yetkisiz rol → 403, SuperAdmin → 200
  • Public başvuru: 201 + pending kayıt; honeypot sessiz 200 (kayıt yok);
    18 altı 400; aynı IP'den 6. istek 429
  • Onay: yeni creator + hesaplar; aynı e-postalı mevcut creator'a bağlanır
  • Metrik girişi → post_stats + özgünlük skoru + snapshot
  • Aşama makinesi: geçersiz geçiş 400, iptal gerekçesiz 400, stage_log
  • Gönderim: Delivery(kargo, preparing) + COGS snapshot, stok DEĞİŞMEZ;
    kargola → stok BİR kez düşer, ikinci çağrı 400; teslim → 2 CrmTask;
    şerhli PDF (%PDF + NUMUNEDİR bandı), kargolanmadan 403
  • Claim token tek kullanım (with_for_update); sayfa state'leri
  • Dosya yükleme: sihirli bayt reddi, boyut reddi
  • Ayarlar seed'li (5 kademe, 60 benchmark, CFG varsayılanları)
  • do_not_resend → gönderim 400

DİKKAT — anonim gönderimler TAZE TestClient(client.app) ile yapılır:
conftest'te authed_client == client (aynı cookie jar'ı), SuperAdmin
çerezi taşıyan istek CSRFMiddleware'e düşer (tests/test_reviews_public.py).
"""
from datetime import datetime

from fastapi.testclient import TestClient

from database import (
    CrmTask, Delivery, InfluencerAddress, InfluencerApplication, InfluencerCollabStageLog,
    InfluencerCreator, InfluencerMetricSnapshot, InfluencerShipment, InfluencerToken,
    Item, Transaction,
)

_H = {"Origin": "http://testserver"}
API = "/api/influencer"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 200
YEAR = datetime.utcnow().year


# ─── Yardımcılar ────────────────────────────────────────────────────────────

def _anon(client: TestClient) -> TestClient:
    return TestClient(client.app)


def _application_body(**over) -> dict:
    body = {
        "store_key": "minerva", "lang": "tr", "full_name": "Ayşe Yılmaz",
        "email": "ayse@example.com", "phone": "+90 555 000 0000", "country": "TR",
        "city": "İstanbul", "birth_year": YEAR - 25,
        "accounts": [{"platform": "instagram", "url": "https://instagram.com/ayse.skin", "followers_claimed": 5200}],
        "niches": ["cilt bakımı", "vegan"], "skin_type": "karma",
        "recent_links": ["https://instagram.com/p/abc"], "why": "Vegan ürünleri seviyorum",
        "past_brands": "-", "accepted_model": "barter", "consent_ack": True,
        "consent_version": "v1", "website": "",
    }
    body.update(over)
    return body


def _submit(client: TestClient, **over):
    return _anon(client).post("/basvuru/gonder", json=_application_body(**over))


def _creator(ac: TestClient, name="Elif Kaya", email="elif@example.com", accounts=None) -> int:
    body = {"full_name": name, "email": email, "country": "TR", "language": "tr"}
    if accounts is not None:
        body["accounts"] = accounts
    r = ac.post(f"{API}/creators", headers=_H, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _account(ac: TestClient, cid: int, platform="instagram", handle="elifkaya", followers=8000) -> int:
    r = ac.post(f"{API}/creators/{cid}/accounts", headers=_H,
                json={"platform": platform, "handle": handle, "followers": followers})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _collab(ac: TestClient, cid: int, stage=None, market="TR") -> int:
    body = {"creator_id": cid, "store_key": "minerva", "market": market, "model": "barter"}
    if stage:
        body["stage"] = stage
    r = ac.post(f"{API}/collabs", headers=_H, json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _item(db, stock=10, cost=45.5, barcode="869111100901") -> Item:
    it = Item(name=f"Inf Ürün {barcode[-3:]}", sku=f"inf-{barcode[-5:]}", category="Bitmiş Ürün",
              unit="adet", current_stock=stock, cost_price=cost, barcode=barcode, domain="cosmetics")
    db.add(it); db.commit(); db.refresh(it)
    return it


def _shipment(ac: TestClient, collab_id: int, item_id: int, qty=2):
    return ac.post(f"{API}/collabs/{collab_id}/shipments", headers=_H,
                   json={"items": [{"item_id": item_id, "quantity": qty}], "recipient_name": "Elif Kaya",
                         "packaging_cost": 20, "shipping_cost": 80})


def _outputs(db, item_id):
    return db.query(Transaction).filter(Transaction.item_id == item_id,
                                        Transaction.transaction_type == "Output").all()


def _stock(db, item_id) -> float:
    db.expire_all()
    return float(db.query(Item).get(item_id).current_stock)


# ─── RBAC ───────────────────────────────────────────────────────────────────

def test_api_requires_login(client: TestClient):
    r = client.get(f"{API}/creators")
    assert r.status_code == 401


def test_labtech_forbidden(labtech_client: TestClient):
    assert labtech_client.get(f"{API}/creators").status_code == 403
    assert labtech_client.get(f"{API}/settings").status_code == 403
    r = labtech_client.post(f"{API}/creators", headers=_H, json={"full_name": "X Y"})
    assert r.status_code == 403


def test_superadmin_lists_and_page(authed_client: TestClient):
    assert authed_client.get(f"{API}/creators").json() == {"items": [], "count": 0}
    assert authed_client.get(f"{API}/collabs/board").status_code == 200
    r = authed_client.get("/influencer")
    assert r.status_code == 200 and "INF_CFG" in r.text


def test_page_redirects_without_permission(labtech_client: TestClient):
    r = labtech_client.get("/influencer", follow_redirects=False)
    assert r.status_code == 302


# ─── Public başvuru ─────────────────────────────────────────────────────────

def test_public_pages_render(client: TestClient):
    anon = _anon(client)
    assert anon.get("/basvuru").status_code == 200
    r = anon.get("/basvuru?lang=en")
    assert r.status_code == 200 and 'lang="en"' in r.text
    assert anon.get("/basvuru/t/olmayan-token").status_code == 404
    assert "Disallow: /basvuru/" in anon.get("/robots.txt").text


def test_public_application_creates_pending(client: TestClient, db_session):
    r = _submit(client)
    assert r.status_code == 201, r.text
    assert r.json()["ok"] is True
    a = db_session.query(InfluencerApplication).one()
    assert a.status == "pending" and a.email == "ayse@example.com" and a.store_key == "minerva"
    assert a.consent_text_version == "v1" and a.consent_at is not None and a.consent_ip
    assert a.honeypot_hit is False
    accs = __import__("json").loads(a.accounts_json)
    assert accs[0]["handle"] == "ayse.skin" and accs[0]["followers_claimed"] == 5200


def test_honeypot_silent_200_no_row(client: TestClient, db_session):
    r = _submit(client, website="http://spam.example")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert db_session.query(InfluencerApplication).count() == 0


def test_under_18_rejected(client: TestClient, db_session):
    r = _submit(client, birth_year=YEAR - 15)
    assert r.status_code == 400 and "18" in r.json()["detail"]
    assert db_session.query(InfluencerApplication).count() == 0


def test_missing_consent_and_no_accounts_rejected(client: TestClient):
    assert _submit(client, consent_ack=False).status_code == 400
    assert _submit(client, accounts=[]).status_code == 400
    assert _submit(client, email="bozuk").status_code == 400


def test_application_rate_limited_sixth_request(client: TestClient):
    """@limiter.limit('5/hour') — sayaç istek SAYISINA göre (sonuçtan bağımsız)."""
    statuses = [_submit(client, email=f"u{i}@example.com").status_code for i in range(6)]
    assert statuses[0] == 201
    assert statuses[-1] == 429


# ─── Onay → creator ─────────────────────────────────────────────────────────

def test_approve_creates_creator_with_accounts(client: TestClient, authed_client: TestClient, db_session):
    aid = _submit(client).json()["id"]
    lst = authed_client.get(f"{API}/applications?status=pending").json()
    assert lst["count"] == 1 and lst["items"][0]["existing_creator_id"] is None
    r = authed_client.post(f"{API}/applications/{aid}/decide", headers=_H, json={"decision": "approved"})
    assert r.status_code == 200, r.text
    cid = r.json()["creator_id"]
    c = authed_client.get(f"{API}/creators/{cid}").json()
    assert c["email"] == "ayse@example.com" and c["relationship_stage"] == "basvurdu"
    assert c["accounts"][0]["handle"] == "ayse.skin" and c["accounts"][0]["followers"] == 5200
    assert c["tier_key"] == "mikro"          # 5.200 takipçi → mikro (DEFAULT_TIERS)
    assert c["categories"] == ["cilt bakımı", "vegan"]
    # ikinci onay → 400
    r2 = authed_client.post(f"{API}/applications/{aid}/decide", headers=_H, json={"decision": "approved"})
    assert r2.status_code == 400


def test_approve_links_existing_creator_by_email(client: TestClient, authed_client: TestClient, db_session):
    cid = _creator(authed_client, name="Ayşe Yılmaz", email="AYSE@example.com")
    aid = _submit(client).json()["id"]
    lst = authed_client.get(f"{API}/applications").json()
    assert lst["items"][0]["existing_creator_id"] == cid       # duplicate uyarısı
    r = authed_client.post(f"{API}/applications/{aid}/decide", headers=_H, json={"decision": "approved"})
    assert r.status_code == 200 and r.json()["creator_id"] == cid
    assert db_session.query(InfluencerCreator).count() == 1


def test_reject_with_reason(client: TestClient, authed_client: TestClient, db_session):
    aid = _submit(client).json()["id"]
    r = authed_client.post(f"{API}/applications/{aid}/decide", headers=_H,
                           json={"decision": "rejected", "reject_reason": "Kitle hedef dışı"})
    assert r.status_code == 200 and r.json()["creator_id"] is None
    a = db_session.query(InfluencerApplication).get(aid)
    assert a.status == "rejected" and a.reject_reason == "Kitle hedef dışı" and a.reviewed_by


# ─── Metrik → skor ──────────────────────────────────────────────────────────

def test_metrics_produce_score_and_snapshot(authed_client: TestClient, db_session):
    cid = _creator(authed_client)
    aid = _account(authed_client, cid, followers=8000)
    posts = [{"likes": 400 + i * 5, "comments": 20, "views": 3000 + i * 50, "saves": 30, "shares": 10}
             for i in range(12)]
    r = authed_client.post(f"{API}/creators/{cid}/accounts/{aid}/metrics", headers=_H,
                           json={"followers": 8000, "posts": posts, "geo_target_pct": 80, "fake_pct": 5})
    assert r.status_code == 200, r.text
    b = r.json()
    assert 0 <= b["score"] <= 100 and b["stats"]["post_count"] == 12
    assert b["stats"]["er_follower"] is not None and b["tier_key"] == "mikro"
    assert set(b["components"]) and isinstance(b["flags"], list)
    assert db_session.query(InfluencerMetricSnapshot).filter_by(account_id=aid).count() == 1
    c = authed_client.get(f"{API}/creators/{cid}").json()
    assert c["authenticity_score"] == b["score"] and c["score"]["components"] == b["components"]
    assert c["accounts"][0]["er_follower"] == b["stats"]["er_follower"]
    # yeniden skorla (benchmark değişince)
    r2 = authed_client.post(f"{API}/creators/{cid}/score", headers=_H)
    assert r2.status_code == 200 and r2.json()["score"] == b["score"]


def test_duplicate_account_handle_409(authed_client: TestClient):
    c1 = _creator(authed_client, email="a@example.com")
    c2 = _creator(authed_client, name="Başka Kişi", email="b@example.com")
    _account(authed_client, c1, handle="ortak")
    r = authed_client.post(f"{API}/creators/{c2}/accounts", headers=_H,
                           json={"platform": "instagram", "handle": "@Ortak"})
    assert r.status_code == 409


# ─── Aşama makinesi ─────────────────────────────────────────────────────────

def test_invalid_transition_400(authed_client: TestClient):
    cid = _creator(authed_client)
    col = _collab(authed_client, cid)
    r = authed_client.post(f"{API}/collabs/{col}/stage", headers=_H, json={"stage": "kargoda"})
    assert r.status_code == 400 and "Geçersiz geçiş" in r.json()["detail"]
    assert authed_client.post(f"{API}/collabs/{col}/stage", headers=_H,
                              json={"stage": "yok_boyle"}).status_code == 400


def test_cancel_requires_reason(authed_client: TestClient, db_session):
    cid = _creator(authed_client)
    col = _collab(authed_client, cid)
    r = authed_client.post(f"{API}/collabs/{col}/stage", headers=_H, json={"stage": "iptal"})
    assert r.status_code == 400 and "gerekçe" in r.json()["detail"].lower()
    r = authed_client.post(f"{API}/collabs/{col}/stage", headers=_H,
                           json={"stage": "iptal", "reason": "Cevap vermedi"})
    assert r.status_code == 200 and r.json()["stage"] == "iptal"
    logs = db_session.query(InfluencerCollabStageLog).filter_by(collab_id=col).order_by(InfluencerCollabStageLog.id).all()
    assert [l.to_stage for l in logs] == ["teklif_gonderildi", "iptal"] and logs[-1].reason == "Cevap vermedi"


def test_valid_transition_logs_and_board(authed_client: TestClient):
    cid = _creator(authed_client)
    col = _collab(authed_client, cid)
    r = authed_client.post(f"{API}/collabs/{col}/stage", headers=_H, json={"stage": "kabul_edildi"})
    assert r.status_code == 200 and r.json()["stage"] == "kabul_edildi"
    full = authed_client.get(f"{API}/collabs/{col}").json()
    assert full["code"].startswith(f"INF-{YEAR}-") and full["accepted_at"] is not None
    assert len(full["stage_log"]) == 2 and "adres_bekleniyor" in full["allowed_stages"]
    board = authed_client.get(f"{API}/collabs/board").json()
    col_map = {c["stage"]: c["count"] for c in board["columns"]}
    assert col_map["kabul_edildi"] == 1 and len(board["columns"]) == 14


# ─── Gönderim — Delivery(kargo), stok defteri ───────────────────────────────

def test_shipment_creates_kargo_delivery_without_stock_change(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10, cost=45.5)
    cid = _creator(authed_client)
    col = _collab(authed_client, cid, stage="kabul_edildi")
    r = _shipment(authed_client, col, it.id, qty=2)
    assert r.status_code == 201, r.text
    b = r.json()
    assert b["document_no"].startswith("KRG-") and b["stage"] == "urun_hazirlaniyor"
    assert b["cogs_total"] == 91.0 and b["loaded_cost"] == 191.0       # 2×45.5 + 20 + 80
    d = db_session.query(Delivery).get(b["delivery_id"])
    assert d.delivery_type == "kargo" and d.status == "preparing"
    assert "NUMUNEDİR" in (d.note or "")
    assert _stock(db_session, it.id) == 10 and len(_outputs(db_session, it.id)) == 0   # DÜŞMEDİ
    s = db_session.query(InfluencerShipment).get(b["id"])
    assert s.delivery_id == d.id and s.cogs_total == 91.0
    # belge kargolanmadan üretilmez
    assert authed_client.get(f"{API}/shipments/{b['id']}/document").status_code == 403


def test_ship_decrements_once_and_second_call_400(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10, barcode="869111100902")
    cid = _creator(authed_client)
    col = _collab(authed_client, cid, stage="kabul_edildi")
    sid = _shipment(authed_client, col, it.id, qty=3).json()["id"]
    r = authed_client.post(f"{API}/shipments/{sid}/ship", headers=_H,
                           json={"tracking_no": "YT777", "carrier": "Yurtiçi"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "shipped" and r.json()["stage"] == "kargoda"
    assert _stock(db_session, it.id) == 7
    tx = _outputs(db_session, it.id)
    assert len(tx) == 1 and tx[0].quantity == 3
    r2 = authed_client.post(f"{API}/shipments/{sid}/ship", headers=_H, json={"tracking_no": "YT778"})
    assert r2.status_code == 400
    assert _stock(db_session, it.id) == 7 and len(_outputs(db_session, it.id)) == 1   # TEK düşüm


def test_delivered_creates_two_reminder_tasks(authed_client: TestClient, db_session):
    it = _item(db_session, stock=10, barcode="869111100903")
    cid = _creator(authed_client)
    col = _collab(authed_client, cid, stage="kabul_edildi")
    sid = _shipment(authed_client, col, it.id, qty=1).json()["id"]
    # kargolanmadan teslim → 400
    assert authed_client.post(f"{API}/shipments/{sid}/delivered", headers=_H, json={}).status_code == 400
    authed_client.post(f"{API}/shipments/{sid}/ship", headers=_H, json={"tracking_no": "YT1"})
    r = authed_client.post(f"{API}/shipments/{sid}/delivered", headers=_H,
                           json={"delivered_on": "2026-09-01"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["stage"] == "icerik_bekleniyor" and len(b["task_ids"]) == 2
    assert b["reminder_dates"] == ["2026-09-08", "2026-09-15"]
    tasks = db_session.query(CrmTask).filter(CrmTask.influencer_collab_id == col).all()
    assert len(tasks) == 2 and all(t.status == "open" for t in tasks)
    s = db_session.query(InfluencerShipment).get(sid)
    assert s.reminder1_task_id in b["task_ids"] and s.reminder2_task_id in b["task_ids"]
    assert str(s.reminder1_due) == "2026-09-08"
    assert db_session.query(InfluencerCreator).get(cid).relationship_stage == "seeded"
    # ikinci teslim → 400
    assert authed_client.post(f"{API}/shipments/{sid}/delivered", headers=_H, json={}).status_code == 400
    # stok hâlâ tek düşüm
    assert _stock(db_session, it.id) == 9


def test_shipment_document_pdf_has_sample_notice(authed_client: TestClient, db_session):
    it = _item(db_session, stock=5, barcode="869111100904")
    cid = _creator(authed_client)
    col = _collab(authed_client, cid, stage="kabul_edildi")
    sid = _shipment(authed_client, col, it.id, qty=1).json()["id"]
    authed_client.post(f"{API}/shipments/{sid}/ship", headers=_H, json={"tracking_no": "YT2"})
    r = authed_client.get(f"{API}/shipments/{sid}/document")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    assert r.headers["content-type"].startswith("application/pdf")
    from io import BytesIO
    from pypdf import PdfReader
    text = "".join(p.extract_text() or "" for p in PdfReader(BytesIO(r.content)).pages)
    assert "SATILAMAZ" in text.upper() or "NOT FOR SALE" in text.upper()


def test_do_not_resend_blocks_shipment(authed_client: TestClient, db_session):
    it = _item(db_session, stock=5, barcode="869111100905")
    cid = _creator(authed_client)
    col = _collab(authed_client, cid, stage="kabul_edildi")
    r = authed_client.put(f"{API}/creators/{cid}", headers=_H,
                          json={"full_name": "Elif Kaya", "email": "elif@example.com", "do_not_resend": True})
    assert r.status_code == 200
    r = _shipment(authed_client, col, it.id)
    assert r.status_code == 400 and "tekrar gönderme" in r.json()["detail"]
    assert db_session.query(Delivery).count() == 0 and _stock(db_session, it.id) == 5


def test_shipment_requires_ship_permission(labtech_client: TestClient):
    assert labtech_client.post(f"{API}/collabs/1/shipments", headers=_H,
                               json={"items": [{"item_id": 1, "quantity": 1}]}).status_code == 403


# ─── Claim token ────────────────────────────────────────────────────────────

def test_claim_token_single_use(client: TestClient, authed_client: TestClient, db_session):
    cid = _creator(authed_client)
    col = _collab(authed_client, cid, stage="adres_bekleniyor")
    r = authed_client.post(f"{API}/creators/{cid}/tokens", headers=_H,
                           json={"purpose": "address", "collab_id": col})
    assert r.status_code == 201, r.text
    tok = r.json()["token"]
    assert r.json()["url"] == f"/basvuru/t/{tok}" and r.json()["state"] == "open"
    anon = _anon(client)
    page = anon.get(f"/basvuru/t/{tok}")
    assert page.status_code == 200 and 'data-purpose="address"' in page.text
    body = {"recipient": "Elif Kaya", "line1": "Bağdat Cd. 1", "district": "Kadıköy",
            "city": "İstanbul", "postal_code": "34700", "country": "TR", "phone": "+90 555"}
    r1 = anon.post(f"/basvuru/t/{tok}/adres", json=body)
    assert r1.status_code == 200, r1.text
    r2 = anon.post(f"/basvuru/t/{tok}/adres", json=body)
    assert r2.status_code == 410                                     # tek kullanım
    assert anon.get(f"/basvuru/t/{tok}").status_code == 410
    ad = db_session.query(InfluencerAddress).filter_by(creator_id=cid).one()
    assert ad.is_default and ad.verified_at is not None and ad.city == "İstanbul"
    t = db_session.query(InfluencerToken).filter_by(token=tok).one()
    assert t.used_at is not None
    assert authed_client.get(f"{API}/collabs/{col}").json()["stage"] == "urun_hazirlaniyor"
    # yanlış amaç: address token'ı onay ucunda → 410 (kullanılmış) / 400 (amaç)
    assert anon.post(f"/basvuru/t/{tok}/onay", json={"ack": True}).status_code in (400, 410)


def test_claim_agreement_sets_guideline_ack(client: TestClient, authed_client: TestClient, db_session):
    cid = _creator(authed_client)
    col = _collab(authed_client, cid)
    r = authed_client.post(f"{API}/creators/{cid}/tokens", headers=_H,
                           json={"purpose": "agreement", "collab_id": col})
    tok = r.json()["token"]
    anon = _anon(client)
    assert anon.post(f"/basvuru/t/{tok}/onay", json={"ack": False}).status_code == 400
    r1 = anon.post(f"/basvuru/t/{tok}/onay", json={"ack": True, "version": "2026-09"})
    assert r1.status_code == 200, r1.text
    full = authed_client.get(f"{API}/collabs/{col}").json()
    assert full["guideline_ack_at"] and full["guideline_version"] == "2026-09"
    assert full["stage"] == "kabul_edildi"
    # agreement token collab'sız açılamaz
    assert authed_client.post(f"{API}/creators/{cid}/tokens", headers=_H,
                              json={"purpose": "agreement"}).status_code == 400


def test_claim_insights_upload(client: TestClient, authed_client: TestClient, db_session):
    cid = _creator(authed_client)
    _account(authed_client, cid, followers=1000)
    tok = authed_client.post(f"{API}/creators/{cid}/tokens", headers=_H,
                             json={"purpose": "insights"}).json()["token"]
    anon = _anon(client)
    r = anon.post(f"/basvuru/t/{tok}/insights", files={"file": ("kayit.webm", WEBM, "video/webm")},
                  data={"reach_30d": "12000", "engaged_30d": "900", "followers": "8100", "geo_target_pct": "72"})
    assert r.status_code == 200, r.text
    c = authed_client.get(f"{API}/creators/{cid}").json()
    assert c["accounts"][0]["followers"] == 8100 and c["accounts"][0]["audience_geo"]["target_pct"] == 72
    assert c["files"][0]["kind"] == "insights_video"
    assert anon.post(f"/basvuru/t/{tok}/insights", files={"file": ("k.webm", WEBM, "video/webm")}).status_code == 410


# ─── Dosya yükleme ──────────────────────────────────────────────────────────

def test_upload_rejects_unknown_magic_bytes(authed_client: TestClient):
    cid = _creator(authed_client)
    r = authed_client.post(f"{API}/files/upload", headers=_H,
                           files={"file": ("x.png", b"GIF89a" + b"\x00" * 100, "image/png")},
                           data={"entity": "creator", "entity_id": str(cid), "kind": "screenshot"})
    assert r.status_code == 400


def test_upload_accepts_png_and_serves_as_attachment(authed_client: TestClient):
    cid = _creator(authed_client)
    r = authed_client.post(f"{API}/files/upload", headers=_H,
                           files={"file": ("ekran.png", PNG, "image/png")},
                           data={"entity": "creator", "entity_id": str(cid), "kind": "screenshot"})
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    assert r.json()["content_type"] == "image/png" and r.json()["size_bytes"] == len(PNG)
    g = authed_client.get(f"{API}/files/{fid}")
    assert g.status_code == 200 and g.content == PNG
    assert g.headers["content-type"].startswith("application/octet-stream")
    assert "attachment" in g.headers["content-disposition"]
    lst = authed_client.get(f"{API}/files?entity=creator&entity_id={cid}").json()
    assert lst["count"] == 1


def test_upload_rejects_oversize(authed_client: TestClient, monkeypatch):
    import core.influencer_files as F
    monkeypatch.setattr(F, "MAX_IMAGE_BYTES", 64)
    cid = _creator(authed_client)
    r = authed_client.post(f"{API}/files/upload", headers=_H,
                           files={"file": ("buyuk.png", PNG, "image/png")},
                           data={"entity": "creator", "entity_id": str(cid), "kind": "screenshot"})
    assert r.status_code == 400 and "büyük" in r.json()["detail"]


# ─── Ayarlar ────────────────────────────────────────────────────────────────

def test_settings_seeded(authed_client: TestClient):
    r = authed_client.get(f"{API}/settings")
    assert r.status_code == 200
    b = r.json()
    keys = [t["key"] for t in b["tiers"]]
    assert keys == ["nano", "mikro", "orta", "makro", "ambassador"]
    assert len(b["benchmarks"]) == 60
    assert b["settings"]["influencer.reminder_days"] == "7,14"
    assert "TR" in b["label_templates"] and b["consent_version"]


def test_settings_update_and_validation(authed_client: TestClient):
    r = authed_client.put(f"{API}/settings", headers=_H,
                          json={"settings": {"influencer.reminder_days": "5,10", "influencer.brand_handles": "@minerva108"}})
    assert r.status_code == 200 and r.json()["settings"]["influencer.reminder_days"] == "5,10"
    assert authed_client.put(f"{API}/settings", headers=_H,
                             json={"settings": {"influencer.forbidden_words": "([unclosed"}}).status_code == 400
    assert authed_client.put(f"{API}/settings", headers=_H,
                             json={"settings": {"bilinmeyen.anahtar": "x"}}).status_code == 400
    t = authed_client.put(f"{API}/settings/tiers/mikro", headers=_H, json={"commission_pct": 12})
    assert t.status_code == 200 and t.json()["tier"]["commission_pct"] == 12
    assert authed_client.put(f"{API}/settings/tiers/yok", headers=_H, json={}).status_code == 404
    bm = authed_client.put(f"{API}/settings/benchmarks", headers=_H,
                           json={"items": [{"platform": "instagram", "tier_key": "nano", "metric": "er_follower",
                                            "low": 4, "high": 10}]})
    assert bm.status_code == 200 and bm.json()["count"] == 1
    row = next(x for x in bm.json()["benchmarks"]
               if (x["platform"], x["tier_key"], x["metric"]) == ("instagram", "nano", "er_follower"))
    assert row["low"] == 4 and row["high"] == 10


# ─── İçerik ─────────────────────────────────────────────────────────────────

def test_content_flow_publish_compliance(authed_client: TestClient):
    cid = _creator(authed_client)
    col = _collab(authed_client, cid, stage="icerik_bekleniyor")
    r = authed_client.post(f"{API}/collabs/{col}/contents", headers=_H,
                           json={"platform": "instagram", "type": "reel", "caption": "taslak"})
    assert r.status_code == 201 and r.json()["stage"] == "taslak_incelemede"
    xid = r.json()["id"]
    assert authed_client.post(f"{API}/contents/{xid}/review", headers=_H,
                              json={"action": "revise"}).status_code == 400     # not zorunlu
    r = authed_client.post(f"{API}/contents/{xid}/review", headers=_H, json={"action": "approve"})
    assert r.status_code == 200 and r.json()["stage"] == "onaylandi"
    authed_client.put(f"{API}/settings", headers=_H, json={"settings": {"influencer.brand_handles": "@minerva108"}})
    r = authed_client.post(f"{API}/contents/{xid}/published", headers=_H,
                           json={"url": "https://instagram.com/reel/1",
                                 "caption": "Reklam — @minerva108 tarafından sağlandı\nCilt bakımı rutinim"})
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "yayinlandi" and r.json()["compliance"]["label_first_line"] is True
    assert r.json()["compliance_score"] >= 50
    r = authed_client.post(f"{API}/contents/{xid}/metrics", headers=_H,
                           json={"window": "d30", "metrics": {"views": 12000, "likes": 800}})
    assert r.status_code == 200 and r.json()["stage"] == "metrik_toplandi"
    r = authed_client.post(f"{API}/collabs/{col}/decision", headers=_H, json={"decision": "tekrarla"})
    assert r.status_code == 200 and r.json()["stage"] == "tamamlandi"


def test_creator_lookup_export_and_tags(authed_client: TestClient):
    cid = _creator(authed_client, name="Zeynep Demir", email="zeynep@example.com",
                   accounts=[{"platform": "tiktok", "handle": "zeydem", "followers": 30000}])
    assert authed_client.get(f"{API}/creators/lookup?q=zeydem").json()["items"][0]["id"] == cid
    r = authed_client.post(f"{API}/creators/{cid}/tags", headers=_H, json={"tags": ["vegan", "TikTok"]})
    assert r.status_code == 200 and sorted(r.json()["tags"]) == ["TikTok", "vegan"]
    x = authed_client.get(f"{API}/creators/export")
    assert x.status_code == 200 and x.content[:2] == b"PK"
    c = authed_client.get(f"{API}/creators/{cid}").json()
    assert c["tier_key"] == "orta" and c["tags"] == ["TikTok", "vegan"]
