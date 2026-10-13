# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Minerva Asistan router'ı — `/api/assistant/*` (çekirdek: `core/assistant/`).

Uçlar (hepsi `require_internal_user`; sohbet/bootstrap ayrıca `assistant.use`):
  POST   /chat                 NDJSON akışı (aşağıda) — kullanıcı bazlı 20/dk
  GET    /conversations        son 20 sohbetim
  GET    /conversations/{id}   sohbet + widget mesajları (yalnız sahibi; 404)
  DELETE /conversations/{id}   sert silme (yalnız sahibi; 204 / 404)
  GET    /bootstrap            widget açılış bilgisi (enabled/sebep, model, günlük kalan…)
  GET    /admin/usage          aylık kullanım raporu — rol ∈ _FINANCE_ROLES
  PUT    /admin/settings       ayar değişikliği — yalnız SuperAdmin, audit'li

Akış sözleşmesi (`POST /chat`, `application/x-ndjson`, satır başına bir JSON):
  {"t":"meta","conversation_id","rotated","rotation_reason","model"}   ← ilk satır
  {"t":"ping"}                                   ← ≤15 s'de bir (nginx 60 s okuma zaman aşımı)
  {"t":"tool","name","status":"start","args"} / {"t":"tool","name","status":"done|error",
   "summary","links","ms"} · {"t":"token","v"}
  {"t":"done","message_id","stop_reason","truncated","cost_usd","usage","sources","calls"}
  {"t":"error","code":"refusal|max_tokens|max_rounds|api_error|timeout","msg"}
Akıştan ÖNCE verilen JSON hatalar: 409 kapalı · 503 anahtar yok · 402 aylık /
günlük tavan · 429 `busy` (eşzamanlı tur sınırı, `retry_after_seconds`) · 404
sohbet (bkz. core.assistant.budget.check_budget) · 422 gövde.

Neden `Depends(get_db)` oturumu akışta KULLANILMAZ: FastAPI yield'li
bağımlılığı yanıt gövdesi akmadan kapatır.  Ön kontroller + sohbet kaydı o
oturumla commit edilir; üreteç (generator) kendi `asyncio.Queue`'sunu besleyen
`run_turn` görevini açar, olayları iletir ve `done`/`error` geldiğinde YENİ bir
`SessionLocal()` ile satırları, `assistant_usage` kayıtlarını ve rotasyon
alanlarını (`turns`, `last_prompt_tokens`) yazar — `done` satırına `message_id`
bunun ardından eklenir.  Bağlantı kopar da üreteç kapanırsa `finally` devreye
girer: görev bitmişse aynı yoldan, bitmemişse görevin bitiş geri çağrısıyla
kalıcılaştırılır (cevapsız `tool_use` taşıyan asistan satırını runner zaten
düşürmüştür).  Her iki yolda da `_persist` THREAD'de koşar (`anyio.to_thread`)
— DB yazımı ve `maybe_warn`'ın senkron webpush çağrıları tek süreçli
uvicorn'un event loop'unu bloklamasın.  Model çağrısı iptal EDİLMEZ — kesilen
istek de bütçeye yazılır; akış sırasında sohbet silinmişse satırlar yazılmaz
ama `assistant_usage` (conversation_id NULL) yine yazılır.  Sert sınırda
(`TURN_DEADLINE + HARD_DEADLINE_MARGIN`) görev iptal edilir; runner iptali
`timeout` sonucuna çevirir, o ana kadarki usage yine kalıcılaşır.

Eşzamanlı tur sınırı: kullanıcı başına en fazla `MAX_ACTIVE_TURNS` devam eden
soru (`_ACTIVE`, süreç belleği; aşımı 429 `busy`).  Tavan denetimi tur
başında yapılır ve devam eden turların maliyetini göremez — bu sınır olmadan
tavanın hemen altındaki kullanıcı dakikada 20 soruyu paralel başlatıp tavanı
aşabilirdi.  Girdiler zaman damgalıdır; iptal yolu kaçırılsa bile
`ACTIVE_TURN_TTL` sonra kendiliğinden düşer (sızıntı kilitlemez).

