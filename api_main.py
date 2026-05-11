"""
Minerva108 ERP — central FastAPI hub.

This module is intentionally lean. All API endpoints have been split into
the routers/ package; the only things that live here are:

  • App + middleware/limiter wiring
  • Startup hooks (DB init)
  • Server-rendered Jinja page routes (the actual UI)
  • include_router(...) calls that mount each domain's APIRouter

Domain logic, schemas, and helpers live alongside the endpoints in their
respective router modules — see routers/.
"""
import os
from typing import Optional

from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sqlalchemy.orm import Session
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from database import init_db, get_db, User
from core.auth import decode_token
from core.limiter import limiter
from core.permissions import _ROLE_LABELS, _has_permission, _resolve_permissions
from core.scheduler import start_scheduler, stop_scheduler

from routers import auth, users, inventory, recipes, production, b2b, reports, notifications, backup


# ─── App init ───────────────────────────────────────────────────────────────
# /api/docs (FastAPI Swagger) — auth-suz tüm endpoint listesini sergiler.
# Prod'da kapalı olmalı (saldırı yüzeyini azaltır). Dev'de EXPOSE_API_DOCS=true
# ile aç.  Default: kapalı (None) — prod'a yeni kullanıcı dahil olduğunda da
# güvenli kalır.
_EXPOSE_DOCS = os.getenv("EXPOSE_API_DOCS", "false").lower() in ("1", "true", "yes")
app = FastAPI(
    title="Minerva108 ERP",
    version="2.0.0",
    docs_url="/api/docs"   if _EXPOSE_DOCS else None,
    redoc_url="/api/redoc" if _EXPOSE_DOCS else None,
    openapi_url="/api/openapi.json" if _EXPOSE_DOCS else None,
)


# ─── Security headers middleware ────────────────────────────────────────────
# Tüm response'lara defansif HTTP header'ları ekler.  CSP "self + Bootstrap/
# CDN + html5-qrcode" — frontend mevcut CDN'lerden script çekiyor, onları
# whitelist'liyoruz.  CSP genel sıkı kalıyor — XSS olsa script çalışamaz.
#
# 'unsafe-inline' hâlâ aktif çünkü Jinja sayfalarında inline <script> blokları
# var.  Tamamen kaldırmak büyük bir frontend refactoru gerektirir (her
# sayfa için ayrı .js).  Bu seferki olgunlaştırma turunda alternatif olarak
# nonce-based CSP'ye geçeriz: middleware her response için 16-byte rastgele
# nonce üretir, response.headers'a CSP-with-nonce ekler, ve template'ler
# inline script'lerini <script nonce="{{ csp_nonce }}"> ile imzalar.
# Şu an Jinja template'lerini hepsini güncellemek yerine (50+ inline blok),
# unsafe-inline'ı tutuyoruz ve XSS'i savunmanın çoğunu CSP'nin diğer
# direktifleri (script-src whitelist, frame-ancestors none, base-uri self,
# form-action self) ile sağlıyoruz.  Bu pragmatik denge.
from starlette.middleware.base import BaseHTTPMiddleware


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        # Content-Security-Policy: çoğu XSS senaryosunu sıfırlar
        # 'unsafe-eval' YASAK — eval() veya new Function() ile saldırı yapılamaz
        # 'object-src none' — Flash/PDF/applet vektörlerini kapatır
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' "
            "  https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' "
            "  https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "font-src 'self' data: https://cdn.jsdelivr.net https://fonts.gstatic.com; "
            "img-src 'self' data: blob: https:; "
            "connect-src 'self'; "
            "object-src 'none'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        # Clickjacking koruması (frame-ancestors zaten kapsar ama legacy uyumluluk)
        response.headers["X-Frame-Options"] = "DENY"
        # MIME-sniff koruması
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Referrer politikası: cross-origin'a referer sızdırma
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # HSTS — sadece prod HTTPS'te anlamlı; lokalde tarayıcı kabul etmez ama zararsız
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Browser API gating — biz sadece kamera (barkod scan) kullanıyoruz
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), payment=(), usb=()"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)


