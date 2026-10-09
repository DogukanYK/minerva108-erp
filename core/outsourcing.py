"""Internal-only coded outsourcing. Caller owns commit/rollback; no normal production.

Stock lock order: items by id -> inventories by id -> job -> containers by id.
QC already locks item -> inventory and then receipt; it never locks the job.
Dispatch is a local stock Output, actual external use is a separate append-only
movement, and physical returns/finished goods enter stock only after QC.
"""
import hashlib
import json
import math
import re
import secrets
import unicodedata
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from sqlalchemy.orm import Session

from core.consumption import expand_recipe, load_recipe_recs
from core.stock_lots import draw_down, lot_kind
from database import (AdminAuditLog, Inventory, Item, Recipe, Transaction, User,
    OutsourcingPartner, OutsourcingMaterialCode, OutsourcingJob,
    OutsourcingPacketRevision, OutsourcingApproval, OutsourcingContainer,
    OutsourcingOperation, OutsourcingShipment, OutsourcingShipmentLine,
    OutsourcingMovement, OutsourcingReceipt)

EPS = 0.000001
ROLES = ("technical", "owner", "manager")


class OutsourcingError(Exception):
    def __init__(self, status, detail, code="invalid_request"):
        self.status, self.detail, self.code = status, detail, code
        super().__init__(detail)


def fail(detail, code="invalid_request", status=400):
    raise OutsourcingError(status, detail, code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _actor(actor):
    return int(actor.get("sub", 0)), (actor.get("full_name") or actor.get("username") or "—")[:100]


def _audit(db, actor, action, job_id=None, details=None):
    uid, name = _actor(actor)
    db.add(AdminAuditLog(actor_id=uid, actor_name=name, action="outsourcing." + action,
                         target_type="outsourcing", target_id=job_id,
                         details=_json(details or {})))


def quantity(value, *, zero=False):
    if isinstance(value, bool):
        fail("Miktar sayı olmalıdır.", "invalid_quantity")
    try:
        dec = Decimal(str(value))
        if not dec.is_finite() or abs(dec) > Decimal("1000000000000"):
            raise InvalidOperation
        dec = dec.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        fail("Miktar sonlu bir sayı olmalıdır.", "invalid_quantity")
    if dec < 0 or (not zero and dec <= 0):
        fail("Miktar sıfırdan büyük olmalıdır.", "invalid_quantity")
    return float(dec)


def unit(value):
    aliases = {"g": "g", "gr": "g", "gram": "g", "kg": "kg", "kilogram": "kg",
               "ml": "ml", "mililitre": "ml", "l": "l", "lt": "l", "litre": "l",
               "adet": "adet", "pcs": "adet", "piece": "adet"}
    found = aliases.get(str(value or "").strip().casefold())
    if not found:
        fail("Birim g/kg, ml/l veya adet olmalıdır.", "invalid_unit")
    return found


def convert_quantity(value, source_unit, target_unit, *, zero=False):
    source, target = unit(source_unit), unit(target_unit)
    scale = {"g": ("mass", Decimal(1)), "kg": ("mass", Decimal(1000)),
             "ml": ("volume", Decimal(1)), "l": ("volume", Decimal(1000)),
             "adet": ("count", Decimal(1))}
    if scale[source][0] != scale[target][0]:
        fail("Kütle/hacim/adet arasında dönüşüm yapılamaz.", "unit_mismatch")
    value = quantity(value, zero=zero)
    return quantity(Decimal(str(value)) * scale[source][1] / scale[target][1], zero=zero)


def _expiry(value, *, required=False):
    text = str(value or "").strip()
    if not text:
        if required:
            fail("Hammadde lotunun geçerli SKT bilgisi gerekli.", "expiry_required")
        return ""
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    fail("SKT YYYY-MM-DD veya GG.AA.YYYY olmalıdır.", "invalid_expiry")


def _fold(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", "".join(c for c in value if unicodedata.category(c) != "Cf")).strip()


def _external_text(value, private_terms):
    value = str(value or "").strip()
    folded = _fold(value)
    for term in private_terms:
        term = _fold(term)
        if term and re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", folded):
            fail("Dış metinde gerçek malzeme/kart/tedarikçi bilgisi var; yalnız kod kullanın.",
                 "private_information")
    return value


def _job(db, job_id, domain, *, lock=False):
    q = db.query(OutsourcingJob).filter_by(id=job_id, domain=domain)
    row = q.with_for_update().populate_existing().first() if lock else q.first()
    if row is None:
        fail("Fason işi bulunamadı.", "not_found", 404)
    return row


def _partner(db, partner_id, domain, *, lock=False):
    q = db.query(OutsourcingPartner).filter_by(id=partner_id, domain=domain, is_active=True)
    row = q.with_for_update().populate_existing().first() if lock else q.first()
    if row is None:
        fail("Fason firma bulunamadı.", "not_found", 404)
    return row


def _packet(db, job):
    row = db.query(OutsourcingPacketRevision).filter_by(job_id=job.id, revision=job.revision).first()
    if row is None:
        fail("Paket sürümü bulunamadı.", "missing_packet", 409)
    return row, json.loads(row.snapshot)


def _containers(db, job, *, lock=False):
    q = db.query(OutsourcingContainer).filter_by(job_id=job.id, is_active=True).order_by(OutsourcingContainer.id)
    return q.with_for_update().populate_existing().all() if lock else q.all()


def _approvals(db, job):
    return db.query(OutsourcingApproval).filter_by(job_id=job.id, revision=job.revision).all()


def _approved(db, job):
    packet, _ = _packet(db, job)
    assigned = {r: getattr(job, r + "_user_id") for r in ROLES}
    approvals = _approvals(db, job)
    return len(approvals) == 3 and all(a.packet_hash == packet.packet_hash and
        a.role in assigned and a.user_id == assigned[a.role] for a in approvals)


def _revision_guard(db, job, data):
    packet, snap = _packet(db, job)
    if int(data.get("revision", 0)) != job.revision or data.get("packet_hash") != packet.packet_hash:
        fail("Paket değişti; son sürümü yeniden açın.", "revision_stale", 409)
    return packet, snap


def _editable(job):
    if job.frozen_at or job.status in ("CANCELLED", "CLOSED"):
        fail("Gönderim sonrası paket değişmez; yeni fason işi açın.", "packet_frozen", 409)


def create_partner(db, domain, actor, data):
    name = str(data.get("name") or "").strip()
    if not name:
        fail("Firma adı gerekli.")
    if db.query(OutsourcingPartner.id).filter_by(domain=domain, name=name).first():
        fail("Bu firma zaten kayıtlı.", "duplicate_partner", 409)
    row = OutsourcingPartner(domain=domain, name=name, contact=data.get("contact"), created_by=_actor(actor)[0])
    db.add(row); db.flush()
    _audit(db, actor, "partner_create", details={"partner_id": row.id})
    return row


def create_material_code(db, domain, actor, data):
    partner = _partner(db, data["partner_id"], domain, lock=True)
    item = db.query(Item).filter_by(id=data["item_id"], domain=domain, is_active=True).first()
    if not item or lot_kind(item.category) == "finished":
        fail("Aktif hammadde/ambalaj kartı bulunamadı.", "invalid_material")
    specification = str(data.get("specification") or "").strip()
    safety = str(data.get("safety_instructions") or "").strip()
    if len(specification) < 3:
        fail("Form/konsantrasyon/grade dahil açık malzeme tanımı gerekli.", "specification_required")
    terms = [item.name, item.name_tr, item.sku, item.supplier.name if item.supplier else ""]
    safety = _external_text(safety, terms)
    identity = _hash(specification)
    existing = db.query(OutsourcingMaterialCode).filter_by(partner_id=partner.id, item_id=item.id,
                                                          spec_hash=identity).first()
    if existing:
        if existing.safety_instructions != safety:
            fail("Kod tanımı değişmez; yeni spec versiyonu oluşturun.", "identity_immutable", 409)
        return existing
    code = str(data.get("code") or "M-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
                                                  for _ in range(8))).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9-]{1,39}", code):
        fail("Kod 2–40 büyük harf/rakam/tire olmalıdır.", "invalid_code")
    _external_text(code, terms)
    if db.query(OutsourcingMaterialCode.id).filter_by(partner_id=partner.id, code=code).first():
        fail("Bu firma için kod daha önce kullanılmış.", "duplicate_code", 409)
    from core.consumption import item_rec, _kind
    row = OutsourcingMaterialCode(domain=domain, partner_id=partner.id, item_id=item.id, code=code,
        specification=specification, spec_hash=identity, safety_instructions=safety,
        unit=unit(item.unit), kind=_kind(item_rec(item)), created_by=_actor(actor)[0])
    db.add(row); db.flush()
    _audit(db, actor, "material_code_create", details={"material_code_id": row.id})
    return row


