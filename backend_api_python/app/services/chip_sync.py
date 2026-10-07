"""Batch chip-distribution (筹码分布) collection.

Why this exists
---------------
Eastmoney's ``push2his`` endpoints — the ones akshare's ``stock_cyq_em`` uses —
reset the connection for this deployment's egress, so 筹码 data could never be
collected from there.  The distribution is not served by Eastmoney anyway: the
server only returns daily OHLC + turnover and the browser computes the rest.
So we compute it locally (``chip_distribution.py``) from a source that works.

Design
------
* Resumable job + item rows, so a crash never loses progress and a re-run only
  touches pending/failed symbols.
* One symbol at a time, gated by the global HTTP throttle installed in
  ``rate_limiter.install_http_throttle`` — no per-symbol sleeping here.
* External IO never holds a DB connection (same rule as ``fundamental_sync``).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.services.chip_distribution import compute_series
from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

SOURCE = "computed_sina_daily"
SOURCE_VERSION = "cyq-v1"
DEFAULT_LOOKBACK_DAYS = 260
# The CYQ window upstream uses is 120 bars; keep a buffer for the recursion.
MIN_BARS = 120
# Eastmoney's own view (and akshare's request) is built from 210 daily bars.
# Cap the recursion there so our numbers match what users see on the site.
CYQ_WINDOW = int(os.getenv("CHIP_CYQ_WINDOW", "210") or 210)
DEFAULT_BATCH_SIZE = int(os.getenv("CHIP_SYNC_BATCH_SIZE", "50") or 50)
# Attempts before a transient failure is recorded as permanent.
MAX_ATTEMPTS = int(os.getenv("CHIP_SYNC_MAX_ATTEMPTS", "3") or 3)
# Backoff before a retryable symbol is tried again.
RETRY_BACKOFF_SEC = int(os.getenv("CHIP_SYNC_RETRY_BACKOFF_SEC", "300") or 300)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _query(sql: str, params: Sequence[Any] = (), many: bool = False):
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            cur.execute(sql, params)
            rows = None
            if sql.lstrip().upper().startswith("SELECT") or "RETURNING" in sql.upper():
                rows = cur.fetchall() if many else cur.fetchone()
            db.commit()
            return rows
        finally:
            cur.close()


def _row_get(row: Any, key: str, index: int = 0):
    if row is None:
        return None
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        try:
            return row[index]
        except Exception:
            return None


def _ak_cn_symbol(symbol: str) -> str:
    """Convert a project symbol to the ``sz000009`` form AkShare's Sina API wants."""
    from app.data_sources.tencent import normalize_cn_code

    code = normalize_cn_code(symbol)
    return code.lower() if code else ""


