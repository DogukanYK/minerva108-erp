"""B2B sipariş akışı uçtan uca: teklif → sipariş → teknik değerlendirme →
yönetim onayı (şifre + çizilen imza) → proforma → ödeme / hazırlık → iki
aşamalı parti üretimi → QC → tek sevkiyat.

Yalnız tests/conftest.py'nin izole test DB'si; sentetik müşteri/malzeme adları.
Kilitlenen sözleşmeler (spec §5): eksik/eksiksiz sipariş, koşullu ödeme,
revizyon → yeniden onay, şifre + imza zorunlu, yetkisiz kullanıcı / başka panel
engeli, ortak malzeme toplamı, tekrar/eşzamanlı başlat-tamamla-sevk tek stok
hareketi, başlatınca bitmiş stok yok, tamamlayınca ikinci malzeme düşümü yok,
şahit ayrımı, QC reddi, QC bitmeden sevk yok, eski proforma değişmez, legacy
"Onayla & stoktan düş" ORDER teklifte kapalı, yetki backfill + banka tohumu.
"""
import base64
import json
import threading
import time
from datetime import date, datetime, timedelta
from io import BytesIO

import bcrypt
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from pypdf import PdfReader

from core import b2b_orders as b2b
from core.snapshots import compute_stock_at
from database import (AppSetting, B2BOrder, B2BOrderBatch, B2BOrderPurchaseLine, BankProfile,
                      Delivery, Inventory, Item, ProductionConsumption, ProductionHistory,
                      Quotation, Recipe, RecipeIngredient, RetentionSample, SessionLocal,
                      Supplier, Transaction, User)

HDR = {"Origin": "http://testserver"}
API = "/api/b2b-orders"
FUTURE = (date.today() + timedelta(days=700)).isoformat()
PAST = (date.today() - timedelta(days=3)).isoformat()


def _png(width=240, height=90):
    img = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    ImageDraw.Draw(img).line([(10, 70), (60, 15), (120, 75), (230, 20)], fill=(20, 20, 60, 255), width=4)
    buf = BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


SIG = _png()


# ─── Kurulum ─────────────────────────────────────────────────────────────────

def _login(username, password="minerva123"):
    from api_main import app
    c = TestClient(app)
    r = c.post("/api/login", json={"username": username, "password": password}, headers=HDR)
    assert r.status_code == 200, r.text
    return c


def _user(db, username, role, permissions=None):
    u = User(username=username, full_name=username.title(), role=role, is_active=True,
             password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode(),
             permissions=json.dumps(permissions) if permissions is not None else None)
    db.add(u)
    db.commit()
    return u


def _uid(db, username):
    return db.query(User).filter_by(username=username).one().id


@pytest.fixture
def world(client, db_session):
    """dogukan (SuperAdmin) siparişi açar, songul (LabLead) teknik değerlendirir
    ve üretir, isik (Manager) imzalar / ödemeyi doğrular, meltem (LabTech)
    yalnız üretim görünümü."""
    db = db_session
    sup = Supplier(name="Sentetik Tedarikçi B2B", domain="cosmetics")
    db.add(sup)
    db.flush()
    raw = Item(name="Sentetik Baz Yağ", sku="B2B-RAW", unit="g", category="Hammadde",
               current_stock=500, supplier_id=sup.id, domain="cosmetics")
    bottle = Item(name="Sentetik Şişe 50 ml", sku="B2B-SISE", unit="adet", category="Ambalaj",
                  pkg_type="şişe", current_stock=100, domain="cosmetics")
    label_tr = Item(name="Sentetik Etiket TR", sku="B2B-ET-TR", unit="adet", category="Ambalaj",
                    pkg_type="etiket", language="TR", label_group="B2B-TEST", current_stock=100,
                    domain="cosmetics")
    label_en = Item(name="Sentetik Etiket EN", sku="B2B-ET-EN", unit="adet", category="Ambalaj",
                    pkg_type="etiket", language="EN", label_group="B2B-TEST", current_stock=100,
                    domain="cosmetics")
    cream = Item(name="Sentetik Krem 50 ml", sku="B2B-KREM", unit="adet", category="Bitmiş Ürün",
                 current_stock=0, domain="cosmetics")
    serum = Item(name="Sentetik Serum 30 ml", sku="B2B-SERUM", unit="adet", category="Bitmiş Ürün",
                 current_stock=0, domain="cosmetics")
    toner = Item(name="Sentetik Tonik 100 ml", sku="B2B-TONIK", unit="adet", category="Bitmiş Ürün",
                 current_stock=30, domain="cosmetics")
    db.add_all([raw, bottle, label_tr, label_en, cream, serum, toner])
    db.flush()
    raw_lot = Inventory(item_id=raw.id, supplier_id=sup.id, lot_number="HM-B2B-1", quantity=500,
                        expiry_date=FUTURE, status="APPROVED", qc_required=False, is_sample=False,
                        domain="cosmetics")
    toner_lot = Inventory(item_id=toner.id, lot_number="TON-1", quantity=30, status="APPROVED",
                          qc_required=False, is_sample=False, domain="cosmetics")
    db.add_all([raw_lot, toner_lot])
    recipes = {}
    for target, name, per in [(cream, "Sentetik Krem Reçetesi", 10), (serum, "Sentetik Serum Reçetesi", 5)]:
        rec = Recipe(name=name, target_item_id=target.id, output_quantity=1, output_unit="adet",
                     waste_percentage=10, domain="cosmetics")
        db.add(rec)
        db.flush()
        for item, qty, phase in [(raw, per, "A"), (bottle, 1, None), (label_tr, 1, None)]:
            db.add(RecipeIngredient(recipe_id=rec.id, item_id=item.id, quantity=qty, unit=item.unit,
                                    phase=phase))
        recipes[target.id] = rec
    db.commit()
    bank = db.query(BankProfile).order_by(BankProfile.sort_order).first()
    w = {"db": db, "owner": _login("dogukan"), "tech": _login("songul"), "signer": _login("isik"),
         "labtech": _login("meltem"), "raw": raw, "bottle": bottle, "label_tr": label_tr,
         "label_en": label_en, "cream": cream, "serum": serum, "toner": toner, "raw_lot": raw_lot,
         "toner_lot": toner_lot, "cream_recipe": recipes[cream.id], "serum_recipe": recipes[serum.id],
         "bank_id": bank.id, "ids": {u: _uid(db, u) for u in ("dogukan", "isik", "songul", "meltem")}}
    w["t0"] = datetime.utcnow()
    time.sleep(0.02)
    return w


def _ok(r, status=200):
    assert r.status_code == status, r.text
    return r.json()


def _err(r, status, code):
    assert r.status_code == status, r.text
    assert r.json().get("code") == code, r.text
    return r.json()


def _quote(w, lines, *, number=None, currency="USD", country="Germany"):
    w["seq"] = w.get("seq", 0) + 1
    number = number or f"B2B-T-{w['seq']:03d}"
    sub = round(sum(q * p for _, q, p in lines), 2)
    body = {"quote_number": number, "customer_name": "Sentetik Müşteri GmbH",
            "customer_contact": "Test Kişi", "customer_email": "alici@example.com",
            "customer_address": "Teststr. 1, Berlin", "customer_country": country,
            "currency": currency, "subtotal_amount": sub, "total_amount": sub,
            "items": [{"item_id": i.id, "quantity": q, "unit_price_foreign": p} for i, q, p in lines]}
    return _ok(w["owner"].post("/api/quotations", json=body, headers=HDR), 201)["id"]