def preview(db, domain, data):
    _partner(db, data["partner_id"], domain)
    qty = quantity(data["quantity"])
    lang = str(data.get("label_language", "TR")).upper()
    if lang not in ("TR", "EN"):
        fail("Etiket dili TR veya EN olmalıdır.")
    recs, items, sibs = load_recipe_recs(db, [data["recipe_id"]], domain)
    if not recs or not recs[0].target_item_id or recs[0].target_item_id not in items:
        fail("Aktif reçete/hedef bulunamadı.", "invalid_recipe")
    rec = recs[0]
    ingredients = []
    for ing in rec.ingredients:
        item = items.get(ing.item_id)
        if not item or not item.is_active:
            fail("Reçetede pasif/eksik malzeme var.", "invalid_material")
        ingredients.append(replace(ing, quantity=convert_quantity(ing.quantity, ing.unit or item.unit, item.unit),
                                   unit=unit(item.unit)))
    rec = replace(rec, ingredients=tuple(ingredients))
    if not math.isfinite(rec.output_quantity) or rec.output_quantity <= 0 or not math.isfinite(rec.waste_percentage) or rec.waste_percentage < 0:
        fail("Reçete verim/fire bilgisi geçersiz.", "invalid_recipe")
    expansion = expand_recipe(rec, qty, items, sibs, label_mode=lang)
    if expansion.missing_item_ids or expansion.skipped_labels:
        fail("Reçetede eksik malzeme veya seçilen dilde etiket var.", "missing_label_or_material")
    materials = []
    from core.production_plan import _build_lines
    for line in _build_lines(rec, expansion, qty):
        item = items[line.item_id]
        codes = db.query(OutsourcingMaterialCode).filter_by(domain=domain, partner_id=data["partner_id"],
                                                            item_id=item.id).order_by(OutsourcingMaterialCode.id).all()
        materials.append({"item_id": item.id, "item_name": item.name, "kind": line.kind,
            "quantity": quantity(line.gross), "dispatch_quantity": quantity(line.gross),
            "unit": unit(item.unit), "phase": line.phase or "",
            "parts": [{"phase": p or "", "quantity": round(q, 6)} for _, p, q in line.parts],
            "material_codes": [{"id": c.id, "code": c.code, "specification": c.specification,
                                "safety_instructions": c.safety_instructions} for c in codes]})
    if not materials:
        fail("Reçete boş.", "invalid_recipe")
    target = items[rec.target_item_id]
    if not target.is_active or lot_kind(target.category) != "finished":
        fail("Aktif bitmiş ürün hedefi gerekli.", "invalid_target")
    return {"materials": materials, "unit": unit(target.unit), "target_item_id": target.id}


def _job_snapshot(db, domain, data):
    result = preview(db, domain, data)
    ids = data.get("material_code_ids") or {}
    dispatch_quantities = data.get("dispatch_quantities") or {}
    materials, private_terms = [], []
    for mat in result["materials"]:
        choices = mat.pop("material_codes")
        code_id = ids.get(str(mat["item_id"]), ids.get(mat["item_id"]))
        if code_id is None and len(choices) == 1:
            code_id = choices[0]["id"]
        code = db.query(OutsourcingMaterialCode).filter_by(id=code_id, domain=domain,
                  partner_id=data["partner_id"], item_id=mat["item_id"]).first() if code_id else None
        if not code:
            fail("Her malzeme için bu firmaya ait birebir spec kodunu seçin.", "material_code_required")
        if code.unit != mat["unit"] or code.kind != mat["kind"]:
            fail("Kart birimi/türü kod tanımından değişmiş; yeniden teknik değerlendirme gerekli.", "material_identity_changed")
        item = db.query(Item).filter_by(id=mat["item_id"]).first()
        private_terms.extend([item.name, item.name_tr, item.sku,
                              item.supplier.name if item.supplier else ""])
        mat.update(code_id=code.id, code=code.code, specification=code.specification,
                   safety_instructions=code.safety_instructions)
        mat["dispatch_quantity"] = quantity(dispatch_quantities.get(str(mat["item_id"]),
                                              dispatch_quantities.get(mat["item_id"], mat["quantity"])))
        if mat["dispatch_quantity"] + EPS < mat["quantity"]:
            fail("Onaylı gönderim miktarı tartım hedefinden az olamaz.", "dispatch_below_target")
        materials.append(mat)
    notes = _external_text(data.get("external_notes"), private_terms)
    name = _external_text(data.get("external_product_name"), private_terms)
    if not notes or not name:
        fail("Kodlu dış ürün adı ve kodlu proses/güvenlik metni gerekli.", "external_notes_required")
    return {"materials": materials, "target_item_id": result["target_item_id"], "unit": result["unit"],
            "external_notes": notes, "external_product_name": name,
            "quantity": quantity(data["quantity"]), "label_language": data.get("label_language", "TR")}


def _save_revision(db, job, actor, snapshot):
    snapshot = dict(snapshot)
    snapshot["containers"] = [{"id": c.id, "container_uid": c.container_uid, "code_id": c.material_code_id,
        "item_id": c.item_id, "inventory_id": c.inventory_id, "source_lot_number": c.source_lot_number,
        "source_snapshot": json.loads(c.source_snapshot), "external_lot": c.external_lot,
        "quantity": c.quantity, "unit": c.unit, "expiry_date": c.expiry_date or ""}
        for c in _containers(db, job)]
    snapshot["approver_ids"] = {r: getattr(job, r + "_user_id") for r in ROLES}
    row = OutsourcingPacketRevision(job_id=job.id, revision=job.revision, snapshot=_json(snapshot),
        packet_hash=_hash(snapshot), created_by=_actor(actor)[0])
    db.add(row); db.flush()
    job.status = "DRAFT"
    return row


