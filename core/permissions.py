# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Role labels, permission catalog, and permission-resolution helpers.
Shared across api_main and every router so we have a single source of truth.

Step 1 (refactoring) keeps the existing `require_role` dependency in core.auth.
Step 2 (RBAC migration) will swap call-sites to `require_permission` defined here.
"""
import json as _json
from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from database import get_db, User
from core.auth import get_current_user


# ─── Role helpers ───────────────────────────────────────────────────────────

_ROLE_LABELS = {
    "SuperAdmin": "Süper Yönetici",
    "Manager":    "Yönetici",
    "LabLead":    "Lab Sorumlusu",
    "LabTech":    "Lab Teknisyeni",
}

_CAN_DELETE       = ["SuperAdmin", "LabLead"]
_CAN_EDIT_RECIPES = ["SuperAdmin", "LabLead"]
_SUPERADMIN_ONLY  = ["SuperAdmin"]
_FINANCE_ROLES    = ["SuperAdmin", "Manager"]   # Costs / margins / quotations / currency rates

_VALID_ROLES = {"SuperAdmin", "Manager", "LabLead", "LabTech", "Staff"}


def _can_see_finance(payload_or_role) -> bool:
    """True if the user's role is allowed to view financial data."""
    role = payload_or_role.get("role") if isinstance(payload_or_role, dict) else payload_or_role
    return role in _FINANCE_ROLES


# ─── RBAC 2.0 — Granular Permission Matrix (Phase 4 / Task 2) ───────────────

# Single source of truth for the permission tree. Adding a new permission here
# automatically makes it appear in the admin UI matrix.
PERMISSION_CATEGORIES = {
    "items":      ["view", "create", "edit", "delete", "import"],
    "recipes":    ["view", "create", "edit", "delete"],
    "inventory":  ["view", "receive", "adjust", "delete"],
    "production": ["view", "create"],
    "qc":         ["view", "approve"],
    "finance":    ["view"],            # Cost prices, recipe BOM costs, margins
    "b2b":        ["view", "create", "confirm"],
    "reports":    ["view"],
    "admin":      ["view", "create", "edit", "delete", "import_excel", "view_audit", "backup"],
}

# Default permission set per role — used when a user's `permissions` JSON is null.
# SuperAdmin is special-cased (always allowed) so doesn't need an entry.
_DEFAULT_PERMISSIONS = {
    "Manager": {
        "items":      {"view": True,  "create": True,  "edit": True,  "delete": False, "import": True},
        "recipes":    {"view": True,  "create": False, "edit": False, "delete": False},
        "inventory":  {"view": True,  "receive": True, "adjust": True,  "delete": False},
        "production": {"view": True,  "create": True},
        "qc":         {"view": True,  "approve": True},
        "finance":    {"view": True},
        "b2b":        {"view": True,  "create": True,  "confirm": True},
        "reports":    {"view": True},
        "admin":      {"view": False, "create": False, "edit": False, "delete": False, "import_excel": False, "view_audit": True, "backup": False},
    },
    "LabLead": {
        "items":      {"view": True,  "create": True,  "edit": True,  "delete": True,  "import": True},
        "recipes":    {"view": True,  "create": True,  "edit": True,  "delete": True},
        "inventory":  {"view": True,  "receive": True, "adjust": True,  "delete": True},
        "production": {"view": True,  "create": True},
        "qc":         {"view": True,  "approve": True},
        "finance":    {"view": False},
        "b2b":        {"view": False, "create": False, "confirm": False},
        "reports":    {"view": True},
        "admin":      {"view": False, "create": False, "edit": False, "delete": False, "import_excel": False, "view_audit": False, "backup": False},
    },
    "LabTech": {
        "items":      {"view": True,  "create": True,  "edit": False, "delete": False, "import": False},
        "recipes":    {"view": True,  "create": False, "edit": False, "delete": False},
        "inventory":  {"view": True,  "receive": True, "adjust": False, "delete": False},
        "production": {"view": True,  "create": True},
        "qc":         {"view": True,  "approve": False},
        "finance":    {"view": False},
        "b2b":        {"view": False, "create": False, "confirm": False},
        "reports":    {"view": True},
        "admin":      {"view": False, "create": False, "edit": False, "delete": False, "import_excel": False, "view_audit": False, "backup": False},
    },
    "Staff": {
        # Read-only baseline
        "items":      {"view": True,  "create": False, "edit": False, "delete": False, "import": False},
        "recipes":    {"view": True,  "create": False, "edit": False, "delete": False},
        "inventory":  {"view": True,  "receive": False, "adjust": False, "delete": False},
        "production": {"view": True,  "create": False},
        "qc":         {"view": True,  "approve": False},
        "finance":    {"view": False},
        "b2b":        {"view": False, "create": False, "confirm": False},
        "reports":    {"view": True},
        "admin":      {"view": False, "create": False, "edit": False, "delete": False, "import_excel": False, "view_audit": False, "backup": False},
    },
}


def _resolve_permissions(user: User) -> dict:
    """
    Compute the EFFECTIVE permission set for a user. Order of precedence:
      1) SuperAdmin → all True (no override possible)
      2) user.permissions JSON if set → that's the source of truth
      3) Otherwise, fall back to _DEFAULT_PERMISSIONS[role]
    """
    if user.role == "SuperAdmin":
        return {cat: {act: True for act in acts} for cat, acts in PERMISSION_CATEGORIES.items()}
    if user.permissions:
        try:
            return _json.loads(user.permissions)
        except Exception:
            pass
    return _DEFAULT_PERMISSIONS.get(user.role, _DEFAULT_PERMISSIONS["Staff"])


def _has_permission(user: User, category: str, action: str) -> bool:
    perms = _resolve_permissions(user)
    return bool(perms.get(category, {}).get(action, False))


def require_permission(category: str, action: str):
    """
    FastAPI dependency factory. Raises 403 if the current user lacks the permission.
    Usage:
        @router.delete(...)
        def endpoint(_: dict = Depends(require_permission("items", "delete"))):
            ...
    """
    def _dep(payload: dict = Depends(get_current_user), db: Session = Depends(get_db)):
        user = db.query(User).filter(User.id == int(payload.get("sub", 0))).first()
        if not user or not user.is_active:
            return JSONResponse(status_code=401, content={"detail": "Yetkisiz."})
        if not _has_permission(user, category, action):
            raise HTTPException(
                status_code=403,
                detail=f"Yetersiz yetki: {category}.{action}",
            )
        return payload
    return _dep
