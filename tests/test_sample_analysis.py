"""
Numune Analiz Formu (FR.KK.01) testleri.

  • Sayfa: render + login redirect + qc.view yeterliliği
  • CRUD: create (NA- belge no) / list / detail / PUT / soft delete
  • RBAC: qc.approve olmayan (LabTech) yazamaz, okuyabilir; anonim reddedilir
  • Domain izolasyonu: panel değişince kayıtlar görünmez
  • Reçete snapshot'ı: recipe_name reçete silinse de belgede kalır
  • PDF: antetli, tüm sayfalar A4, içerik doğru
  • Audit: create audit satırı yazılır
"""
from io import BytesIO

from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy.orm import Session

from database import (AdminAuditLog, Inventory, Item, Recipe, RecipeIngredient,
                      SampleAnalysis, SampleAnalysisIngredient, Supplier, Transaction)

_HDR = {"Origin": "http://testserver"}
_API = "/api/sample-analysis"
_A4_W, _A4_H = 595.276, 841.89


def _all_pages_a4(pdf_bytes: bytes) -> bool:
    pages = PdfReader(BytesIO(pdf_bytes)).pages
    assert len(pages) >= 1
    for pg in pages:
        b = pg.mediabox
        if not (abs(float(b.width) - _A4_W) < 2 and abs(float(b.height) - _A4_H) < 2):
            return False
    return True


def _payload(**over):
    p = {
        "bulk_name": "Saç Kremi Deneme Bulk",
        "production_date": "2026-07-18",
        "lot_number": "LOT-2407-A",
        "formulation_notes": "metil selüloz %3→%1, xanthan %1.7→%0.5",
        "properties": [
            {"key": "gorunum", "label": "GÖRÜNÜM", "spec": "Homojen krem", "found": "Homojen"},
            {"key": "koku", "label": "KOKU", "spec": "KARAKTERİSTİK", "found": "Uygun"},
            {"key": "renk", "label": "RENK", "spec": "Beyaz", "found": "Beyaz"},
            {"key": "ph", "label": "PH", "spec": "4.5-5.5", "found": "5.1"},
        ],
        "analyst_name": "Meltem Kaya",
        "result": "uygun_degil",
        "result_text": "Viskozitesi çok yoğun olduğundan onaylanmadı.",
        "notes": "Sonraki denemede xanthan %0.7.",
        "approved_by": "Işık Durak",
        "qa_representative": "Songül Akgül",
    }
    p.update(over)
    return p


def _recipe(db, name="MINERVA Test Krem 200ML"):
    tgt = Item(name=name, sku=f"sa-{name[:8]}", category="Bitmiş Ürün",
               unit="adet", current_stock=0, domain="cosmetics")
    raw = Item(name=f"Su ({name[:6]})", sku=f"sar-{name[:8]}", category="Hammadde",
               unit="g", current_stock=100, domain="cosmetics")
    db.add_all([tgt, raw]); db.flush()
    rec = Recipe(name=tgt.name, output_quantity=1, output_unit="adet",
                 target_item_id=tgt.id, domain="cosmetics")
    db.add(rec); db.flush()
    db.add(RecipeIngredient(recipe_id=rec.id, item_id=raw.id, quantity=10, unit="g"))
    db.commit()
    return rec.id, rec.name


# ─── Sayfa ───────────────────────────────────────────────────────────────────

def test_page_renders(authed_client: TestClient):
    r = authed_client.get("/numune-analiz")
    assert r.status_code == 200
    assert "Numune Analiz" in r.text and "FR.KK.01" in r.text


def test_page_redirects_without_login(client: TestClient):
    r = client.get("/numune-analiz", follow_redirects=False)
    assert r.status_code == 302 and "/login" in r.headers["location"]


def test_labtech_can_open_page(labtech_client: TestClient):
    r = labtech_client.get("/numune-analiz")
    assert r.status_code == 200                      # qc.view yeter
    # form kartı yalnız qc.approve'a render edilir (JS'teki id string'i sayılmaz)
    assert 'id="formCard"' not in r.text


# ─── CRUD ────────────────────────────────────────────────────────────────────

