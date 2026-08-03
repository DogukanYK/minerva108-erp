# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
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

from database import init_db, get_db, User, log_system_event
from core.auth import decode_token
from core.limiter import limiter
from core.permissions import _ROLE_LABELS, _has_permission, _resolve_permissions, get_role_labels
from core.scheduler import start_scheduler, stop_scheduler

from routers import auth, users, inventory, recipes, production, b2b, reports, notifications, backup, debug, undo, system, domain as domain_router, drive as drive_router, crm as crm_router, kommo as kommo_router, crm_c as crm_c_router, delivery as delivery_router, distributors as distributors_router, portal as portal_router, returns as returns_router, sample_analysis as sample_analysis_router, shopify as shopify_router, pdks as pdks_router
from core.domain import get_active_domain, domain_label


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
        path = request.url.path
        # Drive önizleme HAM ucu (foto/PDF): aynı-origin iframe/img ile gösterilebilsin.
        # YALNIZ bu yol için çerçeveleme 'self'e açılır; script yine bloklu
        # (default-src 'none' → kötücül SVG bile script çalıştıramaz).  Gerisi kilitli.
        preview_raw = path.startswith("/api/drive/files/") and path.endswith("/preview/raw")
        if preview_raw:
            # CSP frame-ancestors 'self' aynı-origin iframe'e izin verir; script yine
            # bloklu (default-src 'none' → kötücül SVG çalışamaz).
            # ÖNEMLİ: X-Frame-Options'ı BURADA SET ETME.  Önde nginx zaten 'SAMEORIGIN'
            # ekliyor; app de eklerse başlık ÇİFTLENİR ve Chrome iframe'i "conflicting
            # X-Frame-Options" diye engeller ("refused to connect").  Tek kaynak = nginx.
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; img-src 'self'; object-src 'self'; "
                "style-src 'unsafe-inline'; frame-ancestors 'self'"
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            return response
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
        # Browser API gating — kamera (barkod/QR scan) + konum (PDKS check-in
        # doğrulaması) same-origin'e açık; geri kalanı kapalı.
        response.headers["Permissions-Policy"] = (
            "geolocation=(self), microphone=(), payment=(), usb=()"
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
        # Kommo webhook'u dış servisten gelir (tarayıcı değil) — Origin/Referer yok;
        # secret'lı path + (handler'da) hmac kontrolü korur.  CSRF'ten muaf.
        if request.url.path.startswith("/api/crm/integrations/kommo/webhook/"):
            return await call_next(request)
        # Shopify orders/paid webhook'u da dış servisten gelir (Origin yok);
        # HMAC (X-Shopify-Hmac-Sha256) handler'da ham gövde üzerinden doğrulanır.
        if request.url.path.startswith("/api/shopify/webhook/"):
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


# ─── Android TWA — Digital Asset Links ──────────────────────────────────────
# Served at the exact path Google requires (`/.well-known/assetlinks.json`) so a
# Trusted Web Activity APK (PWABuilder ile üretilen Android uygulaması) verifies
# its link to this origin and runs full-screen (no URL bar). Content = the APK
# signing key's SHA-256 fingerprint + package name; updated once the APK is built.
# Origin-aware: ims ve crm ayrı bağımsız uygulamalar → her alan adı yalnız kendi
# APK'sının paket+parmak izini doğrular.
@app.get("/.well-known/assetlinks.json", include_in_schema=False)
def serve_assetlinks(request: Request):
    fname = "assetlinks-crm.json" if _is_crm_host(request) else "assetlinks-ims.json"
    return FileResponse(f"static/{fname}", media_type="application/json")


@app.on_event("startup")
def startup_event():
    init_db()
    # Her açılışı sistem olay defterine yaz — aylık raporun restart/downtime
    # bölümü bu kayıtlardan üretilir.  log_system_event asla exception atmaz.
    log_system_event("app_start", "uygulama başlatıldı")
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
app.include_router(debug.router)
app.include_router(undo.router)
app.include_router(system.router)
app.include_router(domain_router.router)
app.include_router(drive_router.router)
app.include_router(drive_router.share_router)
app.include_router(crm_router.router)
app.include_router(crm_c_router.router)
app.include_router(kommo_router.router)
app.include_router(kommo_router.public_router)
app.include_router(delivery_router.router)
app.include_router(returns_router.router)
app.include_router(sample_analysis_router.router)
app.include_router(pdks_router.router)
app.include_router(shopify_router.router)
app.include_router(shopify_router.public_router)
app.include_router(distributors_router.router)
app.include_router(portal_router.router)


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

    active_domain = get_active_domain(request)
    return {
        "request":      request,
        "username":     payload.get("username"),
        "full_name":    payload.get("full_name"),
        "role":         role,
        "role_label":   get_role_labels().get(role, role),   # özelleştirilebilir etiket
        "role_labels":  get_role_labels(),                   # tüm rol→etiket (templates: window.ROLE_LABELS)
        "user_id":      user.id,
        "permissions":  perms,    # full dict — useful for debug/advanced template logic
        "can":          _can,     # callable — primary template API
        "domain":       active_domain,                 # Faz 3 — aktif panel
        "domain_label": domain_label(active_domain),
    }


# ─── Page Routes ─────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    # Zaten girişli + AKTİF + bu host'un sayfasına erişimi olan kullanıcı → uygulamaya
    # yönlt.  Aksi halde (pasif/silinmiş cookie ya da yanlış host — ör. ims'e girişli
    # personel siparis'e gelince) login'i GÖSTER; yoksa "/" ↔ "/login" arası sonsuz
    # redirect döngüsü (ERR_TOO_MANY_REDIRECTS) oluşur.
    token = request.cookies.get("access_token")
    if token:
        payload = decode_token(token)
        if payload:
            user = _resolve_active_user(payload, db)
            if user:
                blocked = (
                    (_is_crm_host(request) and not _user_can(user, "crm", "view")) or
                    (_is_distributor_host(request) and not _user_can(user, "portal", "view"))
                )
                if not blocked:
                    return RedirectResponse(url="/", status_code=302)
    # Host'a göre marka: crm.* → CRM, siparis.* → Sipariş Portalı (aynı login sayfası)
    is_crm  = _is_crm_host(request)
    is_dist = _is_distributor_host(request)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "is_crm": is_crm,
        "is_distributor": is_dist,
        "brand_tagline": (
            "Sipariş Portalı" if is_dist else "CRM Sistemi" if is_crm else "ERP Sistemi"
        ),
    })


