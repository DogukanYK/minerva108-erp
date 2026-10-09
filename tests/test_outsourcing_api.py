"""Fason API + QC entegrasyonu uçtan uca: tek yerel düşüm, tek QC girişi.

Yalnız tests/conftest.py'nin izole test DB'si; sentetik firma/malzeme adları.
Motor kuralları tests/test_outsourcing.py'de; burada HTTP sözleşmesi, yetki,
panel izolasyonu, QC uçları, defter/rapor tutarlılığı ve eşzamanlılık.
"""
import json
import threading
import time
from datetime import datetime, timedelta
from io import BytesIO

import bcrypt
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from core import outsourcing as osrc
from core.outsourcing_access import SENTINEL, backfill_permissions, grant_isik_approval
from core.snapshots import compute_stock_at
from database import (AppSetting, Inventory, Item, OutsourcingContainer, OutsourcingJob,
                      OutsourcingReceipt, OutsourcingShipmentLine, Recipe, RecipeIngredient,
                      SessionLocal, Supplier, Transaction, User)

HDR = {"Origin": "http://testserver"}
API = "/api/outsourcing"
FUTURE = (datetime.utcnow() + timedelta(days=700)).date().isoformat()
PRIVATE = ["Gizli Hammadde Kartı", "GIZLI-RAW-SKU", "Gizli Tedarikçi AŞ", "GIZLI-KAYNAK-LOT",
           "Gizli Reçete Adı"]


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
    """client fixture'ı limiter'ı sıfırlar; her rol ayrı TestClient (ayrı çerez)."""
    db = db_session
    sup = Supplier(name="Gizli Tedarikçi AŞ", domain="cosmetics")
    db.add(sup); db.flush()
    raw = Item(name="Gizli Hammadde Kartı", sku="GIZLI-RAW-SKU", unit="g", category="Hammadde",
               current_stock=100, supplier_id=sup.id, domain="cosmetics")
    bottle = Item(name="Gizli Şişe", sku="GIZLI-SISE", unit="adet", category="Ambalaj",
                  pkg_type="şişe", current_stock=50, domain="cosmetics")
    label = Item(name="Gizli Etiket TR", sku="GIZLI-ET-TR", unit="adet", category="Ambalaj",
                 pkg_type="etiket", language="TR", label_group="FASON-TEST", current_stock=50,
                 domain="cosmetics")
    finished = Item(name="Gizli Mamul", sku="GIZLI-MAMUL", unit="adet", category="Bitmiş Ürün",
                    current_stock=0, domain="cosmetics")
    db.add_all([raw, bottle, label, finished]); db.flush()
    raw_lot = Inventory(item_id=raw.id, supplier_id=sup.id, lot_number="GIZLI-KAYNAK-LOT",
                        quantity=100, expiry_date="2099-12-31", status="APPROVED",
                        qc_required=False, is_sample=False, domain="cosmetics")
    bottle_lot = Inventory(item_id=bottle.id, lot_number="SISE-LOT-1", quantity=50,
                           status="APPROVED", qc_required=False, is_sample=False, domain="cosmetics")
    db.add_all([raw_lot, bottle_lot])
    recipe = Recipe(name="Gizli Reçete Adı", target_item_id=finished.id, output_quantity=1,
                    output_unit="adet", waste_percentage=10, domain="cosmetics")
    db.add(recipe); db.flush()
    for item, qty, phase in [(raw, 2, "A"), (bottle, 1, None), (label, 1, None)]:
        db.add(RecipeIngredient(recipe_id=recipe.id, item_id=item.id, quantity=qty,
                                unit=item.unit, phase=phase))
    db.commit()
    owner = _login("dogukan")
    return {"db": db, "owner": owner, "technical": _login("songul"), "manager": _login("isik"),
            "raw": raw, "bottle": bottle, "label": label, "finished": finished,
            "raw_lot": raw_lot, "bottle_lot": bottle_lot, "recipe": recipe, "supplier": sup}


def _ok(r, status=200):
    assert r.status_code == status, r.text
    return r.json()