def test_create_and_list(authed_client: TestClient):
    r = authed_client.post(_API, json=_payload(), headers=_HDR)
    assert r.status_code == 201, r.text
    doc = r.json()["document_no"]
    assert doc.startswith("NA-")
    d = authed_client.get(_API).json()
    assert d["count"] == 1 and d["analyses"][0]["document_no"] == doc
    assert d["analyses"][0]["result_label"] == "UYGUN DEĞİLDİR"


def test_create_requires_qc_approve(labtech_client: TestClient):
    r = labtech_client.post(_API, json=_payload(), headers=_HDR)
    assert r.status_code == 403


def test_labtech_can_view_list(authed_client: TestClient, client: TestClient, db_session: Session):
    authed_client.post(_API, json=_payload(), headers=_HDR)
    r = client.post("/api/login", json={"username": "meltem", "password": "minerva123"}, headers=_HDR)
    assert r.status_code == 200
    d = client.get(_API).json()
    assert d["count"] == 1


def test_detail_and_edit(authed_client: TestClient):
    rid = authed_client.post(_API, json=_payload(), headers=_HDR).json()["id"]
    x = authed_client.get(f"{_API}/{rid}").json()
    assert x["bulk_name"] == "Saç Kremi Deneme Bulk" and x["production_date"] == "18.07.2026"
    up = _payload(result="uygun", result_text="Revize formülasyon uygun bulundu.")
    up["properties"][3]["found"] = "5.0"
    r = authed_client.put(f"{_API}/{rid}", json=up, headers=_HDR)
    assert r.status_code == 200
    x2 = authed_client.get(f"{_API}/{rid}").json()
    assert x2["result_label"] == "UYGUNDUR"
    assert x2["properties"][3]["found"] == "5.0"


def test_soft_delete(authed_client: TestClient, db_session: Session):
    rid = authed_client.post(_API, json=_payload(), headers=_HDR).json()["id"]
    r = authed_client.delete(f"{_API}/{rid}", headers=_HDR)
    assert r.status_code == 200
    assert authed_client.get(_API).json()["count"] == 0
    assert authed_client.get(f"{_API}/{rid}").status_code == 404
    row = db_session.query(SampleAnalysis).filter(SampleAnalysis.id == rid).one()
    assert row.is_active is False                    # hard delete DEĞİL


def test_invalid_payload_422(authed_client: TestClient):
    bad = _payload(); bad.pop("bulk_name")
    assert authed_client.post(_API, json=bad, headers=_HDR).status_code == 422
    assert authed_client.post(_API, json=_payload(properties=[]), headers=_HDR).status_code == 422
    assert authed_client.post(_API, json=_payload(result="belki"), headers=_HDR).status_code == 400


# ─── Domain + snapshot ───────────────────────────────────────────────────────

def test_domain_isolation(authed_client: TestClient):
    authed_client.post(_API, json=_payload(), headers=_HDR)
    r = authed_client.post("/api/domain/switch", json={"domain": "supplement"}, headers=_HDR)
    assert r.status_code == 200
    assert authed_client.get(_API).json()["count"] == 0
    authed_client.post("/api/domain/switch", json={"domain": "cosmetics"}, headers=_HDR)
    assert authed_client.get(_API).json()["count"] == 1


def test_recipe_snapshot(authed_client: TestClient, db_session: Session):
    rec_id, rec_name = _recipe(db_session)
    r = authed_client.post(_API, json=_payload(recipe_id=rec_id), headers=_HDR)
    assert r.status_code == 201, r.text
    x = authed_client.get(f"{_API}/{r.json()['id']}").json()
    assert x["recipe_id"] == rec_id and x["recipe_name"] == rec_name
    # yabancı/olmayan reçete → 404
    assert authed_client.post(_API, json=_payload(recipe_id=999999), headers=_HDR).status_code == 404


# ─── PDF + audit ─────────────────────────────────────────────────────────────