def _convert(w, qid, **extra):
    body = {"technical_user_id": w["ids"]["songul"], "signer_user_id": w["ids"]["isik"],
            "label_language": "EN", "payment_terms": "prepaid", "bank_profile_id": w["bank_id"], **extra}
    return _ok(w["owner"].post(f"{API}/from-quotation/{qid}", json=body, headers=HDR))


def _review(w, oid, lines, who="tech"):
    d = _ok(w[who].get(f"{API}/{oid}"))
    body = {"commercial_revision": d["order"]["commercial_revision"], "lines": lines}
    return w[who].post(f"{API}/{oid}/technical-review", json=body, headers=HDR)


def _sign_body(w, oid, who="signer", password="minerva123", signature=SIG):
    o = _ok(w[who].get(f"{API}/{oid}"))["order"]
    return {"commercial_revision": o["commercial_revision"], "technical_revision": o["technical_revision"],
            "document_hash": o["document_hash"] or "0" * 64, "password": password, "signature": signature}


def _sign(w, oid, who="signer", **kw):
    return w[who].post(f"{API}/{oid}/sign", json=_sign_body(w, oid, who, **kw), headers=HDR)


def _pay(w, oid, amount, currency=None):
    body = {"amount": amount, "reference": "SWIFT-TEST"}
    if currency:
        body["currency"] = currency
    return w["signer"].post(f"{API}/{oid}/payments", json=body, headers=HDR)


def _prepare(w, oid):
    return w["tech"].post(f"{API}/{oid}/preparation", json={"note": "Hazır"}, headers=HDR)


def _start(w, oid, item, qty, who="tech"):
    return w[who].post(f"{API}/{oid}/batches", json={"item_id": item.id, "quantity": qty}, headers=HDR)


def _complete(w, oid, batch_id, produced, witness=0, who="tech"):
    return w[who].post(f"{API}/{oid}/batches/{batch_id}/complete",
                       json={"produced_quantity": produced, "witness_quantity": witness}, headers=HDR)


def _qc(w, inv_id, status="APPROVED"):
    return _ok(w["owner"].post(f"/api/qc/process/{inv_id}", json={"notes": "Test QC", "status": status},
                               headers=HDR))


def _ship(w, oid, who="tech", tracking="TRK-B2B-1"):
    return w[who].post(f"{API}/{oid}/ship", json={"tracking_no": tracking, "carrier": "Test Kargo"},
                       headers=HDR)


def _stock(db, item):
    db.expire_all()
    return db.get(Item, item.id).current_stock


def _approved_order(w, lines=None, review=None, **convert):
    """Teklif → sipariş → teknik değerlendirme → imza; sipariş id'si."""
    qid = _quote(w, lines or [(w["cream"], 10, 5.0)])
    oid = _convert(w, qid, **convert)["order"]["id"]
    _ok(_review(w, oid, review or [{"item_id": w["cream"].id, "use_stock_quantity": 0}]))
    _ok(_sign(w, oid))
    return oid


def _batch_lots(db, batch):
    db.expire_all()
    rows = db.query(Inventory).filter(Inventory.item_id == batch.item_id,
                                      Inventory.lot_number.in_([batch.lot_number, batch.lot_number + "-S"]))
    return {("witness" if r.lot_number.endswith("-S") else "showroom"): r for r in rows}


# ─── Uçtan uca ───────────────────────────────────────────────────────────────

