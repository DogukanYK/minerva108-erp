"""
Admin user management + permission matrix endpoints.
"""
import json as _json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional

from database import get_db, User
from core.limiter import limiter
from core.audit import log_admin_event
from core.password_strength import validate_password_strength
from core.permissions import (
    _VALID_ROLES,
    PERMISSION_CATEGORIES,
    _resolve_permissions,
    require_permission,
)

router = APIRouter(prefix="/api", tags=["users"])


# ─── Schemas ────────────────────────────────────────────────────────────────
# Min şifre uzunluğu — NIST 2024 önerisi 8+. Biz 10'u baz aldık; kullanıcı
# türü idari (lab), spesifik karakter zorunluluğu yok (zayıflatmaz, hatırlatma
# kolay).  Bu sabit hem create hem reset'te kullanılır.
MIN_PASSWORD_LEN = 10


class AdminUserCreateRequest(BaseModel):
    username:  str = Field(..., min_length=3, max_length=50)
    full_name: str = Field(..., min_length=2, max_length=100)
    password:  str = Field(..., min_length=MIN_PASSWORD_LEN, max_length=128)
    role:      str = Field("LabTech", max_length=20)


class AdminUserUpdateRequest(BaseModel):
    full_name: Optional[str]  = Field(None, max_length=100)
    role:      Optional[str]  = Field(None, max_length=20)
    is_active: Optional[bool] = None


class PasswordResetRequest(BaseModel):
    new_password: str = Field(..., min_length=MIN_PASSWORD_LEN, max_length=128)


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
    request: Request,
    data: AdminUserCreateRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("admin", "create")),
):
    """Yeni kullanıcı hesabı oluştur."""
    if data.role not in _VALID_ROLES:
        return JSONResponse(status_code=422, content={"detail": f"Geçersiz rol: '{data.role}'."})
    pw_ok, pw_err = validate_password_strength(data.password)
    if not pw_ok:
        return JSONResponse(status_code=422, content={"detail": pw_err})

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

    log_admin_event(db, request, current_user,
                    action="user.create",
                    target_type="user", target_id=user.id, target_name=user.username,
                    details={"role": user.role, "full_name": user.full_name})
    return {"id": user.id, "message": f"Kullanıcı '{data.username}' başarıyla oluşturuldu."}


@router.put("/admin/users/{user_id}")
def admin_update_user(
    request: Request,
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

    # Eski değerleri yakala (audit için before/after)
    before = {"role": user.role, "is_active": user.is_active, "full_name": user.full_name}

    if data.role is not None:
        if data.role not in _VALID_ROLES:
            return JSONResponse(status_code=422, content={"detail": f"Geçersiz rol: '{data.role}'."})
        user.role = data.role

    if data.full_name is not None and data.full_name.strip():
        user.full_name = data.full_name.strip()

    if data.is_active is not None:
        user.is_active = data.is_active

    db.commit()

    after = {"role": user.role, "is_active": user.is_active, "full_name": user.full_name}
    # Sadece gerçekten değişen alanları log'la
    changes = {k: {"from": before[k], "to": after[k]} for k in before if before[k] != after[k]}
    if changes:
        action = "user.deactivate" if data.is_active is False and before["is_active"] else "user.update"
        log_admin_event(db, request, current_user,
                        action=action,
                        target_type="user", target_id=user.id, target_name=user.username,
                        details=changes)
    return {"id": user.id, "message": f"Kullanıcı '{user.username}' güncellendi."}


@router.put("/admin/users/{user_id}/reset-password")
@limiter.limit("10/hour")    # Sıkı: kötü niyetli admin/compromised hesap abuse'ı sınırla
def admin_reset_password(
    request: Request,
    user_id: int,
    data: PasswordResetRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("admin", "edit")),
):
    """Kullanıcı şifresini zorla sıfırla."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse(status_code=404, content={"detail": "Kullanıcı bulunamadı."})
    pw_ok, pw_err = validate_password_strength(data.new_password)
    if not pw_ok:
        return JSONResponse(status_code=422, content={"detail": pw_err})

    import bcrypt as _bcrypt
    user.password_hash = _bcrypt.hashpw(data.new_password.encode(), _bcrypt.gensalt()).decode()
    db.commit()

    log_admin_event(db, request, current_user,
                    action="user.password_reset",
                    target_type="user", target_id=user.id, target_name=user.username,
                    details={"reset_by": current_user.get("username")})
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
    request: Request,
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
    log_admin_event(db, request, current_user,
                    action="permissions.update",
                    target_type="user", target_id=user.id, target_name=user.username,
                    details={"new_permissions": cleaned})
    return {
        "message":     f"'{user.username}' yetkileri güncellendi.",
        "permissions": cleaned,
    }


@router.post("/admin/users/{user_id}/permissions/reset")
def admin_reset_user_permissions(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("admin", "edit")),
):
    """Clear custom permissions → user reverts to role defaults."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return JSONResponse(status_code=404, content={"detail": "Kullanıcı bulunamadı."})
    user.permissions = None
    db.commit()
    log_admin_event(db, request, current_user,
                    action="permissions.reset",
                    target_type="user", target_id=user.id, target_name=user.username)
    return {"message": f"'{user.username}' yetkileri rol varsayılanlarına döndürüldü."}


# ─── Audit log okuma endpoint'i ─────────────────────────────────────────────

@router.get("/admin/audit-log")
def admin_audit_log_feed(
    limit: int = 200,
    action: Optional[str] = None,    # 'user.create', 'user.password_reset', vb.
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("admin", "view_audit")),
):
    """
    Admin audit log feed — son N event (default 200).  Bu tablo append-only;
    silinmiş kullanıcı/işlem bile burada görünür.  Action filtresi opsiyonel.
    """
    from database import AdminAuditLog
    q = db.query(AdminAuditLog)
    if action:
        q = q.filter(AdminAuditLog.action == action[:50])
    rows = q.order_by(AdminAuditLog.id.desc()).limit(max(1, min(limit, 1000))).all()
    return [
        {
            "id":          r.id,
            "timestamp":   r.timestamp.strftime("%d.%m.%Y %H:%M:%S") if r.timestamp else "—",
            "actor_id":    r.actor_id,
            "actor_name":  r.actor_name,
            "action":      r.action,
            "target_type": r.target_type,
            "target_id":   r.target_id,
            "target_name": r.target_name,
            "details":     r.details,
            "ip_address":  r.ip_address,
        }
        for r in rows
    ]
