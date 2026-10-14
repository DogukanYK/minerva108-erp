"""
Trendyol satış artırma — yapay zekâ hattı (core/trendyol_ai.py) + uçları.
Sahte Claude (tests/assistant_fakes.py) ve sahte Trendyol — AĞ YOK.

Kilitlenenler:
  • İstek biçimi: yapılandırılmış çıktı `output_config.format` (json_schema),
    araç/tool_choice YOK (Opus 5.5 zorunlu tool_choice'u 400'ler), `thinking`
    gönderilmez, sistem bloğu ttl=1h önbellekli, fallbacks "default".
  • Claude'a giden metinde IMS stoğu/maliyeti ve satış fiyatı YOK; bilgi notu var.
  • Kod denetimi: yasak iddia, başlıkta promosyon, uzunluk, dış iletişim,
    kanıtsız iddia → BİR kez yeniden istenir → yine ihlalse `flagged`;
    izinsiz özellik değeri düşürülür (elle kontrol).
  • Aynı girdi özetinde yeni çağrı yok; not değişince yeni çağrı; force üretir.
  • Bütçe asistanla ortak: aylık/günlük tavan 402, anahtar yok 503; asistanın
    "kapalı" anahtarı burayı kapatmaz; her çağrı `assistant_usage`'a
    (domain 'trendyol', önizleme `trendyol:liste:<barkod>`).
  • Toplu üretim tavanda ve kapanış bayrağında durur.
  • Uçlar: 202 + iş yoklama; RBAC (manage); not audit'li; soru taslağı yalnız
    bekleyen soruya.
"""
import asyncio
import json
import time
from datetime import datetime, timedelta

import bcrypt
import pytest

from core import trendyol as T
from core import trendyol_ai as AI
from core import trendyol_boost as B
from database import (AdminAuditLog, AppSetting, AssistantUsage, Item, TrendyolCategoryAttr,
                      TrendyolListingSuggestion, TrendyolProduct, TrendyolQuestion, User)
from tests.assistant_fakes import FakeClient, bad_request, message, text_block, usage
from tests.trendyol_fakes import FakeApi, trendyol_stop_reset  # noqa: F401 (autouse)

_H = {"Origin": "http://testserver"}
BC = "8680000000011"
NOW = datetime.utcnow().replace(microsecond=0)     # senkron gibi: aynı çekimdeki ürünler aynı last_seen_at'i taşır
NOTE = "Kırmızı yonca özü ve aloe vera içerir. Kuru ve hassas ciltler için. Sabah-akşam temiz cilde sürülür."


