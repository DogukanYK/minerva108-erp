"""Minerva Asistan — operasyon araçları (core/assistant/handlers_ops.py, 13.10.2026).

Kilitler:
  • Her araç sistemdeki uçla AYNI izni ister (`tools.run_tool` denetler, DB açılmaz).
  • Domain kolonu olan tablolar aktif panelle süzülür.
  • Kişisel İLETİŞİM verisi (telefon, e-posta, adres, VKN) hiçbir araçtan çıkmaz; iade
    eden / formu dolduran / creator / yorumcu kimliği hiç çıkmaz (kullanıcı kararı).
  • CRM ve PDKS araçları kaldırıldı (bkz. test_assistant_tools).
Yalnız izole test DB'si; sentetik adlar.
"""
import datetime as dt
import json

import bcrypt
import pytest

from core.assistant.tools import REGISTRY, ToolContext, run_tool
from database import (Delivery, DeliveryItem, InfluencerCampaign, InfluencerCollab, InfluencerContent,
                      InfluencerCreator, InfluencerShipment, Inventory, Item, ProductReturn, ProductReturnItem,
                      ProductReview, Quotation, QuotationItem, Recipe, RecipeIngredient, RetentionSample,
                      ReviewInvite, SampleAnalysis, SampleAnalysisIngredient, ShopifyOrder, Transaction,
                      TrendyolLine, TrendyolPackage, User)


def _user(db, username, role, permissions=None):
    u = User(username=username, full_name=username.title() + " Test", role=role, is_active=True,
             password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode(),
             permissions=json.dumps(permissions) if permissions is not None else None)
    db.add(u)
    db.commit()
    return u


def _ctx(db, role="LabLead", permissions=None, domain="cosmetics", username=None):
    u = _user(db, username or f"ops_{role.lower()}_{dt.datetime.utcnow().timestamp():.6f}", role, permissions)
    return ToolContext.from_user(u, domain, db)


def _sa(db, domain="cosmetics"):
    return ToolContext.from_user(db.query(User).filter_by(username="dogukan").one(), domain, db)


def _card(db, name, category="Hammadde", unit="kg", stock=0.0, domain="cosmetics"):
    it = Item(name=name, category=category, unit=unit, current_stock=stock, domain=domain, is_active=True)
    db.add(it)
    db.flush()
    return it


def _run(ctx, name, args):
    res = run_tool(ctx, name, args)
    return res, json.loads(res.content)


SECRETS = ("0532 111 22 33", "gizli@ornek.com", "Gizli Sokak No:7", "1234567890")


def _no_secrets(content: str):
    for s in SECRETS:
        assert s not in content, s


def test_ops_tools_require_the_same_permission_as_the_system():
    want = {
        "deliveries": (("inventory", "view"),), "product_returns": (("inventory", "view"),),
        "b2b_orders": (("b2b_orders", "view"),), "quotations": (("b2b", "view"),),
        "qc_pending": (("qc", "view"),), "outsourcing_jobs": (("outsourcing", "view"),),
        "stock_movements": (("inventory", "view"), ("reports", "view")),
        "consumption": (("reports", "view"),), "month_end_stock": (("reports", "view"),),
        "production_capacity": (("reports", "view"),), "retention_samples": (("retention", "view"),),
        "sample_analyses": (("qc", "view"),), "trendyol_orders": (("trendyol", "view"),),
        "shopify_orders": (("admin", "view"),), "influencer_overview": (("influencer", "view"),),
        "reviews_overview": (("reviews", "view"),),
    }
    for name, perms in want.items():
        assert REGISTRY[name].perms == perms, name