def _prepare_job(w):
    """Firma + üç kod + iş + kaplar (hammadde iki etap kabı, lotsuz ambalaj/etiket)."""
    o = w["owner"]
    _ok(o.post(f"{API}/partners", json={"name": "Sentetik Fasoncu"}, headers=HDR))
    partner = _ok(o.get(f"{API}/bootstrap"))["partners"][0]["id"]
    for key, code in [("raw", "M-A01"), ("bottle", "M-B01"), ("label", "M-T01")]:
        _ok(o.post(f"{API}/material-codes", headers=HDR, json={
            "partner_id": partner, "item_id": w[key].id, "code": code,
            "specification": f"Sentetik {key} tanımı form/grade v1",
            "safety_instructions": "Eldiven ve gözlük kullanın."}))
    pv = _ok(o.post(f"{API}/preview", headers=HDR, json={
        "partner_id": partner, "recipe_id": w["recipe"].id, "quantity": 10, "label_language": "TR"}))
    raw_line = next(m for m in pv["materials"] if m["item_id"] == w["raw"].id)
    assert raw_line["quantity"] == pytest.approx(22.0)            # 2 g × 10 × 1.10 fire
    detail = _ok(o.post(f"{API}/jobs", headers=HDR, json={
        "partner_id": partner, "recipe_id": w["recipe"].id, "quantity": 10, "label_language": "TR",
        "external_product_name": "P-X01", "external_notes": "PROSES:\nM-A01 ısıt.\n\nGÜVENLİK:\nEldiven.",
        "approver_ids": {"technical": _uid(w["db"], "songul"), "owner": _uid(w["db"], "dogukan"),
                         "manager": _uid(w["db"], "isik")},
        "dispatch_quantities": {str(w["raw"].id): 25}}))
    job_id = detail["job"]["id"]
    codes = {m["code"]: m for m in detail["materials"]}
    for code, qty, lot in [("M-A01", 15, w["raw_lot"].id), ("M-A01", 10, w["raw_lot"].id),
                           ("M-B01", 10, None), ("M-T01", 10, None)]:
        body = {"material_code_id": codes[code]["code_id"], "quantity": qty, "unit": codes[code]["unit"]}
        if lot:
            body["inventory_id"] = lot
        detail = _ok(o.post(f"{API}/jobs/{job_id}/containers", headers=HDR, json=body))
    return job_id, detail


def _approve_all(w, job_id):
    for role in ("technical", "owner", "manager"):
        job = _ok(w[role].get(f"{API}/jobs/{job_id}"))["job"]
        detail = _ok(w[role].post(f"{API}/jobs/{job_id}/approve", headers=HDR,
                                  json={"revision": job["revision"], "packet_hash": job["packet_hash"]}))
    assert detail["job"]["status"] == "APPROVED"
    return detail


def _pdf_text(data):
    reader = PdfReader(BytesIO(data))
    meta = " ".join(str(v) for v in (reader.metadata or {}).values())
    return " ".join(page.extract_text() or "" for page in reader.pages) + " " + meta


def _stock(db, item):
    db.expire_all()
    return db.get(Item, item.id).current_stock


# ─── Uçtan uca ───────────────────────────────────────────────────────────────

