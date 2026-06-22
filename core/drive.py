# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Minerva Drive — dosya depolama + paylaşım yardımcıları (self-hosted).

Dosyalar diske `DRIVE_DIR` altında RASTGELE adla yazılır (path-traversal yok);
orijinal ad yalnızca DB'de gösterim için.  Paylaşım kodu tahmin edilemez
(`secrets`).  Şifreler bcrypt; şifre doğrulanınca o linke özel imzalı çerez
(HMAC + SECRET_KEY) verilir, indirmeler onu kontrol eder.
"""
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

import bcrypt

# Yüklenen dosyaların saklandığı yer.  Prod: /var/www/minerva/drive_files.
DRIVE_DIR = Path(os.environ.get(
    "MINERVA_DRIVE_DIR",
    str(Path(__file__).resolve().parent.parent / "drive_files"),
))
DRIVE_DIR.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(DRIVE_DIR, 0o700)
except OSError:
    pass

# Dosya başına azami boyut (MB).  DoS + disk koruması.  nginx tarafında da
# client_max_body_size bunun biraz üstünde olmalı (multipart payı için).
MAX_UPLOAD_MB = int(os.environ.get("MINERVA_DRIVE_MAX_MB", "500"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

_SECRET = (os.environ.get("SECRET_KEY") or "minerva-drive-fallback").encode()
_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,12}$")


# ─── Token / şifre / imza ────────────────────────────────────────────────────

def new_token(n: int = 16) -> str:
    """Tahmin edilemez paylaşım kodu (URL-safe)."""
    return secrets.token_urlsafe(n)


_TR_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def slugify(s: str) -> str:
    """Kullanıcı metnini URL-güvenli özel link koduna çevir.
    'Geven Belgeleri' → 'geven-belgeleri'.  Türkçe karakterler sadeleştirilir."""
    import unicodedata
    s = (s or "").translate(_TR_MAP)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:64]


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), (hashed or "").encode())
    except Exception:
        return False


def unlock_cookie_name(token: str) -> str:
    """Bir paylaşıma özel kilit-açma çerezinin adı."""
    return "du_" + re.sub(r"[^A-Za-z0-9_-]", "", token)[:40]


def sign_unlock(token: str) -> str:
    """Şifre doğrulanınca verilen imza (stateless — SECRET_KEY ile HMAC)."""
    return hmac.new(_SECRET, ("drive-unlock:" + token).encode(), hashlib.sha256).hexdigest()


def verify_unlock(token: str, sig: str) -> bool:
    if not sig:
        return False
    return hmac.compare_digest(sign_unlock(token), sig)


def is_expired(collection) -> bool:
    exp = getattr(collection, "expires_at", None)
    return bool(exp) and datetime.utcnow() > exp


# ─── Disk depolama ───────────────────────────────────────────────────────────

def _safe_ext(name: str) -> str:
    ext = Path(name or "").suffix.lower()
    return ext if _EXT_RE.match(ext) else ""


async def save_upload(upload) -> tuple:
    """
    UploadFile'ı diske rastgele adla, boyut limitiyle yazar.
    Döner: (stored_name, size_bytes, content_type)  ·  limit aşılırsa ValueError.
    """
    stored = secrets.token_hex(16) + _safe_ext(upload.filename or "")
    path = DRIVE_DIR / stored
    total = 0
    try:
        with open(path, "wb") as fh:
            while True:
                chunk = await upload.read(1024 * 1024)   # 1 MB
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    fh.close()
                    path.unlink(missing_ok=True)
                    raise ValueError(f"Dosya çok büyük (max {MAX_UPLOAD_MB} MB).")
                fh.write(chunk)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except ValueError:
        raise
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return stored, total, (upload.content_type or "application/octet-stream")


def stored_path(stored_name: str) -> Path:
    """stored_name'in diskteki tam yolu — path-traversal'a karşı doğrular."""
    # stored_name yalnızca hex + güvenli uzantı; yine de güvenlik için temizle.
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", stored_name or "")
    p = (DRIVE_DIR / safe).resolve()
    if DRIVE_DIR.resolve() not in p.parents:
        raise ValueError("Geçersiz dosya yolu.")
    return p


def delete_stored(stored_name: str) -> None:
    try:
        stored_path(stored_name).unlink(missing_ok=True)
    except Exception:
        pass


def humanize(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def clean_rel_path(path: str) -> str:
    """Klasör yüklemesinde tarayıcının verdiği göreli yolu görüntü için temizler.

    Diskteki ad zaten rastgele (stored_name) — bu yol yalnızca görüntüleme/indirme
    adı içindir, ama yine de '..'/kök kaçışı ve kontrol karakterleri ayıklanır.
    """
    p = (path or "").replace("\\", "/")
    parts = [seg.strip() for seg in p.split("/")]
    parts = [seg for seg in parts if seg and seg not in (".", "..")]
    p = "/".join(parts)
    p = re.sub(r"[\r\n\t]+", "", p)
    return p[:255] or "dosya"


def safe_download_name(name: str) -> str:
    """Content-Disposition için güvenli dosya adı.

    Klasör yüklemelerinde ad bir yol olabilir (`alt/klasor/a.pdf`); indirirken
    yalnızca son parça (asıl dosya adı) kullanılır.
    """
    base = re.split(r"[\\/]", (name or "dosya"))[-1]
    return re.sub(r'[\r\n"]+', "_", base).strip()[:200] or "dosya"
