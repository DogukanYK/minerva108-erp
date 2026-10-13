"""Minerva Asistan — üst çubuk araması (core/assistant/search.py, GET /api/assistant/search).

Kilitler:
  • YAPAY ZEKÂ ÇAĞRISI YOK: arama SDK istemcisine dokunmaz, `assistant_usage`
    yazmaz, asistan kapalıyken (409) de çalışır.
  • Ürün / reçete / tedarikçi grupları asistanın araç işlevleriyle AYNI izin ve
    panel süzgecinden geçer; lot grubu `inventory.view` + panel; tedarikçi
    satırında iletişim (telefon / e-posta / yetkili) yok.
  • Sayfa listesi `api_main` sayfa route'larının `_user_can` kapısıyla birebir:
    kullanıcıya açamayacağı sayfa önerilmez.
  • Sohbet listesi `limit` (≤100) + `updated_date` (/asistan geçmişi gruplar).
Yalnız izole test DB'si; sentetik adlar.
"""
import ast
import json
import pathlib

import bcrypt
import pytest

from core.assistant import history as H
from core.assistant import runner as R
from core.assistant import search as S
from core.assistant.tools import ToolContext
from database import (AppSetting, AssistantUsage, Inventory, Item, Recipe, RecipeIngredient, Supplier, User)
from tests.assistant_fakes import FakeClient

_H = {"Origin": "http://testserver"}
URL = "/api/assistant/search"
ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    R.reset_cache()
    H.reset_cache()
    yield
    R.reset_cache()
    H.reset_cache()


def _user(db, username, role="LabTech", permissions=None):
    u = db.query(User).filter(User.username == username).first()
    if u is None:
        u = User(username=username, full_name=username.title(), role=role, is_active=True,
                 password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode(),
                 permissions=json.dumps(permissions) if permissions is not None else None)
        db.add(u)
        db.commit()
    return u


def _login(client, db, username, role="LabTech", permissions=None):
    _user(db, username, role, permissions)
    client.post("/api/logout", headers=_H)
    r = client.post("/api/login", json={"username": username, "password": "minerva123"}, headers=_H)
    assert r.status_code == 200, r.text


def _world(db):
    sup = Supplier(name="Arama Kimya Ltd", phone="0532 999 88 77", email="satis@arama.example",
                   contact_person="Gizli Yetkili", domain="cosmetics")
    db.add(sup)
    db.flush()
    raw = Item(name="ARAMA STEARİL ALKOL", sku="ARM-STR", unit="kg", category="Hammadde",
               current_stock=12.5, supplier_id=sup.id, domain="cosmetics", is_active=True)
    cream = Item(name="Arama Krem 50 ml", unit="adet", category="Bitmiş Ürün", current_stock=1250,
                 domain="cosmetics", is_active=True)
    other = Item(name="ARAMA STEARİL SUPPLEMENT", unit="kg", category="Hammadde", current_stock=3,
                 domain="supplement", is_active=True)
    db.add_all([raw, cream, other])
    db.flush()
    db.add_all([
        Inventory(item_id=raw.id, supplier_id=sup.id, lot_number="AR2026-17", quantity=7.5, status="APPROVED",
                  domain="cosmetics"),
        Inventory(item_id=raw.id, supplier_id=sup.id, lot_number="AR2026-18", quantity=5, status="APPROVED",
                  domain="cosmetics"),
        Inventory(item_id=other.id, lot_number="AR2026-99", quantity=3, status="APPROVED", domain="supplement"),
    ])
    rec = Recipe(name="Arama Krem Reçetesi", target_item_id=cream.id, output_quantity=1, output_unit="adet",
                 domain="cosmetics", is_active=True)
    db.add(rec)
    db.flush()
    db.add(RecipeIngredient(recipe_id=rec.id, item_id=raw.id, quantity=0.01, unit="kg"))
    db.commit()
    return {"raw": raw, "cream": cream, "other": other, "sup": sup, "rec": rec}


def _groups(payload):
    return {g["key"]: g["rows"] for g in payload["groups"]}


