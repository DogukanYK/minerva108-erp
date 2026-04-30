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

from database import SessionLocal, Inventory, Item
from core.notifications import notify_expiry_summary

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

    scheduler.start()
    logger.info(
        "APScheduler started · jobs=%s",
        [j.id for j in scheduler.get_jobs()],
    )


def stop_scheduler() -> None:
    """Graceful shutdown — called from FastAPI's shutdown hook."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")
