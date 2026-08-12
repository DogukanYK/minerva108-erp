"""
Ürün yorumları — public yüzey (Faz C2): davet formu + ses yükleme.

Auth YOK — bu uç herkese açık, bu yüzden saldırı yüzeyi test edilir:
sihirli-bayt reddi, akış-ortası boyut reddi, tek-kullanım kilidi, süre
dolması, rıza zorunluluğu, rate limit.  core.product_images'ın public
saldırı süitiyle aynı yaklaşım (tests/test_product_images.py).
"""
import io
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from database import ProductReview, ReviewInvite

_HDR = {"Origin": "http://testserver"}
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 64          # geçerli WebM sihirli baytı
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64          # ses değil — reddedilmeli


@pytest.fixture(autouse=True)
def _tmp_review_audio_dir(tmp_path, monkeypatch):
    """Her test kendi dizininde çalışsın — core.reviews.REVIEW_AUDIO_DIR
    core/drive.py'nin DRIVE_DIR'ı gibi IMPORT ZAMANINDA sabitlenen bir
    modül sabiti (core/product_images.images_dir() gibi tembel/env-okuyan
    bir fonksiyon DEĞİL), bu yüzden env var testler arası etkisiz kalır —
    modül özniteliğini doğrudan monkeypatch'lemek gerekir."""
    import core.reviews as R
    monkeypatch.setattr(R, "REVIEW_AUDIO_DIR", tmp_path / "review_audio")
    (tmp_path / "review_audio").mkdir(parents=True, exist_ok=True)
    yield


