"""
Faz 2 — Numune girişi + gerçek lot/tedarikçi bazlı üretim tüketimi testleri.
Faz 3 (2026-08-24 onarımı) — numune STOK DEĞİLDİR; guard rail'ler.

Kapsam:
  • Numune kabulü ayrı lot + tedarikçi + is_sample; normal lotla birleşmez;
    STOĞA GİRMEZ, Transaction YAZMAZ (24.08.2026 olayı — eskiden giriyordu)
  • Numune → stok: POST /inventory/samples/{id}/convert
  • /api/inventory/samples  &  by-item (is_sample + supplier_name + sample_total)
  • available-lots + üretim lot havuzu numune HARİÇ (üretimde kullanılamaz)
  • Üretimde seçilen lottan düşüş + current_stock + kaynak lot izleme
  • Seçilen lot yetersizse / numune ise NET HATA (mutasyon yok)
  • Lotsuz hammadde → eski davranış (aggregate) korunur
  • Seçim yokken FIFO (en eski lot)
  • receive/convert guard'ları: RBAC (inventory.receive), domain
  • Ürün adı çakışması (Türkçe-katlanmış) → 409, force ile geçilebilir
  • Ledger tutarlılığı: sample receive + convert sonrası snapshot rekonstrüksiyonu sapmaz
"""
import bcrypt
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from core.snapshots import compute_stock_at
from database import Item, Inventory, Recipe, RecipeIngredient, Supplier, Transaction, User

_HDR = {"Origin": "http://testserver"}


def _supplier(db, name):
    s = Supplier(name=name)
    db.add(s); db.flush()
    return s.id


def _hammadde(db, name, stock, domain="cosmetics"):
    it = Item(name=name, sku=f"SKU-{name}", category="Hammadde", unit="ml",
             current_stock=stock, domain=domain)
    db.add(it); db.flush()
    return it


def _lot(db, item_id, lot, qty, supplier_id=None, is_sample=False, age_days=0):
    inv = Inventory(
        item_id=item_id, supplier_id=supplier_id, lot_number=lot, quantity=qty,
        status="APPROVED", is_sample=is_sample,
        created_at=datetime.utcnow() - timedelta(days=age_days),
    )
    db.add(inv); db.flush()
    return inv


def _recipe_one_ingredient(db, target_name, ing_item_id, net_qty):
    target = Item(name=target_name, sku=f"SKU-{target_name}", category="Bitmiş Ürün",
                  unit="adet", current_stock=0)
    db.add(target); db.flush()
    rec = Recipe(name=target_name, output_quantity=1, output_unit="adet",
                 target_item_id=target.id, waste_percentage=0)
    db.add(rec); db.flush()
    db.add(RecipeIngredient(recipe_id=rec.id, item_id=ing_item_id, quantity=net_qty, unit="ml"))
    db.commit()
    return rec.id


def _staff_client(client, db):
    """Staff rolünde login'li client — `inventory.receive` varsayılan KAPALI."""
    u = User(username="raftaki_stajyer",
            password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode(),
            full_name="Stajyer", role="Staff", is_active=True)
    db.add(u); db.commit()
    r = client.post("/api/login", json={"username": "raftaki_stajyer", "password": "minerva123"},
                    headers=_HDR)
    assert r.status_code == 200, r.text
    return client


# ─── Numune girişi ──────────────────────────────────────────────────────────

