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
    "SuperAdmin":  "Süper Yönetici",
    "Manager":     "Yönetici",
    "LabLead":     "Lab Sorumlusu",
    "LabTech":     "Lab Teknisyeni",
    "Staff":       "Personel",
    "Distributor": "Distribütör",
}

# Rollerin GÖRÜNEN ADLARI SuperAdmin tarafından özelleştirilebilir
# (app_setting key='role_label.<rol>').  Rol ANAHTARI (yetkiyi belirleyen) asla
# değişmez — yalnızca etiket.  Süreç-içi cache; güncellemede invalidate edilir.
ROLE_KEYS = ["SuperAdmin", "Manager", "LabLead", "LabTech", "Staff", "Distributor"]
_role_label_cache = None


def get_role_labels(db=None) -> dict:
    """Varsayılan + (varsa) özelleştirilmiş rol etiketleri.  db verilmezse
    süreç-içi cache'ten döner (yoksa kısa bir oturum açıp doldurur)."""
    global _role_label_cache
    if db is None and _role_label_cache is not None:
        return dict(_role_label_cache)
    labels = dict(_ROLE_LABELS)
    own = False
    if db is None:
        from database import SessionLocal
        db = SessionLocal()
        own = True
    try:
        from database import AppSetting
        for s in db.query(AppSetting).filter(AppSetting.key.like("role_label.%")).all():
            k = s.key.split(".", 1)[1]
            if k in _ROLE_LABELS and (s.value or "").strip():
                labels[k] = s.value.strip()
    except Exception:
        pass
    finally:
        if own:
            db.close()
    _role_label_cache = dict(labels)
    return dict(labels)


def invalidate_role_labels():
    """Rol etiketi güncellendiğinde cache'i temizle (sonraki okuma DB'den)."""
    global _role_label_cache
    _role_label_cache = None

_CAN_DELETE       = ["SuperAdmin", "LabLead"]
_CAN_EDIT_RECIPES = ["SuperAdmin", "LabLead"]
_SUPERADMIN_ONLY  = ["SuperAdmin"]
_FINANCE_ROLES    = ["SuperAdmin", "Manager"]   # Costs / margins / quotations / currency rates

