# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Admin audit logger — append-only kayıt yardımcısı.

Stok hareketleri zaten `Transaction` tablosuna düşüyor.  Bu modül onların
dışındaki *hesap/yetki güvenliği* olaylarını `admin_audit_log` tablosuna
yazar:

  • user.create / user.update / user.deactivate / user.delete
  • user.password_reset
  • permissions.update / permissions.reset

Kullanımı:

  from core.audit import log_admin_event
  log_admin_event(db, request, actor=current_user,
                  action="user.password_reset",
                  target_type="user", target_id=user.id,
                  target_name=user.username,
                  details={"reset_at": "..."})

İmmutable: bu modül sadece INSERT yapar, hiçbir yer silmez/güncellemez.
"""
import json as _json
from typing import Optional

from sqlalchemy.orm import Session

from database import AdminAuditLog


def log_admin_event(
    db: Session,
    request,                       # FastAPI Request — IP/User-Agent için
    actor: dict,                   # core.auth get_current_user payload
    action: str,
    target_type: Optional[str] = None,
    target_id:   Optional[int] = None,
    target_name: Optional[str] = None,
    details:     Optional[dict] = None,
) -> None:
    """Log eklenip session'a commit'lenir. Hata olursa sessizce yutar
       (ana akış zaten kritik) — ama log'un bozulması iş kırmaz."""
    try:
        ip      = (request.client.host if request and request.client else None) or "—"
        ua      = (request.headers.get("user-agent", "") if request else "")[:255]
        actor_id   = int(actor.get("sub", 0)) if actor else None
        actor_name = (actor.get("full_name") or actor.get("username") or "—") if actor else "—"

        db.add(AdminAuditLog(
            actor_id     = actor_id,
            actor_name   = actor_name[:100],
            action       = action[:50],
            target_type  = (target_type or None) and target_type[:50],
            target_id    = target_id,
            target_name  = (target_name or None) and target_name[:150],
            details      = _json.dumps(details, ensure_ascii=False)[:8000] if details else None,
            ip_address   = ip[:64],
            user_agent   = ua,
        ))
        db.commit()
    except Exception:
        # Audit'in patlaması akışı durdurmasın — ama hatayı stderr'e bas
        import sys, traceback
        traceback.print_exc(file=sys.stderr)
        try:
            db.rollback()
        except Exception:
            pass
