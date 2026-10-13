"""Minerva Asistan — HTTP katmanı (routers/assistant.py).  SDK tamamen sahte
(tests/assistant_fakes.py), ağ YOK; `ANTHROPIC_API_KEY` sahte değer.

Kapsam (spec §12): 401 / 403 (Distributor, assistant.use=False) / 409 / 503 /
402 / 422 / 404; NDJSON olay sırası (`client.stream`) + başlıklar; satırların ve
usage'ın kalıcılığı (yeni SessionLocal, message_id); geçmişin replay'i; hata
olayında kalıcılık; rotasyon (too_long / model / panel → yeni id, tohum, eski
sohbete dokunulmaz); sahiplik; kullanıcı bazlı rate limit (aynı IP'den A 429
iken B 200); ping; bootstrap; admin usage/settings yetkileri + audit + geçersiz
model 400."""
import json

import bcrypt
import pytest

from core.assistant import history as H
from core.assistant import runner as R
from core.assistant.tools import PROMPT_HASH
from database import (AdminAuditLog, AppSetting, AssistantConversation, AssistantMessage, AssistantUsage,
                      Item, User)
from tests.assistant_fakes import FakeClient, message, status_error, text_block, tool_use_block, usage

_H = {"Origin": "http://testserver"}
CHAT = "/api/assistant/chat"
CONVS = "/api/assistant/conversations"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    R.reset_cache()
    H.reset_cache()
    yield
    R.reset_cache()
    H.reset_cache()


def _install(monkeypatch, script=None, delay=0.0) -> FakeClient:
    fake = FakeClient(script, delay=delay)
    monkeypatch.setattr("core.assistant.client.get_client", lambda: fake)
    return fake


def _login(client, db, username, role="Staff", permissions=None):
    if db.query(User).filter(User.username == username).first() is None:
        db.add(User(username=username, full_name=username.replace("_", " ").title(), role=role, is_active=True,
                    password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode(),
                    permissions=json.dumps(permissions) if permissions is not None else None))
        db.commit()
    client.post("/api/logout", headers=_H)
    r = client.post("/api/login", json={"username": username, "password": "minerva123"}, headers=_H)
    assert r.status_code == 200, r.text
    return db.query(User).filter(User.username == username).one()


def _chat(client, text, conv_id=None):
    """→ (status, olay listesi) ya da (status, JSON hata gövdesi)."""
    body = {"message": text}
    if conv_id:
        body["conversation_id"] = conv_id
    with client.stream("POST", CHAT, json=body, headers=_H) as r:
        if r.status_code != 200:
            r.read()
            return r.status_code, json.loads(r.content or b"{}")
        assert r.headers["content-type"].startswith("application/x-ndjson")
        assert r.headers["x-accel-buffering"] == "no" and "no-cache" in r.headers["cache-control"]
        return 200, [json.loads(line) for line in r.iter_lines() if line.strip()]


def _setting(db, key, value):
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    db.commit()


def _usage_row(db, user_id, cost):
    db.add(AssistantUsage(user_id=user_id, domain="cosmetics", model="claude-opus-5-5", round_no=1,
                          cost_usd=cost))
    db.commit()


def _rows(db, conv_id):
    return (db.query(AssistantMessage).filter(AssistantMessage.conversation_id == conv_id)
            .order_by(AssistantMessage.id).all())


# ─── Kapılar ─────────────────────────────────────────────────────────────────

def test_unauthenticated_401(client):
    assert client.post(CHAT, json={"message": "x"}, headers=_H).status_code == 401
    for p in (CONVS, CONVS + "/1", "/api/assistant/bootstrap", "/api/assistant/admin/usage"):
        assert client.get(p).status_code == 401, p
    assert client.put("/api/assistant/admin/settings", json={"effort": "low"}, headers=_H).status_code == 401
    assert client.delete(CONVS + "/1", headers=_H).status_code == 401