_VALID_ROLES = {"SuperAdmin", "Manager", "LabLead", "LabTech", "Staff", "Distributor"}


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
    "crm":        ["view", "create", "edit", "delete"],   # Müşteri İlişkileri (cross-cutting)
    "reports":    ["view"],
    "admin":      ["view", "create", "edit", "delete", "import_excel", "view_audit", "backup"],
    # Distribütör sipariş portalı
    "portal":        ["view", "order"],                   # distribütör tarafı (self-servis sipariş)
    "distributors":  ["view", "create", "edit", "prices"],  # personel tarafı (hesap + fiyat yönetimi)
    # PDKS — personel devam takibi (cross-cutting).  check = kendi giriş/çıkışı;
    # view_own = kendi puantajı; view_all = tüm personelin günü/izinleri;
    # manage = personel+program+izin+manuel düzeltme+doğrulama ayarları;
    # report = aylık puantaj; kiosk = girişteki dönen-QR ekranını görüntüleme
    # (yalnız özel bir kiosk cihaz hesabına per-user override ile verilir).
    "pdks":          ["check", "view_own", "view_all", "manage", "report", "kiosk"],
    # Şahit numune dolabı — üretimden ayrılan numunelerin saklanması.
    # checkout = dolaptan numune alma (stoktan da düşer), destroy = imha.
    "retention":     ["view", "edit", "checkout", "destroy"],
    # Ürün yorumları (mağaza vitrini, sesli not destekli) — cross-cutting.
    # invite = davet linki üretme, moderate = onay/red/yayından kaldırma,
    # delete = KVKK silme (metaobject+dosya Shopify'dan da kalkar).
    "reviews":       ["view", "invite", "moderate", "delete"],
    # Influencer / creator programı (cross-cutting).  approve = başvuru +
    # içerik + karar onayı, ship = ürün gönderimi (Delivery üzerinden),
    # links = UpPromote link/kod eşlemesi, payout = ödeme kaydı,
    # settings = kademe/benchmark/skaler ayarlar.
    "influencer":    ["view", "edit", "approve", "ship", "links", "payout", "settings"],
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
        # CRM erişimi varsayılan KAPALI — SuperAdmin her zaman açık; diğer
        # kullanıcılara erişim yetki matrisinden (admin paneli) elle verilir.
        "crm":        {"view": False, "create": False, "edit": False, "delete": False},
        "reports":    {"view": True},
        "admin":      {"view": False, "create": False, "edit": False, "delete": False, "import_excel": False, "view_audit": True, "backup": False},
        "portal":        {"view": False, "order": False},
        "distributors":  {"view": True,  "create": True,  "edit": True,  "prices": True},
        "pdks":          {"check": True, "view_own": True, "view_all": True, "manage": True, "report": True, "kiosk": False},
        "retention":          {"view": True,  "edit": True,  "checkout": True,  "destroy": True},
        "reviews":       {"view": True,  "invite": True,  "moderate": True,  "delete": True},
        "influencer":    {"view": True,  "edit": True,  "approve": True,  "ship": True,  "links": True,  "payout": True,  "settings": True},
    },
    "LabLead": {
        "items":      {"view": True,  "create": True,  "edit": True,  "delete": True,  "import": True},
        "recipes":    {"view": True,  "create": True,  "edit": True,  "delete": True},
        "inventory":  {"view": True,  "receive": True, "adjust": True,  "delete": True},
        "production": {"view": True,  "create": True},
        "qc":         {"view": True,  "approve": True},
        "finance":    {"view": False},
        "b2b":        {"view": False, "create": False, "confirm": False},
        "crm":        {"view": False, "create": False, "edit": False, "delete": False},
        "reports":    {"view": True},
        "admin":      {"view": False, "create": False, "edit": False, "delete": False, "import_excel": False, "view_audit": False, "backup": False},
        "pdks":       {"check": True, "view_own": True, "view_all": False, "manage": False, "report": False, "kiosk": False},
        "retention":       {"view": True,  "edit": True,  "checkout": True,  "destroy": True},
        "reviews":         {"view": False, "invite": False, "moderate": False, "delete": False},
        "influencer":    {"view": False, "edit": False, "approve": False, "ship": False, "links": False, "payout": False, "settings": False},
    },
    "LabTech": {
        "items":      {"view": True,  "create": True,  "edit": False, "delete": False, "import": False},
        "recipes":    {"view": True,  "create": False, "edit": False, "delete": False},
        "inventory":  {"view": True,  "receive": True, "adjust": False, "delete": False},
        "production": {"view": True,  "create": True},
        "qc":         {"view": True,  "approve": False},
        "finance":    {"view": False},
        "b2b":        {"view": False, "create": False, "confirm": False},
        "crm":        {"view": False, "create": False, "edit": False, "delete": False},
        "reports":    {"view": True},
        "admin":      {"view": False, "create": False, "edit": False, "delete": False, "import_excel": False, "view_audit": False, "backup": False},
        "pdks":       {"check": True, "view_own": True, "view_all": False, "manage": False, "report": False, "kiosk": False},
        "retention":       {"view": True,  "edit": True,  "checkout": True,  "destroy": False},
        "reviews":         {"view": False, "invite": False, "moderate": False, "delete": False},
        "influencer":    {"view": False, "edit": False, "approve": False, "ship": False, "links": False, "payout": False, "settings": False},
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
        "crm":        {"view": False, "create": False, "edit": False, "delete": False},
        "reports":    {"view": True},
        "admin":      {"view": False, "create": False, "edit": False, "delete": False, "import_excel": False, "view_audit": False, "backup": False},
        "pdks":       {"check": True, "view_own": True, "view_all": False, "manage": False, "report": False, "kiosk": False},
        "retention":       {"view": True,  "edit": False, "checkout": False, "destroy": False},
        "reviews":         {"view": False, "invite": False, "moderate": False, "delete": False},
        "influencer":    {"view": False, "edit": False, "approve": False, "ship": False, "links": False, "payout": False, "settings": False},
    },
    "Distributor": {
        # Dışa dönük distribütör — YALNIZ sipariş portalı, ERP'ye hiçbir erişim yok.
        "items":        {"view": False, "create": False, "edit": False, "delete": False, "import": False},
        "recipes":      {"view": False, "create": False, "edit": False, "delete": False},
        "inventory":    {"view": False, "receive": False, "adjust": False, "delete": False},
        "production":   {"view": False, "create": False},
        "qc":           {"view": False, "approve": False},
        "finance":      {"view": False},
        "b2b":          {"view": False, "create": False, "confirm": False},
        "crm":          {"view": False, "create": False, "edit": False, "delete": False},
        "reports":      {"view": False},
        "admin":        {"view": False, "create": False, "edit": False, "delete": False, "import_excel": False, "view_audit": False, "backup": False},
        "portal":       {"view": True,  "order": True},
        "distributors": {"view": False, "create": False, "edit": False, "prices": False},
        "pdks":         {"check": False, "view_own": False, "view_all": False, "manage": False, "report": False, "kiosk": False},
        "retention":         {"view": False, "edit": False, "checkout": False, "destroy": False},
        "reviews":           {"view": False, "invite": False, "moderate": False, "delete": False},
        "influencer":    {"view": False, "edit": False, "approve": False, "ship": False, "links": False, "payout": False, "settings": False},
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
            # DİKKAT: raise şart — dependency'nin RETURN ettiği Response isteği
            # kısa devre YAPMAZ (endpoint'e parametre olarak enjekte edilir ve
            # endpoint çalışırdı → pasifleştirilmiş kullanıcı auth bypass'ı).
            raise HTTPException(status_code=401, detail="Yetkisiz.")
        if not _has_permission(user, category, action):
            raise HTTPException(
                status_code=403,
                detail=f"Yetersiz yetki: {category}.{action}",
            )
        return payload
    return _dep


def require_any_permission(*pairs):
    """`require_permission`'ın "en az biri" hâli — birden fazla sayfanın
    çağırdığı ortak okuma uçları için (ör. /api/suppliers hem Ürünler hem Mal
    Kabul hem Raporlar sayfasından çağrılır; her sayfanın kendi kapısı var).
        Depends(require_any_permission(("items", "view"), ("inventory", "view")))
    Kullanıcı DB'den canlı okunur; pasif kullanıcı 401, hiçbiri yoksa 403."""
    label = " | ".join(f"{c}.{a}" for c, a in pairs)

    def _dep(payload: dict = Depends(get_current_user), db: Session = Depends(get_db)):
        user = db.query(User).filter(User.id == int(payload.get("sub", 0))).first()
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="Yetkisiz.")
        if not any(_has_permission(user, c, a) for c, a in pairs):
            raise HTTPException(status_code=403, detail=f"Yetersiz yetki: {label}")
        return payload
    return _dep


