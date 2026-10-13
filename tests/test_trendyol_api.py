"""
Trendyol uçları + sayfa + yetki (routers/trendyol.py) — AĞ YOK.

Kilitlenenler:
  • RBAC: LabTech/Distributor kapalı; Manager açık; kâr/maliyet alanları
    (komisyon oranı, kart maliyeti) yalnız trendyol.profit + finans rolü +
    finance.view ile — override'la `profit` verilmiş LabLead de göremez, alan
    yanıtta HİÇ yoktur.
  • Eşleştirme: tek kart → eşleşti; aynı barkodlu iki aktif kart → belirsiz
    (asla tahmin yok); elle eşleme önce gelir; set ürün stoğu min ⌊stok/adet⌋;
    eski barkod yalnız ÖNERİ; pasifleşen kart → "eşleme geçersiz"; panel
    süzgeci kartın domain'inden.
  • Elle eşleme: yalnız aktif "Bitmiş Ürün"; audit'li; manage şart.
  • Senkron ucu: yapılandırma yoksa 400 (yalnız değişken adları), çalışıyorsa
    409, aksi hâlde 202 + arka plan çalıştırması.
  • Probe yalnız SuperAdmin ve kişisel alanlar maskeli.
  • Sayfa: yetkisize yönlendirme; nav bağlantısı yetkiye göre.
  • Yetki backfill'i: override'da finance.view yoksa trendyol.* False.
"""
import json

import bcrypt
import pytest
from fastapi.testclient import TestClient

from core import trendyol as T
from database import AdminAuditLog, AppSetting, Item, TrendyolBarcodeMap, TrendyolProduct, User
from tests.trendyol_fakes import (FakeApi, approved, inventory, line, package, paged, stream,  # noqa: F401
                                  trendyol_stop_reset)

_H = {"Origin": "http://testserver"}


def _login(client, db, username, role="Staff", permissions=None):
    if db.query(User).filter(User.username == username).first() is None:
        db.add(User(username=username, full_name=username.title(), role=role, is_active=True,
                    password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode(),
                    permissions=json.dumps(permissions) if permissions is not None else None))
        db.commit()
    client.post("/api/logout", headers=_H)
    r = client.post("/api/login", json={"username": username, "password": "minerva123"}, headers=_H)
    assert r.status_code == 200, r.text


def _item(db, name, barcode, stock=10.0, cost=40.0, domain="cosmetics", active=True, category="Bitmiş Ürün"):
    it = Item(name=name, category=category, unit="adet", barcode=barcode, current_stock=stock,
              cost_price=cost, domain=domain, is_active=active)
    db.add(it)
    db.commit()
    db.refresh(it)
    return it


def _seed_orders(db, barcode="8680000000011"):
    T.upsert_package(db, T.parse_package(package(lines=[line(barcode=barcode)])))
    db.commit()


def _env(monkeypatch):
    monkeypatch.setenv("TRENDYOL_SELLER_ID", "123456")
    monkeypatch.setenv("TRENDYOL_API_KEY", "k")
    monkeypatch.setenv("TRENDYOL_API_SECRET", "s")


def _no_env(monkeypatch):
    for n in ("TRENDYOL_SELLER_ID", "TRENDYOL_API_KEY", "TRENDYOL_API_SECRET"):
        monkeypatch.delenv(n, raising=False)


# ─── Yetki ───────────────────────────────────────────────────────────────────

