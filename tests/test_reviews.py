"""
Ürün yorumları — Faz C1: veri modeli + davet üretimi.

Kapsam: RBAC (reviews kategorisi), davet oluşturma/listeleme, token
tekilliği, giriş doğrulama.  Moderasyon/yayın/ses yükleme sonraki
fazlarda ayrı test dosyalarında eklenecek (henüz uç yok).
"""
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from database import ReviewInvite

_HDR = {"Origin": "http://testserver"}


def _invite_body(**overrides):
    body = {
        "kind": "gifted",
        "store_key": "minerva",
        "shopify_product_id": 8023875190832,
        "product_title": "Red Clover Night Cream",
        "product_handle": "minerva108-red-clover-night-cream",
        "recipient_name": "Test Alıcı",
        "recipient_email": "test@example.com",
        "locale": "tr",
    }
    body.update(overrides)
    return body


# ─── RBAC ────────────────────────────────────────────────────────────────

def test_labtech_cannot_create_invite(labtech_client: TestClient):
    r = labtech_client.post("/api/reviews/invites", json=_invite_body(), headers=_HDR)
    assert r.status_code == 403


def test_labtech_cannot_view_invites(labtech_client: TestClient):
    r = labtech_client.get("/api/reviews/invites")
    assert r.status_code == 403


def test_labtech_cannot_view_reviews(labtech_client: TestClient):
    r = labtech_client.get("/api/reviews")
    assert r.status_code == 403


def test_anonymous_cannot_create_invite(client: TestClient):
    r = client.post("/api/reviews/invites", json=_invite_body(), headers=_HDR)
    assert r.status_code == 401


# ─── Davet oluşturma (SuperAdmin — her zaman yetkili) ───────────────────

def test_superadmin_creates_invite(authed_client: TestClient, db_session: Session):
    r = authed_client.post("/api/reviews/invites", json=_invite_body(), headers=_HDR)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["kind"] == "gifted"
    assert data["store_key"] == "minerva"
    assert data["shopify_product_id"] == 8023875190832
    assert data["product_title"] == "Red Clover Night Cream"
    assert data["used_at"] is None
    assert data["is_expired"] is False
    assert len(data["token"]) >= 32   # secrets.token_urlsafe(32) ≈ 43 karakter
    assert data["created_by"] == "dogukan"   # authed_client fixture'ı bu kullanıcıyla login olur

    row = db_session.query(ReviewInvite).filter(ReviewInvite.id == data["id"]).first()
    assert row is not None
    assert row.expires_at > datetime.utcnow()


def test_invite_tokens_are_unique(authed_client: TestClient):
    r1 = authed_client.post("/api/reviews/invites", json=_invite_body(), headers=_HDR)
    r2 = authed_client.post("/api/reviews/invites", json=_invite_body(), headers=_HDR)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["token"] != r2.json()["token"]


def test_created_invite_appears_in_list(authed_client: TestClient):
    created = authed_client.post("/api/reviews/invites", json=_invite_body(), headers=_HDR).json()
    r = authed_client.get("/api/reviews/invites")
    assert r.status_code == 200
    tokens = [inv["token"] for inv in r.json()["invites"]]
    assert created["token"] in tokens


def test_only_gifted_kind_accepted_in_v1(authed_client: TestClient):
    r = authed_client.post(
        "/api/reviews/invites",
        json=_invite_body(kind="verified_buyer"),
        headers=_HDR,
    )
    assert r.status_code == 400


def test_invalid_product_id_rejected(authed_client: TestClient):
    r = authed_client.post(
        "/api/reviews/invites",
        json=_invite_body(shopify_product_id=0),
        headers=_HDR,
    )
    assert r.status_code == 422   # pydantic Field(gt=0)


def test_optional_fields_can_be_blank(authed_client: TestClient):
    """Ürün başlığı/handle/alıcı bilgisi olmadan da davet üretilebilmeli —
    v1'de bunlar admin ekranında elle giriliyor, hepsi zorunlu değil."""
    r = authed_client.post(
        "/api/reviews/invites",
        json={
            "kind": "gifted",
            "store_key": "minerva",
            "shopify_product_id": 8023875190832,
        },
        headers=_HDR,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["product_title"] is None
    assert data["locale"] == "tr"   # default


# ─── Yorum listesi (henüz boş — moderasyon ucu yok) ─────────────────────

def test_reviews_list_empty_by_default(authed_client: TestClient):
    r = authed_client.get("/api/reviews")
    assert r.status_code == 200
    assert r.json() == {"reviews": []}


# ─── Sayfa rotası ─────────────────────────────────────────────────────────

def test_reviews_page_requires_login(client: TestClient):
    r = client.get("/yorumlar", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_reviews_page_renders_for_superadmin(authed_client: TestClient):
    r = authed_client.get("/yorumlar")
    assert r.status_code == 200
    assert "Ürün Yorumları" in r.text


def test_reviews_page_redirects_labtech(labtech_client: TestClient):
    r = labtech_client.get("/yorumlar", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/"
