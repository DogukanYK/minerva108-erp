# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Minerva108 — Notification dispatcher.

Single point through which the rest of the codebase fires alerts. Two
backends run in parallel for every alert:

  1. Structured log line (always — permanent audit trail in systemd journal).
  2. Web Push to relevant subscribers (only if VAPID env vars are configured).

If VAPID isn't configured (dev environment, missing env vars), push is
silently skipped — logs still flow, nothing breaks.

Public API
──────────
  notify(level, category, message, **context)
      Generic sink. Logs only — no push dispatch (use named helpers below
      to ensure audience targeting is consistent).

  notify_low_stock(item_name, current_stock, threshold, unit="")
      Fired when a stock-mutating operation drops an item to/below its
      configured min_stock_level. Pushes to managers (SuperAdmin + Manager).

  notify_expiry_summary(lots)
      Daily roll-up of inventory lots whose expiry date is within the watch
      window (default 30 days). Empty list → "all clear" info line.
      Pushes to managers when ≥1 lot in window.

Design notes
────────────
• Log emit has no external deps — works even when pywebpush is not installed.
• All call-sites pass primitives. NEVER pass live SQLAlchemy ORM objects:
  notifications fire from BackgroundTasks after the request session closes.
• Push delivery opens its own short-lived DB session, fans out, and prunes
  any subscription endpoints that respond 404/410 (browser unsubscribed).
• Browser-side `tag` field collapses duplicate notifications (e.g. the same
  item triggering low-stock 5 times in a row → user sees one).
