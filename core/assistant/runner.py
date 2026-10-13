# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Asistan döngüsü — bir soru için Claude çağrıları, araç turları ve olay akışı.

İstek yapısı (`build_params`, bkz. claude-api/prompt-caching):
  tools (ada göre sıralı, dondurulmuş) → system (dondurulmuş kurallar + SSS,
  son blokta `cache_control` ephemeral TTL=ayar) → messages (geçmiş + soru +
  oturum bağlamı `role:"system"`).  Üst düzey `cache_control` 5 dk kuyruk —
  1h önce / 5m sonra sırası izinli.  `tool_choice` DAİMA auto (değişirse
  tools+system önbelleği düşer).  Opus 5.5: thinking adaptive (varsayılan),
  efor `output_config.effort`, `fallbacks:"default"` + beta
  `server-side-fallback-2026-07-01`, `thinking.block_binding.
  prefix_mismatch_behavior:"drop_block"` + beta `thinking-binding-controls-
  2026-08-01`.  SDK 0.125 `block_binding`'i typed kabul etmez → `thinking`
  tümüyle `extra_body` ile gider (betas/fallbacks/cache_control typed).

Tek seferlik SAFE MODE: ilk 400 `fallbacks` / `betas` / `block_binding` /
`role 'system'` adlarından birini anarsa istek beta'sız (ve gerekiyorsa
bağlam user mesajına katlanmış) biçimde BİR kez yinelenir; süreç bayrağı
sonraki isteklerde yinelemeyi atlar (`reset_cache()` testler için).  Başka
4xx'te yineleme YOK (SDK max_retries yalnız 429/5xx/bağlantı).

