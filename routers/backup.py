# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Backup & Restore router — admin sayfasından manuel yedekleme.

Tasarım hedefi: bir gün sunucu çöker, prod DB silinir, lab paniğe kapılır;
o gün admin sayfasından son yedeği indirip restore et → her şey döner.
Hiçbir lot, kullanıcı, transaction, audit log kaybolmaz.

Yedek formatı: pg_dump --format=custom (.dump uzantısı).
  + Schema + data + sequences + indexes + constraints hepsi içinde
  + Custom format paralel restore destekler
  + Versionsızdır (PG14 → PG16 restore çalışır)

Güvenlik:
  - Tüm endpoint'ler admin.backup izniyle gate'li (default: SuperAdmin)
  - Restore ayrıca admin.restore izni ister (default: sadece SuperAdmin)
  - Restore'dan ÖNCE otomatik safety backup alınır (yanlış geri yüklemeye karşı)
  - Filename validation: sadece minerva_*.dump pattern'ı (path traversal yok)
  - Tüm backup/restore eventleri admin_audit_log'a yazılır
  - Backup dosyaları chmod 600 (sadece servis kullanıcısı)

Disk yönetimi:
  - Son N yedek tutulur (varsayılan 30) — eskisini admin kullanıcısı silebilir
  - Otomatik temizlik yok (riskli — admin karar versin)
