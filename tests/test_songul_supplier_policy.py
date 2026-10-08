"""Onaylı firma politikası: kuru çalıştırma, firma takma adları ve atomik guard/audit."""
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import event

from database import (AdminAuditLog, Inventory, Item, MaterialGroup,
                      MaterialSupplierPref, Supplier, SupplierPrice, Transaction)

_SPEC = importlib.util.spec_from_file_location(
    "_songul_supplier_policy", Path(__file__).resolve().parent.parent / "scripts" /
    "apply_songul_supplier_policy_20261008.py")
policy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(policy)


def seed(db, *, dogasa=True):
    names = [n for n, _ in policy.POLICY if dogasa or n != "DOGASA"]
    names += ["UMAYCHEM, BAŞAK ORGANİK", "TATLİDİLİMLER", "TATLI DİLİMLER",
              "KRK GIDA (HAYAT)", "DOALİNN", "DİĞER KİMYA"]
    rows = {n: Supplier(name=n, domain="cosmetics") for n in names}
    rows["KRK GIDA (eski)"] = Supplier(name="KRK GIDA (eski)", is_active=False,
                                      purchase_status="preferred", domain="cosmetics")
    rows["supplement"] = Supplier(name="NATURALYA", domain="supplement")
    db.add_all(rows.values())
    db.commit()
    return rows


def states(db):
    db.expire_all()
    return {s.id: (s.purchase_status, s.status_reason, s.status_by, s.status_at,
                   s.is_active, s.merged_into_id) for s in db.query(Supplier).all()}


def test_dry_run_reports_all_aliases_and_never_writes(db_session, monkeypatch, capsys):
    rows = seed(db_session)
    before = states(db_session)
    monkeypatch.setattr(policy, "SessionLocal", lambda: db_session)
    assert policy.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["committed"] is False and report["changed_count"] == 10
    tatli = next(f for f in report["firms"] if f["firm"] == "TATLIDİLİMLER")
    assert {s["id"] for s in tatli["aliases"]} == {
        rows[n].id for n in ("TATLIDİLİMLER", "TATLİDİLİMLER", "TATLI DİLİMLER")}
    assert states(db_session) == before
    assert db_session.query(AdminAuditLog).count() == 0


def test_commit_updates_active_aliases_with_atomic_audit_and_preserves_other_data(db_session):
    rows = seed(db_session)
    item = Item(name="AKTİF KÖMÜR", unit="g", category="Hammadde", current_stock=123.5,
                min_stock_level=9, supplier_id=rows["TATLIDİLİMLER"].id)
    db_session.add(item)
    db_session.flush()
    lot = Inventory(item_id=item.id, lot_number="POLICY-LOT", quantity=123.5)
    txn = Transaction(item_id=item.id, transaction_type="Input", quantity=123.5)
    price = SupplierPrice(item_id=item.id, supplier_id=rows["TATLIDİLİMLER"].id,
                          supplier_name="TATLIDİLİMLER", unit_price=12,
                          currency="TRY", price_unit="kg")
    pref = MaterialSupplierPref(item_id=item.id, supplier_id=rows["NATURALYA"].id,
                                preference="preferred", rank=1)
    db_session.add_all([lot, txn, price, pref])
    db_session.commit()
    before = states(db_session)
    report = policy.apply(db_session)
    assert report["committed"] is True and report["changed_count"] == 10
    after = states(db_session)
    for firm in report["firms"]:
        for s in firm["aliases"]:
            if s["is_active"]:
                assert after[s["id"]][:3] == (firm["status"], policy.REASON, policy.ACTOR)
                assert after[s["id"]][3] is not None
    for name in ("DOALİNN", "DİĞER KİMYA", "KRK GIDA (eski)", "supplement"):
        assert after[rows[name].id] == before[rows[name].id]
    audits = db_session.query(AdminAuditLog).all()
    assert {a.target_id for a in audits} == {c["supplier_id"] for c in report["changes"]}
    assert len(audits) == report["changed_count"]
    for audit in audits:
        details = json.loads(audit.details)
        assert details["policy_id"] == policy.POLICY_ID
        assert details["before"]["purchase_status"] == "normal"
        assert details["after"]["status_at"] is not None
    assert (item.current_stock, item.min_stock_level, item.supplier_id) == (123.5, 9, rows["TATLIDİLİMLER"].id)
    assert lot.quantity == txn.quantity == 123.5
    assert price.unit_price == 12 and pref.preference == "preferred" and pref.rank == 1
    assert db_session.query(Transaction).count() == 1
    assert db_session.query(MaterialSupplierPref).count() == 1