def test_sample_receive_creates_separate_lot(authed_client: TestClient, db_session: Session):
    """24.08.2026 olayının regresyon kilidi: numune STOK DEĞİLDİR.

    Eskiden bu test `current_stock == 25` bekliyordu — numune de normal mal
    kabul gibi stoğa giriyordu.  Bu, kritik-stok uyarısını maskeliyor,
    üretim fizibilitesine karışıyor, Shopify'a satılabilir stok diye
    gidiyordu.  Artık numune Inventory satırı olarak kaydedilir ama
    current_stock'a dokunmaz ve HİÇBİR Transaction yazmaz — defter yalnız
    gerçek stok hareketlerini bilir.
    """
    it = _hammadde(db_session, "Gliserin", 0); db_session.commit()
    sup = _supplier(db_session, "Numune Tedarikçi Y"); db_session.commit()
    r = authed_client.post("/api/inventory/receive", json={
        "item_id": it.id, "supplier_id": sup, "lot_number": "NUM-1",
        "quantity": 25, "is_sample": True,
    }, headers=_HDR)
    assert r.status_code == 201, r.text
    assert "dahil edilmez" in r.json()["message"]
    inv = db_session.query(Inventory).filter(Inventory.lot_number == "NUM-1").first()
    assert inv.is_sample is True
    assert inv.supplier_id == sup
    assert inv.location == "Numune"          # numune varsayılan konum
    db_session.refresh(it)
    assert it.current_stock == 0             # numune STOĞA GİRMEZ
    assert db_session.query(Transaction).filter(
        Transaction.item_id == it.id).count() == 0    # ve defter YAZMAZ


def test_sample_upsert_merges_without_stock_or_transaction(authed_client: TestClient, db_session: Session):
    """Aynı numune lotuna ikinci giriş — miktar birleşir, defter yine sessiz."""
    it = _hammadde(db_session, "Ksantan Sakızı", 0); db_session.commit()
    body = {"item_id": it.id, "lot_number": "TEKRAR-1", "quantity": 10, "is_sample": True}
    authed_client.post("/api/inventory/receive", json=body, headers=_HDR)
    body["quantity"] = 5
    r = authed_client.post("/api/inventory/receive", json=body, headers=_HDR)
    assert r.status_code == 201, r.text
    inv = db_session.query(Inventory).filter(Inventory.lot_number == "TEKRAR-1").first()
    assert inv.quantity == 15
    db_session.refresh(it)
    assert it.current_stock == 0
    assert db_session.query(Transaction).filter(Transaction.item_id == it.id).count() == 0


def test_sample_does_not_merge_with_normal_lot(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "Setil Alkol", 0); db_session.commit()
    hdr = {"Origin": "http://testserver"}
    # Aynı lot no, biri normal biri numune → İKİ ayrı satır kalmalı
    authed_client.post("/api/inventory/receive", json={"item_id": it.id, "lot_number": "L9", "quantity": 10}, headers=hdr)
    authed_client.post("/api/inventory/receive", json={"item_id": it.id, "lot_number": "L9", "quantity": 5, "is_sample": True}, headers=hdr)
    rows = db_session.query(Inventory).filter(Inventory.item_id == it.id, Inventory.lot_number == "L9").all()
    assert len(rows) == 2
    assert {bool(r.is_sample) for r in rows} == {True, False}


def test_samples_endpoint_lists(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "Lanolin", 0); db_session.commit()
    sup = _supplier(db_session, "Alt Tedarikçi"); db_session.commit()
    authed_client.post("/api/inventory/receive", json={
        "item_id": it.id, "supplier_id": sup, "lot_number": "NUM-Z", "quantity": 12, "is_sample": True,
    }, headers={"Origin": "http://testserver"})
    r = authed_client.get("/api/inventory/samples")
    assert r.status_code == 200
    data = r.json()
    assert any(x["lot_number"] == "NUM-Z" and x["supplier_name"] == "Alt Tedarikçi" for x in data)


def test_available_lots_only_approved_positive(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "Su", 0)
    _lot(db_session, it.id, "A", 10)
    _lot(db_session, it.id, "B", 0)            # qty 0 → listelenmez
    z = _lot(db_session, it.id, "C", 5); z.status = "QUARANTINE"   # → listelenmez
    _lot(db_session, it.id, "D-NUM", 20, is_sample=True)  # numune → listelenmez (24.08.2026)
    db_session.commit()
    r = authed_client.post("/api/inventory/available-lots", json={"item_ids": [it.id]},
                           headers=_HDR)
    assert r.status_code == 200
    lots = r.json().get(str(it.id), [])
    assert [l["lot_number"] for l in lots] == ["A"]


# ─── Üretimde lot/tedarikçi tüketimi ────────────────────────────────────────