def require_internal_user(*pairs):
    """Yalnız iç kullanıcı: aktif ve Distributor DEĞİL; `pairs` verilirse ayrıca
    bunlardan en az biri.  Yalnız girişe bakan (`get_current_user`) okuma uçları
    dış bayi hesabına, kiosk tabletine (yalnız pdks.kiosk yetkili, 10 yıllık
    token) ve pasifleştirilmiş kullanıcının hâlâ geçerli token'ına açıktı
    (reçete formülleri, Drive, izlenebilirlik).  Rol JWT'den değil DB'den CANLI
    okunur ve dönen payload'a o yazılır — `_can_see_finance(payload)` bayat
    token rolüyle karar vermesin."""
    label = " | ".join(f"{c}.{a}" for c, a in pairs)

    def _dep(payload: dict = Depends(get_current_user), db: Session = Depends(get_db)):
        user = db.query(User).filter(User.id == int(payload.get("sub", 0))).first()
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="Yetkisiz.")
        if (user.role or "") == "Distributor":
            raise HTTPException(status_code=403, detail="Bu bölüm bayi hesaplarına kapalı.")
        if pairs and not any(_has_permission(user, c, a) for c, a in pairs):
            raise HTTPException(status_code=403, detail=f"Yetersiz yetki: {label}")
        return {**payload, "role": user.role}
    return _dep
