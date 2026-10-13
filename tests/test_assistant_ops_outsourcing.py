"""Minerva Asistan — `outsourcing_jobs` aracı (core/assistant/handlers_ops.py, 13.10.2026).

`tests/test_outsourcing.py`'nin `world` kurulumunu kullanır (ayrı dosya: iki
modülün `world` fikstürü aynı adı taşır).  Kilitler:
  • Liste + FS-<id> ayrıntısı aktif panelle süzülür.
  • Gerçek kart adı / kaynak lot motorun kuralıyla YALNIZ `outsourcing.mapping`
    ile gelir; yalnız `view` yetkisinde araç sonucunda yalnız kodlar görünür.
  • Dış paket (üreticiye giden kodlu belge) ve onaylayan kullanıcı kimliği çıkmaz.
"""
import json

from core import outsourcing as osrc
from core.assistant.tools import ToolContext, run_tool
from database import User
from tests.test_outsourcing import DOMAIN, approve, create_job, prepare, world  # noqa: F401  (fikstür)

PRIVATE = ("Private Raw Test", "PRIVATE-RAW-SKU", "PRIVATE-SOURCE-LOT", "Secret Supplier Test")


def _ctx(db, username, domain=DOMAIN, permissions=None):
    user = db.query(User).filter_by(username=username).one_or_none()
    if user is None:
        user = User(username=username, full_name=username.title(), role="LabTech", is_active=True,
                    password_hash="x", permissions=json.dumps(permissions) if permissions else None)
        db.add(user)
        db.commit()
    return ToolContext.from_user(user, domain, db)


def _run(ctx, args):
    res = run_tool(ctx, "outsourcing_jobs", {"reference": None, "status": None, **args})
    return res, json.loads(res.content)


def test_outsourcing_jobs_list_detail_and_mapping_gate(world):
    db = world["db"]
    job = approve(world, prepare(world))
    draft = create_job(world)
    ref = f"FS-{job.id}"

    viewer = _ctx(db, "ops_fason_viewer", permissions={"outsourcing": {"view": True}})
    res, p = _run(viewer, {})
    assert not res.is_error and p["count"] == 2 and res.summary == "2 iş"
    rows = {r["reference"]: r for r in p["rows"]}
    assert rows[ref]["status"] == "APPROVED" and rows[ref]["status_label"] == "Onaylandı"
    assert rows[ref]["product"] == "P-X01" and rows[ref]["partner"] == "Synthetic Partner"
    assert rows[f"FS-{draft.id}"]["status"] == "DRAFT"
    res, p = _run(viewer, {"status": "DRAFT"})
    assert [r["reference"] for r in p["rows"]] == [f"FS-{draft.id}"]

    res, p = _run(viewer, {"reference": ref.lower()})
    assert not res.is_error and p["job"]["reference"] == ref and res.summary == f"{ref} · Onaylandı"
    codes = {m["code"] for m in p["materials"]}
    assert {"M-A01", "M-B01", "M-T01"} <= codes
    assert all("item_name" not in m for m in p["materials"])
    assert {a["role"] for a in p["approvals"]} == set(osrc.ROLES)
    assert all(set(a) == {"role", "approved_at"} for a in p["approvals"])   # onaylayan kimliği yok
    for s in PRIVATE:
        assert s not in res.content, s
    assert "external_packet" not in res.content and "packet_hash" not in res.content

    mapper = _ctx(db, "songul")                                   # LabLead: outsourcing.mapping VAR
    res, p = _run(mapper, {"reference": ref})
    raw = next(m for m in p["materials"] if m["code"] == "M-A01")
    assert raw["item_name"] == "Private Raw Test" and raw["kind"] == "raw"


def test_outsourcing_jobs_are_panel_scoped_and_reference_validated(world):
    db = world["db"]
    job = create_job(world)
    other = _ctx(db, "songul", domain="supplement")
    res, p = _run(other, {})
    assert not res.is_error and p["count"] == 0
    res, p = _run(other, {"reference": f"FS-{job.id}"})
    assert res.is_error and "bulunamadı" in p["error"]
    res, p = _run(_ctx(db, "songul"), {"reference": "XYZ"})
    assert res.is_error and "FS-" in p["error"]
    res, p = _run(_ctx(db, "meltem"), {})                         # LabTech varsayılanı: outsourcing yok
    assert res.is_error and "outsourcing.view" in res.content
