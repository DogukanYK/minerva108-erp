#!/usr/bin/env python3
"""08.10.2026 onaylı firma politikası; varsayılan yalnız kuru çalıştırmadır.

    python scripts/apply_songul_supplier_policy_20261008.py
    python scripts/apply_songul_supplier_policy_20261008.py --commit

Yalnız cosmetics tedarikçi durumlarını değiştirir. Stok, defter, fiyat ve
malzeme tercihlerini değiştirmez. Çelişkiler çözülmeden hiçbir kart yazılmaz.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from core.purchase_pricing import SupplierIndex, alnum_fold  # noqa: E402
from core.supplier_prices import firm_keys  # noqa: E402
from core.suppliers import normalize_status  # noqa: E402
from database import (AdminAuditLog, Item, MaterialGroup, MaterialSupplierPref,  # noqa: E402
                      SessionLocal, Supplier)
from scripts.import_lab_price_notes_20261007 import (  # noqa: E402
    SUPPLIER_ALIASES, clean_supplier_name)

DOMAIN = "cosmetics"
POLICY_ID = "songul_supplier_policy_20261008"
ACTOR = "sistem (08.10.2026 tedarikçi planı)"
REASON = ("Songül Hanım ile konuşma ve 08.10.2026 onaylı plan: UMAYCHEM, NATURALYA, "
          "YİĞİTOGLU KİMYA ve DOGASA aynı kademede tercih edilir; "
          "TATLIDİLİMLER ve KRK GIDA için mevcut stok bitirilir, yeni alım yapılmaz.")
POLICY = (
    ("UMAYCHEM", "preferred"),
    ("NATURALYA", "preferred"),
    ("YİĞİTOGLU KİMYA", "preferred"),
    ("DOGASA", "preferred"),
    ("TATLIDİLİMLER", "phase_out"),
    ("KRK GIDA", "phase_out"),
)


class PolicyError(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__("; ".join(e["message"] for e in report["errors"]))


def _name_key(name):
    return next((v for k, v in firm_keys(None, name) if k == "name"), "")


def _canonical_name(name):
    cleaned = clean_supplier_name(name)
    return SUPPLIER_ALIASES.get(alnum_fold(cleaned), cleaned)


def _state(supplier):
    return {"purchase_status": supplier.purchase_status,
            "status_reason": supplier.status_reason, "status_by": supplier.status_by,
            "status_at": supplier.status_at.isoformat() if supplier.status_at else None}


def plan(db):
    """Tam eşleme + guard raporu; DB'ye ve ORM nesnelerine yazmaz."""
    with db.no_autoflush:
        suppliers = (db.query(Supplier).filter(Supplier.domain == DOMAIN)
                     .order_by(Supplier.id).populate_existing().all())
        prefs = (db.query(MaterialSupplierPref)
                 .filter(MaterialSupplierPref.domain == DOMAIN,
                         MaterialSupplierPref.preference == "preferred")
                 .order_by(MaterialSupplierPref.id).populate_existing().all())
    index = SupplierIndex([_canonical_name(s.name) for s in suppliers] +
                          [name for name, _ in POLICY] + ["DOALİNN"])
    key_for = lambda name: index.key(_canonical_name(name))
    by_id = {s.id: s for s in suppliers}
    firms, errors, changes, phase_out_keys = [], [], [], set()
    target_keys = [key_for(name) for name, _ in POLICY]
    if len(set(target_keys)) != len(POLICY) or key_for("DOGASA") == key_for("DOALİNN"):
        errors.append({"code": "ambiguous_firm", "message": "Onaylı firmalar aynı anahtara eşlendi; DOGASA ile DOALİNN ayrı olmalıdır."})

    canonical_firms = {key_for(name): name for name, _ in POLICY}
    canonical_firms[key_for("DOALİNN")] = "DOALİNN"
    markers = {alnum_fold(key): firm for key, firm in canonical_firms.items()}
    for alias, canonical in SUPPLIER_ALIASES.items():
        firm = canonical_firms.get(key_for(canonical))
        if firm:
            markers[alnum_fold(alias)] = firm
    for name, desired in POLICY:
        key = key_for(name)
        aliases = [s for s in suppliers if key_for(s.name) == key]
        active = [s for s in aliases if s.is_active is not False]
        firm = {"firm": name, "key": key, "status": desired,
                "aliases": [{"id": s.id, "name": s.name, "name_key": _name_key(s.name),
                             "is_active": s.is_active is not False,
                             "merged_into_id": s.merged_into_id,
                             "purchase_status": s.purchase_status} for s in aliases]}
        firms.append(firm)
        if not active:
            message = f"{name}: aktif firma kartı bulunamadı."
            if name == "DOGASA":
                message += " DOALİNN kartları DOGASA yerine eşlenmez."
            errors.append({"code": "missing_firm", "firm": name, "message": message})
        for s in aliases:
            present = sorted({brand for marker, brand in markers.items()
                              if marker and marker in alnum_fold(s.name)})
            if len(present) > 1:
                errors.append({"code": "ambiguous_alias", "supplier_id": s.id,
                               "message": f"#{s.id} {s.name}: birden çok onaylı firma adı içeriyor ({', '.join(present)})."})
            if s.merged_into_id is not None:
                dest = by_id.get(s.merged_into_id)
                if dest is None or key_for(dest.name) != key or s.is_active is not False:
                    errors.append({"code": "unexpected_merge", "supplier_id": s.id,
                                   "message": f"#{s.id} {s.name}: beklenmeyen birleştirme hedefi #{s.merged_into_id}."})
            if normalize_status(s.purchase_status) is None:
                errors.append({"code": "invalid_status", "supplier_id": s.id,
                               "message": f"#{s.id} {s.name}: geçersiz durum {s.purchase_status!r}."})
            if desired == "phase_out":
                phase_out_keys.update(firm_keys(s.id, s.name))
        for s in active:
            if s.purchase_status != desired or s.status_reason != REASON:
                changes.append({"supplier_id": s.id, "supplier_name": s.name, "firm": name,
                                "before": _state(s),
                                "after": {"purchase_status": desired, "status_reason": REASON,
                                          "status_by": ACTOR}})

    for pref in prefs:
        s = by_id.get(pref.supplier_id)
        if s is None or not (firm_keys(s.id, s.name) & phase_out_keys):
            continue
        if pref.item_id is not None:
            item = db.get(Item, pref.item_id)
            scope = {"item_id": pref.item_id, "item_name": item.name if item else None}
        else:
            group = db.get(MaterialGroup, pref.material_group_id)
            scope = {"material_group_id": pref.material_group_id,
                     "material_group_name": group.name if group else None}
        errors.append({"code": "contrary_material_preference", "preference_id": pref.id,
                       "supplier_id": s.id, "supplier_name": s.name, **scope,
                       "message": f"Tercih #{pref.id}: {scope} için #{s.id} {s.name} preferred; firma phase_out olacağı için önce açıkça çözülmelidir."})
    return {"policy_id": POLICY_ID, "domain": DOMAIN, "ok": not errors,
            "firms": firms, "changes": sorted(changes, key=lambda c: c["supplier_id"]),
            "errors": errors, "changed_count": len(changes), "committed": False}


