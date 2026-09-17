"""Point-in-time market event stream (HiThink special data).

One storage table backs every board type: limit-up/down/break pools, the
limit-up ladder, dragon-tiger lists, the THS hot list and per-stock anomalies.

Two consumers share this module:

* the strategy runtime (``get_events``) reads only events that were already
  published at the simulated bar date — ``available_at`` is always the trade
  date's post-close instant;
* the API/agent layer queries the same tables for display.

Nothing here talks to the network on the read path; fetching lives in
``fetch_events``/``sync_dates`` and is driven by the sync task and the backfill
script.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

MARKET = "CNStock"
SOURCE = "hithink_finance"

# Board types that can be fetched for an arbitrary trading day (within the
# one-year upstream retention) — these are the ones the backfill can replay.
HISTORICAL_EVENT_TYPES = (
    "limit_up",
    "limit_down",
    "limit_break",
    "dragon_tiger_all",
    "dragon_tiger_org",
    "dragon_tiger_hot_money",
    "hot_rank",
)
# Same-day-only endpoints (no date parameter upstream).
SNAPSHOT_EVENT_TYPES = (
    "limit_up_ladder",
    "anomaly",
    "skyrocket",
)
EVENT_TYPES = HISTORICAL_EVENT_TYPES + SNAPSHOT_EVENT_TYPES

# Columns exposed to strategies/API: booleans/flags as 0/1, numerics as value.
EVENT_VALUE_FIELDS: dict[str, tuple[str, ...]] = {
    "limit_up": ("limit_up", "limit_up_days", "seal_money", "limit_up_ratio"),
    "limit_down": ("limit_down", "limit_down_ratio"),
    "limit_break": ("limit_break", "limit_break_times"),
    "limit_up_ladder": ("limit_up_ladder", "ladder_board"),
    "dragon_tiger_all": ("dragon_tiger", "dragon_tiger_net", "dragon_tiger_net_rate"),
    "dragon_tiger_org": ("dragon_tiger_org", "dragon_tiger_org_net"),
    "dragon_tiger_hot_money": ("dragon_tiger_hot_money", "dragon_tiger_hot_money_net"),
    "hot_rank": ("hot_rank", "hot_heat", "hot_rank_change"),
    "anomaly": ("anomaly",),
    "skyrocket": ("skyrocket",),
}

_CLOSE_HOUR = 15


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _int(value: Any) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def available_at(trade_date: date) -> datetime:
    """Publication instant for a board: trade date 15:00 Asia/Shanghai.

    Boards are only final after the close, and a backtest must not be able to
    read them intraday.
    """
    shanghai = timezone(timedelta(hours=8))
    moment = datetime(
        trade_date.year, trade_date.month, trade_date.day, _CLOSE_HOUR, 0, tzinfo=shanghai
    )
    return moment.astimezone(timezone.utc)


def _row(
    *,
    symbol: str,
    event_type: str,
    trade_date: date,
    name: str = "",
    payload: Mapping[str, Any] | None = None,
    rank: int | None = None,
    score: float | None = None,
) -> dict[str, Any]:
    return {
        "market": MARKET,
        "symbol": symbol,
        "name": name,
        "event_type": event_type,
        "trade_date": trade_date,
        "available_at": available_at(trade_date),
        "rank": rank,
        "score": score,
        "payload": dict(payload or {}),
    }


def _symbol_from_thscode(value: Any) -> str:
    from app.data_sources.hithink_finance import to_project_symbol

    return to_project_symbol(str(value or ""))


# ---------------------------------------------------------------------------
# Fetch + normalize (network path)
# ---------------------------------------------------------------------------

def fetch_events(event_type: str, trade_date: date | None = None) -> list[dict[str, Any]]:
    """Fetch one board type and normalize it into storage rows."""
    from app.data_sources import hithink_finance as hithink

    kind = str(event_type or "").strip().lower()
    if kind not in EVENT_TYPES:
        raise ValueError(f"unsupported event type: {event_type}")
    day = trade_date or hithink.shanghai_today()

    if kind == "limit_up":
        rows = []
        for item in hithink.limit_up_pool(day, sort_field="continue_day_cnt", sort_dir="desc"):
            symbol = _symbol_from_thscode(item.get("thscode"))
            if not symbol:
                continue
            rows.append(
                _row(
                    symbol=symbol,
                    event_type=kind,
                    trade_date=day,
                    name=str(item.get("name") or ""),
                    rank=_int(item.get("continue_day_cnt")),
                    score=_float(item.get("seal_money")),
                    payload=item,
                )
            )
        return rows

    if kind == "limit_down":
        rows = []
        for item in hithink.limit_down_pool(day):
            symbol = _symbol_from_thscode(item.get("thscode"))
            if not symbol:
                continue
            rows.append(
                _row(
                    symbol=symbol,
                    event_type=kind,
                    trade_date=day,
                    name=str(item.get("name") or ""),
                    score=_float(item.get("turnover_ratio_pct")),
                    payload=item,
                )
            )
        return rows

    if kind == "limit_break":
        rows = []
        for item in hithink.limit_break_pool(day):
            symbol = _symbol_from_thscode(item.get("thscode"))
            if not symbol:
                continue
            rows.append(
                _row(
                    symbol=symbol,
                    event_type=kind,
                    trade_date=day,
                    name=str(item.get("name") or ""),
                    score=_float(item.get("open_times")),
                    payload=item,
                )
            )
        return rows

    if kind == "limit_up_ladder":
        data = hithink.limit_up_ladder()
        wanted = hithink.parse_trade_date(day)
        rows = []
        for block in data.get("item") or []:
            if not isinstance(block, dict):
                continue
            block_date = hithink.parse_trade_date(block.get("date"))
            if block_date is None or (trade_date is not None and block_date != wanted):
                continue
            boards = block.get("boards") if isinstance(block.get("boards"), dict) else {}
            for board_name, entries in boards.items():
                for item in entries or []:
                    if not isinstance(item, dict):
                        continue
                    symbol = _symbol_from_thscode(item.get("thscode"))
                    if not symbol:
                        continue
                    rows.append(
                        _row(
                            symbol=symbol,
                            event_type=kind,
                            trade_date=block_date,
                            name=str(item.get("name") or ""),
                            rank=_int(item.get("board_num")),
                            score=_float(item.get("board_num")),
                            payload={"board": board_name, **item},
                        )
                    )
        return rows

    if kind.startswith("dragon_tiger_"):
        board_type = kind[len("dragon_tiger_"):]
        data = hithink.dragon_tiger(board_type, day)
        actual = hithink.parse_trade_date(data.get("trade_date")) or day
        rows = []
        if board_type == "hot_money":
            # Per-stock rows, merging every 游资 that traded the stock.
            merged: dict[str, dict[str, Any]] = {}
            for item in data.get("hot_money_items") or []:
                if not isinstance(item, dict):
                    continue
                trader = str(item.get("name") or "")
                for stock in item.get("rows") or []:
                    if not isinstance(stock, dict):
                        continue
                    symbol = _symbol_from_thscode(stock.get("thscode"))
                    if not symbol:
                        continue
                    entry = merged.setdefault(
                        symbol,
                        _row(
                            symbol=symbol,
                            event_type=kind,
                            trade_date=actual,
                            name=str(stock.get("name") or ""),
                            payload={**stock, "hot_money_names": []},
                        ),
                    )
                    entry["payload"]["hot_money_names"].append(trader)
                    net = _float(stock.get("hot_money_item_net_value"))
                    if net is not None:
                        entry["score"] = (entry["score"] or 0.0) + net
            rows.extend(merged.values())
            return rows

        for item in data.get("stock_items") or []:
            if not isinstance(item, dict):
                continue
            symbol = _symbol_from_thscode(item.get("thscode"))
            if not symbol:
                continue
            rows.append(
                _row(
                    symbol=symbol,
                    event_type=kind,
                    trade_date=actual,
                    name=str(item.get("name") or ""),
                    rank=_int(item.get("hot_rank")),
                    score=_float(item.get("net_value")),
                    payload=item,
                )
            )
        return rows

    if kind == "hot_rank":
        items = hithink.hot_stock_list("day")
        if trade_date is not None and trade_date < hithink.shanghai_today():
            items = hithink.hot_list_history(trade_date)
        rows = []
        for item in items:
            symbol = _symbol_from_thscode(item.get("thscode"))
            if not symbol:
                continue
            rows.append(
                _row(
                    symbol=symbol,
                    event_type=kind,
                    trade_date=day,
                    name=str(item.get("name") or ""),
                    rank=_int(item.get("rank")),
                    score=_float(item.get("heat")),
                    payload=item,
                )
            )
        return rows

    if kind == "anomaly":
        rows = []
        for item in hithink.anomaly_list():
            symbol = _symbol_from_thscode(item.get("thscode"))
            if not symbol:
                continue
            rows.append(
                _row(
                    symbol=symbol,
                    event_type=kind,
                    trade_date=day,
                    name=str(item.get("stock_name") or ""),
                    payload=item,
                )
            )
        return rows

    if kind == "skyrocket":
        rows = []
        for item in hithink.skyrocket_list("day"):
            symbol = _symbol_from_thscode(item.get("thscode"))
            if not symbol:
                continue
            rows.append(
                _row(
                    symbol=symbol,
                    event_type=kind,
                    trade_date=day,
                    name=str(item.get("name") or ""),
                    rank=_int(item.get("rank")),
                    score=_float(item.get("heat")),
                    payload=item,
                )
            )
        return rows

    raise ValueError(f"unsupported event type: {event_type}")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def ensure_schema() -> None:
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS qd_market_events (
                id BIGSERIAL PRIMARY KEY,
                market VARCHAR(32) NOT NULL,
                symbol VARCHAR(80) NOT NULL,
                event_type VARCHAR(40) NOT NULL,
                trade_date DATE NOT NULL,
                available_at TIMESTAMPTZ NOT NULL,
                name VARCHAR(120) NOT NULL DEFAULT '',
                rank INTEGER,
                score DOUBLE PRECISION,
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                source VARCHAR(40) NOT NULL DEFAULT 'hithink_finance',
                source_version VARCHAR(40) NOT NULL DEFAULT '',
                ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (market, symbol, event_type, trade_date, source)
            )
            """
        )
        db.commit()
        cur.close()