# ─── Saf parçalar ────────────────────────────────────────────────────────────

def test_tr_num_and_page_score():
    assert S.tr_num(12500.5) == "12.500,5" and S.tr_num(7.0) == "7" and S.tr_num(0.125) == "0,125"
    assert S.tr_num(None) == "—"
    teslimat = next(p for p in S.PAGES if p.href == "/delivery")
    assert S.page_score(teslimat, "tesl", ["tesl"]) == 3                       # ad öneki
    assert S.page_score(teslimat, "kargo", ["kargo"]) == 2                     # anahtar kelime
    q = "kargolanacak ürünler ne durumda"
    from core.supplier_prices import normalize
    assert S.page_score(teslimat, normalize(q), S._tokens(q)) == 1             # soru kelimesi → kargo
    assert S.page_score(teslimat, "recete", ["recete"]) == 0


def test_page_registry_matches_page_route_guards():
    """Her kayıt api_main'deki sayfa route'unun `_user_can` kapısıyla aynı
    (route'u değiştiren burayı da güncellemek zorunda kalsın — kullanıcıya
    açamayacağı sayfa önerilmesin)."""
    tree = ast.parse((ROOT / "api_main.py").read_text(encoding="utf-8"))
    guards = {}
    for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
        perms = [(c.args[1].value, c.args[2].value) for c in ast.walk(fn)
                 if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_user_can"
                 and len(c.args) == 3 and all(isinstance(a, ast.Constant) for a in c.args[1:])]
        for d in fn.decorator_list:
            if isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "get" and d.args \
                    and isinstance(d.args[0], ast.Constant):
                guards[d.args[0].value] = perms
    # Kapı DEĞİL: "/" bayi yönlendirmesi, /trendyol sayfa içi "manage" bayrağı
    guards["/"] = []
    guards["/trendyol"] = [p for p in guards["/trendyol"] if p != ("trendyol", "manage")]
    for page in S.PAGES:
        if page.href.startswith("/asistan"):
            assert guards["/asistan"] == guards["/asistan/{tab}"] == [("assistant", "use")]
            continue
        assert page.href in guards, page.href
        if page.superadmin:                     # /system: rol denetimi (`user.role != "SuperAdmin"`)
            assert guards[page.href] == [] and page.perm is None
            continue
        assert ([page.perm] if page.perm else []) == guards[page.href], page.href


def test_search_groups_scope_and_contact_fields(db_session):
    db = db_session
    w = _world(db)
    ctx = ToolContext.from_user(_user(db, "ara_lab", "LabLead"), "cosmetics", db)
    out = S.search(ctx, db, "arama stearil")
    g = _groups(out)
    assert [r["title"] for r in g["items"]] == ["ARAMA STEARİL ALKOL"]           # supplement kartı yok
    it = g["items"][0]
    assert it["meta"] == "12,5 kg" and it["href"] == f"/items?q=ARAMA%20STEAR%C4%B0L%20ALKOL&id={w['raw'].id}"
    out = S.search(ctx, db, "AR2026")
    lots = _groups(out)["lots"]
    assert [r["title"] for r in lots] == ["AR2026-18", "AR2026-17"]                # en yeni önce, panel süzgeçli
    assert lots[0]["sub"] == "ARAMA STEARİL ALKOL" and lots[0]["href"].startswith("/traceability?lot=AR2026-18&item_id=")
    assert "lots" not in _groups(S.search(ctx, db, "AR 2026"))                     # boşluklu metin lot değildir
    rec = _groups(S.search(ctx, db, "krem reçetesi"))["recipes"]
    assert rec[0]["title"] == "Arama Krem Reçetesi" and rec[0]["meta"] == "1 bileşen"
    sup = _groups(S.search(ctx, db, "arama kimya"))["suppliers"]
    assert sup[0]["title"] == "Arama Kimya Ltd" and sup[0]["href"] == "/suppliers?q=Arama%20Kimya%20Ltd"
    blob = json.dumps(S.search(ctx, db, "arama"), ensure_ascii=False)
    for secret in ("0532 999 88 77", "satis@arama.example", "Gizli Yetkili"):
        assert secret not in blob
    assert S.search(ctx, db, "a") == {"q": "a", "groups": []}
    pages = _groups(S.search(ctx, db, "kargolanacak ürünler ne durumda"))["pages"]
    assert "/delivery" in [x["href"] for x in pages]                              # "kargo…" → Teslimat