def _is_crm_host(request: Request) -> bool:
    """İstek crm.minerva108.com (veya crm.* herhangi bir host) için mi geldi?
    Aynı app/process iki subdomain'i sunar; kök (/) host'a göre yönlenir."""
    host = (request.headers.get("host", "") or "").split(":")[0].lower()
    return host.startswith("crm.")


def _is_distributor_host(request: Request) -> bool:
    """İstek siparis.minerva108.com (siparis.* herhangi bir host) için mi geldi?
    Distribütör sipariş portalı — kök (/) buraya gelirse /portal'a yönlenir."""
    host = (request.headers.get("host", "") or "").split(":")[0].lower()
    return host.startswith("siparis.")


@app.get("/", response_class=HTMLResponse)
def root(request: Request, db: Session = Depends(get_db)):
    """Dashboard — open to any authenticated user; the data fetched on this
    page is reports.view-gated at the API layer, so users without that perm
    will simply see empty cards rather than be bounced into a redirect loop.

    crm.minerva108.com'dan gelen istekler CRM ana sayfasına yönlenir — tek
    app, iki panel.  Auth cookie SSO ile paylaşıldığından oturum ortaktır."""
    if _is_crm_host(request):
        return RedirectResponse(url="/crm", status_code=302)
    if _is_distributor_host(request):
        return RedirectResponse(url="/portal", status_code=302)
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not user: return RedirectResponse(url="/login", status_code=302)
    # Distribütör (yalnız portal yetkisi, ERP erişimi yok) her host'ta portala yönlenir
    if _user_can(user, "portal", "view") and not _user_can(user, "reports", "view"):
        return RedirectResponse(url="/portal", status_code=302)
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


@app.get("/delivery", response_class=HTMLResponse)
def delivery_page(request: Request, db: Session = Depends(get_db)):
    """Hediye / numune teslimatı — barkod okutarak stok çıkışı + imzalı belge."""
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "inventory", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("delivery.html", _page_ctx(request, payload, user))


@app.get("/returns", response_class=HTMLResponse)
def returns_page(request: Request, db: Session = Depends(get_db)):
    """Ürün iadeleri — geri gelen ürünleri stoğa alma (sağlam/hasarlı ayrımlı)."""
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "inventory", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("returns.html", _page_ctx(request, payload, user))


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
    from core.qc_questions import QC_QUESTIONS
    ctx = _page_ctx(request, payload, user)
    ctx["qc_questions"] = QC_QUESTIONS   # tek kaynak — qc.html window.QC_QUESTIONS olarak kullanır
    return templates.TemplateResponse("qc.html", ctx)


@app.get("/numune-analiz", response_class=HTMLResponse)
def numune_analiz_page(request: Request, db: Session = Depends(get_db)):
    """Numune Analiz Formları (FR.KK.01) — AR-GE/KK belge sayfası (qc.view)."""
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "qc", "view"):
        return RedirectResponse(url="/", status_code=302)
    from core.sample_questions import FORM_CODE, SAMPLE_PROPERTIES
    ctx = _page_ctx(request, payload, user)
    ctx["sample_properties"] = SAMPLE_PROPERTIES   # tek kaynak — form satırları
    ctx["sample_form_code"] = FORM_CODE
    return templates.TemplateResponse("numune_analiz.html", ctx)