def test_production_consumes_chosen_lot(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "Yağ Bazı", 100)
    s1 = _supplier(db_session, "Tedarikçi Z"); s2 = _supplier(db_session, "Tedarikçi Y")
    a = _lot(db_session, it.id, "Z-LOT", 30, supplier_id=s1, age_days=10)
    b = _lot(db_session, it.id, "Y-LOT", 40, supplier_id=s2, age_days=1)
    db_session.commit()
    rid = _recipe_one_ingredient(db_session, "Krem A", it.id, 10)   # 1 üretim = 10 ml

    r = authed_client.post("/api/production", json={
        "recipe_id": rid, "produced_quantity": 1,
        "ingredient_lot_choices": {str(it.id): b.id},     # yeni lotu seç (FIFO'yu ez)
    }, headers=_HDR)
    assert r.status_code == 201, r.text
    db_session.refresh(a); db_session.refresh(b); db_session.refresh(it)
    assert b.quantity == 30          # 40 − 10 (seçilen lot düştü)
    assert a.quantity == 30          # diğer lot dokunulmadı
    assert it.current_stock == 90    # 100 − 10 (kaynak-of-truth)
    # Output transaction kaynak lotu taşımalı
    tx = (db_session.query(Transaction)
          .filter(Transaction.item_id == it.id, Transaction.transaction_type == "Output",
                  Transaction.lot_number == "Y-LOT").first())
    assert tx is not None and abs(tx.quantity - 10) < 1e-6


def test_production_rejects_sample_lot_choice(authed_client: TestClient, db_session: Session):
    """Numune lotu üretimde SEÇİLEMEZ — havuzda hiç yok (24.08.2026 olayı).

    Eskiden numune lotu FIFO'ya girebiliyor, tek lot varsa seçici hiç
    görünmeden sessizce tüketilebiliyordu.  Artık numune lot havuzunda
    olmadığı için `chosen_inv_id` numuneye işaret ederse "lot bulunamadı"
    hatası verir — normal, bilinen bir hata yolu, sessiz tüketim değil.
    """
    it = _hammadde(db_session, "Numune Bazı", 100)
    sample = _lot(db_session, it.id, "S-NUM", 40, is_sample=True)
    db_session.commit()
    rid = _recipe_one_ingredient(db_session, "Krem Numune", it.id, 10)

    r = authed_client.post("/api/production", json={
        "recipe_id": rid, "produced_quantity": 1,
        "ingredient_lot_choices": {str(it.id): sample.id},
    }, headers=_HDR)
    assert r.status_code == 400
    assert "bulunamadı" in r.json()["detail"].lower()
    db_session.refresh(sample); db_session.refresh(it)
    assert sample.quantity == 40      # mutasyon YOK
    assert it.current_stock == 100    # mutasyon YOK


def test_production_fifo_skips_sample_lot(authed_client: TestClient, db_session: Session):
    """Seçim yapılmadan (FIFO) üretimde en eski numune lotu ATLANIR."""
    it = _hammadde(db_session, "FIFO Numune Bazı", 100)
    sample = _lot(db_session, it.id, "ESKİ-NUM", 30, is_sample=True, age_days=30)  # en eski ama numune
    normal = _lot(db_session, it.id, "NORMAL", 40, age_days=5)
    db_session.commit()
    rid = _recipe_one_ingredient(db_session, "Krem FIFO Numune", it.id, 10)

    r = authed_client.post("/api/production", json={"recipe_id": rid, "produced_quantity": 1},
                           headers=_HDR)
    assert r.status_code == 201, r.text
    db_session.refresh(sample); db_session.refresh(normal)
    assert sample.quantity == 30      # numuneye dokunulmadı
    assert normal.quantity == 30      # 40 − 10, normal lottan düştü


