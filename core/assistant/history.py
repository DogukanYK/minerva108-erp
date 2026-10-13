# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Sohbet/mesaj kalıcılığı, API replay'i ve ROTASYON.

GEÇMİŞ YALNIZ EKLENİR (preserved thinking): hiçbir satır düzenlenmez,
budanmaz, silinmez — Opus 5.5'te eski turun değişmesi sonraki thinking
bloklarını geçersiz kılar (400).  `content_json` API'ye giden/gelen blokların
BİREBİR JSON'u (thinking imzaları dahil); replay byte-stabildir.

Rotasyon (`needs_rotation`): model / dondurulmuş istem hash'i / panel
değişimi, `turns ≥ max_turns` ya da `(last_prompt_tokens − statik önek) >
max_history_tokens` → eski sohbete DOKUNULMAZ, `parent_id` ile yeni sohbet
açılır, ilk soru kısa bir özetle (`rotation_seed`) tohumlanır.

Satır türleri (`kind`): question (user, soru; display_text = kullanıcıya
görünen soru, tohum hariç) · context (system, soru sonrası oturum bağlamı /
araç sınırı notu) · assistant (thinking + text + tool_use blokları; tools_json
= widget çipleri) · tool_result (user, tool_result blokları).
"""
import json
from typing import List, Optional

from sqlalchemy.orm import Session

from database import AssistantConversation, AssistantMessage, to_tr

# Statik önek tahmini (token): araçlar ~3,9K + kurallar ~2K + rehber (giriş + dizin) ~1,3K.
# 13.10.2026 canlı ölçüm: SSS önekteyken 19.661; search_help'e taşındıktan sonra
# beklenen ≈7,2K.  Gerçek değer ilk çağrının usage'ından gözlenir (`observe_prefix_tokens`).
STATIC_PREFIX_DEFAULT = 7200
TITLE_MAX = 60
SEED_TURNS = 3
SEED_CHARS = 400
DISPLAY_KINDS = ("question", "assistant")

_observed_prefix: Optional[int] = None


def observe_prefix_tokens(n) -> None:
    """Taze sohbetin 1. turunda gözlenen `cache_read_input_tokens` — statik
    önek boyutunun canlı tahmini (router çağırır)."""
    global _observed_prefix
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return
    if n > 512:
        _observed_prefix = n


def static_prefix_estimate() -> int:
    return _observed_prefix or STATIC_PREFIX_DEFAULT


def reset_cache() -> None:
    global _observed_prefix
    _observed_prefix = None


# ─── Sohbet ──────────────────────────────────────────────────────────────────

def get_conversation(db: Session, user_id: int, conv_id: int) -> Optional[AssistantConversation]:
    """Yalnız sahibinin AKTİF sohbeti; yoksa None (router 404)."""
    if not conv_id:
        return None
    return (db.query(AssistantConversation)
            .filter(AssistantConversation.id == int(conv_id),
                    AssistantConversation.user_id == int(user_id),
                    AssistantConversation.is_active == True)   # noqa: E712
            .first())


def start_conversation(db: Session, user_id: int, domain: str, model: str, prompt_hash: str,
                       title: Optional[str] = None, parent_id: Optional[int] = None) -> AssistantConversation:
    conv = AssistantConversation(user_id=int(user_id), domain=domain, model=model[:60],
                                 prompt_hash=prompt_hash[:16], parent_id=parent_id,
                                 title=(title or None) and title[:120])
    db.add(conv)
    db.flush()
    return conv


def set_title(conv: AssistantConversation, question: str) -> None:
    if conv.title:
        return
    t = " ".join((question or "").split())
    conv.title = (t[:TITLE_MAX] + "…") if len(t) > TITLE_MAX else (t or None)


def needs_rotation(conv: AssistantConversation, settings, model: str, prompt_hash: str,
                   domain: str) -> Optional[str]:
    """None = devam; aksi hâlde sebep: domain_changed | model_changed |
    prompt_changed | too_long."""
    if conv.domain != domain:
        return "domain_changed"
    if conv.model != model:
        return "model_changed"
    if conv.prompt_hash != prompt_hash:
        return "prompt_changed"
    if int(conv.turns or 0) >= int(settings.max_turns):
        return "too_long"
    if (int(conv.last_prompt_tokens or 0) - static_prefix_estimate()) > int(settings.max_history_tokens):
        return "too_long"
    return None


def _rows(db: Session, conv: AssistantConversation) -> List[AssistantMessage]:
    return (db.query(AssistantMessage)
            .filter(AssistantMessage.conversation_id == conv.id)
            .order_by(AssistantMessage.id.asc()).all())


def rotation_seed(db: Session, conv: AssistantConversation) -> str:
    """Son ≤3 (soru, cevap) çifti, her biri ≤400 karakter →
    <onceki_sohbet_ozeti>…</onceki_sohbet_ozeti>; çift yoksa boş dize."""
    pairs = []
    question = None
    for r in _rows(db, conv):
        if r.kind == "question":
            question = (r.display_text or "").strip()
        elif r.kind == "assistant" and question is not None and (r.display_text or "").strip():
            pairs.append((question, r.display_text.strip()))
            question = None
    pairs = pairs[-SEED_TURNS:]
    if not pairs:
        return ""
    lines = ["<onceki_sohbet_ozeti>"]
    for q, a in pairs:
        lines.append("S: " + q[:SEED_CHARS])
        lines.append("C: " + a[:SEED_CHARS])
    lines.append("</onceki_sohbet_ozeti>")
    return "\n".join(lines)


def seed_question(seed: str, question: str) -> str:
    return (seed + "\n\n" + question) if seed else question


# ─── Mesajlar ────────────────────────────────────────────────────────────────

def api_messages(db: Session, conv: AssistantConversation) -> List[dict]:
    """Satırlar id sırasıyla → [{"role", "content"}] (byte-stabil replay).

    Savunma: arkasından `assistant` satırı gelmeyen `system` satırı (hatalı
    turdan kalmış bağlam) ATLANIR — replay'in ardına daima yeni soru eklendiği
    için böyle bir system mesajı "son girdi ya da assistant izler" kuralını
    bozar ve 400 verirdi.  Runner `_finalize` bunları zaten yazmaz; bu kural
    deterministiktir (önek her turda aynı kalır)."""
    rows = _rows(db, conv)
    out = []
    for i, r in enumerate(rows):
        if r.role == "system":
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            if nxt is None or nxt.role != "assistant":
                continue
        try:
            content = json.loads(r.content_json)
        except (TypeError, ValueError):
            continue
        out.append({"role": r.role, "content": content})
    return out


def append_rows(db: Session, conv: AssistantConversation, rows: List[dict]) -> List[AssistantMessage]:
    """rows: [{"role","kind","content","display_text","tools"?}] — olduğu gibi
    (`json.dumps` sort_keys YOK; gönderilen sıra korunur)."""
    added = []
    for r in rows:
        m = AssistantMessage(
            conversation_id=conv.id, role=r["role"], kind=r["kind"],
            content_json=json.dumps(r["content"], ensure_ascii=False),
            display_text=r.get("display_text"),
            tools_json=json.dumps(r["tools"], ensure_ascii=False) if r.get("tools") else None,
        )
        db.add(m)
        added.append(m)
    db.flush()
    return added


def display_messages(db: Session, conv: AssistantConversation) -> List[dict]:
    """Widget için: yalnız soru ve (metni ya da araç çipi olan) asistan satırları."""
    out = []
    for r in _rows(db, conv):
        if r.kind not in DISPLAY_KINDS:
            continue
        tools = None
        if r.tools_json:
            try:
                tools = json.loads(r.tools_json)
            except (TypeError, ValueError):
                tools = None
        text = r.display_text or ""
        if r.kind == "assistant" and not text.strip() and not tools:
            continue
        out.append({"id": r.id, "role": "user" if r.kind == "question" else "assistant",
                    "text": text, "tools": tools or [],
                    "created_at": to_tr(r.created_at).strftime("%d.%m.%Y %H:%M") if r.created_at else ""})
    return out


def list_conversations(db: Session, user_id: int, limit: int = 20) -> List[dict]:
    rows = (db.query(AssistantConversation)
            .filter(AssistantConversation.user_id == int(user_id),
                    AssistantConversation.is_active == True)   # noqa: E712
            .order_by(AssistantConversation.updated_at.desc(), AssistantConversation.id.desc())
            .limit(limit).all())
    return [{"id": c.id, "title": c.title or "—", "turns": c.turns, "model": c.model,
             "updated_at": to_tr(c.updated_at).strftime("%d.%m.%Y %H:%M") if c.updated_at else "",
             # /asistan geçmişi Bugün / Dün / Bu hafta diye gruplar (TR takvim günü)
             "updated_date": to_tr(c.updated_at).date().isoformat() if c.updated_at else ""}
            for c in rows]


def delete_conversation(db: Session, user_id: int, conv_id: int) -> bool:
    """Sahibi siler (sert silme; mesajlar CASCADE, usage SET NULL kalır)."""
    conv = get_conversation(db, user_id, conv_id)
    if conv is None:
        return False
    db.delete(conv)
    db.flush()
    return True
