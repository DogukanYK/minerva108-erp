"""
Admin user management + permission matrix endpoints.
"""
import json as _json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from database import get_db, User
from core.permissions import (
    _VALID_ROLES,
    PERMISSION_CATEGORIES,
    _resolve_permissions,
    require_permission,
)

router = APIRouter(prefix="/api", tags=["users"])


# ─── Schemas ────────────────────────────────────────────────────────────────

class AdminUserCreateRequest(BaseModel):
    username:  str
    full_name: str
    password:  str
    role:      str = "LabTech"


class AdminUserUpdateRequest(BaseModel):
    full_name: Optional[str]  = None
    role:      Optional[str]  = None
    is_active: Optional[bool] = None


class PasswordResetRequest(BaseModel):
    new_password: str


class PermissionsUpdateRequest(BaseModel):
    permissions: dict   # category → action → bool


# ─── Admin User Management Endpoints ────────────────────────────────────────

@router.get("/admin/users")
def admin_list_users(
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("admin", "view")),
):
    """Tüm kullanıcıları listele (şifre hash'i hariç)."""
    users = db.query(User).order_by(User.id.asc()).all()
    return [
        {
            "id":         u.id,
            "username":   u.username,
            "full_name":  u.full_name,
            "role":       u.role,
            "is_active":  u.is_active,
            "created_at": u.created_at.strftime("%d.%m.%Y") if u.created_at else "",
        }
        for u in users
    ]


@router.post("/admin/users", status_code=201)
def admin_create_user(
    data: AdminUserCreateRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("admin", "create")),
):
    """Yeni kullanıcı hesabı oluştur."""
    if data.role not in _VALID_ROLES:
        return JSONResponse(status_code=422, content={"detail": f"Geçersiz rol: '{data.role}'."})
    if len(data.password) < 6:
        return JSONResponse(status_code=422, content={"detail": "Şifre en az 6 karakter olmalıdır."})

    existing = db.query(User).filter(User.username == data.username).first()
    if existing:
        return JSONResponse(status_code=409, content={
            "detail": f"'{data.username}' kullanıcı adı zaten kullanılıyor."
        })

    import bcrypt as _bcrypt
    pw_hash = _bcrypt.hashpw(data.password.encode(), _bcrypt.gensalt()).decode()
    user = User(
        username=data.username,
        full_name=data.full_name,
        password_hash=pw_hash,
        role=data.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "message": f"Kullanıcı '{data.username}' başarıyla oluşturuldu."}


@router.put("/admin/users/{user_id}")
def admin_update_user(
    user_id: int,
    data: AdminUserUpdateRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("admin", "edit")),
):
    """Kullanıcının bilgilerini, rolünü veya aktiflik durumunu güncelle."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse(status_code=404, content={"detail": "Kullanıcı bulunamadı."})

    # Kendi hesabının rol veya aktiflik durumunu değiştirmeye izin verme
    if user_id == int(current_user.get("sub", -1)):
        if data.role is not None or data.is_active is not None:
            return JSONResponse(status_code=400, content={
                "detail": "Kendi hesabınızın rol veya aktiflik durumunu değiştiremezsiniz."
            })

    if data.role is not None:
        if data.role not in _VALID_ROLES:
            return JSONResponse(status_code=422, content={"detail": f"Geçersiz rol: '{data.role}'."})
        user.role = data.role

    if data.full_name is not None and data.full_name.strip():
        user.full_name = data.full_name.strip()

    if data.is_active is not None:
        user.is_active = data.is_active

    db.commit()
    return {"id": user.id, "message": f"Kullanıcı '{user.username}' güncellendi."}


@router.put("/admin/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: int,
    data: PasswordResetRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("admin", "edit")),
):
    """Kullanıcı şifresini zorla sıfırla."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse(status_code=404, content={"detail": "Kullanıcı bulunamadı."})
    if len(data.new_password) < 6:
        return JSONResponse(status_code=422, content={"detail": "Şifre en az 6 karakter olmalıdır."})

    import bcrypt as _bcrypt
    user.password_hash = _bcrypt.hashpw(data.new_password.encode(), _bcrypt.gensalt()).decode()
    db.commit()
    return {"message": f"'{user.username}' kullanıcısının şifresi başarıyla sıfırlandı."}


# ─── Permission Matrix Endpoints (RBAC 2.0) ─────────────────────────────────

@router.get("/admin/permission-schema")
def admin_permission_schema(_: dict = Depends(require_permission("admin", "view"))):
    """Returns the catalogue of categories × actions — drives the admin matrix UI."""
    return {"categories": PERMISSION_CATEGORIES}


@router.get("/admin/users/{user_id}/permissions")
def admin_get_user_permissions(
    user_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("admin", "view")),
):
    """
    Returns the EFFECTIVE permission set for a user, plus a flag indicating
    whether they're using custom perms (`source: 'custom'`) or role defaults
    (`source: 'role-default'`). SuperAdmin always returns 'superadmin'.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse(status_code=404, content={"detail": "Kullanıcı bulunamadı."})

    if user.role == "SuperAdmin":
        source = "superadmin"
    elif user.permissions:
        source = "custom"
    else:
        source = "role-default"

    return {
        "user_id":     user.id,
        "username":    user.username,
        "full_name":   user.full_name,
        "role":        user.role,
        "source":      source,
        "permissions": _resolve_permissions(user),
    }


@router.put("/admin/users/{user_id}/permissions")
def admin_update_user_permissions(
    user_id: int,
    data: PermissionsUpdateRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("admin", "edit")),
):
    """Persist a user-specific permission set as JSON. SuperAdmin perms are immutable."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse(status_code=404, content={"detail": "Kullanıcı bulunamadı."})
    if user.role == "SuperAdmin":
        return JSONResponse(status_code=400, content={
            "detail": "SuperAdmin yetkileri değiştirilemez (her zaman tüm yetkilere sahiptir)."
        })

    # Sanitize: only accept categories/actions defined in PERMISSION_CATEGORIES
    cleaned = {}
    for cat, actions in PERMISSION_CATEGORIES.items():
        cleaned[cat] = {}
        for act in actions:
            cleaned[cat][act] = bool(data.permissions.get(cat, {}).get(act, False))

    user.permissions = _json.dumps(cleaned, ensure_ascii=False)
    db.commit()
    return {
        "message":     f"'{user.username}' yetkileri güncellendi.",
        "permissions": cleaned,
    }


@router.post("/admin/users/{user_id}/permissions/reset")
def admin_reset_user_permissions(
    user_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("admin", "edit")),
):
    """Clear custom permissions → user reverts to role defaults."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse(status_code=404, content={"detail": "Kullanıcı bulunamadı."})
    user.permissions = None
    db.commit()
    return {"message": f"'{user.username}' yetkileri rol varsayılanlarına döndürüldü."}