def create_job(db, domain, actor, data):
    _partner(db, data["partner_id"], domain)
    approvers = data.get("approver_ids") or {}
    if set(approvers) != set(ROLES) or len(set(approvers.values())) != 3:
        fail("Üç farklı onay hesabı gerekli.", "distinct_approvers_required")
    for role in ROLES:
        user = db.query(User).filter_by(id=approvers[role], is_active=True).first()
        expected = {"technical": "LabLead", "owner": "SuperAdmin", "manager": "Manager"}[role]
        if not user or user.role != expected:
            fail("Atanan teknik/sipariş/yönetici hesabının rolü uygun değil.", "invalid_approver")
    snap = _job_snapshot(db, domain, data)
    job = OutsourcingJob(domain=domain, partner_id=data["partner_id"], recipe_id=data["recipe_id"],
        target_item_id=snap["target_item_id"], quantity=snap["quantity"], unit=snap["unit"],
        label_language=snap["label_language"], external_product_name=snap["external_product_name"],
        external_notes=snap["external_notes"], technical_user_id=approvers["technical"],
        owner_user_id=approvers["owner"], manager_user_id=approvers["manager"], created_by=_actor(actor)[0],
        revision=1, status="DRAFT")
    db.add(job); db.flush()
    _save_revision(db, job, actor, snap)
    _audit(db, actor, "job_create", job.id, {"revision": job.revision})
    return job


def update_job(db, domain, actor, job_id, data):
    job = _job(db, job_id, domain, lock=True)
    _editable(job)
    _revision_guard(db, job, data)
    _, old = _packet(db, job)
    values = {"partner_id": job.partner_id, "recipe_id": job.recipe_id, "quantity": job.quantity,
        "label_language": job.label_language, "external_product_name": job.external_product_name,
        "external_notes": job.external_notes, "material_code_ids": {str(m["item_id"]): m["code_id"] for m in old["materials"]},
        "dispatch_quantities": {str(m["item_id"]): m.get("dispatch_quantity", m["quantity"]) for m in old["materials"]}}
    values.update({k: v for k, v in data.items() if k in values and k not in ("partner_id", "recipe_id")})
    snap = _job_snapshot(db, domain, values)
    for c in _containers(db, job):
        match = next((m for m in snap["materials"] if m["code_id"] == c.material_code_id), None)
        if not match:
            fail("Önce eski kodun kaplarını taslaktan kaldırın.", "container_identity_changed")
    for key in ("quantity", "unit", "label_language", "external_product_name", "external_notes"):
        setattr(job, key, snap[key])
    job.revision += 1
    _save_revision(db, job, actor, snap)
    _audit(db, actor, "job_revision", job.id, {"revision": job.revision})
    return job


def _eligible_lot(inv, item, domain, *, raw=False):
    if not inv or inv.item_id != item.id or inv.domain != domain or inv.status != "APPROVED" or inv.qc_required or inv.is_sample:
        fail("Kaynak lot aktif panelde QC onaylı, normal stok olmalıdır.", "invalid_source_lot")
    expiry = _expiry(inv.expiry_date, required=raw)
    if expiry and date.fromisoformat(expiry) < date.today():
        fail("Kaynak lotun SKT'si geçmiş.", "expired_lot")
    return expiry


def create_container(db, domain, actor, job_id, data):
    peek = _job(db, job_id, domain)
    _, packet = _packet(db, peek)
    source_material = next((m for m in packet["materials"] if m["code_id"] == data["material_code_id"]), None)
    if source_material is None:
        fail("Kod bu paketin malzemesi değil.", "invalid_material_code")
    job, _, _, _ = _stock_job(db, domain, job_id, extra_items=(source_material["item_id"],),
                              extra_lots=(data["inventory_id"],) if data.get("inventory_id") else ())
    _editable(job)
    _, snap = _packet(db, job)
    mat = next((m for m in snap["materials"] if m["code_id"] == data["material_code_id"]), None)
    if not mat:
        fail("Kod bu paketin malzemesi değil.", "invalid_material_code")
    item = db.query(Item).filter_by(id=mat["item_id"], domain=domain, is_active=True).first()
    if not item or unit(item.unit) != mat["unit"]:
        fail("Kaynak kart değişmiş.", "material_identity_changed")
    qty = convert_quantity(data["quantity"], data.get("unit") or mat["unit"], mat["unit"])
    if sum(c.quantity for c in _containers(db, job) if c.material_code_id == mat["code_id"]) + qty > mat.get("dispatch_quantity", mat["quantity"]) + EPS:
        fail("Hazırlanan kaplar onay hedefini aşamaz.", "over_planned_quantity")
    inv_id = data.get("inventory_id")
    inv = db.query(Inventory).filter_by(id=inv_id).first() if inv_id else None
    expiry = _eligible_lot(inv, item, domain, raw=mat["kind"] == "raw") if inv_id else ""
    if mat["kind"] == "raw" and inv is None:
        fail("Hammadde için kaynak lot seçimi zorunlu.", "source_lot_required")
    if inv is not None:
        # Aynı lota bağlı, henüz gönderilmemiş (bu ve başka açık işlerdeki) kaplar
        # lotun güncel miktarını aşamaz — üç onaydan sonra sevkte patlamasın.
        bound = sum(float(q or 0) for (q,) in db.query(OutsourcingContainer.quantity)
                    .join(OutsourcingJob, OutsourcingJob.id == OutsourcingContainer.job_id)
                    .filter(OutsourcingContainer.inventory_id == inv.id,
                            OutsourcingContainer.is_active == True,                 # noqa: E712
                            OutsourcingContainer.dispatched_quantity <= EPS,
                            ~OutsourcingJob.status.in_(("CANCELLED", "CLOSED"))))
        if bound + qty > float(inv.quantity or 0) + EPS:
            fail("Kaynak lotta bu kap için yeterli miktar yok (hazırlanmış kaplar dahil).",
                 "insufficient_lot", 409)
    source = {"item_id": item.id, "unit": mat["unit"], "supplier_id": inv.supplier_id if inv else item.supplier_id,
              "lot_number": inv.lot_number if inv else None}
    row = OutsourcingContainer(job_id=job.id, material_code_id=mat["code_id"], item_id=item.id,
        inventory_id=inv.id if inv else None, container_uid="C-" + secrets.token_hex(16),
        external_lot="E-" + secrets.token_hex(12), source_lot_number=inv.lot_number if inv else None,
        source_snapshot=_json(source), expiry_date=expiry, quantity=qty, unit=mat["unit"],
        created_by=_actor(actor)[0])
    db.add(row); db.flush()
    job.revision += 1
    _save_revision(db, job, actor, snap)
    _audit(db, actor, "container_fill", job.id, {"container_id": row.id, "revision": job.revision})
    return job


def remove_container(db, domain, actor, job_id, container_id):
    job = _job(db, job_id, domain, lock=True)
    _editable(job)
    _, snap = _packet(db, job)
    row = next((c for c in _containers(db, job) if c.id == container_id), None)
    if not row:
        fail("Kap bulunamadı.", "not_found", 404)
    row.is_active = False
    db.flush()
    job.revision += 1
    _save_revision(db, job, actor, snap)
    _audit(db, actor, "container_void", job.id, {"container_id": row.id})
    return job


