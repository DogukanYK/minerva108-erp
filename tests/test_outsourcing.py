"""Fason contracts: exact coded identities, approved snapshots and one local debit.

Use only the isolated database selected by tests/conftest.py. Fixtures contain
synthetic company/material names; no live data or external side effects.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from core import outsourcing as osrc
from core.production_plan import CFG_ENABLED
from core.snapshots import compute_stock_at
from database import (AppSetting, Inventory, Item, MaterialGroup, Recipe, RecipeIngredient,
                      SessionLocal, Supplier, Transaction, User, OutsourcingApproval,
                      OutsourcingContainer, OutsourcingJob, OutsourcingMaterialCode,
                      OutsourcingMovement, OutsourcingOperation, OutsourcingPacketRevision,
                      OutsourcingReceipt, OutsourcingShipment, OutsourcingShipmentLine)

DOMAIN = "cosmetics"
HDR = {"Origin": "http://testserver"}


def actor(db, username):
    user = db.query(User).filter_by(username=username).one()
    return {"sub": str(user.id), "username": user.username, "full_name": user.full_name,
            "role": user.role}


@pytest.fixture
def world(db_session):
    db = db_session
    actors = {key: actor(db, name) for key, name in
              [("technical", "songul"), ("owner", "dogukan"), ("manager", "isik"), ("other", "meltem")]}
    supplier = Supplier(name="Secret Supplier Test", domain=DOMAIN)
    group = MaterialGroup(name="Synthetic group", domain=DOMAIN)
    db.add_all([supplier, group]); db.flush()
    raw = Item(name="Private Raw Test", sku="PRIVATE-RAW-SKU", unit="g", category="Hammadde",
               current_stock=100, supplier_id=supplier.id, material_group_id=group.id, domain=DOMAIN)
    alternative = Item(name="Unverified Alternative", sku="PRIVATE-ALT-SKU", unit="g", category="Hammadde",
                       current_stock=100, material_group_id=group.id, domain=DOMAIN)
    bottle = Item(name="Private Bottle Test", sku="PRIVATE-BOTTLE", unit="adet", category="Ambalaj",
                  pkg_type="şişe", current_stock=100, domain=DOMAIN)
    tr = Item(name="Private Label TR", sku="PRIVATE-TR", unit="adet", category="Ambalaj", pkg_type="etiket",
              language="TR", label_group="SYNTHETIC-FASON", current_stock=100, domain=DOMAIN)
    en = Item(name="Private Label EN", sku="PRIVATE-EN", unit="adet", category="Ambalaj", pkg_type="etiket",
              language="EN", label_group="SYNTHETIC-FASON", current_stock=100, domain=DOMAIN)
    finished = Item(name="Private Finished Test", sku="PRIVATE-FINISHED", unit="adet", category="Bitmiş Ürün",
                    current_stock=0, domain=DOMAIN)
    db.add_all([raw, alternative, bottle, tr, en, finished]); db.flush()
    lot = Inventory(item_id=raw.id, supplier_id=supplier.id, lot_number="PRIVATE-SOURCE-LOT",
                    quantity=100, expiry_date="2099-12-31", status="APPROVED", qc_required=False,
                    is_sample=False, domain=DOMAIN)
    db.add(lot)
    recipe = Recipe(name="Private Recipe Test", target_item_id=finished.id, output_quantity=1,
                    output_unit="adet", waste_percentage=10, domain=DOMAIN)
    db.add(recipe); db.flush()
    for item, qty, phase in [(raw, 1, "A"), (raw, 1, "B"), (bottle, 1, None), (tr, 1, None)]:
        db.add(RecipeIngredient(recipe_id=recipe.id, item_id=item.id, quantity=qty, unit=item.unit, phase=phase))
    db.add(AppSetting(key=CFG_ENABLED, value="0"))
    db.flush()
    partner = osrc.create_partner(db, DOMAIN, actors["owner"], {"name": "Synthetic Partner"})
    cards = {"raw": raw, "alternative": alternative, "bottle": bottle, "tr": tr, "en": en, "finished": finished}
    codes = {}
    for key in ["raw", "bottle", "tr", "en"]:
        codes[key] = osrc.create_material_code(db, DOMAIN, actors["owner"], {
            "partner_id": partner.id, "item_id": cards[key].id,
            "specification": f"Synthetic precise {key} specification, form and grade v1",
            "safety_instructions": "Kodlu kullanım için koruyucu ekipman kullanın.",
            "code": {"raw": "M-A01", "bottle": "M-B01", "tr": "M-T01", "en": "M-E01"}[key],
        })
    db.commit()
    return {"db": db, "actors": actors, "cards": cards, "codes": codes, "partner": partner,
            "recipe": recipe, "lot": lot, "supplier": supplier}


def job_data(world, **extra):
    return {"partner_id": world["partner"].id, "recipe_id": world["recipe"].id, "quantity": 10,
            "label_language": "TR", "external_product_name": "P-X01",
            "external_notes": "M-A01 fazlarını sırayla uygulayın. M-B01 ile dolum yapın.",
            "approver_ids": {role: int(world["actors"][role]["sub"]) for role in osrc.ROLES},
            **extra}


def create_job(world, **extra):
    db = world["db"]
    job = osrc.create_job(db, DOMAIN, world["actors"]["owner"], job_data(world, **extra))
    db.commit()
    return job


def guard(db, job):
    db.refresh(job)
    row = db.query(OutsourcingPacketRevision).filter_by(job_id=job.id, revision=job.revision).one()
    return {"revision": job.revision, "packet_hash": row.packet_hash}


def prepare(world, job=None, *, split_raw=False):
    db = world["db"]
    job = job or create_job(world)
    snapshot = json.loads(db.query(OutsourcingPacketRevision).filter_by(job_id=job.id, revision=job.revision).one().snapshot)
    for material in snapshot["materials"]:
        planned = material.get("dispatch_quantity", material["quantity"])
        quantities = [planned / 2] * 2 if split_raw and material["kind"] == "raw" else [planned]
        for qty in quantities:
            data = {"material_code_id": material["code_id"], "quantity": qty, "unit": material["unit"]}
            if material["kind"] == "raw":
                data["inventory_id"] = world["lot"].id
            osrc.create_container(db, DOMAIN, world["actors"]["owner"], job.id, data)
            db.commit()
    return job


def approve(world, job):
    db = world["db"]
    revision = guard(db, job)
    for role in osrc.ROLES:
        osrc.approve_job(db, DOMAIN, world["actors"][role], job.id, **revision)
        db.commit()
    return job


def containers(db, job):
    return db.query(OutsourcingContainer).filter_by(job_id=job.id, is_active=True).order_by(OutsourcingContainer.id).all()


def expect_error(db, code, func, *args, **kwargs):
    with pytest.raises(osrc.OutsourcingError) as error:
        func(*args, **kwargs)
    assert error.value.code == code, error.value.detail
    db.rollback()
    return error.value


def test_code_identity_survives_item_rename_and_is_separate_by_partner_and_spec(world):
    db, card, code = world["db"], world["cards"]["raw"], world["codes"]["raw"]
    card.name = "Renamed Private Raw"
    db.commit()
    data = {"partner_id": world["partner"].id, "item_id": card.id,
            "specification": code.specification, "safety_instructions": code.safety_instructions}
    same = osrc.create_material_code(db, DOMAIN, world["actors"]["owner"], data)
    assert same.id == code.id and same.code == "M-A01"
    second_partner = osrc.create_partner(db, DOMAIN, world["actors"]["owner"], {"name": "Synthetic Partner Two"})
    other = osrc.create_material_code(db, DOMAIN, world["actors"]["owner"], dict(data, partner_id=second_partner.id))
    another_spec = osrc.create_material_code(db, DOMAIN, world["actors"]["owner"], dict(data, specification="Liquid exact grade v2"))
    db.commit()
    assert len({code.id, other.id, another_spec.id}) == 3
    assert len({code.code, other.code, another_spec.code}) == 3


def test_same_group_does_not_grant_code_equivalence(world):
    db = world["db"]
    alternate = osrc.create_material_code(db, DOMAIN, world["actors"]["owner"], {
        "partner_id": world["partner"].id, "item_id": world["cards"]["alternative"].id,
        "specification": "Unverified alternative liquid specification",
        "safety_instructions": "Kod için koruyucu ekipman kullanın."})
    db.commit()
    data = job_data(world, material_code_ids={str(world["cards"]["raw"].id): alternate.id})
    expect_error(db, "material_code_required", osrc.create_job, db, DOMAIN, world["actors"]["owner"], data)
    assert db.query(OutsourcingJob).count() == 0
    assert db.query(AppSetting).filter_by(key=CFG_ENABLED).one().value == "0"


def test_preview_sums_two_phases_with_raw_fire_and_resolves_label_language(world):
    result = osrc.preview(world["db"], DOMAIN, job_data(world, label_language="EN"))
    rows = {row["item_id"]: row for row in result["materials"]}
    raw = rows[world["cards"]["raw"].id]
    assert raw["quantity"] == pytest.approx(22)
    assert raw["parts"] == [{"phase": "A", "quantity": 11}, {"phase": "B", "quantity": 11}]
    assert rows[world["cards"]["bottle"].id]["quantity"] == 10
    assert world["cards"]["en"].id in rows and world["cards"]["tr"].id not in rows


def test_three_distinct_role_correct_accounts_are_required(world):
    db = world["db"]
    ids = job_data(world)["approver_ids"]
    expect_error(db, "distinct_approvers_required", osrc.create_job, db, DOMAIN, world["actors"]["owner"],
                 job_data(world, approver_ids={**ids, "manager": ids["owner"]}))
    expect_error(db, "invalid_approver", osrc.create_job, db, DOMAIN, world["actors"]["owner"],
                 job_data(world, approver_ids={**ids, "technical": int(world["actors"]["other"]["sub"])}))
    assert db.query(OutsourcingJob).count() == 0


def test_approval_order_assigned_account_hash_and_revision_are_enforced(world):
    db, job = world["db"], prepare(world)
    g = guard(db, job)
    expect_error(db, "technical_approval_required", osrc.approve_job, db, DOMAIN, world["actors"]["owner"], job.id, **g)
    expect_error(db, "not_assigned_approver", osrc.approve_job, db, DOMAIN, world["actors"]["other"], job.id, **g)
    expect_error(db, "revision_stale", osrc.approve_job, db, DOMAIN, world["actors"]["technical"], job.id,
                 g["revision"], "0" * 64)
    approve(world, job)
    assert db.query(OutsourcingApproval).filter_by(job_id=job.id, revision=job.revision).count() == 3
    assert job.status == "APPROVED"
    osrc.approve_job(db, DOMAIN, world["actors"]["technical"], job.id, **g)
    db.commit()
    assert db.query(OutsourcingApproval).filter_by(job_id=job.id, revision=job.revision).count() == 3


def test_same_material_in_two_phases_has_one_summed_container_limit(world):
    db, job = world["db"], prepare(world, split_raw=True)
    raw = [c for c in containers(db, job) if c.item_id == world["cards"]["raw"].id]
    assert [c.quantity for c in raw] == [11, 11]
    approve(world, job)
    assert job.status == "APPROVED"
    expect_error(db, "over_planned_quantity", osrc.create_container, db, DOMAIN, world["actors"]["owner"], job.id,
                 {"material_code_id": world["codes"]["raw"].id, "inventory_id": world["lot"].id,
                  "quantity": 0.001, "unit": "g"})


def test_revision_change_preserves_old_approval_audit_but_requires_new_approvals(world):
    db, job = world["db"], approve(world, prepare(world))
    previous = guard(db, job)
    osrc.update_job(db, DOMAIN, world["actors"]["owner"], job.id,
                    {**previous, "external_notes": "M-A01 için kodlu prosesin yeni sürümü."})
    db.commit()
    assert job.revision == previous["revision"] + 1 and job.status == "DRAFT"
    assert db.query(OutsourcingApproval).filter_by(job_id=job.id, revision=job.revision).count() == 0
    assert db.query(OutsourcingApproval).filter_by(job_id=job.id, revision=previous["revision"]).count() == 3
    expect_error(db, "revision_stale", osrc.approve_job, db, DOMAIN, world["actors"]["technical"], job.id, **previous)


def test_no_external_documents_before_all_three_approve_and_private_names_never_leave_dto(world):
    db, job = world["db"], prepare(world)
    expect_error(db, "approval_required", osrc.external_packet, db, job)
    approve(world, job)
    packet = osrc.external_packet(db, job)
    serialized = json.dumps(packet)
    for term in [world["cards"]["raw"].name, world["cards"]["raw"].sku,
                 world["supplier"].name, world["lot"].lot_number, world["recipe"].name]:
        assert term not in serialized
    for forbidden in ["item_id", "supplier_id", "source_snapshot", "specification", "source_lot_number", "internal_notes"]:
        assert forbidden not in serialized


def test_code_safety_text_is_required_for_human_technical_approval(world):
    db = world["db"]
    world["codes"]["raw"].safety_instructions = ""
    db.commit()
    job = prepare(world)
    expect_error(db, "safety_required", osrc.approve_job, db, DOMAIN, world["actors"]["technical"], job.id, **guard(db, job))
    assert db.query(OutsourcingApproval).filter_by(job_id=job.id).count() == 0


@pytest.mark.parametrize("changes,code", [
    ({"status": "QUARANTINE"}, "invalid_source_lot"),
    ({"qc_required": True}, "invalid_source_lot"),
    ({"is_sample": True}, "invalid_source_lot"),
    ({"expiry_date": "2000-01-01"}, "expired_lot"),
    ({"expiry_date": ""}, "expiry_required"),
    ({"domain": "supplement"}, "invalid_source_lot"),
])
def test_raw_container_rejects_unusable_source_lots_without_mutation(world, changes, code):
    db, job = world["db"], create_job(world)
    for key, value in changes.items():
        setattr(world["lot"], key, value)
    db.commit()
    before = guard(db, job)
    expect_error(db, code, osrc.create_container, db, DOMAIN, world["actors"]["owner"], job.id,
                 {"material_code_id": world["codes"]["raw"].id, "inventory_id": world["lot"].id,
                  "quantity": 22, "unit": "g"})
    assert guard(db, job) == before and db.query(OutsourcingContainer).count() == 0
    assert db.get(Item, world["cards"]["raw"].id).current_stock == 100