def test_deliveries_queue_filters_superadmin_pending_and_no_contacts(db_session):
    db = db_session
    cream = _card(db, "Ops Krem 50 ml", "Bitmiş Ürün", "adet", 20)
    now = dt.datetime.utcnow()

    def _dlv(no, dtype, status, method="kargo", domain="cosmetics", shipped=None, org=None):
        d = Delivery(document_no=no, recipient_name="Ayşe Alıcı", recipient_org=org,
                     recipient_phone=SECRETS[0], customer_address=SECRETS[2], customer_country="Türkiye",
                     delivery_type=dtype, method=method, status=status, domain=domain,
                     tracking_no="TRK-1" if shipped else None, carrier="Yurtiçi" if shipped else None,
                     shipped_at=shipped, created_at=now)
        db.add(d)
        db.flush()
        db.add(DeliveryItem(delivery_id=d.id, item_id=cream.id, item_name=cream.name, quantity=3, unit="adet",
                            unit_price=9.5))
        return d

    _dlv("TES-OPS-1", "kargo", "preparing", org="Ops Bayi Ltd")
    _dlv("TES-OPS-2", "kargo", "shipped", shipped=now - dt.timedelta(days=2))
    _dlv("PRF-OPS-3", "proforma", "pending", org="Ops Export GmbH")
    _dlv("TES-OPS-4", "kargo", "preparing", domain="supplement")
    db.commit()

    lab = _ctx(db, "LabLead")
    res, p = _run(lab, "deliveries", {"status": "kargo_bekleyen", "q": None, "limit": None})
    assert not res.is_error and p["filter"] == "kargo_bekleyen"
    assert [r["document_no"] for r in p["rows"]] == ["TES-OPS-1"]            # supplement paneli yok
    row = p["rows"][0]
    assert row["recipient"] == "Ops Bayi Ltd" and row["items"] == [{"name": cream.name, "qty": 3.0, "unit": "adet"}]
    assert p["summary"] == {"kargo_bekleyen": 1, "kargolanan_son_30_gun": 1}   # proforma sayısı SA'ya özel
    assert "unit_price" not in res.content and "9.5" not in res.content
    _no_secrets(res.content)
    assert res.links == [{"label": "Teslimat", "href": "/delivery"}]
    assert res.summary == "1 kargo bekliyor · 1 kayıt"
    res, p = _run(lab, "deliveries", {"status": "kargolanan", "q": None, "limit": None})
    assert [r["document_no"] for r in p["rows"]] == ["TES-OPS-2"] and p["rows"][0]["tracking_no"] == "TRK-1"
    res, p = _run(lab, "deliveries", {"status": None, "q": "ops krem", "limit": 2})
    assert len(p["rows"]) == 2 and p["count"] == 3
    res, p = _run(lab, "deliveries", {"status": "onay_bekleyen", "q": None, "limit": None})
    assert res.is_error and "SuperAdmin" in p["error"]
    sa = _sa(db)
    res, p = _run(sa, "deliveries", {"status": "onay_bekleyen", "q": None, "limit": None})
    assert [r["document_no"] for r in p["rows"]] == ["PRF-OPS-3"] and p["summary"]["onay_bekleyen_proforma"] == 1
    _no_secrets(res.content)
    # inventory.view olmayan hesap: DB'ye hiç dokunmadan reddedilir
    nobody = _ctx(db, "Staff", {"items": {"view": True}})
    res, p = _run(nobody, "deliveries", {"status": None, "q": None, "limit": None})
    assert res.is_error and p["permission"] == "inventory.view"