def approve_job(db, domain, actor, job_id, revision, packet_hash):
    job = _job(db, job_id, domain, lock=True)
    if job.status in ("CANCELLED", "CLOSED"):
        fail("İş kapalı.", "job_terminal", 409)
    packet, snap = _revision_guard(db, job, {"revision": revision, "packet_hash": packet_hash})
    uid = _actor(actor)[0]
    role = next((r for r in ROLES if getattr(job, r + "_user_id") == uid), None)
    user = db.query(User).filter_by(id=uid, is_active=True).first()
    if not role or not user:
        fail("Yalnız atanmış hesap kendi onayını verebilir.", "not_assigned_approver", 403)
    approvals = _approvals(db, job)
    if any(a.role == role for a in approvals):
        return job
    if role != "technical" and not any(a.role == "technical" for a in approvals):
        fail("Önce Songül'ün teknik onayı gerekli.", "technical_approval_required", 409)
    if role == "technical":
        containers = _containers(db, job)
        for mat in snap["materials"]:
            if not mat.get("safety_instructions", "").strip():
                fail("Her kod için insan tarafından doğrulanacak güvenlik talimatı gerekli.", "safety_required")
            prepared = sum(c.quantity for c in containers if c.material_code_id == mat["code_id"])
            if abs(prepared - mat.get("dispatch_quantity", mat["quantity"])) > EPS:
                fail("Teknik onaydan önce her malzemenin hedef miktarı kaplara atanmalı.", "containers_incomplete")
            code = db.query(OutsourcingMaterialCode).filter_by(id=mat["code_id"]).first()
            code.verified_by, code.verified_at = uid, datetime.utcnow()
    db.add(OutsourcingApproval(job_id=job.id, revision=job.revision, packet_hash=packet.packet_hash,
                               role=role, user_id=uid))
    db.flush()
    job.status = "APPROVED" if _approved(db, job) else "TECH_APPROVED"
    _audit(db, actor, "approve", job.id, {"role": role, "revision": job.revision, "packet_hash": packet_hash})
    return job


def _stock_job(db, domain, job_id, *, extra_items=(), extra_lots=()):
    """Peek IDs, then lock in the same direction as QC/normal stock paths.

    The mutable job is re-read after obtaining stock locks. If a draft gained
    new containers while waiting, callers reject/retry rather than lock backwards.
    """
    peek = _job(db, job_id, domain)
    before = _containers(db, peek)
    item_ids = sorted({peek.target_item_id, *(c.item_id for c in before), *extra_items})
    cards = {r.id: r for r in db.query(Item).filter(Item.id.in_(item_ids))
             .order_by(Item.id).with_for_update().populate_existing().all()}
    inv_ids = sorted({c.inventory_id for c in before if c.inventory_id} | set(extra_lots))
    inventories = {r.id: r for r in db.query(Inventory).filter(Inventory.id.in_(inv_ids))
                   .order_by(Inventory.id).with_for_update().populate_existing().all()} if inv_ids else {}
    job = _job(db, job_id, domain, lock=True)
    containers = _containers(db, job, lock=True)
    if any(c.item_id not in cards or (c.inventory_id and c.inventory_id not in inventories) for c in containers):
        fail("Kap hazırlığı değişti; işlemi yeniden deneyin.", "revision_stale", 409)
    return job, containers, cards, inventories


def _operation(db, job, actor, kind, data):
    key = str(data.get("idempotency_key") or "").strip()
    if not key or len(key) > 100:
        fail("İşleme ait idempotency_key gerekli.", "idempotency_required")
    digest = _hash({k: v for k, v in data.items() if k != "idempotency_key"})
    existing = db.query(OutsourcingOperation).filter_by(job_id=job.id, idempotency_key=key).first()
    if existing:
        if existing.kind != kind or existing.request_hash != digest:
            fail("Bu işlem anahtarı farklı içerikle kullanılmış.", "idempotency_conflict", 409)
        return existing, True
    row = OutsourcingOperation(job_id=job.id, kind=kind, idempotency_key=key,
                                request_hash=digest, actor_id=_actor(actor)[0])
    db.add(row); db.flush()
    return row, False


def dispatch(db, domain, actor, job_id, data):
    job, containers, cards, inventories = _stock_job(db, domain, job_id)
    op, replay = _operation(db, job, actor, "dispatch", data)
    if replay:
        return job
    _, snap = _revision_guard(db, job, data)
    if job.status in ("CANCELLED", "CLOSED") or not _approved(db, job):
        fail("Son paketi üç atanmış hesap onaylamadan gönderim yapılamaz.", "approval_required", 409)
    ids = data.get("container_ids") or []
    if not ids or len(set(ids)) != len(ids):
        fail("En az bir farklı kap seçin.", "invalid_containers")
    selected = [c for c in containers if c.id in ids]
    if len(selected) != len(ids) or any(c.dispatched_quantity > EPS for c in selected):
        fail("Kaplar bu işe ait, aktif ve henüz gönderilmemiş olmalıdır.", "container_already_dispatched", 409)
    frozen = {c["id"]: c for c in snap["containers"]}
    by_card, by_lot = defaultdict(float), defaultdict(float)
    for c in selected:
        approved = frozen.get(c.id)
        if not approved or any(approved[k] != getattr(c, k) for k in
                              ("quantity", "unit", "item_id", "inventory_id", "material_code_id") if k != "material_code_id"):
            fail("Kap onaylı paketten değişmiş.", "container_changed", 409)
        if approved["code_id"] != c.material_code_id:
            fail("Kap kodu onaylı paketten değişmiş.", "container_changed", 409)
        item = cards.get(c.item_id)
        material = next((m for m in snap["materials"] if m["code_id"] == c.material_code_id), None)
        if not material or not item or not item.is_active or item.domain != domain or unit(item.unit) != c.unit:
            fail("Kaynak kart birimi/paneli/aktivasyonu değişmiş.", "material_identity_changed")
        inv = inventories.get(c.inventory_id) if c.inventory_id else None
        if inv is not None:
            expiry = _eligible_lot(inv, item, domain, raw=material["kind"] == "raw")
            source = json.loads(c.source_snapshot)
            if inv.lot_number != source["lot_number"] or inv.supplier_id != source["supplier_id"] or expiry != (c.expiry_date or ""):
                fail("Kap kaynak lotu/tedarikçisi/SKT değişmiş; yeniden teknik hazırlık gerekli.", "source_lot_changed", 409)
            by_lot[inv.id] += c.quantity
        elif material["kind"] == "raw":
            fail("Hammadde kaynak lotu gerekli; toplam stoktan fallback yapılamaz.", "source_lot_required")
        by_card[item.id] += c.quantity
    for item_id, qty in by_card.items():
        if float(cards[item_id].current_stock or 0) + EPS < qty:
            fail("Güncel kart stoğu gönderilecek kapları karşılamıyor.", "insufficient_stock", 409)
    for inv_id, qty in by_lot.items():
        if float(inventories[inv_id].quantity or 0) + EPS < qty:
            fail("Güncel kaynak lotu gönderilecek kapları karşılamıyor.", "insufficient_lot", 409)
    # Packet totals are checked independently of mutable counters.
    for mat in snap["materials"]:
        sent = sum(c.dispatched_quantity for c in containers if c.material_code_id == mat["code_id"])
        new = sum(c.quantity for c in selected if c.material_code_id == mat["code_id"])
        if sent + new > mat.get("dispatch_quantity", mat["quantity"]) + EPS:
            fail("Etap toplamı onaylı malzeme hedefini aşamaz.", "over_planned_quantity", 409)
    shipment = OutsourcingShipment(job_id=job.id, revision=job.revision,
        packet_hash=_packet(db, job)[0].packet_hash, operation_id=op.id, dispatched_by=_actor(actor)[0])
    db.add(shipment); db.flush()
    # Lot bağlı kaplar önce: seçilen lot düşülür ve flush edilir; lotsuz kaplar
    # (ambalaj/etiket) ardından FIFO ile lot satırlarından düşülür — yalnız
    # current_stock'u düşmek lot tablosunu defterden koparırdı (09.09.2026 lot
    # kayması).  Defter yine kap başına TEK Output (ShipmentLine sözleşmesi).
    for c in sorted(selected, key=lambda row: (row.inventory_id is None, row.id)):
        item, inv = cards[c.item_id], inventories.get(c.inventory_id)
        item.current_stock = round(float(item.current_stock or 0) - c.quantity, 6)
        lot_number, lot_note = (inv.lot_number if inv else None), ""
        if inv is not None:
            inv.quantity = round(float(inv.quantity or 0) - c.quantity, 6)
            inv.updated_at = datetime.utcnow()
            db.flush()
        else:
            touched, _uncovered = draw_down(db, item, c.quantity)
            if len(touched) == 1 and touched[0][0] != "—":
                lot_number = touched[0][0]
            elif touched:
                lot_note = " | Lotlar: " + ", ".join(f"{lot} ({qty:g})" for lot, qty in touched)
        tx = Transaction(item_id=item.id, lot_number=lot_number,
            transaction_type="Output", quantity=c.quantity, performed_by=_actor(actor)[1],
            notes=f"Fason sevk — FS-{job.id} | Paket r{job.revision} | Kap {c.container_uid}{lot_note}")
        db.add(tx); db.flush()
        db.add(OutsourcingShipmentLine(shipment_id=shipment.id, container_id=c.id,
                                       transaction_id=tx.id, quantity=c.quantity))
        c.dispatched_quantity = c.quantity
    job.frozen_at = job.frozen_at or datetime.utcnow()
    job.status = "DISPATCHED"
    _audit(db, actor, "dispatch", job.id, {"shipment_id": shipment.id, "container_ids": ids, "revision": job.revision})
    return job


