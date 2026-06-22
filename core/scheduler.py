# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Minerva108 — APScheduler wiring for daily/weekly/hourly automated jobs.

A single AsyncIOScheduler instance lives at module scope so the rest of the
app can introspect or add jobs. `start_scheduler()` is idempotent — safe to
call from FastAPI's startup hook even on uvicorn auto-reload.

Job catalogue
─────────────
  daily_expiry_scan — 09:00 every day. Walks APPROVED inventory looking
                       for lots whose expiry_date falls inside the next
                       30 days; emits a single summary alert through
                       core.notifications.

Design notes
────────────
• Each scheduled function MUST open and close its own DB session. Job
  callbacks run in scheduler-managed contexts that are completely
  separate from FastAPI's request-scoped sessions.
• `max_instances=1` on every job: if a previous run is still going at the
  next fire time, the new one is skipped (better than queueing and piling
  up under load).
• Log a single INFO line on start so we can confirm the cron jobs were
  registered after a deploy by tailing the journal.
"""
from datetime import datetime, timedelta
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database import SessionLocal, Inventory, Item, CrmTask, TR_OFFSET
from core.notifications import notify_expiry_summary, notify_crm_reminder
from core.snapshots import capture_previous_month, backfill_missing_snapshots

logger = logging.getLogger("minerva108.scheduler")

# Module-scope singleton. AsyncIOScheduler attaches itself to the running
# event loop the first time `start()` is called, so we deliberately do NOT
# instantiate it inside start_scheduler() — that would re-create it on
# uvicorn auto-reload and leak schedulers.
scheduler = AsyncIOScheduler()

# Window the daily scan looks ahead. Single source of truth so the API
# endpoint (/api/traceability/expiring) and the cron job stay in sync if
# we tune it later — currently the API uses 60 days, the alert uses 30.
EXPIRY_WATCH_DAYS = 30


def scan_expiring_lots() -> None:
    """
    Daily job: find APPROVED inventory lots expiring within EXPIRY_WATCH_DAYS
    and dispatch a single summary alert. Robust against malformed
    expiry_date strings (skipped silently) — never crashes the scheduler.
    """
    db = SessionLocal()
    try:
        today = datetime.utcnow().date()
        rows = (
            db.query(Inventory)
            .filter(
                Inventory.expiry_date.isnot(None),
                Inventory.expiry_date != "",
                Inventory.status == "APPROVED",
            )
            .all()
        )

        expiring: list[dict] = []
        for r in rows:
            try:
                exp = datetime.strptime(r.expiry_date, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                # Legacy / malformed expiry strings — log once if we want to
                # but don't pollute the alert summary with noise.
                continue
            days_left = (exp - today).days
            if 0 <= days_left <= EXPIRY_WATCH_DAYS:
                item = db.query(Item).filter(Item.id == r.item_id).first()
                expiring.append({
                    "lot_number":  r.lot_number,
                    "item_name":   item.name if item else "—",
                    "expiry_date": r.expiry_date,
                    "days_left":   days_left,
                })

        expiring.sort(key=lambda x: x["days_left"])
        notify_expiry_summary(expiring)
    except Exception:
        # Catch-all: a scheduler exception kills the next fire, so log and swallow.
        logger.exception("daily_expiry_scan failed")
    finally:
        db.close()


def daily_crm_followup_scan() -> None:
    """
    Günlük job (08:00): vadesi BUGÜN dolan veya geçmiş, henüz hatırlatılmamış
    açık CRM görevlerini bul, atanan kişi başına tek özet push gönder, ve
    reminder_sent=True ile işaretle (tekrarlı bildirimi önle).
    """
    db = SessionLocal()
    try:
        # Bugünün (TR) gün sonunun UTC karşılığı
        tr_today = (datetime.utcnow() + TR_OFFSET).date()
        tr_end = datetime(tr_today.year, tr_today.month, tr_today.day, 23, 59, 59)
        due_cutoff = tr_end - TR_OFFSET

        rows = (db.query(CrmTask).filter(
            CrmTask.status == "open",
            CrmTask.reminder_sent == False,          # noqa: E712
            CrmTask.due_at.isnot(None),
            CrmTask.due_at <= due_cutoff,
            CrmTask.assigned_to_user_id.isnot(None),
        ).all())

        by_user: dict[int, list] = {}
        for t in rows:
            by_user.setdefault(t.assigned_to_user_id, []).append(t)

        for uid, tasks in by_user.items():
            try:
                notify_crm_reminder(uid, len(tasks), [t.title for t in tasks])
            except Exception:
                logger.exception("notify_crm_reminder failed for user %s", uid)
            for t in tasks:
                t.reminder_sent = True
        db.commit()
        if by_user:
            logger.info("daily_crm_followup_scan: %d kullanıcıya hatırlatma", len(by_user))
    except Exception:
        logger.exception("daily_crm_followup_scan failed")
    finally:
        db.close()


def monthly_stock_snapshot() -> None:
    """
    Aylık job: her ayın 1'i 00:30'da bir önceki ayın stok durumunu
    `stock_snapshot` tablosuna dondurur.  Ayrıca eksik kalmış geçmiş
    ayları da telafi eder (restart / kaçırılan ay senaryosu).
    """
    db = SessionLocal()
    try:
        capture_previous_month(db)
        backfill_missing_snapshots(db)   # kaçırılan eski aylar varsa telafi
    except Exception:
        logger.exception("monthly_stock_snapshot failed")
    finally:
        db.close()


def monthly_system_report() -> None:
    """
    Aylık job: her ayın 1'i 01:00'de bir önceki ayın detaylı sistem raporunu
    (PDF + Excel) üretip system_reports/ klasörüne kaydeder.  Stok snapshot
    job'undan (00:30) sonra çalışır ki ay sonu verileri hazır olsun.
    """
    db = SessionLocal()
    try:
        from core.monthly_report import save_monthly_report
        now = datetime.utcnow()
        if now.month == 1:
            year, month = now.year - 1, 12
        else:
            year, month = now.year, now.month - 1
        written = save_monthly_report(db, year, month)
        logger.info("monthly_system_report tamam: %s", written)
    except Exception:
        logger.exception("monthly_system_report failed")
    finally:
        db.close()


def start_scheduler() -> None:
    """Register all cron jobs and start the scheduler. Idempotent."""
    if scheduler.running:
        return

    scheduler.add_job(
        scan_expiring_lots,
        CronTrigger(hour=9, minute=0),       # 09:00 server-local time, every day
        id="daily_expiry_scan",
        replace_existing=True,
        max_instances=1,
        coalesce=True,                       # if missed (sleep/restart), run ONE catch-up not all
    )

    scheduler.add_job(
        daily_crm_followup_scan,
        CronTrigger(hour=8, minute=0),       # 08:00 her gün — kullanıcılar gelmeden önce
        id="daily_crm_followup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        monthly_stock_snapshot,
        CronTrigger(day=1, hour=0, minute=30),   # her ayın 1'i 00:30
        id="monthly_stock_snapshot",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        monthly_system_report,
        CronTrigger(day=1, hour=1, minute=0),    # her ayın 1'i 01:00 (snapshot'tan sonra)
        id="monthly_system_report",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    logger.info(
        "APScheduler started · jobs=%s",
        [j.id for j in scheduler.get_jobs()],
    )

    # Açılışta eksik geçmiş ay snapshot'larını telafi et (idempotent —
    # var olanı atlar, yalnızca eksikleri doldurur).
    try:
        _db = SessionLocal()
        try:
            filled = backfill_missing_snapshots(_db)
            if filled:
                logger.info("Startup snapshot backfill tamam: %s", filled)
        finally:
            _db.close()
    except Exception:
        logger.exception("startup snapshot backfill failed")


def stop_scheduler() -> None:
    """Graceful shutdown — called from FastAPI's shutdown hook."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")
