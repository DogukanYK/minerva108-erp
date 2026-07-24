"""
IMS → Shopify stok senkron testleri — AĞSIZ (httpx mock'lu, test_kommo stili).

  • RBAC: status/sync/preview/config admin.view (labtech 403, anon 401)
  • Yapılandırma: env yoksa no-op/400, mağaza atlanır
  • Eşleşme: barkod → qty=floor(stok)-buffer, negatif→0, marka yönlendirme, domain izolasyonu
  • Belirsiz barkod (dup) atlanır+raporlanır; iki yön eşleşmeyen raporlanır
  • dry-run push ETMEZ + state yazmaz; canlı state+audit satırı; userErrors raporlanır
  • global enabled=false → scheduler job no-op
"""
from fastapi.testclient import TestClient

from core import shopify as S
from database import AdminAuditLog, Item, ShopifySyncState

_H = {"Origin": "http://testserver"}


def _env(monkeypatch, brand="MINERVA"):
    monkeypatch.setenv(f"SHOPIFY_{brand}_STORE", f"{brand.lower()}.myshopify.com")
    monkeypatch.setenv(f"SHOPIFY_{brand}_CLIENT_ID", f"cid-{brand.lower()}")
    monkeypatch.setenv(f"SHOPIFY_{brand}_CLIENT_SECRET", f"csec-{brand.lower()}")
    monkeypatch.setenv(f"SHOPIFY_{brand}_LOCATION", "111")


def _item(db, name, barcode, stock=10.0, domain="cosmetics", category="Bitmiş Ürün", sku=None):
    it = Item(name=name, sku=sku or f"sku-{name[:20]}", category=category, unit="adet",
              barcode=barcode, current_stock=stock, domain=domain, is_active=True)
    db.add(it); db.commit(); db.refresh(it)
    return it


def _variant(barcode, iid, status="ACTIVE"):
    return {"barcode": barcode, "sku": "", "inventory_item_id": f"gid://iv/{iid}",
            "variant_id": f"gid://v/{iid}", "product_status": status}


# ─── RBAC ─────────────────────────────────────────────────────────────────────

def test_status_requires_admin(labtech_client: TestClient):
    assert labtech_client.get("/api/shopify/status").status_code == 403


def test_status_anon_401(client: TestClient):
    assert client.get("/api/shopify/status").status_code == 401


def test_mutations_require_admin(labtech_client: TestClient):
    assert labtech_client.post("/api/shopify/sync", json={}, headers=_H).status_code == 403
    assert labtech_client.get("/api/shopify/preview").status_code == 403
    assert labtech_client.put("/api/shopify/config", json={"buffer": 5}, headers=_H).status_code == 403


# ─── Yapılandırma ─────────────────────────────────────────────────────────────

def test_status_ok_unconfigured(authed_client: TestClient):
    d = authed_client.get("/api/shopify/status").json()
    assert d["any_configured"] is False
    assert {s["store_key"] for s in d["stores"]} == {"minerva", "evanira", "serenida"}
    assert all(s["configured"] is False for s in d["stores"])


def test_sync_400_when_unconfigured(authed_client: TestClient):
    assert authed_client.post("/api/shopify/sync", json={}, headers=_H).status_code == 400
    assert authed_client.get("/api/shopify/preview").status_code == 400


def test_store_skips_when_env_empty(authed_client, db_session, monkeypatch):
    _env(monkeypatch, "MINERVA")                       # yalnız Minerva
    monkeypatch.setattr(S, "fetch_variants", lambda b: [])
    monkeypatch.setattr(S, "push_inventory", lambda b, p: {"pushed": 0, "userErrors": []})
    d = authed_client.post("/api/shopify/sync", json={}, headers=_H).json()
    by = {s["store"]: s for s in d["per_store"]}
    assert by["minerva"]["configured"] is True
    assert by["evanira"].get("skipped") is True and by["serenida"].get("skipped") is True


# ─── Eşleşme + qty ────────────────────────────────────────────────────────────

def test_match_by_barcode_and_qty(authed_client, db_session, monkeypatch):
    _env(monkeypatch, "MINERVA")
    it = _item(db_session, "Minerva 108 Face Scrub", "111000", stock=21.0)
    pushed = {}
    monkeypatch.setattr(S, "fetch_variants",
                        lambda b: [_variant("111000", it.id)] if b == "Minerva" else [])
    monkeypatch.setattr(S, "push_inventory",
                        lambda b, p: (pushed.update({b: p}) or {"pushed": len(p), "userErrors": []}))
    d = authed_client.post("/api/shopify/sync", json={"brands": ["Minerva"]}, headers=_H).json()
    m = next(s for s in d["per_store"] if s["store"] == "minerva")
    assert m["matched"] == 1 and m["pushed"] == 1
    assert pushed["Minerva"] == [(f"gid://iv/{it.id}", 21)]