# ─── CSRF: Origin/Referer kontrolü (SameSite=Lax üzerine ek katman) ─────────
# Cookie'miz SameSite=Lax — bu zaten 3rd-party POST/PUT/DELETE'leri büyük
# oranda engelliyor.  Buna ek olarak: state-değiştiren isteklerde Origin
# (varsa) ya da Referer header'ının kendi origin'imizle uyumlu olduğunu
# kontrol et.  Uyumsuz → 403.  GET ve HEAD muaf (state değiştirmez).
#
# Yardımcı script'lerden (curl, postman, vb.) gelen isteklerde Origin yok ve
# bu kontrol onları kırmaz.  Ama tarayıcı tabanlı saldırılarda her zaman
# Origin header'ı set edilir — eksikse browser değil demektir.
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_CSRF_EXEMPT_PATHS = {"/api/login", "/api/logout"}  # login kendi auth'unu CSRF korur


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method in _CSRF_SAFE_METHODS:
            return await call_next(request)
        if request.url.path in _CSRF_EXEMPT_PATHS:
            return await call_next(request)
        # Cookie tabanlı auth değilse (örn token-only API çağrısı) muaf —
        # bizde access_token cookie var, dolayısıyla saldırı yüzeyi cookie.
        if "access_token" not in request.cookies:
            return await call_next(request)

        origin  = request.headers.get("origin")
        referer = request.headers.get("referer", "")
        host    = request.headers.get("host", "")

        if origin:
            # Origin "https://example.com" formatında; host'la eşleşmeli
            ok = origin.endswith(f"://{host}") or origin.endswith(f"@{host}")
        elif referer:
            # Referer "https://example.com/page" — host kısmı eşleşmeli
            ok = f"//{host}/" in referer or f"//{host}" == referer.rstrip("/")
        else:
            # Browser her zaman Origin/Referer gönderir; ikisi de yoksa
            # çağrı browser dışından (curl/CLI/script).  Cookie de göndermesi
            # zor — yine de güvenli tarafta kalıp reddedelim.
            ok = False

        if not ok:
            return JSONResponse(
                status_code=403,
                content={"detail": "CSRF doğrulaması başarısız (Origin/Referer eşleşmedi)."},
            )
        return await call_next(request)


app.add_middleware(CSRFMiddleware)


# ─── Rate limiter wiring (P0 / brute-force protection) ──────────────────────
# The Limiter object itself lives in core.limiter so any router can decorate
# endpoints with @limiter.limit(...) without circular-importing api_main.
# SlowAPIMiddleware default_limits'in tüm endpoint'lere otomatik uygulanmasını
# sağlar — bu olmadan sadece @limiter.limit decoratör'lü endpoint'ler korunur.
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


@app.exception_handler(RateLimitExceeded)
def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """Friendly Turkish error when a client hits the rate limit."""
    retry_after = getattr(exc, "retry_after", 60)
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Çok fazla deneme. Lütfen biraz bekleyip tekrar deneyin.",
            "retry_after_seconds": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )


# ─── Static + templates ─────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ─── PWA service worker ─────────────────────────────────────────────────────
# Served from `/sw.js` (not `/static/sw.js`) so its default scope is the entire
# site. A SW mounted under /static/ can only intercept /static/* requests and
# would never see page navigations — defeating the PWA install model.
@app.get("/sw.js", include_in_schema=False)
def serve_service_worker():
    return FileResponse("static/sw.js", media_type="application/javascript")


@app.on_event("startup")
def startup_event():
    init_db()
    # Test ortamında DISABLE_SCHEDULER=true → APScheduler atla.
    # Lifespan async cleanup'ı TestClient teardown'unda event loop
    # kapanırken hata atıyordu; testler için bu güvenli kapı.
    if os.getenv("DISABLE_SCHEDULER", "false").lower() not in ("1", "true", "yes"):
        start_scheduler()


@app.on_event("shutdown")
def shutdown_event():
    if os.getenv("DISABLE_SCHEDULER", "false").lower() not in ("1", "true", "yes"):
        stop_scheduler()


# ─── Mount domain routers ───────────────────────────────────────────────────

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(inventory.router)
app.include_router(recipes.router)
app.include_router(production.router)
app.include_router(b2b.router)
app.include_router(reports.router)
app.include_router(notifications.router)
app.include_router(backup.router)


# ─── Page-route helpers ─────────────────────────────────────────────────────

def _get_user_context(request: Request) -> Optional[dict]:
    """Decode the access_token cookie into the JWT payload, or None."""
    token = request.cookies.get("access_token")
    if not token:
        return None
    return decode_token(token)


