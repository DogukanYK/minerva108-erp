# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Auth router — login, logout.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional

from database import get_db, User
from core.auth import verify_password, create_access_token, set_auth_cookie
from core.limiter import limiter
from core.permissions import _has_permission


# ─── Hesap kilitleme parametreleri (R3) ──────────────────────────────────
# N başarısız → M dakika kilit.  Şu an: 5 → 15dk.  IP-based 5/15dk rate
# limit zaten /api/login'de var; bu user-bazlı ikinci katman.
LOCKOUT_THRESHOLD       = 5      # bu kadar başarısız sonra kilit
LOCKOUT_DURATION_MIN    = 15

# Global default idle timeout — kullanıcı user.idle_timeout_minutes set
# etmediğinde frontend bu değeri kullanır.  Lab istediği gibi değiştirebilir.
DEFAULT_IDLE_TIMEOUT_MIN = 5

# PDKS kiosk hesapları (örn. /pdks-qr'ı sürekli açık tutan ekran) normal
# 8 saatlik oturumla her gece dışarı düşer — 11.08.2026 kesintisiyle aynı
# aileden bir arıza: ekran sabah "giriş yapın" gösterir, kimse imza atamaz.
# Bu hesaplara login'de pratikte süresiz oturum verilir (~10 yıl); yetki
# denetimi (pdks.kiosk) yine HER istekte canlı kontrol edilir (bkz.
# api_main.pdks_qr_page → _user_can) — bu yalnız "yeniden giriş" ihtiyacını
# kaldırır, yetkiyi kalıcı yapmaz. Yetki geri alınırsa token geçerli olsa
# bile sayfa aynı anda kapanır.
# SuperAdmin HARİÇ: _resolve_permissions SuperAdmin'i her kategoride otomatik
# True döndürür, yani bu istisna olmadan patronun kendi normal girişi de
# sessizce 10 yıllık oturuma dönerdi — _kiosk_account_exists'teki "SuperAdmin
# sayılmaz" kuralıyla aynı sebep, burada da aynı istisna şart.
KIOSK_SESSION_DAYS = 3650

router = APIRouter(prefix="/api", tags=["auth"])


# ─── Schemas ────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    # Login için max_length güvenliği — saldırgan 1MB username gönderemez,
    # bcrypt 72 byte'ı geçemez (DoS önler).
    username:    str           = Field(..., min_length=1, max_length=50)
    password:    str           = Field(..., min_length=1, max_length=128)
    remember_me: Optional[bool] = False


# ─── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/login")
@limiter.limit("40/15minutes")   # IP-based; ortak ofis IP'si tüm ekibi paylaşır → 5 çok düşüktü
                                 # (başarılı girişler de sayılır). Asıl brute-force koruması
                                 # aşağıdaki per-HESAP kilidi (5 yanlış → 15dk).
def login(request: Request, data: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = (
        db.query(User)
        .filter(User.username == data.username, User.is_active == True)
        .first()
    )

    # ── Kullanıcı yoksa erken çık (timing attack için aynı path) ────
    if not user:
        return JSONResponse(
            status_code=401,
            content={"detail": "Kullanıcı adı veya şifre hatalı."}
        )

    # ── R3: hesap kilitli mi? ─────────────────────────────────────────
    now = datetime.utcnow()
    if user.lockout_until and user.lockout_until > now:
        remaining = int((user.lockout_until - now).total_seconds() / 60) + 1
        return JSONResponse(
            status_code=423,   # 423 Locked
            content={"detail": (
                f"Hesap güvenlik gereği geçici olarak kilitlendi. "
                f"~{remaining} dakika sonra tekrar deneyin."
            )}
        )

    # ── Şifre kontrolü ────────────────────────────────────────────────
    if not verify_password(data.password, user.password_hash):
        user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
        if user.failed_login_attempts >= LOCKOUT_THRESHOLD:
            user.lockout_until = now + timedelta(minutes=LOCKOUT_DURATION_MIN)
        db.commit()
        return JSONResponse(
            status_code=401,
            content={"detail": "Kullanıcı adı veya şifre hatalı."}
        )

    # ── Başarılı login: failed counter'ı sıfırla ──────────────────────
    user.failed_login_attempts = 0
    user.lockout_until = None
    db.commit()

    kiosk_days = (KIOSK_SESSION_DAYS
                  if user.role != "SuperAdmin" and _has_permission(user, "pdks", "kiosk")
                  else None)
    token = create_access_token(
        {"sub": str(user.id), "username": user.username, "full_name": user.full_name, "role": user.role},
        remember_me=data.remember_me,
        days=kiosk_days,
    )
    set_auth_cookie(response, token, remember_me=data.remember_me, days=kiosk_days)
    return {"message": "Giriş başarılı", "redirect": "/"}


@router.post("/logout")
def logout(response: Response):
    # delete_cookie domain'i set_auth_cookie ile aynı olmalı — aksi halde
    # SSO (Domain=.minerva108.com) cookie'si silinmez ve oturum açık kalır.
    from core.auth import COOKIE_DOMAIN
    response.delete_cookie("access_token", domain=COOKIE_DOMAIN)
    return {"message": "Çıkış yapıldı"}


@router.get("/me/session-policy")
def get_session_policy(
    db: Session = Depends(get_db),
    current_user: dict = Depends(__import__("core.auth", fromlist=["get_current_user"]).get_current_user),
):
    """
    Frontend idle-timeout counter'ının kullandığı endpoint.
    Kullanıcıya özel ayar (user.idle_timeout_minutes) varsa onu döner;
    yoksa global DEFAULT_IDLE_TIMEOUT_MIN (5 dk).
    """
    u = db.query(User).filter(User.id == int(current_user.get("sub", 0))).first()
    if not u or not u.is_active:
        return JSONResponse(status_code=401, content={"detail": "Oturum geçersiz."})
    return {
        "idle_timeout_minutes": (u.idle_timeout_minutes or DEFAULT_IDLE_TIMEOUT_MIN),
        "default_idle_timeout_minutes": DEFAULT_IDLE_TIMEOUT_MIN,
        "is_custom": u.idle_timeout_minutes is not None,
    }
