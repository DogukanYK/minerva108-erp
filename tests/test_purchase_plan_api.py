"""
Satın Alma Planı API'si (routers/purchase_plan.py) + /satin-alma sayfası.

    MINERVA_TEST_DB=minerva_test_u .venv/bin/pytest tests/test_purchase_plan_api.py -q

Kapsam: ürün listesi (kanonik marka çipleri, reçetesiz `i:` satırı, soyut
ebeveyn hariç, varsayılan hariç DİSTİLE SU, domain izolasyonu); önizleme
(bölümler + row_keys + sunucu biçimli tedarikçi hücresi; boş/0 adet 422,
supplement id'si kozmetik panelde 400, yalnız reçetesiz satır 400, pasif
reçete 400); kur YALNIZ farklı para birimli teklif varken çekilir; PDF/Excel
dışa aktarma + audit; geçmiş üretimden doldur; senaryo CRUD (ad tekilliği
TR-katlanmış, yumuşak silme sonrası ad yeniden kullanılabilir, sahip /
SuperAdmin / Manager kuralı, audit satırları, missing[] işaretleme,
last_run_at damgası updated_at'i değiştirmez); Origin'siz mutasyon 403;
sayfa render + script dosyası.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from database import (AdminAuditLog, AppSetting, Item, ProductionHistory, PurchasePlan, Recipe,
                      RecipeIngredient, SupplierPrice)

_HDR = {"Origin": "http://testserver"}
_seq = [0]


def _item(db, name, *, cat="Hammadde", unit="g", stock=0.0, domain="cosmetics", pkg=None, parent=None,
          active=True):
    _seq[0] += 1
    it = Item(name=name, sku=f"pp-{_seq[0]}", category=cat, unit=unit, current_stock=stock,
              domain=domain, pkg_type=pkg, parent_id=parent, is_active=active)
    db.add(it)
    db.flush()
    return it


def _seed(db, *, domain="cosmetics", brand="Serenida", price_cur="USD"):
    """Bir reçeteli ürün (gliserin + kavanoz) + reçetesiz bitmiş ürün + fiyat."""
    gli = _item(db, f"GLİSERİN {domain}", unit="g", stock=500, domain=domain)
    jar = _item(db, f"50 ML KAVANOZ {domain}", cat="Ambalaj", unit="adet", stock=10, domain=domain,
                pkg="kavanoz")
    tgt = _item(db, f"{brand} Gece Kremi 50 ml {domain}", cat="Bitmiş Ürün", unit="adet", stock=3,
                domain=domain)
    rl = _item(db, f"{brand} Reçetesiz Losyon {domain}", cat="Bitmiş Ürün", unit="adet", stock=7,
               domain=domain)
    rec = Recipe(name=tgt.name, output_quantity=1, output_unit="adet", target_item_id=tgt.id,
                 waste_percentage=10, domain=domain)
    db.add(rec)
    db.flush()
    db.add_all([RecipeIngredient(recipe_id=rec.id, item_id=gli.id, quantity=30, unit="g"),
                RecipeIngredient(recipe_id=rec.id, item_id=jar.id, quantity=1, unit="adet")])
    db.add(SupplierPrice(item_id=gli.id, supplier_name="UMAYCHEM", unit_price=4.52, package_size=25,
                         currency=price_cur, price_unit="kg", source="stok_son_durum", domain=domain))
    db.commit()
    return {"gli": gli.id, "jar": jar.id, "tgt": tgt.id, "rl": rl.id, "rec": rec.id}


def _req(ids, qty=100, **opts):
    return {"title": "Deneme Siparişi",
            "lines": [{"recipe_id": ids["rec"], "target_item_id": ids["tgt"], "qty": qty}],
            "options": opts}


def _login(client, username):
    client.cookies.clear()
    r = client.post("/api/login", json={"username": username, "password": "minerva123"}, headers=_HDR)
    assert r.status_code == 200, r.text
    return client


def _audit(db, action):
    db.expire_all()
    return db.query(AdminAuditLog).filter(AdminAuditLog.action == action).all()


# ─── Ürün listesi ───────────────────────────────────────────────────────────

def test_products_brands_recipeless_and_defaults(authed_client: TestClient, db_session: Session):
    ids = _seed(db_session)
    # "MİNERVA-108" yazımı kanonik "Minerva 108" çipine düşer
    mv = _item(db_session, "MİNERVA-108 Saç Kremi 300 ml", cat="Bitmiş Ürün", unit="adet")
    # soyut varyasyon ebeveyni listelenmez
    parent = _item(db_session, "Minerva 108 Şampuan", cat="Bitmiş Ürün", unit="adet")
    _item(db_session, "Minerva 108 Şampuan 300 ml", cat="Bitmiş Ürün", unit="adet", parent=parent.id)
    water = _item(db_session, "DİSTİLE SU", unit="ml")
    db_session.commit()

    r = authed_client.get("/api/purchase-plan/products")
    assert r.status_code == 200
    d = r.json()
    by_key = {p["key"]: p for p in d["products"]}
    rp = by_key[f"r:{ids['rec']}"]
    assert rp["brand"] == "Serenida" and rp["brand_key"] == "serenida"
    assert rp["recipeless"] is False and rp["has_packaging"] is True and rp["primary"] is True
    assert rp["finished_stock"] == 3 and rp["size_ml"] == 50 and rp["target_item_id"] == ids["tgt"]
    rl = by_key[f"i:{ids['rl']}"]
    assert rl["recipeless"] is True and rl["recipe_id"] is None
    assert by_key[f"i:{mv.id}"]["brand"] == "Minerva 108"
    assert f"i:{parent.id}" not in by_key                       # soyut ebeveyn yok
    brands = {b["key"]: b for b in d["brands"]}
    assert brands["serenida"]["count"] == 2 and brands["minerva"]["label"] == "Minerva 108"
    assert {"id": water.id, "name": "DİSTİLE SU", "unit": "ml"} in d["defaults"]["excluded_items"]


def test_products_domain_isolation(authed_client: TestClient, db_session: Session):
    cos = _seed(db_session, domain="cosmetics")
    sup = _seed(db_session, domain="supplement", brand="Takviye")
    keys = {p["key"] for p in authed_client.get("/api/purchase-plan/products").json()["products"]}
    assert f"r:{cos['rec']}" in keys and f"r:{sup['rec']}" not in keys and f"i:{sup['rl']}" not in keys
    authed_client.cookies.set("active_domain", "supplement")
    keys = {p["key"] for p in authed_client.get("/api/purchase-plan/products").json()["products"]}
    assert f"r:{sup['rec']}" in keys and f"r:{cos['rec']}" not in keys


# ─── Önizleme ───────────────────────────────────────────────────────────────

def test_preview_report_sections_and_ui(authed_client: TestClient, db_session: Session):
    ids = _seed(db_session)
    r = authed_client.post("/api/purchase-plan/preview", json=_req(ids, qty=100), headers=_HDR)
    assert r.status_code == 200, r.text
    d = r.json()
    mats = {m["item_id"]: m for m in d["materials"]}
    gli = mats[ids["gli"]]
    # 30 g × 100 × 1,10 = 3300 g gerek, 500 g stok → 2,8 kg (güvenli yuvarlama)
    assert gli["need"] == pytest.approx(3300) and gli["status"] == "to_buy"
    assert gli["display"]["buy_num"] == pytest.approx(2.8)
    assert gli["group"] == "list" and gli["supplier"] == "UMAYCHEM"
    assert gli["amount"] == 13                                  # round_half_up(2,8 × 4,52 = 12,656)
    assert gli["ui"]["sup"][0]["s"] == "b" and "UMAYCHEM" in gli["ui"]["sup"][0]["t"]
    assert gli["ui"]["amount"] == "13 $"
    keys = [s["key"] for s in d["sections"]]
    assert keys[0] == "raw_priced" and "notes" in keys and "products" in keys
    assert [s["no"] for s in d["sections"]] == list(range(1, len(keys) + 1))
    raw = d["sections"][0]
    assert raw["row_keys"] == [gli["key"]] and "rows" not in raw and raw["total_text"] == "13 $"
    assert d["pricing"]["currency"] == "USD" and d["meta"]["domain"] == "cosmetics"
    assert d["meta"]["title"] == "Deneme Siparişi"


def test_preview_validation(authed_client: TestClient, db_session: Session):
    ids = _seed(db_session)
    sup = _seed(db_session, domain="supplement", brand="Takviye")
    url = "/api/purchase-plan/preview"
    assert authed_client.post(url, json={"lines": []}, headers=_HDR).status_code == 422
    assert authed_client.post(url, json=_req(ids, qty=0), headers=_HDR).status_code == 422
    assert authed_client.post(url, json={"lines": [{"qty": 5}]}, headers=_HDR).status_code == 422
    # Supplement reçetesi kozmetik panelde → 400 "Bu panelde değil"
    r = authed_client.post(url, json=_req(sup), headers=_HDR)
    assert r.status_code == 400 and "Bu panelde değil" in r.json()["detail"]
    r = authed_client.post(url, json={"lines": [{"recipe_id": ids["rec"], "qty": 5}],
                                      "options": {"excluded_item_ids": [sup["gli"]]}}, headers=_HDR)
    assert r.status_code == 400 and "Bu panelde değil" in r.json()["detail"]
    # Yalnız reçetesiz satır → hesaplanacak malzeme yok → 400
    r = authed_client.post(url, json={"lines": [{"target_item_id": ids["rl"], "qty": 5}]}, headers=_HDR)
    assert r.status_code == 400 and "reçetesi yok" in r.json()["detail"]
    # Pasif reçete → 400 (senaryo sessizce boş rapor üretmesin)
    db_session.query(Recipe).filter(Recipe.id == ids["rec"]).update({"is_active": False})
    db_session.commit()
    r = authed_client.post(url, json=_req(ids), headers=_HDR)
    assert r.status_code == 400 and "pasif" in r.json()["detail"]


def test_fx_fetched_only_for_foreign_currency(authed_client: TestClient, db_session: Session, monkeypatch):
    import core.fx as fx
    calls = []

    def fake_fetch():
        calls.append(1)
        return {"USD": 40.0, "EUR": 44.0}

    fx.reset_cache()
    monkeypatch.setattr(fx, "_fetch_tcmb_rates", fake_fetch)
    ids = _seed(db_session, price_cur="USD")
    assert authed_client.post("/api/purchase-plan/preview", json=_req(ids), headers=_HDR).status_code == 200
    assert calls == []                                           # USD liste + USD rapor → ağ yok
    r = authed_client.post("/api/purchase-plan/preview", json=_req(ids, currency="EUR"), headers=_HDR)
    assert r.status_code == 200 and calls == [1]
    d = r.json()
    gli = next(m for m in d["materials"] if m["item_id"] == ids["gli"])
    assert gli["price"] == pytest.approx(4.52 * 40 / 44)
    assert d["pricing"]["fx"]["used"] is True and d["pricing"]["symbol"] == "€"
    fx.reset_cache()


# ─── Dışa aktarma ───────────────────────────────────────────────────────────

def test_export_pdf_and_xlsx_with_audit(authed_client: TestClient, db_session: Session):
    ids = _seed(db_session)
    r = authed_client.post("/api/purchase-plan/export?format=pdf", json=_req(ids), headers=_HDR)
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    cd = r.headers["content-disposition"]
    assert cd.startswith("inline;") and "satin_alma_plani_deneme_siparisi_" in cd and ".pdf" in cd
    r = authed_client.post("/api/purchase-plan/export?format=xlsx", json=_req(ids), headers=_HDR)
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert r.headers["content-disposition"].startswith("attachment;")
    rows = _audit(db_session, "purchase_plan.export")
    assert len(rows) == 2 and {'"pdf"' in (a.details or "") for a in rows} == {True, False}
    assert authed_client.post("/api/purchase-plan/export?format=csv", json=_req(ids),
                              headers=_HDR).status_code == 422


# ─── Geçmiş üretimden doldur ────────────────────────────────────────────────

def test_history_fill(authed_client: TestClient, db_session: Session):
    ids = _seed(db_session)
    sup = _seed(db_session, domain="supplement", brand="Takviye")
    now = datetime.utcnow()
    db_session.add_all([
        ProductionHistory(recipe_id=ids["rec"], target_item_id=ids["tgt"], produced_quantity=120,
                          produced_at=now - timedelta(days=3), domain="cosmetics"),
        ProductionHistory(recipe_id=ids["rec"], target_item_id=ids["tgt"], produced_quantity=4,
                          produced_at=now - timedelta(days=1), domain="cosmetics"),
        ProductionHistory(recipe_id=sup["rec"], target_item_id=sup["tgt"], produced_quantity=9,
                          produced_at=now - timedelta(days=1), domain="supplement"),
    ])
    db_session.commit()
    r = authed_client.get("/api/purchase-plan/history-fill")
    assert r.status_code == 200 and r.json() == {str(ids["tgt"]): 124}
    r = authed_client.get("/api/purchase-plan/history-fill?start=2026-05-01&end=2026-01-01")
    assert r.status_code == 400


# ─── Senaryolar ─────────────────────────────────────────────────────────────

def test_scenario_crud_and_audit(authed_client: TestClient, db_session: Session):
    ids = _seed(db_session)
    base = "/api/purchase-plan/scenarios"
    r = authed_client.post(base, json={"name": "  Rusya   Siparişi ", "config": _req(ids)}, headers=_HDR)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["name"] == "Rusya Siparişi" and r.json()["can_edit"] is True
    # Aynı ad (TR-katlanmış, büyük/küçük harf) → 409
    r = authed_client.post(base, json={"name": "RUSYA SİPARİŞİ", "config": _req(ids)}, headers=_HDR)
    assert r.status_code == 409
    lst = authed_client.get(base).json()["scenarios"]
    assert [s["id"] for s in lst] == [sid] and lst[0]["lines"] == 1
    one = authed_client.get(f"{base}/{sid}").json()
    assert one["version"] == 1 and one["missing"] == [] and one["config"]["lines"][0]["qty"] == 100
    assert one["config"]["options"]["stock_mode"] == "net"     # varsayılanlar doldurulmuş
    cfg = _req(ids, qty=250, stock_mode="gross")
    r = authed_client.put(f"{base}/{sid}", json={"name": "Rusya 2", "config": cfg}, headers=_HDR)
    assert r.status_code == 200 and r.json()["name"] == "Rusya 2"
    one = authed_client.get(f"{base}/{sid}").json()
    assert one["config"]["lines"][0]["qty"] == 250 and one["config"]["options"]["stock_mode"] == "gross"
    assert authed_client.put(f"{base}/{sid}", json={}, headers=_HDR).status_code == 400
    assert authed_client.delete(f"{base}/{sid}", headers=_HDR).status_code == 200
    assert authed_client.get(f"{base}/{sid}").status_code == 404
    assert authed_client.get(base).json()["scenarios"] == []
    db_session.expire_all()
    assert db_session.query(PurchasePlan).filter(PurchasePlan.id == sid).one().is_active is False
    # Yumuşak silinenin adı yeniden kullanılabilir (kısmi tekil indeks)
    assert authed_client.post(base, json={"name": "Rusya 2", "config": _req(ids)},
                              headers=_HDR).status_code == 201
    assert len(_audit(db_session, "purchase_plan.create")) == 2
    upd = _audit(db_session, "purchase_plan.update")
    assert len(upd) == 1 and "renamed_from" in (upd[0].details or "")
    assert len(_audit(db_session, "purchase_plan.delete")) == 1


def test_scenario_rename_conflict_and_domain_scope(authed_client: TestClient, db_session: Session):
    ids = _seed(db_session)
    base = "/api/purchase-plan/scenarios"
    a = authed_client.post(base, json={"name": "A", "config": _req(ids)}, headers=_HDR).json()["id"]
    authed_client.post(base, json={"name": "B", "config": _req(ids)}, headers=_HDR)
    assert authed_client.put(f"{base}/{a}", json={"name": "b"}, headers=_HDR).status_code == 409
    # Supplement panelinde kozmetik senaryosu görünmez/değişmez; aynı ad serbest
    authed_client.cookies.set("active_domain", "supplement")
    assert authed_client.get(base).json()["scenarios"] == []
    assert authed_client.get(f"{base}/{a}").status_code == 404
    assert authed_client.delete(f"{base}/{a}", headers=_HDR).status_code == 404
    sup = _seed(db_session, domain="supplement", brand="Takviye")
    assert authed_client.post(base, json={"name": "A", "config": _req(sup)}, headers=_HDR).status_code == 201


def test_scenario_owner_rules(client: TestClient, db_session: Session):
    ids = _seed(db_session)
    base = "/api/purchase-plan/scenarios"
    _login(client, "dogukan")                                    # SuperAdmin
    boss = client.post(base, json={"name": "Patron", "config": _req(ids)}, headers=_HDR).json()["id"]
    _login(client, "meltem")                                     # LabTech — reports.view var
    mine = client.post(base, json={"name": "Meltem", "config": _req(ids)}, headers=_HDR)
    assert mine.status_code == 201
    mine = mine.json()["id"]
    flags = {s["id"]: s["can_edit"] for s in client.get(base).json()["scenarios"]}
    assert flags == {boss: False, mine: True}
    assert client.put(f"{base}/{boss}", json={"name": "X"}, headers=_HDR).status_code == 403
    assert client.delete(f"{base}/{boss}", headers=_HDR).status_code == 403
    assert client.get(f"{base}/{boss}").json()["can_edit"] is False   # okuma serbest
    assert client.put(f"{base}/{mine}", json={"name": "Meltem 2"}, headers=_HDR).status_code == 200
    _login(client, "songul")                                     # LabLead — sahibi değil
    assert client.put(f"{base}/{mine}", json={"name": "Y"}, headers=_HDR).status_code == 403
    _login(client, "isik")                                       # Manager — herkesinkini
    assert client.put(f"{base}/{mine}", json={"name": "Meltem 3"}, headers=_HDR).status_code == 200
    assert client.delete(f"{base}/{mine}", headers=_HDR).status_code == 200


def test_scenario_missing_refs_flagged(authed_client: TestClient, db_session: Session):
    ids = _seed(db_session)
    gone = _item(db_session, "ESKİ KUTU", cat="Ambalaj", unit="adet")
    db_session.commit()
    cfg = {"lines": [{"recipe_id": ids["rec"], "target_item_id": ids["tgt"], "qty": 10,
                      "extra_packaging": [{"item_id": gone.id, "per_unit": 1}]},
                     {"target_item_id": ids["rl"], "qty": 5}],
           "options": {"held_items": [{"item_id": ids["jar"], "reason": "teyit"}]}}
    sid = authed_client.post("/api/purchase-plan/scenarios", json={"name": "Eksikli", "config": cfg},
                             headers=_HDR).json()["id"]
    db_session.query(Recipe).filter(Recipe.id == ids["rec"]).update({"is_active": False})
    db_session.query(Item).filter(Item.id == ids["jar"]).update({"is_active": False})
    db_session.query(Item).filter(Item.id == gone.id).update({"domain": "supplement"})
    db_session.commit()
    d = authed_client.get(f"/api/purchase-plan/scenarios/{sid}").json()
    miss = {(m["field"], m["id"]): m for m in d["missing"]}
    assert miss[("recipe", ids["rec"])]["status"] == "inactive" and miss[("recipe", ids["rec"])]["line"] == 1
    assert miss[("extra_packaging", gone.id)]["status"] == "not_found"
    assert miss[("held", ids["jar"])]["status"] == "inactive"
    assert "1. satırın reçetesi" in miss[("recipe", ids["rec"])]["text"]
    # config olduğu gibi döner — hiçbir satır düşürülmez
    assert len(d["config"]["lines"]) == 2


def test_item_refs_for_draft_restore(authed_client: TestClient, db_session: Session):
    # Taslak geri yüklenirken kart durumu sunucudan: pasif kart "silinmiş" sayılmaz,
    # öteki panelin kartı / silinmiş id not_found olur.
    ids = _seed(db_session)
    passive = _item(db_session, "PASİF KAVANOZ", cat="Ambalaj", unit="adet", active=False)
    foreign = _item(db_session, "SUPP KAPSÜL", cat="Ambalaj", unit="adet", domain="supplement")
    db_session.commit()
    q = f"{ids['gli']},{passive.id},{foreign.id},999999,{passive.id}"
    r = authed_client.get(f"/api/purchase-plan/item-refs?ids={q}")
    assert r.status_code == 200
    miss = {m["id"]: m for m in r.json()["missing"]}
    assert set(miss) == {passive.id, foreign.id, 999999}           # aktif kart listede yok, tekrar yok
    assert miss[passive.id]["status"] == "inactive" and miss[passive.id]["name"] == "PASİF KAVANOZ"
    assert miss[foreign.id]["status"] == "not_found" and miss[foreign.id]["name"] is None
    assert miss[999999]["kind"] == "item"
    assert authed_client.get("/api/purchase-plan/item-refs").json() == {"missing": []}
    big = ",".join(str(i) for i in range(1, 1102))
    assert authed_client.get(f"/api/purchase-plan/item-refs?ids={big}").status_code == 400


def test_preview_with_scenario_stamps_last_run_only(authed_client: TestClient, db_session: Session):
    ids = _seed(db_session)
    sid = authed_client.post("/api/purchase-plan/scenarios", json={"name": "S", "config": _req(ids)},
                             headers=_HDR).json()["id"]
    db_session.expire_all()
    before = db_session.query(PurchasePlan).get(sid).updated_at
    r = authed_client.post(f"/api/purchase-plan/preview?scenario_id={sid}", json=_req(ids), headers=_HDR)
    assert r.status_code == 200
    db_session.expire_all()
    p = db_session.query(PurchasePlan).get(sid)
    assert p.last_run_at is not None and p.updated_at == before


def test_mutations_without_origin_are_403(authed_client: TestClient, db_session: Session):
    ids = _seed(db_session)
    r = authed_client.post("/api/purchase-plan/scenarios", json={"name": "X", "config": _req(ids)})
    assert r.status_code == 403
    sid = authed_client.post("/api/purchase-plan/scenarios", json={"name": "X", "config": _req(ids)},
                             headers=_HDR).json()["id"]
    assert authed_client.delete(f"/api/purchase-plan/scenarios/{sid}").status_code == 403
    assert authed_client.post("/api/purchase-plan/preview", json=_req(ids)).status_code == 403


def test_endpoints_require_login(client: TestClient):
    assert client.get("/api/purchase-plan/products").status_code == 401
    assert client.get("/api/purchase-plan/scenarios").status_code == 401
    assert client.get("/api/purchase-plan/item-refs?ids=1").status_code == 401


def test_default_excluded_from_appsetting_drops_water(authed_client: TestClient, db_session: Session):
    """Varsayılan hariç (AppSetting yoksa ad kuralı) ürünler listesinde ve hesapta aynı."""
    ids = _seed(db_session)
    water = _item(db_session, "SAF SU", unit="ml", stock=0)
    rec = db_session.query(Recipe).get(ids["rec"])
    db_session.add(RecipeIngredient(recipe_id=rec.id, item_id=water.id, quantity=50, unit="ml"))
    db_session.commit()
    d = authed_client.post("/api/purchase-plan/preview", json=_req(ids), headers=_HDR).json()
    assert water.id not in {m["item_id"] for m in d["materials"]}
    assert [e["item_id"] for e in d["excluded"]] == [water.id]
    # Panel anahtarıyla açıkça "hariç yok" → su listeye girer
    db_session.add(AppSetting(key="purchase_plan.default_excluded.cosmetics", value="[]"))
    db_session.commit()
    assert authed_client.get("/api/purchase-plan/products").json()["defaults"]["excluded_items"] == []
    d = authed_client.post("/api/purchase-plan/preview", json=_req(ids), headers=_HDR).json()
    assert water.id in {m["item_id"] for m in d["materials"]}


# ─── Sayfa ──────────────────────────────────────────────────────────────────

def test_satin_alma_page_renders(authed_client: TestClient):
    r = authed_client.get("/satin-alma")
    assert r.status_code == 200
    html = r.text
    assert "/static/satin-alma.js" in html and "Satın Alma Planı" in html
    assert 'href="/satin-alma" class="nav-link-item active"' in html
    assert authed_client.get("/static/satin-alma.js").status_code == 200


def test_satin_alma_page_requires_login(client: TestClient):
    r = client.get("/satin-alma", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"


def test_reports_page_links_to_satin_alma(authed_client: TestClient):
    html = authed_client.get("/reports").text
    assert 'href="/satin-alma"' in html and "Gelişmiş: Satın Alma Planı" in html