def test_full_flow_two_batches_qc_and_single_shipment(world):
    w = world
    db = w["db"]
    qid = _quote(w, [(w["cream"], 10, 5.0)])
    detail = _convert(w, qid)
    oid = detail["order"]["id"]
    assert detail["order"]["status"] == "SUBMITTED"
    assert detail["commercial"]["bank"]["iban"]
    db.expire_all()
    assert db.get(Quotation, qid).status == "ORDER"
    # Legacy "Onayla & stoktan düş" artık bu teklifte çalışmaz; stok hareketi yok
    r = w["owner"].post(f"/api/quotations/{qid}/confirm", headers=HDR)
    assert r.status_code == 400
    listed = _ok(w["owner"].get("/api/quotations"))
    assert next(q for q in listed if q["id"] == qid)["b2b_order_id"] == oid

    # Teknik değerlendirme: eksik yok (110 g baz yağ ihtiyacı < 500 g stok)
    d = _ok(_review(w, oid, [{"item_id": w["cream"].id, "use_stock_quantity": 0}]))
    assert d["order"]["status"] == "TECH_REVIEWED"
    assert d["lines"][0]["produce"] == 10 and d["lines"][0]["recipe_id"] == w["cream_recipe"].id
    raw_row = next(m for m in d["technical"]["materials"] if m["item_id"] == w["raw"].id)
    assert raw_row["need"] == pytest.approx(110) and raw_row["buy"] == pytest.approx(0)
    assert db.query(B2BOrderPurchaseLine).count() == 0

    # Onay → proforma (stok hareketi yok)
    d = _ok(_sign(w, oid))
    assert d["order"]["status"] == "APPROVED" and d["signature"]["image"].startswith("data:image/png")
    assert db.query(Transaction).count() == 0
    r = w["signer"].get(f"{API}/{oid}/proforma")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    text = " ".join(p.extract_text() or "" for p in PdfReader(BytesIO(r.content)).pages)
    assert "B2B-T-001" in text and "Sentetik Müşteri" in text

    # Peşin: ödeme ve son hazırlık olmadan parti yok
    _err(_start(w, oid, w["cream"], 6), 409, "payment_required")
    _err(_pay(w, oid, 50, currency="EUR"), 400, "currency_mismatch")
    _ok(_pay(w, oid, 50))
    _err(_start(w, oid, w["cream"], 6), 409, "preparation_required")
    _ok(_prepare(w, oid))

    # Parti 1 BAŞLAT: yalnız tüketim — bitmiş stok yok, üretim kaydı yok
    d = _ok(_start(w, oid, w["cream"], 6))
    batch1 = db.query(B2BOrderBatch).one()
    assert batch1.status == "STARTED" and batch1.lot_number
    assert _stock(db, w["raw"]) == pytest.approx(434)              # 6 × 10 g × 1.10
    assert db.get(Inventory, w["raw_lot"].id).quantity == pytest.approx(434)
    assert _stock(db, w["bottle"]) == pytest.approx(94)
    assert _stock(db, w["label_en"]) == pytest.approx(94)          # EN sipariş → EN etiket
    assert _stock(db, w["label_tr"]) == pytest.approx(100)
    assert _stock(db, w["cream"]) == pytest.approx(0)
    assert db.query(ProductionHistory).count() == 0
    outs = db.query(Transaction).filter_by(transaction_type="Output").all()
    assert len(outs) == 3 and all(t.notes.startswith("Üretim tüketimi — Reçete:") for t in outs)
    assert all(t.notes.endswith(f"Üretim Lot: {batch1.lot_number}") for t in outs)
    line = next(l for l in d["lines"] if l["item_id"] == w["cream"].id)
    assert line["started"] == 6 and line["remaining_to_start"] == 4
    _err(_start(w, oid, w["cream"], 5), 409, "over_plan")
    _err(_start(w, oid, w["cream"], 2.5), 400, "invalid_quantity")

    # TAMAMLA: bitmiş ürün + şahit; malzeme ikinci kez düşmez
    d = _ok(_complete(w, oid, batch1.id, 6, witness=1))
    db.expire_all()
    batch1 = db.get(B2BOrderBatch, batch1.id)
    assert batch1.status == "COMPLETED" and batch1.production_history_id
    assert _stock(db, w["raw"]) == pytest.approx(434)
    assert _stock(db, w["cream"]) == pytest.approx(6)
    prod = db.get(ProductionHistory, batch1.production_history_id)
    assert prod.lot_number == batch1.lot_number and prod.produced_quantity == 6
    cons = db.query(ProductionConsumption).filter_by(production_id=prod.id).all()
    assert sorted(c.kind for c in cons) == ["label", "output", "output", "packaging", "raw"]
    assert {c.transaction_id for c in cons if c.kind != "output"} == {t.id for t in outs}
    lots1 = _batch_lots(db, batch1)
    assert lots1["showroom"].quantity == 5 and lots1["showroom"].qc_required
    assert lots1["witness"].quantity == 1
    assert db.query(RetentionSample).filter_by(inventory_id=lots1["witness"].id).count() == 1
    _err(_complete(w, oid, batch1.id, 6), 409, "batch_not_started")     # tekrar → tek giriş
    assert db.query(Transaction).filter_by(transaction_type="Input").count() == 1

    # Şahit müşteri adedinden düşer → kalan 5
    line = next(l for l in d["lines"] if l["item_id"] == w["cream"].id)
    assert line["customer_quantity"] == 5 and line["remaining_to_start"] == 5
    _ok(_start(w, oid, w["cream"], 5))
    batch2 = db.query(B2BOrderBatch).filter(B2BOrderBatch.id != batch1.id).one()
    assert batch2.lot_number != batch1.lot_number
    _ok(_complete(w, oid, batch2.id, 5))
    assert _stock(db, w["raw"]) == pytest.approx(379)
    assert _stock(db, w["cream"]) == pytest.approx(11)

    # QC bitmeden sevk yok
    d = _ok(w["tech"].get(f"{API}/{oid}"))
    assert d["shipment"]["ready"] is False and d["actions"]["ship"] is False
    _err(_ship(w, oid), 409, "not_ready")
    _qc(w, lots1["showroom"].id)
    _err(_ship(w, oid), 409, "not_ready")                              # parti 2 hâlâ QC'de
    _qc(w, _batch_lots(db, batch2)["showroom"].id)
    d = _ok(w["tech"].get(f"{API}/{oid}"))
    assert d["shipment"]["ready"] is True and d["shipment"]["lines"][0]["released_stock"] == 10

    # Tek sevkiyat: şahit lot dokunulmaz, ikinci deneme hareket yazmaz
    d = _ok(_ship(w, oid))
    assert d["order"]["status"] == "SHIPPED" and d["order"]["delivery_id"]
    assert _stock(db, w["cream"]) == pytest.approx(1)
    assert db.get(Inventory, lots1["witness"].id).quantity == 1
    assert db.query(Delivery).count() == 1
    ship_outs = db.query(Transaction).filter(Transaction.item_id == w["cream"].id,
                                             Transaction.transaction_type == "Output").count()
    _err(_ship(w, oid, tracking="TRK-B2B-2"), 409, "order_terminal")
    assert db.query(Transaction).filter(Transaction.item_id == w["cream"].id,
                                        Transaction.transaction_type == "Output").count() == ship_outs

    # Defter tutarlı: her stok değişimi bir kayıtla eşleşiyor
    at, _ = compute_stock_at(db, w["t0"])
    for key, start in [("raw", 500), ("bottle", 100), ("label_en", 100), ("label_tr", 100), ("cream", 0)]:
        assert at[w[key].id] == pytest.approx(start), key
    timeline = [t["action"] for t in d["timeline"]]
    assert timeline[0].startswith("Siparişe dönüştürüldü") and "Sevk edildi" in timeline


def test_shortage_shared_materials_and_purchase_lines(world):
    w = world
    db = w["db"]
    # Krem 30 (330 g) + serum 40 (220 g) aynı baz yağı kullanır → 550 g > 500 g
    qid = _quote(w, [(w["cream"], 30, 5.0), (w["serum"], 40, 4.0), (w["toner"], 20, 3.0)])
    oid = _convert(w, qid)["order"]["id"]
    r = _review(w, oid, [{"item_id": w["cream"].id}, {"item_id": w["serum"].id}])
    _err(r, 400, "lines_incomplete")
    r = _review(w, oid, [{"item_id": w["cream"].id}, {"item_id": w["serum"].id},
                         {"item_id": w["toner"].id, "use_stock_quantity": 25}])
    _err(r, 400, "invalid_split")
    d = _ok(_review(w, oid, [{"item_id": w["cream"].id}, {"item_id": w["serum"].id},
                             {"item_id": w["toner"].id, "use_stock_quantity": 20}]))
    toner_line = next(l for l in d["lines"] if l["item_id"] == w["toner"].id)
    assert toner_line["produce"] == 0 and toner_line["recipe_id"] is None
    raw_row = next(m for m in d["technical"]["materials"] if m["item_id"] == w["raw"].id)
    assert raw_row["need"] == pytest.approx(550) and raw_row["buy"] == pytest.approx(50)
    bottle_row = next(m for m in d["technical"]["materials"] if m["item_id"] == w["bottle"].id)
    assert bottle_row["need"] == pytest.approx(70) and bottle_row["buy"] == pytest.approx(0)
    lines = db.query(B2BOrderPurchaseLine).all()
    assert [(l.item_id, l.need_quantity, l.status) for l in lines] == [(w["raw"].id, 50, "open")]

    # Alım verildi → yeniden değerlendirmede kesinleşmiş miktar ihtiyaçtan düşer
    _ok(w["tech"].post(f"{API}/{oid}/purchase-lines/{lines[0].id}", headers=HDR,
                       json={"status": "ordered", "ordered_quantity": 50, "supplier_name": "Sentetik"}))
    d = _ok(_review(w, oid, [{"item_id": w["cream"].id}, {"item_id": w["serum"].id},
                             {"item_id": w["toner"].id, "use_stock_quantity": 20}]))
    db.expire_all()
    assert d["order"]["technical_revision"] == 2
    assert [(l.status, l.ordered_quantity) for l in db.query(B2BOrderPurchaseLine).all()] == [("ordered", 50)]
    # Teknik görünümde (meltem) alım tutarı / satış fiyatı yok
    view = _ok(w["labtech"].get(f"{API}/{oid}"))
    assert "commercial" not in view and "unit_price" not in view["lines"][0]
    assert all("amount" not in m and "price" not in m for m in view["technical"]["materials"])