Olay protokolü (`emit`, NDJSON satırı olarak router iletir; `meta` ve `ping`
router'ındır):
  {"t":"tool","name","status":"start","args"} / {"t":"tool","name","status":
  "done|error","summary","links","ms"} · {"t":"token","v"} ·
  {"t":"done","stop_reason","truncated","cost_usd","usage","sources"} ·
  {"t":"error","code":"refusal|max_tokens|max_rounds|api_error|timeout","msg"}

Araç turu: paralel `tool_use` blokları → TEK user mesajı, aynı sırada
`tool_result` (hatalı olan `is_error`).  `max_tokens`'ta araç ÇALIŞMAZ.
Araç turu sınırında sonuçlar hata olarak döner + `role:"system"` notu
(tool_choice DEĞİŞMEZ); model yine araç isterse `max_rounds`.  `tool_use`'u
cevapsız kalan son asistan satırı kalıcılaştırılmaz (rows'tan düşer).

Hatayla biten tur (timeout, 4xx/5xx, refusal, max_rounds…): asistan cevabı
yoksa sondaki `role:"system"` satır(lar)ı da kalıcılaştırılmaz (`_finalize`).
Sebep: mid-conversation system mesajı ya messages'ın SON girdisi olmalı ya da
bir assistant turu izlemeli (prompt-caching.md); `[…, user(q1), system(ctx1),
user(q2), system(ctx2)]` dizisi 400 döner ve sohbet kilitlenirdi.  Soru satırı
kalır (ardışık user mesajlarını API tek tura birleştirir).  Aynı kural
`history.api_messages` replay'inde de uygulanır (savunma derinliği).

Zaman aşımı / dış iptal: akan çağrı kesilirse `stream.current_message_snapshot`
(message_start usage'ı: girdi + önbellek sayaçları) `stop_reason="timeout"`
ile usages'a yazılır — API bu girdiyi faturalar, bütçe de görmeli.  Router'ın
sert sınırındaki `task.cancel()` da `timeout` sonucuna çevrilir (görev normal
biter, kısmi usage kalıcılaşır; CancelledError yutulur — bilinçli).
`request_id` mesajdan DEĞİL `stream.request_id`'den okunur: SDK 0.125'te
`get_final_message()` akümüle edilmiş mesajı döner, `_request_id` taşımaz.
"""
import asyncio
import datetime as _dt
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import anthropic
import anyio
import httpx

from database import tr_now
from core.domain import domain_label
from core.assistant import prompt
from core.assistant import tools as T
from core.assistant.budget import PRICES, compute_cost, usage_fields
from core.assistant.tools import ToolContext, ToolResult, run_tool

TURN_DEADLINE = 150.0
BETAS = ["server-side-fallback-2026-07-01", "thinking-binding-controls-2026-08-01"]
BLOCK_BINDING = {"prefix_mismatch_behavior": "drop_block"}
TOOL_CHOICE = {"type": "auto"}
LIMIT_NOTICE = "Araç çağrısı sınırına ulaşıldı; elindeki verilerle kısa cevap ver, araç çağırma."
LIMIT_RESULT = '{"error":"araç çağrısı sınırına ulaşıldı"}'
CONTEXT_TAG = "oturum_baglami"

ERROR_MESSAGES = {
    "refusal":    "Bu soruya güvenlik nedeniyle yanıt verilemedi.",
    "max_tokens": "Cevap uzunluk sınırına takıldı; soruyu daraltıp tekrar deneyin.",
    "max_rounds": "Araç çağrısı sınırına ulaşıldı; soruyu daraltıp tekrar deneyin.",
    "api_error":  "Asistan servisine ulaşılamadı; biraz sonra tekrar deneyin.",
    "timeout":    "Cevap zaman aşımına uğradı; soruyu daraltıp tekrar deneyin.",
}
_SAFE_MODE_MARKERS = ("fallbacks", "betas", "block_binding", "anthropic-beta")
# Katlama YALNIZ belgelenmiş "role 'system' is not supported on this model" 400'ünde;
# "not supported" şartı, başka bir system-yerleşim hatasının süreç genelinde
# katlamayı açmasını önler (aksi hâlde tüm sohbetlerin öneki değişirdi).
_FOLD_MARKERS = ("role 'system'", 'role "system"', "system role", "role: system", "'system' is not")
_FOLD_REQUIRED = "not supported"
_WEEKDAYS_TR = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")

_SAFE_MODE = False
_FOLD_SYSTEM = False

Emit = Callable[[dict], Awaitable[None]]


def reset_cache() -> None:
    """Süreç bayraklarını sıfırla (testler)."""
    global _SAFE_MODE, _FOLD_SYSTEM
    _SAFE_MODE = False
    _FOLD_SYSTEM = False


def safe_mode_active() -> bool:
    return _SAFE_MODE


# ─── İstek kurulumu ──────────────────────────────────────────────────────────

def _escape_context_tag(text: str) -> str:
    """Kullanıcı/araç metnindeki `<oturum_baglami` / `</oturum_baglami`
    dizilerini etkisizleştir — katlanmış bağlam bloğu user metninden
    sahtelenemesin.  Deterministik (replay byte-stabil kalır)."""
    return text.replace(f"<{CONTEXT_TAG}", f"&lt;{CONTEXT_TAG}").replace(f"</{CONTEXT_TAG}", f"&lt;/{CONTEXT_TAG}")


def _escape_user_blocks(content):
    if isinstance(content, str):
        return _escape_context_tag(content)
    out = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
            b = dict(b)
            b["text"] = _escape_context_tag(b["text"])
        out.append(b)
    return out


def _fold_system(messages: List[dict]) -> List[dict]:
    """`role:"system"` mesajlarını önceki user mesajına <oturum_baglami> text
    bloğu olarak katla (desteklemeyen model için yedek yol).  User metnindeki
    aynı etiket kaçırılır (`_escape_context_tag`) — yalnız katlama yolunda;
    normal yolda `role:"system"` zaten sahtelenemez."""
    out: List[dict] = []
    for m in messages:
        if m.get("role") != "system":
            content = _escape_user_blocks(m["content"]) if m.get("role") == "user" else m["content"]
            out.append({"role": m["role"], "content": content})
            continue
        text = m["content"] if isinstance(m["content"], str) else "\n".join(
            b.get("text", "") for b in m["content"] if isinstance(b, dict))
        block = {"type": "text", "text": f"<{CONTEXT_TAG}>\n{text}\n</{CONTEXT_TAG}>"}
        if out and out[-1]["role"] == "user":
            prev = out[-1]["content"]
            prev = [{"type": "text", "text": prev}] if isinstance(prev, str) else list(prev)
            out[-1] = {"role": "user", "content": prev + [block]}
        else:
            out.append({"role": "user", "content": [block]})
    return out


def build_params(settings, messages: List[dict], *, safe_mode: bool = False,
                 fold_system: bool = False) -> dict:
    """`client.beta.messages.stream(**params)` sözlüğü.  Önek (tools, system)
    ayarlardan/geçmişten bağımsız, her çağrıda byte-aynı.  `safe_mode` beta
    alanlarını düşürür; `fold_system` sistem mesajlarını user'a katlar."""
    params: Dict[str, Any] = {
        "model": settings.model,
        "max_tokens": int(settings.max_output_tokens),
        "system": prompt.system_blocks(settings.cache_ttl),
        "tools": T.tool_specs(),
        "tool_choice": dict(TOOL_CHOICE),
        "output_config": {"effort": settings.effort},
        "messages": _fold_system(messages) if fold_system else list(messages),
        "cache_control": {"type": "ephemeral"},
    }
    if safe_mode:
        params["thinking"] = {"type": "adaptive"}
    else:
        params["betas"] = list(BETAS)
        params["fallbacks"] = "default"
        params["extra_body"] = {"thinking": {"type": "adaptive", "block_binding": dict(BLOCK_BINDING)}}
    return params


def context_message(ctx: ToolContext, settings=None, today: Optional[_dt.datetime] = None) -> dict:
    """Soru mesajından SONRA gelen `role:"system"` oturum bağlamı — önbelleğe
    dokunmaz, user metninden sahtelenemez."""
    now = today or tr_now()
    allowed = T.allowed_tools(ctx)
    denied = T.denied_tools(ctx)
    parts = [
        f"Kullanıcı: {ctx.full_name or ctx.username} ({ctx.role_label or ctx.role})",
        f"Panel: {domain_label(ctx.domain)}",
        f"Tarih: {now.strftime('%d.%m.%Y')} {_WEEKDAYS_TR[now.weekday()]} (TR)",
        "Kullanabildiği araçlar: " + (", ".join(allowed) or "—"),
        "Kullanamadığı araçlar (sebep): " + (", ".join(f"{n} ({p} yok)" for n, p in denied) or "—"),
        "Finans verisi: " + ("evet" if ctx.can_finance else "hayır"),
    ]
    return {"role": "system", "content": "Oturum bağlamı — " + " · ".join(parts)}


# ─── Sonuç ───────────────────────────────────────────────────────────────────

@dataclass
class TurnResult:
    rows: List[dict] = field(default_factory=list)      # history.append_rows girdisi
    usages: List[dict] = field(default_factory=list)    # çağrı başına: budget.record_usage alanları
    final_text: str = ""
    stop_reason: str = ""
    tool_calls: List[dict] = field(default_factory=list)   # [{"name","ms","is_error"}]
    sources: List[dict] = field(default_factory=list)      # dedup linkler
    cost_usd: float = 0.0
    truncated: bool = False
    error: Optional[dict] = None                           # {"code","msg"} ya da None
    last_prompt_tokens: int = 0                            # son isteğin toplam girdi boyutu
    calls: int = 0


class _Failed(Exception):
    def __init__(self, code: str, msg: Optional[str] = None):
        super().__init__(code)
        self.code = code
        self.msg = msg or ERROR_MESSAGES.get(code, ERROR_MESSAGES["api_error"])


def _block_dict(b) -> dict:
    if isinstance(b, dict):
        return dict(b)
    if hasattr(b, "to_dict"):
        return b.to_dict()
    return dict(getattr(b, "__dict__", {}))


def _text_of(blocks) -> str:
    return "".join(getattr(b, "text", "") or "" for b in blocks
                   if getattr(b, "type", None) == "text")


def _usage_dict(msg, settings, latency_ms: int, round_no: int, request_id: Optional[str] = None) -> dict:
    """Bir çağrının `budget.record_usage` alanları.  `request_id` akıştan
    (`stream.request_id`) gelir — akümüle mesajda yoktur."""
    model = getattr(msg, "model", None) or settings.model
    price_model = model if model in PRICES else settings.model
    u = usage_fields(getattr(msg, "usage", None))
    u.update({
        "model": model, "round_no": round_no, "latency_ms": latency_ms,
        "stop_reason": getattr(msg, "stop_reason", None),
        "request_id": request_id,
        "cost_usd": compute_cost(price_model, getattr(msg, "usage", None)),
        "tool_calls": [],
    })
    return u


def _stream_request_id(stream) -> Optional[str]:
    try:
        rid = getattr(stream, "request_id", None)
    except Exception:          # response henüz yoksa SDK özelliği patlayabilir
        return None
    return str(rid) if rid else None


def _partial_snapshot(stream):
    """Kesilen akışın o ana kadarki mesajı (SDK: `current_message_snapshot`,
    hiç olay gelmediyse AssertionError → None)."""
    try:
        return stream.current_message_snapshot
    except Exception:
        return None


# ─── Çalışma durumu ──────────────────────────────────────────────────────────

class _State:
    def __init__(self, messages: List[dict], rows: List[dict]):
        self.messages = messages
        self.rows = rows
        self.res = TurnResult(rows=rows)
        self.pending_tool_use = False      # son asistan satırı cevapsız tool_use taşıyor


def _record_partial(state: _State, stream, settings, t0: float) -> None:
    """Kesilen çağrının o ana kadarki usage'ını `stop_reason="timeout"` ile
    kaydet (girdi + önbellek sayaçları faturalanır; çıktı yalnız message_start
    kadar).  Hiç olay gelmediyse (snapshot yok) kayıt yok."""
    snap = _partial_snapshot(stream)
    if snap is None or getattr(snap, "usage", None) is None:
        return
    u = _usage_dict(snap, settings, int((time.perf_counter() - t0) * 1000), state.res.calls,
                    _stream_request_id(stream))
    u["stop_reason"] = "timeout"
    state.res.usages.append(u)
    state.res.cost_usd = round(state.res.cost_usd + u["cost_usd"], 6)


async def _stream_once(state: _State, client, params: dict, emit: Emit, settings):
    """Tek akış: token olaylarını iletir; (mesaj, gecikme ms, request-id) döner.
    İptal/zaman aşımında kısmi usage kaydedilip istisna yeniden fırlatılır."""
    t0 = time.perf_counter()
    async with client.beta.messages.stream(**params) as stream:
        try:
            async for ev in stream:
                if getattr(ev, "type", None) == "text":
                    text = getattr(ev, "text", "") or ""
                    if text:
                        await emit({"t": "token", "v": text})
            msg = await stream.get_final_message()
        except (asyncio.CancelledError, anthropic.APITimeoutError, httpx.TimeoutException):
            # Akış ortasında okuma zaman aşımı httpx'ten çıplak gelir (SDK
            # yalnız istek gönderimini sarar) — ikisi de kısmi usage yazar.
            _record_partial(state, stream, settings, t0)
            raise
        rid = _stream_request_id(stream)
    return msg, int((time.perf_counter() - t0) * 1000), rid


async def _call_model(state: _State, client, settings, emit: Emit):
    """Bir API çağrısı; tek seferlik safe-mode yinelemesi burada."""
    global _SAFE_MODE, _FOLD_SYSTEM
    params = build_params(settings, state.messages, safe_mode=_SAFE_MODE, fold_system=_FOLD_SYSTEM)
    try:
        return await _stream_once(state, client, params, emit, settings)
    except anthropic.BadRequestError as e:
        text = str(e).lower()
        wants_safe = (not _SAFE_MODE) and any(m in text for m in _SAFE_MODE_MARKERS)
        wants_fold = ((not _FOLD_SYSTEM) and _FOLD_REQUIRED in text
                      and any(m in text for m in _FOLD_MARKERS))
        if not (wants_safe or wants_fold):
            raise _Failed("api_error", f"{ERROR_MESSAGES['api_error']} (400)")
        if wants_safe:
            _SAFE_MODE = True
            print("[assistant] UYARI: API beta alanlarını reddetti; süreç SAFE MODE'a geçti "
                  "(betas/fallbacks/block_binding kapalı).", file=sys.stderr)
        if wants_fold:
            _FOLD_SYSTEM = True
            print(f"[assistant] UYARI: model '{settings.model}' role:system mesajını desteklemiyor; "
                  "oturum bağlamı user mesajına katlanıyor (önek değişti, önbellek yeniden yazılır). "
                  "Ayarlardan destekleyen bir model seçin.", file=sys.stderr)
        params = build_params(settings, state.messages, safe_mode=_SAFE_MODE, fold_system=_FOLD_SYSTEM)
        try:
            return await _stream_once(state, client, params, emit, settings)
        except anthropic.APIStatusError as e2:
            raise _Failed("api_error", f"{ERROR_MESSAGES['api_error']} ({e2.status_code})")
    except (anthropic.APITimeoutError, httpx.TimeoutException):
        raise _Failed("timeout")
    except anthropic.APIStatusError as e:
        raise _Failed("api_error", f"{ERROR_MESSAGES['api_error']} ({e.status_code})")
    except anthropic.APIConnectionError:
        raise _Failed("api_error")


async def _exec_tools(ctx: ToolContext, blocks, emit: Emit) -> List[ToolResult]:
    for b in blocks:
        args = b.input if isinstance(getattr(b, "input", None), dict) else {}
        await emit({"t": "tool", "name": b.name, "status": "start", "args": args})
    results = await asyncio.gather(*[
        anyio.to_thread.run_sync(run_tool, ctx, b.name,
                                 dict(b.input) if isinstance(getattr(b, "input", None), dict) else {})
        for b in blocks])
    for b, r in zip(blocks, results):
        await emit({"t": "tool", "name": b.name, "status": "error" if r.is_error else "done",
                    "summary": r.summary, "links": r.links, "ms": r.ms})
    return list(results)


def _add_sources(res: TurnResult, links: List[dict]) -> None:
    seen = {s["href"] for s in res.sources}
    for l in links:
        if l.get("href") and l["href"] not in seen:
            res.sources.append({"label": l.get("label", ""), "href": l["href"]})
            seen.add(l["href"])


async def _run(state: _State, client, settings, ctx: ToolContext, emit: Emit) -> None:
    res = state.res
    tool_rounds = 0
    limit_notice_sent = False
    while True:
        res.calls += 1
        msg, latency, rid = await _call_model(state, client, settings, emit)
        usage = _usage_dict(msg, settings, latency, res.calls, rid)
        res.usages.append(usage)
        res.cost_usd = round(res.cost_usd + usage["cost_usd"], 6)
        res.last_prompt_tokens = (usage["input_tokens"] + usage["cache_creation_input_tokens"]
                                  + usage["cache_read_input_tokens"])
        content = list(getattr(msg, "content", None) or [])
        text = _text_of(content)
        stop = getattr(msg, "stop_reason", None) or "end_turn"
        res.stop_reason = stop
        tool_uses = [b for b in content if getattr(b, "type", None) == "tool_use"]
        row = {"role": "assistant", "kind": "assistant",
               "content": [_block_dict(b) for b in content], "display_text": text, "tools": []}
        if content:
            state.messages.append({"role": "assistant", "content": row["content"]})
            state.rows.append(row)
        state.pending_tool_use = bool(tool_uses)
        if text:
            res.final_text = text

        if stop == "refusal":
            raise _Failed("refusal")
        if stop == "max_tokens":
            res.truncated = True
            if tool_uses:
                raise _Failed("max_tokens")
            return
        if not tool_uses:
            return
        if limit_notice_sent:
            raise _Failed("max_rounds")

        if tool_rounds >= int(settings.max_tool_rounds):
            # Sınır: araç ÇALIŞMAZ, sonuç hata, sistem notu; tool_choice değişmez.
            results_msg = {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": LIMIT_RESULT, "is_error": True}
                for b in tool_uses]}
            notice = {"role": "system", "content": LIMIT_NOTICE}
            row["tools"] = [{"name": b.name, "args": dict(b.input) if isinstance(b.input, dict) else {},
                             "summary": "Araç sınırı", "links": [], "is_error": True, "ms": 0}
                            for b in tool_uses]
            state.messages.append(results_msg)
            state.rows.append({"role": "user", "kind": "tool_result", "content": results_msg["content"],
                               "display_text": None})
            state.messages.append(notice)
            state.rows.append({"role": "system", "kind": "context", "content": notice["content"],
                               "display_text": None})
            state.pending_tool_use = False
            limit_notice_sent = True
            continue

        tool_rounds += 1
        results = await _exec_tools(ctx, tool_uses, emit)
        blocks = []
        for b, r in zip(tool_uses, results):
            blk = {"type": "tool_result", "tool_use_id": b.id, "content": r.content}
            if r.is_error:
                blk["is_error"] = True
            blocks.append(blk)
            call = {"name": b.name, "ms": r.ms, "is_error": r.is_error}
            res.tool_calls.append(call)
            usage["tool_calls"].append(call)
            row["tools"].append({"name": b.name, "args": dict(b.input) if isinstance(b.input, dict) else {},
                                 "summary": r.summary, "links": r.links, "is_error": r.is_error, "ms": r.ms})
            if not r.is_error:
                _add_sources(res, r.links)
        results_msg = {"role": "user", "content": blocks}
        state.messages.append(results_msg)
        state.rows.append({"role": "user", "kind": "tool_result", "content": blocks, "display_text": None})
        state.pending_tool_use = False


def _finalize(state: _State) -> TurnResult:
    """Cevapsız tool_use taşıyan ya da boş son asistan satırını düşür; sonra
    satırlar bir asistan cevabıyla bitmiyorsa sondaki `context` (role system)
    satırlarını da düşür — replay'de arkasından assistant gelmeyen system
    mesajı 400 verir (bkz. modül docstring'i).  Soru/tool_result satırları
    kalır; thinking imzaları etkilenmez (sonrasında blok yok)."""
    rows = state.rows
    while rows and rows[-1]["kind"] == "assistant":
        last = rows[-1]
        has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in last["content"])
        if (has_tool_use and state.pending_tool_use) or not last["content"]:
            rows.pop()
            state.pending_tool_use = False
            continue
        break
    if not (rows and rows[-1]["kind"] == "assistant"):
        while rows and rows[-1]["kind"] == "context":
            rows.pop()
    state.res.rows = rows
    return state.res


async def _emit_done(res: TurnResult, emit: Emit) -> None:
    totals = {k: sum(u.get(k) or 0 for u in res.usages)
              for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                        "output_tokens")}
    totals["cache_creation_1h_tokens"] = sum(u.get("cache_creation_1h_tokens") or 0 for u in res.usages)
    await emit({"t": "done", "stop_reason": res.stop_reason, "truncated": res.truncated,
                "cost_usd": res.cost_usd, "usage": totals, "sources": res.sources,
                "calls": res.calls})