def test_returns_quotations_and_qc_pending(db_session):
    db = db_session
    cream = _card(db, "Ops Tonik 100 ml", "Bitmiş Ürün", "adet", 5)
    ret = ProductReturn(document_no="IAD-OPS-1", delivery_document_no="TES-OPS-9", channel="teslimat",
                        reason="Kutu ezik geldi", returned_by="Mehmet Müşteri", received_by="Depo Personeli",
                        domain="cosmetics")
    db.add(ret)
    db.flush()
    db.add_all([ProductReturnItem(return_id=ret.id, item_id=cream.id, item_name=cream.name, quantity=2,
                                  unit="adet", condition="saglam", restocked=True),
                ProductReturnItem(return_id=ret.id, item_id=cream.id, item_name=cream.name, quantity=1,
                                  unit="adet", condition="hasarli", restocked=False)])
    q1 = Quotation(quote_number="TKL-OPS-1", customer_name="Ops Müşteri GmbH", customer_contact="Hans Kişi",
                   customer_email=SECRETS[1], customer_phone=SECRETS[0], customer_address=SECRETS[2],
                   customer_vat=SECRETS[3], customer_country="Germany", currency="EUR", total_amount=1250.0,
                   status="PENDING", domain="cosmetics")
    q2 = Quotation(quote_number="TKL-OPS-2", customer_name="Başka Panel AŞ", currency="USD", total_amount=10,
                   status="PENDING", domain="supplement")
    db.add_all([q1, q2])
    db.flush()
    db.add(QuotationItem(quotation_id=q1.id, item_id=cream.id, quantity=100, unit_price_foreign=12.5,
                         line_total=1250.0))
    raw = _card(db, "Ops Gliserin", "Hammadde", "kg", 10)
    old = dt.datetime.utcnow() - dt.timedelta(days=5)
    db.add_all([Inventory(item_id=raw.id, lot_number="KR-OPS-1", quantity=4, status="QUARANTINE",
                          qc_required=False, domain="cosmetics", created_at=old),
                Inventory(item_id=cream.id, lot_number="URT-OPS-1", quantity=12, status="APPROVED",
                          qc_required=True, domain="cosmetics"),
                Inventory(item_id=raw.id, lot_number="ONAYLI-OPS", quantity=4, status="APPROVED",
                          qc_required=False, domain="cosmetics")])
    db.commit()

    sa = _sa(db)
    res, p = _run(sa, "product_returns", {"q": "ops tonik", "limit": None})
    assert not res.is_error and p["count"] == 1
    r = p["rows"][0]
    assert r["document_no"] == "IAD-OPS-1" and r["restocked_qty"] == 2.0 and r["damaged_qty"] == 1.0
    assert r["channel"] == "Teslimat dönüşü" and r["reason"] == "Kutu ezik geldi"
    assert "Mehmet Müşteri" not in res.content and "Depo Personeli" not in res.content

    res, p = _run(sa, "quotations", {"status": "PENDING", "q": None})
    assert [x["quote_number"] for x in p["rows"]] == ["TKL-OPS-1"]               # panel süzgeci
    x = p["rows"][0]
    assert x["customer"] == "Ops Müşteri GmbH" and x["total"] == 1250.0 and x["items"] == 1
    assert "Hans Kişi" not in res.content
    _no_secrets(res.content)
    lab = _ctx(db, "LabLead")                                                   # b2b.view yok
    res, p = _run(lab, "quotations", {"status": None, "q": None})
    assert res.is_error and p["permission"] == "b2b.view"
    res, p = _run(sa, "quotations", {"status": "BOGUS", "q": None})
    assert res.is_error

    res, p = _run(lab, "qc_pending", {})
    assert not res.is_error and p["count"] == 2 and p["by_source"] == {"Mal Kabul": 1, "Üretim": 1}
    assert p["rows"][0]["lot"] == "KR-OPS-1" and p["rows"][0]["days_waiting"] >= 4
    assert "ONAYLI-OPS" not in res.content and res.links[0]["href"] == "/qc"


