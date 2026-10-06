"""
scripts/fix_numune_double_count_20261006.py — 758 çift sayım düzeltmesi.

Prod kurulumu (06.10.2026, salt-okunur teşhis) birebir kurulur: 758 JOJOBA
UÇUCU YAĞI (NUMUNE) — 10.09 Betül elle +20, 05.10 "Stoğa çevir" +20 → stok
40, lot 20.  746/753 numuneleri aynı elle +20'yi taşıyor ama henüz
çevrilmedi (rapor edilmeli).  Kapsam: plan/apply doğrulaması, tek
Adjustment + audit, ikinci çalıştırmanın no-op olması, uyumsuzlukta durma,
CLI (kuru / --commit).
"""
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from core.snapshots import compute_stock_at
from database import AdminAuditLog, Inventory, Item, Supplier, Transaction

_SPEC = importlib.util.spec_from_file_location(
    "_fix_double_count",
    Path(__file__).resolve().parent.parent / "scripts" / "fix_numune_double_count_20261006.py")
fix = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(fix)

RECEIVED = datetime(2026, 8, 24, 9, 0)        # numunelerin geldiği an (UTC)
MANUAL_AT = datetime(2026, 9, 10, 8, 0)       # Betül'ün elle +20'si (UTC)
CONVERT_AT = datetime(2026, 10, 5, 5, 40)     # 05.10 08:40 TR "Stoğa çevir"


def _manual(db, item_id, at=MANUAL_AT, qty=20.0):
    db.add(Transaction(item_id=item_id, transaction_type="Adjustment", quantity=qty,
                       timestamp=at, performed_by="Betül",
                       notes=f"Stok düzeltme — Eski: 0.0 ml → Yeni: {qty} ml (Δ +{qty}) | Sebep: DÜZELT"))


def _sample_card(db, item_id, name, sup_id, *, adjust_at=MANUAL_AT, received=RECEIVED):
    """746/753 gibi: hâlâ numune, kartında elle +20."""
    db.add(Item(id=item_id, name=name, category="Hammadde", unit="ml",
                current_stock=20.0 if adjust_at else 0.0))
    db.flush()
    db.add(Inventory(item_id=item_id, lot_number="NUMUNE", quantity=20.0, status="APPROVED",
                     is_sample=True, location="Numune", supplier_id=sup_id, created_at=received))
    if adjust_at:
        _manual(db, item_id, at=adjust_at)


def _setup(db: Session, *, name="JOJOBA UÇUCU YAĞI (NUMUNE)", stock=40.0, unit="ml",
           manual=True, convert_at=CONVERT_AT, extra_lot=0.0, others=True):
    sup = Supplier(name="DOĞASA")
    db.add(sup); db.flush()
    db.add(Item(id=758, name=name, category="Hammadde", unit=unit, current_stock=stock))
    db.flush()
    db.add(Inventory(item_id=758, lot_number="NUMUNE", quantity=20.0, status="APPROVED",
                     is_sample=False, supplier_id=sup.id, created_at=RECEIVED))
    if extra_lot:
        db.add(Inventory(item_id=758, lot_number="L-EK", quantity=extra_lot, status="APPROVED",
                         is_sample=False, supplier_id=sup.id))
    if manual:
        _manual(db, 758)
    if convert_at:
        db.add(Transaction(item_id=758, transaction_type="Input", quantity=20.0,
                           lot_number="NUMUNE", timestamp=convert_at, performed_by="sued",
                           notes="Numune stoğa çevrildi — Lot: NUMUNE"))
    if others:
        _sample_card(db, 746, "PORTAKAL UÇUCU YAĞI (NUMUNE)", sup.id)
        _sample_card(db, 753, "LAVANTA UÇUCU YAĞI", sup.id)
        # Rapora GİRMEMELİ: elle düzeltmesi yok / düzeltme numuneden önce
        _sample_card(db, 760, "GÜL SUYU (NUMUNE)", sup.id, adjust_at=None)
        _sample_card(db, 761, "NANE UÇUCU YAĞI (NUMUNE)", sup.id,
                     received=datetime(2026, 9, 20, 9, 0))
    db.commit()


def _txs(db, item_id=758):
    db.expire_all()
    return db.query(Transaction).filter(Transaction.item_id == item_id).order_by(Transaction.id).all()


def _audits(db):
    return db.query(AdminAuditLog).filter(AdminAuditLog.action == fix.AUDIT_ACTION).all()


# ─── plan ───────────────────────────────────────────────────────────────────

def test_plan_verifies_758_and_reports_746_753_without_writing(db_session: Session):
    _setup(db_session)
    p = fix.plan(db_session)
    assert p["status"] == "ready", p["problems"]
    assert p["delta"] == -20 and p["new_stock"] == 20
    assert p["manual"]["by"] == "Betül" and p["convert"]["lot"] == "NUMUNE"
    # Guard'a ve satın alma işaretine karışmayan not
    assert p["note"].startswith("Çift sayım düzeltmesi —")
    assert "Numune stoğa çevrildi" not in p["note"]
    # Yalnız elle sayılmış ve hâlâ numune olan lotlar raporlanır
    assert [o["item_id"] for o in p["others"]] == [746, 753]
    assert all(o["lot"] == "NUMUNE" and o["qty"] == 20 for o in p["others"])
    # Hiçbir şey yazılmadı
    db_session.rollback()
    assert len(_txs(db_session)) == 2
    assert db_session.get(Item, 758).current_stock == 40


