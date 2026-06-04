# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Panel (domain) geçiş ucu — Kozmetik ↔ Food Supplement.
"""
import os

from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.auth import get_current_user
from core.domain import (
    normalize_domain, get_active_domain, domain_label,
    DOMAIN_COOKIE, VALID_DOMAINS,
)

router = APIRouter(prefix="/api", tags=["domain"])

# Çerez güvenli bayrağı — prod HTTPS'te true, lokalde COOKIE_SECURE=false
_COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() not in ("0", "false", "no")


class DomainSwitchRequest(BaseModel):
    domain: str


@router.get("/domain")
def current_domain(request: Request, _: dict = Depends(get_current_user)):
    """Aktif panel + seçilebilir paneller."""
    d = get_active_domain(request)
    return {
        "domain": d,
        "label":  domain_label(d),
        "domains": [{"value": v, "label": domain_label(v)} for v in VALID_DOMAINS],
    }


@router.post("/domain/switch")
def switch_domain(data: DomainSwitchRequest, _: dict = Depends(get_current_user)):
    """Aktif paneli değiştir — `active_domain` çerezini set eder."""
    d = normalize_domain(data.domain)
    resp = JSONResponse({"domain": d, "label": domain_label(d), "redirect": "/"})
    resp.set_cookie(
        DOMAIN_COOKIE, d,
        max_age=60 * 60 * 24 * 365,   # 1 yıl
        httponly=True, samesite="lax", secure=_COOKIE_SECURE, path="/",
    )
    return resp
