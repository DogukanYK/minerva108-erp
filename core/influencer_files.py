# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Influencer dosyaları (Insights ekran kaydı, ekran görüntüsü, sözleşme, vergi
belgesi, taslak) — depolama yardımcıları.

core/reviews.py `save_audio` kalıbı: dosya `DRIVE_DIR` altına RASTGELE adla
yazılır (core.drive.stored_path ile geri okunur), uzantı ve MIME İSTEMCİDEN
DEĞİL sihirli bayttan türetilir (`sniff_media`).  Public claim ucundan
(`/basvuru/t/{token}/insights`) kimlik doğrulamasız yükleme gelir — dosya
adı/Content-Type beyanı güvenilmez.

Kabul edilen türler ve üst sınırlar:
  video/mp4, video/quicktime ('ftyp' 4. bayttan), video/webm (1A 45 DF A3) → 60 MB
  image/jpeg (FF D8), image/png (89 50 4E 47)                              → 10 MB
  application/pdf ('%PDF-')                                                → 10 MB
"""
import os
import secrets
from typing import Optional

from core.drive import DRIVE_DIR

MAX_VIDEO_MB = int(os.environ.get("MINERVA_INFLUENCER_VIDEO_MAX_MB", "60"))
MAX_IMAGE_MB = int(os.environ.get("MINERVA_INFLUENCER_IMAGE_MAX_MB", "10"))
MAX_VIDEO_BYTES = MAX_VIDEO_MB * 1024 * 1024
MAX_IMAGE_BYTES = MAX_IMAGE_MB * 1024 * 1024

_VIDEO_TYPES = {"video/mp4", "video/quicktime", "video/webm"}


def sniff_media(head: bytes) -> Optional[tuple]:
    """İlk baytlardan (uzantı, mime) ya da desteklenmeyen içerik için None."""
    if not head or len(head) < 8:
        return None
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return (".webm", "video/webm")
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand.startswith(b"qt"):
            return (".mov", "video/quicktime")
        return (".mp4", "video/mp4")
    if head.startswith(b"\xff\xd8"):
        return (".jpg", "image/jpeg")
    if head.startswith(b"\x89PNG"):
        return (".png", "image/png")
    if head.startswith(b"%PDF-"):
        return (".pdf", "application/pdf")
    return None


def limit_for(mime: str) -> int:
    return MAX_VIDEO_BYTES if mime in _VIDEO_TYPES else MAX_IMAGE_BYTES


async def save_media(upload) -> tuple:
    """UploadFile → diske (DRIVE_DIR), sihirli bayt + tür bazlı boyut limiti.

    Döner: (stored_name, size_bytes, content_type)
    ValueError → 400'e çevrilecek insan-okur mesaj.
    """
    chunk = await upload.read(1024 * 1024)   # ilk 1 MB — sniff + ilk yazım
    kind = sniff_media(chunk)
    if kind is None:
        raise ValueError("Dosya tanınmadı (mp4/mov/webm video, jpg/png görsel ya da PDF olmalı).")
    ext, mime = kind
    limit = limit_for(mime)
    limit_mb = limit // (1024 * 1024)

    stored = secrets.token_hex(16) + ext
    path = DRIVE_DIR / stored
    total = 0
    try:
        with open(path, "wb") as fh:
            while chunk:
                total += len(chunk)
                if total > limit:
                    fh.close()
                    path.unlink(missing_ok=True)
                    raise ValueError(f"Dosya çok büyük (max {limit_mb} MB).")
                fh.write(chunk)
                chunk = await upload.read(1024 * 1024)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except ValueError:
        raise
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return stored, total, mime


def delete_media(stored_name: str) -> None:
    """Diskten sil — hata olursa sessizce yutar (silme akışı bundan kırılmasın)."""
    if not stored_name:
        return
    try:
        from core.drive import stored_path
        stored_path(stored_name).unlink(missing_ok=True)
    except Exception:
        pass