Rate limit kullanıcı bazlı (`_user_key`: cookie'deki JWT `sub`); ofis tek IP'den
çıktığı için IP anahtarı bütün ekibi birlikte kilitlerdi.

Claude SDK'sı (`core.assistant.runner` / `.client`) yalnız sohbet ucunda ve
TEMBEL import edilir — `api_main` import'u anahtarsız ortamda da ucuz kalır.
"""
import asyncio
import datetime as _dt
import json
import sys
import time
import traceback
from dataclasses import asdict
from typing import Any, Dict, List, Optional

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from core.assistant import budget
from core.assistant import history
from core.assistant import settings as S
from core.assistant.tools import PROMPT_HASH, ToolContext
from core.audit import log_admin_event
from core.auth import decode_token
from core.domain import active_domain
from core.limiter import limiter
from core.permissions import _FINANCE_ROLES, get_role_labels, require_internal_user
from database import AssistantConversation, AssistantUsage, SessionLocal, User, get_db, to_tr, tr_now

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

_USER = require_internal_user(("assistant", "use"))
_ANY = require_internal_user()

MESSAGE_MAX = 2000
PING_INTERVAL = 15.0            # saniye; nginx proxy_read_timeout 60 s'nin altında
HARD_DEADLINE_MARGIN = 15.0     # runner'ın 150 s sınırı + pay → görev iptal + timeout
STREAM_HEADERS = {"X-Accel-Buffering": "no", "Cache-Control": "no-cache, no-store"}
QUESTIONS_LIST_MAX = 50
TOP_TOOLS_MAX = 10
CONVERSATIONS_MAX = 20

_END = object()                 # kuyruk sonu nöbetçisi
_BACKGROUND = set()             # bitmemiş görevlere güçlü referans (GC önlemi)
MAX_ACTIVE_TURNS = 2            # kullanıcı başına eşzamanlı devam eden soru
ACTIVE_TURN_TTL = 240.0         # saniye; 150 + 15 s sınırından sonra kendiliğinden düşer
_ACTIVE: Dict[int, List[float]] = {}   # user_id → başlangıç zamanları (monotonic)


def _active_turns(user_id: int, now: Optional[float] = None) -> int:
    """Kullanıcının süresi dolmamış devam eden turları (bayatları temizler)."""
    now = time.monotonic() if now is None else now
    live = [t for t in _ACTIVE.get(user_id, ()) if now - t < ACTIVE_TURN_TTL]
    if live:
        _ACTIVE[user_id] = live
    else:
        _ACTIVE.pop(user_id, None)
    return len(live)


def _acquire_turn(user_id: int) -> float:
    stamp = time.monotonic()
    _ACTIVE.setdefault(user_id, []).append(stamp)
    return stamp


def _release_turn(user_id: int, stamp: float) -> None:
    lst = _ACTIVE.get(user_id)
    if not lst:
        return
    try:
        lst.remove(stamp)
    except ValueError:
        pass
    if not lst:
        _ACTIVE.pop(user_id, None)


# ─── Gövdeler ────────────────────────────────────────────────────────────────

class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=MESSAGE_MAX)
    conversation_id: Optional[int] = Field(None, ge=1)

    @field_validator("message")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("Mesaj boş olamaz.")
        return v


class SettingsIn(BaseModel):
    """Tüm alanlar isteğe bağlı; yalnız gönderilenler değişir
    (doğrulama `core.assistant.settings.validate`)."""
    enabled: Optional[bool] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    monthly_cap_usd: Optional[float] = None
    daily_user_cap_usd: Optional[float] = None
    max_tool_rounds: Optional[int] = None
    max_output_tokens: Optional[int] = None
    max_turns: Optional[int] = None
    max_history_tokens: Optional[int] = None
    cache_ttl: Optional[str] = None


# ─── Yardımcılar ─────────────────────────────────────────────────────────────

def _user_key(request: Request) -> str:
    """Rate limit anahtarı: oturum cookie'sindeki JWT `sub` → "user:<id>";
    çözülemezse IP (oturumsuz istek zaten 401 alır)."""
    token = request.cookies.get("access_token")
    payload = decode_token(token) if token else None
    sub = (payload or {}).get("sub")
    if sub:
        return f"user:{sub}"
    return get_remote_address(request)


def _err(status: int, detail: str, code: Optional[str] = None, **extra) -> JSONResponse:
    body: Dict[str, Any] = {"detail": detail}
    if code:
        body["code"] = code
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def _line(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False, default=str) + "\n"


def _load_user(db: Session, current_user: dict) -> User:
    user = db.get(User, int(current_user.get("sub", 0)))
    if user is None:
        raise HTTPException(status_code=401, detail="Yetkisiz.")
    return user


def _month_arg(month: Optional[str]):
    """'YYYY-MM' → (yıl, ay); boşsa TR takviminde bu ay; bozuksa 400."""
    if not month:
        now = tr_now()
        return now.year, now.month
    try:
        y, m = month.split("-")
        y, m = int(y), int(m)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Ay biçimi YYYY-AA olmalı.")
    if not (2020 <= y <= 2100 and 1 <= m <= 12):
        raise HTTPException(status_code=400, detail="Ay biçimi YYYY-AA olmalı.")
    return y, m


# ─── Kalıcılık (akış sonrası, YENİ oturum) ───────────────────────────────────

def _persist(res, *, conv_id: int, user_id: int, domain: str, settings, question_preview: str,
             fresh: bool) -> Optional[int]:
    """Bir turun satırlarını + çağrı başına `assistant_usage`'ı + rotasyon
    alanlarını yazar; cevabı taşıyan asistan satırının id'sini döner.
    Kendi `SessionLocal()`'ını açar (bağımlılık oturumu çoktan kapalı).
    Akış sırasında sohbet silinmişse satır/rotasyon atlanır ama usage
    (conversation_id/message_id NULL) ve %80 uyarısı YİNE yazılır — maliyet
    oluştu, tavan görmeli.  Hata akışı bozmaz: iz stderr'e, None döner."""
    db = SessionLocal()
    try:
        conv = db.get(AssistantConversation, conv_id)      # None = akış sırasında silinmiş
        message_id = None
        rows = list(getattr(res, "rows", None) or [])
        if conv is not None and rows:
            added = history.append_rows(db, conv, rows)
            for m in reversed(added):
                if m.kind == "assistant":
                    message_id = m.id
                    break
            conv.turns = int(conv.turns or 0) + 1
            if getattr(res, "last_prompt_tokens", 0):
                conv.last_prompt_tokens = int(res.last_prompt_tokens)
            conv.updated_at = _dt.datetime.utcnow()
        usages = list(getattr(res, "usages", None) or [])
        for u in usages:
            budget.record_usage(
                db, user_id=user_id, conversation_id=conv.id if conv is not None else None,
                message_id=message_id,
                model=u.get("model") or settings.model, usage=u, cost_usd=u.get("cost_usd") or 0.0,
                tool_calls=u.get("tool_calls"), latency_ms=u.get("latency_ms"),
                stop_reason=u.get("stop_reason"), request_id=u.get("request_id"),
                domain=domain, round_no=u.get("round_no") or 1, question_preview=question_preview)
        if fresh and usages:
            # Taze sohbetin ilk çağrısı: cache okuma = statik önek boyutu (rotasyon eşiği için)
            history.observe_prefix_tokens(usages[0].get("cache_read_input_tokens"))
        now = tr_now()
        budget.maybe_warn(db, settings, budget.month_spend(db, now.year, now.month), now)
        db.commit()
        return message_id
    except Exception:
        db.rollback()
        traceback.print_exc(file=sys.stderr)
        return None
    finally:
        db.close()


def _persist_from_task(task: "asyncio.Task", persist_kwargs: dict) -> None:
    """Bitmiş görevin sonucunu kalıcılaştır — bağlantı kopma yolu.  Event loop
    iş parçacığında (done-callback) çağrılır; asıl yazım THREAD'e devredilir
    (`_persist` → DB + `maybe_warn` webpush; loop bloklanmasın).  Thread
    görevi `_BACKGROUND`'da tutulur (GC önlemi)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
        return
    res = task.result()
    try:
        bg = asyncio.ensure_future(anyio.to_thread.run_sync(lambda: _persist(res, **persist_kwargs)))
    except RuntimeError:            # loop yok (kapanış): son çare senkron
        _persist(res, **persist_kwargs)
        return
    _BACKGROUND.add(bg)
    bg.add_done_callback(_BACKGROUND.discard)


async def _stream(meta: dict, run_kwargs: dict, persist_kwargs: dict, deadline: float,
                  release: Optional[Any] = None):
    """NDJSON üreteci.  Kuyruğu besleyen `run_turn` görevi ayrı; burada yalnız
    olaylar iletilir, ping atılır ve `done`/`error` sonrası kalıcılaştırılır.
    `release` (eşzamanlı tur sayacı) görev bitince tam bir kez çağrılır."""
    from core.assistant import runner

    queue: "asyncio.Queue" = asyncio.Queue()

    async def emit(event: dict) -> None:
        queue.put_nowait(event)

    async def work():
        try:
            return await runner.run_turn(emit=emit, **run_kwargs)
        finally:
            queue.put_nowait(_END)

    task = asyncio.create_task(work())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    if release is not None:
        task.add_done_callback(lambda _t: release())
    persisted = False
    started = time.monotonic()
    yield _line(meta)
    try:
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), PING_INTERVAL)
            except asyncio.TimeoutError:
                if time.monotonic() - started > deadline + HARD_DEADLINE_MARGIN:
                    task.cancel()
                    yield _line({"t": "error", "code": "timeout", "msg": runner.ERROR_MESSAGES["timeout"]})
                    break
                yield _line({"t": "ping"})
                continue
            if ev is _END:
                break
            if ev.get("t") in ("done", "error"):
                # Dış iptale (bağlantı kopması) karşı korumalı: görev bitsin, satırlar yazılsın.
                with anyio.CancelScope(shield=True):
                    res = None
                    try:
                        res = await task
                    except asyncio.CancelledError:
                        res = None
                    except Exception:
                        traceback.print_exc(file=sys.stderr)
                    message_id = None
                    if res is not None:
                        message_id = await anyio.to_thread.run_sync(
                            lambda: _persist(res, **persist_kwargs))
                    persisted = True
                if ev.get("t") == "done":
                    ev = dict(ev)
                    ev["message_id"] = message_id
            yield _line(ev)
    finally:
        if not persisted:
            # Bağlantı koptu: görev bitmişse hemen, bitmemişse bitince — ikisi de thread'e
            if task.done():
                _persist_from_task(task, persist_kwargs)
            else:
                task.add_done_callback(lambda t: _persist_from_task(t, persist_kwargs))


# ─── Sohbet ──────────────────────────────────────────────────────────────────

@router.post("/chat")
@limiter.limit("20/minute", key_func=_user_key)
async def chat(
    request: Request,
    body: ChatIn,
    db: Session = Depends(get_db),
    current_user: dict = Depends(_USER),
    domain: str = Depends(active_domain),
):
    """Bir soru → NDJSON olay akışı.  Ön kontroller 409/503/402/404 JSON; sohbet
    (yeni / devam / rotasyon) akıştan önce commit edilir ki `meta` gerçek id taşısın."""
    from core.assistant import client as client_mod
    from core.assistant import runner

    user = _load_user(db, current_user)
    settings = S.load_settings(db)
    denied = budget.check_budget(db, settings, user.id)
    if denied is not None:
        return _err(denied.status, denied.detail, denied.code)
    if _active_turns(user.id) >= MAX_ACTIVE_TURNS:
        return _err(429, "Devam eden sorunuz bitmeden yeni soru gönderilemez.", "busy",
                    retry_after_seconds=10)
    ctx = ToolContext.from_user(user, domain, db)

    rotated, reason, seed = False, None, ""
    if body.conversation_id:
        conv = history.get_conversation(db, user.id, body.conversation_id)
        if conv is None:
            return _err(404, "Sohbet bulunamadı.", "conversation_not_found")
        reason = history.needs_rotation(conv, settings, settings.model, PROMPT_HASH, domain)
        if reason:
            seed = history.rotation_seed(db, conv)
            conv = history.start_conversation(db, user.id, domain, settings.model, PROMPT_HASH,
                                              parent_id=conv.id)
            rotated = True
    else:
        conv = history.start_conversation(db, user.id, domain, settings.model, PROMPT_HASH)
    history.set_title(conv, body.message)
    db.commit()
    conv_id = conv.id
    hist = history.api_messages(db, conv)

    try:
        client = client_mod.get_client()
    except client_mod.NotConfigured:
        return _err(503, "Asistan yapılandırılmamış (API anahtarı yok).", "not_configured")

    meta = {"t": "meta", "conversation_id": conv_id, "rotated": rotated, "rotation_reason": reason,
            "model": settings.model}
    run_kwargs = dict(client=client, settings=settings, ctx=ctx, history=hist,
                      question=history.seed_question(seed, body.message),
                      display_question=body.message)
    persist_kwargs = dict(conv_id=conv_id, user_id=user.id, domain=domain, settings=settings,
                          question_preview=body.message[:160], fresh=not hist)
    # Sayaç burada artar (denetimle aynı loop adımında — araya başka istek giremez);
    # görev bitince `release` düşürür, üreteç hiç başlamazsa TTL temizler.
    stamp = _acquire_turn(user.id)
    uid = user.id

    def release() -> None:
        _release_turn(uid, stamp)

    return StreamingResponse(_stream(meta, run_kwargs, persist_kwargs, runner.TURN_DEADLINE, release),
                             media_type="application/x-ndjson", headers=dict(STREAM_HEADERS))


# ─── Sohbet listesi / detay / silme (yalnız sahibi) ──────────────────────────

@router.get("/conversations")
def list_conversations(db: Session = Depends(get_db), current_user: dict = Depends(_USER)):
    return history.list_conversations(db, int(current_user["sub"]), CONVERSATIONS_MAX)


@router.get("/conversations/{conv_id}")
def conversation_detail(conv_id: int, db: Session = Depends(get_db),
                        current_user: dict = Depends(_USER)):
    conv = history.get_conversation(db, int(current_user["sub"]), conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Sohbet bulunamadı.")
    return {
        "id": conv.id, "title": conv.title or "—", "model": conv.model, "domain": conv.domain,
        "turns": conv.turns, "parent_id": conv.parent_id,
        "updated_at": to_tr(conv.updated_at).strftime("%d.%m.%Y %H:%M") if conv.updated_at else "",
        "messages": history.display_messages(db, conv),
    }


@router.delete("/conversations/{conv_id}", status_code=204)
def delete_conversation(conv_id: int, db: Session = Depends(get_db),
                        current_user: dict = Depends(_USER)):
    if not history.delete_conversation(db, int(current_user["sub"]), conv_id):
        raise HTTPException(status_code=404, detail="Sohbet bulunamadı.")
    db.commit()
    return Response(status_code=204)


# ─── Bootstrap ───────────────────────────────────────────────────────────────

@router.get("/bootstrap")
def bootstrap(db: Session = Depends(get_db), current_user: dict = Depends(_USER)):
    """Widget açılışı: açık mı (değilse Türkçe sebep + kod), model, günlük
    tavan/harcama, kullanıcı; finans rolüne aylık durum + %80 uyarısı."""
    user = _load_user(db, current_user)
    settings = S.load_settings(db)
    now = tr_now()
    denied = budget.check_budget(db, settings, user.id, now)
    role = current_user.get("role") or user.role or ""
    daily_spent = budget.user_day_spend(db, user.id, now.date())
    cap_day = float(settings.daily_user_cap_usd or 0.0)
    out: Dict[str, Any] = {
        "enabled": denied is None,
        "reason": denied.detail if denied else None,
        "code": denied.code if denied else None,
        "model": settings.model,
        "model_label": S.model_label(settings.model),
        "daily_cap_usd": cap_day,
        "daily_spent_usd": round(daily_spent, 4),
        "daily_remaining_usd": round(max(cap_day - daily_spent, 0.0), 4) if cap_day > 0 else None,
        "max_message_chars": MESSAGE_MAX,
        "user": {"id": user.id, "name": user.full_name or user.username, "role": role,
                 "role_label": get_role_labels(db).get(role, role)},
        "can_see_usage": role in _FINANCE_ROLES,
        "can_edit_settings": role == "SuperAdmin",
        "warning": None,
    }
    if out["can_see_usage"]:
        spent = budget.month_spend(db, now.year, now.month)
        cap = float(settings.monthly_cap_usd or 0.0)
        pct = int(round(100.0 * spent / cap)) if cap > 0 else 0
        out["month"] = {"spent_usd": round(spent, 4), "cap_usd": cap, "pct": pct}
        if cap > 0 and spent >= budget.WARN_PCT * cap:
            out["warning"] = f"Aylık asistan bütçesinin %{pct}'i kullanıldı (${spent:.2f} / ${cap:.0f})."
    return out


# ─── Yönetim ─────────────────────────────────────────────────────────────────

def _tool_names(row: AssistantUsage) -> List[dict]:
    if not row.tool_calls_json:
        return []
    try:
        calls = json.loads(row.tool_calls_json)
    except (TypeError, ValueError):
        return []
    return [c for c in calls if isinstance(c, dict) and c.get("name")]


@router.get("/admin/usage")
def admin_usage(month: Optional[str] = Query(None, max_length=7),
                db: Session = Depends(get_db), current_user: dict = Depends(_ANY)):
    """Aylık kullanım raporu (`assistant_usage`): kullanıcı / gün / araç
    kırılımı, önbellek oranı, stop_reason dağılımı, son 50 soru.  Rol DB'den
    canlı; yalnız finans rolleri (SuperAdmin, Manager)."""
    if (current_user.get("role") or "") not in _FINANCE_ROLES:
        raise HTTPException(status_code=403, detail="Bu rapor yalnız finans rollerine açık.")
    year, mon = _month_arg(month)
    settings = S.load_settings(db)
    lo, hi = budget.month_bounds_utc(year, mon)
    rows = (db.query(AssistantUsage)
            .filter(AssistantUsage.created_at >= lo, AssistantUsage.created_at < hi)
            .order_by(AssistantUsage.id.asc()).all())
    names = {u.id: (u.full_name or u.username) for u in db.query(User.id, User.full_name, User.username).all()}

    by_user: Dict[int, dict] = {}
    by_day: Dict[str, dict] = {}
    tools: Dict[str, dict] = {}
    stop_reasons: Dict[str, int] = {}
    cache = {"read_tokens": 0, "creation_tokens": 0, "creation_1h_tokens": 0,
             "uncached_input_tokens": 0, "output_tokens": 0}
    groups: Dict[Any, dict] = {}        # soru (tur) bazında: message_id ya da satır
    spent = 0.0
    for r in rows:
        cost = float(r.cost_usd or 0.0)
        spent += cost
        first = int(r.round_no or 1) == 1
        bu = by_user.setdefault(r.user_id, {"user_id": r.user_id, "name": names.get(r.user_id, f"#{r.user_id}"),
                                            "questions": 0, "calls": 0, "cost_usd": 0.0})
        bu["calls"] += 1
        bu["cost_usd"] += cost
        if first:
            bu["questions"] += 1
        day = to_tr(r.created_at).strftime("%Y-%m-%d") if r.created_at else "—"
        bd = by_day.setdefault(day, {"day": day, "questions": 0, "calls": 0, "cost_usd": 0.0})
        bd["calls"] += 1
        bd["cost_usd"] += cost
        if first:
            bd["questions"] += 1
        for c in _tool_names(r):
            t = tools.setdefault(c["name"], {"name": c["name"], "calls": 0, "errors": 0})
            t["calls"] += 1
            if c.get("is_error"):
                t["errors"] += 1
        sr = r.stop_reason or "—"
        stop_reasons[sr] = stop_reasons.get(sr, 0) + 1
        cache["read_tokens"] += int(r.cache_read_input_tokens or 0)
        cache["creation_tokens"] += int(r.cache_creation_input_tokens or 0)
        cache["creation_1h_tokens"] += int(r.cache_creation_1h_tokens or 0)
        cache["uncached_input_tokens"] += int(r.input_tokens or 0)
        cache["output_tokens"] += int(r.output_tokens or 0)
        key = ("m", r.message_id) if r.message_id else ("r", r.id)
        g = groups.setdefault(key, {
            "at": to_tr(r.created_at).strftime("%d.%m.%Y %H:%M") if r.created_at else "",
            "user_id": r.user_id, "user": names.get(r.user_id, f"#{r.user_id}"),
            "conversation_id": r.conversation_id, "preview": r.question_preview or "",
            "cost_usd": 0.0, "calls": 0, "tools": [], "stop_reason": None, "_id": r.id})
        g["cost_usd"] += cost
        g["calls"] += 1
        g["tools"].extend(c["name"] for c in _tool_names(r))
        g["stop_reason"] = r.stop_reason
        g["_id"] = r.id

    denom = cache["read_tokens"] + cache["creation_tokens"] + cache["uncached_input_tokens"]
    cache["hit_ratio"] = round(cache["read_tokens"] / denom, 3) if denom else 0.0
    cap = float(settings.monthly_cap_usd or 0.0)
    questions = sorted(groups.values(), key=lambda g: g["_id"], reverse=True)[:QUESTIONS_LIST_MAX]
    for g in questions:
        g.pop("_id", None)
        g["cost_usd"] = round(g["cost_usd"], 4)
    for d in by_user.values():
        d["cost_usd"] = round(d["cost_usd"], 4)
    for d in by_day.values():
        d["cost_usd"] = round(d["cost_usd"], 4)
    return {
        "month": f"{year:04d}-{mon:02d}",
        "cap_usd": cap,
        "spent_usd": round(spent, 4),
        "pct": int(round(100.0 * spent / cap)) if cap > 0 else 0,
        "questions_count": sum(1 for r in rows if int(r.round_no or 1) == 1),
        "calls_count": len(rows),
        "by_user": sorted(by_user.values(), key=lambda d: d["cost_usd"], reverse=True),
        "by_day": sorted(by_day.values(), key=lambda d: d["day"]),
        "top_tools": sorted(tools.values(), key=lambda d: (-d["calls"], d["name"]))[:TOP_TOOLS_MAX],
        "cache": cache,
        "stop_reasons": stop_reasons,
        "questions": questions,
        "settings": asdict(settings),
        "models": [{"id": m, "label": S.model_label(m)} for m in budget.PRICES],
    }


@router.put("/admin/settings")
def admin_settings(request: Request, body: SettingsIn, db: Session = Depends(get_db),
                   current_user: dict = Depends(_ANY)):
    """Yalnız SuperAdmin (rol DB'den canlı).  Doğrulama Türkçe 400 (model fiyat
    tablosunda olmalı); değişiklik `assistant.settings.update` audit satırı."""
    if (current_user.get("role") or "") != "SuperAdmin":
        raise HTTPException(status_code=403, detail="Asistan ayarlarını yalnız Süper Yönetici değiştirebilir.")
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=400, detail="Değiştirilecek ayar yok.")
    old = S.load_settings(db)
    try:
        new = S.save_settings(db, changes)
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    old_d, new_d = asdict(old), asdict(new)
    diff_old = {k: old_d[k] for k in changes if old_d.get(k) != new_d.get(k)}
    diff_new = {k: new_d[k] for k in changes if old_d.get(k) != new_d.get(k)}
    log_admin_event(db, request, current_user, action="assistant.settings.update",
                    target_type="assistant", target_name="settings",
                    details={"old": diff_old, "new": diff_new})
    return {"settings": new_d, "changed": sorted(diff_new)}
