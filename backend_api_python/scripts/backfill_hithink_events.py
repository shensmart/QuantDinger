"""Backfill HiThink board events (limit pools, dragon-tiger, hot list).

Upstream keeps one year of board history, so the window is capped at
``--days 365``. Re-runs are idempotent (unique key upsert) and ``--resume``
skips dates that already have rows, which is what you want after a rate-limit
abort.

Usage:
    python scripts/backfill_hithink_events.py --dry-run --days 10
    python scripts/backfill_hithink_events.py --days 365 --resume
    python scripts/backfill_hithink_events.py --only hot_rank --only anomaly
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta

ROOT = "/app"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.services import events_data  # noqa: E402
from app.services.hithink_events_sync import (  # noqa: E402
    UNIVERSE_POOLS,
    refresh_universe_pools,
)
from app.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)


def _target_dates(days: int, only_dates: list[str] | None = None) -> list[date]:
    from app.data_sources import hithink_finance as hithink

    if only_dates:
        parsed = [hithink.parse_trade_date(item) for item in only_dates]
        return sorted({item for item in parsed if item})
    calendar = hithink.trading_days()
    if not calendar:
        raise RuntimeError("trading calendar unavailable; cannot backfill")
    cutoff = calendar[-1] - timedelta(days=max(1, days))
    return [day for day in calendar if day >= cutoff]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill HiThink market events")
    parser.add_argument("--days", type=int, default=365, help="lookback window (max 365)")
    parser.add_argument("--date", action="append", default=[], help="explicit trade date (repeatable)")
    parser.add_argument("--only", action="append", default=[], help="event type (repeatable)")
    parser.add_argument("--limit-days", type=int, default=None, help="only process the newest N dates (smoke runs)")
    parser.add_argument("--resume", action="store_true", help="skip (date, event type) pairs already stored")
    parser.add_argument("--dry-run", action="store_true", help="fetch without writing")
    parser.add_argument("--skip-universes", action="store_true", help="do not refresh system pools")
    args = parser.parse_args(argv)

    event_types = [str(item).strip().lower() for item in args.only if str(item).strip()] or list(
        events_data.HISTORICAL_EVENT_TYPES
    )
    unknown = sorted(set(event_types) - set(events_data.EVENT_TYPES))
    if unknown:
        print(json.dumps({"error": f"unsupported event types: {unknown}"}, ensure_ascii=False))
        return 2

    dates = _target_dates(min(365, max(1, args.days)), args.date)
    if args.limit_days:
        dates = dates[: max(1, args.limit_days)]
    if not dates:
        print(json.dumps({"error": "no target trading dates"}, ensure_ascii=False))
        return 2

    # Existing coverage tells --resume which dates to skip entirely; the UNIQUE
    # key makes re-fetching an already-stored date harmless, just slower.
    coverage = {} if args.dry_run else events_data.coverage_by_date(event_types)
    processed: list[dict] = []
    failures: list[dict] = []
    for index, day in enumerate(dates, start=1):
        if args.resume and set(event_types) <= coverage.get(day, set()):
            continue
        try:
            report = events_data.sync_date(day, event_types, dry_run=bool(args.dry_run))
        except Exception as exc:  # noqa: BLE001 - keep going, report at the end
            logger.exception("backfill failed date=%s", day)
            failures.append({"trade_date": day.isoformat(), "error": str(exc)[:200]})
            continue
        processed.append(report)
        failures.extend({"trade_date": day.isoformat(), **item} for item in report["failures"])
        if index % 10 == 0:
            logger.info("backfill progress %s/%s", index, len(dates))

    universes: dict = {"skipped": True}
    if not args.dry_run and not args.skip_universes and processed:
        universes = refresh_universe_pools(dates[-1])

    summary = {
        "dryRun": bool(args.dry_run),
        "eventTypes": event_types,
        "dates": len(dates),
        "datesWritten": len(processed),
        "rowsWritten": sum(item["written"] for item in processed),
        "failures": len(failures),
        "failureSample": failures[:20],
        "universes": universes,
        "universePools": [item[0] for item in UNIVERSE_POOLS],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 1 if failures and not processed else 0


if __name__ == "__main__":
    raise SystemExit(main())