def test_search_respects_permissions(db_session):
    db = db_session
    _world(db)
    only_recipes = ToolContext.from_user(
        _user(db, "ara_rec", "Staff", {"recipes": {"view": True}, "assistant": {"use": True}}), "cosmetics", db)
    g = _groups(S.search(only_recipes, db, "arama"))
    assert set(g) <= {"recipes", "pages"} and "recipes" in g                     # ürün/lot/tedarikçi izni yok
    assert {r["href"] for r in g.get("pages", [])} <= {"/", "/recipes", "/drive", "/asistan", "/asistan/belge",
                                                       "/asistan/yaz"}
    assert "lots" not in _groups(S.search(only_recipes, db, "AR2026"))
    staff = ToolContext.from_user(_user(db, "ara_staff", "Staff"), "cosmetics", db)
    hrefs = {p.href for p in S.visible_pages(staff)}
    assert "/system" not in hrefs and "/admin" not in hrefs
    sa = ToolContext.from_user(db.query(User).filter_by(username="dogukan").one(), "cosmetics", db)
    assert "/system" in {p.href for p in S.visible_pages(sa)}


# ─── HTTP ────────────────────────────────────────────────────────────────────

def test_search_endpoint_no_ai_call_and_works_when_assistant_disabled(client, db_session, monkeypatch):
    db = db_session
    _world(db)
    fake = FakeClient([])
    monkeypatch.setattr("core.assistant.client.get_client", lambda: fake)
    _login(client, db, "ara_http", "LabLead")
    r = client.get(URL, params={"q": "arama stearil"})
    assert r.status_code == 200 and _groups(r.json())["items"][0]["title"] == "ARAMA STEARİL ALKOL"
    db.add(AppSetting(key="assistant.enabled", value="0"))
    db.commit()
    assert client.get("/api/assistant/bootstrap").json()["enabled"] is False
    r = client.get(URL, params={"q": "AR2026"})
    assert r.status_code == 200 and _groups(r.json())["lots"]
    assert fake.calls == [] and db.query(AssistantUsage).count() == 0
    client.cookies.set("active_domain", "supplement")
    r = client.get(URL, params={"q": "AR2026"})
    assert [x["title"] for x in _groups(r.json())["lots"]] == ["AR2026-99"]
    assert client.get(URL, params={"q": "x" * 201}).status_code == 422


def test_search_endpoint_auth(client, db_session):
    assert client.get(URL, params={"q": "arama"}).status_code == 401
    _login(client, db_session, "ara_noai", "LabTech", {"items": {"view": True}, "assistant": {"use": False}})
    assert client.get(URL, params={"q": "arama"}).status_code == 403
    _login(client, db_session, "ara_dist", "Distributor")
    assert client.get(URL, params={"q": "arama"}).status_code == 403


def test_conversation_list_limit_and_updated_date(client, db_session):
    db = db_session
    _login(client, db, "ara_hist", "LabTech")
    uid = db.query(User).filter_by(username="ara_hist").one().id
    from core.assistant.tools import PROMPT_HASH
    for i in range(5):
        H.start_conversation(db, uid, "cosmetics", "claude-opus-5-5", PROMPT_HASH)
    db.commit()
    rows = client.get("/api/assistant/conversations", params={"limit": 3}).json()
    assert len(rows) == 3 and all(len(r["updated_date"]) == 10 and r["updated_date"][4] == "-" for r in rows)
    assert len(client.get("/api/assistant/conversations").json()) == 5
    assert client.get("/api/assistant/conversations", params={"limit": 101}).status_code == 422