def _outstanding(c):
    return round(c.dispatched_quantity - c.consumed_quantity - c.waste_quantity - c.returned_quantity, 6)


def _live_external(job):
    if not job.frozen_at or job.status in ("CANCELLED", "CLOSED"):
        fail("Dış hareket yalnız gönderilmiş ve açık işte kaydedilebilir.", "job_not_dispatched", 409)


def _entries(containers, entries):
    if not entries or len({e["container_id"] for e in entries}) != len(entries):
        fail("Her kap için tek miktar satırı gerekli.", "invalid_entries")
    by_id = {c.id: c for c in containers}
    result = []
    for entry in entries:
        c = by_id.get(entry["container_id"])
        if c is None or c.dispatched_quantity <= EPS:
            fail("Kap bu işte gönderilmemiş.", "invalid_container")
        result.append((c, entry))
    return result


def record_consumption(db, domain, actor, job_id, data):
    job = _job(db, job_id, domain, lock=True)
    op, replay = _operation(db, job, actor, "consumption", data)
    if replay:
        return job
    _live_external(job)
    for c, entry in _entries(_containers(db, job, lock=True), data.get("entries")):
        consumed = convert_quantity(entry.get("consumed_quantity", 0), entry.get("unit") or c.unit, c.unit, zero=True)
        waste = convert_quantity(entry.get("waste_quantity", 0), entry.get("unit") or c.unit, c.unit, zero=True)
        reason = str(entry.get("reason") or "").strip()
        if consumed + waste <= 0:
            fail("Yeni tüketim veya fire miktarı gerekli.", "invalid_quantity")
        if waste > 0 and len(reason) < 5:
            fail("Fire/kayıp için en az beş karakterlik gerekçe gerekli.", "reason_required")
        if consumed + waste > _outstanding(c) + EPS:
            fail("Tüketim ve fire dış bakiyeyi aşamaz.", "over_external_balance", 409)
        for kind, qty in (("consumption", consumed), ("waste", waste)):
            if qty > 0:
                db.add(OutsourcingMovement(job_id=job.id, operation_id=op.id, container_id=c.id,
                                          kind=kind, quantity=qty, unit=c.unit, reason=reason))
        c.consumed_quantity = round(c.consumed_quantity + consumed, 6)
        c.waste_quantity = round(c.waste_quantity + waste, 6)
    job.status = "RECONCILING"
    _audit(db, actor, "consumption", job.id, {"operation_id": op.id})
    return job


def _new_receipt(db, job, op, *, item_id, kind, qty, stock_unit, external_lot, expiry="", container=None,
                 received_by="—"):
    receipt = OutsourcingReceipt(job_id=job.id, operation_id=op.id, container_id=container.id if container else None,
        item_id=item_id, kind=kind, quantity=qty, unit=stock_unit, external_lot=external_lot)
    db.add(receipt); db.flush()
    # Separate physical receipts never upsert into source/other QC lots.
    # Kullanılmamış iade özgün lot no + tedarikçiyi taşır (kabın dondurulmuş
    # snapshot'ından; kaynak satır sonradan değişse/silinse de) — izlenebilirlik
    # sevk Output'u ile iade Input'unu aynı lot altında gösterir.  Satır yine
    # AYRI: outsourcing_receipt_id normal lotla birleşmeyi engeller.
    source = json.loads(container.source_snapshot) if container is not None else {}
    lot_number = (source.get("lot_number") if kind == "return" else None) or f"FS-{job.id}-R{receipt.id}"
    inv = Inventory(item_id=item_id, lot_number=lot_number, quantity=qty,
        supplier_id=source.get("supplier_id") if kind == "return" else None,
        location="Fason Şahit Karantina" if kind == "finished_sample" else "Fason Karantina",
        status="QUARANTINE", qc_required=True, is_sample=False,
        expiry_date=expiry or None, domain=job.domain, received_by=received_by[:50],
        outsourcing_receipt_id=receipt.id)
    db.add(inv); db.flush()
    return receipt


def receive_returns(db, domain, actor, job_id, data):
    job, containers, _, _ = _stock_job(db, domain, job_id)
    op, replay = _operation(db, job, actor, "returns", data)
    if replay:
        return job
    _live_external(job)
    for c, entry in _entries(containers, data.get("entries")):
        qty = convert_quantity(entry["quantity"], entry.get("unit") or c.unit, c.unit)
        if qty > _outstanding(c) + EPS:
            fail("Fiziksel iade dış bakiyeyi aşamaz.", "over_external_balance", 409)
        _new_receipt(db, job, op, item_id=c.item_id, kind="return", qty=qty, stock_unit=c.unit,
                     external_lot=c.external_lot, expiry=c.expiry_date, container=c,
                     received_by=_actor(actor)[1])
        c.returned_quantity = round(c.returned_quantity + qty, 6)
        db.add(OutsourcingMovement(job_id=job.id, operation_id=op.id, container_id=c.id,
                                   kind="return", quantity=qty, unit=c.unit))
    job.status = "RECONCILING"
    _audit(db, actor, "returns", job.id, {"operation_id": op.id})
    return job


