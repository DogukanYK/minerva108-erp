"""
Üretim Stok Analizi (what-if simülasyonu) testleri.

  • simulate(): fire dahil brüt tüketim, ambalaj fire muaf, kalan/eksik,
    tek-başına üretilebilir adet — sentetik veriyle birebir doğrulama
  • Endpoint'ler: ürün listesi · JSON rapor · Excel export
  • Domain izolasyonu: supplement reçetesi cosmetics panelinde görünmez
"""
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from database import Item, Recipe, RecipeIngredient
from core.production_sim import simulate, list_plan_products

_HDR = {"Origin": "http://testserver"}


def _recipe(db, *, brand="Serenida", waste=10, raw_qty=10, raw_stock=1000,
            amb_qty=1, amb_stock=1000, domain="cosmetics"):
    raw = Item(name=f"{brand} Su", sku=f"raw-{brand}", category="Hammadde", unit="ml",
               current_stock=raw_stock, domain=domain)
    amb = Item(name=f"{brand} Şişe", sku=f"amb-{brand}", category="Ambalaj", unit="adet",
               current_stock=amb_stock, domain=domain)
    tgt = Item(name=f"{brand} Krem Ürün", sku=f"tgt-{brand}", category="Bitmiş Ürün",
               unit="adet", current_stock=0, domain=domain)
    db.add_all([raw, amb, tgt]); db.flush()
    rec = Recipe(name=tgt.name, output_quantity=1, output_unit="adet",
                 target_item_id=tgt.id, waste_percentage=waste, domain=domain)
    db.add(rec); db.flush()
    db.add(RecipeIngredient(recipe_id=rec.id, item_id=raw.id, quantity=raw_qty, unit="ml"))
    db.add(RecipeIngredient(recipe_id=rec.id, item_id=amb.id, quantity=amb_qty, unit="adet"))
    db.commit()
    return rec.id


def test_simulate_fire_and_ambalaj_exempt(db_session: Session):
    rid = _recipe(db_session, waste=10, raw_qty=10, raw_stock=1000, amb_qty=1, amb_stock=1000)
    rep = simulate(db_session, [rid], 100, "TR", "cosmetics")
    mats = {m["name"]: m for m in rep["materials"]}
    # Hammadde: 10 × 100 × 1.10 = 1100 → kalan 1000−1100 = −100 → eksik 100
    su = mats["Serenida Su"]
    assert su["used"] == 1100 and su["remaining"] == -100 and su["shortfall"] == 100
    assert su["status"] == "YETERSİZ"
    # Ambalaj: fire MUAF → 1 × 100 × 1.0 = 100 → kalan 900
    sise = mats["Serenida Şişe"]
    assert sise["used"] == 100 and sise["remaining"] == 900 and sise["status"] == "Yeterli"
    assert rep["summary"]["short_count"] == 1


def test_simulate_producible_in_isolation(db_session: Session):
    # raw: 1 adet için 10×1.1 = 11 ml gerek; 1000 stok → floor(1000/11)=90
    rid = _recipe(db_session, waste=10, raw_qty=10, raw_stock=1000, amb_stock=100000)
    rep = simulate(db_session, [rid], 250, "TR", "cosmetics")
    p = rep["producible"][0]
    assert p["producible"] == 90 and p["limiting"] == "Serenida Su"


def test_plan_products_endpoint_lists_brand(authed_client: TestClient, db_session: Session):
    _recipe(db_session, brand="Serenida")
    r = authed_client.get("/api/reports/production-plan/products")
    assert r.status_code == 200
    prods = r.json()["products"]
    assert any(p["brand"] == "Serenida" for p in prods)


def test_plan_report_endpoint(authed_client: TestClient, db_session: Session):
    rid = _recipe(db_session)
    r = authed_client.post("/api/reports/production-plan",
                           json={"recipe_ids": [rid], "quantity": 100, "language": "TR"}, headers=_HDR)
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["products"] == 1 and body["summary"]["total_units"] == 100
    assert len(body["materials"]) == 2