def test_stock_movements_consumption_and_month_end_stock(db_session):
    db = db_session
    oil = _card(db, "Ops Badem Yağı", "Hammadde", "kg", 40)
    wax = _card(db, "Ops Balmumu", "Hammadde", "kg", 5)
    other = _card(db, "Ops Takviye Tozu", "Hammadde", "kg", 3, domain="supplement")
    now = dt.datetime.utcnow()
    db.add_all([
        Transaction(item_id=oil.id, transaction_type="Input", quantity=50, timestamp=now - dt.timedelta(days=3),
                    lot_number="BY-1", notes="Mal kabul — Lot: BY-1", performed_by="Gizli Kişi"),
        Transaction(item_id=oil.id, transaction_type="Output", quantity=12, timestamp=now - dt.timedelta(days=2),
                    lot_number="BY-1", notes="Üretim tüketimi — Reçete: X", performed_by="Gizli Kişi"),
        Transaction(item_id=oil.id, transaction_type="Adjustment", quantity=2, timestamp=now - dt.timedelta(days=1),
                    notes="Stok düzeltme", performed_by="Gizli Kişi"),
        Transaction(item_id=wax.id, transaction_type="Output", quantity=4, timestamp=now - dt.timedelta(days=1)),
        Transaction(item_id=oil.id, transaction_type="Output", quantity=99, timestamp=now - dt.timedelta(days=90)),
        Transaction(item_id=other.id, transaction_type="Output", quantity=500, timestamp=now - dt.timedelta(days=1)),
    ])
    db.commit()
    lab = _ctx(db, "LabLead")
    res, p = _run(lab, "stock_movements", {"item_id": oil.id, "start": None, "end": None})
    assert not res.is_error and p["count"] == 3                                  # 90 gün önceki dönem dışı
    assert p["totals_by_type"] == {"Input": 50.0, "Output": 12.0, "Adjustment": 2.0}
    assert p["net_change"] == 40.0 and p["rows"][0]["type"] == "Adjustment"
    assert "Gizli Kişi" not in res.content and res.summary == "3 hareket · net 40"
    res, p = _run(lab, "stock_movements", {"item_id": other.id, "start": None, "end": None})
    assert res.is_error and "panelde" in p["error"]                             # başka panelin kartı
    res, p = _run(lab, "stock_movements", {"item_id": oil.id, "start": "2026-13-01", "end": None})
    assert res.is_error

    res, p = _run(lab, "consumption", {"start": None, "end": None, "limit": None})
    assert [(r["name"], r["used"]) for r in p["rows"]] == [("Ops Badem Yağı", 12.0), ("Ops Balmumu", 4.0)]
    ninety = (dt.date.today() - dt.timedelta(days=100)).isoformat()
    _, p = _run(lab, "consumption", {"start": ninety, "end": None, "limit": 1})
    assert p["rows"] == [{"item_id": oil.id, "name": "Ops Badem Yağı", "unit": "kg", "used": 111.0}]

    res, p = _run(lab, "month_end_stock", {"year": None, "month": None, "category": "hammadde", "q": "badem"})
    assert not res.is_error and p["source"] == "canlı"
    sec = p["sections"][0]
    assert sec["rows"] == [{"name": "Ops Badem Yağı", "stock": 40.0, "unit": "kg"}] and sec["total"] == 40.0
    res, p = _run(lab, "month_end_stock", {"year": None, "month": None, "category": "kozmik", "q": None})
    assert res.is_error
    staff = _ctx(db, "Staff", {"inventory": {"view": True}})                     # reports.view yok
    res, p = _run(staff, "consumption", {"start": None, "end": None, "limit": None})
    assert res.is_error and p["permission"] == "reports.view"
    res, p = _run(staff, "stock_movements", {"item_id": oil.id, "start": None, "end": None})
    assert not res.is_error                                                      # inventory.view yeterli


def test_production_capacity_reports_shortages_without_recipe_breakdown(db_session):
    db = db_session
    base = _card(db, "Ops Krem Bazı", "Hammadde", "g", 300)
    jar = _card(db, "Ops Kavanoz 50 ml", "Ambalaj", "adet", 1000)
    cream = _card(db, "Ops Gece Kremi 50 ml", "Bitmiş Ürün", "adet", 0)
    cream2 = _card(db, "Ops Gündüz Kremi 50 ml", "Bitmiş Ürün", "adet", 0)
    for target, name in [(cream, "Ops Gece Kremi Reçetesi"), (cream2, "Ops Gündüz Kremi Reçetesi")]:
        rec = Recipe(name=name, target_item_id=target.id, output_quantity=1, output_unit="adet",
                     waste_percentage=0, domain="cosmetics", is_active=True)
        db.add(rec)
        db.flush()
        db.add_all([RecipeIngredient(recipe_id=rec.id, item_id=base.id, quantity=45, unit="g"),
                    RecipeIngredient(recipe_id=rec.id, item_id=jar.id, quantity=1, unit="adet")])
    db.commit()
    lab = _ctx(db, "LabLead")
    res, p = _run(lab, "production_capacity", {"product": "gece kremi", "quantity": 10})
    assert not res.is_error and p["product"] == cream.name and p["quantity"] == 10
    assert p["producible_now"] == 6 and p["limiting_material"] == base.name
    assert p["short_materials"] == [{"name": base.name, "category": "Hammadde", "unit": "g",
                                     "in_stock": 300.0, "shortfall": 150.0}]
    assert "per_unit" not in res.content and '"used"' not in res.content
    res, p = _run(lab, "production_capacity", {"product": "ops", "quantity": 10})
    assert p["ambiguous"] is True and set(p["candidates"]) == {cream.name, cream2.name}
    res, p = _run(lab, "production_capacity", {"product": "yok böyle ürün", "quantity": 5})
    assert res.is_error
    res, p = _run(lab, "production_capacity", {"product": "gece kremi", "quantity": 0})
    assert res.is_error