@app.get("/pdks", response_class=HTMLResponse)
def pdks_page(request: Request, db: Session = Depends(get_db)):
    """PDKS — personel devam takibi (giriş/çıkış, puantaj, izin, rapor)."""
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "pdks", "view_own"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("pdks.html", _page_ctx(request, payload, user))


@app.get("/pdks-qr", response_class=HTMLResponse)
def pdks_qr_page(request: Request, db: Session = Depends(get_db)):
    """PDKS kiosk — girişteki ekranda dönen imzalı QR (yalnız pdks.kiosk yetkili
    özel cihaz hesabı görebilir; sidebar/idle-watch YOK — kiosk boşta kalmaz)."""
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "pdks", "kiosk"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("pdks_qr.html", _page_ctx(request, payload, user))


@app.get("/shopify-sync", response_class=HTMLResponse)
def shopify_sync_page(request: Request, db: Session = Depends(get_db)):
    """Shopify stok senkron paneli (admin.view) — 3 mağaza durumu + elle tetik."""
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "admin", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("shopify_sync.html", _page_ctx(request, payload, user))


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


@app.get("/system", response_class=HTMLResponse)
def system_page(request: Request, db: Session = Depends(get_db)):
    """Sistem sağlık/canlılık durumu — yalnızca SuperAdmin."""
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.role != "SuperAdmin":
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("system.html", _page_ctx(request, payload, user))


@app.get("/drive", response_class=HTMLResponse)
def drive_page(request: Request, db: Session = Depends(get_db)):
    """Minerva Drive — dosya paylaşım yönetimi (oturum gerekir)."""
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("drive.html", _page_ctx(request, payload, user))


@app.get("/crm", response_class=HTMLResponse)
def crm_page(request: Request, db: Session = Depends(get_db)):
    """CRM — Müşteri İlişkileri Yönetimi (tek birleşik panel, oturum gerekir).
    crm.minerva108.com'un ana sayfası buraya yönlenir."""
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "crm", "view"):
        # Yetkisiz: CRM host'unda /login'e, IMS host'unda anasayfaya yolla
        return RedirectResponse(url="/login" if _is_crm_host(request) else "/", status_code=302)
    return templates.TemplateResponse("crm.html", _page_ctx(request, payload, user))


@app.get("/distributors", response_class=HTMLResponse)
def distributors_page(request: Request, db: Session = Depends(get_db)):
    """Distribütör hesap + fiyat listesi yönetimi (personel)."""
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "distributors", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("distributors.html", _page_ctx(request, payload, user))


@app.get("/portal", response_class=HTMLResponse)
def portal_page(request: Request, db: Session = Depends(get_db)):
    """Distribütör sipariş portalı — siparis.minerva108.com ana sayfası buraya yönlenir."""
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    user = _resolve_active_user(payload, db)
    if not _user_can(user, "portal", "view"):
        return RedirectResponse(url="/login" if _is_distributor_host(request) else "/", status_code=302)
    return templates.TemplateResponse("distributor_portal.html", _page_ctx(request, payload, user))


@app.get("/s/{token}", response_class=HTMLResponse)
def share_page(token: str, request: Request, db: Session = Depends(get_db)):
    """Public paylaşım sayfası — auth YOK.  Şifre/süre durumuna göre render."""
    from database import DriveCollection, DriveFile, DriveCollectionFile
    from core import drive as D
    c = db.query(DriveCollection).filter(DriveCollection.share_token == token).first()
    ctx = {"request": request, "token": token, "coll_name": "", "files": [],
           "error": request.query_params.get("e") == "1"}
    if not c:
        ctx["state"] = "notfound"
        return templates.TemplateResponse("share.html", ctx, status_code=404)
    ctx["coll_name"] = c.name
    if D.is_expired(c):
        ctx["state"] = "expired"
        return templates.TemplateResponse("share.html", ctx)
    if c.password_hash:
        sig = request.cookies.get(D.unlock_cookie_name(token), "")
        if not D.verify_unlock(token, sig):
            ctx["state"] = "locked"
            return templates.TemplateResponse("share.html", ctx)
    files = (db.query(DriveFile)
             .join(DriveCollectionFile, DriveCollectionFile.file_id == DriveFile.id)
             .filter(DriveCollectionFile.collection_id == c.id)
             .order_by(DriveCollectionFile.sort_order, DriveCollectionFile.id)
             .all())
    ctx["state"] = "open"
    ctx["files"] = [{"id": f.id, "name": f.original_name, "size": D.humanize(f.size_bytes)} for f in files]
    return templates.TemplateResponse("share.html", ctx)