def test_labtech_and_distributor_forbidden(client: TestClient, db_session):
    _login(client, db_session, "meltem")
    assert client.get("/api/trendyol/status").status_code == 403
    r = client.get("/trendyol", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"
    _login(client, db_session, "bayi", role="Distributor")
    assert client.get("/api/trendyol/orders").status_code == 403


def test_manager_sees_cost_fields(client: TestClient, db_session):
    _item(db_session, "Minerva Krem", "8680000000011")
    _seed_orders(db_session)
    _login(client, db_session, "isik")
    d = client.get("/api/trendyol/orders").json()
    ln = d["orders"][0]["lines"][0]
    assert ln["commission_rate"] == 15.0
    assert ln["resolution"]["components"][0]["cost_price"] == 40.0
    st = client.get("/api/trendyol/status").json()
    assert st["can_profit"] is True and st["can_manage"] is True


@pytest.mark.parametrize("extra", [{}, {"profit": True, "manage": True}])
def test_non_finance_role_never_gets_cost_fields(client: TestClient, db_session, extra):
    _item(db_session, "Minerva Krem", "8680000000011")
    _seed_orders(db_session)
    perms = {"trendyol": {"view": True, "profit": False, "manage": False, **extra},
             "finance": {"view": True}}
    _login(client, db_session, "lablead_x", role="LabLead", permissions=perms)
    d = client.get("/api/trendyol/orders").json()
    ln = d["orders"][0]["lines"][0]
    assert "commission_rate" not in ln
    assert "cost_price" not in ln["resolution"]["components"][0]
    detail = client.get("/api/trendyol/orders/1065440001").json()
    assert "finance" not in detail and "cargo" not in detail
    assert client.get("/api/trendyol/calibration").status_code == 403


def test_view_only_cannot_manage(client: TestClient, db_session):
    _login(client, db_session, "viewer", role="Manager",
           permissions={"trendyol": {"view": True, "profit": False, "manage": False}})
    assert client.put("/api/trendyol/mappings", json={"barcode": "X", "components": [{"item_id": 1}]},
                      headers=_H).status_code == 403
    assert client.post("/api/trendyol/sync", json={"kind": "all"}, headers=_H).status_code == 403
    assert client.get("/api/trendyol/items?q=a").status_code == 403


# ─── Eşleştirme ──────────────────────────────────────────────────────────────

def test_catalog_matching_states(authed_client: TestClient, db_session, monkeypatch):
    a = _item(db_session, "Minerva Krem", "BC-MATCH", stock=5)
    _item(db_session, "Kopya 1", "BC-AMB")
    _item(db_session, "Kopya 2", "BC-AMB")
    _item(db_session, "Pasif", "BC-PASSIVE", active=False)
    _item(db_session, "Hammadde", "BC-RAW", category="Hammadde")
    new = _item(db_session, "Serenida Tonik", "NEW-GS1", domain="cosmetics")
    sup = _item(db_session, "Geven Kapsül", "BC-SUP", domain="supplement", stock=3)
    monkeypatch.setattr(T, "_ALIASES", {"OLD-UTS": "NEW-GS1"})
    for bc, q in (("BC-MATCH", 9), ("BC-AMB", 1), ("BC-PASSIVE", 1), ("BC-RAW", 1), ("OLD-UTS", 1), ("BC-SUP", 2)):
        db_session.add(TrendyolProduct(barcode=bc, title=bc, quantity=q))
    db_session.commit()
    d = authed_client.get("/api/trendyol/catalog").json()
    rows = {r["barcode"]: r for r in d["rows"]}
    assert rows["BC-MATCH"]["resolution"]["status"] == "matched"
    assert rows["BC-MATCH"]["ims_stock"] == 5 and rows["BC-MATCH"]["diff"] == 4 and rows["BC-MATCH"]["over"]
    assert rows["BC-AMB"]["resolution"]["status"] == "ambiguous" and rows["BC-AMB"]["ims_stock"] is None
    assert len(rows["BC-AMB"]["resolution"]["candidates"]) == 2
    assert rows["BC-PASSIVE"]["resolution"]["status"] == "unmatched"
    assert rows["BC-RAW"]["resolution"]["status"] == "unmatched"
    assert rows["OLD-UTS"]["resolution"]["status"] == "unmatched"
    assert rows["OLD-UTS"]["resolution"]["suggestion"]["id"] == new.id
    assert rows["BC-SUP"]["resolution"]["panel"] == "supplement"
    assert "cost_price" not in json.dumps(d)                 # katalog uçları maliyet taşımaz
    sup_only = authed_client.get("/api/trendyol/catalog?panel=supplement").json()
    assert [r["barcode"] for r in sup_only["rows"]] == ["BC-SUP"]
    assert d["summary"]["matched"] == 2 and d["summary"]["ambiguous"] == 1 and d["summary"]["over"] == 1
    assert a.id not in [m["id"] for m in d["ims_missing"]]


def test_manual_mapping_bundle_and_audit(authed_client: TestClient, db_session):
    a = _item(db_session, "Krem", "A1", stock=7)
    b = _item(db_session, "Tonik", "B1", stock=10)
    db_session.add(TrendyolProduct(barcode="SET-1", title="İkili set", quantity=2))
    db_session.commit()
    r = authed_client.put("/api/trendyol/mappings", headers=_H, json={
        "barcode": "SET-1", "components": [{"item_id": a.id, "qty": 1}, {"item_id": b.id, "qty": 2}],
        "note": "ikili set"})
    assert r.status_code == 200, r.text
    assert r.json()["resolution"]["status"] == "mapped"
    row = {x["barcode"]: x for x in authed_client.get("/api/trendyol/catalog").json()["rows"]}["SET-1"]
    assert row["ims_stock"] == 5                             # min(7/1, 10/2)
    audit = db_session.query(AdminAuditLog).filter(AdminAuditLog.action == "trendyol.mapping").one()
    assert audit.target_name == "SET-1"
    # yeniden kaydetmek eskisini TAMAMEN değiştirir
    authed_client.put("/api/trendyol/mappings", headers=_H, json={"barcode": "SET-1", "components": [{"item_id": a.id}]})
    assert db_session.query(TrendyolBarcodeMap).filter_by(barcode="SET-1").count() == 1
    assert authed_client.delete("/api/trendyol/mappings?barcode=SET-1", headers=_H).status_code == 200
    assert authed_client.delete("/api/trendyol/mappings?barcode=SET-1", headers=_H).status_code == 404


def test_manual_mapping_overrides_ambiguous_and_goes_invalid_when_card_passive(authed_client: TestClient, db_session):
    x = _item(db_session, "Kopya 1", "AMB")
    _item(db_session, "Kopya 2", "AMB")
    db_session.add(TrendyolProduct(barcode="AMB", title="Ürün", quantity=1))
    db_session.commit()
    assert authed_client.put("/api/trendyol/mappings", headers=_H,
                             json={"barcode": "AMB", "components": [{"item_id": x.id}]}).status_code == 200
    rows = {r["barcode"]: r for r in authed_client.get("/api/trendyol/catalog").json()["rows"]}
    assert rows["AMB"]["resolution"]["status"] == "mapped"
    x.is_active = False
    db_session.commit()
    rows = {r["barcode"]: r for r in authed_client.get("/api/trendyol/catalog").json()["rows"]}
    assert rows["AMB"]["resolution"]["status"] == "invalid_map" and rows["AMB"]["ims_stock"] is None


@pytest.mark.parametrize("bad", ["raw", "passive", "dup", "barcode"])
def test_mapping_validation(authed_client: TestClient, db_session, bad):
    ok = _item(db_session, "Krem", "A1")
    raw = _item(db_session, "Gliserin", "R1", category="Hammadde")
    passive = _item(db_session, "Eski", "P1", active=False)
    body = {"barcode": "BC1", "components": [{"item_id": ok.id}]}
    if bad == "raw":
        body["components"] = [{"item_id": raw.id}]
    elif bad == "passive":
        body["components"] = [{"item_id": passive.id}]
    elif bad == "dup":
        body["components"] = [{"item_id": ok.id}, {"item_id": ok.id}]
    else:
        body["barcode"] = "boşluk var"
    r = authed_client.put("/api/trendyol/mappings", headers=_H, json=body)
    assert r.status_code == 400, r.text
    assert db_session.query(TrendyolBarcodeMap).count() == 0


def test_item_search_both_panels(authed_client: TestClient, db_session):
    _item(db_session, "Minerva Krem", "A1")
    _item(db_session, "Geven Kapsül", "S1", domain="supplement")
    _item(db_session, "Gliserin", "R1", category="Hammadde")
    names = [i["name"] for i in authed_client.get("/api/trendyol/items?q=").json()["items"]]
    assert "Minerva Krem" in names and "Geven Kapsül" in names and "Gliserin" not in names
    assert [i["name"] for i in authed_client.get("/api/trendyol/items?q=kapsul").json()["items"]] == ["Geven Kapsül"]


def test_orders_filters_and_panel(authed_client: TestClient, db_session):
    _item(db_session, "Minerva Krem", "BC-C")
    _item(db_session, "Geven Kapsül", "BC-S", domain="supplement")
    T.upsert_package(db_session, T.parse_package(package(1, order="O1", lines=[line(barcode="BC-C")])))
    T.upsert_package(db_session, T.parse_package(package(2, order="O2", status="Cancelled", lines=[line(barcode="BC-S")])))
    T.upsert_package(db_session, T.parse_package(package(3, order="O3", lines=[line(barcode="UNKNOWN")])))
    db_session.commit()
    d = authed_client.get("/api/trendyol/orders").json()
    assert d["total"] == 3 and d["status_counts"] == {"Delivered": 2, "Cancelled": 1}
    sup = authed_client.get("/api/trendyol/orders?panel=supplement").json()
    assert [o["order_number"] for o in sup["orders"]] == ["O2"]
    un = authed_client.get("/api/trendyol/orders?panel=unmatched").json()
    assert [o["order_number"] for o in un["orders"]] == ["O3"] and un["orders"][0]["warnings"]
    assert authed_client.get("/api/trendyol/orders?status=Cancelled").json()["total"] == 1
    assert authed_client.get("/api/trendyol/orders?q=O3").json()["total"] == 1
    assert authed_client.get("/api/trendyol/orders?panel=x").status_code == 400
    assert authed_client.get("/api/trendyol/orders/YOK").status_code == 404


def test_orders_date_range_uses_tr_days(authed_client: TestClient, db_session):
    # 2026-10-12 22:30 UTC = 13.10.2026 01:30 TR → 13 Ekim'e düşer
    ms = T.to_ty_ms(__import__("datetime").datetime(2026, 10, 12, 22, 30))
    T.upsert_package(db_session, T.parse_package(package(od=ms)))
    db_session.commit()
    assert authed_client.get("/api/trendyol/orders?start=2026-10-13&end=2026-10-13").json()["total"] == 1
    assert authed_client.get("/api/trendyol/orders?start=2026-10-12&end=2026-10-12").json()["total"] == 0


# ─── Senkron / bağlantı uçları ───────────────────────────────────────────────

def test_sync_not_configured_lists_names_only(authed_client: TestClient, monkeypatch):
    _no_env(monkeypatch)
    r = authed_client.post("/api/trendyol/sync", json={"kind": "all"}, headers=_H)
    assert r.status_code == 400 and r.json()["missing"] == list(T.REQUIRED_ENV)


def test_sync_conflict_while_running(authed_client: TestClient, monkeypatch):
    _env(monkeypatch)
    assert T._RUN_LOCK.acquire(blocking=False)
    try:
        assert authed_client.post("/api/trendyol/sync", json={"kind": "orders"}, headers=_H).status_code == 409
    finally:
        T._RUN_LOCK.release()


def test_sync_runs_in_background_and_audits(authed_client: TestClient, db_session, monkeypatch):
    _env(monkeypatch)
    api = FakeApi({"orders/stream": stream([package()]),
                   "products/approved/inventory-and-price": paged([inventory()]),
                   "products/approved": paged([approved()]),
                   "settlements": paged([]), "otherfinancials": paged([])})
    monkeypatch.setattr(T, "make_api", lambda: api)
    r = authed_client.post("/api/trendyol/sync", json={"kind": "all", "backfill_days": 30}, headers=_H)
    assert r.status_code == 202, r.text
    T._THREAD.join(timeout=60)                               # istekten bağımsız daemon iş parçacığı
    assert not T._THREAD.is_alive() and T._THREAD.daemon
    st = authed_client.get("/api/trendyol/status").json()
    assert st["counts"]["packages"] == 1 and st["counts"]["products"] == 1 and st["running"] is False
    assert {s["stream"]: bool(s["last_ok_at"]) for s in st["streams"]} == {"orders": True, "finance": True, "catalog": True}
    assert db_session.query(AdminAuditLog).filter(AdminAuditLog.action == "trendyol.sync").count() == 1
    assert authed_client.post("/api/trendyol/sync", json={"kind": "nope"}, headers=_H).status_code == 422
    assert authed_client.post("/api/trendyol/sync", json={"kind": "all", "backfill_days": 999}, headers=_H).status_code == 422


def test_connection_test_endpoint(authed_client: TestClient, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(T, "make_api", lambda: FakeApi({"products/approved": {"totalElements": 42, "content": []}}))
    r = authed_client.post("/api/trendyol/test", headers=_H)
    assert r.status_code == 200 and r.json()["catalog_total"] == 42
    monkeypatch.setattr(T, "make_api", lambda: FakeApi({"products/approved": T.TrendyolAuthError("401 anahtar")}))
    r = authed_client.post("/api/trendyol/test", headers=_H)
    assert r.status_code == 502 and "401" in r.json()["detail"]


def test_config_toggle(authed_client: TestClient, db_session):
    assert authed_client.put("/api/trendyol/config", json={"enabled": False}, headers=_H).json()["enabled"] is False
    assert authed_client.get("/api/trendyol/status").json()["enabled"] is False
    assert db_session.get(AppSetting, "trendyol.enabled").value == "false"


def test_probe_superadmin_only_and_redacted(client: TestClient, db_session, monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(T, "make_api", lambda: FakeApi({"orders/stream": stream([package()])}))
    _login(client, db_session, "isik")
    assert client.get("/api/trendyol/probe?kind=orders").status_code == 403
    _login(client, db_session, "dogukan")
    r = client.get("/api/trendyol/probe?kind=orders")
    assert r.status_code == 200
    text = json.dumps(r.json(), ensure_ascii=False)
    for secret in ("Ayşe", "Yılmaz", "ayse@example.com", "12345678901", "Gizli", "0555"):
        assert secret not in text
    assert "lineGrossAmount" in text                         # alan adları görünür
    assert client.get("/api/trendyol/probe?kind=bad").status_code == 400


def test_calibration_groups(authed_client: TestClient, db_session):
    from database import TrendyolFinRow
    db_session.add(TrendyolFinRow(source="settlement", row_id="1", type_param="Sale", type_label="Satış",
                                  credit=449.99, debt=0.0, commission_amount=67.4985, seller_revenue=382.4915))
    db_session.add(TrendyolFinRow(source="other", row_id="TYC1", type_param="DeductionInvoices",
                                  type_label="Kargo Faturası", debt=41.09))
    db_session.commit()
    d = authed_client.get("/api/trendyol/calibration").json()
    sale = [g for g in d["groups"] if g["type"] == "Sale"][0]
    assert sale["credit"] == 449.99 and sale["commission_amount"] == 67.4985
    assert d["cargo_invoices"][0]["serial"] == "TYC1" and d["cargo_invoices"][0]["fetched"] is False


# ─── Sayfa + nav ─────────────────────────────────────────────────────────────

def test_page_renders_with_config(authed_client: TestClient):
    r = authed_client.get("/trendyol")
    assert r.status_code == 200
    assert 'window.TY_CFG' in r.text and '"can_profit": true' in r.text
    assert "/static/trendyol.js" in r.text and 'data-ty-tab="catalog"' in r.text
    assert 'href="/trendyol"' in authed_client.get("/").text


def test_nav_link_hidden_without_permission(client: TestClient, db_session):
    _login(client, db_session, "meltem")
    assert 'href="/trendyol"' not in client.get("/").text


def test_nav_script_idempotent(tmp_path, monkeypatch):
    import importlib.util
    import shutil
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    (tmp_path / "templates").mkdir()
    for name in ("index.html", "reports.html", "trendyol.html"):
        shutil.copy(root / "templates" / name, tmp_path / "templates" / name)
    src = (tmp_path / "templates" / "reports.html").read_text(encoding="utf-8")
    stripped = "\n".join(ln for ln in src.split("\n") if 'href="/trendyol"' not in ln)
    (tmp_path / "templates" / "reports.html").write_text(stripped, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("nav", root / "scripts" / "add_trendyol_nav.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    mod.main()
    mod.main()
    out = (tmp_path / "templates" / "reports.html").read_text(encoding="utf-8")
    assert out.count('href="/trendyol"') == 1
    assert out == src                                         # tam olarak aynı yere döndü


# ─── Yetki backfill ──────────────────────────────────────────────────────────

def test_backfill_gates_on_finance_view(db_session):
    from database import _backfill_perm_trendyol
    with_fin = User(username="m_fin", full_name="M", role="Manager", is_active=True, password_hash="x",
                    permissions=json.dumps({"finance": {"view": True}, "items": {"view": True}}))
    no_fin = User(username="m_nofin", full_name="N", role="Manager", is_active=True, password_hash="x",
                  permissions=json.dumps({"items": {"view": True}}))
    db_session.add_all([with_fin, no_fin])
    db_session.query(AppSetting).filter(AppSetting.key == "backfill.perm.trendyol.v1").delete()
    db_session.commit()
    _backfill_perm_trendyol()
    db_session.expire_all()
    p1 = json.loads(db_session.query(User).filter_by(username="m_fin").one().permissions)["trendyol"]
    p2 = json.loads(db_session.query(User).filter_by(username="m_nofin").one().permissions)["trendyol"]
    assert p1 == {"view": True, "profit": True, "manage": True}
    assert p2 == {"view": False, "profit": False, "manage": False}
    # sentinel: ikinci çalıştırma dokunmaz
    u = db_session.query(User).filter_by(username="m_nofin").one()
    perms = json.loads(u.permissions)
    perms["trendyol"] = {"view": True}
    u.permissions = json.dumps(perms)
    db_session.commit()
    _backfill_perm_trendyol()
    db_session.expire_all()
    assert json.loads(db_session.query(User).filter_by(username="m_nofin").one().permissions)["trendyol"] == {"view": True}