def test_full_flow_posts_one_local_debit_and_one_qc_input(world):
    w = world
    db, o = w["db"], w["owner"]
    job_id, detail = _prepare_job(w)

    # Onaydan önce belge yok; önizleme yine kodlu ve isim içermez
    assert _ok(o.get(f"{API}/jobs/{job_id}"))["actions"]["documents"] is False
    r = o.get(f"{API}/jobs/{job_id}/documents/sheet")
    assert r.status_code == 409 and r.json()["code"] == "approval_required"
    assert not any(p in json.dumps(detail["external_packet"]) for p in PRIVATE)

    _approve_all(w, job_id)
    for kind in ("sheet", "labels", "manifest", "report"):
        r = o.get(f"{API}/jobs/{job_id}/documents/{kind}")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        assert f"fason_{kind}_r" in r.headers["content-disposition"]
        text = _pdf_text(r.content)
        assert not any(p in text for p in PRIVATE), kind

    detail = _ok(o.get(f"{API}/jobs/{job_id}"))
    by_code = {}
    for c in detail["containers"]:
        by_code.setdefault(c["code"], []).append(c)
    first = [by_code["M-A01"][0]["id"], by_code["M-B01"][0]["id"], by_code["M-T01"][0]["id"]]
    guard = {"revision": detail["job"]["revision"], "packet_hash": detail["job"]["packet_hash"]}
    before_dispatch = datetime.utcnow()
    time.sleep(0.02)

    # Etap 1 — idempotent; aynı anahtar farklı içerik → 409
    body = {"idempotency_key": "k-etap-1", "container_ids": first, **guard}
    _ok(o.post(f"{API}/jobs/{job_id}/dispatch", headers=HDR, json=body))
    _ok(o.post(f"{API}/jobs/{job_id}/dispatch", headers=HDR, json=body))
    r = o.post(f"{API}/jobs/{job_id}/dispatch", headers=HDR,
               json={**body, "container_ids": [by_code["M-A01"][1]["id"]]})
    assert r.status_code == 409 and r.json()["code"] == "idempotency_conflict"
    assert _stock(db, w["raw"]) == pytest.approx(85)
    assert _stock(db, w["bottle"]) == pytest.approx(40)
    assert db.get(Inventory, w["bottle_lot"].id).quantity == pytest.approx(40)   # lotsuz kap FIFO lotu düştü
    assert db.get(Inventory, w["raw_lot"].id).quantity == pytest.approx(85)

    # Etap 2
    _ok(o.post(f"{API}/jobs/{job_id}/dispatch", headers=HDR,
               json={"idempotency_key": "k-etap-2", "container_ids": [by_code["M-A01"][1]["id"]], **guard}))
    assert _stock(db, w["raw"]) == pytest.approx(75)
    outputs = db.query(Transaction).filter(Transaction.transaction_type == "Output").all()
    assert len(outputs) == 4 and all(t.notes.startswith("Fason sevk") for t in outputs)
    lines = db.query(OutsourcingShipmentLine).all()
    assert sorted(l.transaction_id for l in lines) == sorted(t.id for t in outputs)

    # Sevk transferi top-usage'da kullanım DEĞİL
    usage = {r["item_id"]: r["total_used"] for r in _ok(o.get("/api/reports/top-usage"))}
    assert w["raw"].id not in usage

    a1, a2 = by_code["M-A01"][0]["id"], by_code["M-A01"][1]["id"]
    _ok(o.post(f"{API}/jobs/{job_id}/consumption", headers=HDR, json={"idempotency_key": "k-cons", "entries": [
        {"container_id": a1, "consumed_quantity": 14, "waste_quantity": 1, "reason": "Dökülme kaybı"},
        {"container_id": a2, "consumed_quantity": 0.006, "unit": "kg"},     # 6 g — kesin birim dönüşümü
        {"container_id": by_code["M-B01"][0]["id"], "consumed_quantity": 10},
        {"container_id": by_code["M-T01"][0]["id"], "consumed_quantity": 10}]}))
    assert _stock(db, w["raw"]) == pytest.approx(75)              # dış tüketim yerel stoğu tekrar düşmez
    r = o.post(f"{API}/jobs/{job_id}/consumption", headers=HDR, json={"idempotency_key": "k-cons-2", "entries": [
        {"container_id": a2, "consumed_quantity": 5}]})
    assert r.status_code == 409 and r.json()["code"] == "over_external_balance"
    usage = {r["item_id"]: r["total_used"] for r in _ok(o.get("/api/reports/top-usage"))}
    assert usage[w["raw"].id] == pytest.approx(21)                # 14 + 6 tüketim + 1 fire

    _ok(o.post(f"{API}/jobs/{job_id}/returns", headers=HDR, json={"idempotency_key": "k-ret", "entries": [
        {"container_id": a2, "quantity": 4}]}))
    assert _stock(db, w["raw"]) == pytest.approx(75)              # iade QC'ye kadar stok değil
    _ok(o.post(f"{API}/jobs/{job_id}/receipts", headers=HDR, json={
        "idempotency_key": "k-rec", "quantity": 10, "sample_quantity": 2, "unit": "adet",
        "external_lot": "EXT-9", "expiry_date": FUTURE}))
    assert _stock(db, w["finished"]) == pytest.approx(0)

    r = o.post(f"{API}/jobs/{job_id}/close", headers=HDR, json={"idempotency_key": "k-close-early"})
    assert r.status_code == 409 and r.json()["code"] == "qc_pending"

    # QC listesi "Fason" kaynağını gösterir; iki farklı QC ucu da tek Input yazar
    qc = {row["id"]: row for row in _ok(o.get("/api/qc/quarantine"))}
    fason = db.query(Inventory).filter(Inventory.outsourcing_receipt_id.isnot(None)).all()
    assert {qc[i.id]["source"] for i in fason} == {"Fason"}
    rec = {db.get(OutsourcingReceipt, i.outsourcing_receipt_id).kind: i for i in fason}
    # İade AYRI satırdır ama özgün lot no + tedarikçiyi taşır (izlenebilirlik)
    assert rec["return"].lot_number == "GIZLI-KAYNAK-LOT" and rec["return"].id != w["raw_lot"].id
    assert rec["return"].supplier_id == w["supplier"].id
    assert rec["finished"].lot_number.startswith(f"FS-{job_id}-R")
    form = {"status": "APPROVED", "checklist": {"q01": "Evet"}, "notes": "uygun"}
    _ok(o.post(f"/api/inventory/{rec['finished'].id}/qc-approve", headers=HDR, json=form))
    _ok(o.post(f"/api/inventory/{rec['finished_sample'].id}/qc-approve", headers=HDR, json=form))
    _ok(o.post(f"/api/qc/process/{rec['return'].id}", headers=HDR, json={"status": "APPROVED", "notes": "ok"}))
    r = o.post(f"/api/inventory/{rec['finished'].id}/qc-approve", headers=HDR, json=form)
    assert r.status_code == 400                                   # karar kesin
    assert _stock(db, w["finished"]) == pytest.approx(8)          # şahit 2 adet kullanılabilir stoğa girmez
    # Şahit numune "alternatif tedarikçi numunesi" DEĞİL: RETAINED, numune
    # listesinde yok, stoğa çevrilemez, FIFO havuzuna girmez
    witness = db.get(Inventory, rec["finished_sample"].id)
    assert (witness.status, witness.is_sample, witness.quantity) == ("RETAINED", False, 2)
    assert all(r["inventory_id"] != witness.id for r in _ok(o.get("/api/inventory/samples?include_empty=true")))
    assert o.post(f"/api/inventory/samples/{witness.id}/convert", headers=HDR, json={}).status_code == 404
    from core.stock_lots import plan_fifo
    allocations, uncovered = plan_fifo(db, db.get(Item, w["finished"].id), 10, lock=False)
    assert witness.id not in {lot.id for lot, _ in allocations} and uncovered == pytest.approx(2)
    db.rollback()
    assert _stock(db, w["raw"]) == pytest.approx(79)              # 75 + 4 iade
    inputs = db.query(Transaction).filter(Transaction.transaction_type == "Input").all()
    assert sorted((t.item_id, t.quantity) for t in inputs) == sorted([(w["finished"].id, 8), (w["raw"].id, 4)])
    assert all(t.notes.startswith("Fason") for t in inputs)

    # Defter rekonstrüksiyonu: sevk öncesi an = başlangıç stokları
    at, _ = compute_stock_at(db, before_dispatch)
    assert at[w["raw"].id] == pytest.approx(100)
    assert at[w["bottle"].id] == pytest.approx(50)
    assert at[w["finished"].id] == pytest.approx(0)

    detail = _ok(o.post(f"{API}/jobs/{job_id}/close", headers=HDR, json={"idempotency_key": "k-close"}))
    assert detail["job"]["status"] == "CLOSED"
    finished_row = next(r for r in detail["receipts"] if r["kind"] == "finished")
    assert (finished_row["quantity"], finished_row["sample_quantity"], finished_row["saleable_quantity"]) == (10, 2, 8)

    # Aylık rapor: sevk transfer bölümünde, fiilî kullanım malzemelerde
    from core.monthly_report import gather_report_data
    now = datetime.utcnow()
    data = gather_report_data(db, now.year, now.month)
    transfers = {m["name"]: m["qty"] for m in data["fason_transfers"]}
    assert transfers["Gizli Hammadde Kartı"] == pytest.approx(25)
    raw_use = next(m for m in data["materials"] if m["name"] == "Gizli Hammadde Kartı")
    assert raw_use["qty"] == pytest.approx(21) and raw_use["fason_waste"] == pytest.approx(1)


