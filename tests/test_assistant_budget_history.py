"""Minerva Asistan — ayarlar (settings.py), bütçe (budget.py) ve sohbet
kalıcılığı/rotasyon (history.py).  Fiyat tablosu, TR ay/gün sınırları,
tavan sırası 409→503→402, %80 uyarısı ayda bir + audit; geçmiş yalnız
eklenir, replay byte-stabil, rotasyon sebepleri, sahiplik."""
import datetime as dt
import json

import bcrypt
import pytest

from core.assistant import budget as B
from core.assistant import history as H
from core.assistant import settings as S
from core.assistant.tools import PROMPT_HASH
from database import (AdminAuditLog, AppSetting, AssistantConversation, AssistantMessage, AssistantUsage,
                      TR_OFFSET, User, tr_now)


def _user(db, username, role="Staff"):
    u = User(username=username, full_name=username, role=role, is_active=True,
             password_hash=bcrypt.hashpw(b"x", bcrypt.gensalt()).decode())
    db.add(u)
    db.commit()
    return u


def _usage_row(db, user_id, cost, created_at=None):
    row = AssistantUsage(user_id=user_id, domain="cosmetics", model="claude-opus-5-5", round_no=1,
                         cost_usd=cost, created_at=created_at or dt.datetime.utcnow())
    db.add(row)
    db.commit()
    return row


# ─── Ayarlar ─────────────────────────────────────────────────────────────────

def test_settings_defaults_load_save_validate(db_session):
    db = db_session
    s = S.load_settings(db)
    assert s == S.Settings() and s.model == "claude-opus-5-5" and s.effort == "low" and s.cache_ttl == "1h"
    new = S.save_settings(db, {"model": "claude-sonnet-5-5", "effort": "medium", "monthly_cap_usd": 50,
                               "enabled": False, "max_tool_rounds": "4", "cache_ttl": "5m"})
    db.commit()
    assert new.model == "claude-sonnet-5-5" and new.effort == "medium" and new.monthly_cap_usd == 50.0
    assert new.enabled is False and new.max_tool_rounds == 4 and new.cache_ttl == "5m"
    assert {r.key: r.value for r in db.query(AppSetting).filter(AppSetting.key.like("assistant.%"))} == {
        "assistant.model": "claude-sonnet-5-5", "assistant.effort": "medium", "assistant.monthly_cap_usd": "50.0",
        "assistant.enabled": "false", "assistant.max_tool_rounds": "4", "assistant.cache_ttl": "5m"}
    assert S.load_settings(db) == new
    for bad in ({"model": "gpt-9"}, {"effort": "ultra"}, {"max_turns": 0}, {"monthly_cap_usd": -1},
                {"bilinmeyen": 1}, {"max_output_tokens": "abc"}, {"cache_ttl": "2h"}):
        with pytest.raises(ValueError):
            S.validate(bad)
    # elle bozulmuş satırlar varsayılana düşer
    db.merge(AppSetting(key="assistant.max_turns", value="abc"))
    db.merge(AppSetting(key="assistant.model", value="gpt"))      # satır zaten var → merge
    db.commit()
    s = S.load_settings(db)
    assert s.max_turns == 12 and s.model == "claude-opus-5-5"
    assert S.model_label("claude-opus-5-5") == "Claude Opus 5.5" and S.model_label("claude-foo-6-1") == "Claude Foo 6.1"


# ─── Maliyet ─────────────────────────────────────────────────────────────────

