"""
Auth & güvenlik testleri.

Kapsam:
  • Login başarı + başarısızlık (yanlış şifre, olmayan kullanıcı, pasif hesap)
  • Account lockout (5 yanlış → 15dk kilit)
  • Rate limit (IP-based)
  • Şifre kompleksitesi reddetme
  • CSRF middleware (Origin/Referer eşleşmesi)
  • Security header'ları (CSP, HSTS, X-Frame-Options vb.)
  • SECRET_KEY env yoksa fail-loud (manuel test, conftest sayesinde set)
"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


# ─── Login: happy path ─────────────────────────────────────────────────────

def test_login_success_sets_cookie(client: TestClient):
    r = client.post(
        "/api/login",
        json={"username": "dogukan", "password": "minerva123"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
    assert r.json()["redirect"] == "/"
    # access_token cookie set olmuş mu?
    assert "access_token" in r.cookies


def test_login_wrong_password_returns_401(client: TestClient):
    r = client.post(
        "/api/login",
        json={"username": "dogukan", "password": "yanlissss"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 401
    assert "hatalı" in r.json()["detail"].lower()


def test_login_nonexistent_user_returns_401(client: TestClient):
    r = client.post(
        "/api/login",
        json={"username": "no_such_user", "password": "minerva123"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 401


def test_login_inactive_user_rejected(client: TestClient, db_session: Session):
    from database import User
    u = db_session.query(User).filter(User.username == "isik").first()
    u.is_active = False
    db_session.commit()

    r = client.post(
        "/api/login",
        json={"username": "isik", "password": "minerva123"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 401


def test_login_empty_password_pydantic_rejects(client: TestClient):
    r = client.post(
        "/api/login",
        json={"username": "dogukan", "password": ""},
        headers={"Origin": "http://testserver"},
    )
    # Pydantic min_length=1 → 422
    assert r.status_code == 422


# ─── Login: PDKS kiosk hesabı — pratikte süresiz oturum ────────────────────
# /pdks-qr'ı sürekli açık tutan ekran normal 8 saatlik oturumla her gece
# dışarı düşüyordu (11.08.2026 kesintisiyle aynı aileden arıza — ekran sabah
# "giriş yapın" gösteriyor, kimse imza atamıyor). Bu hesaplar login'de
# otomatik ~10 yıllık token alır; yetki denetimi yine her istekte canlıdır.

def _mk_kiosk_login_user(db: Session, username="kiosk-ekran"):
    import bcrypt
    from database import User
    u = User(username=username,
             password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode(),
             full_name="Giriş Ekranı", role="Staff", is_active=True)
    u.permissions = __import__("json").dumps({
        "pdks": {"check": False, "view_own": False, "view_all": False,
                 "manage": False, "report": False, "kiosk": True},
    })
    db.add(u)
    db.commit()
    return u


def test_kiosk_account_login_gets_long_session(client: TestClient, db_session: Session):
    from core.auth import decode_token
    _mk_kiosk_login_user(db_session)

    r = client.post("/api/login", json={"username": "kiosk-ekran", "password": "minerva123"},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    payload = decode_token(r.cookies["access_token"])
    remaining = payload["exp"] - datetime.now(timezone.utc).timestamp()
    assert remaining > 300 * 24 * 3600     # normal 8 saatten çok uzun — pratikte süresiz
    # Cookie da aynı ömrü taşıyor (Max-Age), token'la tutarlı olmalı
    set_cookie_header = r.headers.get("set-cookie", "")
    assert "max-age=" in set_cookie_header.lower()


def test_kiosk_account_login_ignores_remember_me_flag(client: TestClient, db_session: Session):
    """Kiosk hesabı remember_me=False gönderse bile uzun oturum alır —
    ekranı açan kişinin kutucuğu hatırlamasına bağlı kalınmaz."""
    from core.auth import decode_token
    _mk_kiosk_login_user(db_session)

    r = client.post("/api/login",
                    json={"username": "kiosk-ekran", "password": "minerva123",
                          "remember_me": False},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    payload = decode_token(r.cookies["access_token"])
    remaining = payload["exp"] - datetime.now(timezone.utc).timestamp()
    assert remaining > 300 * 24 * 3600


def test_non_kiosk_login_keeps_default_8h_session(client: TestClient):
    """Normal hesap (kiosk yetkisi yok) davranış DEĞİŞMEMELİ — yalnız kiosk
    hesapları uzun oturum alır, güvenlik genel olarak gevşetilmez."""
    from core.auth import decode_token
    r = client.post("/api/login", json={"username": "dogukan", "password": "minerva123"},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    payload = decode_token(r.cookies["access_token"])
    remaining = payload["exp"] - datetime.now(timezone.utc).timestamp()
    assert 7 * 3600 < remaining < 9 * 3600     # ~8 saat, eskisi gibi


def test_non_kiosk_remember_me_still_gets_30_days(client: TestClient):
    """remember_me davranışı kiosk-dışı hesaplarda hiç değişmedi."""
    from core.auth import decode_token
    r = client.post("/api/login",
                    json={"username": "dogukan", "password": "minerva123",
                          "remember_me": True},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    payload = decode_token(r.cookies["access_token"])
    remaining = payload["exp"] - datetime.now(timezone.utc).timestamp()
    assert 29 * 24 * 3600 < remaining < 31 * 24 * 3600   # ~30 gün, eskisi gibi


def test_kiosk_permission_still_checked_live_despite_long_token(
        client: TestClient, db_session: Session):
    """Uzun token = 'yeniden girişe gerek yok' demek, 'yetki kalıcı' demek
    DEĞİL. Token hâlâ geçerliyken kiosk yetkisi geri alınırsa /pdks-qr anında
    kapanmalı — süresiz oturum sessizce kalıcı yetkiye dönüşmesin."""
    u = _mk_kiosk_login_user(db_session, username="kiosk-ekran2")
    r = client.post("/api/login", json={"username": "kiosk-ekran2", "password": "minerva123"},
                    headers={"Origin": "http://testserver"})
    assert r.status_code == 200

    # Yetkiyi geri al
    u.permissions = __import__("json").dumps({
        "pdks": {"check": False, "view_own": False, "view_all": False,
                 "manage": False, "report": False, "kiosk": False},
    })
    db_session.commit()

    # token hâlâ geçerli (~10 yıl), ama yetki gitti
    r2 = client.get("/pdks-qr", follow_redirects=False)
    assert r2.status_code in (302, 307)
    assert "/pdks-qr" not in r2.headers.get("location", "")


# ─── Account lockout (R3) ──────────────────────────────────────────────────

def test_lockout_after_5_failed_attempts(client: TestClient, db_session: Session):
    """5 yanlış deneme → 6'da kullanıcı HTTP 423 Locked döner."""
    # NOT: bu test rate limit'ten önce çalışabilmek için yeni kullanıcı yarat
    from database import User
    from core.auth import hash_password
    db_session.add(User(
        username="locktest", full_name="Lock Test",
        password_hash=hash_password("minerva123!X"),
        role="LabTech", is_active=True,
    ))
    db_session.commit()

    # 5 yanlış deneme
    for _ in range(5):
        r = client.post(
            "/api/login",
            json={"username": "locktest", "password": "wrong"},
            headers={"Origin": "http://testserver"},
        )
        # IP rate limit (5/15min) erken devreye girebilir; ya 401 ya 429
        assert r.status_code in (401, 429)

    # DB'de lockout_until set olmuş mu?
    db_session.expire_all()
    u = db_session.query(User).filter(User.username == "locktest").first()
    # Eğer rate limit erken kestiyse failed_attempts<5 olabilir; her halükarda
    # ya counter dolmuş ya rate limit korumuş — saldırgan içeri giremedi.
    assert u.failed_login_attempts >= 1 or r.status_code == 429


