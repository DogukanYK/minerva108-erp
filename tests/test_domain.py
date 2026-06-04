"""
Faz 3 — Kozmetik / Food Supplement panel (domain) ayrımı testleri.

  • Varsayılan domain 'cosmetics' (çerez yokken)
  • /api/domain/switch çerezi set eder, /api/domain mevcut paneli döner
  • Bir panelde oluşturulan ürün/tedarikçi/reçete DİĞER panelde görünmez
  • Varsayılan (cosmetics) davranışı bozulmaz
"""
from fastapi.testclient import TestClient

_HDR = {"Origin": "http://testserver"}


def _switch(client: TestClient, domain: str):
    r = client.post("/api/domain/switch", json={"domain": domain}, headers=_HDR)
    assert r.status_code == 200, r.text
    assert r.json()["domain"] == domain
    return r


def _create_item(client: TestClient, name: str):
    r = client.post("/api/items", json={"name": name, "category": "Hammadde", "unit": "ml"},
                    headers=_HDR)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _item_names(client: TestClient):
    r = client.get("/api/items")
    assert r.status_code == 200
    return {it["name"] for it in r.json()}


# ─── Domain çözümü ──────────────────────────────────────────────────────────

def test_default_domain_is_cosmetics(authed_client: TestClient):
    r = authed_client.get("/api/domain")
    assert r.status_code == 200
    assert r.json()["domain"] == "cosmetics"


def test_switch_sets_cookie(authed_client: TestClient):
    _switch(authed_client, "supplement")
    r = authed_client.get("/api/domain")
    assert r.json()["domain"] == "supplement"
    # Geçersiz değer → cosmetics'e normalize
    _switch(authed_client, "cosmetics")
    r2 = authed_client.post("/api/domain/switch", json={"domain": "garbage"}, headers=_HDR)
    assert r2.json()["domain"] == "cosmetics"


# ─── İzolasyon ──────────────────────────────────────────────────────────────

def test_items_isolated_between_domains(authed_client: TestClient):
    # cosmetics'te bir ürün
    _create_item(authed_client, "Kozmetik Hammadde")
    assert "Kozmetik Hammadde" in _item_names(authed_client)

    # supplement'e geç → cosmetics ürünü GÖRÜNMEZ
    _switch(authed_client, "supplement")
    names_supp = _item_names(authed_client)
    assert "Kozmetik Hammadde" not in names_supp

    # supplement'te bir ürün oluştur
    _create_item(authed_client, "Takviye Hammadde")
    assert "Takviye Hammadde" in _item_names(authed_client)

    # cosmetics'e dön → supplement ürünü GÖRÜNMEZ, kozmetik ürünü görünür
    _switch(authed_client, "cosmetics")
    names_cos = _item_names(authed_client)
    assert "Kozmetik Hammadde" in names_cos
    assert "Takviye Hammadde" not in names_cos


def test_suppliers_isolated_between_domains(authed_client: TestClient):
    authed_client.post("/api/suppliers", json={"name": "Kozmetik Tedarikçi"}, headers=_HDR)
    _switch(authed_client, "supplement")
    supp_names = {s["name"] for s in authed_client.get("/api/suppliers").json()}
    assert "Kozmetik Tedarikçi" not in supp_names
    authed_client.post("/api/suppliers", json={"name": "Takviye Tedarikçi"}, headers=_HDR)
    assert any(s["name"] == "Takviye Tedarikçi" for s in authed_client.get("/api/suppliers").json())
    _switch(authed_client, "cosmetics")
    cos_names = {s["name"] for s in authed_client.get("/api/suppliers").json()}
    assert "Kozmetik Tedarikçi" in cos_names and "Takviye Tedarikçi" not in cos_names


def test_dashboard_stats_scoped(authed_client: TestClient):
    # cosmetics'te 1 ürün; supplement boş başlar
    _create_item(authed_client, "Sadece Kozmetik")
    cos = authed_client.get("/api/dashboard/stats").json()
    _switch(authed_client, "supplement")
    supp = authed_client.get("/api/dashboard/stats").json()
    assert supp["total_items"] == 0          # supplement boş
    assert cos["total_items"] >= 1           # cosmetics dolu


def test_inventory_receive_stamps_item_domain(authed_client: TestClient):
    # supplement panelinde ürün + mal kabul → lot supplement domaininde olmalı
    _switch(authed_client, "supplement")
    iid = _create_item(authed_client, "Takviye Stoklu")
    r = authed_client.post("/api/inventory/receive",
                           json={"item_id": iid, "lot_number": "SUP-1", "quantity": 10},
                           headers=_HDR)
    assert r.status_code == 201, r.text
    # supplement'te görünür
    inv_supp = authed_client.get("/api/inventory").json()
    assert any(x["lot_number"] == "SUP-1" for x in inv_supp)
    # cosmetics'te görünmez
    _switch(authed_client, "cosmetics")
    inv_cos = authed_client.get("/api/inventory").json()
    assert not any(x["lot_number"] == "SUP-1" for x in inv_cos)