def receive_finished(db, domain, actor, job_id, data):
    job, _, cards, _ = _stock_job(db, domain, job_id)
    op, replay = _operation(db, job, actor, "receipts", data)
    if replay:
        return job
    _live_external(job)
    item = cards[job.target_item_id]
    if item.domain != domain or not item.is_active or unit(item.unit) != job.unit:
        fail("Bitmiş ürün hedefi/birimi değişmiş.", "material_identity_changed")
    qty = convert_quantity(data["quantity"], data.get("unit") or job.unit, job.unit)
    sample = convert_quantity(data.get("sample_quantity", 0), data.get("unit") or job.unit, job.unit, zero=True)
    if sample > qty + EPS or (job.unit == "adet" and (qty != int(qty) or sample != int(sample))):
        fail("Şahit miktarı toplamın içinde olmalı; adet kesirli olamaz.", "invalid_sample_quantity")
    received = sum(r.quantity for r in db.query(OutsourcingReceipt).filter(
        OutsourcingReceipt.job_id == job.id, OutsourcingReceipt.kind.in_(("finished", "finished_sample"))))
    if received + qty > job.quantity + EPS:
        fail("Toplam mamul kabulü onaylı üretim miktarını aşamaz.", "over_planned_quantity", 409)
    external_lot = str(data.get("external_lot") or "").strip()
    if not external_lot or len(external_lot) > 100:
        fail("Üretici parti/lot numarası gerekli.", "external_lot_required")
    expiry = _expiry(data.get("expiry_date"), required=True)
    if date.fromisoformat(expiry) < date.today():
        fail("Mamul SKT'si geçmiş.", "expired_lot")
    if qty - sample > EPS:
        _new_receipt(db, job, op, item_id=item.id, kind="finished", qty=round(qty - sample, 6),
                     stock_unit=job.unit, external_lot=external_lot, expiry=expiry,
                     received_by=_actor(actor)[1])
    if sample > 0:
        _new_receipt(db, job, op, item_id=item.id, kind="finished_sample", qty=sample,
                     stock_unit=job.unit, external_lot=external_lot, expiry=expiry,
                     received_by=_actor(actor)[1])
    job.status = "RECONCILING"
    _audit(db, actor, "receipts", job.id, {"operation_id": op.id, "quantity": qty, "sample_quantity": sample})
    return job


record_return = receive_returns
record_finished_receipt = receive_finished


def approve_receipt(db, inv, actor, decision=None):
    """Root QC owns item->inventory locks, sets decision, then calls this helper.

    True bypasses legacy stock logic. Receipt terminal state, not the already
    mutated inventory status, determines whether stock has previously posted.
    """
    if not inv.outsourcing_receipt_id:
        return False
    receipt = db.query(OutsourcingReceipt).filter_by(id=inv.outsourcing_receipt_id) \
        .with_for_update().populate_existing().first()
    job = db.query(OutsourcingJob).filter_by(id=receipt.job_id).first() if receipt else None
    item = db.query(Item).filter_by(id=inv.item_id).first()
    decision = decision or inv.status
    if (not receipt or not job or not item or receipt.item_id != inv.item_id or
            item.domain != job.domain or inv.domain != job.domain or unit(item.unit) != receipt.unit or
            abs(float(inv.quantity or 0) - receipt.quantity) > EPS or inv.is_sample):
        fail("Fason kabulünün kart/lot/miktar/panel bağı bozulmuş.", "receipt_source_mismatch", 409)
    if decision not in ("APPROVED", "REJECTED"):
        fail("QC kararı geçersiz.", "invalid_qc_decision")
    if receipt.status != "QUARANTINE":
        if receipt.status == decision:
            return True
        fail("Fason QC kararı kesinleşmiş; tekrar değiştirilemez.", "receipt_terminal", 409)
    if receipt.input_transaction_id:
        fail("Karantina kabulü daha önce post edilmiş.", "receipt_already_posted", 409)
    if isinstance(actor, dict):
        actor_name = _actor(actor)[1]
    else:
        actor_name = str(actor or "—")[:100]
    witness = receipt.kind == "finished_sample"
    if decision == "APPROVED" and not witness:
        tx = Transaction(item_id=item.id, lot_number=inv.lot_number, transaction_type="Input",
            quantity=receipt.quantity, performed_by=actor_name,
            notes=f"{'Fason iade' if receipt.kind == 'return' else 'Fason kabul'} — FS-{job.id} | Kabul #{receipt.id}")
        db.add(tx); db.flush()
        receipt.input_transaction_id = tx.id
        item.current_stock = round(float(item.current_stock or 0) + receipt.quantity, 6)
    receipt.status, receipt.qc_at, receipt.qc_by = decision, datetime.utcnow(), actor_name
    # Şahit numune `is_sample` DEĞİLDİR (o bayrak alternatif tedarikçi numunesi
    # demek: numune listesi, "stoğa çevir", Numune Analizi kaynağı).  Onaylı
    # şahit RETAINED kalır — her stok havuzu yalnız APPROVED lot kullandığından
    # FIFO/teslimat/üretim ona dokunmaz; Input yazılmadığı için stokta da yok.
    inv.status = "RETAINED" if witness and decision == "APPROVED" else decision
    inv.qc_required = False
    db.add(AdminAuditLog(actor_name=actor_name, action="outsourcing.receipt_qc", target_type="outsourcing_receipt",
        target_id=receipt.id, details=_json({"job_id": job.id, "decision": decision,
                                          "input_transaction_id": receipt.input_transaction_id})))
    return True


def _settled(db, job):
    containers = _containers(db, job)
    if any(abs(_outstanding(c)) > EPS for c in containers):
        fail("Dışarıda kalan malzeme var; kullanılmayan miktarlar fiziksel geri gelmeli.", "external_balance_remaining", 409)
    receipts = db.query(OutsourcingReceipt).filter_by(job_id=job.id).all()
    if any(r.status not in ("APPROVED", "REJECTED") for r in receipts):
        fail("Tüm mamul/şahit/iade kabullerinin QC kararı tamamlanmalı.", "qc_pending", 409)
    return receipts


def close_job(db, domain, actor, job_id, data):
    job = _job(db, job_id, domain, lock=True)
    op, replay = _operation(db, job, actor, "close", data)
    if replay:
        return job
    _live_external(job)
    receipts = _settled(db, job)
    if not any(r.kind in ("finished", "finished_sample") for r in receipts):
        fail("Mamul kabulü yok; başarısız işi gerekçeyle iptal edin.", "finished_receipt_required", 409)
    job.status, job.closed_at = "CLOSED", datetime.utcnow()
    _audit(db, actor, "close", job.id, {"operation_id": op.id})
    return job