def test_qty_floor_and_buffer(authed_client, db_session, monkeypatch):
    _env(monkeypatch, "MINERVA")
    it = _item(db_session, "Minerva 108 Serum", "111001", stock=21.7)
    authed_client.put("/api/shopify/config", json={"buffer": 5}, headers=_H)
    captured = {}
    monkeypatch.setattr(S, "fetch_variants", lambda b: [_variant("111001", it.id)] if b == "Minerva" else [])
    monkeypatch.setattr(S, "push_inventory", lambda b, p: (captured.update({b: p}) or {"pushed": len(p), "userErrors": []}))
    authed_client.post("/api/shopify/sync", json={"brands": ["Minerva"]}, headers=_H)
    assert captured["Minerva"] == [(f"gid://iv/{it.id}", 16)]     # floor(21.7)-5


def test_qty_negative_clamped_zero(authed_client, db_session, monkeypatch):
    _env(monkeypatch, "MINERVA")
    it = _item(db_session, "Minerva 108 Krem", "111002", stock=3.0)
    authed_client.put("/api/shopify/config", json={"buffer": 10}, headers=_H)
    captured = {}
    monkeypatch.setattr(S, "fetch_variants", lambda b: [_variant("111002", it.id)] if b == "Minerva" else [])
    monkeypatch.setattr(S, "push_inventory", lambda b, p: (captured.update({b: p}) or {"pushed": len(p), "userErrors": []}))
    authed_client.post("/api/shopify/sync", json={"brands": ["Minerva"]}, headers=_H)
    assert captured["Minerva"] == [(f"gid://iv/{it.id}", 0)]      # negatif değil


def test_duplicate_barcode_ambiguous_skipped(authed_client, db_session, monkeypatch):
    _env(monkeypatch, "MINERVA")
    a = _item(db_session, "Minerva 108 Foot Cream", "111DUP", stock=21.0)
    b = _item(db_session, "MİNERVA 108 FOOT CREAM KOPYA", "111DUP", stock=0.0)
    captured = {}
    monkeypatch.setattr(S, "fetch_variants", lambda br: [_variant("111DUP", 900)] if br == "Minerva" else [])
    monkeypatch.setattr(S, "push_inventory", lambda br, p: (captured.update({br: p}) or {"pushed": len(p), "userErrors": []}))
    d = authed_client.post("/api/shopify/sync", json={"brands": ["Minerva"]}, headers=_H).json()
    m = next(s for s in d["per_store"] if s["store"] == "minerva")
    assert m["matched"] == 0                                       # belirsiz → push edilmedi
    assert m["ambiguous"] and m["ambiguous"][0]["barcode"] == "111DUP"
    assert set(m["ambiguous"][0]["ids"]) == {a.id, b.id}
    assert captured.get("Minerva", []) == []


def test_unmatched_both_directions(authed_client, db_session, monkeypatch):
    _env(monkeypatch, "MINERVA")
    _item(db_session, "Minerva 108 IMS Only", "111IMS", stock=5.0)      # IMS'te var, Shopify'da yok
    monkeypatch.setattr(S, "fetch_variants",
                        lambda b: [_variant("111SHOP", 42)] if b == "Minerva" else [])  # Shopify'da var, IMS'te yok
    monkeypatch.setattr(S, "push_inventory", lambda b, p: {"pushed": len(p), "userErrors": []})
    d = authed_client.post("/api/shopify/sync", json={"brands": ["Minerva"]}, headers=_H).json()
    m = next(s for s in d["per_store"] if s["store"] == "minerva")
    assert any(u["barcode"] == "111IMS" for u in m["unmatched_ims"])
    assert "111SHOP" in m["unmatched_shop"]


def test_brand_routing(authed_client, db_session, monkeypatch):
    _env(monkeypatch, "MINERVA")
    _env(monkeypatch, "EVANIRA")
    mi = _item(db_session, "Minerva 108 A", "MB1", stock=4.0)
    ev = _item(db_session, "Evanira B", "EB1", stock=6.0)
    calls = {}
    monkeypatch.setattr(S, "fetch_variants",
                        lambda b: {"Minerva": [_variant("MB1", mi.id)],
                                   "Evanira": [_variant("EB1", ev.id)]}.get(b, []))
    monkeypatch.setattr(S, "push_inventory",
                        lambda b, p: (calls.update({b: [x[0] for x in p]}) or {"pushed": len(p), "userErrors": []}))
    authed_client.post("/api/shopify/sync", json={}, headers=_H)
    assert calls["Minerva"] == [f"gid://iv/{mi.id}"]               # Minerva ürünü yalnız minerva'ya
    assert calls["Evanira"] == [f"gid://iv/{ev.id}"]