def test_payment_terms_gate_production_and_shipment(world):
    w = world
    db = w["db"]
    # Avans %30: üretim 15 USD ister, sevkiyat 50'nin tamamını
    oid = _approved_order(w, payment_terms="advance", advance_percent=30)
    _ok(_prepare(w, oid))
    _err(_start(w, oid, w["cream"], 10), 409, "payment_required")
    _ok(_pay(w, oid, 15))
    batch = _ok(_start(w, oid, w["cream"], 10))["batches"][0]
    _ok(_complete(w, oid, batch["id"], 10))
    _qc(w, _batch_lots(db, db.get(B2BOrderBatch, batch["id"]))["showroom"].id)
    d = _ok(w["tech"].get(f"{API}/{oid}"))
    assert d["shipment"]["ready"] is False
    assert any("Ödeme" in p for p in d["shipment"]["problems"])
    _ok(_pay(w, oid, 35))
    _ok(_ship(w, oid))

    # Vadeli: ödeme ne üretimi ne sevkiyatı durdurur
    oid2 = _approved_order(w, lines=[(w["toner"], 5, 3.0)], review=[{"item_id": w["toner"].id,
                                                                      "use_stock_quantity": 5}],
                           payment_terms="net")
    d = _ok(w["tech"].get(f"{API}/{oid2}"))
    assert d["payment"]["production_ok"] and d["payment"]["shipment_ok"]
    d = _ok(_ship(w, oid2, tracking="TRK-NET"))
    assert d["order"]["status"] == "SHIPPED"
    assert _stock(db, w["toner"]) == pytest.approx(25)


def test_revision_reapproval_and_signed_proforma_is_frozen(world):
    w = world
    db = w["db"]
    oid = _approved_order(w)
    pdf_before = w["signer"].get(f"{API}/{oid}/proforma").content
    bank = db.get(BankProfile, w["bank_id"])
    iban_before = bank.iban_usd

    # Banka profili değişir → imzalı proforma aynı IBAN'ı basar (snapshot)
    body = {k: getattr(bank, k) for k in ("label", "bank_name", "branch", "swift", "account_holder",
                                          "iban_usd", "iban_eur", "iban_try")}
    _ok(w["signer"].put(f"{API}/banks/{bank.id}", json={**body, "iban_usd": "TR000000000000000000000001"},
                        headers=HDR))
    text = " ".join(p.extract_text() or "" for p in
                    PdfReader(BytesIO(w["signer"].get(f"{API}/{oid}/proforma").content)).pages)
    assert iban_before.replace(" ", "")[:12] in text.replace(" ", "") or iban_before[:10] in text
    assert "TR000000000000000000000001" not in text.replace(" ", "")
    assert pdf_before[:4] == b"%PDF"

    # Fiyat değişikliği → yalnız yönetim onayı yenilenir (teknik korunur)
    d = _ok(w["owner"].get(f"{API}/{oid}"))
    r = w["owner"].post(f"{API}/{oid}/revise", headers=HDR,
                        json={"commercial_revision": 0, "notes": "x"})
    _err(r, 409, "revision_stale")
    d = _ok(w["owner"].post(f"{API}/{oid}/revise", headers=HDR, json={
        "commercial_revision": d["order"]["commercial_revision"],
        "lines": [{"item_id": w["cream"].id, "quantity": 10, "unit_price": 6.0}]}))
    assert d["order"]["status"] == "TECH_REVIEWED" and d["order"]["commercial_revision"] == 2
    assert d["commercial"]["total"] == pytest.approx(60)
    assert d["signature"] is None and d["actions"]["proforma"] is False
    _err(w["signer"].get(f"{API}/{oid}/proforma"), 409, "approval_required")
    # Teknik sürüm eski ticari sürüme ait ama kapsam (ürün/adet/dil) aynı → imza yeterli
    _ok(_sign(w, oid))

    # Adet değişikliği → teknik değerlendirme + onay yeniden
    d = _ok(w["owner"].get(f"{API}/{oid}"))
    d = _ok(w["owner"].post(f"{API}/{oid}/revise", headers=HDR, json={
        "commercial_revision": d["order"]["commercial_revision"],
        "lines": [{"item_id": w["cream"].id, "quantity": 12, "unit_price": 6.0}]}))
    assert d["order"]["status"] == "SUBMITTED" and d["technical"]["stale"] is True
    _err(_sign(w, oid), 409, "invalid_stage")
    _ok(_review(w, oid, [{"item_id": w["cream"].id}]))
    _ok(_sign(w, oid))
    # Değişiklik yoksa yeni sürüm açılmaz
    d = _ok(w["owner"].get(f"{API}/{oid}"))
    _err(w["owner"].post(f"{API}/{oid}/revise", headers=HDR,
                         json={"commercial_revision": d["order"]["commercial_revision"],
                               "lines": [{"item_id": w["cream"].id, "quantity": 12, "unit_price": 6.0}]}),
         400, "no_change")


def test_multi_bank_terms_signature_under_best_regards_and_bank_change_resets_only_signature(world):
    """Proforma şablonu: siparişte 1–3 banka + düzenlenebilir şartlar; banka/şart
    değişikliği yalnız imzayı yeniler; imzalı proforma seçilen bankaları (RUB dahil),
    şartları ve imzacıyı "Best Regards." altında basar."""
    w = world
    db = w["db"]
    banks = {b.bank_name: b for b in db.query(BankProfile)}
    kuveyt = next(b for n, b in banks.items() if "KUVEYT" in n)
    vakif = next(b for n, b in banks.items() if "VAKIF" in n)
    emlak = _ok(w["signer"].post(f"{API}/banks", headers=HDR, json={
        "label": "Emlak — Rusya", "bank_name": "EMLAK KATILIM BANKASI", "account_holder": "MİNERVA 108",
        "iban_rub": "TR000000000000000000000077"}))["id"]
    qid = _quote(w, [(w["cream"], 10, 5.0)], country="Russia")
    d = _convert(w, qid, bank_profile_id=None, bank_profile_ids=[vakif.id, kuveyt.id],
                 terms={"loading_days": 30, "payment": "yok sayılır"})
    oid = d["order"]["id"]
    assert [b["id"] for b in d["commercial"]["banks"]] == [vakif.id, kuveyt.id]
    assert d["order"]["bank_profile_ids"] == [vakif.id, kuveyt.id]
    assert d["commercial"]["terms"]["loading_days"] == 30
    assert d["commercial"]["terms"]["payment"] == "% 100 IN ADVANCE"      # ödeme koşulundan
    assert d["commercial"]["terms"]["transportation"] == "EXCLUDING"
    # Teknik görünüm banka seçimini de görmez
    assert "bank_profile_ids" not in _ok(w["labtech"].get(f"{API}/{oid}"))["order"]
    _ok(_review(w, oid, [{"item_id": w["cream"].id, "use_stock_quantity": 0}]))
    signed = _ok(_sign(w, oid))
    text = " ".join(p.extract_text() or "" for p in
                    PdfReader(BytesIO(w["signer"].get(f"{API}/{oid}/proforma").content)).pages)
    flat = "".join(text.split())
    assert "Best Regards." in text and signed["signature"]["signer_name"] in text
    assert text.index("Best Regards.") < text.index(signed["signature"]["signer_name"])
    assert "LOADING WITHIN 30 DAYS" in text and "RUBIBANNO" not in flat
    assert flat.index("TÜRKİYEVAKIFLAR") < flat.index("KUVEYTTÜRK")

    # Banka + kısmi şart değişikliği → kapsam aynı: yalnız imza yenilenir, diğer şartlar korunur
    o = _ok(w["owner"].get(f"{API}/{oid}"))["order"]
    d = _ok(w["owner"].post(f"{API}/{oid}/revise", headers=HDR, json={
        "commercial_revision": o["commercial_revision"], "bank_profile_ids": [emlak],
        "terms": {"transportation": "INCLUDING"}}))
    assert d["order"]["status"] == "TECH_REVIEWED" and d["signature"] is None
    assert d["commercial"]["terms"]["transportation"] == "INCLUDING"
    assert d["commercial"]["terms"]["loading_days"] == 30
    for bad, code in (({"bank_profile_ids": [kuveyt.id, vakif.id, emlak, 999]}, "too_many_banks"),
                      ({"terms": {"loading_days": 999}}, "invalid_terms")):
        _err(w["owner"].post(f"{API}/{oid}/revise", headers=HDR,
                             json={"commercial_revision": d["order"]["commercial_revision"], **bad}), 400, code)
    _ok(_sign(w, oid))
    flat = "".join(" ".join(p.extract_text() or "" for p in PdfReader(BytesIO(
        w["signer"].get(f"{API}/{oid}/proforma").content)).pages).split())
    assert "RUBIBANNO" in flat and "TR000000000000000000000077" in flat and "KUVEYT" not in flat
    assert "TRANSPORTATION:INCLUDING" in flat

    # Bankasız sipariş imzalanamaz (proformaya basılacak hesap yok)
    qid = _quote(w, [(w["cream"], 2, 5.0)])
    oid2 = _convert(w, qid, bank_profile_id=None, bank_profile_ids=[])["order"]["id"]
    _ok(_review(w, oid2, [{"item_id": w["cream"].id, "use_stock_quantity": 0}]))
    _err(_sign(w, oid2), 409, "bank_required")


