"""
Auth router — login, logout.
"""
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from database import get_db, User
from core.auth import verify_password, create_access_token, set_auth_cookie
from core.limiter import limiter

router = APIRouter(prefix="/api", tags=["auth"])


# ─── Schemas ────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: Optional[bool] = False


# ─── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/login")
@limiter.limit("5/minute")     # Max 5 login attempts per IP per minute → blunts credential stuffing
def login(request: Request, data: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = (
        db.query(User)
        .filter(User.username == data.username, User.is_active == True)
        .first()
    )
    if not user or not verify_password(data.password, user.password_hash):
        return JSONResponse(
            status_code=401,
            content={"detail": "Kullanıcı adı veya şifre hatalı."}
        )
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