def upsert_events(rows: Sequence[Mapping[str, Any]]) -> int:
    """Idempotent write; the unique key makes re-runs safe."""
    if not rows:
        return 0
    import json

    ensure_schema()
    written = 0
    with get_db_connection() as db:
        cur = db.cursor()
        for row in rows:
            cur.execute(
                """
                INSERT INTO qd_market_events
                  (market, symbol, event_type, trade_date, available_at, name,
                   rank, score, payload_json, source, source_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?, ?)
                ON CONFLICT (market, symbol, event_type, trade_date, source) DO UPDATE SET
                  available_at = EXCLUDED.available_at,
                  name = EXCLUDED.name,
                  rank = EXCLUDED.rank,
                  score = EXCLUDED.score,
                  payload_json = EXCLUDED.payload_json,
                  source_version = EXCLUDED.source_version,
                  ingested_at = NOW()
                """,
                (
                    str(row.get("market") or MARKET),
                    str(row.get("symbol") or ""),
                    str(row.get("event_type") or ""),
                    row.get("trade_date"),
                    row.get("available_at") or available_at(row["trade_date"]),
                    str(row.get("name") or "")[:120],
                    row.get("rank"),
                    row.get("score"),
                    json.dumps(row.get("payload") or {}, ensure_ascii=False, default=str),
                    str(row.get("source") or SOURCE),
                    str(row.get("source_version") or ""),
                ),
            )
            written += 1
        db.commit()
        cur.close()
    return written