def test_plan_export_xlsx(authed_client: TestClient, db_session: Session):
    rid = _recipe(db_session)
    r = authed_client.post("/api/reports/production-plan/export",
                           json={"recipe_ids": [rid], "quantity": 100, "language": "TR"}, headers=_HDR)
    assert r.status_code == 200
    assert r.content[:2] == b"PK"          # xlsx = zip


def test_plan_domain_isolation(authed_client: TestClient, db_session: Session):
    cos = _recipe(db_session, brand="Serenida", domain="cosmetics")
    sup = _recipe(db_session, brand="Takviye", domain="supplement")
    # cosmetics panelinde (varsayılan) supplement ürünü listede YOK
    prods = authed_client.get("/api/reports/production-plan/products").json()["products"]
    brands = {p["brand"] for p in prods}
    assert "Serenida" in brands and "Takviye" not in brands
    # supplement reçete id'siyle cosmetics'te simülasyon → boş (domain filtre)
    r = authed_client.post("/api/reports/production-plan",
                           json={"recipe_ids": [sup], "quantity": 100, "language": "TR"}, headers=_HDR)
    assert r.status_code == 200 and r.json()["summary"]["products"] == 0


def test_recipeless_products_listed(db_session: Session):
    """Reçetesi olmayan bitmiş ürün recipeless'ta çıkar; reçeteli olan çıkmaz."""
    from core.production_sim import list_recipeless_products
    _recipe(db_session, brand="Serenida")   # reçeteli bitmiş ürün: 'Serenida Krem Ürün'
    db_session.add(Item(name="Minerva Foot Care Cream", sku="x-foot", category="Bitmiş Ürün",
                        unit="adet", current_stock=24, domain="cosmetics"))
    db_session.commit()
    names = [p["name"] for p in list_recipeless_products(db_session, "cosmetics")]
    assert "Minerva Foot Care Cream" in names
    assert "Serenida Krem Ürün" not in names      # reçetesi var → listede yok
    # başka panelde görünmez (domain-kapsamlı)
    assert "Minerva Foot Care Cream" not in [p["name"] for p in list_recipeless_products(db_session, "supplement")]


def test_products_endpoint_includes_recipeless(authed_client: TestClient, db_session: Session):
    db_session.add(Item(name="Reçetesiz Ürün X", sku="x-rl", category="Bitmiş Ürün",
                        unit="adet", current_stock=5, domain="cosmetics"))
    db_session.commit()
    d = authed_client.get("/api/reports/production-plan/products").json()
    assert "recipeless" in d
    assert any(p["name"] == "Reçetesiz Ürün X" for p in d["recipeless"])


def test_producible_never_negative_with_negative_stock(db_session: Session):
    """Negatif stoklu kalem kapasiteyi 0'a çeker — eskiden floor(−50/11) = −5 çıkıyordu."""
    rid = _recipe(db_session, waste=10, raw_qty=10, raw_stock=-50, amb_stock=1000)
    rep = simulate(db_session, [rid], 100, "TR", "cosmetics")
    p = rep["producible"][0]
    assert p["producible"] == 0 and p["limiting"] == "Serenida Su"
    assert p["limit_stock"] == -50                   # gösterimde gerçek stok kalır


def test_plan_products_brand_chip_merges_variants(authed_client: TestClient, db_session: Session):
    """'MİNERVA …' / 'Minerva108 …' hedefli reçeteler tek 'Minerva 108' çipine düşer."""
    _recipe(db_session, brand="MİNERVA")
    _recipe(db_session, brand="Minerva108")
    prods = authed_client.get("/api/reports/production-plan/products").json()["products"]
    brands = {p["brand"] for p in prods}
    assert brands == {"Minerva 108"}
    assert len(prods) == 2