"""
import json
import logging
import os
from typing import Iterable, Optional

logger = logging.getLogger("minerva108.alerts")

# Configure once at import time so the module is self-sufficient. If the
# host application later attaches its own handlers, propagation still works.
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] [ALERT-%(alert_category)s] %(message)s"
    ))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _emit(level: str, category: str, message: str, **context) -> None:
    """
    Backend dispatch. Currently logs only.

    To add SMTP/Telegram/Slack: branch here on `level` or `category`
    and call the relevant transport. Keep the log line — it's the
    permanent audit trail.
    """
    if context:
        ctx_str = " ".join(f"{k}={v}" for k, v in context.items() if v is not None)
        full = f"{message} · {ctx_str}" if ctx_str else message
    else:
        full = message
    log_fn = getattr(logger, level.lower(), logger.info)
    log_fn(full, extra={"alert_category": category})


# ─── Push delivery ──────────────────────────────────────────────────────────

# Audience → list of role names. Permission-based targeting is a future
# upgrade; today we use roles because they're the immediate user mental model
# and match the existing RBAC defaults (Manager/SuperAdmin = decision-makers).
_AUDIENCE_ROLES = {
    "managers": ["SuperAdmin", "Manager"],
    "lab":      ["SuperAdmin", "LabLead", "LabTech"],
    "all":      ["SuperAdmin", "Manager", "LabLead", "LabTech", "Staff"],
}


def _vapid_config():
    """Returns (private_key, subject) or None if either env var is unset."""
    priv    = os.getenv("VAPID_PRIVATE_KEY")
    subject = os.getenv("VAPID_SUBJECT", "mailto:dev@minerva108.com")
    if not priv:
        return None
    return (priv, subject)


def _send_push(audience: str, payload: dict) -> None:
    """
    Fan-out a push payload to all active subscriptions of users in the given
    audience. Silently no-ops if VAPID isn't configured. Prunes dead
    subscription endpoints (404/410) so the table self-cleans over time.

    `payload` is a dict matching what the SW's push handler expects:
      { title, body, tag, url, icon?, requireInteraction? }
    """
    cfg = _vapid_config()
    if cfg is None:
        return                      # dev / unconfigured → log-only path

    priv_key, subject = cfg

    # Lazy imports — pywebpush is heavy and only needed when push is actually
    # configured. Keeps `core.notifications` importable in minimal envs.
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.warning("pywebpush not installed — install it to enable push delivery")
        return

    from database import SessionLocal, PushSubscription, User

    roles = _AUDIENCE_ROLES.get(audience, _AUDIENCE_ROLES["managers"])
    db = SessionLocal()
    try:
        subs = (
            db.query(PushSubscription)
            .join(User, User.id == PushSubscription.user_id)
            .filter(User.role.in_(roles), User.is_active == True)
            .all()
        )
        if not subs:
            return

        sent, dead_ids = 0, []
        body_json = json.dumps(payload, ensure_ascii=False)

        for sub in subs:
            try:
                webpush(
                    subscription_info={
                        "endpoint": sub.endpoint,
                        "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                    },
                    data=body_json,
                    vapid_private_key=priv_key,
                    vapid_claims={"sub": subject},
                    ttl=86400,                       # 24h — push service holds while device offline
                )
                sent += 1
            except WebPushException as e:
                status = e.response.status_code if e.response is not None else None
                if status in (404, 410):
                    # Subscription expired / unsubscribed — schedule prune.
                    dead_ids.append(sub.id)
                logger.warning("Push send failed (status=%s) for sub#%s", status, sub.id)
            except Exception as e:
                logger.warning("Push send error for sub#%s: %s", sub.id, e)

        if dead_ids:
            db.query(PushSubscription).filter(
                PushSubscription.id.in_(dead_ids)
            ).delete(synchronize_session=False)
            db.commit()
            logger.info("Pruned %d dead push subscriptions", len(dead_ids))

        logger.info(
            "Push delivered to %d/%d subscriptions [audience=%s]",
            sent, len(subs), audience,
        )
    except Exception:
        logger.exception("Push fan-out failed (logging path still succeeded)")
    finally:
        db.close()


# ─── Public API ─────────────────────────────────────────────────────────────

def notify(level: str, category: str, message: str, **context) -> None:
    """Generic notification sink — log only, no push dispatch."""
    _emit(level, category, message, **context)


def notify_proforma_pending(document_no: str, recipient: str, preparer: str) -> None:
    """Proforma oluşturuldu → SuperAdmin/Manager onayına düştü."""
    _emit("info", "PROFORMA",
          f"Proforma onay bekliyor: {document_no} ({recipient}) — hazırlayan {preparer}",
          document_no=document_no, recipient=recipient, preparer=preparer)
    _send_push("managers", {
        "title": "🧾 Proforma Onayı Bekliyor",
        "body": f"{document_no} · {recipient} · hazırlayan {preparer}",
        "tag": f"proforma-{document_no}",
        "url": "/delivery",
        "requireInteraction": True,
    })


def notify_return_created(document_no: str, source: str,
                          restocked_count: int, damaged_count: int, actor: str) -> None:
    """Ürün iadesi alındı — sağlamlar stoğa döndü, hasarlılar fire izi olarak kaydedildi."""
    _emit("info", "IADE",
          f"İade alındı: {document_no} ← {source} — {restocked_count} kalem stoğa, "
          f"{damaged_count} kalem hasarlı/fire (alan: {actor})",
          document_no=document_no, source=source,
          restocked=restocked_count, damaged=damaged_count, actor=actor)
    _send_push("managers", {
        "title": "↩️ Ürün İadesi Alındı",
        "body": f"{document_no} ← {source} · {restocked_count} stoğa, {damaged_count} fire",
        "tag": f"return-{document_no}",
        "url": "/returns",
    })


def notify_proforma_decision(document_no: str, decision: str, by: str) -> None:
    """Proforma onaylandı / reddedildi → karar bildirimi (yönetim + denetim izi)."""
    label = "onaylandı" if decision == "approved" else "reddedildi"
    _emit("info", "PROFORMA", f"Proforma {label}: {document_no} — {by}",
          document_no=document_no, decision=decision, by=by)
    _send_push("managers", {
        "title": "🧾 Proforma " + ("Onaylandı ✓" if decision == "approved" else "Reddedildi ✗"),
        "body": f"{document_no} · {by}",
        "tag": f"proforma-{document_no}",
        "url": "/delivery",
    })


def notify_low_stock(
    item_name: str,
    current_stock: float,
    threshold: float,
    unit: str = "",
) -> None:
    """
    Fired when a stock-mutating operation (production, sale confirmation,
    item edit) leaves an item at or below its min_stock_level.

    Idempotency: callers are expected to fire this AT MOST ONCE per item
    per request. Browser-side dedup happens via the `tag` field — same
    item firing repeatedly collapses to a single visible notification.
    """
    unit_part = f" {unit}" if unit else ""
    _emit(
        "warning",
        "LOW-STOCK",
        f"{item_name} kritik seviyede: "
        f"{current_stock}{unit_part} ≤ min {threshold}{unit_part}",
        item=item_name, current=current_stock, threshold=threshold, unit=unit or None,
    )
    _send_push("managers", {
        "title": "🔔 Kritik Stok Uyarısı",
        "body":  f"{item_name}: {current_stock}{unit_part} (min {threshold}{unit_part})",
        "tag":   f"low-stock-{item_name}",       # collapse duplicates per-item
        "url":   "/items",
        "requireInteraction": False,
    })


def notify_new_distributor_order(order_no: str, company_name: str, total: float, currency: str) -> None:
    """Distribütör portaldan yeni sipariş verdi → yöneticilere onay bildirimi."""
    _emit("info", "DISTRIBUTOR-ORDER",
          f"Yeni distribütör siparişi: {order_no} · {company_name} · {total} {currency}",
          order_no=order_no, company=company_name, total=total, currency=currency)
    _send_push("managers", {
        "title": "📦 Yeni Distribütör Siparişi",
        "body":  f"{order_no} · {company_name} · {total} {currency}",
        "tag":   f"dist-order-{order_no}",
        "url":   "/quotations",
        "requireInteraction": True,
    })


def notify_expiry_summary(lots: Iterable[dict]) -> None:
    """
    Daily roll-up. `lots` is a list of {lot_number, item_name, expiry_date,
    days_left} dicts already filtered to the watch window.

    Empty → emits an INFO "all clear" line and skips the push (no need to
    notify managers when there's nothing to act on).
    """
    lot_list = list(lots)
    n = len(lot_list)
    if n == 0:
        _emit(
            "info",
            "EXPIRY-SCAN",
            "Önümüzdeki 30 gün içinde son kullanım tarihi yaklaşan lot yok.",
            lot_count=0,
        )
        return

    head = lot_list[:20]
    tail = n - len(head)
    detail = "; ".join(
        f"{l.get('lot_number')}({l.get('item_name')}, "
        f"{l.get('days_left')}gün)"
        for l in head
    )
    if tail > 0:
        detail += f" (… +{tail} lot daha)"

    _emit(
        "warning",
        "EXPIRY-SCAN",
        f"{n} lot 30 gün içinde son kullanım tarihine ulaşıyor: {detail}",
        lot_count=n,
    )
    _send_push("managers", {
        "title": "⏰ Son Kullanım Tarihi Yaklaşıyor",
        "body":  f"{n} lot 30 gün içinde son kullanım tarihine ulaşıyor. Detay için izlenebilirlik sayfasına bakın.",
        "tag":   "expiry-summary",                # one notification, regenerated daily
        "url":   "/traceability",
        "requireInteraction": True,                # important — keep on screen until dismissed
    })


def _send_push_to_users(user_ids: Iterable[int], payload: dict) -> None:
    """Push'u BELİRLİ kullanıcıların abonelerine gönder (role audience değil).
    CRM hatırlatmaları görevin atandığı kişiye gider.  VAPID yoksa no-op."""
    cfg = _vapid_config()
    if cfg is None:
        return
    ids = [int(u) for u in user_ids if u]
    if not ids:
        return
    priv_key, subject = cfg
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        logger.warning("pywebpush not installed — install it to enable push delivery")
        return
    from database import SessionLocal, PushSubscription

    db = SessionLocal()
    try:
        subs = db.query(PushSubscription).filter(PushSubscription.user_id.in_(ids)).all()
        if not subs:
            return
        body_json = json.dumps(payload, ensure_ascii=False)
        dead_ids = []
        for sub in subs:
            try:
                webpush(
                    subscription_info={"endpoint": sub.endpoint,
                                       "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                    data=body_json, vapid_private_key=priv_key,
                    vapid_claims={"sub": subject}, ttl=86400,
                )
            except WebPushException as e:
                status = e.response.status_code if e.response is not None else None
                if status in (404, 410):
                    dead_ids.append(sub.id)
            except Exception as e:
                logger.warning("CRM push send error for sub#%s: %s", sub.id, e)
        if dead_ids:
            db.query(PushSubscription).filter(
                PushSubscription.id.in_(dead_ids)).delete(synchronize_session=False)
            db.commit()
    except Exception:
        logger.exception("CRM push fan-out failed")
    finally:
        db.close()


def notify_crm_reminder(user_id: int, count: int, sample_titles: Iterable[str]) -> None:
    """CRM görev hatırlatması — atanan kişiye, vadesi gelen/geçen açık görevleri
    için tek bir özet push.  Günlük scheduler taramasından çağrılır."""
    titles = list(sample_titles)[:3]
    sample = "; ".join(titles) + (" …" if count > len(titles) else "")
    _emit("info", "CRM-REMINDER",
          f"Kullanıcı#{user_id}: {count} CRM görevi vadesi geldi/geçti · {sample}",
          user_id=user_id, count=count)
    _send_push_to_users([user_id], {
        "title": "📋 CRM Görev Hatırlatması",
        "body":  (f"{count} görevin var: {sample}" if count > 1
                  else f"Görev: {sample}"),
        "tag":   f"crm-reminder-{user_id}",        # kişi başına tek bildirim, günlük yenilenir
        "url":   "/crm",
        "requireInteraction": False,
    })


def notify_crm_task_assigned(user_id: int, task_title: str, due_label, assigner: str) -> None:
    """CRM görev ataması — atanan kişiye ANINDA push (günlük taramayı beklemez).
    BackgroundTasks'ten İLKEL argümanlarla çağır (ORM nesnesi geçirme — üstteki
    tasarım notu).  Kendi kendine atamada çağıran taraf zaten atlamalı."""
    _emit("info", "CRM-ASSIGN",
          f"Kullanıcı#{user_id}: '{task_title}' görevi atandı ({assigner})",
          user_id=user_id)
    _send_push_to_users([user_id], {
        "title": "📋 Yeni CRM Görevi",
        "body":  f"{assigner} sana görev atadı: {task_title}"
                 + (f" · Son tarih: {due_label}" if due_label else ""),
        "tag":   f"crm-task-assign-{user_id}",
        "url":   "/crm",
        "requireInteraction": False,
    })