def test_internal_technical_sheet_recipe_lots_without_sales_prices_or_banks(world):
    """İç teknik föy: onaylanan reçete bileşimi (teknik snapshot), malzeme ihtiyacı,
    parti tüketimi kaynak lot + tedarikçiyle; satış fiyatı ve banka YOK; alım
    tutarı yalnız b2b.view; reçete sonradan değişirse uyarı; eski snapshot'ta
    güncel reçete notla."""
    w = world
    db = w["db"]
    oid = _approved_order(w, lines=[(w["cream"], 10, 7.77)])
    _ok(_pay(w, oid, 77.7))
    _ok(_prepare(w, oid))
    _ok(_start(w, oid, w["cream"], 4))
    batch = db.query(B2BOrderBatch).one()

    def sheet(who):
        r = w[who].get(f"{API}/{oid}/technical-sheet")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf", r.text
        assert "Teknik_Foy_" in r.headers["content-disposition"]
        return " ".join(p.extract_text() or "" for p in PdfReader(BytesIO(r.content)).pages)
    text = sheet("signer")
    flat = "".join(text.split())
    assert "İÇ BELGE" in text and "müşteriye gönderilmez" in text
    assert "Sentetik Krem Reçetesi" in text and "Sentetik Baz Yağ" in text
    assert "HM-B2B-1" in text and "Sentetik Tedarikçi B2B" in text and batch.lot_number in text
    assert "Alım tutarı" in text                                       # b2b.view → alım tutarı görünür
    assert "7,77" not in text and "7.77" not in text and "77,70" not in flat   # satış fiyatı / toplam YOK
    assert "KUVEYT" not in flat and "IBAN" not in flat                 # banka YOK
    labtech = sheet("labtech")                                         # teknik görünüm
    assert "Alım tutarı" not in labtech and "HM-B2B-1" in labtech

    # Onaylı reçete sonradan değişirse föy uyarır (parti başlatılamaz)
    ing = db.query(RecipeIngredient).filter_by(recipe_id=w["cream_recipe"].id, item_id=w["raw"].id).one()
    ing.quantity = 12
    db.commit()
    assert "reçete teknik değerlendirmeden sonra değişti" in sheet("tech")
    # Bugünden önceki teknik sürüm (bileşimsiz) → güncel reçete, notla
    from database import B2BOrderRevision
    row = db.query(B2BOrderRevision).filter_by(order_id=oid, kind="technical").order_by(
        B2BOrderRevision.revision.desc()).first()
    snap = json.loads(row.snapshot)
    for line in snap["lines"]:
        line.pop("recipe", None)
    row.snapshot = json.dumps(snap)
    db.commit()
    assert "güncel reçete gösteriliyor" in sheet("tech")
    # Bayi / başka panel erişemez
    _user(db, "b2b_dist_sheet", "Distributor", {"b2b_orders": {"view": True}})
    assert _login("b2b_dist_sheet").get(f"{API}/{oid}/technical-sheet").status_code == 403
    db.query(B2BOrder).filter_by(id=oid).update({"domain": "supplement"})
    db.commit()
    assert w["tech"].get(f"{API}/{oid}/technical-sheet").status_code == 404


def test_sign_requires_assigned_signer_password_and_drawn_signature(world):
    w = world
    db = w["db"]
    qid = _quote(w, [(w["cream"], 10, 5.0)])
    # Siparişi açan kişi imzacı olamaz (dört göz)
    r = w["owner"].post(f"{API}/from-quotation/{qid}", headers=HDR, json={
        "technical_user_id": w["ids"]["songul"], "signer_user_id": w["ids"]["dogukan"],
        "payment_terms": "prepaid", "bank_profile_id": w["bank_id"]})
    _err(r, 400, "four_eyes")
    # İmza yetkisi olmayan kişi imzacı atanamaz
    r = w["owner"].post(f"{API}/from-quotation/{qid}", headers=HDR, json={
        "technical_user_id": w["ids"]["songul"], "signer_user_id": w["ids"]["meltem"],
        "payment_terms": "prepaid", "bank_profile_id": w["bank_id"]})
    _err(r, 400, "invalid_assignee")
    oid = _convert(w, qid)["order"]["id"]
    _err(_sign(w, oid), 409, "invalid_stage")                  # teknik değerlendirme yok
    _err(_review(w, oid, [{"item_id": w["cream"].id}], who="owner"), 403, "not_assigned")
    _ok(_review(w, oid, [{"item_id": w["cream"].id}]))

    # Atanmamış (ama sign yetkili SuperAdmin) kişi: şifre denemesi bile harcanmaz
    _err(_sign(w, oid, who="owner"), 403, "not_assigned")
    # Bozuk / boş imza, eski belge özeti
    _err(_sign(w, oid, signature="data:image/png;base64,AAAA"), 400, "signature_invalid")
    _err(_sign(w, oid, signature="data:image/jpeg;base64," + SIG.split(",")[1]), 400, "signature_invalid")
    _err(_sign(w, oid, signature=_png(20, 10)), 400, "signature_invalid")
    body = _sign_body(w, oid)
    _err(w["signer"].post(f"{API}/{oid}/sign", headers=HDR, json={**body, "document_hash": "0" * 64}),
         409, "revision_stale")

    # Yanlış şifre giriş sayaçlarını paylaşır → 5. denemede kilit
    for _ in range(4):
        _err(_sign(w, oid, password="yanlis-sifre"), 401, "invalid_password")
    db.expire_all()
    assert db.get(User, w["ids"]["isik"]).failed_login_attempts == 4
    _err(_sign(w, oid, password="yanlis-sifre"), 401, "invalid_password")
    _err(_sign(w, oid), 423, "locked")
    assert db.get(B2BOrder, oid).status == "TECH_REVIEWED"
    db.expire_all()
    u = db.get(User, w["ids"]["isik"])
    u.lockout_until, u.failed_login_attempts = None, 0
    db.commit()
    d = _ok(_sign(w, oid))
    assert d["order"]["status"] == "APPROVED" and d["signature"]["signer_name"]
    db.expire_all()
    assert db.get(User, w["ids"]["isik"]).failed_login_attempts == 0