def apply(db):
    """Guard, tüm kartlar ve audit tek transaction; hata durumunda tam rollback."""
    if db.new or db.dirty or db.deleted:
        raise ValueError("Politika yalnız temiz, bu işleme ayrılmış Session ile uygulanır.")
    try:
        # Tercih ekleme ve firma yeniden adlandırma yarışı guard'dan sonra
        # yeni bir çelişki oluşturamasın. EXCLUSIVE, SELECT okumalarını açar;
        # yazma / SELECT FOR UPDATE işlemleri kısa transaction sonunu bekler.
        db.execute(text("SET LOCAL lock_timeout = '10s'"))
        db.execute(text("LOCK TABLE suppliers, material_supplier_prefs IN EXCLUSIVE MODE"))
        report = plan(db)
        if not report["ok"]:
            raise PolicyError(report)
        now = datetime.utcnow()
        for change in report["changes"]:
            supplier = db.get(Supplier, change["supplier_id"])
            for field, value in change["after"].items():
                setattr(supplier, field, value)
            supplier.status_at = now
            change["after"]["status_at"] = now.isoformat()
            db.add(AdminAuditLog(
                actor_name=ACTOR, action="supplier.status", target_type="supplier",
                target_id=supplier.id, target_name=supplier.name,
                details=json.dumps({"policy_id": POLICY_ID, "domain": DOMAIN,
                                    "firm": change["firm"], "reason": REASON,
                                    "before": change["before"], "after": change["after"]},
                                   ensure_ascii=False)))
        db.flush()
        db.commit()
        report["committed"] = True
        return report
    except Exception:
        db.rollback()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="Guard'lar geçerse politika + audit'i atomik yaz.")
    args = parser.parse_args(argv)
    db = SessionLocal()
    try:
        report = apply(db) if args.commit else plan(db)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 2
    except PolicyError as exc:
        print(json.dumps(exc.report, ensure_ascii=False, indent=2))
        return 2
    except Exception as exc:
        print(json.dumps({"policy_id": POLICY_ID, "committed": False,
                          "errors": [{"code": "transaction_failed", "message": str(exc)}]},
                         ensure_ascii=False, indent=2))
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