def _create_invite(authed_client: TestClient) -> dict:
    r = authed_client.post(
        "/api/reviews/invites",
        json={
            "kind": "gifted",
            "store_key": "minerva",
            "shopify_product_id": 8023875190832,
            "product_title": "Red Clover Night Cream",
            "recipient_name": "Test Alıcı",
        },
        headers=_HDR,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _submit(client: TestClient, token: str, *, audio=None, **fields):
    """DİKKAT: conftest'te authed_client == client (aynı nesne, aynı cookie
    jar'ı — authed_client fixture'ı client'ın kendisine login yapıp aynı
    referansı döner).  Bir testte hem authed_client'la davet oluşturup hem
    client'la "anonim" gönderim yapılırsa, o "client" aslında SuperAdmin'in
    access_token çerezini taşır → CSRFMiddleware Origin kontrolüne düşer
    (çerez varken Origin/Referer eşleşmesi aranıyor) → sahte 403.
    Bu yüzden HER gönderim, app'i PAYLAŞAN ama cookie jar'ı BOŞ taze bir
    TestClient ile yapılır — tests/test_product_images.py'deki
    TestClient(client.app) deseniyle aynı çözüm."""
    anon = TestClient(client.app)
    form = {"rating": "5", "author_name": "Anonim Alıcı", "consent": "on"}
    form.update(fields)
    files = None
    if audio is not None:
        files = {"audio": audio}
    return anon.post(f"/yorum/{token}/gonder", data=form, files=files, follow_redirects=False)


# ─── GET /yorum/{token} — state makinesi ───────────────────────────────────

def test_unknown_token_404(client: TestClient):
    r = client.get("/yorum/does-not-exist")
    assert r.status_code == 404
    assert "Bulunamadı" in r.text


def test_open_invite_shows_form(client: TestClient, authed_client: TestClient):
    inv = _create_invite(authed_client)
    r = client.get(f"/yorum/{inv['token']}")
    assert r.status_code == 200
    assert 'id="reviewForm"' in r.text
    assert "Red Clover Night Cream" in r.text


def test_expired_invite_returns_410(client: TestClient, authed_client: TestClient, db_session: Session):
    inv = _create_invite(authed_client)
    row = db_session.query(ReviewInvite).filter(ReviewInvite.token == inv["token"]).first()
    row.expires_at = datetime.utcnow() - timedelta(days=1)
    db_session.commit()

    r = client.get(f"/yorum/{inv['token']}")
    assert r.status_code == 410
    assert "Süresi doldu" in r.text


def test_robots_disallows_yorum(client: TestClient):
    r = client.get("/robots.txt")
    assert "Disallow: /yorum/" in r.text


# ─── POST /yorum/{token}/gonder — gönderim + tek kullanım ──────────────────

def test_successful_submission_marks_invite_used(client: TestClient, authed_client: TestClient, db_session: Session):
    inv = _create_invite(authed_client)
    r = _submit(client, inv["token"], body="Harika bir ürün!")
    assert r.status_code == 303
    assert r.headers["location"] == f"/yorum/{inv['token']}"

    row = db_session.query(ReviewInvite).filter(ReviewInvite.token == inv["token"]).first()
    assert row.used_at is not None

    review = db_session.query(ProductReview).filter(ProductReview.invite_id == row.id).first()
    assert review is not None
    assert review.status == "pending"
    assert review.body == "Harika bir ürün!"
    assert review.rating == 5
    assert review.source == "gifted"
    assert review.consent_voice is False
    assert review.consent_text_version == "v1"


def test_used_invite_shows_thank_you(client: TestClient, authed_client: TestClient):
    inv = _create_invite(authed_client)
    r = client.get(f"/yorum/{inv['token']}")
    assert 'id="reviewForm"' in r.text   # önce hâlâ açık

    _submit(client, inv["token"], body="ilk gönderim")
    r2 = client.get(f"/yorum/{inv['token']}")
    assert r2.status_code == 200
    assert "Teşekkürler" in r2.text
    assert 'id="reviewForm"' not in r2.text


def test_reused_token_rejected_and_no_second_review(client: TestClient, authed_client: TestClient, db_session: Session):
    inv = _create_invite(authed_client)
    _submit(client, inv["token"], body="ilk gönderim")

    r2 = _submit(client, inv["token"], body="ikinci deneme")
    assert r2.status_code == 410

    reviews = db_session.query(ProductReview).filter(ProductReview.invite_id ==
        db_session.query(ReviewInvite).filter(ReviewInvite.token == inv["token"]).first().id).all()
    assert len(reviews) == 1   # ikinci gönderim yorum ÜRETMEDİ


def test_unknown_token_submission_404(client: TestClient):
    r = _submit(client, "does-not-exist", body="x")
    assert r.status_code == 404


def test_missing_consent_rejected(client: TestClient, authed_client: TestClient, db_session: Session):
    inv = _create_invite(authed_client)
    r = _submit(client, inv["token"], body="yorum var ama onay yok", consent="")
    assert r.status_code == 400
    assert "onay" in r.text.lower()
    row = db_session.query(ReviewInvite).filter(ReviewInvite.token == inv["token"]).first()
    assert row.used_at is None   # davet YANMADI


def test_empty_body_and_no_audio_rejected(client: TestClient, authed_client: TestClient):
    inv = _create_invite(authed_client)
    r = _submit(client, inv["token"], body="")
    assert r.status_code == 400
    assert "sesli not" in r.text.lower()


def test_invalid_rating_rejected(client: TestClient, authed_client: TestClient):
    inv = _create_invite(authed_client)
    r = _submit(client, inv["token"], body="yorum", rating="7")
    assert r.status_code == 400


def test_missing_name_rejected(client: TestClient, authed_client: TestClient):
    inv = _create_invite(authed_client)
    r = _submit(client, inv["token"], body="yorum", author_name="  ")
    assert r.status_code == 400


# ─── Ses yükleme — sihirli bayt + boyut ────────────────────────────────────

def test_audio_with_valid_magic_bytes_accepted(client: TestClient, authed_client: TestClient, db_session: Session):
    inv = _create_invite(authed_client)
    r = _submit(client, inv["token"], audio=("kayit.webm", io.BytesIO(WEBM), "audio/webm"))
    assert r.status_code == 303

    review = db_session.query(ProductReview).filter(ProductReview.invite_id ==
        db_session.query(ReviewInvite).filter(ReviewInvite.token == inv["token"]).first().id).first()
    assert review.audio_stored_name is not None
    assert review.audio_stored_name.endswith(".webm")
    assert review.audio_mime == "audio/webm"
    assert review.consent_voice is True

    from core.reviews import audio_path
    assert audio_path(review.audio_stored_name).is_file()


def test_audio_duration_stored_when_audio_present(client: TestClient, authed_client: TestClient, db_session: Session):
    inv = _create_invite(authed_client)
    r = _submit(client, inv["token"], audio=("kayit.webm", io.BytesIO(WEBM), "audio/webm"),
                audio_duration_s="47")
    assert r.status_code == 303

    review = db_session.query(ProductReview).filter(ProductReview.invite_id ==
        db_session.query(ReviewInvite).filter(ReviewInvite.token == inv["token"]).first().id).first()
    assert review.audio_duration_s == 47


def test_audio_duration_ignored_without_audio_file(client: TestClient, authed_client: TestClient, db_session: Session):
    """audio_duration_s ses dosyası OLMADAN gönderilirse yok sayılmalı —
    süre yalnız gerçekten kaydedilmiş bir dosyayla anlamlı, sahte/tutarsız
    veri DB'ye yazılmamalı."""
    inv = _create_invite(authed_client)
    r = _submit(client, inv["token"], body="Yazılı yorum, ses yok.", audio_duration_s="47")
    assert r.status_code == 303

    review = db_session.query(ProductReview).filter(ProductReview.invite_id ==
        db_session.query(ReviewInvite).filter(ReviewInvite.token == inv["token"]).first().id).first()
    assert review.audio_duration_s is None


def test_audio_with_fake_extension_rejected_by_magic_bytes(client: TestClient, authed_client: TestClient, db_session: Session):
    """.webm uzantılı ama içeriği PNG olan dosya — uzantı yalanı, sihirli
    bayt kontrolü yakalamalı (core.product_images.is_jpeg'in aynı gerekçesi)."""
    inv = _create_invite(authed_client)
    r = _submit(client, inv["token"], audio=("sahte.webm", io.BytesIO(PNG), "audio/webm"))
    assert r.status_code == 400
    assert "tanınmadı" in r.text.lower()

    row = db_session.query(ReviewInvite).filter(ReviewInvite.token == inv["token"]).first()
    assert row.used_at is None   # reddedilen ses davet YAKMADI
    assert db_session.query(ProductReview).filter(ProductReview.invite_id == row.id).count() == 0


def test_oversized_audio_rejected_mid_stream(client: TestClient, authed_client: TestClient, db_session: Session, monkeypatch):
    """Akış ortasında boyut aşımı — dosya diske tam yazılmadan kesilmeli."""
    import core.reviews as R
    monkeypatch.setattr(R, "MAX_UPLOAD_BYTES", 100)   # test için düşür

    inv = _create_invite(authed_client)
    big = WEBM + b"\x00" * 500
    r = _submit(client, inv["token"], audio=("buyuk.webm", io.BytesIO(big), "audio/webm"))
    assert r.status_code == 400
    assert "büyük" in r.text.lower()

    row = db_session.query(ReviewInvite).filter(ReviewInvite.token == inv["token"]).first()
    assert row.used_at is None
    # Reddedilen dosya diskte KALMAMALI (save_audio path.unlink ile temizler).
    import os
    assert len(os.listdir(R.REVIEW_AUDIO_DIR)) == 0


# ─── Rate limit ─────────────────────────────────────────────────────────────

def test_submission_rate_limited_after_five_per_hour(client: TestClient, authed_client: TestClient):
    """DİKKAT — slowapi varsayılanı `key_style="url"`dir: sayaç route
    ŞABLONUNA değil, isteğin GERÇEK/çözülmüş yoluna göre tutulur
    (slowapi/extension.py: `endpoint_url = request["path"]`).  Yani
    /yorum/AAA/gonder ve /yorum/BBB/gonder birbirinden BAĞIMSIZ sayaçlardır
    — farklı token'larla gelen bir sel bu dekoratörle YAKALANMAZ. Bu yüzden
    testin kendisi de AYNI token'a tekrar tekrar POST atarak gerçek
    davranışı doğruluyor (farklı davetlerle değil).
    Asıl (URL'den bağımsız, IP bazlı) koruma nginx `limit_req` katmanında —
    bkz. routers.reviews.submit_review'daki not. Bu dekoratör yalnız aynı
    linki tekrar tekrar deneyen birine karşı ikincil bir savunma."""
    inv = _create_invite(authed_client)
    statuses = []
    for _ in range(6):
        r = _submit(client, inv["token"], body="tekrar deneme")
        statuses.append(r.status_code)
    # İlk istek başarılı (303), sonrakiler davet zaten kullanıldığı için 410
    # — ama sayaç REQUEST SAYISINA göre işler (sonucun durumuna bakmaksızın),
    # bu yüzden 6. istekte 429 görülmeli.
    assert statuses[0] == 303
    assert statuses[-1] == 429


# ─── CSRF — anonim gönderim Origin header'ı olmadan geçmeli ────────────────

def test_anonymous_submission_succeeds_without_origin_header(client: TestClient, authed_client: TestClient):
    """CSRFMiddleware, access_token cookie'si olmayan istekleri muaf tutar
    (bkz. api_main.CSRFMiddleware) — anonim yorum gönderen ziyaretçide bu
    cookie hiç yok.  Gerçekten çerezsiz bir client kullanılmalı (bkz. _submit
    docstring'i — authed_client ile client aynı cookie jar'ını paylaşır)."""
    inv = _create_invite(authed_client)
    anon = TestClient(client.app)
    r = anon.post(
        f"/yorum/{inv['token']}/gonder",
        data={"rating": "5", "author_name": "Origin'siz Test", "body": "origin yok", "consent": "on"},
        follow_redirects=False,
    )
    assert r.status_code == 303