"""
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session

from database import get_db, to_tr
from core.audit import log_admin_event
from core.auth import get_current_user
from core.permissions import _has_permission, require_permission

router = APIRouter(prefix="/api/admin/backup", tags=["backup"])


# ─── Konfigürasyon ──────────────────────────────────────────────────────────

# Yedek dosyalarının saklandığı yer.  Sunucuda /var/www/minerva/backups,
# lokalde worktree/backups.  Env ile override edilebilir.
BACKUP_DIR = Path(os.environ.get(
    "MINERVA_BACKUP_DIR",
    str(Path(__file__).resolve().parent.parent / "backups")
))
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
# chmod 700 — sadece servis kullanıcısı okuyabilsin
try:
    os.chmod(BACKUP_DIR, 0o700)
except OSError:
    pass

# Safe filename pattern — path traversal'a karşı sıkı
_BACKUP_NAME_RE = re.compile(r"^minerva_[A-Za-z0-9_-]+\.dump$")

# Max upload boyutu (100MB — DB büyük değil, bu fazlasıyla yeterli)
MAX_BACKUP_UPLOAD_BYTES = 100 * 1024 * 1024


# ─── Yardımcılar ────────────────────────────────────────────────────────────

def _parse_db_url() -> dict:
    """DATABASE_URL'i pg_dump/pg_restore için subprocess env'ine çevir."""
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        return {}
    p = urlparse(url)
    return {
        "host":     p.hostname or "localhost",
        "port":     str(p.port or 5432),
        "user":     p.username or "",
        "password": p.password or "",
        "dbname":   p.path.lstrip("/") if p.path else "",
    }


def _safe_filename(name: str) -> Optional[str]:
    """Path traversal'a karşı koruma — sadece pattern'a uyan adlar kabul."""
    name = (name or "").strip()
    if not _BACKUP_NAME_RE.match(name):
        return None
    return name


# ─── PostgreSQL binary path detection ────────────────────────────────────
# Prod systemd unit'inin PATH'i sadece /var/www/minerva/venv/bin — pg_dump
# orada YOK.  shutil.which() de bu yüzden bulamaz.  Bu helper yaygın
# kurulum yerlerini tarar; ilk bulduğunu döner.  Bulamazsa None.
def _find_pg_binary(name: str) -> Optional[str]:
    """name = 'pg_dump' / 'pg_restore' / 'psql'.  Önce PATH, sonra yaygın yerler."""
    found = shutil.which(name)
    if found:
        return found
    candidates = [
        f"/usr/bin/{name}",                                  # Debian/Ubuntu
        f"/usr/local/bin/{name}",                            # CentOS / custom
        f"/opt/homebrew/opt/postgresql@16/bin/{name}",       # Mac Apple Silicon
        f"/opt/homebrew/opt/postgresql@14/bin/{name}",
        f"/usr/local/opt/postgresql@16/bin/{name}",          # Mac Intel
        f"/usr/local/opt/postgresql@14/bin/{name}",
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    return None


def _list_backups() -> list:
    """Yedek dizinindeki tüm .dump dosyalarını listele (en yeni önce)."""
    rows = []
    for f in sorted(BACKUP_DIR.glob("minerva_*.dump"), key=lambda p: p.stat().st_mtime, reverse=True):
        st = f.stat()
        rows.append({
            "filename":   f.name,
            "size_bytes": st.st_size,
            "size_human": _humanize(st.st_size),
            "created_at": to_tr(datetime.fromtimestamp(st.st_mtime)).strftime("%d.%m.%Y %H:%M:%S"),
            "timestamp":  st.st_mtime,
        })
    return rows


def _humanize(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _run_pg_dump(out_path: Path) -> tuple[bool, str]:
    """pg_dump --format=custom ile yedek al.  (ok, stderr_or_path) döner."""
    db = _parse_db_url()
    if not db:
        return False, "DATABASE_URL set edilmemiş veya PostgreSQL değil."

    pg_dump_bin = _find_pg_binary("pg_dump")
    if not pg_dump_bin:
        return False, "pg_dump bulunamadı. PostgreSQL client paketi yüklü mü?"

    env = os.environ.copy()
    if db["password"]:
        env["PGPASSWORD"] = db["password"]

    cmd = [
        pg_dump_bin,
        "-h", db["host"], "-p", db["port"],
        "-U", db["user"], "-d", db["dbname"],
        "--format=custom",
        "--no-owner", "--no-acl",
        "-f", str(out_path),
    ]
    try:
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            return False, result.stderr.strip()[:2000]
        # chmod 600 — sadece servis kullanıcısı okuyabilsin
        try:
            os.chmod(out_path, 0o600)
        except OSError:
            pass
        return True, str(out_path)
    except subprocess.TimeoutExpired:
        return False, "pg_dump 5 dakika içinde tamamlanamadı (timeout)."
    except Exception as e:
        return False, f"Beklenmeyen hata: {e}"


def _run_pg_restore(in_path: Path) -> tuple[bool, str]:
    """
    pg_restore --clean --if-exists ile yükle.  Hedef DB'deki tüm objeleri
    drop edip yeniden oluşturur.  Aktif bağlantıları önce sonlandır.
    """
    db = _parse_db_url()
    if not db:
        return False, "DATABASE_URL set edilmemiş veya PostgreSQL değil."

    pg_restore_bin = _find_pg_binary("pg_restore")
    psql_bin       = _find_pg_binary("psql")
    if not pg_restore_bin:
        return False, "pg_restore bulunamadı. PostgreSQL client paketi yüklü mü?"

    env = os.environ.copy()
    if db["password"]:
        env["PGPASSWORD"] = db["password"]

    # 1) Aktif bağlantıları sonlandır (kendimiz hariç) — restore'un takılmaması için
    if psql_bin:
        try:
            subprocess.run(
                [psql_bin,
                 "-h", db["host"], "-p", db["port"],
                 "-U", db["user"], "-d", db["dbname"],
                 "-c", (
                     "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                     "WHERE datname = current_database() AND pid <> pg_backend_pid()"
                 )],
                env=env, capture_output=True, text=True, timeout=30,
            )
        except Exception:
            pass  # En kötü ihtimalle pg_restore takılır, kullanıcı görür

    # 2) pg_restore --clean --if-exists  → mevcut objeleri drop et, yeniden yarat
    cmd = [
        pg_restore_bin,
        "-h", db["host"], "-p", db["port"],
        "-U", db["user"], "-d", db["dbname"],
        "--clean", "--if-exists",
        "--no-owner", "--no-acl",
        "--single-transaction",       # ya hep ya hiç — kısmi restore yok
        str(in_path),
    ]
    try:
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=600,
        )
        stderr = result.stderr or ""
        had_real_error = any(
            line.lower().startswith("pg_restore: error:")
            for line in stderr.splitlines()
        )
        if had_real_error:
            return False, stderr[:3000]
        return True, "OK"
    except subprocess.TimeoutExpired:
        return False, "pg_restore 10 dakika içinde tamamlanamadı."
    except Exception as e:
        return False, f"Beklenmeyen hata: {e}"


# ─── İzin yardımcısı: restore için ek SuperAdmin kontrolü ───────────────────

def _require_superadmin(user_payload: dict, db: Session) -> Optional[JSONResponse]:
    """Restore yıkıcı — sadece SuperAdmin yapabilir, custom izin override edilmez."""
    from database import User
    user = db.query(User).filter(User.id == int(user_payload.get("sub", 0))).first()
    if not user or user.role != "SuperAdmin":
        return JSONResponse(
            status_code=403,
            content={"detail": "Restore yalnızca SuperAdmin tarafından yapılabilir."}
        )
    return None


# ─── Endpoints ──────────────────────────────────────────────────────────────

@router.get("")
def list_backups(_: dict = Depends(require_permission("admin", "backup"))):
    """Yedek dosyalarını listele (en yeni önce)."""
    return {"backup_dir": str(BACKUP_DIR), "backups": _list_backups()}


@router.post("", status_code=201)
def create_backup(
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("admin", "backup")),
):
    """
    Anlık yedek al — pg_dump --format=custom ile.
    Dosya BACKUP_DIR'a yazılır, audit log'a event eklenir.
    """
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    filename = f"minerva_manual_{ts}.dump"
    out_path = BACKUP_DIR / filename

    ok, msg = _run_pg_dump(out_path)
    if not ok:
        return JSONResponse(status_code=500, content={
            "detail": f"Yedek alınamadı: {msg}"
        })

    size = out_path.stat().st_size
    log_admin_event(
        db, request, current_user,
        action="backup.create",
        target_type="backup", target_name=filename,
        details={"size_bytes": size, "size_human": _humanize(size)},
    )
    return {
        "message":    "Yedek başarıyla alındı.",
        "filename":   filename,
        "size_bytes": size,
        "size_human": _humanize(size),
    }


@router.get("/{filename}/download")
def download_backup(
    filename: str,
    _: dict = Depends(require_permission("admin", "backup")),
):
    """Yedek dosyasını indir."""
    safe = _safe_filename(filename)
    if not safe:
        return JSONResponse(status_code=400, content={"detail": "Geçersiz dosya adı."})
    path = BACKUP_DIR / safe
    if not path.is_file():
        return JSONResponse(status_code=404, content={"detail": "Yedek bulunamadı."})
    return FileResponse(
        path=str(path),
        media_type="application/octet-stream",
        filename=safe,
    )


@router.delete("/{filename}")
def delete_backup(
    filename: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("admin", "backup")),
):
    """Yedek dosyasını sil."""
    safe = _safe_filename(filename)
    if not safe:
        return JSONResponse(status_code=400, content={"detail": "Geçersiz dosya adı."})
    path = BACKUP_DIR / safe
    if not path.is_file():
        return JSONResponse(status_code=404, content={"detail": "Yedek bulunamadı."})
    size = path.stat().st_size
    try:
        path.unlink()
    except OSError as e:
        return JSONResponse(status_code=500, content={"detail": f"Silinemedi: {e}"})

    log_admin_event(
        db, request, current_user,
        action="backup.delete",
        target_type="backup", target_name=safe,
        details={"size_bytes": size},
    )
    return {"message": f"'{safe}' silindi."}


@router.post("/upload", status_code=201)
async def upload_backup(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("admin", "backup")),
):
    """
    Önceden indirilmiş bir yedek dosyasını sunucuya yükle.
    Dosya BACKUP_DIR'a kaydedilir, restore için hazır olur.
    """
    # 1) Dosya adı güvenliği — kullanıcı bizim pattern'a uymalı veya yeniden adlandır
    original = (file.filename or "").strip()
    if not original.lower().endswith(".dump"):
        return JSONResponse(status_code=400, content={
            "detail": "Yalnızca .dump uzantılı dosyalar kabul edilir."
        })
    # Pattern'a uymuyorsa otomatik yeniden adlandır
    candidate = original.replace(" ", "_")
    if not _BACKUP_NAME_RE.match(candidate):
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        candidate = f"minerva_uploaded_{ts}.dump"

    out_path = BACKUP_DIR / candidate

    # 2) Boyut limiti + stream-based okuma (DoS koruması)
    total = 0
    try:
        with open(out_path, "wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)  # 1MB
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BACKUP_UPLOAD_BYTES:
                    fh.close()
                    out_path.unlink(missing_ok=True)
                    return JSONResponse(
                        status_code=413,
                        content={"detail": f"Dosya çok büyük (max {MAX_BACKUP_UPLOAD_BYTES//1024//1024} MB)."}
                    )
                fh.write(chunk)
        try:
            os.chmod(out_path, 0o600)
        except OSError:
            pass
    except Exception as e:
        out_path.unlink(missing_ok=True)
        return JSONResponse(status_code=500, content={"detail": f"Yükleme hatası: {e}"})

    # 3) Magic-bytes doğrulama — pg_dump custom format "PGDMP" header'ı ile başlar
    try:
        with open(out_path, "rb") as fh:
            head = fh.read(5)
        if head != b"PGDMP":
            out_path.unlink(missing_ok=True)
            return JSONResponse(
                status_code=400,
                content={"detail": "Geçersiz yedek formatı (PGDMP header bulunamadı)."}
            )
    except Exception:
        pass

    log_admin_event(
        db, request, current_user,
        action="backup.upload",
        target_type="backup", target_name=candidate,
        details={"size_bytes": total, "original_filename": original},
    )
    return {
        "message":    "Yedek yüklendi, restore için hazır.",
        "filename":   candidate,
        "size_bytes": total,
        "size_human": _humanize(total),
    }


@router.post("/{filename}/restore")
def restore_backup(
    filename: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Belirtilen yedeği geri yükle — TÜM mevcut veri silinir, yedek geri yüklenir.

    Akış:
      1) İzin kontrolü (SuperAdmin zorunlu)
      2) Otomatik safety backup (geri yüklemeden önce mevcut durumu yedekle)
      3) pg_restore --clean --if-exists --single-transaction
      4) Migration uygula (yeni kolon vb. yoksa)
      5) Audit log'a event ekle

    NOT: Bu istek bittiğinde uvicorn'un connection pool'u eski olabilir;
    SQLAlchemy bağlantıları otomatik yenileyecek (pool_pre_ping=True
    database.py'da set).  Yine de tarayıcıdan logout/login tavsiye edilir.
    """
    # 1) İzin: SuperAdmin
    perm_err = _require_superadmin(current_user, db)
    if perm_err:
        return perm_err

    safe = _safe_filename(filename)
    if not safe:
        return JSONResponse(status_code=400, content={"detail": "Geçersiz dosya adı."})
    in_path = BACKUP_DIR / safe
    if not in_path.is_file():
        return JSONResponse(status_code=404, content={"detail": "Yedek bulunamadı."})

    # 2) Safety backup
    ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    safety_name = f"minerva_pre-restore_{ts}.dump"
    safety_path = BACKUP_DIR / safety_name
    safety_ok, safety_msg = _run_pg_dump(safety_path)
    if not safety_ok:
        return JSONResponse(status_code=500, content={
            "detail": f"Önce safety backup alınamadı, restore iptal: {safety_msg}"
        })

    # 3) Restore
    ok, msg = _run_pg_restore(in_path)
    if not ok:
        # Safety backup duruyor — kullanıcıya nasıl geri yükleyeceğini söyle
        return JSONResponse(status_code=500, content={
            "detail": (
                f"Restore başarısız: {msg}\n\n"
                f"Eski durumu geri yüklemek için: pg_restore --clean --if-exists "
                f"-d <db> {safety_name}"
            ),
            "safety_backup": safety_name,
        })

    # 4) Migration — yedek eski şemada olabilir, en güncel migration'ı uygula
    try:
        from database import init_db
        init_db()
    except Exception as e:
        # Restore başarılıydı ama migration patladı — nadiren olur
        return JSONResponse(status_code=500, content={
            "detail": f"Restore başarılı ama migration patladı: {e}",
            "safety_backup": safety_name,
        })

    # 5) Audit log — yeni DB'de yazılacak (yedek geldikten sonra)
    log_admin_event(
        db, request, current_user,
        action="backup.restore",
        target_type="backup", target_name=safe,
        details={"safety_backup": safety_name},
    )
    return {
        "message": (
            f"'{safe}' başarıyla geri yüklendi.  Önceki durum '{safety_name}' "
            "yedeği olarak saklandı.  Çıkış yapıp tekrar girmeniz önerilir."
        ),
        "safety_backup": safety_name,
    }