def test_pdf_is_a4_letterhead(authed_client: TestClient):
    rid = authed_client.post(_API, json=_payload(), headers=_HDR).json()["id"]
    r = authed_client.get(f"{_API}/{rid}/pdf")
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"
    assert "numune_analiz" in r.headers["content-disposition"]
    r.headers["content-disposition"].encode("latin-1")      # header-safe
    assert _all_pages_a4(r.content)
    text = "".join(pg.extract_text() for pg in PdfReader(BytesIO(r.content)).pages)
    assert "NUMUNE ANAL" in text and "LOT-2407-A" in text and "UYGUN DE" in text
    assert "FR.KK.01" in text


def test_audit_row_written(authed_client: TestClient, db_session: Session):
    doc = authed_client.post(_API, json=_payload(), headers=_HDR).json()["document_no"]
    rows = (db_session.query(AdminAuditLog)
            .filter(AdminAuditLog.action == "sample_analysis.create").all())
    assert any(r.target_name == doc for r in rows)


# ═══ Bileşen satırları — kaynak-farkındalıklı düşüm ═════════════════════════

def _raw(db, name="Xanthan Gum", stock=100.0, unit="g", domain="cosmetics", category="Hammadde"):
    it = Item(name=name, sku=f"sa-{name[:10]}", category=category, unit=unit,
              current_stock=stock, domain=domain)
    db.add(it); db.flush()
    if stock > 0:
        db.add(Inventory(item_id=it.id, lot_number=f"L-{name[:4]}", quantity=stock,
                         status="APPROVED", domain=domain))
    db.commit()
    return it.id


def _sample_lot(db, item_id, qty=10.0, lot="NUM-001", supplier=None, domain="cosmetics"):
    sid = None
    if supplier:
        sup = Supplier(name=supplier); db.add(sup); db.flush(); sid = sup.id
    inv = Inventory(item_id=item_id, lot_number=lot, quantity=qty, status="APPROVED",
                    is_sample=True, location="Numune", domain=domain, supplier_id=sid)
    db.add(inv); db.commit()
    return inv.id


def _inv_qty(db, inv_id):
    db.expire_all()
    row = db.query(Inventory).filter(Inventory.id == inv_id).first()
    return None if row is None else round(float(row.quantity), 6)


def _stock(db, item_id):
    db.expire_all()
    return round(float(db.query(Item).filter(Item.id == item_id).one().current_stock), 6)


def _tx(db, item_id, kind=None):
    db.expire_all()
    q = db.query(Transaction).filter(Transaction.item_id == item_id)
    if kind:
        q = q.filter(Transaction.transaction_type == kind)
    return q.order_by(Transaction.id).all()


def _post(c, **over):
    r = c.post(_API, json=_payload(**over), headers=_HDR)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_sample_row_deducts_only_inventory(authed_client, db_session):
    item = _raw(db_session); inv = _sample_lot(db_session, item, 10, supplier="Alt Tedarikçi")
    rid = _post(authed_client, ingredients=[
        {"item_id": item, "source": "sample", "inventory_id": inv, "quantity": 3}])
    assert _inv_qty(db_session, inv) == 7
    assert _stock(db_session, item) == 100                 # numune stok değildir
    assert _tx(db_session, item) == []                     # Transaction YOK
    x = authed_client.get(f"{_API}/{rid}").json()
    g = x["ingredients"][0]
    assert x["mode"] == "new" and g["consumed_qty"] == 3 and g["source_label"] == "Numune lotu"
    assert g["lot_number"] == "NUM-001" and g["supplier_name"] == "Alt Tedarikçi"


def test_stock_row_consumes_fifo_and_writes_output(authed_client, db_session):
    item = _raw(db_session, stock=100)
    rid = _post(authed_client, ingredients=[{"item_id": item, "source": "stock", "quantity": 5}])
    assert _stock(db_session, item) == 95
    outs = _tx(db_session, item, "Output")
    assert len(outs) == 1 and outs[0].quantity == 5 and outs[0].lot_number == "L-Xant"
    doc = authed_client.get(f"{_API}/{rid}").json()["document_no"]
    assert doc in outs[0].notes
    lot = db_session.query(Inventory).filter(Inventory.item_id == item).one()
    assert round(lot.quantity, 6) == 95


