"""HiThink board synchronization: daily incremental, backfill, and universe refresh.

The scheduler calls :func:`schedule_due` on a short beat; it only dispatches the
real sync once per trading day after the configured close time, so restarting
the beat container cannot double-write (the event table's unique key would make
a double write harmless anyway).
"""

from __future__ import annotations

import os
import threading
import time
from datetime import date, timedelta

from app.utils.logger import get_logger

logger = get_logger(__name__)

_thread: threading.Thread | None = None

# System universes rebuilt from the boards. (code prefix, name, event type,
# member cap) — the boards are daily snapshots, so each carries snapshot_only
# metadata and backtest readiness rejects dates before the snapshot.
UNIVERSE_POOLS: tuple[tuple[str, str, str, int], ...] = (
    ("hithink_limit_up", "HiThink Limit-Up Pool", "limit_up", 300),
    ("hithink_limit_up_ladder", "HiThink Limit-Up Ladder", "limit_up_ladder", 200),
    ("hithink_dragon_tiger", "HiThink Dragon-Tiger List", "dragon_tiger_all", 300),
    ("hithink_hot_rank", "HiThink Hot Rank", "hot_rank", 100),
)


def _int_env(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def sync_enabled() -> bool:
    raw = str(os.getenv("HITHINK_EVENTS_SYNC_ENABLED") or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _sync_hhmm() -> tuple[int, int]:
    return (
        max(0, min(23, _int_env("HITHINK_EVENTS_SYNC_HOUR", 15))),
        max(0, min(59, _int_env("HITHINK_EVENTS_SYNC_MINUTE", 10))),
    )


def backfill_days() -> int:
    return max(1, min(365, _int_env("HITHINK_EVENTS_BACKFILL_DAYS", 365)))


def last_synced_trade_date() -> date | None:
    from app.services import events_data

    dates = events_data.latest_trade_dates(("limit_up",))
    return dates.get("limit_up")


def due_now(now=None) -> bool:
    """True when today's boards are already published and not yet stored."""
    from app.data_sources import hithink_finance as hithink

    moment = now or hithink.shanghai_now()
    hour, minute = _sync_hhmm()
    if (moment.hour, moment.minute) < (hour, minute):
        return False
    today = moment.date()
    if not hithink.is_trading_day(today):
        return False
    return last_synced_trade_date() != today


def schedule_due() -> dict:
    """Beat entry point: dispatch the sync at most once per trading day."""
    if not sync_enabled():
        return {"skipped": "disabled"}
    try:
        if not due_now():
            return {"skipped": "not_due"}
    except Exception as exc:  # noqa: BLE001 - calendar/network hiccup must not crash beat
        logger.warning("HiThink event schedule check failed: %s", exc)
        return {"skipped": "check_failed"}
    _dispatch()
    return {"dispatched": True}


def _dispatch() -> None:
    """Queue the sync on Celery; fall back to inline when no broker is reachable."""
    try:
        from app.tasks.hithink_events import hithink_events_sync

        hithink_events_sync.delay()
        return
    except Exception as exc:  # noqa: BLE001 - beat/worker may be offline
        logger.warning("Celery dispatch unavailable (%s); running event sync inline", exc)
    run_sync(
        event_types=_default_event_types(),
        trigger_type="inline",
    )


def _default_event_types() -> list[str]:
    from app.services import events_data

    return list(events_data.HISTORICAL_EVENT_TYPES) + ["limit_up_ladder", "anomaly", "skyrocket"]


def _heartbeat_worker() -> None:
    import threading

    from app.data_sources import hithink_finance as hithink

    interval = max(300, _int_env("HITHINK_EVENTS_SYNC_TICK_SEC", 600))
    while True:
        try:
            now = hithink.shanghai_now()
            if due_now(now):
                run_sync(event_types=_default_event_types(), trigger_type="scheduler")
        except Exception as exc:  # noqa: BLE001 - never kill the loop
            logger.warning("HiThink event sync tick failed: %s", exc)
        time.sleep(interval)


def start_event_sync_worker() -> None:
    """Start the in-process daily sync heartbeat (idempotent)."""
    global _thread
    if not sync_enabled():
        return
    if _thread is not None and _thread.is_alive():
        return
    _thread = threading.Thread(target=_heartbeat_worker, name="hithink-events-sync", daemon=True)
    _thread.start()
    logger.info("HiThink event sync worker started")


def run_sync(*, event_types, trigger_type: str = "schedule", as_of: date | None = None) -> dict:
    """Fetch today's boards, persist them, then refresh the system universes."""
    from app.data_sources import hithink_finance as hithink
    from app.services import events_data

    if not hithink.enabled() or not sync_enabled():
        return {"skipped": "disabled"}

    day = as_of or hithink.shanghai_today()
    run_id = events_data.start_run(
        trigger_type=trigger_type,
        event_types=event_types,
        start=day,
        end=day,
    )
    try:
        report = events_data.sync_date(day, event_types)
    except Exception as exc:  # noqa: BLE001
        events_data.finish_run(run_id, status="failed", failures=1, detail={"error": str(exc)[:300]})
        raise
    events_data.finish_run(
        run_id,
        status="succeeded" if not report["failures"] else "partial",
        rows_written=report["written"],
        failures=len(report["failures"]),
        detail=report,
    )
    pools = refresh_universe_pools(day) if not report["failures"] else {"skipped": "sync_failures"}
    return {**report, "universes": pools}


def refresh_universe_pools(day: date, *, dry_run: bool = False) -> dict:
    """Rebuild the four system universes from the stored boards.

    ponytail: members are replaced wholesale, so ``history_from`` always equals
    the newest snapshot. That is the honest semantic for a daily board snapshot
    (``snapshot_only``); per-date point-in-time membership would need a
    membership history table that only `get_events` actually needs.
    """
    from app.services.universe import UniverseService
    from app.services import events_data

    # daily boards are the source of the pool universes
    service = UniverseService()
    results: list[dict] = []
    failures: list[dict] = []
    start = _pool_history_from(day)
    for code, name, event_type, cap in UNIVERSE_POOLS:
        try:
            page = events_data.query_events(
                event_types=[event_type],
                trade_date=day,
                limit=cap,
            )
            members = [
                {
                    "market": str(item.get("market") or events_data.MARKET),
                    "symbol": str(item.get("symbol") or ""),
                    "name": str(item.get("name") or ""),
                    "rank": item.get("rank"),
                    "metadata": {
                        "source": events_data.SOURCE,
                        "event_type": event_type,
                        "trade_date": day.isoformat(),
                    },
                }
                for item in page["items"]
                if item.get("symbol")
            ]
            if dry_run:
                results.append({"code": code, "members": len(members)})
                continue
            results.append(
                service.upsert_system_universe(
                    code=code,
                    name=name,
                    members=members,
                    source=events_data.SOURCE,
                    source_ref=f"hithink:{event_type}",
                    valid_from=start,
                    metadata={
                        "source": events_data.SOURCE,
                        "event_type": event_type,
                        "snapshot_only": True,
                        "snapshot_as_of": start.isoformat(),
                        "snapshot_latest": day.isoformat(),
                        "member_count": len(members),
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("universe pool refresh failed code=%s", code)
            failures.append({"code": code, "error": str(exc)[:200]})
    return {"results": results, "failures": failures}


def _pool_history_from(day: date) -> date:
    """Start of the stored board history, so recent backtests stay usable."""
    earliest = _earliest_event_date()
    floor = day - timedelta(days=backfill_days())
    return max(floor, earliest) if earliest is not None else floor


def _earliest_event_date() -> date | None:
    from app.utils.db import get_db_connection

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute("SELECT MIN(trade_date) AS first_date FROM qd_market_events")
        row = cur.fetchone() or {}
        cur.close()
    return row.get("first_date")


__all__ = [
    "UNIVERSE_POOLS",
    "sync_enabled",
    "backfill_days",
    "due_now",
    "schedule_due",
    "run_sync",
    "refresh_universe_pools",
    "start_event_sync_worker",
]
