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
from typing import Optional

from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sqlalchemy.orm import Session
from slowapi.errors import RateLimitExceeded

from database import init_db, get_db, User
from core.auth import decode_token
from core.limiter import limiter
from core.permissions import _ROLE_LABELS, _has_permission

from routers import auth, users, inventory, recipes, production, b2b, reports


# ─── App init ───────────────────────────────────────────────────────────────

app = FastAPI(title="Minerva108 ERP", version="2.0.0", docs_url="/api/docs")


# ─── Rate limiter wiring (P0 / brute-force protection) ──────────────────────
# The Limiter object itself lives in core.limiter so any router can decorate
# endpoints with @limiter.limit(...) without circular-importing api_main.
app.state.limiter = limiter


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


@app.on_event("startup")
def startup_event():
    init_db()


# ─── Mount domain routers ───────────────────────────────────────────────────

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(inventory.router)
app.include_router(recipes.router)
app.include_router(production.router)
app.include_router(b2b.router)
app.include_router(reports.router)


# ─── Page-route helpers ─────────────────────────────────────────────────────

def _page_ctx(request: Request, payload: dict) -> dict:
    role = payload.get("role", "")
    return {
        "request":    request,
        "username":   payload.get("username"),
        "full_name":  payload.get("full_name"),
        "role":       role,
        "role_label": _ROLE_LABELS.get(role, role),
        "user_id":    int(payload.get("sub", 0)),   # current user ID — used by admin UI self-protection
    }


def _get_user_context(request: Request) -> Optional[dict]:
    token = request.cookies.get("access_token")
    if not token:
        return None
    return decode_token(token)


def _payload_can(payload: dict, db: Session, category: str, action: str) -> bool:
    """
    Page-route permission gate. Resolves the request user from the JWT payload,
    loads the live User row (so DB-stored custom permissions apply), and checks
    one category.action against the effective permission set.

    Mirrors the logic of `require_permission` used at the API layer, so the
    routing-layer perimeter and the page-rendering perimeter agree exactly.
    """
    user = db.query(User).filter(User.id == int(payload.get("sub", 0))).first()
    if not user or not user.is_active:
        return False
    return _has_permission(user, category, action)


# ─── Page Routes ─────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    token = request.cookies.get("access_token")
    if token and decode_token(token):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    """Dashboard — open to any authenticated user; the data fetched on this
    page is reports.view-gated at the API layer, so users without that perm
    will simply see empty cards rather than be bounced into a redirect loop."""
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("index.html", _page_ctx(request, payload))


@app.get("/items", response_class=HTMLResponse)
def items_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    if not _payload_can(payload, db, "items", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("items.html", _page_ctx(request, payload))


@app.get("/suppliers", response_class=HTMLResponse)
def suppliers_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    if not _payload_can(payload, db, "items", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("suppliers.html", _page_ctx(request, payload))


@app.get("/recipes", response_class=HTMLResponse)
def recipes_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    if not _payload_can(payload, db, "recipes", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("recipes.html", _page_ctx(request, payload))


@app.get("/production", response_class=HTMLResponse)
def production_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    if not _payload_can(payload, db, "production", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("production.html", _page_ctx(request, payload))


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    if not _payload_can(payload, db, "reports", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("reports.html", _page_ctx(request, payload))


@app.get("/ledger", response_class=HTMLResponse)
def ledger_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    if not _payload_can(payload, db, "reports", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("ledger.html", _page_ctx(request, payload))


@app.get("/qc", response_class=HTMLResponse)
def qc_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    if not _payload_can(payload, db, "qc", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("qc.html", _page_ctx(request, payload))


@app.get("/receiving", response_class=HTMLResponse)
def receiving_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    if not _payload_can(payload, db, "inventory", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("receiving.html", _page_ctx(request, payload))


@app.get("/traceability", response_class=HTMLResponse)
def traceability_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    if not _payload_can(payload, db, "inventory", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("traceability.html", _page_ctx(request, payload))


@app.get("/quotations", response_class=HTMLResponse)
def quotations_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload: return RedirectResponse(url="/login", status_code=302)
    if not _payload_can(payload, db, "b2b", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("quotations.html", _page_ctx(request, payload))


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_db)):
    payload = _get_user_context(request)
    if not payload:
        return RedirectResponse(url="/login", status_code=302)
    if not _payload_can(payload, db, "admin", "view"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("admin.html", _page_ctx(request, payload))