def start_run(*, trigger_type: str, event_types: Sequence[str], start: date, end: date) -> int:
    import json

    ensure_schema()
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO qd_market_event_sync_runs
              (trigger_type, source, status, event_types, trade_date_start, trade_date_end)
            VALUES (?, ?, 'running', ?::jsonb, ?, ?)
            RETURNING id
            """,
            (str(trigger_type or "manual"), SOURCE, json.dumps(list(event_types)), start, end),
        )
        row = cur.fetchone() or {}
        db.commit()
        cur.close()
    return int(row.get("id") or 0)


def finish_run(run_id: int, *, status: str, rows_written: int = 0, failures: int = 0, detail: Mapping[str, Any] | None = None) -> None:
    import json

    if not run_id:
        return
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            UPDATE qd_market_event_sync_runs
            SET status = ?, rows_written = ?, failures = ?, detail_json = ?::jsonb,
                finished_at = NOW()
            WHERE id = ?
            """,
            (
                str(status),
                int(rows_written),
                int(failures),
                json.dumps(dict(detail or {}), ensure_ascii=False, default=str),
                int(run_id),
            ),
        )
        db.commit()
        cur.close()


def sync_date(
    trade_date: date,
    event_types: Sequence[str],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fetch and persist every requested board for one date."""
    from app.data_sources.hithink_finance import HiThinkError

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for event_type in event_types:
        try:
            rows = fetch_events(event_type, trade_date)
            if dry_run:
                results.append({"event_type": event_type, "rows": len(rows), "written": 0})
                continue
            written = upsert_events(rows)
            results.append({"event_type": event_type, "rows": len(rows), "written": written})
        except HiThinkError as exc:
            logger.warning("event sync failed type=%s date=%s: %s", event_type, trade_date, exc)
            failures.append({"event_type": event_type, "error": str(exc)[:200]})
        except Exception as exc:  # noqa: BLE001 - surface every provider failure
            logger.exception("event sync crashed type=%s date=%s", event_type, trade_date)
            failures.append({"event_type": event_type, "error": str(exc)[:200]})
    return {
        "trade_date": trade_date.isoformat(),
        "results": results,
        "failures": failures,
        "written": sum(item["written"] for item in results),
    }


# ---------------------------------------------------------------------------
# Point-in-time reads (strategy runtime)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _event_columns() -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(sorted(EVENT_VALUE_FIELDS.items()))


def load_points(
    market: str,
    symbol: str,
    event_types: Sequence[str],
    end: Any,
) -> list[dict[str, Any]]:
    """All events for one symbol up to ``end``, oldest first, as dicts."""
    wanted = [str(item).strip().lower() for item in event_types if str(item).strip()]
    if not symbol or not wanted:
        return []
    cutoff = pd.Timestamp(end)
    cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT symbol, event_type, trade_date, available_at, rank, score, payload_json
            FROM qd_market_events
            WHERE market = ? AND symbol = ? AND event_type = ANY(?) AND available_at <= ?
            ORDER BY trade_date, event_type
            """,
            (str(market), str(symbol).upper(), wanted, cutoff.to_pydatetime()),
        )
        rows = cur.fetchall() or []
        cur.close()
    return [dict(row) for row in rows]