async def run_turn(*, client, settings, ctx: ToolContext, history: List[dict], question: str,
                   emit: Emit, display_question: Optional[str] = None,
                   today: Optional[_dt.datetime] = None,
                   deadline: Optional[float] = None) -> TurnResult:
    """Bir soru: `history` (api_messages) + soru + bağlam → döngü → TurnResult.
    DB'ye DOKUNMAZ (araçlar kendi oturumunu thread'de açar; kalıcılık router'ın).
    `display_question`: rotasyon tohumu soruya katlanmışsa widget'a görünecek
    temiz soru.  Her durumda ya `done` ya `error` olayı gelir (dış iptalde de
    `error`/timeout — CancelledError dışarı çıkmaz)."""
    question_msg = {"role": "user", "content": [{"type": "text", "text": question}]}
    ctx_msg = context_message(ctx, settings, today)
    messages = list(history) + [question_msg, ctx_msg]
    rows = [
        {"role": "user", "kind": "question", "content": question_msg["content"],
         "display_text": display_question if display_question is not None else question},
        {"role": "system", "kind": "context", "content": ctx_msg["content"], "display_text": None},
    ]
    state = _State(messages, rows)
    res = state.res
    try:
        await asyncio.wait_for(_run(state, client, settings, ctx, emit),
                               TURN_DEADLINE if deadline is None else deadline)
    except _Failed as f:
        res.error = {"code": f.code, "msg": f.msg}
    except asyncio.TimeoutError:
        res.error = {"code": "timeout", "msg": ERROR_MESSAGES["timeout"]}
    except asyncio.CancelledError:
        # Dış iptal (router sert sınırı / kapanış): tur timeout sayılır ve görev
        # NORMAL biter ki kısmi usage + soru satırı kalıcılaşsın.  Bilinçli:
        # bu görev yalnız router'ın kuyruk işçisidir, iptali yutmak güvenli.
        res.error = {"code": "timeout", "msg": ERROR_MESSAGES["timeout"]}
    except Exception:
        traceback.print_exc(file=sys.stderr)
        res.error = {"code": "api_error", "msg": ERROR_MESSAGES["api_error"]}
    _finalize(state)
    if res.error is not None:
        if not res.stop_reason:
            res.stop_reason = res.error["code"]
        await emit({"t": "error", "code": res.error["code"], "msg": res.error["msg"]})
    else:
        await _emit_done(res, emit)
    return res
