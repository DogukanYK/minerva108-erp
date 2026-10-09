"""One-time denied-category backfill without replacing user overrides."""
import json

from database import AdminAuditLog, AppSetting, User

SENTINEL = "backfill.perm.outsourcing.v1"
ACTIONS = ("view", "manage", "mapping", "approve", "dispatch", "record")


def backfill_permissions(db):
    if db.query(AppSetting).filter_by(key=SENTINEL).first():
        return 0
    changed = 0
    for user in db.query(User).filter(User.permissions.isnot(None), User.role != "SuperAdmin").order_by(User.id):
        try:
            permissions = json.loads(user.permissions)
        except (TypeError, ValueError):
            continue
        if not isinstance(permissions, dict):
            continue
        category = permissions.get("outsourcing")
        if category is not None and not isinstance(category, dict):
            continue
        category = dict(category or {})
        added = {action: False for action in ACTIONS if action not in category}
        if not added:
            continue
        category.update(added)
        permissions["outsourcing"] = category
        user.permissions = json.dumps(permissions, ensure_ascii=False)
        db.add(AdminAuditLog(actor_name="sistem", action="permissions.backfill", target_type="user",
                             target_id=user.id, target_name=user.username,
                             details=json.dumps({"sentinel": SENTINEL, "keys": added})))
        changed += 1
    db.add(AppSetting(key=SENTINEL, value=str(changed)))
    db.commit()
    return changed


def grant_isik_approval(db, *, commit=False):
    user = db.query(User).filter_by(username="isik", is_active=True).with_for_update().first()
    if not user or user.role != "Manager":
        raise ValueError("Aktif Işık yönetici hesabı bulunamadı.")
    if user.permissions:
        permissions = json.loads(user.permissions)
        if not isinstance(permissions, dict):
            raise ValueError("Işık hesabının yetki JSON'u geçersiz.")
        category = permissions.get("outsourcing") or {}
        if not isinstance(category, dict):
            raise ValueError("Fason yetkileri geçersiz.")
        updated = dict(category)
        updated.update(view=True, approve=True)
        changed = updated != category
        result = {"username": user.username, "changed": changed, "commit": commit,
                  "grants": ["outsourcing.view", "outsourcing.approve"]}
        if commit and changed:
            permissions["outsourcing"] = updated
            user.permissions = json.dumps(permissions, ensure_ascii=False)
            db.add(AdminAuditLog(actor_name="fason yetki betiği", action="permissions.update", target_type="user",
                                 target_id=user.id, target_name=user.username, details=json.dumps(result)))
            db.commit()
        else:
            db.rollback()
        return result
    db.rollback()
    return {"username": user.username, "changed": False, "commit": commit,
            "grants": ["outsourcing.view", "outsourcing.approve"], "message": "Rol varsayılanı yeterli; override oluşturulmadı."}