def test_visibility_permissions_distributor_and_domain(world):
    w = world
    db = w["db"]
    oid = _approved_order(w)
    # LabTech: üretim görünümü — satış fiyatı / banka / ödeme / proforma yok
    view = _ok(w["labtech"].get(f"{API}/{oid}"))
    assert "commercial" not in view and "payments" not in view
    assert "image" not in (view["signature"] or {})
    assert _ok(w["labtech"].get(API))["orders"][0]["total"] is None
    assert w["labtech"].get(f"{API}/{oid}/proforma").status_code == 403
    assert w["labtech"].post(f"{API}/{oid}/payments", json={"amount": 1}, headers=HDR).status_code == 403
    assert _sign(w, oid, who="labtech").status_code == 403
    qid = _quote(w, [(w["serum"], 5, 4.0)], number="B2B-T-002")
    r = w["labtech"].post(f"{API}/from-quotation/{qid}", headers=HDR, json={
        "technical_user_id": w["ids"]["songul"], "signer_user_id": w["ids"]["isik"]})
    assert r.status_code == 403
    # LabLead: ticari revizyon / banka kapalı (b2b.view yok)
    assert w["tech"].post(f"{API}/{oid}/revise", json={"commercial_revision": 1},
                          headers=HDR).status_code == 403
    assert w["tech"].post(f"{API}/bank-rules", json={"country": "*", "currency": "USD",
                                                     "bank_profile_id": w["bank_id"]},
                          headers=HDR).status_code == 403

    # Staff ve Distributor
    _user(db, "b2b_staff", "Staff")
    _user(db, "b2b_dist", "Distributor")
    staff, dist = _login("b2b_staff"), _login("b2b_dist")
    for c in (staff, dist):
        assert c.get(API).status_code == 403
        assert c.get(f"{API}/{oid}").status_code == 403
        assert c.get("/b2b-siparisler", follow_redirects=False).status_code in (302, 303, 403)
    assert _ok(staff.get(f"{API}/my-tasks"))["tasks"] == []
    assert dist.get(f"{API}/my-tasks").status_code == 403

    # Başka panel: sipariş görünmez, işlem yapılamaz
    w["owner"].cookies.set("active_domain", "supplement")
    assert w["owner"].get(f"{API}/{oid}").status_code == 404
    assert _ok(w["owner"].get(API))["orders"] == []
    _err(w["owner"].post(f"{API}/{oid}/cancel", json={"reason": "panel dışı deneme"}, headers=HDR),
         404, "not_found")
    w["owner"].cookies.set("active_domain", "cosmetics")
    assert _ok(w["owner"].get(f"{API}/{oid}"))["order"]["status"] == "APPROVED"


def test_strict_lot_pool_skips_unreleased_lots_and_writes_nothing_when_short(world):
    w = world
    db = w["db"]
    # En eski lot SKT'si geçmiş, ikincisi QC bekliyor, üçüncüsü SKT'siz; geçerli 4. lot 40 g
    raw = db.get(Item, w["raw"].id)
    lot = db.get(Inventory, w["raw_lot"].id)
    lot.expiry_date = PAST
    lot.quantity = 300
    db.add_all([
        Inventory(item_id=raw.id, lot_number="HM-QC", quantity=100, expiry_date=FUTURE, status="APPROVED",
                  qc_required=True, is_sample=False, domain="cosmetics"),
        Inventory(item_id=raw.id, lot_number="HM-NOEXP", quantity=60, expiry_date=None, status="APPROVED",
                  qc_required=False, is_sample=False, domain="cosmetics"),
        Inventory(item_id=raw.id, lot_number="HM-SAMPLE", quantity=200, expiry_date=FUTURE,
                  status="APPROVED", qc_required=False, is_sample=True, domain="cosmetics"),
        Inventory(item_id=raw.id, lot_number="HM-OK", quantity=40, expiry_date=FUTURE, status="APPROVED",
                  qc_required=False, is_sample=False, domain="cosmetics")])
    db.commit()
    oid = _approved_order(w)
    _ok(_pay(w, oid, 50))
    _ok(_prepare(w, oid))
    before = db.query(Transaction).count()
    # 4 adet = 44 g > 40 g geçerli lot → hiçbir şey yazılmaz
    _err(_start(w, oid, w["cream"], 4), 409, "lot_missing")
    db.expire_all()
    assert db.query(Transaction).count() == before and db.query(B2BOrderBatch).count() == 0
    assert _stock(db, w["raw"]) == pytest.approx(500) and _stock(db, w["bottle"]) == pytest.approx(100)
    # 3 adet = 33 g → yalnız geçerli lottan
    _ok(_start(w, oid, w["cream"], 3))
    db.expire_all()
    out = db.query(Transaction).filter_by(item_id=w["raw"].id, transaction_type="Output").one()
    assert out.lot_number == "HM-OK" and out.quantity == pytest.approx(33)
    assert db.query(Inventory).filter_by(lot_number="HM-OK").one().quantity == pytest.approx(7)
    assert db.get(Inventory, w["raw_lot"].id).quantity == pytest.approx(300)
    assert b2b.strict_lot_ok(db.query(Inventory).filter_by(lot_number="HM-QC").one()) is False