def _resolve_active_user(payload: Optional[dict], db: Session) -> Optional[User]:
    """
    Load the live User row referenced by a decoded JWT payload.
    Returns None if the payload is missing or the user has been deleted/deactivated
    after the token was issued — so mid-session deactivation kicks the user out.
    """
    if not payload:
        return None
    user = db.query(User).filter(User.id == int(payload.get("sub", 0))).first()
    return user if (user and user.is_active) else None


def _user_can(user: Optional[User], category: str, action: str) -> bool:
    """Page-level permission gate — mirrors require_permission used at the API layer."""
    return bool(user) and _has_permission(user, category, action)


def _page_ctx(request: Request, payload: dict, user: User) -> dict:
    """
    Build the Jinja template context for an authenticated page.

    Resolves the current user's effective permissions ONCE per request and
    exposes both:
      • permissions — full {category: {action: bool}} dict (for advanced templates)
      • can(category, action) — callable shortcut for `{% if can('items', 'create') %}`

    Templates should prefer can() because it gracefully returns False on unknown
    category/action keys instead of raising — schema drift won't break pages.
    """
    role = payload.get("role", "")
    perms = _resolve_permissions(user)

    def _can(category: str, action: str) -> bool:
        return bool(perms.get(category, {}).get(action, False))

    return {
        "request":     request,
        "username":    payload.get("username"),
        "full_name":   payload.get("full_name"),
        "role":        role,
        "role_label":  _ROLE_LABELS.get(role, role),
        "user_id":     user.id,
        "permissions": perms,    # full dict — useful for debug/advanced template logic
        "can":         _can,     # callable — primary template API
    }


# ─── Page Routes ─────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    token = request.cookies.get("access_token")
    if token and decode_token(token):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/", response_class=HTMLResponse)
def root(request: Request, db: Session = Depends(get_db)):
    """Dashboard — open to any authenticated user; the data fetched on this
    page is reports.view-gated at the API layer, so users without that perm
    will simply see empty cards rather than be bounced into a redirect loop."""
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not user: return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("index.html", _page_ctx(request, payload, user))


@app.get("/items", response_class=HTMLResponse)
def items_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "items", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("items.html", _page_ctx(request, payload, user))


@app.get("/suppliers", response_class=HTMLResponse)
def suppliers_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "items", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("suppliers.html", _page_ctx(request, payload, user))


@app.get("/recipes", response_class=HTMLResponse)
def recipes_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "recipes", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("recipes.html", _page_ctx(request, payload, user))


@app.get("/production", response_class=HTMLResponse)
def production_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "production", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("production.html", _page_ctx(request, payload, user))


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "reports", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("reports.html", _page_ctx(request, payload, user))


@app.get("/ledger", response_class=HTMLResponse)
def ledger_page(request: Request, db: Session = Depends(get_db)):
    """Pure transactions / audit-trail view. Stocks moved out to /stocks (Bug 4)."""
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "reports", "view"):
        return RedirectResponse(url="/", status_code=302)
    ctx = _page_ctx(request, payload, user)
    ctx["default_tab"] = "txs"
    ctx["single_view"] = True
    return templates.TemplateResponse("ledger.html", ctx)


@app.get("/stocks", response_class=HTMLResponse)
def stocks_page(request: Request, db: Session = Depends(get_db)):
    """Pure inventory view — splits stocks out of the financial ledger (Bug 4)."""
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "inventory", "view"):
        return RedirectResponse(url="/", status_code=302)
    ctx = _page_ctx(request, payload, user)
    ctx["default_tab"] = "stocks"
    ctx["single_view"] = True
    return templates.TemplateResponse("ledger.html", ctx)


@app.get("/qc", response_class=HTMLResponse)
def qc_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "qc", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("qc.html", _page_ctx(request, payload, user))


@app.get("/receiving", response_class=HTMLResponse)
def receiving_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "inventory", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("receiving.html", _page_ctx(request, payload, user))


@app.get("/traceability", response_class=HTMLResponse)
def traceability_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "inventory", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("traceability.html", _page_ctx(request, payload, user))


@app.get("/quotations", response_class=HTMLResponse)
def quotations_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "b2b", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("quotations.html", _page_ctx(request, payload, user))


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "admin", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("admin.html", _page_ctx(request, payload, user))