def fetch_daily_bars(symbol: str, lookback_days: int) -> List[Dict[str, Any]]:
    """Fetch adjusted daily bars for one A-share from Sina via AkShare.

    Returns bars oldest→newest with ``turnover`` as a fraction.  Raises on
    failure so the caller can record the error against the item row.
    """
    import akshare as ak  # type: ignore

    ak_symbol = _ak_cn_symbol(symbol)
    if not ak_symbol:
        raise ValueError("unsupported symbol: %r" % (symbol,))

    today = date.today()
    # Calendar days >> trading days; ~1.6x covers weekends and holidays.
    start = today - timedelta(days=int(lookback_days * 1.6) + 30)
    try:
        df = ak.stock_zh_a_daily(
            symbol=ak_symbol,
            start_date=start.strftime("%Y%m%d"),
            end_date=today.strftime("%Y%m%d"),
            adjust="qfq",
        )
    except Exception as exc:
        # Delisted / suspended tickers make Sina answer with an empty body,
        # which surfaces as a JSON decode error.  Treat it as "no data" so the
        # caller reports a clean reason instead of a parser traceback.
        name = type(exc).__name__
        if name in {"JSONDecodeError", "ValueError", "ConnectionError", "RemoteDisconnected"}:
            logger.debug("No daily bars for %s (%s): %s", symbol, ak_symbol, exc)
            return []
        raise
    if df is None or getattr(df, "empty", True):
        return []

    bars: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        try:
            turnover = row.get("turnover")
            bars.append(
                {
                    "date": str(row["date"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "turnover": float(turnover) if turnover is not None else 0.0,
                }
            )
        except (TypeError, ValueError, KeyError):
            continue
    return bars


def _available_at(trade_date: Any) -> datetime:
    """First instant a close-of-day snapshot is knowable: 15:00 Asia/Shanghai."""
    if isinstance(trade_date, str):
        try:
            trade_date = datetime.strptime(trade_date[:10], "%Y-%m-%d").date()
        except ValueError:
            trade_date = date.today()
    if not isinstance(trade_date, date):
        trade_date = date.today()
    # +08:00 == Asia/Shanghai; market closes 15:00 local.
    return datetime(
        trade_date.year, trade_date.month, trade_date.day, 15, 0, tzinfo=timezone(timedelta(hours=8))
    )


def store_series(
    market: str,
    symbol: str,
    series: Sequence[Dict[str, Any]],
    *,
    source: str = SOURCE,
    bars_used: int = 0,
) -> int:
    """Upsert computed rows.  Returns the number of rows written."""
    if not series:
        return 0
    rows = [
        (
            market,
            symbol,
            item["date"],
            _available_at(item["date"]),
            item.get("close"),
            item["benefit_ratio"],
            item["avg_cost"],
            item["cost90_low"],
            item["cost90_high"],
            item["concentration90"],
            item["cost70_low"],
            item["cost70_high"],
            item["concentration70"],
            source,
            SOURCE_VERSION,
            bars_used,
            '{"window": %d}' % CYQ_WINDOW,
        )
        for item in series
    ]
    sql = """
        INSERT INTO qd_chip_distribution (
            market, symbol, trade_date, available_at, close_price,
            benefit_ratio, avg_cost,
            cost90_low, cost90_high, concentration90,
            cost70_low, cost70_high, concentration70,
            source, source_version, bars_used, metadata_json
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (market, symbol, trade_date, source) DO UPDATE SET
            available_at = EXCLUDED.available_at,
            close_price = EXCLUDED.close_price,
            benefit_ratio = EXCLUDED.benefit_ratio,
            avg_cost = EXCLUDED.avg_cost,
            cost90_low = EXCLUDED.cost90_low,
            cost90_high = EXCLUDED.cost90_high,
            concentration90 = EXCLUDED.concentration90,
            cost70_low = EXCLUDED.cost70_low,
            cost70_high = EXCLUDED.cost70_high,
            concentration70 = EXCLUDED.concentration70,
            source_version = EXCLUDED.source_version,
            bars_used = EXCLUDED.bars_used,
            metadata_json = EXCLUDED.metadata_json,
            ingested_at = NOW()
    """
    written = 0
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            for row in rows:
                cur.execute(sql, row)
                written += 1
            db.commit()
            return written
        finally:
            cur.close()


def sync_symbol(market: str, symbol: str, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> int:
    """Fetch, compute, store one symbol.  Returns rows written."""
    bars = fetch_daily_bars(symbol, lookback_days)
    if len(bars) < MIN_BARS:
        # Delisted and newly-listed tickers legitimately fall here; report it
        # as a skip rather than a hard failure so batch stats stay readable.
        raise ValueError(
            "insufficient bars (%d < %d) — delisted, suspended, or newly listed"
            % (len(bars), MIN_BARS)
        )
    series = compute_series(bars, CYQ_WINDOW)
    if not series:
        raise ValueError("no distribution computed")
    return store_series(market, symbol, series, bars_used=len(bars))


# ---------------------------------------------------------------------------
# Job orchestration
# ---------------------------------------------------------------------------


def start_job(
    market: str = "CNStock",
    *,
    limit: int = 0,
    symbols: Optional[Iterable[str]] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> int:
    """Create a resumable job.  Returns the new job id."""
    if symbols is not None:
        wanted = [(market, s.strip()) for s in symbols if s and s.strip()]
    else:
        rows = _query(
            "SELECT market, symbol FROM qd_market_symbols "
            "WHERE market = %s AND is_active = 1 ORDER BY sort_order, symbol",
            (market,),
            many=True,
        ) or []
        wanted = [(str(_row_get(r, "market", 0)), str(_row_get(r, "symbol", 1))) for r in rows]
        if limit:
            wanted = wanted[:limit]

    if not wanted:
        raise ValueError("no symbols for market %s" % market)

    job_id = _row_get(
        _query(
            "INSERT INTO qd_chip_sync_jobs (market, symbols_total, lookback_days) "
            "VALUES (%s, %s, %s) RETURNING id",
            (market, len(wanted), lookback_days),
        ),
        "id",
        0,
    )
    with get_db_connection() as db:
        cur = db.cursor()
        try:
            for mkt, sym in wanted:
                cur.execute(
                    "INSERT INTO qd_chip_sync_items (job_id, market, symbol) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (job_id, mkt, sym),
                )
            db.commit()
        finally:
            cur.close()
    logger.info("Chip sync job %s created with %d symbols", job_id, len(wanted))
    return int(job_id)


def claim_item(job_id: int) -> Optional[Dict[str, Any]]:
    """Atomically claim the next item that is due for a try.

    ``retry_at`` is honoured so a symbol that keeps failing backs off instead
    of being retried on the very next claim (which would starve the rest of
    the batch).
    """
    row = _query(
        """
        UPDATE qd_chip_sync_items SET status = 'running', attempts = attempts + 1,
               updated_at = NOW()
        WHERE id = (
            SELECT id FROM qd_chip_sync_items
            WHERE job_id = %s AND status = 'pending' AND retry_at <= NOW()
              AND attempts < %s
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, market, symbol, attempts
        """,
        (job_id, MAX_ATTEMPTS),
    )
    if not row:
        return None
    return {
        "id": int(_row_get(row, "id", 0)),
        "market": str(_row_get(row, "market", 1)),
        "symbol": str(_row_get(row, "symbol", 2)),
        "attempts": int(_row_get(row, "attempts", 3) or 0),
    }


def finish_item(
    item_id: int,
    rows_written: int,
    error: str = "",
    *,
    retryable: bool = False,
    attempts: int = 0,
) -> None:
    """Record an item outcome.

    A retryable error stays ``pending`` with a backoff so the rest of the batch
    gets a turn; once attempts are exhausted it becomes ``failed`` so the job
    can reach a terminal state.
    """
    if error:
        exhausted = attempts >= MAX_ATTEMPTS
        status = "pending" if (retryable and not exhausted) else "failed"
    else:
        status = "success"
    _query(
        "UPDATE qd_chip_sync_items SET status = %s, rows_written = %s, error = %s, "
        "retry_at = CASE WHEN %s THEN NOW() + (%s * INTERVAL '1 second') ELSE retry_at END, "
        "updated_at = NOW() WHERE id = %s",
        (status, int(rows_written), (error or "")[:300], status == "pending", RETRY_BACKOFF_SEC, item_id),
    )


def run_batch(job_id: int, batch_size: int = DEFAULT_BATCH_SIZE) -> Dict[str, int]:
    """Process up to ``batch_size`` symbols for a job.  Never raises per symbol."""
    lookback = int(
        _row_get(
            _query("SELECT lookback_days FROM qd_chip_sync_jobs WHERE id = %s", (job_id,)),
            "lookback_days",
            0,
        )
        or DEFAULT_LOOKBACK_DAYS
    )
    processed = succeeded = failed = rows_total = 0

    for _ in range(max(1, batch_size)):
        item = claim_item(job_id)
        if item is None:
            break
        processed += 1
        try:
            written = sync_symbol(item["market"], item["symbol"], lookback)
            finish_item(item["id"], written)
            succeeded += 1
            rows_total += written
        except Exception as exc:
            # Delisted/suspended symbols raise ValueError; those never succeed,
            # so don't burn retries on them.
            retryable = not isinstance(exc, ValueError)
            finish_item(
                item["id"],
                0,
                "%s: %s" % (type(exc).__name__, exc),
                retryable=retryable,
                attempts=item["attempts"],
            )
            failed += 1
            logger.debug(
                "Chip sync failed %s/%s (attempt %d): %s",
                item["market"], item["symbol"], item["attempts"], exc,
            )

    _refresh_job_counters(job_id)
    return {
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
        "rows_written": rows_total,
    }


def _refresh_job_counters(job_id: int) -> None:
    _query(
        """
        UPDATE qd_chip_sync_jobs j SET
            symbols_done = agg.done_count,
            symbols_failed = agg.failed_count,
            status = CASE
                WHEN EXISTS (
                    SELECT 1 FROM qd_chip_sync_items i
                    WHERE i.job_id = j.id AND i.status IN ('pending', 'running')
                ) THEN 'running'
                ELSE 'complete'
            END,
            updated_at = NOW()
        FROM (
            SELECT
                COUNT(*) FILTER (WHERE status = 'success') AS done_count,
                COUNT(*) FILTER (WHERE status = 'failed') AS failed_count
            FROM qd_chip_sync_items WHERE job_id = %s
        ) agg
        WHERE j.id = %s
        """,
        (job_id, job_id),
    )


def job_status(job_id: int) -> Optional[Dict[str, Any]]:
    job = _query("SELECT * FROM qd_chip_sync_jobs WHERE id = %s", (job_id,))
    if not job:
        return None
    out = {
        "id": int(_row_get(job, "id", 0)),
        "market": _row_get(job, "market", 1),
        "status": _row_get(job, "status", 2),
        "symbols_total": int(_row_get(job, "symbols_total", 3) or 0),
        "symbols_done": int(_row_get(job, "symbols_done", 4) or 0),
        "symbols_failed": int(_row_get(job, "symbols_failed", 5) or 0),
    }
    sample = _query(
        "SELECT symbol, status, rows_written, error FROM qd_chip_sync_items "
        "WHERE job_id = %s AND status = 'failed' ORDER BY id LIMIT 5",
        (job_id,),
        many=True,
    ) or []
    out["failures_sample"] = [
        {
            "symbol": _row_get(r, "symbol", 0),
            "status": _row_get(r, "status", 1),
            "rows": _row_get(r, "rows_written", 2),
            "error": _row_get(r, "error", 3),
        }
        for r in sample
    ]
    return out


def latest_for(market: str, symbol: str, limit: int = 30) -> List[Dict[str, Any]]:
    """Most recent chip rows for one symbol (diagnostics / API use)."""
    rows = _query(
        "SELECT trade_date, close_price, benefit_ratio, avg_cost, "
        "cost90_low, cost90_high, concentration90, "
        "cost70_low, cost70_high, concentration70, source, available_at "
        "FROM qd_chip_distribution WHERE market = %s AND symbol = %s "
        "ORDER BY trade_date DESC LIMIT %s",
        (market, symbol, int(limit)),
        many=True,
    ) or []
    keys = [
        "trade_date", "close_price", "benefit_ratio", "avg_cost",
        "cost90_low", "cost90_high", "concentration90",
        "cost70_low", "cost70_high", "concentration70", "source", "available_at",
    ]
    return [{k: _row_get(r, k, i) for i, k in enumerate(keys)} for r in rows]


if __name__ == "__main__":  # pragma: no cover - operator entry point
    import argparse

    parser = argparse.ArgumentParser(description="Batch chip-distribution sync")
    parser.add_argument("--market", default="CNStock")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--symbols", default="", help="comma-separated, overrides --limit")
    parser.add_argument("--job", type=int, default=0, help="continue an existing job")
    parser.add_argument("--status", type=int, default=0, help="print job status and exit")
    parser.add_argument("--probe", default="", help="sync one symbol now and print rows")
    args = parser.parse_args()

    from app.data_sources.rate_limiter import install_http_throttle

    install_http_throttle()

    if args.status:
        print(job_status(args.status))
    elif args.probe:
        n = sync_symbol(args.market, args.probe)
        print("wrote %d rows for %s" % (n, args.probe))
        for row in latest_for(args.market, args.probe, 3):
            print(row)
    else:
        job = args.job or start_job(
            args.market,
            limit=args.limit,
            symbols=args.symbols.split(",") if args.symbols else None,
        )
        print("job", job, run_batch(job, args.batch_size))
        print(job_status(job))