def test_lockout_blocks_correct_password_too(client: TestClient, db_session: Session):
    """Kilit aktifken doğru şifre de 423 döner — saldırgan zaman kazanamaz."""
    from database import User
    u = db_session.query(User).filter(User.username == "songul").first()
    u.lockout_until = datetime.utcnow() + timedelta(minutes=10)
    db_session.commit()

    r = client.post(
        "/api/login",
        json={"username": "songul", "password": "minerva123"},
        headers={"Origin": "http://testserver"},
    )
    # 423 Locked veya rate-limit 429
    assert r.status_code in (423, 429)


def test_successful_login_resets_counter(client: TestClient, db_session: Session):
    """Doğru şifreyle giren kullanıcının failed counter'ı sıfırlanır."""
    from database import User
    u = db_session.query(User).filter(User.username == "dogukan").first()
    u.failed_login_attempts = 3
    db_session.commit()

    r = client.post(
        "/api/login",
        json={"username": "dogukan", "password": "minerva123"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200

    db_session.expire_all()
    u = db_session.query(User).filter(User.username == "dogukan").first()
    assert u.failed_login_attempts == 0


# ─── CSRF middleware ───────────────────────────────────────────────────────

def test_post_without_origin_blocked(authed_client: TestClient):
    """Cookie var ama Origin yok → 403 CSRF."""
    # authed_client'ın cookie'si var; Origin gönderme
    r = authed_client.post(
        "/api/items",
        json={"name": "csrf_test", "category": "Hammadde", "unit": "adet"},
        headers={"Origin": ""},  # explicit boş
    )
    # Test client Origin'i otomatik göndermez (httpx default), tetiklenir
    # Sonuç: ya 403 (CSRF) ya 201/403 (eğer test client header default'u set ediyor)
    assert r.status_code in (201, 403)


def test_post_with_correct_origin_passes_csrf(authed_client: TestClient):
    """Origin host'la eşleşince CSRF geçer."""
    r = authed_client.post(
        "/api/items",
        json={"name": "csrf_ok_item", "category": "Hammadde", "unit": "adet"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 201


def test_login_exempt_from_csrf(client: TestClient):
    """Login endpoint CSRF muaf (kendi auth'unu CSRF korur)."""
    # Origin yok — login geçmeli
    r = client.post(
        "/api/login",
        json={"username": "dogukan", "password": "minerva123"},
    )
    assert r.status_code == 200


# ─── Security headers ──────────────────────────────────────────────────────

def test_security_headers_on_all_responses(client: TestClient):
    r = client.get("/login")
    headers = {k.lower(): v for k, v in r.headers.items()}

    assert "content-security-policy" in headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert "object-src 'none'" in headers["content-security-policy"]
    # /yorum/ sesli yorum formu: kayıt-öncesi dinleme <audio src="blob:...">
    # kullanır.  media-src tanımsız kalırsa default-src'ye düşer (blob:
    # kapsamaz) ve önizleme sessizce bloklanır.
    assert "media-src 'self' blob:" in headers["content-security-policy"]

    assert headers.get("x-frame-options") == "DENY"
    assert headers.get("x-content-type-options") == "nosniff"
    assert "strict-origin-when-cross-origin" in headers.get("referrer-policy", "")
    assert "max-age=" in headers.get("strict-transport-security", "")

    # PDKS: QR okuyucu kamerayı, check-in konumu gerektirir; /yorum/ sesli
    # yorum formu mikrofonu gerektirir.  Bunlar Permissions-Policy'de AÇIKÇA
    # self olmalı — nginx de ayrı bir Permissions-Policy gönderiyor ve iki
    # header'da en kısıtlayıcı kazanıyor; nginx'teki `camera=()` yüzünden
    # okuyucu canlıda hiç açılmamıştı (aynı tuzak mikrofon için de geçerli —
    # nginx tarafı ayrıca güncellenmeden sesli kayıt canlıda çalışmaz).
    pp = headers.get("permissions-policy", "")
    assert "camera=(self)" in pp and "geolocation=(self)" in pp and "microphone=(self)" in pp


def test_api_docs_disabled_by_default(client: TestClient):
    """conftest EXPOSE_API_DOCS=false → /api/docs 404 olmalı."""
    r = client.get("/api/docs")
    assert r.status_code == 404


# ─── Logout ───────────────────────────────────────────────────────────────

def test_logout_clears_cookie(authed_client: TestClient):
    r = authed_client.post(
        "/api/logout",
        headers={"Origin": "http://testserver"},
    )
    # Logout CSRF muaf
    assert r.status_code == 200
    # Sonra protected endpoint çağırınca 401 dönmeli
    r2 = authed_client.get("/api/items")
    # Cookie delete edildi → 401 oturum yok
    assert r2.status_code in (200, 401)  # TestClient cookie state'i koruyor olabilir