def test_pending_row_no_effect(authed_client, db_session):
    item = _raw(db_session)
    rid = _post(authed_client, ingredients=[{"item_id": item, "source": "pending", "quantity": 50}])
    assert _stock(db_session, item) == 100 and _tx(db_session, item) == []
    assert authed_client.get(f"{_API}/{rid}").json()["ingredients"][0]["consumed_qty"] == 0


def test_put_quantity_delta(authed_client, db_session):
    item = _raw(db_session); inv = _sample_lot(db_session, item, 10)
    stk = _raw(db_session, name="Gliserin", stock=50)
    rid = _post(authed_client, ingredients=[
        {"item_id": item, "source": "sample", "inventory_id": inv, "quantity": 3},
        {"item_id": stk, "source": "stock", "quantity": 4}])
    rows = authed_client.get(f"{_API}/{rid}").json()["ingredients"]
    r1, r2 = rows[0]["row_id"], rows[1]["row_id"]
    # artır
    assert authed_client.put(f"{_API}/{rid}", headers=_HDR, json=_payload(ingredients=[
        {"row_id": r1, "item_id": item, "source": "sample", "inventory_id": inv, "quantity": 5},
        {"row_id": r2, "item_id": stk, "source": "stock", "quantity": 6}])).status_code == 200
    assert _inv_qty(db_session, inv) == 5 and _stock(db_session, stk) == 44
    assert len(_tx(db_session, stk, "Output")) == 2      # 4 + 2
    # azalt
    assert authed_client.put(f"{_API}/{rid}", headers=_HDR, json=_payload(ingredients=[
        {"row_id": r1, "item_id": item, "source": "sample", "inventory_id": inv, "quantity": 1},
        {"row_id": r2, "item_id": stk, "source": "stock", "quantity": 1}])).status_code == 200
    assert _inv_qty(db_session, inv) == 9 and _stock(db_session, stk) == 49
    adj = _tx(db_session, stk, "Adjustment")
    assert len(adj) == 1 and adj[0].quantity == 5 and "iadesi" in adj[0].notes
    assert len(_tx(db_session, stk, "Output")) == 2      # Output silinmedi
    x = authed_client.get(f"{_API}/{rid}").json()["ingredients"]
    assert [g["consumed_qty"] for g in x] == [1, 1]
    assert [g["row_id"] for g in x] == [r1, r2]          # satır kimlikleri korundu


def test_put_source_change_returns_then_consumes(authed_client, db_session):
    item = _raw(db_session, stock=20); inv = _sample_lot(db_session, item, 10)
    rid = _post(authed_client, ingredients=[
        {"item_id": item, "source": "sample", "inventory_id": inv, "quantity": 4}])
    r1 = authed_client.get(f"{_API}/{rid}").json()["ingredients"][0]["row_id"]
    assert authed_client.put(f"{_API}/{rid}", headers=_HDR, json=_payload(ingredients=[
        {"row_id": r1, "item_id": item, "source": "stock", "quantity": 4}])).status_code == 200
    assert _inv_qty(db_session, inv) == 10               # numune tam iade
    assert _stock(db_session, item) == 16
    assert len(_tx(db_session, item, "Output")) == 1 and _tx(db_session, item, "Adjustment") == []


def test_put_removed_row_released(authed_client, db_session):
    item = _raw(db_session); inv = _sample_lot(db_session, item, 10)
    rid = _post(authed_client, ingredients=[
        {"item_id": item, "source": "sample", "inventory_id": inv, "quantity": 4}])
    assert authed_client.put(f"{_API}/{rid}", headers=_HDR,
                             json=_payload(ingredients=[])).status_code == 200
    assert _inv_qty(db_session, inv) == 10
    assert db_session.query(SampleAnalysisIngredient).count() == 0