def test_production_chosen_lot_insufficient_errors(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "Esans", 100)
    small = _lot(db_session, it.id, "KÜÇÜK", 5)     # yetersiz
    db_session.commit()
    rid = _recipe_one_ingredient(db_session, "Krem B", it.id, 10)  # 10 ml gerek
    r = authed_client.post("/api/production", json={
        "recipe_id": rid, "produced_quantity": 1,
        "ingredient_lot_choices": {str(it.id): small.id},
    }, headers={"Origin": "http://testserver"})
    assert r.status_code == 400
    assert "yetersiz" in r.json()["detail"].lower()
    db_session.refresh(small); db_session.refresh(it)
    assert small.quantity == 5        # mutasyon YOK
    assert it.current_stock == 100    # mutasyon YOK


def test_production_no_lots_legacy_behavior(authed_client: TestClient, db_session: Session):
    # Hiç Inventory lotu olmayan hammadde — eski davranış: aggregate düşüş
    it = _hammadde(db_session, "Eski Hammadde", 50); db_session.commit()
    rid = _recipe_one_ingredient(db_session, "Krem C", it.id, 10)
    r = authed_client.post("/api/production", json={"recipe_id": rid, "produced_quantity": 1},
                           headers={"Origin": "http://testserver"})
    assert r.status_code == 201, r.text
    db_session.refresh(it)
    assert it.current_stock == 40     # 50 − 10
    tx = (db_session.query(Transaction)
          .filter(Transaction.item_id == it.id, Transaction.transaction_type == "Output").first())
    assert tx is not None and tx.lot_number is None   # lot kaydı yok


def test_production_fifo_when_no_choice(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "FIFO Bazı", 100)
    old = _lot(db_session, it.id, "ESKI", 30, age_days=20)
    new = _lot(db_session, it.id, "YENI", 40, age_days=1)
    db_session.commit()
    rid = _recipe_one_ingredient(db_session, "Krem D", it.id, 10)
    r = authed_client.post("/api/production", json={"recipe_id": rid, "produced_quantity": 1},
                           headers={"Origin": "http://testserver"})
    assert r.status_code == 201, r.text
    db_session.refresh(old); db_session.refresh(new)
    assert old.quantity == 20         # en eski lot düştü (FIFO)
    assert new.quantity == 40         # yeni lot dokunulmadı


# ─── Guard rail'ler: RBAC + domain (24.08.2026 olayı — receive'de hiçbiri yoktu) ──

def test_receive_requires_permission_staff_403(client: TestClient, db_session: Session):
    it = _hammadde(db_session, "Yetkisiz Hammadde", 0); db_session.commit()
    c = _staff_client(client, db_session)
    r = c.post("/api/inventory/receive", json={"item_id": it.id, "lot_number": "X1", "quantity": 5},
              headers=_HDR)
    assert r.status_code == 403
    db_session.refresh(it)
    assert it.current_stock == 0


def test_receive_rejects_wrong_domain(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "Takviye Hammaddesi", 0, domain="supplement"); db_session.commit()
    # authed_client varsayılan 'cosmetics' panelinde — supplement ürününe kabul yapılamaz
    r = authed_client.post("/api/inventory/receive",
                           json={"item_id": it.id, "lot_number": "SUP-X", "quantity": 5},
                           headers=_HDR)
    assert r.status_code == 400
    db_session.refresh(it)
    assert it.current_stock == 0


def test_convert_requires_permission_staff_403(client: TestClient, db_session: Session, authed_client: TestClient):
    it = _hammadde(db_session, "Çevrilecek Hammadde", 0); db_session.commit()
    authed_client.post("/api/inventory/receive",
                       json={"item_id": it.id, "lot_number": "CNV-1", "quantity": 8, "is_sample": True},
                       headers=_HDR)
    inv = db_session.query(Inventory).filter(Inventory.lot_number == "CNV-1").first()
    c = _staff_client(client, db_session)
    r = c.post(f"/api/inventory/samples/{inv.id}/convert", headers=_HDR)
    assert r.status_code == 403
    db_session.refresh(inv)
    assert inv.is_sample is True


# ─── Numuneyi stoğa çevirme ──────────────────────────────────────────────────

