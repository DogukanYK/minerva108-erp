"""
Ürün yorumları — Faz C1 (veri modeli + davet üretimi) + Faz C3
(moderasyon: dinle/transkript/onay/red).

Ses yükleme + tek-kullanım kilidi + public saldırı yüzeyi
tests/test_reviews_public.py'de.  Yayın (Shopify'a itme) henüz yok —
approve yalnız durumu değiştirir, bkz. routers/reviews.py docstring'i.
"""
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from database import ProductReview, ReviewInvite

_HDR = {"Origin": "http://testserver"}


def _make_review(db: Session, *, has_audio=False, status="pending", **overrides) -> ProductReview:
    """Moderasyon testleri için doğrudan DB'ye bir davet+yorum çifti yazar —
    submission akışı tests/test_reviews_public.py'de zaten ayrıca test
    edildiği için burada kısayoldan gidilir."""
    import secrets as _secrets
    inv = ReviewInvite(
        token="test-token-" + _secrets.token_hex(8),
        kind="gifted", store_key="minerva", shopify_product_id=8023875190832,
        product_title="Red Clover Night Cream",
        expires_at=datetime(2099, 1, 1), used_at=datetime.utcnow(),
    )
    db.add(inv)
    db.flush()
    r = ProductReview(
        invite_id=inv.id, store_key="minerva", shopify_product_id=8023875190832,
        product_title="Red Clover Night Cream", source="gifted",
        author_name="Test Yazar", rating=5, body="Harika bir ürün." if not has_audio else None,
        locale="tr", status=status,
        audio_stored_name=("fake.webm" if has_audio else None),
        audio_mime=("audio/webm" if has_audio else None),
        consent_voice=has_audio, consent_text_version="v1",
    )
    for k, v in overrides.items():
        setattr(r, k, v)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


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


# ─── Moderasyon — onay ────────────────────────────────────────────────────

def test_approve_text_only_review_no_transcript_needed(authed_client: TestClient, db_session: Session):
    r = _make_review(db_session, has_audio=False)
    resp = authed_client.post(f"/api/reviews/{r.id}/approve", json={}, headers=_HDR)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "approved"
    assert data["transcript"] is None


def test_approve_audio_review_without_transcript_rejected(authed_client: TestClient, db_session: Session):
    r = _make_review(db_session, has_audio=True)
    resp = authed_client.post(f"/api/reviews/{r.id}/approve", json={}, headers=_HDR)
    assert resp.status_code == 400
    assert "transkript" in resp.text.lower()

    db_session.refresh(r)
    assert r.status == "pending"   # onay uygulanmadı


def test_approve_audio_review_with_transcript_succeeds(authed_client: TestClient, db_session: Session):
    r = _make_review(db_session, has_audio=True)
    resp = authed_client.post(
        f"/api/reviews/{r.id}/approve",
        json={"transcript": "Ürünü çok beğendim, harika kokuyor."},
        headers=_HDR,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "approved"
    assert data["transcript"] == "Ürünü çok beğendim, harika kokuyor."


def test_approve_already_approved_review_rejected(authed_client: TestClient, db_session: Session):
    r = _make_review(db_session, status="approved")
    resp = authed_client.post(f"/api/reviews/{r.id}/approve", json={}, headers=_HDR)
    assert resp.status_code == 400


def test_approve_unknown_review_404(authed_client: TestClient):
    resp = authed_client.post("/api/reviews/999999/approve", json={}, headers=_HDR)
    assert resp.status_code == 404


def test_labtech_cannot_approve(labtech_client: TestClient, db_session: Session):
    r = _make_review(db_session)
    resp = labtech_client.post(f"/api/reviews/{r.id}/approve", json={}, headers=_HDR)
    assert resp.status_code == 403


# ─── Moderasyon — red ─────────────────────────────────────────────────────

def test_reject_requires_note(authed_client: TestClient, db_session: Session):
    r = _make_review(db_session)
    resp = authed_client.post(f"/api/reviews/{r.id}/reject", json={"moderation_note": ""}, headers=_HDR)
    assert resp.status_code in (400, 422)

    db_session.refresh(r)
    assert r.status == "pending"


def test_reject_whitespace_only_note_rejected(authed_client: TestClient, db_session: Session):
    r = _make_review(db_session)
    resp = authed_client.post(f"/api/reviews/{r.id}/reject", json={"moderation_note": "   "}, headers=_HDR)
    assert resp.status_code == 400


def test_reject_with_note_succeeds(authed_client: TestClient, db_session: Session):
    r = _make_review(db_session)
    resp = authed_client.post(
        f"/api/reviews/{r.id}/reject",
        json={"moderation_note": "Spam şüphesi — aynı metin başka üründe de gönderilmiş."},
        headers=_HDR,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "rejected"
    assert data["moderation_note"] == "Spam şüphesi — aynı metin başka üründe de gönderilmiş."


def test_labtech_cannot_reject(labtech_client: TestClient, db_session: Session):
    r = _make_review(db_session)
    resp = labtech_client.post(f"/api/reviews/{r.id}/reject", json={"moderation_note": "x"}, headers=_HDR)
    assert resp.status_code == 403


# ─── Dinleme (sesli not) ──────────────────────────────────────────────────

def test_audio_endpoint_404_when_no_audio(authed_client: TestClient, db_session: Session):
    r = _make_review(db_session, has_audio=False)
    resp = authed_client.get(f"/api/reviews/{r.id}/audio")
    assert resp.status_code == 404


def test_audio_endpoint_404_when_file_missing_from_disk(authed_client: TestClient, db_session: Session):
    """DB'de audio_stored_name var ama disk'te dosya yok — 404, 500 değil."""
    r = _make_review(db_session, has_audio=True)
    resp = authed_client.get(f"/api/reviews/{r.id}/audio")
    assert resp.status_code == 404


def test_labtech_cannot_access_audio(labtech_client: TestClient, db_session: Session):
    r = _make_review(db_session, has_audio=True)
    resp = labtech_client.get(f"/api/reviews/{r.id}/audio")
    assert resp.status_code == 403


def test_audio_endpoint_serves_real_file(authed_client: TestClient, db_session: Session, tmp_path, monkeypatch):
    import core.reviews as R
    monkeypatch.setattr(R, "REVIEW_AUDIO_DIR", tmp_path)
    stored = "real-test-audio.webm"
    (tmp_path / stored).write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 32)

    r = _make_review(db_session, has_audio=True, audio_stored_name=stored, audio_mime="audio/webm")
    resp = authed_client.get(f"/api/reviews/{r.id}/audio")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/webm"
    assert resp.content == b"\x1a\x45\xdf\xa3" + b"\x00" * 32