def test_delete_returns_everything(authed_client, db_session):
    item = _raw(db_session); inv = _sample_lot(db_session, item, 10)
    stk = _raw(db_session, name="Gliserin", stock=50)
    rid = _post(authed_client, ingredients=[
        {"item_id": item, "source": "sample", "inventory_id": inv, "quantity": 3},
        {"item_id": stk, "source": "stock", "quantity": 4},
        {"item_id": stk, "source": "pending"}])
    assert _stock(db_session, stk) == 46
    assert authed_client.delete(f"{_API}/{rid}", headers=_HDR).status_code == 200
    assert _inv_qty(db_session, inv) == 10 and _stock(db_session, stk) == 50
    assert len(_tx(db_session, stk, "Output")) == 1      # DEĞİŞMEZ
    adj = _tx(db_session, stk, "Adjustment")
    assert len(adj) == 1 and adj[0].quantity == 4
    # satırlar duruyor (belge pasif), consumed sıfır
    db_session.expire_all()
    assert all(g.consumed_qty == 0 for g in db_session.query(SampleAnalysisIngredient).all())


def test_shortage_400_full_rollback(authed_client, db_session):
    item = _raw(db_session, stock=2); inv = _sample_lot(db_session, item, 1)
    r = authed_client.post(_API, headers=_HDR, json=_payload(ingredients=[
        {"item_id": item, "source": "stock", "quantity": 1},
        {"item_id": item, "source": "sample", "inventory_id": inv, "quantity": 5}]))
    assert r.status_code == 400 and "numune lotunda" in r.json()["detail"]
    assert _stock(db_session, item) == 2 and _inv_qty(db_session, inv) == 1
    assert _tx(db_session, item) == []
    assert db_session.query(SampleAnalysis).count() == 0
    r = authed_client.post(_API, headers=_HDR, json=_payload(ingredients=[
        {"item_id": item, "source": "stock", "quantity": 3}]))
    assert r.status_code == 400 and "yetersiz" in r.json()["detail"]
    # PUT'ta hata → alan değişiklikleri de geri alınır
    rid = _post(authed_client, ingredients=[{"item_id": item, "source": "stock", "quantity": 1}])
    r = authed_client.put(f"{_API}/{rid}", headers=_HDR, json=_payload(
        bulk_name="DEĞİŞTİ", ingredients=[{"item_id": item, "source": "stock", "quantity": 99}]))
    assert r.status_code == 400
    assert authed_client.get(f"{_API}/{rid}").json()["bulk_name"] == "Saç Kremi Deneme Bulk"
    assert _stock(db_session, item) == 1


def test_item_validation(authed_client, db_session):
    other = _raw(db_session, name="Supp Item", domain="supplement")
    r = authed_client.post(_API, headers=_HDR, json=_payload(ingredients=[
        {"item_id": other, "source": "pending"}]))
    assert r.status_code == 404
    amb = _raw(db_session, name="Şişe 200", category="Ambalaj", unit="adet")
    r = authed_client.post(_API, headers=_HDR, json=_payload(ingredients=[
        {"item_id": amb, "source": "pending"}]))
    assert r.status_code == 400 and "hammadde değil" in r.json()["detail"]
    a = _raw(db_session, name="A"); b = _raw(db_session, name="B")
    inv_b = _sample_lot(db_session, b, 10)
    r = authed_client.post(_API, headers=_HDR, json=_payload(ingredients=[
        {"item_id": a, "source": "sample", "inventory_id": inv_b, "quantity": 1}]))
    assert r.status_code == 400 and "ait değil" in r.json()["detail"]
    r = authed_client.post(_API, headers=_HDR, json=_payload(ingredients=[
        {"item_id": a, "source": "sample", "quantity": 1}]))
    assert r.status_code == 400 and "lotu seçilmedi" in r.json()["detail"]
    assert db_session.query(SampleAnalysis).count() == 0


def test_mode_validation(authed_client, db_session):
    rec_id, rec_name = _recipe(db_session)
    r = authed_client.post(_API, headers=_HDR, json=_payload(mode="existing"))
    assert r.status_code == 400
    rid = _post(authed_client, mode="new", recipe_id=rec_id)
    x = authed_client.get(f"{_API}/{rid}").json()
    assert x["mode"] == "new" and x["recipe_id"] is None
    rid = _post(authed_client, recipe_id=rec_id)                   # mode yok → türet
    x = authed_client.get(f"{_API}/{rid}").json()
    assert x["mode"] == "existing" and x["recipe_name"] == rec_name
    assert x["mode_label"] == "Mevcut reçete üzerinde çalışma"
    assert authed_client.post(_API, headers=_HDR, json=_payload(mode="belki")).status_code == 400