def test_domain_isolation(authed_client, db_session, monkeypatch):
    _env(monkeypatch, "MINERVA")
    _item(db_session, "Minerva 108 Supp", "SUPP1", stock=9.0, domain="supplement")
    captured = {}
    monkeypatch.setattr(S, "fetch_variants", lambda b: [_variant("SUPP1", 7)] if b == "Minerva" else [])
    monkeypatch.setattr(S, "push_inventory", lambda b, p: (captured.update({b: p}) or {"pushed": len(p), "userErrors": []}))
    d = authed_client.post("/api/shopify/sync", json={"brands": ["Minerva"]}, headers=_H).json()
    m = next(s for s in d["per_store"] if s["store"] == "minerva")
    assert m["matched"] == 0 and captured.get("Minerva", []) == []  # supplement asla push edilmez


# ─── dry-run + state + audit ─────────────────────────────────────────────────

def test_dry_run_does_not_push_or_write_state(authed_client, db_session, monkeypatch):
    _env(monkeypatch, "MINERVA")
    it = _item(db_session, "Minerva 108 Dry", "DRY1", stock=8.0)
    calls = []
    monkeypatch.setattr(S, "fetch_variants", lambda b: [_variant("DRY1", it.id)] if b == "Minerva" else [])
    monkeypatch.setattr(S, "push_inventory", lambda b, p: (calls.append(p) or {"pushed": len(p), "userErrors": []}))
    d = authed_client.get("/api/shopify/preview").json()
    m = next(s for s in d["per_store"] if s["store"] == "minerva")
    assert m["matched"] == 1 and m["pushed"] == 0
    assert calls == []                                             # push ÇAĞRILMADI
    assert db_session.query(ShopifySyncState).filter_by(store_key="minerva").first() is None  # state YAZILMADI


def test_sync_writes_state(authed_client, db_session, monkeypatch):
    _env(monkeypatch, "MINERVA")
    it = _item(db_session, "Minerva 108 State", "ST1", stock=12.0)
    monkeypatch.setattr(S, "fetch_variants", lambda b: [_variant("ST1", it.id)] if b == "Minerva" else [])
    monkeypatch.setattr(S, "push_inventory", lambda b, p: {"pushed": len(p), "userErrors": []})
    authed_client.post("/api/shopify/sync", json={"brands": ["Minerva"]}, headers=_H)
    st = db_session.query(ShopifySyncState).filter_by(store_key="minerva").first()
    assert st and st.matched_count == 1 and st.pushed_count == 1 and st.last_sync_at is not None


def test_sync_writes_audit(authed_client, db_session, monkeypatch):
    _env(monkeypatch, "MINERVA")
    monkeypatch.setattr(S, "fetch_variants", lambda b: [])
    monkeypatch.setattr(S, "push_inventory", lambda b, p: {"pushed": 0, "userErrors": []})
    authed_client.post("/api/shopify/sync", json={}, headers=_H)
    assert db_session.query(AdminAuditLog).filter(AdminAuditLog.action == "shopify.sync").count() >= 1


def test_user_errors_reported(authed_client, db_session, monkeypatch):
    _env(monkeypatch, "MINERVA")
    it = _item(db_session, "Minerva 108 UE", "UE1", stock=5.0)
    monkeypatch.setattr(S, "fetch_variants", lambda b: [_variant("UE1", it.id)] if b == "Minerva" else [])
    monkeypatch.setattr(S, "push_inventory",
                        lambda b, p: {"pushed": 0, "userErrors": [{"field": "x", "message": "boom", "code": "E"}]})
    d = authed_client.post("/api/shopify/sync", json={"brands": ["Minerva"]}, headers=_H).json()
    m = next(s for s in d["per_store"] if s["store"] == "minerva")
    assert m["userErrors"] and m["userErrors"][0]["message"] == "boom"


def test_access_token_client_credentials(monkeypatch):
    """Client ID+Secret → POST /admin/oauth/access_token → token cache'lenir."""
    import httpx
    calls = []

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"access_token": "shpat_live", "expires_in": 86399}

    def fake_post(url, **kw):
        calls.append((url, kw.get("data", {})))
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    S._TOKEN_CACHE.clear()
    cfg = {"store": "x.myshopify.com", "client_id": "cid", "client_secret": "csec"}
    assert S._get_access_token(cfg) == "shpat_live"
    assert calls[0][0].endswith("/admin/oauth/access_token")
    assert calls[0][1]["grant_type"] == "client_credentials"
    # ikinci çağrı cache'ten gelir (yeni HTTP yok)
    assert S._get_access_token(cfg) == "shpat_live"
    assert len(calls) == 1
    S._TOKEN_CACHE.clear()


def test_global_enabled_toggle_pauses_job(authed_client, db_session, monkeypatch):
    _env(monkeypatch, "MINERVA")
    authed_client.put("/api/shopify/config", json={"enabled": False}, headers=_H)
    assert S.get_enabled(db_session) is False
    # scheduler job global kapalıyken run_shopify_sync ÇAĞIRMAZ
    from core import scheduler as sch
    called = []
    monkeypatch.setattr(S, "run_shopify_sync", lambda *a, **k: called.append(1))
    sch.shopify_stock_sync()
    assert called == []