def test_retention_samples_and_sample_analyses(db_session):
    db = db_session
    cream = _card(db, "Ops Şampuan 200 ml", "Bitmiş Ürün", "adet", 0)
    raw = _card(db, "Ops Kokamid", "Hammadde", "kg", 5)
    past = dt.date.today() - dt.timedelta(days=3)
    db.add_all([
        RetentionSample(item_id=cream.id, item_name=cream.name, lot_number="SR-OPS-1", brand="Serenida",
                        quantity=2, initial_quantity=2, unit="adet", shelf="A", slot="3", status="stored",
                        retention_until=past, domain="cosmetics"),
        RetentionSample(item_id=cream.id, item_name=cream.name, lot_number="SR-OPS-2", brand="Serenida",
                        quantity=1, initial_quantity=1, unit="adet", status="stored",
                        retention_until=dt.date.today() + dt.timedelta(days=300), domain="cosmetics"),
    ])
    an = SampleAnalysis(bulk_name="Ops Şampuan Bulk", lot_number="NA-LOT-1", recipe_name="Ops Şampuan Reçetesi",
                        mode="existing", result="uygun", result_text="Viskozite uygun", created_by="Gizli Kişi",
                        document_no="NA-2026-00001", domain="cosmetics", is_active=True)
    db.add(an)
    db.flush()
    db.add(SampleAnalysisIngredient(analysis_id=an.id, item_id=raw.id, item_name=raw.name, unit="kg",
                                    source="sample", supplier_name="Ops Alternatif Kimya", quantity=0.5))
    db.commit()
    sa = _sa(db)
    res, p = _run(sa, "retention_samples", {"q": None, "brand": None, "status": None, "expired": True})
    assert not res.is_error and p["count"] == 1 and p["rows"][0]["lot"] == "SR-OPS-1"
    assert p["rows"][0]["location"] and res.links[0]["href"] == "/sahit-numune"
    _, p = _run(sa, "retention_samples", {"q": "SR-OPS", "brand": "Serenida", "status": None, "expired": None})
    assert p["count"] == 2
    res, p = _run(sa, "sample_analyses", {"q": "kokamid"})
    assert not res.is_error and p["count"] == 1
    a = p["rows"][0]
    assert a["document_no"] == "NA-2026-00001" and a["suppliers"] == ["Ops Alternatif Kimya"]
    assert a["ingredients"] == 1 and "Gizli Kişi" not in res.content
    staff = _ctx(db, "Staff", {"inventory": {"view": True}})
    res, p = _run(staff, "retention_samples", {"q": None, "brand": None, "status": None, "expired": None})
    assert res.is_error and p["permission"] == "retention.view"


