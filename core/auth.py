# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from jose import JWTError, jwt
from fastapi import Cookie, Depends, HTTPException, status
from fastapi.responses import Response

# ─── SECRET_KEY — fail loud if missing ──────────────────────────────────────
# Eskiden insecure default ("minerva108-secret-key-...") vardı; .env yoksa
# saldırgan default key ile JWT forge edebilirdi. Şimdi env yoksa proces
# patlar — sessizce zayıf default'a düşmez. Lokal dev için .env'de
# SECRET_KEY set olmalı (worktree'de zaten var).
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
if not SECRET_KEY:
    sys.stderr.write(
        "\n❌ HATA: SECRET_KEY environment variable set edilmemiş.\n"
        "   Prod'da systemd'nin .env dosyasında olmalı.\n"
        "   Lokalde worktree'ye .env dosyası oluşturup SECRET_KEY=... ekleyin.\n\n"
    )
    sys.exit(1)
if len(SECRET_KEY) < 16:
    sys.stderr.write("\n❌ HATA: SECRET_KEY 16+ karakter olmalı.\n\n")
    sys.exit(1)

ALGORITHM = "HS256"

# ─── Cookie güvenliği — env-driven, default secure ──────────────────────────
# Prod HTTPS arkasında: COOKIE_SECURE=true (default) → cookie sadece HTTPS'te
# gönderilir.  Lokal dev http://localhost için COOKIE_SECURE=false set et.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() in ("1", "true", "yes")

# ─── Cookie Domain — alt alanlar arası tek oturum (SSO) ─────────────────────
# Boş (default) → cookie yalnızca isteğin geldiği host'a aittir (eski davranış,
# lokal dev için doğru).  Prod'da COOKIE_DOMAIN=.minerva108.com set edilirse
# auth cookie hem ims.minerva108.com hem crm.minerva108.com için geçerli olur
# → kullanıcı bir kez giriş yapar, her iki panelde de oturumu açıktır.
# Mevcut host-kilitli cookie'ler geçerli kalır; yalnızca yeni login'ler bu
# geniş scope'u alır.  Logout aynı domain ile silmek zorunda — yoksa cookie kalır.
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN", "").strip() or None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(data: dict, remember_me: bool = False) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (timedelta(days=30) if remember_me else timedelta(hours=8))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def set_auth_cookie(response: Response, token: str, remember_me: bool = False):
    max_age = 30 * 24 * 3600 if remember_me else 8 * 3600
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        max_age=max_age,
        samesite="lax",
        secure=COOKIE_SECURE,   # Prod: True (HTTPS-only); Lokal: env'de False'a çek
        domain=COOKIE_DOMAIN,   # None → host-scoped; '.minerva108.com' → alt alanlar arası SSO
    )


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def get_current_user(access_token: Optional[str] = Cookie(default=None)) -> dict:
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Oturum bulunamadı"
        )
    payload = decode_token(access_token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Geçersiz veya süresi dolmuş oturum"
        )
    return payload


def require_role(allowed_roles: list):
    """FastAPI dependency factory — 403 döner izinsiz roller için."""
    def checker(user: dict = Depends(get_current_user)):
        if user.get("role") not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Bu işlem için yetkiniz bulunmamaktadır.",
            )
        return user
    return checker
