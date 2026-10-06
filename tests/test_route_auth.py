"""
Rota yetki denetimi — 06.10.2026 açığı.

/api/suppliers, /api/inventory, /api/inventory/summary ve /api/transactions
OTURUMSUZ 200 dönüyordu (tedarikçi iletişim bilgileri + bütün stok defteri
internete açıktı).  Bu dosya her API rotasının bağımlılık ağacında
`get_current_user` olmasını zorunlu kılar; bilerek açık olanlar PUBLIC_API
listesinde, gerekçesiyle durur.  Yeni bir uç auth'suz eklenirse test kırılır.
"""
import bcrypt
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from core.auth import get_current_user
from database import Inventory, Item, ProductionHistory, Recipe, User

_H = {"Origin": "http://testserver"}

# (method, path) — bilerek oturumsuz.  Her satırın sebebi yanında.
PUBLIC_API = {
    ("GET", "/robots.txt"), ("GET", "/sw.js"), ("GET", "/.well-known/assetlinks.json"),
    ("POST", "/api/login"), ("POST", "/api/logout"),
    ("GET", "/api/notifications/vapid-public-key"),          # açık anahtar, tasarım gereği
    ("GET", "/health"),                                       # uptime probu
    ("POST", "/s/{token}/unlock"), ("GET", "/s/{token}/f/{file_id}"),   # Drive paylaşım linki
    ("GET", "/s/{token}/browse"), ("GET", "/s/{token}/zip"),
    ("POST", "/api/crm/integrations/kommo/webhook/{secret}"),          # gizli yol + HMAC
    ("POST", "/api/shopify/webhook/orders/{store_key}"),               # HMAC
    ("GET", "/public/product-images/{filename}"),                       # Amazon/Walmart görsel+SDS
    ("POST", "/yorum/{token}/gonder"),                                  # müşteri yorum daveti
    ("POST", "/basvuru/gonder"), ("POST", "/basvuru/t/{token}/adres"),  # influencer başvurusu
    ("POST", "/basvuru/t/{token}/insights"), ("POST", "/basvuru/t/{token}/onay"),
}

# Bu açıkla kapanan uçlar — oturumsuz 401 dönmeli.
FORMERLY_PUBLIC = ["/api/suppliers", "/api/inventory", "/api/inventory/summary",
                   "/api/transactions", "/api/production", "/api/qc/quarantine"]


def _has_auth(dep) -> bool:
    for d in dep.dependencies:
        if d.call is get_current_user or _has_auth(d):
            return True
    return False


def _api_routes():
    from api_main import app
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        if r.endpoint.__module__ == "api_main":      # sayfa rotaları kullanıcıyı kendi çözer
            continue
        for m in sorted(r.methods - {"HEAD"}):
            yield m, r.path, r


def test_every_api_route_requires_auth():
    missing = [f"{m} {p}" for m, p, r in _api_routes()
               if not _has_auth(r.dependant) and (m, p) not in PUBLIC_API]
    assert not missing, "Auth'suz API rotası: " + ", ".join(missing)


def test_public_allowlist_not_stale():
    from api_main import app
    existing = {(m, r.path) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}
    stale = sorted(f"{m} {p}" for m, p in PUBLIC_API if (m, p) not in existing)
    assert not stale, "PUBLIC_API'de artık olmayan rota: " + ", ".join(stale)


@pytest.mark.parametrize("path", FORMERLY_PUBLIC)
def test_formerly_public_endpoints_need_login(client: TestClient, path):
    assert client.get(path).status_code == 401


def _user_client(client, db, username, role):
    db.add(User(username=username,
                password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode(),
                full_name=username, role=role, is_active=True))
    db.commit()
    r = client.post("/api/login", json={"username": username, "password": "minerva123"}, headers=_H)
    assert r.status_code == 200, r.text
    return client


INTERNAL_READS = FORMERLY_PUBLIC + ["/api/recipes", "/api/inventory/samples",
                                    "/api/traceability/expiring", "/api/drive/files"]


def test_staff_still_reads(client: TestClient, db_session: Session):
    c = _user_client(client, db_session, "rafci", "Staff")
    for p in INTERNAL_READS:
        assert c.get(p).status_code == 200, p


def test_distributor_forbidden_on_internal_reads(client: TestClient, db_session: Session):
    c = _user_client(client, db_session, "bayi_x", "Distributor")
    for p in INTERNAL_READS:
        assert c.get(p).status_code == 403, p


def test_inactive_user_token_rejected(client: TestClient, db_session: Session):
    c = _user_client(client, db_session, "ayrilan", "Staff")
    u = db_session.query(User).filter(User.username == "ayrilan").one()
    u.is_active = False
    db_session.commit()
    for p in ("/api/suppliers", "/api/recipes", "/api/drive/files"):
        assert c.get(p).status_code == 401, p


def test_by_item_domain_scoped(authed_client: TestClient, db_session: Session):
    it = Item(name="Takviye kapsül", sku="SUP-1", category="Hammadde", unit="g",
              current_stock=5, domain="supplement", is_active=True)
    db_session.add(it); db_session.commit()
    db_session.add(Inventory(item_id=it.id, lot_number="L1", quantity=5, domain="supplement"))
    db_session.commit()
    assert authed_client.get(f"/api/inventory/by-item/{it.id}").status_code == 404
    authed_client.cookies.set("active_domain", "supplement")
    r = authed_client.get(f"/api/inventory/by-item/{it.id}")
    assert r.status_code == 200 and len(r.json()["lots"]) == 1


def test_recipe_and_production_detail_domain_scoped(authed_client: TestClient, db_session: Session):
    rc = Recipe(name="Kapsül reçete", output_quantity=1, domain="supplement", is_active=True)
    db_session.add(rc); db_session.commit()
    ph = ProductionHistory(recipe_id=rc.id, produced_quantity=1, domain="supplement")
    db_session.add(ph); db_session.commit()
    assert authed_client.get(f"/api/recipes/{rc.id}").status_code == 404
    assert authed_client.get(f"/api/production/{ph.id}").status_code == 404
    authed_client.cookies.set("active_domain", "supplement")
    assert authed_client.get(f"/api/recipes/{rc.id}").status_code == 200