# ─── Yetki ve panel ──────────────────────────────────────────────────────────

def test_permissions_mapping_secrecy_distributor_and_domain(world):
    w = world
    db, o = w["db"], w["owner"]
    job_id, _ = _prepare_job(w)

    # LabTech varsayılanı: fason kapalı
    assert _login("meltem").get(f"{API}/jobs").status_code == 403
    # Distribütör yetki verilse bile iç modüle giremez (API + sayfa)
    _user(db, "bayi1", "Distributor", {"outsourcing": {"view": True, "manage": True}})
    bayi = _login("bayi1")
    assert bayi.get(f"{API}/jobs").status_code == 403
    assert bayi.get("/outsourcing", follow_redirects=False).status_code == 302

    # Yalnız görüntüleme: gerçek eşleştirme sızmaz, yazma uçları kapalı
    _user(db, "izleyici", "Staff", {"outsourcing": {"view": True}})
    viewer = _login("izleyici")
    boot = _ok(viewer.get(f"{API}/bootstrap"))
    assert not {"items", "material_codes", "lots"} & set(boot)
    detail = _ok(viewer.get(f"{API}/jobs/{job_id}"))
    assert not any(p in json.dumps(detail) for p in PRIVATE)
    assert all("item_name" not in m and "specification" not in m for m in detail["materials"])
    assert viewer.post(f"{API}/preview", headers=HDR, json={
        "partner_id": 1, "recipe_id": w["recipe"].id, "quantity": 1}).status_code == 403
    job = detail["job"]
    # Atanmamış hesap onay veremez (yetkisi olsa bile)
    _user(db, "baskasi", "Staff", {"outsourcing": {"view": True, "approve": True}})
    r = _login("baskasi").post(f"{API}/jobs/{job_id}/approve", headers=HDR,
                               json={"revision": job["revision"], "packet_hash": job["packet_hash"]})
    assert r.status_code == 403 and r.json()["code"] == "not_assigned_approver"
    # Kap hazırlığı kaynak lot gördüğü için mapping de ister
    _user(db, "hazirlayici", "Staff", {"outsourcing": {"view": True, "manage": True}})
    r = _login("hazirlayici").post(f"{API}/jobs/{job_id}/containers", headers=HDR,
                                   json={"material_code_id": 1, "quantity": 1})
    assert r.status_code == 403

    # Mapping yetkilisi gerçek kartı görür
    full = _ok(o.get(f"{API}/jobs/{job_id}"))
    assert any(m.get("item_name") == "Gizli Hammadde Kartı" for m in full["materials"])

    # Diğer panelde iş yok
    o.cookies.set("active_domain", "supplement")
    assert o.get(f"{API}/jobs/{job_id}").status_code == 404
    assert _ok(o.get(f"{API}/jobs"))["jobs"] == []


