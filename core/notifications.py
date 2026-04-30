"""
Minerva108 — Notification dispatcher.

Single point through which the rest of the codebase fires alerts.
Today's backend is just structured logging (stdout → systemd journal); when
SMTP / Telegram / Slack land, swap the body of `_emit` and every call-site
keeps working unchanged.

Public API
──────────
  notify(level, category, message, **context)
      Generic sink. Use this for ad-hoc alerts that don't fit a named helper.

  notify_low_stock(item_name, current_stock, threshold, unit="")
      Fired when a stock-mutating operation drops an item to/below its
      configured min_stock_level.

  notify_expiry_summary(lots)
      Daily roll-up of inventory lots whose expiry date is within the watch
      window (default 30 days). Empty list → "all clear" info line.

Design notes
────────────
• No external dependencies — works in any FastAPI worker, scheduler thread,
  or one-shot script.
• All call-sites pass primitives (str/float/int/dict). NEVER pass live
  SQLAlchemy ORM objects: notifications are often delivered from a
  BackgroundTask whose request session has already closed.
• The logger name is "minerva108.alerts" so it can be routed to its own
  handler (file, Sentry, Loki…) without touching app logging defaults.
"""
import logging
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


# ─── Public API ─────────────────────────────────────────────────────────────

def notify(level: str, category: str, message: str, **context) -> None:
    """Generic notification sink — use named helpers below when one fits."""
    _emit(level, category, message, **context)


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
    per request. The notification module does not deduplicate — when the
    SMTP/Telegram backend lands we will add a sliding-window dedup there.
    """
    unit_part = f" {unit}" if unit else ""
    _emit(
        "warning",
        "LOW-STOCK",
        f"{item_name} kritik seviyede: "
        f"{current_stock}{unit_part} ≤ min {threshold}{unit_part}",
        item=item_name,
        current=current_stock,
        threshold=threshold,
        unit=unit or None,
    )


def notify_expiry_summary(lots: Iterable[dict]) -> None:
    """
    Daily roll-up. `lots` is a list of {lot_number, item_name, expiry_date,
    days_left} dicts already filtered to the watch window.

    Empty → emits an INFO "all clear" line so silence in the log doesn't
    look like a missed cron run.
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
