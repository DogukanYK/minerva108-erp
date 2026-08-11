# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Ürün yorumu sesli notları — depolama yardımcıları.

Dosyalar diske `REVIEW_AUDIO_DIR` altında RASTGELE adla yazılır
(path-traversal yok, bkz. core/drive.py save_upload aynı desen).  Uzantı
İSTEMCİDEN DEĞİL sihirli bayttan türetilir (`sniff_audio`) — bu uç
kimlik doğrulaması olmadan herkese açık (bkz. routers/reviews.py
public_router), core/product_images.is_jpeg'in aynı gerekçesi burada da
geçerli: dosya adı/Content-Type istemci beyanı, güvenilmez.

İki tarayıcı ailesi iki farklı konteyner üretir — ikisi de kabul edilir,
sunucuda transcode YAPILMAZ (ikisi de her tarayıcıda native çalar):
  • Chrome/Firefox/Edge (MediaRecorder, audio/webm;codecs=opus) → WebM
  • Safari/iOS (audio/mp4)                                       → ISO-BMFF (m4a)
"""
import os
import secrets
from pathlib import Path
from typing import Optional

# Yüklenen ses dosyalarının saklandığı yer.  Prod: /var/www/minerva/review_audio.
REVIEW_AUDIO_DIR = Path(os.environ.get(
    "MINERVA_REVIEW_AUDIO_DIR",
    str(Path(__file__).resolve().parent.parent / "review_audio"),
))
REVIEW_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(REVIEW_AUDIO_DIR, 0o700)
except OSError:
    pass

# 2 dk Opus ≈ 1 MB, 2 dk AAC ≈ 2 MB — 10 MB bol pay.  Shopify Files dosya
# başına 20 MB kabul ediyor, bu limit onun altında kalır.
MAX_UPLOAD_MB = int(os.environ.get("MINERVA_REVIEW_AUDIO_MAX_MB", "10"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024


# ─── Sihirli bayt tespiti ────────────────────────────────────────────────────

def sniff_audio(head: bytes) -> Optional[tuple]:
    """İlk birkaç yüz bayttan konteyner tipini belirler.

    Döner: (uzantı, mime) ya da tanınmayan/desteklenmeyen içerik için None.
    Content-Type/dosya adı İSTEMCİ BEYANI — asla güvenilmez, yalnız bu
    kontrol geçerse dosya kabul edilir.
    """
    if not head:
        return None
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return (".webm", "audio/webm")
    if head.startswith(b"OggS"):
        return (".ogg", "audio/ogg")
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"M4A \x00", b"M4A ", b"mp42", b"isom", b"iso2", b"mp41"):
            return (".m4a", "audio/mp4")
    if head.startswith(b"ID3") or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return (".mp3", "audio/mpeg")
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WAVE":
        return (".wav", "audio/wav")
    return None


# ─── Disk depolama ───────────────────────────────────────────────────────────

async def save_audio(upload) -> tuple:
    """
    UploadFile'ı diske rastgele adla, boyut limitiyle ve sihirli-bayt
    doğrulamasıyla yazar.  core.drive.save_upload ile aynı akış şekli,
    tek fark: uzantı `upload.filename`'den değil `sniff_audio`'dan gelir.

    Döner: (stored_name, size_bytes, mime, duration_hint_none)
    ValueError → 400'e çevrilecek insan-okur mesaj.
    """
    chunk = await upload.read(1024 * 1024)   # ilk 1 MB — sniff + ilk yazım
    kind = sniff_audio(chunk)
    if kind is None:
        raise ValueError("Ses dosyası tanınmadı (webm/ogg/mp4/mp3/wav olmalı).")
    ext, mime = kind

    stored = secrets.token_hex(16) + ext
    path = REVIEW_AUDIO_DIR / stored
    total = 0
    try:
        with open(path, "wb") as fh:
            while chunk:
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    fh.close()
                    path.unlink(missing_ok=True)
                    raise ValueError(f"Ses dosyası çok büyük (max {MAX_UPLOAD_MB} MB).")
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


def audio_path(stored_name: str) -> Path:
    """stored_name'in diskteki tam yolu — path-traversal'a karşı doğrular
    (core.drive.stored_path ile aynı desen)."""
    import re
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", stored_name or "")
    p = (REVIEW_AUDIO_DIR / safe).resolve()
    if REVIEW_AUDIO_DIR.resolve() not in p.parents:
        raise ValueError("Geçersiz dosya yolu.")
    return p


def delete_audio(stored_name: str) -> None:
    """Diskten sil — hata olursa sessizce yutar (silme akışı bundan kırılmasın)."""
    if not stored_name:
        return
    try:
        audio_path(stored_name).unlink(missing_ok=True)
    except Exception:
        pass