# ─── QC bağı, mal kabul izolasyonu ───────────────────────────────────────────

def _to_receipts(w):
    o = w["owner"]
    job_id, _ = _prepare_job(w)
    _approve_all(w, job_id)
    d = _ok(o.get(f"{API}/jobs/{job_id}"))
    _ok(o.post(f"{API}/jobs/{job_id}/dispatch", headers=HDR, json={
        "idempotency_key": "k1", "container_ids": [c["id"] for c in d["containers"]],
        "revision": d["job"]["revision"], "packet_hash": d["job"]["packet_hash"]}))
    _ok(o.post(f"{API}/jobs/{job_id}/receipts", headers=HDR, json={
        "idempotency_key": "k2", "quantity": 5, "sample_quantity": 0, "external_lot": "EXT-1",
        "expiry_date": FUTURE}))
    db = w["db"]
    db.expire_all()
    return job_id, db.query(Inventory).filter(Inventory.outsourcing_receipt_id.isnot(None)).one()


def test_qc_rejects_broken_receipt_link_with_reason_and_posts_nothing(world):
    w = world
    db, o = w["db"], w["owner"]
    _, inv = _to_receipts(w)
    inv.quantity = 6                                   # karantina satırı kabulden sapmış
    db.commit()
    r = o.post(f"/api/inventory/{inv.id}/qc-approve", headers=HDR,
               json={"status": "APPROVED", "checklist": {"q01": "Evet"}})
    assert r.status_code == 409 and r.json()["code"] == "receipt_source_mismatch"
    assert db.query(Transaction).filter_by(transaction_type="Input").count() == 0
    assert _stock(db, w["finished"]) == 0
    db.expire_all()
    assert db.get(Inventory, inv.id).status == "QUARANTINE"


