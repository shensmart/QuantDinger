"""Which symbol-tag families are due for a refresh right now.

Industry/concept constituents change once a day after the close; the quote
thresholds are live intraday. Rather than two beat entries with different
periods, one tick asks this module what is due, so the beat schedule stays a
single low-frequency entry and the decision is testable without Celery.

"Did sectors already run today" is read back from the tags themselves
(``updated_at``), not from process memory: the beat, the worker, and a manual
admin sync are three different processes and must agree.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time

from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(str(os.getenv(name) or "").strip() or default))
    except (TypeError, ValueError):
        return default


def sector_refresh_time() -> time:
    return time(
        min(23, _int_env("SYMBOL_TAG_SECTOR_HOUR", 15)),
        min(59, _int_env("SYMBOL_TAG_SECTOR_MINUTE", 40)),
    )


def quote_tick_seconds() -> int:
    return max(60, _int_env("SYMBOL_TAG_QUOTE_TICK_SEC", 300))


def _quote_window(moment: time) -> bool:
    """Quote tags only matter while A-shares are trading (plus the close gap)."""
    return (9, 25) <= (moment.hour, moment.minute) <= (15, 60)


def _is_working_day(day: date) -> bool:
    """Trading-day check that tolerates a stale upstream calendar.

    HiThink serves a rolling one-year calendar that lags the current session, so
    ``is_trading_day(today)`` is False on a perfectly normal trading morning and
    the quote refresh would never run. A weekday after the calendar's last entry
    is therefore provisionally treated as trading; the refresh itself is cheap
    and the upstream snapshot is the real source of truth.
    """
    from app.data_sources import hithink_finance as hithink

    if hithink.is_trading_day(day):
        return True
    if day.weekday() >= 5:
        return False
    try:
        known = hithink.trading_days()
    except Exception as exc:  # noqa: BLE001 - a calendar outage must not stop the tick
        logger.warning("Trading calendar unavailable (%s); assuming a weekday is tradeable", exc)
        return True
    if not known:
        return True
    return day > max(known)


def last_sector_refresh_day() -> date | None:
    """Date of the newest industry/concept tag update, None when never synced."""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT MAX(updated_at) AS last_at
            FROM qd_symbol_tags
            WHERE category IN ('industry', 'concept')
            """
        )
        row = cur.fetchone() or {}
        cur.close()
    value = row.get("last_at")
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def due_categories(now=None, *, last_run: date | None = None) -> list[str]:
    """Materialized categories to refresh for this tick.

    Returns ``[]`` outside the quote window when sectors are already done, so
    the beat task exits without touching upstream.
    """
    from app.data_sources import hithink_finance as hithink

    moment = now or hithink.shanghai_now()
    today = moment.date()
    if not _is_working_day(today):
        return []
    due: list[str] = []
    reached = moment.time() >= sector_refresh_time()
    seen = last_sector_refresh_day() if last_run is None else last_run
    if reached and seen != today:
        due.extend(["industry", "concept"])
    if _quote_window(moment.time()):
        due.append("quote")
    return due


__all__ = [
    "due_categories",
    "last_sector_refresh_day",
    "quote_tick_seconds",
    "sector_refresh_time",
]