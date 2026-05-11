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


# ─── Hesap kilitleme parametreleri (R3) ──────────────────────────────────
# N başarısız → M dakika kilit.  Şu an: 5 → 15dk.  IP-based 5/15dk rate
# limit zaten /api/login'de var; bu user-bazlı ikinci katman.
LOCKOUT_THRESHOLD       = 5      # bu kadar başarısız sonra kilit
LOCKOUT_DURATION_MIN    = 15

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
@limiter.limit("5/15minutes")    # IP-based — credential stuffing'i etkisiz kılar
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

    token = create_access_token(
        {"sub": str(user.id), "username": user.username, "full_name": user.full_name, "role": user.role},
        remember_me=data.remember_me,
    )
    set_auth_cookie(response, token, remember_me=data.remember_me)
    return {"message": "Giriş başarılı", "redirect": "/"}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("access_token")
    return {"message": "Çıkış yapıldı"}
