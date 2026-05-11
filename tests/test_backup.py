"""
Backup/restore endpoint testleri.

Kapsam:
  • Yedek alma (POST /api/admin/backup)
  • Liste (GET /api/admin/backup)
  • İndirme (GET /api/admin/backup/{fn}/download)
  • Silme (DELETE /api/admin/backup/{fn})
  • Path traversal koruması (../etc/passwd vb.)
  • Magic-bytes upload doğrulama (PGDMP)
  • Restore SuperAdmin zorunlu
  • Backup endpoint'leri admin.backup izniyle gate'li

NOT: pg_dump'ı subprocess çağırır → test PG database'i lokalde çalışıyor
olmalı (conftest minerva_test'i kullanır).  CI'da bu testler skip edilebilir.
"""
import os
import subprocess
import pytest

from fastapi.testclient import TestClient


HDR = {"Origin": "http://testserver"}


def _pg_dump_available() -> bool:
    """pg_dump binary'si var mı? — yoksa testleri skip et."""
    try:
        subprocess.run(["pg_dump", "--version"], capture_output=True, timeout=2)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(
    not _pg_dump_available(),
    reason="pg_dump not in PATH — backup tests need PostgreSQL client tools",
)


# ─── Authorization ─────────────────────────────────────────────────────────

def test_backup_endpoints_require_permission(client: TestClient):
    """Unauth GET → 401."""
    assert client.get("/api/admin/backup").status_code == 401


def test_backup_endpoints_require_admin_backup_perm(client: TestClient):
    """LabTech admin.backup=False → 403."""
    client.post("/api/login", json={"username": "meltem", "password": "minerva123"}, headers=HDR)
    assert client.get("/api/admin/backup").status_code == 403


# ─── Backup creation ──────────────────────────────────────────────────────

def test_create_backup_returns_filename(authed_client: TestClient):
    """SuperAdmin POST → pg_dump çalıştırır, dosya adı döner."""
    r = authed_client.post("/api/admin/backup", headers=HDR)
    assert r.status_code == 201
    data = r.json()
    assert data["filename"].startswith("minerva_manual_")
    assert data["filename"].endswith(".dump")
    assert data["size_bytes"] > 0


def test_list_backups_after_create(authed_client: TestClient):
    # 1 yedek al
    r = authed_client.post("/api/admin/backup", headers=HDR)
    assert r.status_code == 201
    fn = r.json()["filename"]

    # Liste — yeni alınan içinde olmalı
    r = authed_client.get("/api/admin/backup")
    assert r.status_code == 200
    names = [b["filename"] for b in r.json()["backups"]]
    assert fn in names


# ─── Path traversal koruması ──────────────────────────────────────────────

def test_download_path_traversal_blocked(authed_client: TestClient):
    """../etc/passwd gibi pattern → 400."""
    r = authed_client.get("/api/admin/backup/..%2Fetc%2Fpasswd/download")
    # 400 veya 404 — her halükarda dosyaya erişim verilmez
    assert r.status_code in (400, 404)


def test_download_invalid_extension_blocked(authed_client: TestClient):
    """Pattern dışı (örn .py uzantısı) → 400."""
    r = authed_client.get("/api/admin/backup/api_main.py/download")
    assert r.status_code == 400


def test_delete_path_traversal_blocked(authed_client: TestClient):
    r = authed_client.delete(
        "/api/admin/backup/..%2F..%2Fetc%2Fpasswd",
        headers=HDR,
    )
    assert r.status_code in (400, 404)


# ─── Download + delete ─────────────────────────────────────────────────────

def test_download_and_delete_lifecycle(authed_client: TestClient):
    """Yedek al → indir (200 + bytes) → sil (200) → tekrar indir (404)."""
    r = authed_client.post("/api/admin/backup", headers=HDR)
    assert r.status_code == 201
    fn = r.json()["filename"]

    # İndir
    r = authed_client.get(f"/api/admin/backup/{fn}/download")
    assert r.status_code == 200
    # pg_dump custom format → PGDMP magic bytes
    assert r.content[:5] == b"PGDMP"

    # Sil
    r = authed_client.delete(f"/api/admin/backup/{fn}", headers=HDR)
    assert r.status_code == 200

    # Tekrar indir → 404
    r = authed_client.get(f"/api/admin/backup/{fn}/download")
    assert r.status_code == 404


# ─── Upload validation ────────────────────────────────────────────────────

def test_upload_rejects_non_dump_extension(authed_client: TestClient):
    files = {"file": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")}
    r = authed_client.post("/api/admin/backup/upload", files=files, headers=HDR)
    assert r.status_code == 400


def test_upload_rejects_bad_magic_bytes(authed_client: TestClient, tmp_path):
    """Uzantı .dump ama içerik pg_dump değil → 400 (PGDMP header eksik)."""
    files = {"file": ("fake.dump", b"NOT_PGDMP_HEADER_AT_ALL", "application/octet-stream")}
    r = authed_client.post("/api/admin/backup/upload", files=files, headers=HDR)
    assert r.status_code == 400
    assert "PGDMP" in r.json()["detail"]


def test_upload_valid_dump_accepted(authed_client: TestClient):
    """Önce gerçek bir backup al, içeriğini al → upload et → kabul edilir."""
    r = authed_client.post("/api/admin/backup", headers=HDR)
    fn = r.json()["filename"]
    r = authed_client.get(f"/api/admin/backup/{fn}/download")
    content = r.content
    assert content[:5] == b"PGDMP"

    # Şimdi upload et
    files = {"file": ("uploaded.dump", content, "application/octet-stream")}
    r = authed_client.post("/api/admin/backup/upload", files=files, headers=HDR)
    assert r.status_code == 201
    # Uploaded olduğu için yeniden adlandırılmış olabilir
    assert r.json()["filename"].endswith(".dump")


# ─── Restore: SuperAdmin zorunlu ──────────────────────────────────────────

def test_restore_requires_superadmin(client: TestClient):
    """Manager rolü admin.backup'a sahip olsaydı bile, restore SuperAdmin
       zorunlu — bu test bunu doğrular (eğer Manager'a manuel backup verirsek)."""
    # Manager olarak login
    client.post(
        "/api/login",
        json={"username": "isik", "password": "minerva123"},
        headers=HDR,
    )
    # Manager admin.backup=False zaten — 403
    r = client.post("/api/admin/backup/x.dump/restore", headers=HDR)
    assert r.status_code == 403


def test_restore_invalid_filename_400(authed_client: TestClient):
    r = authed_client.post(
        "/api/admin/backup/..%2Fetc%2Fpasswd/restore",
        headers=HDR,
    )
    assert r.status_code in (400, 404)


def test_restore_nonexistent_file_404(authed_client: TestClient):
    r = authed_client.post(
        "/api/admin/backup/minerva_does_not_exist.dump/restore",
        headers=HDR,
    )
    assert r.status_code == 404