def test_compute_cost_breakdown_and_conservative_fallback():
    u = {"input_tokens": 1000, "cache_creation_input_tokens": 11000, "cache_read_input_tokens": 20000,
         "output_tokens": 500, "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 10000}}
    expect = (1000 * 4 + 20000 * 0.20 + 1000 * 5 + 10000 * 8 + 500 * 20) / 1e6
    assert B.compute_cost("claude-opus-5-5", u) == round(expect, 6)
    flat = {"input_tokens": 1000, "cache_creation_input_tokens": 11000, "cache_read_input_tokens": 0,
            "output_tokens": 0}
    assert B.compute_cost("claude-opus-5-5", flat) == round((1000 * 4 + 11000 * 8) / 1e6, 6)   # hepsi 1h
    assert B.compute_cost("claude-sonnet-5-5", flat) == round((1000 * 2 + 11000 * 4) / 1e6, 6)
    assert B.compute_cost("bilinmeyen", flat) == B.compute_cost("claude-opus-5-5", flat)
    assert B.compute_cost("claude-haiku-5-5", None) == 0.0
    f = B.usage_fields(u)
    assert f["cache_creation_1h_tokens"] == 10000 and f["cache_creation_5m_tokens"] == 1000
    assert B.usage_fields(flat)["cache_creation_1h_tokens"] is None


# ─── Tavan denetimi ──────────────────────────────────────────────────────────

def test_check_budget_order_and_scoping(db_session, monkeypatch):
    db = db_session
    me, other = _user(db, "bud_me"), _user(db, "bud_other")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    d = B.check_budget(db, S.Settings(enabled=False), me.id)
    assert (d.status, d.code) == (409, "disabled") and "kapalı" in d.detail
    d = B.check_budget(db, S.Settings(), me.id)
    assert (d.status, d.code) == (503, "not_configured")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert B.check_budget(db, S.Settings(), me.id) is None
    _usage_row(db, other.id, 2.0)
    _usage_row(db, me.id, 2.5)
    d = B.check_budget(db, S.Settings(monthly_cap_usd=4.0), me.id)
    assert (d.status, d.code) == (402, "monthly_cap")
    d = B.check_budget(db, S.Settings(monthly_cap_usd=100.0, daily_user_cap_usd=3.0), me.id)
    assert d is None                                          # 2.5 < 3
    _usage_row(db, me.id, 0.6)
    d = B.check_budget(db, S.Settings(monthly_cap_usd=100.0, daily_user_cap_usd=3.0), me.id)
    assert (d.status, d.code) == (402, "daily_cap")
    assert B.check_budget(db, S.Settings(monthly_cap_usd=100.0, daily_user_cap_usd=3.0), other.id) is None
    assert B.check_budget(db, S.Settings(monthly_cap_usd=0, daily_user_cap_usd=0), me.id) is None   # 0 = sınırsız


def test_record_usage_and_tr_month_day_bounds(db_session):
    db = db_session
    me = _user(db, "rec_me")
    row = B.record_usage(db, user_id=me.id, conversation_id=None, message_id=None, model="claude-opus-5-5",
                         usage={"input_tokens": 10, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30,
                                "output_tokens": 40, "cache_creation": {"ephemeral_1h_input_tokens": 20,
                                                                        "ephemeral_5m_input_tokens": 0}},
                         cost_usd=0.001234, tool_calls=[{"name": "find_item", "ms": 5, "is_error": False}],
                         latency_ms=812, stop_reason="end_turn", request_id="req_x", domain="cosmetics",
                         round_no=2, question_preview="x" * 500)
    db.commit()
    db.expire_all()
    r = db.get(AssistantUsage, row.id)
    assert (r.input_tokens, r.cache_creation_input_tokens, r.cache_creation_1h_tokens, r.cache_read_input_tokens,
            r.output_tokens) == (10, 20, 20, 30, 40)
    assert r.cost_usd == 0.001234 and r.round_no == 2 and r.stop_reason == "end_turn" and r.request_id == "req_x"
    assert json.loads(r.tool_calls_json)[0]["name"] == "find_item" and len(r.question_preview) == 160
    # TR günü: bugün TR 00:30 = dün UTC 21:30 → bugüne sayılır; dün TR 23:30 sayılmaz
    today = tr_now().date()
    t_030 = dt.datetime.combine(today, dt.time(0, 30)) - TR_OFFSET
    y_2330 = dt.datetime.combine(today - dt.timedelta(days=1), dt.time(23, 30)) - TR_OFFSET
    _usage_row(db, me.id, 1.0, t_030)
    _usage_row(db, me.id, 5.0, y_2330)
    assert B.user_day_spend(db, me.id, today) == pytest.approx(1.001234)
    lo, hi = B.month_bounds_utc(today.year, today.month)
    assert lo == dt.datetime(today.year, today.month, 1) - TR_OFFSET and hi > lo
    assert B.month_spend(db, today.year, today.month) >= 1.001234


def test_maybe_warn_once_per_month_with_audit_and_push(db_session, monkeypatch):
    db = db_session
    sent = []
    import core.notifications as N
    monkeypatch.setattr(N, "_send_push_to_users", lambda ids, payload: sent.append((sorted(ids), payload)))
    st = S.Settings(monthly_cap_usd=100.0)
    assert B.maybe_warn(db, st, 70.0) is False and sent == []
    assert B.maybe_warn(db, st, 85.0) is True
    db.commit()
    now = tr_now()
    key = f"assistant.warned.{now.year:04d}-{now.month:02d}"
    assert db.query(AppSetting).filter_by(key=key).one().value == "85.00"
    audit = db.query(AdminAuditLog).filter_by(action="assistant.budget_warning").all()
    assert len(audit) == 1 and audit[0].actor_name == "sistem" and json.loads(audit[0].details)["cap"] == 100.0
    sa_ids = sorted(u.id for u in db.query(User).filter_by(role="SuperAdmin", is_active=True))
    assert sent == [(sa_ids, {"title": "Minerva Asistan bütçesi", "body": "Aylık bütçenin %80'i kullanıldı ($85.00 / $100)",
                              "tag": "assistant-budget", "url": "/admin"})]
    assert B.maybe_warn(db, st, 95.0) is False and len(sent) == 1       # ayda bir
    assert B.maybe_warn(db, S.Settings(monthly_cap_usd=0), 999.0) is False


# ─── Geçmiş ──────────────────────────────────────────────────────────────────

def test_history_roundtrip_display_rotation_and_ownership(db_session):
    db = db_session
    me, other = _user(db, "h_me"), _user(db, "h_other")
    conv = H.start_conversation(db, me.id, "cosmetics", "claude-opus-5-5", PROMPT_HASH)
    H.set_title(conv, "  Stearil   alkol " + "x" * 80)
    assert conv.title.endswith("…") and len(conv.title) == 61
    H.set_title(conv, "ikinci")                                     # ilk başlık korunur
    assert conv.title.startswith("Stearil alkol")
    thinking = {"type": "thinking", "thinking": "", "signature": "sig-1"}
    tool_use = {"type": "tool_use", "id": "t1", "name": "find_item", "input": {"q": "stearil", "category": None}}
    rows = [
        {"role": "user", "kind": "question", "content": [{"type": "text", "text": "Stearil alkol?"}],
         "display_text": "Stearil alkol?"},
        {"role": "system", "kind": "context", "content": "Oturum bağlamı — …", "display_text": None},
        {"role": "assistant", "kind": "assistant", "content": [thinking, tool_use], "display_text": "",
         "tools": [{"name": "find_item", "summary": "3 kart", "links": [{"label": "Ürünler", "href": "/items?q=s"}],
                    "is_error": False, "ms": 4, "args": {"q": "stearil"}}]},
        {"role": "user", "kind": "tool_result", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "{}"}],
         "display_text": None},
        {"role": "assistant", "kind": "assistant", "content": [{"type": "text", "text": "3944 kg var."}],
         "display_text": "3944 kg var.", "tools": []},
    ]
    added = H.append_rows(db, conv, rows)
    conv.turns += 1
    db.commit()
    assert len(added) == 5 and [m.kind for m in added] == ["question", "context", "assistant", "tool_result", "assistant"]
    # replay birebir (thinking imzası dahil, sıra korunur)
    msgs = H.api_messages(db, conv)
    assert [m["role"] for m in msgs] == ["user", "system", "assistant", "user", "assistant"]
    assert msgs[2]["content"] == [thinking, tool_use] and msgs[1]["content"] == "Oturum bağlamı — …"
    assert json.loads(added[2].content_json) == [thinking, tool_use]
    # widget görünümü: soru + (çipli) araç turu + cevap; context/tool_result yok
    disp = H.display_messages(db, conv)
    assert [(d["role"], d["text"]) for d in disp] == [("user", "Stearil alkol?"), ("assistant", ""), ("assistant", "3944 kg var.")]
    assert disp[1]["tools"][0]["summary"] == "3 kart"
    # rotasyon sebepleri
    st = S.Settings(max_turns=12, max_history_tokens=8000)
    assert H.needs_rotation(conv, st, "claude-opus-5-5", PROMPT_HASH, "cosmetics") is None
    assert H.needs_rotation(conv, st, "claude-opus-5-5", PROMPT_HASH, "supplement") == "domain_changed"
    assert H.needs_rotation(conv, st, "claude-sonnet-5-5", PROMPT_HASH, "cosmetics") == "model_changed"
    assert H.needs_rotation(conv, st, "claude-opus-5-5", "ffffffffffff", "cosmetics") == "prompt_changed"
    assert H.needs_rotation(conv, S.Settings(max_turns=1), "claude-opus-5-5", PROMPT_HASH, "cosmetics") == "too_long"
    conv.last_prompt_tokens = H.static_prefix_estimate() + 8001
    assert H.needs_rotation(conv, st, "claude-opus-5-5", PROMPT_HASH, "cosmetics") == "too_long"
    H.observe_prefix_tokens(12000)
    assert H.static_prefix_estimate() == 12000
    assert H.needs_rotation(conv, st, "claude-opus-5-5", PROMPT_HASH, "cosmetics") is None
    H.reset_cache()
    assert H.static_prefix_estimate() == H.STATIC_PREFIX_DEFAULT
    # tohum
    seed = H.rotation_seed(db, conv)
    assert seed.startswith("<onceki_sohbet_ozeti>\nS: Stearil alkol?\nC: 3944 kg var.") and seed.endswith("</onceki_sohbet_ozeti>")
    assert H.seed_question(seed, "devam") == seed + "\n\ndevam" and H.seed_question("", "devam") == "devam"
    child = H.start_conversation(db, me.id, "cosmetics", "claude-opus-5-5", PROMPT_HASH, parent_id=conv.id)
    db.commit()
    assert child.parent_id == conv.id and H.rotation_seed(db, child) == ""
    assert db.query(AssistantMessage).filter_by(conversation_id=conv.id).count() == 5   # eskiye dokunulmadı
    # sahiplik + liste + silme
    assert [c["id"] for c in H.list_conversations(db, me.id)] == [child.id, conv.id]
    assert H.list_conversations(db, other.id) == []
    assert H.get_conversation(db, other.id, conv.id) is None and H.get_conversation(db, me.id, conv.id) is conv
    assert H.delete_conversation(db, other.id, conv.id) is False
    assert H.delete_conversation(db, me.id, conv.id) is True
    db.commit()
    assert db.get(AssistantConversation, conv.id) is None
    assert db.query(AssistantMessage).filter_by(conversation_id=conv.id).count() == 0
    db.expire_all()
    assert db.get(AssistantConversation, child.id).parent_id is None           # SET NULL