def test_qc_rejection_posts_no_stock_and_normal_receiving_never_merges(world):
    w = world
    db, o = w["db"], w["owner"]
    _, inv = _to_receipts(w)
    lot = inv.lot_number
    # Bekleyen fason lotuna elle stok düzeltmesi yapılamaz
    r = o.post("/api/inventory/adjust", headers=HDR, json={
        "item_id": w["finished"].id, "new_quantity": 99, "reason": "deneme düzeltmesi",
        "lot_number": lot})
    assert r.status_code == 400 and "Fason" in r.json()["detail"]
    # Aynı kart + aynı lot no ile normal mal kabul → AYRI satır, fason satırı değişmez
    _ok(o.post("/api/inventory/receive", headers=HDR, json={
        "item_id": w["finished"].id, "lot_number": lot, "quantity": 3}), 201)
    db.expire_all()
    rows = db.query(Inventory).filter_by(item_id=w["finished"].id, lot_number=lot).all()
    assert len(rows) == 2 and db.get(Inventory, inv.id).quantity == 5
    _ok(o.post(f"/api/qc/process/{inv.id}", headers=HDR, json={"status": "REJECTED", "notes": "uygunsuz"}))
    assert _stock(db, w["finished"]) == pytest.approx(3)          # yalnız normal kabul
    assert db.get(OutsourcingReceipt, inv.outsourcing_receipt_id).status == "REJECTED"
    assert db.query(Transaction).filter(Transaction.notes.like("Fason kabul%")).count() == 0


# ─── Eşzamanlılık ────────────────────────────────────────────────────────────