def test_recipe_prefill_contract(authed_client, db_session):
    rec_id, _ = _recipe(db_session)
    ings = authed_client.get(f"/api/recipes/{rec_id}").json()["ingredients"]
    assert ings and {"item_id", "item_name", "unit", "quantity", "is_ambalaj"} <= set(ings[0])


def test_pdf_includes_ingredients(authed_client, db_session):
    item = _raw(db_session, name="Panthenol Deneme"); inv = _sample_lot(db_session, item, 10, lot="NUM-77")
    rid = _post(authed_client, ingredients=[
        {"item_id": item, "source": "sample", "inventory_id": inv, "quantity": 2.5, "note": "yeni tedarikçi"}])
    r = authed_client.get(f"{_API}/{rid}/pdf")
    assert r.status_code == 200 and _all_pages_a4(r.content)
    text = "".join(pg.extract_text() for pg in PdfReader(BytesIO(r.content)).pages)
    assert "Panthenol Deneme" in text and "NUM-77" in text and "2.5 g" in text
    assert "Kullan" in text and "Numune lotu" in text


def test_release_after_sample_converted(authed_client, db_session):
    item = _raw(db_session, stock=0); inv = _sample_lot(db_session, item, 10, lot="NUM-C")
    rid = _post(authed_client, ingredients=[
        {"item_id": item, "source": "sample", "inventory_id": inv, "quantity": 3}])
    r = authed_client.post(f"/api/inventory/samples/{inv}/convert", headers=_HDR)
    assert r.status_code == 200, r.text
    assert _stock(db_session, item) == 7
    assert authed_client.delete(f"{_API}/{rid}", headers=_HDR).status_code == 200
    assert _stock(db_session, item) == 10 and _inv_qty(db_session, inv) == 10
    adj = _tx(db_session, item, "Adjustment")
    assert len(adj) == 1 and adj[0].quantity == 3 and adj[0].lot_number == "NUM-C"
    # merge yolu: aynı lotlu normal satır var → numune satırı silinir.  06.10.2026'dan
    # beri bağ silmeden ÖNCE hayatta kalan satıra yönlenir (eskiden FK SET NULL ile
    # kopuyor, iade sessizce kayboluyordu) → iade o lota + stoğa Adjustment ile döner.
    item2 = _raw(db_session, name="Merge Item", stock=0)
    survivor = Inventory(item_id=item2, lot_number="NUM-M", quantity=5, status="APPROVED",
                         domain="cosmetics")
    db_session.add(survivor)
    db_session.commit()
    inv2 = _sample_lot(db_session, item2, 10, lot="NUM-M")
    rid2 = _post(authed_client, ingredients=[
        {"item_id": item2, "source": "sample", "inventory_id": inv2, "quantity": 4}])
    assert authed_client.post(f"/api/inventory/samples/{inv2}/convert", headers=_HDR).status_code == 200
    assert _inv_qty(db_session, inv2) is None
    assert _inv_qty(db_session, survivor.id) == 11                      # 5 + kalan 6
    x = authed_client.get(f"{_API}/{rid2}").json()["ingredients"][0]
    assert x["inventory_id"] == survivor.id and x["lot_number"] == "NUM-M"
    assert _stock(db_session, item2) == 6
    assert authed_client.delete(f"{_API}/{rid2}", headers=_HDR).status_code == 200
    assert _stock(db_session, item2) == 10                              # 4 iade defterle döndü
    assert _inv_qty(db_session, survivor.id) == 15
    adj = _tx(db_session, item2, "Adjustment")
    assert len(adj) == 1 and adj[0].quantity == 4 and adj[0].lot_number == "NUM-M"


def test_item_with_pending_row_soft_deletes(authed_client, db_session):
    item = _raw(db_session, name="Pending Only", stock=0)
    _post(authed_client, ingredients=[{"item_id": item, "source": "pending"}])
    r = authed_client.delete(f"/api/items/{item}", headers=_HDR)
    assert r.status_code == 200, r.text
    db_session.expire_all()
    assert db_session.query(Item).filter(Item.id == item).one().is_active is False