def test_api_messages_skips_orphan_system_rows(db_session):
    """Arkasından assistant gelmeyen system satırı (eski/hatalı turdan kalmış)
    replay'e girmez — ardına soru eklenince 400 verirdi; kural deterministik."""
    db = db_session
    me = _user(db, "orphan_me")
    conv = H.start_conversation(db, me.id, "cosmetics", "claude-opus-5-5", PROMPT_HASH)
    H.append_rows(db, conv, [
        {"role": "user", "kind": "question", "content": [{"type": "text", "text": "q1"}], "display_text": "q1"},
        {"role": "system", "kind": "context", "content": "ctx1", "display_text": None},       # öksüz
        {"role": "user", "kind": "question", "content": [{"type": "text", "text": "q2"}], "display_text": "q2"},
        {"role": "system", "kind": "context", "content": "ctx2", "display_text": None},       # assistant izler
        {"role": "assistant", "kind": "assistant", "content": [{"type": "text", "text": "a2"}], "display_text": "a2"},
        {"role": "user", "kind": "question", "content": [{"type": "text", "text": "q3"}], "display_text": "q3"},
        {"role": "system", "kind": "context", "content": "ctx3", "display_text": None},       # sonda, öksüz
    ])
    db.commit()
    msgs = H.api_messages(db, conv)
    assert [m["role"] for m in msgs] == ["user", "user", "system", "assistant", "user"]
    assert msgs[2]["content"] == "ctx2"
    assert msgs == H.api_messages(db, conv)                                   # byte-stabil
    # widget görünümü etkilenmez
    assert [m["text"] for m in H.display_messages(db, conv)] == ["q1", "q2", "a2", "q3"]