def test_marketplace_influencer_and_reviews_never_leak_people(db_session):
    db = db_session
    now = dt.datetime.utcnow()
    pkg = TrendyolPackage(package_id=91001, order_number="TY-OPS-1", status="Delivered", order_date=now,
                          total_price=349.9, currency="TRY", cargo_provider="Trendyol Express",
                          cargo_tracking="7340001")
    db.add(pkg)
    db.flush()
    db.add(TrendyolLine(package_row_id=pkg.id, line_id=1, barcode="8680000000011", product_name="Ops Serum 30 ml",
                        quantity=2, unit_price=174.95, commission_rate=19.5))
    db.add(ShopifyOrder(store_key="serenida", shopify_order_id=55501, order_number="#1001", status="done",
                        total=59.0, currency="USD", country="US", customer_email=SECRETS[1],
                        customer_name="Jane Buyer", vkn_tckn=SECRETS[3],
                        lines_json=json.dumps({"lines": [{"title": "Ops Body Lotion", "quantity": 1, "sku": "SR-1"}],
                                               "address": SECRETS[2], "phone": SECRETS[0], "city": "Austin"})))
    creator = InfluencerCreator(slug="ops-creator", full_name="Gizli Creator", email=SECRETS[1], phone=SECRETS[0])
    camp = InfluencerCampaign(name="Ops Yaz Kampanyası", store_key="serenida", market="TR", status="aktif")
    db.add_all([creator, camp])
    db.flush()
    col = InfluencerCollab(code="IC-OPS-1", creator_id=creator.id, campaign_id=camp.id, store_key="serenida",
                           stage="kargoda")
    db.add(col)
    db.flush()
    db.add_all([InfluencerShipment(collab_id=col.id), InfluencerContent(collab_id=col.id, status="taslak")])
    inv = ReviewInvite(token="tok-ops-1", kind="gifted", store_key="serenida", shopify_product_id=777,
                       product_title="Ops Body Lotion", recipient_email=SECRETS[1], recipient_name="Davetli Kişi",
                       expires_at=now + dt.timedelta(days=30))
    db.add(inv)
    db.flush()
    for rating, status, published, author in [(5, "approved", True, "Yorumcu Bir"),
                                              (3, "approved", True, "Yorumcu İki"),
                                              (1, "pending", False, "Yorumcu Üç")]:
        db.add(ProductReview(invite_id=inv.id, store_key="serenida", shopify_product_id=777,
                             product_title="Ops Body Lotion", source="gifted", author_name=author,
                             author_email=SECRETS[1], rating=rating, body=f"{rating} yıldız deneyim",
                             status=status, is_published=published))
    db.commit()
    sa = _sa(db)
    res, p = _run(sa, "trendyol_orders", {"start": None, "end": None, "status": None, "q": None})
    assert not res.is_error and p["total"] == 1
    o = p["rows"][0]
    assert o["order_number"] == "TY-OPS-1" and o["lines"][0]["product"] == "Ops Serum 30 ml"
    res, p = _run(sa, "shopify_orders", {"status": None, "limit": None})
    assert not res.is_error and p["rows"][0]["lines"] == [{"title": "Ops Body Lotion", "qty": 1, "sku": "SR-1"}]
    assert "Jane Buyer" not in res.content and "Austin" not in res.content
    _no_secrets(res.content)
    res, p = _run(sa, "influencer_overview", {})
    assert not res.is_error and p["campaigns"][0]["campaign"] == "Ops Yaz Kampanyası"
    assert p["campaigns"][0]["collabs_by_stage"] == {"Kargoda": 1}
    assert p["campaigns"][0]["shipments"] == 1 and p["campaigns"][0]["contents"] == 1
    assert "Gizli Creator" not in res.content and "ops-creator" not in res.content
    _no_secrets(res.content)
    res, p = _run(sa, "reviews_overview", {"q": "body lotion"})
    assert not res.is_error and p["pending_moderation"] == 1
    prod = p["products"][0]
    assert prod["published"] == 2 and prod["avg_rating"] == 4.0 and prod["pending"] == 1
    assert len(prod["latest"]) == 2 and "1 yıldız" not in res.content            # bekleyen yorum metni yok
    for who in ("Yorumcu", "Davetli Kişi"):
        assert who not in res.content
    _no_secrets(res.content)
    lab = _ctx(db, "LabLead")
    for tool, perm in [("shopify_orders", "admin.view"), ("trendyol_orders", "trendyol.view")]:
        res, p = _run(lab, tool, {"status": None, "limit": None} if tool == "shopify_orders"
                      else {"start": None, "end": None, "status": None, "q": None})
        assert res.is_error and p["permission"] == perm, tool