# ─── apply ──────────────────────────────────────────────────────────────────

def test_apply_writes_one_adjustment_keeps_lot_and_history(db_session: Session):
    _setup(db_session)
    eoms = [datetime(2026, 8, 31, 21, 0), datetime(2026, 9, 30, 21, 0)]
    before = [compute_stock_at(db_session, e)[0][758] for e in eoms]

    p = fix.apply(db_session)
    assert p["status"] == "applied"

    txs = _txs(db_session)
    assert len(txs) == 3                                  # eski iki kayıt yerinde
    new = txs[-1]
    assert new.transaction_type == "Adjustment" and new.quantity == -20
    assert new.lot_number is None and new.performed_by == fix.ACTOR
    assert new.notes.startswith("Çift sayım düzeltmesi —")
    assert not new.notes.startswith("Stok düzeltme")
    assert "Numune stoğa çevrildi" not in new.notes
    assert db_session.get(Item, 758).current_stock == 20
    lot = db_session.query(Inventory).filter(Inventory.item_id == 758).one()
    assert lot.quantity == 20 and lot.is_sample is False  # lota dokunulmadı
    # Geçmiş ay sonları değişmez (yalnız bugünkü stok 40 → 20)
    assert [compute_stock_at(db_session, e)[0][758] for e in eoms] == before == [0, 20]

    (audit,) = _audits(db_session)
    assert audit.target_type == "item" and audit.target_id == 758 and audit.actor_id is None
    d = json.loads(audit.details)
    assert d["actor"] == fix.ACTOR and d["transaction_id"] == new.id
    assert d["old_stock"] == 40 and d["new_stock"] == 20
    assert d["manual_adjustment_id"] == txs[0].id and d["convert_input_id"] == txs[1].id
    # Raporlanan numunelere yazılmaz
    assert all(len(_txs(db_session, i)) == 1 for i in (746, 753))


def test_second_run_is_noop(db_session: Session):
    _setup(db_session)
    assert fix.apply(db_session)["status"] == "applied"
    again = fix.apply(db_session)
    assert again["status"] == "already_applied"
    assert fix.plan(db_session)["status"] == "already_applied"
    assert len(_txs(db_session)) == 3 and len(_audits(db_session)) == 1
    assert db_session.get(Item, 758).current_stock == 20


@pytest.mark.parametrize("kwargs, needle", [
    ({"stock": 35.0}, "stok 35"),
    ({"unit": "g"}, "birim g"),
    ({"name": "JOJOBA YAĞI"}, "ad «JOJOBA YAĞI»"),
    ({"extra_lot": 5.0}, "normal lot toplamı 25"),
    ({"manual": False}, "'Stok düzeltme' Adjustment'ı 0 adet"),
    ({"convert_at": datetime(2026, 10, 6, 7, 0)}, "çevirme Input'u 0 adet"),
])
def test_mismatch_stops_without_writing(db_session: Session, kwargs, needle):
    _setup(db_session, others=False, **kwargs)
    n_before = len(_txs(db_session))
    p = fix.apply(db_session)
    assert p["status"] == "mismatch"
    assert any(needle in pr for pr in p["problems"]), p["problems"]
    assert len(_txs(db_session)) == n_before and not _audits(db_session)
    assert db_session.get(Item, 758).current_stock == kwargs.get("stock", 40.0)


def test_missing_card_stops(db_session: Session):
    p = fix.plan(db_session)
    assert p["status"] == "mismatch" and "bulunamadı" in p["problems"][0]


# ─── CLI ────────────────────────────────────────────────────────────────────

def test_cli_dry_run_then_commit(db_session: Session, capsys):
    _setup(db_session)
    assert fix.main([]) == 0
    out = capsys.readouterr().out
    assert "KURU ÇALIŞTIRMA" in out and "stok 40 → 20" in out
    assert "id=746 PORTAKAL" in out and "id=753 LAVANTA" in out and "id=760" not in out
    assert "yalnız lotu bağla" in out
    assert len(_txs(db_session)) == 2

    assert fix.main(["--commit"]) == 0
    assert "YAZILDI" in capsys.readouterr().out
    assert fix.main(["--commit"]) == 0
    assert "zaten uygulanmış" in capsys.readouterr().out
    assert len(_txs(db_session)) == 3 and len(_audits(db_session)) == 1


def test_cli_mismatch_exit_code(db_session: Session, capsys):
    _setup(db_session, stock=35.0, others=False)
    assert fix.main(["--commit"]) == 2
    assert "DURDURULDU" in capsys.readouterr().out
    assert len(_txs(db_session)) == 2