def test_convert_sample_writes_input_and_bumps_stock(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "Çevrilecek Yağ", 0); db_session.commit()
    authed_client.post("/api/inventory/receive",
                       json={"item_id": it.id, "lot_number": "CNV-2", "quantity": 12, "is_sample": True},
                       headers=_HDR)
    inv = db_session.query(Inventory).filter(Inventory.lot_number == "CNV-2").first()
    tx_before = db_session.query(Transaction).filter(Transaction.item_id == it.id).count()
    assert tx_before == 0

    r = authed_client.post(f"/api/inventory/samples/{inv.id}/convert", headers=_HDR)
    assert r.status_code == 200, r.text

    db_session.refresh(it)
    assert it.current_stock == 12
    db_session.expire(inv)
    inv2 = db_session.query(Inventory).filter(Inventory.id == inv.id).first()
    assert inv2.is_sample is False
    assert inv2.location is None       # "Numune" konumu temizlendi
    tx = (db_session.query(Transaction)
          .filter(Transaction.item_id == it.id, Transaction.transaction_type == "Input").first())
    assert tx is not None and abs(tx.quantity - 12) < 1e-6
    assert "çevrildi" in tx.notes.lower()


def test_convert_merges_into_existing_normal_lot(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "Birleşecek Yağ", 20); db_session.commit()
    normal = _lot(db_session, it.id, "AYNI-LOT", 20)
    db_session.commit()
    authed_client.post("/api/inventory/receive",
                       json={"item_id": it.id, "lot_number": "AYNI-LOT", "quantity": 8, "is_sample": True},
                       headers=_HDR)
    sample = db_session.query(Inventory).filter(
        Inventory.item_id == it.id, Inventory.lot_number == "AYNI-LOT",
        Inventory.is_sample == True).first()   # noqa: E712

    r = authed_client.post(f"/api/inventory/samples/{sample.id}/convert", headers=_HDR)
    assert r.status_code == 200, r.text
    assert "birleşti" in r.json()["message"]

    db_session.refresh(it)
    assert it.current_stock == 28   # 20 + 8
    db_session.refresh(normal)
    assert normal.quantity == 28    # 20 + 8, tek satırda toplandı
    remaining = db_session.query(Inventory).filter(
        Inventory.item_id == it.id, Inventory.lot_number == "AYNI-LOT").all()
    assert len(remaining) == 1      # numune satırı silindi, tek satır kaldı


def test_convert_cross_domain_not_found(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "Panel Değişecek Hammadde", 0); db_session.commit()
    authed_client.post("/api/inventory/receive",
                       json={"item_id": it.id, "lot_number": "DOM-1", "quantity": 3, "is_sample": True},
                       headers=_HDR)
    inv = db_session.query(Inventory).filter(Inventory.lot_number == "DOM-1").first()
    authed_client.post("/api/domain/switch", json={"domain": "supplement"}, headers=_HDR)
    r = authed_client.post(f"/api/inventory/samples/{inv.id}/convert", headers=_HDR)
    assert r.status_code == 404
    authed_client.post("/api/domain/switch", json={"domain": "cosmetics"}, headers=_HDR)
    db_session.refresh(inv)
    assert inv.is_sample is True   # dokunulmadı


# ─── Ad çakışması (Türkçe-katlanmış) — 24.08.2026 olayının kapısı ───────────

def test_create_item_blocks_turkish_folded_duplicate(authed_client: TestClient, db_session: Session):
    it = _hammadde(db_session, "BADEM YAĞI", 50); db_session.commit()
    r = authed_client.post("/api/items",
                           json={"name": "badem yagi", "category": "Hammadde", "unit": "g"},
                           headers=_HDR)
    assert r.status_code == 409, r.text
    assert r.json()["existing"]["id"] == it.id
    assert db_session.query(Item).filter(Item.name == "badem yagi").count() == 0


def test_create_item_force_overrides_conflict(authed_client: TestClient, db_session: Session):
    _hammadde(db_session, "HİNT YAĞI", 50); db_session.commit()
    r = authed_client.post("/api/items",
                           json={"name": "hint yagi", "category": "Hammadde", "unit": "g",
                                 "force": True},
                           headers=_HDR)
    assert r.status_code == 201, r.text
    assert db_session.query(Item).filter(Item.name == "hint yagi").count() == 1