@pytest.fixture(autouse=True)
def ai_state(monkeypatch):
    """Süreç içi iş kaydı / toplu durum / fallback bayrağı testler arası taşınmasın."""
    AI._JOBS.clear()
    AI._BATCH.update(running=False, total=0, done=0, generated=0, cached=0, failed=0, current=None,
                     stopped=None, errors=[], started_at=None, finished_at=None, actor=None)
    monkeypatch.setattr(AI, "_NO_FALLBACK", False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    yield
    AI._JOBS.clear()


def _user(db, username="mgr", role="Manager"):
    u = db.query(User).filter(User.username == username).first()
    if u is None:
        u = User(username=username, full_name=username.title(), role=role, is_active=True,
                 password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode())
        db.add(u)
        db.commit()
    return u


def _login(client, db, username="mgr", role="Manager", permissions=None):
    u = _user(db, username, role)
    if permissions is not None:
        u.permissions = json.dumps(permissions)
        db.commit()
    client.post("/api/logout", headers=_H)
    r = client.post("/api/login", json={"username": username, "password": "minerva123"}, headers=_H)
    assert r.status_code == 200, r.text
    return u


def _seed(db, barcode=BC, note=NOTE, title="Kırmızı Yonca Krem 50 ML", now=None):
    now = now or NOW
    attrs = [{"id": 47, "name": "Hacim", "values": ["50 ml"], "value_ids": [501]}]
    p = TrendyolProduct(barcode=barcode, title=title, brand="Minerva 108", category="Yüz Kremi", category_id=2,
                        description="<p>Nemlendirici yüz kremi.</p>", attributes_json=json.dumps(attrs),
                        image_count=3, delivery_duration=2, on_sale=True, archived=False, locked=False,
                        blacklisted=False, quantity=4, sale_price=315.0, list_price=350.0,
                        product_main_id="PM-" + barcode[-4:], content_hash="h-" + barcode,
                        first_seen_at=now, last_seen_at=now, synced_at=now)
    db.add(p)
    if db.query(TrendyolCategoryAttr).filter(TrendyolCategoryAttr.category_id == 2).count() == 0:
        for aid, name, req, slicer, custom, values in (
                (47, "Hacim", True, False, False, [{"id": 501, "name": "50 ml"}]),
                (338, "Cilt Tipi", True, True, False, [{"id": 9001, "name": "Kuru Cilt"},
                                                         {"id": 9002, "name": "Yağlı Cilt"}]),
                (60, "Menşei", False, False, False, [])):
            db.add(TrendyolCategoryAttr(category_id=2, category_name="Yüz Kremi", attribute_id=aid, name=name,
                                        required=req, slicer=slicer, varianter=False, allow_custom=custom,
                                        allow_multiple=False, values_json=json.dumps(values, ensure_ascii=False),
                                        values_fetched_at=now, fetched_at=now))
    # IMS kartı — stok/maliyet değerleri Claude'a GİTMEMELİ
    db.add(Item(name="Minerva Kırmızı Yonca Krem 50 ml", category="Bitmiş Ürün", unit="adet", barcode=barcode,
                current_stock=987.0, cost_price=43.21, domain="cosmetics", is_active=True))
    db.commit()
    if note is not None:
        B.save_note(db, barcode, note, "Test")
    return p


DESC_OUT = ("Kırmızı yonca özü ve aloe vera ile hazırlanan bu nemlendirici yüz kremi, kuru ve hassas ciltlerin "
            "günlük bakımı için tasarlandı.\n\nÖne çıkan özellikler\n- Hafif doku, hızlı emilim\n- Gün boyu nem "
            "desteğine yardımcı olur\n\nKullanım\nSabah ve akşam temizlenmiş cilde yeterli miktarda sürün.")


def listing_json(**over):
    kw = lambda terms, use: [{"term": t, "use_in": use} for t in terms]  # noqa: E731
    d = {
        "keywords": {
            "primary": kw(["yüz kremi", "nemlendirici krem", "kırmızı yonca krem"], ["baslik", "aciklama"]),
            "secondary": kw(["hassas cilt kremi", "kuru cilt nemlendirici", "hafif yüz kremi", "aloe vera krem",
                             "günlük yüz kremi"], ["aciklama"]),
            "long_tail": kw(["kuru ciltler için nemlendirici yüz kremi", "hassas ciltler için yüz kremi",
                             "hafif dokulu günlük yüz kremi", "kırmızı yonca özlü yüz kremi",
                             "aloe veralı nemlendirici krem"], ["aciklama", "ozellik"]),
        },
        "title": "Minerva 108 Kırmızı Yonca Özlü Nemlendirici Yüz Kremi Kuru ve Hassas Ciltler 50 ml",
        "title_reason": "Marka, ürün tipi, içerik ve hacim bir arada.",
        "description": DESC_OUT,
        "attributes": [
            {"attribute_id": 338, "name": "Cilt Tipi", "value_id": 9001, "value": "kuru", "manual_check": False,
             "reason": "Bilgi notunda kuru cilt yazıyor."},
            {"attribute_id": 60, "name": "Menşei", "value_id": None, "value": "", "manual_check": True,
             "reason": "Veride menşei yok."},
        ],
        "warnings": ["Bilgi notuna hacim dışında kullanım sıklığı eklenebilir."],
    }
    d.update(over)
    return json.dumps(d, ensure_ascii=False)


def msg(text, **kw):
    return message(text_block(text), **kw)


def _fake(monkeypatch, script):
    fake = FakeClient(script)
    monkeypatch.setattr("core.assistant.client.get_client", lambda: fake)
    return fake


def _wait(client, url, pred, timeout=15.0):
    t0, d = time.time(), None
    while time.time() - t0 < timeout:
        d = client.get(url).json()
        if pred(d):
            return d
        time.sleep(0.05)
    raise AssertionError(f"zaman aşımı: {d}")


def _gen(barcode=BC, uid=None, force=False, client=None):
    return asyncio.run(AI.generate_listing(barcode, user_id=uid, actor="Test", force=force, client=client,
                                           api_factory=lambda: FakeApi()))


# ─── İstek biçimi + veri sınırı ──────────────────────────────────────────────

def test_request_shape_and_data_boundary(db_session):
    u = _user(db_session)
    _seed(db_session)
    fake = FakeClient([msg(listing_json())])
    res = _gen(uid=u.id, client=fake)
    assert res["cached"] is False and res["flagged"] is False
    p = fake.calls[0]
    assert p["model"] == "claude-opus-5-5"
    assert p["output_config"]["format"]["type"] == "json_schema"
    assert p["output_config"]["effort"] == AI.LISTING_EFFORT
    assert "tools" not in p and "tool_choice" not in p and "thinking" not in p
    assert p["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert p["fallbacks"] == "default" and p["betas"] == AI.FALLBACK_BETAS
    text = p["messages"][0]["content"]
    assert NOTE in text and "Kırmızı Yonca Krem 50 ML" in text and "<eksik_ozellikler>" in text
    assert "9001=Kuru Cilt" in text and "Minerva Kırmızı Yonca Krem 50 ml" in text   # izinli değer + IMS adı
    for secret in ("987", "43.21", "43,21", "315", "350"):                              # stok, maliyet, fiyat
        assert secret not in text


def test_schema_has_no_unsupported_keywords():
    def walk(node):
        if isinstance(node, dict):
            assert not ({"minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems"} & set(node))
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(AI.LISTING_SCHEMA)
    walk(AI.QNA_SCHEMA)


# ─── Denetim ─────────────────────────────────────────────────────────────────

def test_validate_listing_clean_and_canonicalises(db_session):
    _seed(db_session)
    ctx = AI.build_listing_context(db_session, BC)
    clean, hard, soft = AI.validate_listing(json.loads(listing_json()), ctx)
    assert hard == []
    cilt = next(a for a in clean["attributes"] if a["attribute_id"] == 338)
    assert cilt["value"] == "Kuru Cilt" and cilt["value_id"] == 9001 and cilt["manual_check"] is False
    mensei = next(a for a in clean["attributes"] if a["attribute_id"] == 60)
    assert mensei["manual_check"] is True and mensei["value_id"] is None
    assert len(clean["keywords"]["primary"]) == 3


def test_validate_listing_flags_violations(db_session):
    _seed(db_session)
    ctx = AI.build_listing_context(db_session, BC)
    bad = json.loads(listing_json(
        title="MİNERVA 108 YÜZ KREMİ ÜCRETSİZ KARGO FIRSAT " + "X" * 130,
        description=DESC_OUT + "\nAkneyi tedavi eder. Dermatolojik olarak test edilmiştir. www.minerva108.com",
        attributes=[{"attribute_id": 338, "name": "Cilt Tipi", "value_id": 12345, "value": "Karma",
                     "manual_check": False, "reason": ""},
                    {"attribute_id": 999, "name": "Uydurma", "value_id": None, "value": "x",
                     "manual_check": True, "reason": ""}]))
    bad["keywords"]["primary"].append({"term": "indirimli krem", "use_in": ["baslik"]})
    clean, hard, soft = AI.validate_listing(bad, ctx)
    text = " ".join(hard)
    for frag in ("en fazla 150", "promosyon dili", "büyük harf", "Yasak iddia", "Dış iletişim", "Kanıtsız iddia"):
        assert frag in text, frag
    assert any("izinli listede olmayan" in s for s in soft)
    assert any("Uydurma" in s for s in soft) and any("indirimli krem" in s for s in soft)
    cilt = next(a for a in clean["attributes"] if a["attribute_id"] == 338)
    assert cilt["value_id"] is None and cilt["manual_check"] is True
    assert all(a["attribute_id"] != 999 for a in clean["attributes"])


def test_evidence_in_note_allows_claim(db_session):
    _seed(db_session, note=NOTE + " Dermatolojik olarak test edildi (rapor no 2025-17).")
    ctx = AI.build_listing_context(db_session, BC)
    out = json.loads(listing_json(description=DESC_OUT + "\nDermatolojik olarak test edilmiştir."))
    assert AI.validate_listing(out, ctx)[1] == []


def test_response_text_after_fallback_block():
    from tests.assistant_fakes import FakeBlock
    blocks = [FakeBlock(type="thinking", thinking=""), text_block('{"yarim": '),
              FakeBlock(type="fallback"), text_block('{"tam": 1}')]
    assert AI.response_text(blocks) == '{"tam": 1}'


# ─── Yeniden deneme + işaretleme + defter ────────────────────────────────────

def test_retry_once_then_flagged_and_usage_recorded(db_session):
    u = _user(db_session)
    _seed(db_session)
    bad = listing_json(description=DESC_OUT + "\nSivilceyi tedavi eder.")
    fake = FakeClient([msg(bad), msg(bad)])
    res = _gen(uid=u.id, client=fake)
    assert res["flagged"] is True and len(fake.calls) == 2
    assert "<onceki_deneme_sorunlari>" in fake.calls[1]["messages"][0]["content"]
    assert "tedavi" in fake.calls[1]["messages"][0]["content"]
    s = db_session.query(TrendyolListingSuggestion).one()
    assert s.flagged is True and "Yasak iddia" in s.flags_json
    rows = db_session.query(AssistantUsage).order_by(AssistantUsage.id).all()
    assert [r.round_no for r in rows] == [1, 2]
    assert all(r.domain == "trendyol" and r.question_preview == f"trendyol:liste:{BC}"
               and r.conversation_id is None for r in rows)
    assert s.cost_usd == pytest.approx(sum(r.cost_usd for r in rows))


def test_retry_fixes_violation(db_session):
    u = _user(db_session)
    _seed(db_session)
    fake = FakeClient([msg(listing_json(title="Yüz Kremi %100 garanti etkili 50 ml Minerva 108 nemlendirici")),
                       msg(listing_json())])
    res = _gen(uid=u.id, client=fake)
    assert res["flagged"] is False and len(fake.calls) == 2


def test_refusal_and_bad_json_are_retried(db_session):
    u = _user(db_session)
    _seed(db_session)
    fake = FakeClient([message(text_block(""), stop_reason="refusal"), msg(listing_json())])
    assert _gen(uid=u.id, client=fake)["flagged"] is False
    assert db_session.query(AssistantUsage).count() == 2
    fake2 = FakeClient([msg("bu json değil"), msg("{bozuk")])
    with pytest.raises(AI.AIError) as e:
        _gen(uid=u.id, client=fake2, force=True)
    assert e.value.code == "no_output"
    assert db_session.query(AssistantUsage).count() == 4          # başarısız çağrılar da deftere


def test_fallback_rejected_switches_off_once(db_session):
    u = _user(db_session)
    _seed(db_session)
    fake = FakeClient([bad_request("fallbacks: Extra inputs are not permitted"), msg(listing_json())])
    assert _gen(uid=u.id, client=fake)["flagged"] is False
    assert "fallbacks" in fake.calls[0] and "fallbacks" not in fake.calls[1] and "betas" not in fake.calls[1]
    assert AI._NO_FALLBACK is True


# ─── Önbellek ────────────────────────────────────────────────────────────────

def test_same_input_hash_is_cached_note_change_regenerates(db_session):
    u = _user(db_session)
    _seed(db_session)
    fake = FakeClient([msg(listing_json()), msg(listing_json()), msg(listing_json())])
    assert _gen(uid=u.id, client=fake)["cached"] is False
    assert _gen(uid=u.id, client=fake)["cached"] is True and len(fake.calls) == 1
    view = B.listing_view(db_session)["rows"][0]
    assert view["suggestion_at"] and view["suggestion_stale"] is False
    B.save_note(db_session, BC, NOTE + " Parfümsüz.", "Test")
    assert B.listing_view(db_session)["rows"][0]["suggestion_stale"] is True
    assert _gen(uid=u.id, client=fake)["cached"] is False and len(fake.calls) == 2
    assert _gen(uid=u.id, client=fake, force=True)["cached"] is False and len(fake.calls) == 3


# ─── Bütçe ───────────────────────────────────────────────────────────────────

def _spend(db, uid, usd, when=None):
    db.add(AssistantUsage(user_id=uid, domain="cosmetics", model="claude-opus-5-5", round_no=1,
                          cost_usd=usd, created_at=when or datetime.utcnow()))
    db.commit()


def test_monthly_and_daily_caps_and_missing_key(db_session, monkeypatch):
    u = _user(db_session)
    other = _user(db_session, "baska")
    _seed(db_session)
    fake = FakeClient([msg(listing_json())])
    _spend(db_session, u.id, 3.5)                                  # kişi başı günlük 3 $
    with pytest.raises(AI.AIError) as e:
        _gen(uid=u.id, client=fake)
    assert e.value.status == 402 and e.value.code == "daily_cap"
    _spend(db_session, other.id, 120.0)                             # ortak aylık 100 $
    with pytest.raises(AI.AIError) as e:
        _gen(uid=other.id, client=fake)
    assert e.value.code == "monthly_cap" and fake.calls == []
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(AI.AIError) as e:
        _gen(uid=other.id, client=fake)
    assert e.value.status == 503 and e.value.code == "not_configured"


def test_assistant_disabled_switch_does_not_block(db_session):
    u = _user(db_session)
    _seed(db_session)
    db_session.add(AppSetting(key="assistant.enabled", value="0"))
    db_session.commit()
    assert _gen(uid=u.id, client=FakeClient([msg(listing_json())]))["cached"] is False


def test_model_setting_and_spend_summary(db_session):
    u = _user(db_session)
    _seed(db_session)
    db_session.add(AppSetting(key=AI.MODEL_KEY, value="claude-sonnet-5-5"))
    db_session.commit()
    fake = FakeClient([msg(listing_json(), model="claude-sonnet-5-5")])
    _gen(uid=u.id, client=fake)
    assert fake.calls[0]["model"] == "claude-sonnet-5-5"
    db_session.query(AppSetting).filter(AppSetting.key == AI.MODEL_KEY).update({"value": "gpt-x"})
    db_session.commit()
    assert AI.ai_model(db_session) == "claude-opus-5-5"            # fiyat tablosunda yoksa varsayılan
    s = AI.spend_summary(db_session, u.id)
    assert s["trendyol_month_usd"] >= 0 and s["month_cap_usd"] == 100.0 and s["configured"] is True


# ─── Toplu üretim ────────────────────────────────────────────────────────────

def test_batch_stops_at_cap_and_on_shutdown(db_session):
    u = _user(db_session)
    _seed(db_session, barcode="8680000000011")
    _seed(db_session, barcode="8680000000028", title="Aloe Krem 100 ML")
    db_session.add(AppSetting(key="assistant.daily_user_cap_usd", value="0.001"))
    db_session.commit()
    fake = FakeClient([msg(listing_json()), msg(listing_json())])
    snap = asyncio.run(AI.run_batch(["8680000000011", "8680000000028"], user_id=u.id, actor="Test",
                                    client=fake, api_factory=lambda: FakeApi()))
    assert snap["generated"] == 1 and snap["done"] == 1 and "Günlük" in (snap["stopped"] or "")
    assert snap["running"] is False and len(fake.calls) == 1
    T.request_stop()
    snap = asyncio.run(AI.run_batch(["8680000000028"], user_id=u.id, actor="Test", client=fake))
    assert "yeniden başlatılıyor" in snap["stopped"] and len(fake.calls) == 1


def test_batch_candidates_skip_current_suggestions(db_session):
    u = _user(db_session)
    _seed(db_session, barcode="8680000000011")
    _seed(db_session, barcode="8680000000028", title="Aloe Krem 100 ML")
    _gen("8680000000011", uid=u.id, client=FakeClient([msg(listing_json())]))
    assert AI.batch_candidates(db_session, 10) == ["8680000000028"]


# ─── Uçlar ───────────────────────────────────────────────────────────────────

def test_suggest_endpoint_runs_job_and_listing(client, db_session, monkeypatch):
    _login(client, db_session)
    _seed(db_session)
    _fake(monkeypatch, [msg(listing_json())])
    monkeypatch.setattr(T, "make_api", lambda: FakeApi())
    r = client.post(f"/api/trendyol/listing/{BC}/suggest", json={"force": False}, headers=_H)
    assert r.status_code == 202, r.text
    d = _wait(client, f"/api/trendyol/listing/{BC}", lambda d: d["job"] and d["job"]["state"] != "running")
    assert d["job"]["state"] == "done" and d["suggestion"]["payload"]["title"].startswith("Minerva 108")
    assert d["suggestion"]["current"] is True and "_meta" not in d["suggestion"]["payload"]
    lst = client.get("/api/trendyol/listing").json()
    assert lst["rows"][0]["suggestion_at"] and lst["ai"]["trendyol_month_usd"] >= 0 and lst["can_manage"] is True
    assert client.get("/api/trendyol/listing/YOK-BARKOD").status_code == 404


def test_suggest_endpoint_errors_and_rbac(client, db_session, monkeypatch):
    _seed(db_session)
    _login(client, db_session, "lab", role="LabTech")
    assert client.get("/api/trendyol/listing").status_code == 403
    assert client.post(f"/api/trendyol/listing/{BC}/suggest", json={}, headers=_H).status_code == 403
    _login(client, db_session, "goren", role="LabLead",
           permissions={"trendyol": {"view": True, "profit": False, "manage": False}})
    assert client.get("/api/trendyol/listing").status_code == 200
    assert client.post(f"/api/trendyol/listing/{BC}/suggest", json={}, headers=_H).status_code == 403
    assert client.put(f"/api/trendyol/listing/{BC}/note", json={"note": "x"}, headers=_H).status_code == 403
    _login(client, db_session, "bayi", role="Distributor")
    assert client.get("/api/trendyol/listing").status_code == 403
    _login(client, db_session)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    r = client.post(f"/api/trendyol/listing/{BC}/suggest", json={}, headers=_H)
    assert r.status_code == 503 and r.json()["code"] == "not_configured"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert client.post("/api/trendyol/listing/YOK/suggest", json={}, headers=_H).status_code == 404


def test_note_put_is_audited(client, db_session):
    _login(client, db_session)
    _seed(db_session, note=None)
    r = client.put(f"/api/trendyol/listing/{BC}/note", json={"note": "  Shea yağı içerir.  "}, headers=_H)
    assert r.status_code == 200 and r.json()["note"]["text"] == "Shea yağı içerir."
    log = db_session.query(AdminAuditLog).filter(AdminAuditLog.action == "trendyol.note").one()
    assert log.target_name == BC and "Shea" in log.details
    assert client.get(f"/api/trendyol/listing/{BC}").json()["note"]["text"] == "Shea yağı içerir."
    assert client.put(f"/api/trendyol/listing/{BC}/note", json={"note": "x" * 2001}, headers=_H).status_code == 422
    assert client.put("/api/trendyol/listing/YOK/note", json={"note": "x"}, headers=_H).status_code == 404


def test_estimate_and_batch_endpoint(client, db_session, monkeypatch):
    _login(client, db_session)
    _seed(db_session, barcode="8680000000011")
    _seed(db_session, barcode="8680000000028", title="Aloe Krem 100 ML")
    _fake(monkeypatch, [msg(listing_json()), msg(listing_json())])
    monkeypatch.setattr(T, "make_api", lambda: FakeApi())
    e = client.get("/api/trendyol/listing/estimate?limit=10").json()
    assert e["count"] == 2 and e["per_item_usd"] == AI.EST_DEFAULT_USD and e["fits"] is True
    r = client.post("/api/trendyol/listing/suggest-batch", json={"limit": 10}, headers=_H)
    assert r.status_code == 202, r.text
    d = _wait(client, "/api/trendyol/listing", lambda d: not d["batch"]["running"])
    assert d["batch"]["generated"] == 2 and d["batch"]["failed"] == 0
    assert db_session.query(AdminAuditLog).filter(AdminAuditLog.action == "trendyol.ai_batch").count() == 1
    r = client.post("/api/trendyol/listing/suggest-batch", json={"limit": 10}, headers=_H)
    assert r.status_code == 200 and r.json()["count"] == 0              # hepsinin güncel önerisi var


# ─── Müşteri soruları ────────────────────────────────────────────────────────

def _question(db, qid=5001, status=T.WAITING, text="Bu krem yağlı cilde uygun mu?", barcode=BC):
    db.add(TrendyolQuestion(question_id=qid, product_main_id="PM-0011", barcode=barcode,
                            product_name="Kırmızı Yonca Krem 50 ML", text=text, status=status,
                            asked_at=datetime.utcnow() - timedelta(hours=5),
                            web_url=f"https://www.trendyol.com/soru/{qid}", first_seen_at=datetime.utcnow()))
    db.commit()


def test_answer_draft_retry_and_save(db_session):
    u = _user(db_session)
    _seed(db_session)
    _question(db_session)
    fake = FakeClient([msg(json.dumps({"answer": "Merhaba, bizi 0532 123 45 67'den arayın.", "warnings": []})),
                       msg(json.dumps({"answer": "Merhaba, ürün kuru ve hassas ciltler için hazırlanmıştır; yağlı "
                                                 "ciltte de hafif dokusuyla kullanılabilir.",
                                       "warnings": ["Yağlı cilt için ayrı test bilgisi yok."]}))])
    asyncio.run(AI.generate_answer(5001, user_id=u.id, actor="Test", client=fake))
    p = fake.calls[0]
    assert p["output_config"]["effort"] == AI.QNA_EFFORT and "<musteri_sorusu>" in p["messages"][0]["content"]
    assert "Dış iletişim" in fake.calls[1]["messages"][0]["content"]
    db_session.expire_all()
    q = db_session.query(TrendyolQuestion).filter(TrendyolQuestion.question_id == 5001).one()
    assert q.draft_text.startswith("Merhaba, ürün") and q.draft_by == "Test"
    v = B.question_view(q)
    assert v["draft_flagged"] is False and v["draft_warnings"] == ["Yağlı cilt için ayrı test bilgisi yok."]
    assert db_session.query(AssistantUsage).filter(
        AssistantUsage.question_preview == "trendyol:soru:5001").count() == 2


def test_answer_too_long_is_flagged(db_session):
    u = _user(db_session)
    _seed(db_session)
    _question(db_session)
    long = json.dumps({"answer": "Merhaba, " + "uzun cevap " * 80, "warnings": []})
    asyncio.run(AI.generate_answer(5001, user_id=u.id, actor="Test", client=FakeClient([msg(long), msg(long)])))
    db_session.expire_all()
    v = B.question_view(db_session.query(TrendyolQuestion).one())
    assert v["draft_flagged"] is True and any("en fazla 600" in f for f in v["draft_flags"])


def test_questions_endpoints(client, db_session, monkeypatch):
    _login(client, db_session)
    _seed(db_session)
    _question(db_session, 5001)
    _question(db_session, 5002, status="ANSWERED", text="Kargo ne zaman?")
    d = client.get("/api/trendyol/questions").json()
    assert [q["question_id"] for q in d["questions"]] == [5001, 5002]          # bekleyen üstte
    assert d["questions"][0]["waiting_hours"] >= 4.9 and d["counts"][T.WAITING] == 1
    assert client.post("/api/trendyol/questions/5002/draft", headers=_H).status_code == 400
    assert client.post("/api/trendyol/questions/9999/draft", headers=_H).status_code == 404
    _fake(monkeypatch, [msg(json.dumps({"answer": "Merhaba, ürün kuru ve hassas ciltler için uygundur.",
                                        "warnings": []}))])
    assert client.post("/api/trendyol/questions/5001/draft", headers=_H).status_code == 202
    d = _wait(client, "/api/trendyol/questions?status=" + T.WAITING,
              lambda d: d["questions"][0]["job"] and d["questions"][0]["job"]["state"] != "running")
    assert d["questions"][0]["draft_text"].startswith("Merhaba") and d["questions"][0]["job"]["state"] == "done"
    _login(client, db_session, "goren", role="LabLead",
           permissions={"trendyol": {"view": True, "profit": False, "manage": False}})
    assert client.get("/api/trendyol/questions").status_code == 200
    assert client.post("/api/trendyol/questions/5001/draft", headers=_H).status_code == 403


def test_job_claim_blocks_double_click():
    AI.claim(AI.listing_key("X1"))
    with pytest.raises(AI.AIError) as e:
        AI.claim(AI.listing_key("X1"))
    assert e.value.status == 409
    AI._finish(AI.listing_key("X1"), "done")
    AI.claim(AI.listing_key("X1"))                                  # bittikten sonra yeniden alınabilir
