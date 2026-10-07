"""
Kart kaydında maliyet (cost_price) koruması — 07.10.2026 hatası.

items.html finans dışı kullanıcıda maliyet alanını HİÇ göndermiyor; Pydantic
varsayılanı 0.0 olduğu için `update_item` (`data.cost_price or 0.0`) LabLead
min stoğu değiştirip kaydedince Manager'ın girdiği maliyeti 0'lıyordu.
Artık maliyet yalnız finans rolü alanı fiilen gönderdiyse yazılır; create'te
finans dışı kullanıcının cost_price'ı yok sayılır; "Geri al" maliyete yalnız
o düzenleme ona dokunduysa döner.  Ayrıca update_item aktif panelle sınırlı.
"""
from fastapi.testclient import TestClient

from database import Item

_H = {"Origin": "http://testserver"}


def _login(c: TestClient, username: str) -> TestClient:
    c.post("/api/logout", headers=_H)
    r = c.post("/api/login", json={"username": username, "password": "minerva123"}, headers=_H)
    assert r.status_code == 200, r.text
    return c


def _card(db, cost=12.5, domain="cosmetics") -> int:
    it = Item(name="ARGAN YAĞI", category="Hammadde", unit="g", current_stock=0,
              min_stock_level=100, cost_price=cost, domain=domain)
    db.add(it)
    db.commit()
    return it.id


def _cost(db, iid) -> float:
    db.expire_all()
    return db.get(Item, iid).cost_price


_BODY = {"name": "ARGAN YAĞI", "category": "Hammadde", "unit": "g"}


def test_lablead_save_without_cost_keeps_cost(client: TestClient, db_session):
    iid = _card(db_session)
    _login(client, "songul")
    r = client.put(f"/api/items/{iid}", headers=_H, json={**_BODY, "min_stock": 250})
    assert r.status_code == 200, r.text
    assert _cost(db_session, iid) == 12.5
    assert db_session.get(Item, iid).min_stock_level == 250


def test_lablead_sent_cost_is_ignored(client: TestClient, db_session):
    iid = _card(db_session)
    _login(client, "songul")
    r = client.put(f"/api/items/{iid}", headers=_H, json={**_BODY, "cost_price": 99})
    assert r.status_code == 200
    assert _cost(db_session, iid) == 12.5


def test_manager_cost_is_saved(client: TestClient, db_session):
    iid = _card(db_session)
    _login(client, "isik")                                     # Manager (finans)
    r = client.put(f"/api/items/{iid}", headers=_H, json={**_BODY, "cost_price": 8.0})
    assert r.status_code == 200
    assert _cost(db_session, iid) == 8.0
    # Manager de alanı göndermeden kaydederse maliyet korunur
    client.put(f"/api/items/{iid}", headers=_H, json={**_BODY, "min_stock": 5})
    assert _cost(db_session, iid) == 8.0


def test_lablead_create_ignores_cost(client: TestClient, db_session):
    _login(client, "songul")
    r = client.post("/api/items", headers=_H,
                    json={"name": "YENİ HAMMADDE", "category": "Hammadde", "unit": "g",
                          "cost_price": 99})
    assert r.status_code == 201, r.text
    assert _cost(db_session, r.json()["id"]) == 0.0


def test_undo_does_not_revert_cost_it_did_not_touch(client: TestClient, db_session):
    iid = _card(db_session, cost=5.0)
    _login(client, "songul")
    assert client.put(f"/api/items/{iid}", headers=_H,
                      json={**_BODY, "min_stock": 300}).status_code == 200
    _login(client, "isik")
    assert client.put(f"/api/items/{iid}", headers=_H,
                      json={**_BODY, "min_stock": 300, "cost_price": 8.0}).status_code == 200
    _login(client, "songul")
    r = client.post("/api/undo", headers=_H)
    assert r.status_code == 200, r.text
    assert _cost(db_session, iid) == 8.0                       # Manager'ın maliyeti kaldı
    assert db_session.get(Item, iid).min_stock_level == 100    # LabLead'in düzenlemesi geri alındı


def test_update_item_other_domain_404(authed_client: TestClient, db_session):
    iid = _card(db_session, domain="supplement")
    r = authed_client.put(f"/api/items/{iid}", headers=_H, json={**_BODY, "cost_price": 0})
    assert r.status_code == 404
    assert _cost(db_session, iid) == 12.5


# ── Toplu içe aktarma (POST /api/import-items) ──────────────────────────────
# items.import LabLead'de de açık; bu yol rol bakmadan Cost_Price yazıyordu.
# pandas boş hücreyi NaN okur → karta cost_price = NaN yazılıyor, finans
# kullanıcısının GET /api/items yanıtı JSON'a çevrilemeyip 500 dönüyordu.