def cancel_job(db, domain, actor, job_id, data):
    job = _job(db, job_id, domain, lock=True)
    op, replay = _operation(db, job, actor, "cancel", data)
    if replay:
        return job
    if job.status in ("CLOSED", "CANCELLED"):
        fail("İş zaten kapalı.", "job_terminal", 409)
    reason = str(data.get("reason") or "").strip()
    if len(reason) < 5:
        fail("En az beş karakterlik iptal gerekçesi gerekli.", "reason_required")
    if job.frozen_at:
        _settled(db, job)
    job.status, job.cancel_reason, job.closed_at = "CANCELLED", reason, datetime.utcnow()
    _audit(db, actor, "cancel", job.id, {"operation_id": op.id, "reason": reason, "auto_return": False})
    return job


# ─── Read models ─────────────────────────────────────────────────────────────
# İç ekran (detail) ile dış belge (external_packet) AYRI kurulur.  Dış paket
# yalnız dondurulmuş paket sürümündeki izin listesi alanlarından oluşur; kart
# adı/SKU/tedarikçi/kaynak lot/spec metni hiçbir koşulda girmez.  İç detayda
# gerçek eşleştirme (kart adı, spec, kaynak lot) yalnız `mapping` yetkisiyle döner.

TERMINAL = ("CLOSED", "CANCELLED")
_STATUS_LABEL = {"QUARANTINE": "Kalite kontrol bekliyor", "APPROVED": "Onaylandı",
                 "REJECTED": "Reddedildi", "MIXED": "Kısmen sonuçlandı"}


def _when(value):
    from database import to_tr
    return to_tr(value).strftime("%d.%m.%Y %H:%M") if value else ""


def _code_map(snap):
    return {m["code_id"]: m["code"] for m in snap.get("materials", [])}


def _shipments(db, job, snap):
    frozen = {c["id"]: c for c in snap.get("containers", [])}
    codes = _code_map(snap)
    rows = db.query(OutsourcingShipment).filter_by(job_id=job.id).order_by(OutsourcingShipment.id).all()
    result = []
    for index, shipment in enumerate(rows, 1):
        lines = db.query(OutsourcingShipmentLine).filter_by(shipment_id=shipment.id) \
            .order_by(OutsourcingShipmentLine.id).all()
        containers = []
        for line in lines:
            c = frozen.get(line.container_id, {})
            containers.append({"container_uid": c.get("container_uid"), "code": codes.get(c.get("code_id")),
                               "external_lot": c.get("external_lot"), "quantity": line.quantity,
                               "unit": c.get("unit"), "expiry_date": c.get("expiry_date") or ""})
        result.append({"id": shipment.id, "reference": f"FS-{job.id}-S{index}",
                       "dispatched_at": _when(shipment.dispatched_at), "containers": containers})
    return result


def _balance(db, job):
    codes = {c.id: c.code for c in db.query(OutsourcingMaterialCode).join(
        OutsourcingContainer, OutsourcingContainer.material_code_id == OutsourcingMaterialCode.id)
        .filter(OutsourcingContainer.job_id == job.id)}
    return [{"container_id": c.id, "container_uid": c.container_uid, "code": codes.get(c.material_code_id),
             "unit": c.unit, "dispatched": c.dispatched_quantity, "consumed": c.consumed_quantity,
             "waste": c.waste_quantity, "returned": c.returned_quantity, "outstanding": _outstanding(c)}
            for c in _containers(db, job) if c.dispatched_quantity > EPS]


def external_packet(db, job, *, require_approval=True):
    """Dış üreticiye giden tek veri kaynağı (PDF'ler + ekrandaki kodlu önizleme)."""
    approved = _approved(db, job)
    if require_approval and not approved:
        fail("Kodlu belgeler üç atanmış hesap onayından sonra üretilir.", "approval_required", 409)
    _, snap = _packet(db, job)
    codes = _code_map(snap)
    partner = db.query(OutsourcingPartner).filter_by(id=job.partner_id).first()
    materials = [{"code": m["code"], "kind": m["kind"], "phase": m.get("phase") or "",
                  "parts": [{"phase": p.get("phase") or "", "quantity": p.get("quantity")}
                            for p in m.get("parts") or []],
                  "quantity": m["quantity"], "dispatch_quantity": m.get("dispatch_quantity", m["quantity"]),
                  "unit": m["unit"], "safety_instructions": m.get("safety_instructions") or ""}
                 for m in snap.get("materials", [])]
    containers = [{"container_uid": c["container_uid"], "code": codes.get(c["code_id"]),
                   "external_lot": c["external_lot"], "quantity": c["quantity"], "unit": c["unit"],
                   "expiry_date": c.get("expiry_date") or ""} for c in snap.get("containers", [])]
    balance = [{k: row[k] for k in ("container_uid", "code", "unit", "dispatched", "consumed",
                                    "waste", "returned", "outstanding")} for row in _balance(db, job)]
    shipments = [{k: s[k] for k in ("reference", "dispatched_at", "containers")}
                 for s in _shipments(db, job, snap)]
    return {"approved": approved, "job_reference": f"FS-{job.id}",
            "partner_reference": partner.name if partner else "",
            "revision": job.revision, "product_name": snap.get("external_product_name") or "",
            "quantity": snap.get("quantity"), "unit": snap.get("unit"),
            "label_language": snap.get("label_language") or "",
            "instructions": snap.get("external_notes") or "", "materials": materials,
            "containers": containers, "shipments": shipments, "balance": balance}


def job_summary(db, job, partner_names=None):
    packet = db.query(OutsourcingPacketRevision).filter_by(job_id=job.id, revision=job.revision).first()
    if partner_names is None:
        partner = db.query(OutsourcingPartner).filter_by(id=job.partner_id).first()
        partner_name = partner.name if partner else ""
    else:
        partner_name = partner_names.get(job.partner_id, "")
    return {"id": job.id, "reference": f"FS-{job.id}", "partner_id": job.partner_id,
            "partner_name": partner_name, "external_product_name": job.external_product_name,
            "quantity": job.quantity, "unit": job.unit, "label_language": job.label_language,
            "revision": job.revision, "status": job.status,
            "packet_hash": packet.packet_hash if packet else None,
            "approver_ids": {r: getattr(job, r + "_user_id") for r in ROLES},
            "created_at": _when(job.created_at), "frozen_at": _when(job.frozen_at),
            "closed_at": _when(job.closed_at), "cancel_reason": job.cancel_reason or ""}


def _receipt_rows(db, job):
    rows = db.query(OutsourcingReceipt).filter_by(job_id=job.id).order_by(OutsourcingReceipt.id).all()
    finished, returns = {}, []
    for r in rows:
        if r.kind == "return":
            returns.append({"id": r.id, "kind": "return", "container_id": r.container_id,
                            "external_lot": r.external_lot, "quantity": r.quantity, "unit": r.unit,
                            "status": r.status, "status_label": _STATUS_LABEL.get(r.status, r.status)})
            continue
        group = finished.setdefault(r.operation_id, {"kind": "finished", "operation_id": r.operation_id,
            "external_lot": r.external_lot, "quantity": 0.0, "sample_quantity": 0.0,
            "saleable_quantity": 0.0, "unit": r.unit, "statuses": set()})
        group["quantity"] = round(group["quantity"] + r.quantity, 6)
        key = "sample_quantity" if r.kind == "finished_sample" else "saleable_quantity"
        group[key] = round(group[key] + r.quantity, 6)
        group["statuses"].add(r.status)
    result = []
    for group in finished.values():
        statuses = group.pop("statuses")
        status = statuses.pop() if len(statuses) == 1 else "MIXED"
        group.update(status=status, status_label=_STATUS_LABEL.get(status, status))
        result.append(group)
    return result + returns


