"""
scripts/repair_witness_lots_20261016.py — şahit lot onarımı.

Canlı durum (16.10.2026, salt-okunur teşhis) birebir kurulur: Serenida
Niacinamide #32 — kart 16, SR003 raf 8, SR003-S şahit 2, SR002-S şahit kaydı 2
ama lotu 0 (08.10 "66 → 15 yanlış sayım" düzeltmesi FIFO ile SR002-S×2 düştü).
Kapsam: kuru çalıştırma hiçbir şey yazmaz; commit yalnız lot satırını kayıttaki
miktara döndürür (kart / defter değişmez) + audit; ikinci çalıştırma no-op;
pasif / tükenmiş kayıt ve kayıttan fazla lot dokunulmaz; çapraz kontrol notu
ayrıştırır; CLI.
"""
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from database import AdminAuditLog, Inventory, Item, RetentionSample, Transaction

_SPEC = importlib.util.spec_from_file_location(
    "_repair_witness_lots",
    Path(__file__).resolve().parent.parent / "scripts" / "repair_witness_lots_20261016.py")
rep = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rep)

DRAW_NOTE = ("Stok düzeltme — Eski: 66.0 adet → Yeni: 15.0 adet (Δ -51.0) | "
             "Lot düşümü: SR002×9 · SR002-S×2 · SR003×40 | Sebep: yanlış sayım")


def _inv(db, item, lot, qty, *, days=0, location="Showroom"):
    inv = Inventory(item_id=item.id, lot_number=lot, quantity=qty, status="APPROVED",
                    location=location, domain="cosmetics",
                    created_at=datetime.utcnow() - timedelta(days=days))
    db.add(inv)
    db.flush()
    return inv


def _rs(db, item, inv, qty, *, status="stored", active=True):
    r = RetentionSample(inventory_id=inv.id if inv else None, item_id=item.id, item_name=item.name,
                        lot_number=inv.lot_number if inv else "X-S", brand="Serenida",
                        quantity=qty, initial_quantity=qty, status=status, is_active=active,
                        source="production", domain="cosmetics")
    db.add(r)
    db.flush()
    return r


def _setup(db: Session):
    it = Item(name="Serenida Niacinamide", category="Bitmiş Ürün", unit="adet",
              current_stock=16, domain="cosmetics")
    db.add(it)
    db.flush()
    _inv(db, it, "SR002", 0, days=40)
    sr002s = _inv(db, it, "SR002-S", 0, days=40, location="Şahit Numune Dolabı — Serenida")
    _rs(db, it, sr002s, 2)
    _inv(db, it, "SR003", 8, days=8)
    sr003s = _inv(db, it, "SR003-S", 2, days=8, location="Şahit Numune Dolabı — Serenida")
    _rs(db, it, sr003s, 2)
    db.add(Transaction(item_id=it.id, transaction_type="Adjustment", quantity=-51,
                       notes=DRAW_NOTE, performed_by="Songül",
                       timestamp=datetime(2026, 10, 8, 13, 24)))
    # Dokunulmayacaklar: tükenmiş kayıt, pasif kayıt, kayıttan FAZLA lot
    other = Item(name="Serenida Aloe", category="Bitmiş Ürün", unit="adet",
                 current_stock=30, domain="cosmetics")
    db.add(other)
    db.flush()
    _rs(db, other, _inv(db, other, "SR010-S", 0), 0, status="depleted")
    _rs(db, other, _inv(db, other, "SR011-S", 0), 2, active=False)
    _rs(db, other, _inv(db, other, "SR012-S", 5), 3)
    db.commit()
    return it, sr002s


def _state(db):
    db.expire_all()
    return {
        "inv": {i.id: i.quantity for i in db.query(Inventory).all()},
        "stock": {i.id: i.current_stock for i in db.query(Item).all()},
        "tx": db.query(Transaction).count(),
        "audit": db.query(AdminAuditLog).filter(AdminAuditLog.action == rep.AUDIT_ACTION).count(),
    }


def test_plan_is_dry_and_finds_only_the_drawn_witness_lot(db_session):
    it, sr002s = _setup(db_session)
    before = _state(db_session)
    p = rep.plan(db_session)
    db_session.rollback()
    assert p["status"] == "ready" and p["lot_count"] == 1 and p["total_add"] == 2
    (row,) = p["items"]
    assert row["item_id"] == it.id and row["lots"][0]["lot"] == "SR002-S"
    assert (row["lots"][0]["before"], row["lots"][0]["after"]) == (0, 2)
    # Kart 16 aynı; şahit 2 → 4, lotsuz 6 → 4
    assert (row["card"], row["shelf"], row["witness"], row["lotless"]) == (16, 8, 2, 6)
    assert (row["witness_after"], row["lotless_after"], row["excess_after"]) == (4, 4, 0)
    # Çapraz kontrol: notu ayrıştırır, yalnız -S satırı
    assert [(d["lot"], d["qty"], d["planned"]) for d in p["draws"]] == [("SR002-S", 2, 2)]
    assert _state(db_session) == before


def test_apply_restores_lot_only_with_audit_and_is_idempotent(db_session):
    it, sr002s = _setup(db_session)
    before = _state(db_session)
    p = rep.apply(db_session)
    assert p["status"] == "applied" and p["written"] == 1
    after = _state(db_session)
    assert after["inv"][sr002s.id] == 2
    assert {k: v for k, v in after["inv"].items() if k != sr002s.id} == \
        {k: v for k, v in before["inv"].items() if k != sr002s.id}
    assert after["stock"] == before["stock"]                 # kart stoğu DEĞİŞMEZ
    assert after["tx"] == before["tx"]                       # Transaction YAZILMAZ
    assert after["audit"] == 1
    log = db_session.query(AdminAuditLog).filter(AdminAuditLog.action == rep.AUDIT_ACTION).one()
    det = json.loads(log.details)
    assert log.target_id == sr002s.id and det["lot_before"] == 0 and det["lot_after"] == 2
    assert det["card_stock_changed"] is False

    p2 = rep.apply(db_session)
    assert p2["status"] == "nothing" and _state(db_session) == after


def test_cli_dry_and_commit(db_session, capsys):
    _, sr002s = _setup(db_session)
    assert rep.main([]) == 0
    out = capsys.readouterr().out
    assert "KURU ÇALIŞTIRMA" in out and "SR002-S: lot 0 → 2" in out and "tx " in out
    db_session.expire_all()
    assert db_session.get(Inventory, sr002s.id).quantity == 0
    assert rep.main(["--commit"]) == 0
    out = capsys.readouterr().out
    assert "YAZILDI" in out
    db_session.expire_all()
    assert db_session.get(Inventory, sr002s.id).quantity == 2
    assert rep.main([]) == 0
    assert "onarılacak şahit lotu yok" in capsys.readouterr().out