_COLS = ["Item_Name", "SKU", "Category", "Unit", "Stock", "Cost_Price", "Min_Stock_Level"]


def _xlsx(*rows) -> bytes:
    """Şablon başlıklı xlsx; None hücre BOŞ bırakılır (pandas → NaN)."""
    import io
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(_COLS)
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _import(c: TestClient, *rows):
    files = {"file": ("urunler.xlsx", _xlsx(*rows),
                      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    r = c.post("/api/import-items", headers=_H, files=files)
    assert r.status_code == 200, r.text
    return r.json()


def _sku_card(db, cost=25.0, domain="cosmetics", sku="ARG-1") -> int:
    it = Item(name="ARGAN YAĞI", sku=sku, category="Hammadde", unit="g", current_stock=40,
              min_stock_level=100, cost_price=cost, domain=domain)
    db.add(it)
    db.commit()
    return it.id


def test_import_lablead_empty_or_zero_cost_keeps_cost(client: TestClient, db_session):
    iid = _sku_card(db_session)
    _login(client, "songul")                                   # LabLead
    assert _import(client, ["ARGAN YAĞI", "ARG-1", "Hammadde", "g", 40, None, 100])["updated"] == 1
    assert _cost(db_session, iid) == 25.0
    assert _import(client, ["ARGAN YAĞI", "ARG-1", "Hammadde", "g", 40, 0, 100])["updated"] == 1
    assert _cost(db_session, iid) == 25.0
    _login(client, "isik")                                     # finans: liste 500 vermemeli
    r = client.get("/api/items")
    assert r.status_code == 200, r.text
    row = next(i for i in r.json() if i["id"] == iid)
    assert row["cost_price"] == 25.0


def test_import_lablead_create_ignores_cost(client: TestClient, db_session):
    _login(client, "songul")
    assert _import(client, ["YENİ YAĞ", "NEW-1", "Hammadde", "g", 0, 99, 10])["created"] == 1
    db_session.expire_all()
    assert db_session.query(Item).filter(Item.sku == "NEW-1").one().cost_price == 0.0


def test_import_manager_empty_cost_kept_number_written(client: TestClient, db_session):
    iid = _sku_card(db_session)
    _login(client, "isik")                                     # Manager (finans)
    _import(client, ["ARGAN YAĞI", "ARG-1", "Hammadde", "g", 40, None, 100])
    assert _cost(db_session, iid) == 25.0
    _import(client, ["ARGAN YAĞI", "ARG-1", "Hammadde", "g", 40, "31,5", 100])
    assert _cost(db_session, iid) == 31.5


def test_import_empty_numbers_do_not_write_nan(client: TestClient, db_session):
    """Boş Min_Stock_Level/Stock mevcut değeri korur; boş metin "nan" yazmaz.
    Güncellemede boş Category / Unit de mevcut değeri korur (boş Unit kartı
    sessizce "adet" yapıp g/kg ailesini bozardı)."""
    iid = _sku_card(db_session)
    _login(client, "isik")
    _import(client, ["ARGAN YAĞI", "ARG-1", None, "g", None, None, None])
    db_session.expire_all()
    it = db_session.get(Item, iid)
    assert it.min_stock_level == 100
    assert it.current_stock == 40
    assert it.category == "Hammadde"
    _import(client, ["ARGAN YAĞI", "ARG-1", None, None, None, None, None])
    db_session.expire_all()
    it = db_session.get(Item, iid)
    assert (it.category, it.unit) == ("Hammadde", "g")
    _import(client, ["ARGAN YAĞI", "ARG-1", "Ambalaj", "kg", None, None, None])
    db_session.expire_all()
    it = db_session.get(Item, iid)
    assert (it.category, it.unit) == ("Ambalaj", "kg")            # dolu hücre yine yazılır
    _import(client, ["BOŞ SAYILI", "NEW-2", None, None, None, None, None])
    db_session.expire_all()
    new = db_session.query(Item).filter(Item.sku == "NEW-2").one()
    assert (new.min_stock_level, new.current_stock, new.cost_price, new.unit) == (0.0, 0.0, 0.0, "adet")
    assert client.get("/api/items").status_code == 200


def test_import_skips_other_panel_sku(client: TestClient, db_session):
    iid = _sku_card(db_session, domain="supplement")
    _login(client, "isik")                                     # aktif panel: cosmetics
    out = _import(client, ["DEĞİŞTİ", "ARG-1", "Hammadde", "g", 1, 1, 1])
    assert out["updated"] == 0 and out["error_count"] == 1
    assert "başka panelin" in out["errors"][0]
    db_session.expire_all()
    it = db_session.get(Item, iid)
    assert (it.name, it.cost_price, it.current_stock) == ("ARGAN YAĞI", 25.0, 40)