def event_value(event_type: str, payload: Mapping[str, Any], rank: Any, score: Any) -> dict[str, float]:
    """Derive the numeric columns a strategy reads for one event row."""
    kind = str(event_type or "").lower()
    def num(value: Any) -> float:
        parsed = _float(value)
        return float(parsed) if parsed is not None else 0.0

    if kind == "limit_up":
        return {
            "limit_up": 1.0,
            "limit_up_days": num(payload.get("continue_day_cnt")) or num(rank),
            "seal_money": num(payload.get("seal_money")) if payload.get("seal_money") is not None else num(score),
            "limit_up_ratio": num(payload.get("price_change_ratio_pct")),
        }
    if kind == "limit_down":
        return {"limit_down": 1.0, "limit_down_ratio": num(payload.get("price_change_ratio_pct"))}
    if kind == "limit_break":
        return {"limit_break": 1.0, "limit_break_times": num(payload.get("open_times"))}
    if kind == "limit_up_ladder":
        return {"limit_up_ladder": 1.0, "ladder_board": num(payload.get("board_num")) or num(rank)}
    if kind == "dragon_tiger_all":
        return {
            "dragon_tiger": 1.0,
            "dragon_tiger_net": num(payload.get("net_value")),
            "dragon_tiger_net_rate": num(payload.get("net_rate")),
        }
    if kind == "dragon_tiger_org":
        return {"dragon_tiger_org": 1.0, "dragon_tiger_org_net": num(payload.get("org_net_value"))}
    if kind == "dragon_tiger_hot_money":
        return {
            "dragon_tiger_hot_money": 1.0,
            "dragon_tiger_hot_money_net": num(payload.get("hot_money_item_net_value")) or num(score),
        }
    if kind == "hot_rank":
        return {
            "hot_rank": num(rank) if rank is not None else num(payload.get("rank")),
            "hot_heat": num(payload.get("heat")) or num(score),
            "hot_rank_change": num(payload.get("rank_change")),
        }
    if kind == "anomaly":
        return {"anomaly": 1.0}
    if kind == "skyrocket":
        return {"skyrocket": 1.0}
    return {}