def test_second_commit_is_idempotent_without_new_audit_or_timestamp(db_session):
    seed(db_session)
    policy.apply(db_session)
    before = states(db_session)
    audit_count = db_session.query(AdminAuditLog).count()
    report = policy.apply(db_session)
    assert report["changed_count"] == 0 and report["changes"] == []
    assert states(db_session) == before
    assert db_session.query(AdminAuditLog).count() == audit_count


def test_missing_dogasa_is_not_replaced_by_doalinn_and_writes_nothing(db_session):
    seed(db_session, dogasa=False)
    before = states(db_session)
    report = policy.plan(db_session)
    assert any(e["code"] == "missing_firm" and "DOALİNN" in e["message"] for e in report["errors"])
    with pytest.raises(policy.PolicyError, match="DOGASA"):
        policy.apply(db_session)
    assert states(db_session) == before
    assert db_session.query(AdminAuditLog).count() == 0


@pytest.mark.parametrize("ambiguous_name", ["DOGASA DOALİNN", "DOGASA DOLAIN", "DOGASA DOALIN",
                                           "DOGASA (DOLAIN)", "DOGASA (DOALIN)"])
def test_ambiguous_firm_alias_aborts_all_changes(db_session, ambiguous_name):
    seed(db_session)
    db_session.add(Supplier(name=ambiguous_name, domain="cosmetics"))
    db_session.commit()
    before = states(db_session)
    with pytest.raises(policy.PolicyError, match="birden çok") as exc:
        policy.apply(db_session)
    assert any(e["code"] == "ambiguous_alias" for e in exc.value.report["errors"])
    assert states(db_session) == before
    assert db_session.query(AdminAuditLog).count() == 0


@pytest.mark.parametrize("group_scope", [False, True])
def test_contrary_active_charcoal_material_preference_blocks_even_inactive_alias(db_session, group_scope):
    rows = seed(db_session)
    scope = MaterialGroup(name="AKTİF KÖMÜR") if group_scope else Item(name="AKTİF KÖMÜR", unit="g")
    db_session.add(scope)
    db_session.flush()
    pref = MaterialSupplierPref(supplier_id=rows["KRK GIDA (eski)"].id, preference="preferred",
                                **({"material_group_id": scope.id} if group_scope else {"item_id": scope.id}))
    db_session.add(pref)
    db_session.commit()
    before = states(db_session)
    with pytest.raises(policy.PolicyError, match="AKTİF KÖMÜR") as exc:
        policy.apply(db_session)
    error = next(e for e in exc.value.report["errors"] if e["code"] == "contrary_material_preference")
    assert error["preference_id"] == pref.id and error["supplier_id"] == rows["KRK GIDA (eski)"].id
    assert states(db_session) == before
    assert db_session.query(AdminAuditLog).count() == 0
    assert db_session.query(MaterialSupplierPref).one().preference == "preferred"


def test_commit_rechecks_preferences_after_successful_dry_run(db_session):
    rows = seed(db_session)
    assert policy.plan(db_session)["ok"] is True
    item = Item(name="AKTİF KÖMÜR", unit="g")
    db_session.add(item)
    db_session.flush()
    db_session.add(MaterialSupplierPref(item_id=item.id, supplier_id=rows["TATLİDİLİMLER"].id,
                                        preference="preferred", note="A2-13-S1"))
    db_session.commit()
    before = states(db_session)
    with pytest.raises(policy.PolicyError, match="AKTİF KÖMÜR"):
        policy.apply(db_session)
    assert states(db_session) == before and db_session.query(AdminAuditLog).count() == 0


def test_cross_firm_merge_is_unexpected_and_blocks_all_writes(db_session):
    rows = seed(db_session)
    rows["KRK GIDA (eski)"].merged_into_id = rows["NATURALYA"].id
    db_session.commit()
    before = states(db_session)
    with pytest.raises(policy.PolicyError, match="beklenmeyen birleştirme"):
        policy.apply(db_session)
    assert states(db_session) == before and db_session.query(AdminAuditLog).count() == 0


def test_audit_storage_failure_rolls_back_every_supplier(db_session):
    seed(db_session)
    before = states(db_session)

    def fail_audit(mapper, connection, target):
        raise RuntimeError("audit storage unavailable")

    event.listen(AdminAuditLog, "before_insert", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="audit storage unavailable"):
            policy.apply(db_session)
    finally:
        event.remove(AdminAuditLog, "before_insert", fail_audit)
    assert states(db_session) == before
    assert db_session.query(AdminAuditLog).count() == 0