def test_distributor_and_use_false_403(client, db_session, monkeypatch):
    _install(monkeypatch)
    _login(client, db_session, "bayi_a", "Distributor")
    assert _chat(client, "merhaba")[0] == 403
    assert client.get("/api/assistant/bootstrap").status_code == 403
    assert client.get(CONVS).status_code == 403
    # override rol varsayılanının yerine geçer: assistant anahtarı yoksa kapalı
    _login(client, db_session, "staff_kapali", "Staff", {"items": {"view": True}, "assistant": {"use": False}})
    status, body = _chat(client, "merhaba")
    assert status == 403 and "assistant.use" in body["detail"]
    assert client.get("/api/assistant/bootstrap").status_code == 403
    _login(client, db_session, "staff_acik", "Staff", {"items": {"view": True}, "assistant": {"use": True}})
    assert _chat(client, "merhaba")[0] == 200
    assert client.get("/api/assistant/bootstrap").status_code == 200


def test_disabled_409_not_configured_503_caps_402(client, db_session, monkeypatch):
    _install(monkeypatch)
    me = _login(client, db_session, "staff_b", "Staff")
    _setting(db_session, "assistant.enabled", "false")
    status, body = _chat(client, "merhaba")
    assert status == 409 and body["code"] == "disabled" and "kapalı" in body["detail"]
    b = client.get("/api/assistant/bootstrap").json()
    assert b["enabled"] is False and b["code"] == "disabled" and "kapalı" in b["reason"]
    _setting(db_session, "assistant.enabled", "true")

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    status, body = _chat(client, "merhaba")
    assert status == 503 and body["code"] == "not_configured" and "anahtar" in body["detail"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    other = _login(client, db_session, "staff_c", "Staff")
    _usage_row(db_session, other.id, 2.0)
    _login(client, db_session, "staff_b", "Staff")
    _usage_row(db_session, me.id, 2.5)
    _setting(db_session, "assistant.monthly_cap_usd", "4")   # 4.5 ≥ 4 aylık
    status, body = _chat(client, "merhaba")
    assert status == 402 and body["code"] == "monthly_cap" and "Aylık" in body["detail"]
    _setting(db_session, "assistant.monthly_cap_usd", "1000")
    assert _chat(client, "merhaba")[0] == 200                # 2.5 < 3 günlük
    _usage_row(db_session, me.id, 0.6)                       # 3.1 ≥ 3.0 günlük
    status, body = _chat(client, "merhaba")
    assert status == 402 and body["code"] == "daily_cap" and "Günlük" in body["detail"]
    b = client.get("/api/assistant/bootstrap").json()
    assert b["enabled"] is False and b["code"] == "daily_cap" and b["daily_spent_usd"] > 3.1
    # başka kullanıcı etkilenmez
    _login(client, db_session, "staff_c", "Staff")
    assert client.get("/api/assistant/bootstrap").json()["enabled"] is True


def test_validation_422_and_unknown_conversation_404(client, db_session, monkeypatch):
    _install(monkeypatch)
    _login(client, db_session, "staff_d", "Staff")
    assert client.post(CHAT, json={"message": "a" * 2001}, headers=_H).status_code == 422
    assert client.post(CHAT, json={"message": "   "}, headers=_H).status_code == 422
    assert client.post(CHAT, json={"message": "x", "conversation_id": 0}, headers=_H).status_code == 422
    assert client.post(CHAT, json={}, headers=_H).status_code == 422
    status, body = _chat(client, "merhaba", conv_id=999999)
    assert status == 404 and "Sohbet" in body["detail"]
    assert db_session.query(AssistantConversation).count() == 0       # 404'te sohbet açılmaz
    assert client.get(CONVS + "/999999").status_code == 404
    assert client.delete(CONVS + "/999999", headers=_H).status_code == 404


# ─── Akış + kalıcılık ─────────────────────────────────────────────────────────

def test_stream_sequence_real_tool_and_persistence(client, db_session, monkeypatch):
    db = db_session
    db.add(Item(name="JOJOBA YAĞI", category="Hammadde", unit="kg", current_stock=12.5, domain="cosmetics"))
    db.commit()
    me = _login(client, db_session, "lab_a", "LabLead")
    fake = _install(monkeypatch, [
        message(tool_use_block("t1", "find_item", {"q": "jojoba", "category": None}), stop_reason="tool_use",
                usage_obj=usage(read=11000)),
        message(text_block("Jojoba yağı 12,5 kg."), usage_obj=usage(read=11200, output=40)),
    ])
    status, events = _chat(client, "Jojoba kaç kg?")
    assert status == 200
    assert [(e["t"], e.get("status")) for e in events] == [
        ("meta", None), ("tool", "start"), ("tool", "done"), ("token", None), ("done", None)]
    meta, done = events[0], events[-1]
    conv = db.query(AssistantConversation).one()
    assert meta == {"t": "meta", "conversation_id": conv.id, "rotated": False, "rotation_reason": None,
                    "model": "claude-opus-5-5"}
    assert events[1]["args"] == {"q": "jojoba", "category": None}
    assert events[2]["summary"] == "1 kart" and events[2]["links"] == [{"label": "Ürünler", "href": "/items?q=jojoba"}]
    assert events[3]["v"] == "Jojoba yağı 12,5 kg."
    assert done["stop_reason"] == "end_turn" and done["truncated"] is False and done["calls"] == 2
    assert done["sources"] == [{"label": "Ürünler", "href": "/items?q=jojoba"}]
    assert done["usage"]["cache_read_input_tokens"] == 22200 and done["cost_usd"] > 0

    # Satırlar: question → context → assistant(tool_use) → tool_result → assistant
    rows = _rows(db, conv.id)
    assert [r.kind for r in rows] == ["question", "context", "assistant", "tool_result", "assistant"]
    assert [r.role for r in rows] == ["user", "system", "assistant", "user", "assistant"]
    assert rows[0].display_text == "Jojoba kaç kg?" and json.loads(rows[0].content_json) == [
        {"type": "text", "text": "Jojoba kaç kg?"}]
    assert json.loads(rows[1].content_json).startswith("Oturum bağlamı — Kullanıcı: Lab A")
    assert json.loads(rows[2].tools_json)[0]["summary"] == "1 kart"
    assert json.loads(json.loads(rows[3].content_json)[0]["content"])["items"][0]["name"] == "JOJOBA YAĞI"
    assert rows[4].display_text == "Jojoba yağı 12,5 kg."
    assert done["message_id"] == rows[4].id
    assert conv.title == "Jojoba kaç kg?" and conv.turns == 1 and conv.last_prompt_tokens == 11400
    assert conv.user_id == me.id and conv.domain == "cosmetics" and conv.prompt_hash == PROMPT_HASH
    # usage: çağrı başına satır, aynı message_id, soru önizlemesi, maliyet donuk
    us = db.query(AssistantUsage).order_by(AssistantUsage.id).all()
    assert [u.round_no for u in us] == [1, 2] and {u.message_id for u in us} == {rows[4].id}
    assert all(u.user_id == me.id and u.conversation_id == conv.id and u.domain == "cosmetics" for u in us)
    assert us[0].question_preview == "Jojoba kaç kg?" and us[0].cache_read_input_tokens == 11000
    assert json.loads(us[0].tool_calls_json)[0]["name"] == "find_item" and us[1].stop_reason == "end_turn"
    assert round(sum(u.cost_usd for u in us), 6) == done["cost_usd"]
    assert H.static_prefix_estimate() == 11000               # taze sohbetin 1. turu gözlendi

    # İkinci tur: geçmiş birebir replay + yeni soru + bağlam; turns artar
    fake.script.append(message(text_block("Depoda.")))
    status, events = _chat(client, "Nerede?", conv_id=conv.id)
    assert status == 200 and events[0]["conversation_id"] == conv.id and events[0]["rotated"] is False
    msgs = fake.calls[-1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "system", "assistant", "user", "assistant", "user", "system"]
    assert msgs[2]["content"] == json.loads(rows[2].content_json)
    assert msgs[5]["content"] == [{"type": "text", "text": "Nerede?"}]
    db.expire_all()
    conv = db.get(AssistantConversation, conv.id)
    assert conv.turns == 2 and len(_rows(db, conv.id)) == 8
    assert client.get(CONVS).json()[0] == {"id": conv.id, "title": "Jojoba kaç kg?", "turns": 2,
                                           "model": "claude-opus-5-5",
                                           "updated_at": client.get(CONVS).json()[0]["updated_at"],
                                           "updated_date": client.get(CONVS).json()[0]["updated_date"]}
    detail = client.get(f"{CONVS}/{conv.id}").json()
    assert [(m["role"], m["text"]) for m in detail["messages"]] == [
        ("user", "Jojoba kaç kg?"), ("assistant", ""), ("assistant", "Jojoba yağı 12,5 kg."),
        ("user", "Nerede?"), ("assistant", "Depoda.")]
    assert detail["messages"][1]["tools"][0]["name"] == "find_item"


def test_error_event_persists_question_and_usage(client, db_session, monkeypatch):
    _install(monkeypatch, [status_error(500)])
    _login(client, db_session, "lab_b", "LabTech")
    status, events = _chat(client, "Bir şey sor")
    assert status == 200
    assert [e["t"] for e in events] == ["meta", "error"]
    assert events[1]["code"] == "api_error" and "ulaşılamadı" in events[1]["msg"]
    conv = db_session.query(AssistantConversation).one()
    # cevap yok → bağlam (role:system) satırı YAZILMAZ; soru kalır
    assert [r.kind for r in _rows(db_session, conv.id)] == ["question"]
    assert conv.turns == 1 and db_session.query(AssistantUsage).count() == 0
    # refusal (içeriksiz): usage YAZILIR (çağrı yapıldı), asistan satırı yok
    _install(monkeypatch, [message(stop_reason="refusal")])
    status, events = _chat(client, "Soru 2", conv_id=conv.id)
    assert events[-1]["code"] == "refusal"
    assert db_session.query(AssistantUsage).count() == 1
    assert [r.kind for r in _rows(db_session, conv.id)] == ["question", "question"]
    # Hatalı turlardan sonra devam: API'ye giden dizide assistant'sız system mesajı YOK
    fake = _install(monkeypatch, [message(text_block("Şimdi oldu."))])
    status, events = _chat(client, "Soru 3", conv_id=conv.id)
    assert status == 200 and events[-1]["t"] == "done"
    msgs = fake.calls[-1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "user", "user", "system"]
    for i, m in enumerate(msgs[:-1]):
        assert not (m["role"] == "system" and msgs[i + 1]["role"] != "assistant")
    assert [r.kind for r in _rows(db_session, conv.id)] == ["question", "question", "question", "context",
                                                              "assistant"]
    db_session.expire_all()
    assert db_session.get(AssistantConversation, conv.id).turns == 3


def test_usage_recorded_when_conversation_deleted_mid_stream(client, db_session, monkeypatch):
    """Akış sırasında sohbet silinirse satırlar yazılmaz ama usage (conversation_id
    NULL) YAZILIR — maliyet oluştu, tavan görmeli."""
    from database import SessionLocal

    deleted = {}

    def _delete_during_stream(params):
        db = SessionLocal()
        try:
            conv = db.query(AssistantConversation).one()
            deleted["id"] = conv.id
            assert H.delete_conversation(db, conv.user_id, conv.id)
            db.commit()
        finally:
            db.close()

    fake = FakeClient([message(text_block("Cevap."), usage_obj=usage(read=11000, output=30))],
                      on_stream=_delete_during_stream)
    monkeypatch.setattr("core.assistant.client.get_client", lambda: fake)
    me = _login(client, db_session, "del_mid", "LabTech")
    status, events = _chat(client, "Silinecek soru")
    assert status == 200 and events[-1]["t"] == "done" and events[-1]["message_id"] is None
    db_session.expire_all()
    assert db_session.get(AssistantConversation, deleted["id"]) is None
    assert db_session.query(AssistantMessage).count() == 0
    u = db_session.query(AssistantUsage).one()
    assert u.user_id == me.id and u.conversation_id is None and u.message_id is None
    assert u.cost_usd > 0 and u.cache_read_input_tokens == 11000
    # bütçe bu turu görür
    assert client.get("/api/assistant/bootstrap").json()["daily_spent_usd"] == round(u.cost_usd, 4)


def test_concurrent_turn_cap_busy_429(client, db_session, monkeypatch):
    """Kullanıcı başına eşzamanlı tur sınırı: akış sırasında sayaç 1, bitince 0;
    sınır doluysa 429 `busy` (akış açılmaz, sohbet yazılmaz)."""
    from routers import assistant as RA

    seen = {}

    def _peek(params):
        seen["active"] = RA._active_turns(me.id)

    fake = FakeClient([message(text_block("Ok."))], on_stream=_peek)
    monkeypatch.setattr("core.assistant.client.get_client", lambda: fake)
    me = _login(client, db_session, "busy_a", "Staff")
    status, events = _chat(client, "ilk")
    assert status == 200 and seen["active"] == 1 and RA._active_turns(me.id) == 0
    # sınır dolu (başka sekmede 2 soru akıyor): 429 busy
    monkeypatch.setitem(RA._ACTIVE, me.id, [RA.time.monotonic()] * RA.MAX_ACTIVE_TURNS)
    status, body = _chat(client, "üçüncü")
    assert status == 429 and body["code"] == "busy" and body["retry_after_seconds"] > 0
    assert db_session.query(AssistantConversation).count() == 1
    # bayat damgalar TTL sonrası kendiliğinden düşer (iptal yolu kaçırılsa bile kilit kalmaz)
    monkeypatch.setitem(RA._ACTIVE, me.id, [RA.time.monotonic() - RA.ACTIVE_TURN_TTL - 1] * 2)
    assert RA._active_turns(me.id) == 0
    fake.script.append(message(text_block("Yine ok.")))
    assert _chat(client, "dördüncü")[0] == 200
    # başka kullanıcı etkilenmez
    monkeypatch.setitem(RA._ACTIVE, me.id, [RA.time.monotonic()] * RA.MAX_ACTIVE_TURNS)
    _login(client, db_session, "busy_b", "Staff")
    fake.script.append(message(text_block("B ok.")))
    assert _chat(client, "B")[0] == 200


def test_ping_while_waiting(client, db_session, monkeypatch):
    monkeypatch.setattr("routers.assistant.PING_INTERVAL", 0.05)
    _install(monkeypatch, [message(text_block("Geç cevap."))], delay=0.25)
    _login(client, db_session, "lab_c", "LabTech")
    status, events = _chat(client, "Yavaş?")
    kinds = [e["t"] for e in events]
    assert kinds[0] == "meta" and kinds[-1] == "done" and "ping" in kinds
    assert kinds.index("ping") < kinds.index("token")


# ─── Rotasyon ────────────────────────────────────────────────────────────────

def test_rotation_too_long_model_and_domain(client, db_session, monkeypatch):
    db = db_session
    _setting(db, "assistant.max_turns", "1")
    _login(client, db_session, "lab_d", "LabLead")
    fake = _install(monkeypatch, [message(text_block("İlk cevap.")), message(text_block("İkinci cevap."))])
    _, ev1 = _chat(client, "İlk soru")
    old_id = ev1[0]["conversation_id"]
    old_rows = len(_rows(db, old_id))

    _, ev2 = _chat(client, "İkinci soru", conv_id=old_id)
    meta = ev2[0]
    assert meta["rotated"] is True and meta["rotation_reason"] == "too_long" and meta["conversation_id"] != old_id
    new = db.get(AssistantConversation, meta["conversation_id"])
    assert new.parent_id == old_id and new.turns == 1 and new.title == "İkinci soru"
    # Tohum: API'ye giden ilk mesaj özet + soru; widget'a görünen soru temiz; geçmiş taşınmaz
    sent = fake.calls[-1]["messages"]
    assert [m["role"] for m in sent] == ["user", "system"]
    text = sent[0]["content"][0]["text"]
    assert text.startswith("<onceki_sohbet_ozeti>") and "S: İlk soru" in text and "C: İlk cevap." in text
    assert text.endswith("</onceki_sohbet_ozeti>\n\nİkinci soru")
    q = _rows(db, new.id)[0]
    assert q.display_text == "İkinci soru" and "<onceki_sohbet_ozeti>" in q.content_json
    # Eski sohbete dokunulmaz
    db.expire_all()
    old = db.get(AssistantConversation, old_id)
    assert old.turns == 1 and old.is_active is True and len(_rows(db, old_id)) == old_rows
    assert {c["id"] for c in client.get(CONVS).json()} == {old_id, new.id}

    # Model değişimi → rotasyon (max_turns yüksek olsa da)
    _setting(db, "assistant.max_turns", "12")
    _setting(db, "assistant.model", "claude-sonnet-5-5")
    fake.script.append(message(text_block("Sonnet cevabı."), model="claude-sonnet-5-5"))
    _, ev3 = _chat(client, "Üçüncü", conv_id=new.id)
    assert ev3[0]["rotated"] is True and ev3[0]["rotation_reason"] == "model_changed"
    assert ev3[0]["model"] == "claude-sonnet-5-5"
    third = db.get(AssistantConversation, ev3[0]["conversation_id"])
    assert third.model == "claude-sonnet-5-5" and third.parent_id == new.id
    assert db.query(AssistantUsage).filter_by(conversation_id=third.id).one().model == "claude-sonnet-5-5"

    # Panel değişimi → rotasyon, yeni sohbet yeni panelde
    client.cookies.set("active_domain", "supplement")
    fake.script.append(message(text_block("Takviye.")))
    _, ev4 = _chat(client, "Dördüncü", conv_id=third.id)
    assert ev4[0]["rotation_reason"] == "domain_changed"
    assert db.get(AssistantConversation, ev4[0]["conversation_id"]).domain == "supplement"


# ─── Sahiplik ────────────────────────────────────────────────────────────────

def test_conversation_ownership_list_detail_delete(client, db_session, monkeypatch):
    _install(monkeypatch)
    _login(client, db_session, "owner_a", "LabTech")
    _, ev = _chat(client, "Benim sorum")
    cid = ev[0]["conversation_id"]
    assert [c["id"] for c in client.get(CONVS).json()] == [cid]

    _login(client, db_session, "owner_b", "LabTech")
    assert client.get(CONVS).json() == []
    assert client.get(f"{CONVS}/{cid}").status_code == 404
    assert client.delete(f"{CONVS}/{cid}", headers=_H).status_code == 404
    assert _chat(client, "Devam", conv_id=cid)[0] == 404                 # başkasının sohbetine yazılamaz
    assert db_session.get(AssistantConversation, cid) is not None

    _login(client, db_session, "owner_a", "LabTech")
    d = client.get(f"{CONVS}/{cid}").json()
    assert d["id"] == cid and d["title"] == "Benim sorum" and d["messages"][0]["text"] == "Benim sorum"
    assert client.delete(f"{CONVS}/{cid}", headers=_H).status_code == 204
    assert client.get(f"{CONVS}/{cid}").status_code == 404 and client.get(CONVS).json() == []
    db_session.expire_all()
    assert db_session.get(AssistantConversation, cid) is None
    assert db_session.query(AssistantMessage).count() == 0               # CASCADE
    u = db_session.query(AssistantUsage).one()                           # usage kalır, SET NULL
    assert u.conversation_id is None and u.message_id is None and u.cost_usd > 0


# ─── Rate limit — kullanıcı bazlı ────────────────────────────────────────────

def test_rate_limit_is_per_user_not_per_ip(client, db_session, monkeypatch):
    _install(monkeypatch)
    _login(client, db_session, "rl_a", "Staff")
    for i in range(20):
        assert _chat(client, f"soru {i}")[0] == 200, i
    status, body = _chat(client, "21.")
    assert status == 429 and "Çok fazla" in body["detail"]
    assert client.get("/api/assistant/bootstrap").status_code == 200    # yalnız sohbet ucu sınırlı
    _login(client, db_session, "rl_b", "Staff")                          # aynı IP (testclient), başka kullanıcı
    assert _chat(client, "B'nin sorusu")[0] == 200
    _login(client, db_session, "rl_a", "Staff")
    assert _chat(client, "A yine")[0] == 429


# ─── Bootstrap ───────────────────────────────────────────────────────────────

def test_bootstrap_shape_by_role(authed_client, db_session):
    b = authed_client.get("/api/assistant/bootstrap").json()
    assert b["enabled"] is True and b["reason"] is None and b["code"] is None
    assert b["model"] == "claude-opus-5-5" and b["model_label"] == "Claude Opus 5.5"
    assert b["daily_cap_usd"] == 3.0 and b["daily_spent_usd"] == 0 and b["daily_remaining_usd"] == 3.0
    assert b["max_message_chars"] == 2000
    assert b["user"]["role"] == "SuperAdmin" and b["user"]["name"] and b["user"]["role_label"] == "Süper Yönetici"
    assert b["can_see_usage"] is True and b["can_edit_settings"] is True
    assert b["month"] == {"spent_usd": 0, "cap_usd": 100.0, "pct": 0} and b["warning"] is None

    me = db_session.query(User).filter_by(username="dogukan").one()
    _usage_row(db_session, me.id, 85.0)
    b = authed_client.get("/api/assistant/bootstrap").json()
    assert b["month"]["pct"] == 85 and "%85" in b["warning"]

    _login(authed_client, db_session, "boot_tech", "LabTech")
    b = authed_client.get("/api/assistant/bootstrap").json()
    assert b["can_see_usage"] is False and b["can_edit_settings"] is False and "month" not in b
    assert b["user"]["role"] == "LabTech" and b["warning"] is None
    # Manager finans rolü: aylık durumu görür, ayar değiştiremez
    _login(authed_client, db_session, "boot_mgr", "Manager")
    b = authed_client.get("/api/assistant/bootstrap").json()
    assert b["can_see_usage"] is True and b["can_edit_settings"] is False and b["month"]["spent_usd"] == 85.0


# ─── Yönetim ─────────────────────────────────────────────────────────────────

def test_admin_usage_roles_and_shape(client, db_session, monkeypatch):
    db = db_session
    db.add(Item(name="JOJOBA YAĞI", category="Hammadde", unit="kg", current_stock=1, domain="cosmetics"))
    db.commit()
    _install(monkeypatch, [
        message(tool_use_block("t1", "find_item", {"q": "jojoba", "category": None}), stop_reason="tool_use",
                usage_obj=usage(creation=11000)),
        message(text_block("Var."), usage_obj=usage(read=11000)),
        message(text_block("Kısa."), stop_reason="max_tokens"),
    ])
    lab = _login(client, db_session, "usage_lab", "LabLead")
    _chat(client, "Jojoba var mı?")
    _chat(client, "Kısa soru")
    assert client.get("/api/assistant/admin/usage").status_code == 403
    assert client.put("/api/assistant/admin/settings", json={"effort": "high"}, headers=_H).status_code == 403

    _login(client, db_session, "usage_mgr", "Manager")
    r = client.get("/api/assistant/admin/usage")
    assert r.status_code == 200
    u = r.json()
    assert len(u["month"]) == 7 and u["cap_usd"] == 100.0 and u["spent_usd"] > 0 and u["pct"] == 0
    assert u["questions_count"] == 2 and u["calls_count"] == 3
    assert u["by_user"] == [{"user_id": lab.id, "name": "Usage Lab", "questions": 2, "calls": 3,
                             "cost_usd": u["spent_usd"]}]
    assert len(u["by_day"]) == 1 and u["by_day"][0]["questions"] == 2 and u["by_day"][0]["calls"] == 3
    assert u["top_tools"] == [{"name": "find_item", "calls": 1, "errors": 0}]
    assert u["cache"]["read_tokens"] == 11000 and u["cache"]["creation_tokens"] == 11000
    assert u["cache"]["uncached_input_tokens"] == 600 and u["cache"]["hit_ratio"] == round(11000 / 22600, 3)
    assert u["stop_reasons"] == {"tool_use": 1, "end_turn": 1, "max_tokens": 1}
    assert [q["preview"] for q in u["questions"]] == ["Kısa soru", "Jojoba var mı?"]      # yeni → eski
    assert u["questions"][1]["calls"] == 2 and u["questions"][1]["tools"] == ["find_item"]
    assert u["questions"][1]["user"] == "Usage Lab" and u["questions"][1]["stop_reason"] == "end_turn"
    assert u["questions"][0]["stop_reason"] == "max_tokens" and u["questions"][0]["conversation_id"]
    assert u["settings"]["model"] == "claude-opus-5-5" and {m["id"] for m in u["models"]} == {
        "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"}
    # başka ay boş; bozuk ay 400
    assert client.get("/api/assistant/admin/usage?month=2020-01").json()["calls_count"] == 0
    assert client.get("/api/assistant/admin/usage?month=2026-13").status_code == 400
    assert client.get("/api/assistant/admin/usage?month=abc").status_code == 400


def test_admin_settings_superadmin_only_audit_and_validation(client, db_session):
    _login(client, db_session, "set_mgr", "Manager")
    r = client.put("/api/assistant/admin/settings", json={"model": "claude-sonnet-5-5"}, headers=_H)
    assert r.status_code == 403 and "Süper Yönetici" in r.json()["detail"]

    _login(client, db_session, "dogukan", "SuperAdmin")
    r = client.put("/api/assistant/admin/settings",
                   json={"model": "claude-sonnet-5-5", "effort": "medium", "monthly_cap_usd": 50,
                         "max_turns": 12}, headers=_H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["settings"]["model"] == "claude-sonnet-5-5" and body["settings"]["effort"] == "medium"
    assert body["settings"]["monthly_cap_usd"] == 50.0 and body["settings"]["enabled"] is True
    assert body["changed"] == ["effort", "model", "monthly_cap_usd"]        # max_turns zaten 12
    vals = {r.key: r.value for r in db_session.query(AppSetting).filter(AppSetting.key.like("assistant.%"))}
    assert vals["assistant.model"] == "claude-sonnet-5-5" and vals["assistant.monthly_cap_usd"] == "50.0"
    audit = db_session.query(AdminAuditLog).filter_by(action="assistant.settings.update").one()
    details = json.loads(audit.details)
    assert audit.actor_name == "Doğukan Yalçınkaya" or audit.actor_name == "dogukan"
    assert audit.target_type == "assistant" and details["old"]["model"] == "claude-opus-5-5"
    assert details["new"] == {"model": "claude-sonnet-5-5", "effort": "medium", "monthly_cap_usd": 50.0}
    assert client.get("/api/assistant/bootstrap").json()["model_label"] == "Claude Sonnet 5.5"

    r = client.put("/api/assistant/admin/settings", json={"model": "gpt-9"}, headers=_H)
    assert r.status_code == 400 and "fiyat tablosunda yok" in r.json()["detail"]
    r = client.put("/api/assistant/admin/settings", json={"max_turns": 0}, headers=_H)
    assert r.status_code == 400 and "aralığında" in r.json()["detail"]
    assert client.put("/api/assistant/admin/settings", json={}, headers=_H).status_code == 400
    assert client.put("/api/assistant/admin/settings", json={"effort": "ultra"}, headers=_H).status_code == 400
    assert db_session.query(AdminAuditLog).filter_by(action="assistant.settings.update").count() == 1
    # kapatma → sohbet 409, geri açma tek PUT
    assert client.put("/api/assistant/admin/settings", json={"enabled": False}, headers=_H).status_code == 200
    assert client.post(CHAT, json={"message": "x"}, headers=_H).status_code == 409
    assert client.put("/api/assistant/admin/settings", json={"enabled": True}, headers=_H).status_code == 200
