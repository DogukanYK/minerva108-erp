"""
Pytest fixture'ları — paylaşılan test altyapısı.

Stratejimiz:
  • Test DB = ayrı PostgreSQL ('minerva_test'), dev/prod DB'lerine asla
    bulaşmaz.  Her test fonksiyonu temiz başlar (tablolar her testten önce
    drop_all + create_all ile yeniden yaratılır).
  • TestClient (FastAPI'nin requests-based fake client'ı) — gerçek HTTP
    yok, hız için.
  • Test ortamında SECRET_KEY, COOKIE_SECURE, EXPOSE_API_DOCS env'leri
    fixture içinde set edilir; .env dosyasından bağımsız çalışır.
  • SEED_DEFAULT_USERS=true ile auth test'leri için 4 default user
    yaratılır (dogukan/isik/songul/meltem, hepsi minerva123).
"""
import os
from typing import Generator

import pytest

# ── Env setup — herşeyden ÖNCE ────────────────────────────────────────────
# core.auth SECRET_KEY'i import-time kontrol eder; database.py DATABASE_URL'i
# import-time okur.  Bu yüzden test env'i Python interpreter'in başında set
# edilmeli.  Aşağıdaki satırlar tüm test modülleri import edilmeden çalışır.
# DATABASE_URL bilerek BURADA kurulur — çağıranın env'ine güvenilmez, yoksa
# yanlış bir DATABASE_URL ile dev/prod veritabanı drop_all edilebilir.
# Yalnız DB ADI opt-in bir değişkenle değiştirilebilir; böylece iki oturum
# aynı anda test koşarken birbirinin tablolarını silmez (HANDOFF'taki bilinen
# tuzak):  MINERVA_TEST_DB=minerva_test2 .venv/bin/pytest tests/...
_TEST_DB = os.environ.get("MINERVA_TEST_DB", "minerva_test")
os.environ["DATABASE_URL"]  = f"postgresql://minerva_user:devpass123@localhost:5432/{_TEST_DB}"
os.environ["SECRET_KEY"]    = "test_only_secret_key_for_pytest_session_minimum_16chars"
os.environ["COOKIE_SECURE"] = "false"
os.environ["EXPOSE_API_DOCS"] = "false"
os.environ["SEED_DEFAULT_USERS"] = "true"
os.environ["SEED_DEFAULT_PASSWORD"] = "minerva123"
# Test'lerde APScheduler'ı devre dışı bırak — lifespan event'i async loop
# kapanırken hata atıyor; testlerde zaten gerek yok.
os.environ["DISABLE_SCHEDULER"] = "true"
# Drive yüklemeleri repo'ya değil geçici dizine yazılsın (test izolasyonu)
os.environ["MINERVA_DRIVE_DIR"] = "/tmp/minerva_test_drive"
# Ürün yorumu sesli notları da aynı sebeple — core.reviews.REVIEW_AUDIO_DIR
# modül IMPORT ZAMANINDA hesaplanır, bu satır ilk import'tan ÖNCE gelmeli.
os.environ["MINERVA_REVIEW_AUDIO_DIR"] = "/tmp/minerva_test_review_audio"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database import Base, SessionLocal, engine, init_db  # noqa: E402


@pytest.fixture(scope="function", autouse=True)
def clean_db() -> Generator[None, None, None]:
    """
    Her test öncesi tablolar yeniden yaratılır → testler birbirini etkilemez.
    autouse=True olduğu için her testte otomatik koşar.
    """
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # Seed default users (dogukan/isik/songul/meltem) — login testleri için
    init_db()
    yield
    # Teardown — bir sonraki test başlamadan drop_all ile temizleyeceğiz


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """
    FastAPI TestClient — HTTP isteklerini in-memory yapar.  Tüm middleware
    (rate limit, CSRF, security headers) doğal akışla çalışır.
    Her test öncesi rate limiter'ı sıfırlar (in-memory state testler arası
    taşımasın; aksi halde 5 test sonra her şey 429 dönmeye başlar).
    """
    from api_main import app
    from core.limiter import limiter

    # Rate limit state'ini temizle — SlowAPI in-memory storage tutar
    if hasattr(limiter, "_storage") and hasattr(limiter._storage, "reset"):
        limiter._storage.reset()
    elif hasattr(limiter, "reset"):
        limiter.reset()

    # TestClient'ı LifespanContext'siz başlat — APScheduler async cleanup
    # event loop kapanırken hata atıyor.  raise_server_exceptions=True
    # default; lifespan'i de zaten DISABLE_SCHEDULER ile no-op'a çevirdik.
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Doğrudan DB session — test'in beklediği state'i kurmak için."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def authed_client(client: TestClient) -> TestClient:
    """
    SuperAdmin (dogukan) olarak login olmuş client.
    Auth cookie set edilmiş şekilde döner.
    """
    r = client.post(
        "/api/login",
        json={"username": "dogukan", "password": "minerva123"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200, f"Login başarısız: {r.text}"
    return client


@pytest.fixture
def labtech_client(client: TestClient, db_session: Session) -> TestClient:
    """
    LabTech (meltem) olarak login — kısıtlı yetkili kullanıcı.
    RBAC testlerinde "yetkim yetmez" path'lerini sınamak için.
    """
    r = client.post(
        "/api/login",
        json={"username": "meltem", "password": "minerva123"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200, f"LabTech login başarısız: {r.text}"
    return client