def test_create_item_conflict_checks_name_tr_too(authed_client: TestClient, db_session: Session):
    it = Item(name="Rosemary Oil", name_tr="Biberiye Yağı", category="Hammadde",
             unit="ml", current_stock=0, domain="cosmetics")
    db_session.add(it); db_session.commit()
    r = authed_client.post("/api/items",
                           json={"name": "biberiye yagi", "category": "Hammadde", "unit": "ml"},
                           headers=_HDR)
    assert r.status_code == 409
    assert r.json()["existing"]["id"] == it.id


def test_update_item_rename_collision(authed_client: TestClient, db_session: Session):
    a = _hammadde(db_session, "Çinko Oksit", 10)
    b = _hammadde(db_session, "Çinko Oksit Yedek", 5)
    db_session.commit()
    r = authed_client.put(f"/api/items/{b.id}",
                          json={"name": "cinko oksit", "category": "Hammadde", "unit": "g"},
                          headers=_HDR)
    assert r.status_code == 409
    assert r.json()["existing"]["id"] == a.id


def test_update_item_same_name_no_self_conflict(authed_client: TestClient, db_session: Session):
    """Ürünün kendi adını değiştirmeden kaydetmesi kendine çakışma dönmemeli."""
    it = _hammadde(db_session, "Kakao Yağı", 10); db_session.commit()
    r = authed_client.put(f"/api/items/{it.id}",
                          json={"name": "Kakao Yağı", "category": "Hammadde", "unit": "kg"},
                          headers=_HDR)
    assert r.status_code == 200, r.text


def test_create_item_requires_unit(authed_client: TestClient, db_session: Session):
    r = authed_client.post("/api/items", json={"name": "Birimsiz Ürün", "category": "Hammadde"},
                           headers=_HDR)
    assert r.status_code == 422
    assert db_session.query(Item).filter(Item.name == "Birimsiz Ürün").count() == 0


# ─── Defter tutarlılığı ──────────────────────────────────────────────────────

def test_snapshot_reconstruction_unaffected_by_sample_lifecycle(authed_client: TestClient, db_session: Session):
    """Numune kabul + stoğa çevirme sonrası `compute_stock_at` sapmamalı.

    core/snapshots.py yalnız Input/Output/Adjustment sayar; numune kabul
    HİÇBİRİNİ yazmaz (defter dışı), çevirme TAM O ANDA bir Input yazar.
    İkisi de current_stock ile Transaction toplamının birbirini tutmasını
    bozmamalı — aylık rapor rekonstrüksiyonunun regresyon kilidi.
    """
    it = _hammadde(db_session, "Snapshot Testi Yağı", 5); db_session.commit()
    authed_client.post("/api/inventory/receive",
                       json={"item_id": it.id, "lot_number": "SNAP-1", "quantity": 9, "is_sample": True},
                       headers=_HDR)
    db_session.refresh(it)
    assert it.current_stock == 5     # numune aşamasında STOK SABİT

    inv = db_session.query(Inventory).filter(Inventory.lot_number == "SNAP-1").first()
    authed_client.post(f"/api/inventory/samples/{inv.id}/convert", headers=_HDR)
    db_session.expire(it); db_session.refresh(it)
    assert it.current_stock == 14    # 5 + 9, çevrilince stoğa girdi

    # Rekonstrüksiyon = current_stock − (eom SONRASI hareketler).  eom "şimdi
    # + 1 gün" olduğu için sonrasında hiç hareket yok → sonuç current_stock'un
    # AYNISI olmalı.  Numune aşamasının deftere hiç dokunmaması + çevirmenin
    # TAM O ANDA tek bir Input yazması sayesinde ikisi hep tutarlı kalıyor.
    reconstructed, _ = compute_stock_at(db_session, datetime.utcnow() + timedelta(days=1))
    assert abs(reconstructed.get(it.id, 0.0) - it.current_stock) < 1e-6