def test_concurrent_batch_start_and_ship_move_stock_once(world):
    w = world
    db = w["db"]
    oid = _approved_order(w)
    _ok(_pay(w, oid, 50))
    _ok(_prepare(w, oid))
    actor = {"sub": str(w["ids"]["songul"]), "username": "songul", "full_name": "Songül"}

    def race(fn):
        results, started = {}, threading.Event()

        def run(name, delay):
            s = SessionLocal()
            try:
                if name == "b":
                    started.wait(5)
                fn(s)
                if name == "a":
                    started.set()
                    time.sleep(delay)                 # kilit açıkken B bekliyor
                s.commit()
                results[name] = "ok"
            except b2b.B2BError as exc:
                s.rollback()
                results[name] = exc.code
            finally:
                started.set()
                s.close()

        threads = [threading.Thread(target=run, args=("a", 0.4)), threading.Thread(target=run, args=("b", 0))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        return sorted(results.values())

    assert race(lambda s: b2b.start_batch(s, "cosmetics", actor, oid,
                                          {"item_id": w["cream"].id, "quantity": 10})) == ["ok", "over_plan"]
    db.expire_all()
    assert db.query(B2BOrderBatch).count() == 1
    assert _stock(db, w["raw"]) == pytest.approx(390)
    batch = db.query(B2BOrderBatch).one()
    assert race(lambda s: b2b.complete_batch(s, "cosmetics", actor, oid, batch.id,
                                             {"produced_quantity": 10},
                                             retention_months=lambda: (24, 12))) == ["batch_not_started", "ok"]
    assert _stock(db, w["cream"]) == pytest.approx(10)
    _qc(w, _batch_lots(db, batch)["showroom"].id)
    assert race(lambda s: b2b.ship(s, "cosmetics", actor, oid, {"tracking_no": "TRK-RACE"})) == \
        ["ok", "order_terminal"]
    db.expire_all()
    assert db.query(Delivery).count() == 1
    assert _stock(db, w["cream"]) == pytest.approx(0)
    assert db.query(Transaction).filter_by(item_id=w["cream"].id, transaction_type="Output").count() == 1


def test_cancelled_batch_returns_are_explicit_and_bounded(world):
    w = world
    db = w["db"]
    oid = _approved_order(w)
    _ok(_pay(w, oid, 50))
    _ok(_prepare(w, oid))
    _ok(_start(w, oid, w["cream"], 10))
    batch = db.query(B2BOrderBatch).one()
    assert _stock(db, w["raw"]) == pytest.approx(390)
    # Açık parti varken sipariş iptal edilemez
    _err(w["owner"].post(f"{API}/{oid}/cancel", json={"reason": "müşteri vazgeçti"}, headers=HDR),
         409, "open_batch")
    _err(w["tech"].post(f"{API}/{oid}/batches/{batch.id}/returns", headers=HDR,
                        json={"item_id": w["raw"].id, "quantity": 10, "reason": "kullanılmadı"}),
         409, "batch_not_cancelled")
    _err(w["tech"].post(f"{API}/{oid}/batches/{batch.id}/cancel", json={"reason": "x"}, headers=HDR),
         400, "reason_required")
    _ok(w["tech"].post(f"{API}/{oid}/batches/{batch.id}/cancel", json={"reason": "Kazan arızası"},
                       headers=HDR))
    # İptal otomatik iade YAPMAZ
    assert _stock(db, w["raw"]) == pytest.approx(390)
    _ok(w["tech"].post(f"{API}/{oid}/batches/{batch.id}/returns", headers=HDR,
                       json={"item_id": w["raw"].id, "quantity": 80, "reason": "Kova kullanılmadı"}))
    assert _stock(db, w["raw"]) == pytest.approx(470)
    assert db.get(Inventory, w["raw_lot"].id).quantity == pytest.approx(470)
    _err(w["tech"].post(f"{API}/{oid}/batches/{batch.id}/returns", headers=HDR,
                        json={"item_id": w["raw"].id, "quantity": 31, "reason": "fazla iade"}),
         409, "over_consumed")
    _err(w["tech"].post(f"{API}/{oid}/batches/{batch.id}/returns", headers=HDR,
                        json={"item_id": w["toner"].id, "quantity": 1, "reason": "yanlış kart"}),
         400, "invalid_item")
    adj = db.query(Transaction).filter_by(transaction_type="Adjustment").one()
    assert adj.quantity == 80 and adj.notes.startswith(b2b.BATCH_RETURN_MARK)
    # İptal edilen partinin lot no'su yeniden verilmez, kalan üretim yeniden açılır
    d = _ok(w["tech"].get(f"{API}/{oid}"))
    assert d["lines"][0]["remaining_to_start"] == 10
    r = w["tech"].post("/api/production", headers=HDR, json={
        "recipe_id": w["cream_recipe"].id, "produced_quantity": 1, "label_language": "TR",
        "lot_number": batch.lot_number})
    assert r.status_code == 400 and "zaten kullanılmış" in r.json()["detail"]
    at, _ = compute_stock_at(db, w["t0"])
    assert at[w["raw"].id] == pytest.approx(500)


def test_qc_rejection_blocks_shipment_and_normal_cancel_is_redirected(world):
    w = world
    db = w["db"]
    oid = _approved_order(w)
    _ok(_pay(w, oid, 50))
    _ok(_prepare(w, oid))
    _ok(_start(w, oid, w["cream"], 10))
    batch = db.query(B2BOrderBatch).one()
    _ok(_complete(w, oid, batch.id, 10))
    db.expire_all()
    batch = db.get(B2BOrderBatch, batch.id)
    # Normal üretim iptali B2B partisini geri alamaz (sipariş tamamlandı sanırdı)
    pv = _ok(w["owner"].get(f"/api/production/{batch.production_history_id}/cancel-preview"))
    assert pv["can_cancel"] is False and any("B2B" in b for b in pv["blockers"])
    # QC reddi: stok geri alınır, sevk hazır değil
    _qc(w, _batch_lots(db, batch)["showroom"].id, status="REJECTED")
    assert _stock(db, w["cream"]) == pytest.approx(0)
    d = _ok(w["tech"].get(f"{API}/{oid}"))
    assert d["shipment"]["ready"] is False
    assert d["lines"][0]["customer_quantity"] == 10        # üretim kaydı durur; yeni parti
    _err(_ship(w, oid), 409, "not_ready")


def test_legacy_lotless_stock_ships_but_witness_and_qc_pending_do_not(world):
    w = world
    db = w["db"]
    toner = db.get(Item, w["toner"].id)
    toner.current_stock = 45            # 30 lotlu + 15 lot kaydı olmayan eski stok
    db.add_all([Inventory(item_id=toner.id, lot_number="TON-QC", quantity=7, status="APPROVED",
                          qc_required=True, is_sample=False, domain="cosmetics")])
    toner.current_stock = 52
    db.commit()
    wit = Inventory(item_id=toner.id, lot_number="TON-1-S", quantity=3, status="APPROVED",
                    qc_required=False, is_sample=False, domain="cosmetics")
    db.add(wit)
    db.flush()
    db.add(RetentionSample(inventory_id=wit.id, item_id=toner.id, item_name=toner.name,
                           lot_number="TON-1-S", brand="Genel", quantity=3, initial_quantity=3,
                           status="stored", domain="cosmetics"))
    toner.current_stock = 55
    db.commit()
    # 55 − 7 (QC bekliyor) − 3 (şahit) = 45 sevke uygun
    assert b2b.released_stock(db, toner.id) == pytest.approx(45)
    oid = _approved_order(w, lines=[(w["toner"], 46, 1.0)],
                          review=[{"item_id": w["toner"].id, "use_stock_quantity": 46}], payment_terms="net")
    _err(_ship(w, oid), 409, "not_ready")
    d = _ok(w["owner"].get(f"{API}/{oid}"))
    d = _ok(w["owner"].post(f"{API}/{oid}/revise", headers=HDR, json={
        "commercial_revision": d["order"]["commercial_revision"],
        "lines": [{"item_id": w["toner"].id, "quantity": 45, "unit_price": 1.0}]}))
    _ok(_review(w, oid, [{"item_id": w["toner"].id, "use_stock_quantity": 45}]))
    _ok(_sign(w, oid))
    _ok(_ship(w, oid))
    db.expire_all()
    assert _stock(db, w["toner"]) == pytest.approx(10)
    assert db.query(Inventory).filter_by(lot_number="TON-1").one().quantity == 0
    assert db.query(Inventory).filter_by(lot_number="TON-QC").one().quantity == 7
    assert db.get(Inventory, wit.id).quantity == 3


def test_my_tasks_follow_next_step_owner(world):
    w = world
    qid = _quote(w, [(w["cream"], 10, 5.0)])
    oid = _convert(w, qid)["order"]["id"]
    tasks = lambda who: [t for t in _ok(w[who].get(f"{API}/my-tasks"))["tasks"] if t["kind"] == "b2b_order"]
    assert [t["text"] for t in tasks("tech")] == ["Teknik değerlendirme"]
    assert tasks("signer") == []
    _ok(_review(w, oid, [{"item_id": w["cream"].id}]))
    assert tasks("tech") == [] and [t["text"] for t in tasks("signer")] == ["Yönetim onayı (şifre + imza)"]
    _ok(_sign(w, oid))
    assert [t["text"] for t in tasks("signer")] == ["Ödeme doğrulaması"]
    assert tasks("tech") == []
    _ok(_pay(w, oid, 50))
    assert [t["text"] for t in tasks("tech")] == ["Son hazırlık kontrolü"]
    _ok(_prepare(w, oid))
    assert [t["text"] for t in tasks("tech")] == ["Üretim partisi başlat"]
    assert [t["text"] for t in tasks("labtech")] == ["Üretim partisi başlat"]
    page = w["tech"].get("/")
    assert page.status_code == 200 and "myTasksCard" in page.text


def test_backfill_gates_new_category_and_bank_seed_is_idempotent(db_session):
    from database import _backfill_perm_b2b_orders, _seed_bank_profiles
    db = db_session
    assert db.query(BankProfile).count() == 3
    assert all(b.iban_usd and b.account_holder for b in db.query(BankProfile))
    # Admin bir bankayı pasifleştirse / silse de tohum geri gelmez
    db.query(BankProfile).filter(BankProfile.sort_order == 3).update({"is_active": False})
    db.commit()
    _seed_bank_profiles()
    assert db.query(BankProfile).count() == 3

    # Override rol varsayılanının yerine geçer: yeni kategori rol varsayılanı
    # VE kullanıcının mevcut alanı (b2b.view / production.create / inventory.adjust) ile
    sales = _user(db, "b2b_sales", "Manager", {"b2b": {"view": True, "create": True, "confirm": False}})
    lab = _user(db, "b2b_lab", "LabLead", {"production": {"view": True, "create": True},
                                           "inventory": {"adjust": True}})
    plain = _user(db, "b2b_plain", "Staff", {"items": {"view": True}})
    db.query(AppSetting).filter(AppSetting.key == "backfill.perm.b2b_orders.v1").delete()
    db.commit()
    _backfill_perm_b2b_orders()
    db.expire_all()
    perm = lambda u: json.loads(db.get(User, u.id).permissions)["b2b_orders"]
    assert perm(sales) == {"view": True, "manage": True, "tech_review": False, "sign": True,
                           "payment": True, "produce": False, "ship": False}
    assert perm(lab) == {"view": True, "manage": False, "tech_review": True, "sign": False,
                         "payment": False, "produce": True, "ship": True}
    assert not any(perm(plain).values())
    # Sentinel: ikinci çağrı elle verilen yetkiyi ezmez
    u = db.get(User, plain.id)
    p = json.loads(u.permissions)
    p["b2b_orders"]["view"] = True
    u.permissions = json.dumps(p)
    db.commit()
    _backfill_perm_b2b_orders()
    db.expire_all()
    assert perm(plain)["view"] is True


def test_recipe_change_after_approval_requires_new_review_and_signature(world):
    w = world
    db = w["db"]
    oid = _approved_order(w)
    _ok(_pay(w, oid, 50))
    _ok(_prepare(w, oid))
    # Reçete onaydan sonra düzenlenir (satır miktarı 10 g → 12 g)
    ing = db.query(RecipeIngredient).filter_by(recipe_id=w["cream_recipe"].id, item_id=w["raw"].id).one()
    ing.quantity = 12
    db.commit()
    d = _ok(w["tech"].get(f"{API}/{oid}"))
    assert d["lines"][0]["recipe_changed"] is True
    assert d["order"]["next_step"]["text"].startswith("Reçete değişti")
    _err(_start(w, oid, w["cream"], 5), 409, "recipe_changed")
    assert db.query(Transaction).count() == 0
    # Yeniden teknik değerlendirme → imza → hazırlık; yeni reçeteyle tüketim
    d = _ok(_review(w, oid, [{"item_id": w["cream"].id}]))
    assert d["order"]["status"] == "TECH_REVIEWED" and d["lines"][0]["recipe_changed"] is False
    _err(_start(w, oid, w["cream"], 5), 409, "invalid_stage")
    _ok(_sign(w, oid))
    _err(_start(w, oid, w["cream"], 5), 409, "preparation_required")
    _ok(_prepare(w, oid))
    _ok(_start(w, oid, w["cream"], 5))
    assert _stock(db, w["raw"]) == pytest.approx(500 - 5 * 12 * 1.1)


def test_insufficient_stock_at_start_writes_nothing_and_refreshes_shortages(world):
    w = world
    db = w["db"]
    oid = _approved_order(w)                       # incelemede 110 g ihtiyaç, 500 g stok → eksik yok
    _ok(_pay(w, oid, 50))
    _ok(_prepare(w, oid))
    assert db.query(B2BOrderPurchaseLine).count() == 0
    # Arada günlük üretim / düzeltme stoğu tüketti: 60 g kaldı
    raw = db.get(Item, w["raw"].id)
    raw.current_stock = 60
    db.get(Inventory, w["raw_lot"].id).quantity = 60
    db.commit()
    before = db.query(Transaction).count()
    r = _start(w, oid, w["cream"], 10)
    body = _err(r, 409, "insufficient_stock")
    assert body["shortages_refreshed"] is True
    db.expire_all()
    assert db.query(Transaction).count() == before and db.query(B2BOrderBatch).count() == 0
    assert _stock(db, w["raw"]) == pytest.approx(60) and _stock(db, w["bottle"]) == pytest.approx(100)
    lines = db.query(B2BOrderPurchaseLine).all()
    assert [(l.item_id, l.need_quantity, l.status) for l in lines] == [(w["raw"].id, 50, "open")]
    timeline = [t["action"] for t in _ok(w["tech"].get(f"{API}/{oid}"))["timeline"]]
    assert any(t.startswith("Eksikler güncellendi") for t in timeline)
    # Teslim alınan alım ihtiyaçtan iki kez düşülmez: 50 g geldi → stok yeter
    _ok(w["tech"].post(f"{API}/{oid}/purchase-lines/{lines[0].id}", headers=HDR,
                       json={"status": "received", "ordered_quantity": 50, "received_quantity": 50}))
    raw = db.get(Item, w["raw"].id)
    raw.current_stock = 110
    db.get(Inventory, w["raw_lot"].id).quantity = 110
    db.commit()
    _ok(_start(w, oid, w["cream"], 10))
    assert _stock(db, w["raw"]) == pytest.approx(0)