def test_concurrent_dispatch_of_same_container_debits_once(world):
    w = world
    o = w["owner"]
    job_id, _ = _prepare_job(w)
    _approve_all(w, job_id)
    d = _ok(o.get(f"{API}/jobs/{job_id}"))
    container = d["containers"][0]["id"]
    guard = {"revision": d["job"]["revision"], "packet_hash": d["job"]["packet_hash"]}
    actor = {"sub": str(_uid(w["db"], "dogukan")), "username": "dogukan", "full_name": "Doğukan"}
    results, started = {}, threading.Event()

    def run(name, delay):
        s = SessionLocal()
        try:
            if name == "b":
                started.wait(5)
            osrc.dispatch(s, "cosmetics", actor, job_id,
                          {"idempotency_key": f"k-{name}", "container_ids": [container], **guard})
            if name == "a":
                started.set()
                time.sleep(delay)                     # kilit açıkken B bekliyor
            s.commit()
            results[name] = "ok"
        except osrc.OutsourcingError as exc:
            s.rollback()
            results[name] = exc.code
        finally:
            started.set()
            s.close()

    threads = [threading.Thread(target=run, args=("a", 0.4)), threading.Thread(target=run, args=("b", 0))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(results.values()) == ["container_already_dispatched", "ok"], results
    db = w["db"]
    db.expire_all()
    assert db.query(OutsourcingShipmentLine).filter_by(container_id=container).count() == 1
    assert db.query(Transaction).filter_by(transaction_type="Output").count() == 1


# ─── Yetki backfill + Işık betiği ────────────────────────────────────────────

def test_backfill_adds_denied_category_once_and_grant_script_is_dry_run_by_default(db_session):
    db = db_session
    db.query(AppSetting).filter_by(key=SENTINEL).delete()
    isik = db.query(User).filter_by(username="isik").one()
    isik.permissions = json.dumps({"items": {"view": True}, "b2b": {"view": True, "confirm": True}})
    db.commit()
    assert backfill_permissions(db) == 1
    db.expire_all()
    perms = json.loads(db.query(User).filter_by(username="isik").one().permissions)
    assert perms["outsourcing"] == {a: False for a in ("view", "manage", "mapping", "approve", "dispatch", "record")}
    assert perms["b2b"] == {"view": True, "confirm": True}
    assert backfill_permissions(db) == 0                      # sentinel → no-op

    dry = grant_isik_approval(db, commit=False)
    assert dry["changed"] is True
    db.expire_all()
    assert json.loads(db.query(User).filter_by(username="isik").one().permissions)["outsourcing"]["approve"] is False
    done = grant_isik_approval(db, commit=True)
    assert done["changed"] is True
    db.expire_all()
    perms = json.loads(db.query(User).filter_by(username="isik").one().permissions)
    assert perms["outsourcing"]["view"] is True and perms["outsourcing"]["approve"] is True
    assert perms["outsourcing"]["dispatch"] is False and perms["b2b"]["confirm"] is True


# ─── Hazırlık kapasitesi, lot birleştirme, kart silme ───────────────────────

def test_container_cannot_overbook_source_lot(world):
    w = world
    db, o = w["db"], w["owner"]
    w["raw_lot"].quantity = 30          # kart stoğu 100 ama bu lotta 30 g
    db.commit()
    job_id, detail = _prepare_job(w)    # iki hammadde kabı: 15 + 10 = 25 g ≤ 30
    code = next(m for m in detail["materials"] if m["code"] == "M-A01")
    # İkinci bir iş aynı lottan 10 g daha isterse 25 + 10 > 30 → reddedilir
    second = _ok(o.post(f"{API}/jobs", headers=HDR, json={
        "partner_id": detail["job"]["partner_id"], "recipe_id": w["recipe"].id, "quantity": 1,
        "label_language": "TR", "external_product_name": "P-X02", "external_notes": "M-A01 kullan.",
        "approver_ids": {"technical": _uid(db, "songul"), "owner": _uid(db, "dogukan"),
                         "manager": _uid(db, "isik")},
        "dispatch_quantities": {str(w["raw"].id): 10}}))
    r = o.post(f"{API}/jobs/{second['job']['id']}/containers", headers=HDR, json={
        "material_code_id": code["code_id"], "quantity": 10, "unit": "g", "inventory_id": w["raw_lot"].id})
    assert r.status_code == 409 and r.json()["code"] == "insufficient_lot"
    # Aynı işteki gönderilmemiş kap kaldırılınca kapasite geri gelir
    first_raw = next(c for c in detail["containers"] if c["code"] == "M-A01")
    _ok(o.post(f"{API}/jobs/{job_id}/containers/{first_raw['id']}/void", headers=HDR, json={}))
    _ok(o.post(f"{API}/jobs/{second['job']['id']}/containers", headers=HDR, json={
        "material_code_id": code["code_id"], "quantity": 10, "unit": "g", "inventory_id": w["raw_lot"].id}))


def test_absorbed_source_lot_redirects_container_and_coded_card_is_archived(world):
    from core.stock_lots import absorb_row
    w = world
    db, o = w["db"], w["owner"]
    job_id, _ = _prepare_job(w)
    twin = Inventory(item_id=w["raw"].id, lot_number="GIZLI-KAYNAK-LOT-2", quantity=1,
                     status="APPROVED", domain="cosmetics")
    db.add(twin); db.commit()
    row = db.get(Inventory, w["raw_lot"].id)
    absorb_row(db, row, twin, item_id=w["raw"].id)
    db.commit()                                         # FK ihlali olmadan silinir
    db.expire_all()
    bound = db.query(OutsourcingContainer).filter_by(job_id=job_id, item_id=w["raw"].id).all()
    assert bound and all(c.inventory_id == twin.id for c in bound)
    assert all(json.loads(c.source_snapshot)["lot_number"] == "GIZLI-KAYNAK-LOT" for c in bound)

    # Yalnız malzeme kodu olan (hiç hareketsiz) kart: hard-delete değil arşiv
    orphan = Item(name="Kodlu Boş Kart", unit="g", category="Hammadde", current_stock=0, domain="cosmetics")
    db.add(orphan); db.commit()
    partner = _ok(o.get(f"{API}/bootstrap"))["partners"][0]["id"]
    _ok(o.post(f"{API}/material-codes", headers=HDR, json={
        "partner_id": partner, "item_id": orphan.id, "code": "M-Z99",
        "specification": "Boş kart tanımı v1", "safety_instructions": "Eldiven."}))
    r = o.delete(f"/api/items/{orphan.id}", headers=HDR)
    assert r.status_code == 200 and r.json()["soft_deleted"] is True, r.text