def job_detail(db, job, *, user_id, permissions):
    """İç ekran DTO'su.  `permissions` = kullanıcının outsourcing yetki sözlüğü."""
    _, snap = _packet(db, job)
    can_map = bool(permissions.get("mapping"))
    codes = {c.id: c for c in db.query(OutsourcingMaterialCode).filter(
        OutsourcingMaterialCode.id.in_([m["code_id"] for m in snap.get("materials", [])] or [0]))}
    items = {i.id: i for i in db.query(Item).filter(
        Item.id.in_([m["item_id"] for m in snap.get("materials", [])] or [0]))}
    materials = []
    for m in snap.get("materials", []):
        row = {"code_id": m["code_id"], "code": m["code"], "kind": m["kind"], "phase": m.get("phase") or "",
               "parts": m.get("parts") or [], "quantity": m["quantity"],
               "dispatch_quantity": m.get("dispatch_quantity", m["quantity"]), "unit": m["unit"]}
        if can_map:
            item, code = items.get(m["item_id"]), codes.get(m["code_id"])
            row.update(item_id=m["item_id"], item_name=item.name if item else "",
                       specification=code.specification if code else m.get("specification", ""))
        materials.append(row)
    code_text = {c.id: c.code for c in codes.values()}
    containers = []
    for c in _containers(db, job):
        row = {"id": c.id, "container_uid": c.container_uid, "code": code_text.get(c.material_code_id),
               "code_id": c.material_code_id, "quantity": c.quantity, "unit": c.unit,
               "expiry_date": c.expiry_date or "", "dispatched": c.dispatched_quantity > EPS}
        if can_map:
            row["source_lot_number"] = c.source_lot_number or ""
        containers.append(row)
    approvals = [{"role": a.role, "user_id": a.user_id, "approved_at": _when(a.approved_at)}
                 for a in _approvals(db, job)]
    approved = _approved(db, job)
    terminal = job.status in TERMINAL
    my_roles = [r for r in ROLES if getattr(job, r + "_user_id") == user_id]
    done = {a["role"] for a in approvals}
    can_approve = (not terminal and not job.frozen_at and any(r not in done for r in my_roles)
                   and ("technical" in done or "technical" in my_roles))
    undispatched = any(not c["dispatched"] for c in containers)
    actions = {"edit": not terminal and not job.frozen_at, "approve": can_approve,
               "dispatch": not terminal and approved and undispatched,
               "record": not terminal and bool(job.frozen_at),
               "close": not terminal and bool(job.frozen_at), "cancel": not terminal,
               "documents": approved}
    return {"job": job_summary(db, job), "actions": actions, "materials": materials,
            "containers": containers, "approvals": approvals, "balance": _balance(db, job),
            "receipts": _receipt_rows(db, job),
            "shipments": [{"id": s["id"], "reference": s["reference"], "dispatched_at": s["dispatched_at"]}
                          for s in _shipments(db, job, snap)],
            "external_packet": external_packet(db, job, require_approval=False)}


def list_jobs(db, domain):
    jobs = db.query(OutsourcingJob).filter_by(domain=domain).order_by(OutsourcingJob.id.desc()).limit(200).all()
    names = {p.id: p.name for p in db.query(OutsourcingPartner).filter_by(domain=domain)}
    return [job_summary(db, j, names) for j in jobs]


def eligible_lots(db, domain):
    """Kap hazırlığında seçilebilecek kaynak lotlar — `_eligible_lot` ile aynı kural."""
    today = date.today()
    rows = (db.query(Inventory, Item).join(Item, Item.id == Inventory.item_id)
            .filter(Inventory.domain == domain, Inventory.status == "APPROVED",
                    Inventory.qc_required == False, Inventory.is_sample == False,   # noqa: E712
                    Inventory.outsourcing_receipt_id.is_(None), Inventory.quantity > EPS,
                    Item.is_active == True)                                          # noqa: E712
            .order_by(Item.name, Inventory.created_at, Inventory.id).all())
    result = []
    for inv, item in rows:
        if lot_kind(item.category) == "finished":
            continue
        try:
            expiry = _expiry(inv.expiry_date)
        except OutsourcingError:
            continue
        if expiry and date.fromisoformat(expiry) < today:
            continue
        result.append({"id": inv.id, "item_id": item.id, "lot_number": inv.lot_number or "",
                       "quantity": inv.quantity, "unit": item.unit or "", "expiry_date": expiry})
    return result


def bootstrap(db, domain, permissions):
    partners = db.query(OutsourcingPartner).filter_by(domain=domain, is_active=True) \
        .order_by(OutsourcingPartner.name).all()
    recipes = (db.query(Recipe).join(Item, Item.id == Recipe.target_item_id)
               .filter(Recipe.domain == domain, Recipe.is_active == True,       # noqa: E712
                       Item.is_active == True).order_by(Recipe.name).all())      # noqa: E712
    approvers = (db.query(User).filter(User.is_active == True,                   # noqa: E712
                                       User.role.in_(("LabLead", "SuperAdmin", "Manager")))
                 .order_by(User.full_name, User.username).all())
    data = {"permissions": dict(permissions),
            "partners": [{"id": p.id, "name": p.name, "contact": p.contact or ""} for p in partners],
            "recipes": [{"id": r.id, "name": r.name} for r in recipes],
            "approvers": [{"id": u.id, "username": u.username, "full_name": u.full_name or "",
                           "role": u.role} for u in approvers]}
    if permissions.get("mapping"):
        names = {p.id: p.name for p in partners}
        items = (db.query(Item).filter(Item.domain == domain, Item.is_active == True)  # noqa: E712
                 .order_by(Item.name).all())
        data["items"] = [{"id": i.id, "name": i.name, "unit": i.unit or ""} for i in items
                         if lot_kind(i.category) != "finished"]
        item_names = {i.id: i.name for i in items}
        codes = db.query(OutsourcingMaterialCode).filter_by(domain=domain).order_by(OutsourcingMaterialCode.code).all()
        data["material_codes"] = [{"id": c.id, "partner_id": c.partner_id, "partner_name": names.get(c.partner_id, ""),
                                   "code": c.code, "item_id": c.item_id, "item_name": item_names.get(c.item_id, ""),
                                   "specification": c.specification, "unit": c.unit, "kind": c.kind,
                                   "verified": bool(c.verified_at)} for c in codes]
        data["lots"] = eligible_lots(db, domain)
    return data


def public_preview(result, permissions):
    """`preview` çıktısı — gerçek kart adı/spec yalnız mapping yetkisiyle."""
    if permissions.get("mapping"):
        return result
    materials = []
    for m in result["materials"]:
        row = {k: v for k, v in m.items() if k not in ("item_name",)}
        row["material_codes"] = [{k: v for k, v in c.items() if k != "specification"}
                                 for c in m.get("material_codes", [])]
        materials.append(row)
    return {**result, "materials": materials}