def enrich_frame(*, market: str, symbol: str, frame: pd.DataFrame) -> pd.DataFrame:
    """Attach event columns to a price frame using point-in-time visibility."""
    if frame.empty or not market or not symbol:
        return frame
    try:
        rows = load_points(market, symbol, EVENT_TYPES, frame.index.max())
    except Exception as exc:  # noqa: BLE001 - enrichment must never break a backtest
        logger.warning("event point-in-time load failed %s:%s: %s", market, symbol, exc)
        return frame
    if not rows:
        return frame

    dates = pd.DatetimeIndex(pd.to_datetime(frame.index, utc=True)).tz_localize(None).normalize()
    records: list[dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload_json")
        if isinstance(payload, str):
            import json

            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        # Keyed on the trade date, not the raw publication instant: the daily bar
        # dated D closes at 15:00 Asia/Shanghai, and orders built from it fill on
        # D+1's open. Intraday visibility stays enforced by load_points.
        trade_date = row.get("trade_date")
        stamp = pd.Timestamp(trade_date) if trade_date is not None else pd.Timestamp(row.get("available_at"))
        records.append(
            {
                "available_at": stamp.normalize(),
                **event_value(str(row.get("event_type") or ""), payload, row.get("rank"), row.get("score")),
            }
        )
    observations = pd.DataFrame(records)
    if observations.empty:
        return frame
    observations = observations.groupby("available_at", as_index=True).max()
    enriched = frame.copy()
    for _event_type, columns in _event_columns():
        for column in columns:
            source = observations[column] if column in observations.columns else pd.Series(index=observations.index, dtype=float)
            filled = pd.to_numeric(source, errors="coerce").reindex(dates, method="ffill")
            enriched[column] = filled.fillna(0.0).to_numpy()
    return enriched


def enrich_panel(frames: Mapping[str, pd.DataFrame], members: list[dict]) -> dict[str, pd.DataFrame]:
    identities: dict[str, tuple[str, str]] = {}
    for item in members:
        symbol = str(item.get("symbol") or "").upper()
        key = str(item.get("key") or "")
        identity = (str(item.get("market") or MARKET), symbol)
        identities[symbol] = identity
        if key:
            identities[key] = identity
    output: dict[str, pd.DataFrame] = {}
    for key, frame in frames.items():
        market, symbol = identities.get(key, identities.get(str(key).upper(), (MARKET, str(key).upper())))
        output[key] = enrich_frame(market=market, symbol=symbol, frame=frame)
    return output


# ---------------------------------------------------------------------------
# Query path (API / agent tools)
# ---------------------------------------------------------------------------

def query_events(
    *,
    market: str = MARKET,
    event_types: Sequence[str] | None = None,
    symbols: Sequence[str] | None = None,
    trade_date: date | None = None,
    start: date | None = None,
    end: date | None = None,
    limit: int = 200,
    offset: int = 0,
    as_of: Any = None,
) -> dict[str, Any]:
    """Read events for display. Never used by backtests (no implicit cut-off)."""
    clauses = ["market = ?"]
    params: list[Any] = [str(market or MARKET)]
    wanted = [str(item).strip().lower() for item in (event_types or []) if str(item).strip()]
    if wanted:
        clauses.append("event_type = ANY(?)")
        params.append(wanted)
    clean_symbols = [str(item).strip().upper() for item in (symbols or []) if str(item).strip()]
    if clean_symbols:
        clauses.append("symbol = ANY(?)")
        params.append(clean_symbols)
    if trade_date is not None:
        clauses.append("trade_date = ?")
        params.append(trade_date)
    else:
        if start is not None:
            clauses.append("trade_date >= ?")
            params.append(start)
        if end is not None:
            clauses.append("trade_date <= ?")
            params.append(end)
    if as_of is not None:
        stamp = pd.Timestamp(as_of)
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        clauses.append("available_at <= ?")
        params.append(stamp.to_pydatetime())

    where = " AND ".join(clauses)
    capped = max(1, min(1000, int(limit)))
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(f"SELECT COUNT(*) AS total FROM qd_market_events WHERE {where}", tuple(params))
        total = int((cur.fetchone() or {}).get("total") or 0)
        cur.execute(
            f"""
            SELECT market, symbol, name, event_type, trade_date, available_at,
                   rank, score, payload_json, source, ingested_at
            FROM qd_market_events
            WHERE {where}
            ORDER BY trade_date DESC, rank ASC NULLS LAST, symbol
            LIMIT ? OFFSET ?
            """,
            tuple(params) + (capped, max(0, int(offset))),
        )
        rows = [dict(row) for row in (cur.fetchall() or [])]
        cur.close()
    return {"items": rows, "total": total, "limit": capped, "offset": max(0, int(offset))}


def coverage() -> dict[str, Any]:
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT event_type, COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols,
                   MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
            FROM qd_market_events
            GROUP BY event_type
            ORDER BY event_type
            """
        )
        rows = [dict(row) for row in (cur.fetchall() or [])]
        cur.close()
    return {"available": bool(rows), "eventTypes": rows}


def coverage_by_date(event_types: Sequence[str], *, since: date | None = None) -> dict[date, set[str]]:
    """Which event types already have rows for each trade date (resume support)."""
    wanted = [str(item).strip().lower() for item in event_types if str(item).strip()]
    if not wanted:
        return {}
    clauses = ["event_type = ANY(?)"]
    params: list[Any] = [wanted]
    if since is not None:
        clauses.append("trade_date >= ?")
        params.append(since)
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            f"""
            SELECT trade_date, event_type
            FROM qd_market_events
            WHERE {' AND '.join(clauses)}
            GROUP BY trade_date, event_type
            """,
            tuple(params),
        )
        rows = cur.fetchall() or []
        cur.close()
    output: dict[date, set[str]] = {}
    for row in rows:
        output.setdefault(row["trade_date"], set()).add(str(row["event_type"]))
    return output


def latest_trade_dates(event_types: Sequence[str]) -> dict[str, date]:
    wanted = [str(item).strip().lower() for item in event_types if str(item).strip()]
    if not wanted:
        return {}
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT event_type, MAX(trade_date) AS last_date
            FROM qd_market_events
            WHERE event_type = ANY(?)
            GROUP BY event_type
            """,
            (wanted,),
        )
        rows = cur.fetchall() or []
        cur.close()
    return {str(row["event_type"]): row["last_date"] for row in rows}


__all__ = [
    "MARKET",
    "SOURCE",
    "EVENT_TYPES",
    "HISTORICAL_EVENT_TYPES",
    "SNAPSHOT_EVENT_TYPES",
    "EVENT_VALUE_FIELDS",
    "available_at",
    "fetch_events",
    "ensure_schema",
    "upsert_events",
    "start_run",
    "finish_run",
    "sync_date",
    "load_points",
    "event_value",
    "enrich_frame",
    "enrich_panel",
    "query_events",
    "coverage",
    "coverage_by_date",
    "latest_trade_dates",
]
