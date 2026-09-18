"""Symbol tags: sector/concept membership, event labels, and quote thresholds.

Three tag families with deliberately different resolution rules:

``industry`` / ``concept``
    Materialized in ``qd_symbol_tag_members``. The upstream constituent
    endpoint is present-day only, so a tag carries the ``as_of_date`` of its
    snapshot and ``point_in_time = FALSE``; a backtest before that date must be
    rejected rather than silently shown today's members.

``quote``
    Threshold rules in ``rule_json``, evaluated against the live full-market
    snapshot (cached in-process for ``SYMBOL_TAG_QUOTE_TICK_SEC``). Matched
    symbols are also written to ``qd_symbol_tag_members`` so per-symbol tag
    lookups and tag member counts stay cheap.

``event``
    Never materialized. ``qd_market_events`` is already point-in-time
    (``available_at`` is the trade date's post-close instant), so members are
    queried per date. ``point_in_time = TRUE``.

Hierarchy: tags are the flat screening/labelling layer. Multi-tag conditions
are persisted as a ``smart`` universe (``qd_universes``) rather than a second
table, so the whole existing backtest path (``POOL:<code>``, snapshots,
readiness checks) keeps working unchanged.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, datetime
from typing import Any, Iterable, Optional

from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

CATEGORIES = ("industry", "concept", "event", "quote")
MATERIALIZED_CATEGORIES = ("industry", "concept", "quote")
MARKET = "CNStock"
SOURCE = "hithink_finance"

# A missing data source must never silently satisfy a threshold: metrics listed
# here have a real feed, everything else makes refresh report the gap instead.
_METRIC_SOURCES: dict[str, str] = {
    "change_pct": "hithink snapshot price_change_ratio_pct",
    "amount": "hithink snapshot turnover",
    "turnover_rate": "hithink snapshot volume / fundamental shares_outstanding",
}

_OPERATORS = {
    "gte": lambda value, threshold: value >= threshold,
    "gt": lambda value, threshold: value > threshold,
    "lte": lambda value, threshold: value <= threshold,
    "lt": lambda value, threshold: value < threshold,
}

SCREEN_DEFAULT_LIMIT = 200
SCREEN_MAX_LIMIT = 2000

_quote_lock = threading.Lock()
_quote_cache: dict[str, Any] = {"day": None, "at": 0.0, "rows": []}

# The symbol master holds delisted/invalid A-share codes (e.g. 000004.SZ), and
# HiThink rejects the *entire* snapshot batch for one unknown thscode. Learned at
# runtime by dropping the code named in the error and retrying; per-process is
# enough since only the refresh worker runs this path.
# ponytail: module-level set, promote to a table if this ever runs multi-process
# with a high churn of newly delisted symbols.
_REJECTED_SYMBOLS: set[str] = set()
_SNAPSHOT_BATCH = 200
_REJECTED_RE = re.compile(r"Unknown A-share thscode:\s*([0-9]{6}\.[A-Z]{2})", re.IGNORECASE)


class SymbolTagError(ValueError):
    """A stable API-facing tag validation error."""

    def __init__(self, code: str, *, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(str(os.getenv(name) or "").strip() or default))
    except (TypeError, ValueError):
        return default


def _json(value: Any, fallback: Any) -> Any:
    if value is None or value == "":
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _iso(value: Any) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def normalize_tag_code(value: Any) -> str:
    code = str(value or "").strip().lower()
    if not code:
        raise SymbolTagError("symbolTag.codeRequired")
    return code


def normalize_conditions(raw: Any) -> list[dict]:
    """Validate a screen request: one ``{tag_code, value?}`` per condition."""
    if not isinstance(raw, (list, tuple)) or not raw:
        raise SymbolTagError("symbolTag.conditionsRequired")
    if len(raw) > 32:
        raise SymbolTagError("symbolTag.tooManyConditions")
    conditions: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise SymbolTagError("symbolTag.invalidCondition")
        code = normalize_tag_code(item.get("tag_code") or item.get("tagCode") or item.get("code"))
        condition: dict[str, Any] = {"tag_code": code}
        value = item.get("value")
        if value is not None and value != "":
            try:
                condition["value"] = float(value)
            except (TypeError, ValueError) as exc:
                raise SymbolTagError("symbolTag.invalidThreshold") from exc
        conditions.append(condition)
    return conditions


def _parse_day(value: Any) -> date:
    if value is None or value == "":
        from app.data_sources import hithink_finance as hithink

        return hithink.shanghai_today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError) as exc:
        raise SymbolTagError("symbolTag.invalidAsOf") from exc


def _parse_range(value: Any, fallback: date) -> tuple[date, date]:
    """Normalize an optional ``[start, end]`` tag-evaluation window."""
    if not value:
        return fallback, fallback
    if isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = _parse_day(value[0]), _parse_day(value[1])
    elif isinstance(value, dict):
        start = _parse_day(value.get("start"))
        end = _parse_day(value.get("end"))
    else:
        raise SymbolTagError("symbolTag.invalidDateRange")
    if end < start:
        raise SymbolTagError("symbolTag.invalidDateRange")
    return start, end


def _exchange_suffix(digits: str) -> str:
    if digits.startswith("6"):
        return ".SH"
    if digits.startswith(("4", "8", "92")):
        return ".BJ"
    return ".SZ"


class SymbolTagService:
    """Tag catalogue, membership resolution, screening, and refresh."""

    # -- catalogue ---------------------------------------------------------
    def list_tags(self, *, category: str = "", keyword: str = "") -> list[dict]:
        clauses = ["t.status <> 'deprecated'"]
        params: list[Any] = []
        wanted = str(category or "").strip().lower()
        if wanted:
            if wanted not in CATEGORIES:
                raise SymbolTagError("symbolTag.invalidCategory")
            clauses.append("t.category = ?")
            params.append(wanted)
        text = str(keyword or "").strip()
        if text:
            clauses.append("(t.name LIKE ? OR t.code LIKE ?)")
            params.extend([f"%{text}%", f"%{text.lower()}%"])
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                SELECT t.*,
                       (SELECT COUNT(*) FROM qd_symbol_tag_members m WHERE m.tag_id = t.id)
                         AS member_count,
                       (SELECT MAX(m.as_of_date) FROM qd_symbol_tag_members m WHERE m.tag_id = t.id)
                         AS as_of_date
                FROM qd_symbol_tags t
                WHERE {' AND '.join(clauses)}
                ORDER BY t.category, t.sort_order, t.code
                """,
                tuple(params),
            )
            rows = cur.fetchall() or []
            cur.close()
        return [_serialize_tag(row) for row in rows]

    def get_tag(self, code: str) -> dict:
        return _serialize_tag(self._tag_row(normalize_tag_code(code)))

    def _tag_row(self, code: str) -> dict:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT t.*,
                       (SELECT COUNT(*) FROM qd_symbol_tag_members m WHERE m.tag_id = t.id)
                         AS member_count,
                       (SELECT MAX(m.as_of_date) FROM qd_symbol_tag_members m WHERE m.tag_id = t.id)
                         AS as_of_date
                FROM qd_symbol_tags t
                WHERE t.code = ?
                """,
                (code,),
            )
            row = cur.fetchone()
            cur.close()
        if not row:
            raise SymbolTagError("symbolTag.notFound", status_code=404)
        return row

    # -- membership --------------------------------------------------------
    def resolve_members(self, code: str, *, as_of: Any = None, limit: int = 0) -> list[dict]:
        tag = self._tag_row(normalize_tag_code(code))
        if str(tag.get("category")) == "event":
            day = _parse_day(as_of)
            return self._resolve_event_members(tag, start=day, end=day, limit=limit)
        return self._resolve_stored_members(int(tag["id"]), limit=limit)

    def _resolve_stored_members(self, tag_id: int, *, limit: int = 0) -> list[dict]:
        sql = """
            SELECT market, symbol, name, as_of_date, metadata_json
            FROM qd_symbol_tag_members
            WHERE tag_id = ?
            ORDER BY symbol
        """
        params: list[Any] = [tag_id]
        if limit and int(limit) > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(sql, tuple(params))
            rows = cur.fetchall() or []
            cur.close()
        return [_serialize_member(row) for row in rows]

    def _resolve_event_members(
        self,
        tag: dict,
        *,
        start: date,
        end: date,
        limit: int = 0,
    ) -> list[dict]:
        """Board members in ``[start, end]``.

        The range matters for a backtest pool: a candidate list wants every
        symbol that was on the board at any point in the window, while a
        screener passes a single day. ``available_at`` is still enforced so an
        intraday evaluation cannot read the day's not-yet-published board.
        """
        rule = _json(tag.get("rule_json"), {})
        event_types = [str(item) for item in (rule.get("event_types") or []) if str(item).strip()]
        if not event_types:
            return []
        from app.services.events_data import available_at as event_available_at

        cap = int(limit) if limit and int(limit) > 0 else _int_env("SYMBOL_TAG_EVENT_MEMBER_CAP", 3000)
        placeholders = ", ".join("?" for _ in event_types)
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                f"""
                SELECT market, symbol, MIN(name) AS name, MAX(trade_date) AS trade_date,
                       MIN(rank) AS rank
                FROM qd_market_events
                WHERE market = ?
                  AND event_type IN ({placeholders})
                  AND trade_date BETWEEN ? AND ?
                  AND available_at <= ?
                GROUP BY market, symbol
                ORDER BY rank NULLS LAST, symbol
                LIMIT ?
                """,
                (str(tag.get("market") or MARKET), *event_types, start, end,
                 event_available_at(end), cap),
            )
            rows = cur.fetchall() or []
            cur.close()
        return [
            {
                "market": str(row.get("market") or MARKET),
                "symbol": str(row.get("symbol") or ""),
                "name": str(row.get("name") or ""),
                "as_of_date": _iso(row.get("trade_date")),
                "rank": row.get("rank"),
                "metadata": {"event_types": event_types},
            }
            for row in rows
            if row.get("symbol")
        ]

    # -- screening ---------------------------------------------------------
    def screen(
        self,
        conditions: Any,
        *,
        as_of: Any = None,
        date_range: Any = None,
        limit: int = SCREEN_DEFAULT_LIMIT,
        offset: int = 0,
        with_quotes: bool = True,
    ) -> dict:
        """Members matching every condition group.

        Semantics: OR within one category, AND across categories. Two industry
        tags therefore mean "in either industry"; an industry tag plus an event
        tag means "in that industry AND on that board".

        Quote thresholds are the exception: each one is an independent numeric
        filter, so they combine with AND. OR-ing "change >= 9.8%" with "amount
        >= 1e9" would return every liquid large cap alongside the limit-ups,
        which is never what a screener wants.

        ``date_range`` (``[start, end]``) widens event tags to "on the board at
        any point in the window", which is the right candidate set for a
        backtest; without it every tag is evaluated as of ``as_of``.

        ``with_quotes`` warms the full-market snapshot so the returned page
        carries price/change. Universe resolution turns it off: a backtest
        never needs today's quotes.
        """
        normalized = normalize_conditions(conditions)
        limit = max(1, min(SCREEN_MAX_LIMIT, int(limit or SCREEN_DEFAULT_LIMIT)))
        offset = max(0, int(offset or 0))
        day = _parse_day(as_of)
        start, end = _parse_range(date_range, day)
        groups: dict[str, list[dict]] = {}
        for condition in normalized:
            tag = self._tag_row(condition["tag_code"])
            category = str(tag.get("category"))
            # Quote conditions each get their own group so they AND together.
            key = f"{category}:{condition['tag_code']}" if category == "quote" else category
            groups.setdefault(key, []).append({**condition, "tag": tag})

        resolved: dict[tuple[str, str], dict] = {}
        for conditions_in_category in groups.values():
            group: dict[tuple[str, str], dict] = {}
            for condition in conditions_in_category:
                for member in self._members_for_condition(condition, day, start, end):
                    key = (member["market"], member["symbol"])
                    entry = group.get(key)
                    if entry is None:
                        entry = {**member, "tags": [], "matched": {}}
                        group[key] = entry
                    code = condition["tag"]["code"]
                    if code not in entry["tags"]:
                        entry["tags"].append(code)
                    entry["matched"].update(member.get("matched") or {})
            # No member in one category means the AND can only be empty.
            if not group:
                return {"items": [], "total": 0, "as_of": end.isoformat(), "truncated": False}
            if not resolved:
                resolved = group
                continue
            merged: dict[tuple[str, str], dict] = {}
            for key, entry in resolved.items():
                other = group.get(key)
                if other is None:
                    continue
                entry["tags"] = sorted(set(entry["tags"]) | set(other["tags"]))
                entry["matched"].update(other["matched"])
                merged[key] = entry
            resolved = merged
            if not resolved:
                return {"items": [], "total": 0, "as_of": end.isoformat(), "truncated": False}

        items = sorted(resolved.values(), key=lambda item: item["symbol"])
        total = len(items)
        page = items[offset: offset + limit]
        if with_quotes:
            self._attach_quotes(page, self.quote_rows(day))
        for item in page:
            item.pop("matched", None)
            item["tags"] = sorted(set(item["tags"]))
        return {
            "items": page,
            "total": total,
            "as_of": end.isoformat(),
            "truncated": total > offset + len(page),
        }

    def _members_for_condition(
        self,
        condition: dict,
        day: date,
        start: date,
        end: date,
    ) -> list[dict]:
        tag = condition["tag"]
        if str(tag.get("category")) == "event":
            return self._resolve_event_members(tag, start=start, end=end)
        if str(tag.get("category")) == "quote":
            override = condition.get("value")
            overrides = {tag["code"]: override} if override is not None else None
            return self.quote_members(
                day, only={tag["code"]}, rule_override=overrides
            )
        members = self.resolve_members(tag["code"], as_of=day)
        for member in members:
            member["tags"] = [tag["code"]]
        return members

    # -- single symbol -----------------------------------------------------
    def symbols_tags_batch(
        self,
        market: str,
        symbols: Iterable[str],
        categories: Iterable[str] = CATEGORIES,
    ) -> dict[str, list[dict]]:
        """Tags for many symbols in one round trip.

        A 200-row board must not issue 200 queries: everything materialized
        (industry/concept/quote) comes from one ``= ANY(?)`` join, and the
        non-materialized event tags from one more. Both are independent of the
        symbol count, which is the whole point of this method existing.
        """
        wanted_market = str(market or MARKET).strip() or MARKET
        clean: list[str] = []
        seen: set[str] = set()
        for item in symbols or []:
            symbol = str(item or "").strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                clean.append(symbol)
        if not clean:
            raise SymbolTagError("symbolTag.symbolRequired")
        if len(clean) > 500:
            raise SymbolTagError("symbolTag.tooManySymbols")
        wanted = [
            name for name in CATEGORIES
            if name in {str(item or "").strip().lower() for item in (categories or ())}
        ]
        if not wanted:
            raise SymbolTagError("symbolTag.invalidCategory")
        output: dict[str, list[dict]] = {symbol: [] for symbol in clean}
        materialized = [name for name in wanted if name in MATERIALIZED_CATEGORIES]
        with get_db_connection() as db:
            cur = db.cursor()
            if materialized:
                cur.execute(
                    """
                    SELECT m.symbol, t.*, m.as_of_date
                    FROM qd_symbol_tag_members m
                    JOIN qd_symbol_tags t ON t.id = m.tag_id
                    WHERE m.market = ? AND m.symbol = ANY(?) AND t.status <> 'deprecated'
                      AND t.category = ANY(?)
                    ORDER BY t.category, t.sort_order, t.code
                    """,
                    (wanted_market, clean, materialized),
                )
                for row in cur.fetchall() or []:
                    symbol = str(row.get("symbol") or "").upper()
                    if symbol in output:
                        output[symbol].append(_serialize_tag(row))
            if "event" in wanted:
                for symbol, tag in self._event_tags_batch(cur, wanted_market, clean):
                    if symbol in output:
                        output[symbol].append(tag)
            cur.close()
        return output

    @staticmethod
    def _event_tags_batch(cur, market: str, symbols: list[str]) -> list[tuple[str, dict]]:
        """``[(symbol, tag)]`` for every event tag any of ``symbols`` has ever hit.

        One query for the whole list: ``qd_market_events`` is point-in-time, so
        the join goes through each tag's ``rule_json.event_types`` instead of a
        stored membership row.
        """
        cur.execute(
            """
            SELECT e.symbol, t.*, MAX(e.trade_date) AS last_trade_date
            FROM qd_symbol_tags t
            JOIN qd_market_events e
              ON e.market = t.market
             AND e.available_at <= NOW()
             AND e.event_type = ANY(
                   SELECT jsonb_array_elements_text(t.rule_json -> 'event_types')
                 )
            WHERE t.category = 'event'
              AND t.status <> 'deprecated'
              AND t.market = ?
              AND e.symbol = ANY(?)
            GROUP BY t.id, e.symbol
            ORDER BY t.sort_order, t.code
            """,
            (market, symbols),
        )
        output: list[tuple[str, dict]] = []
        seen: set[tuple[str, int]] = set()
        for row in cur.fetchall() or []:
            symbol = str(row.get("symbol") or "").upper()
            key = (symbol, int(row.get("id") or 0))
            if not symbol or key in seen:
                continue
            seen.add(key)
            tag = _serialize_tag(row)
            tag["as_of_date"] = _iso(row.get("last_trade_date"))
            output.append((symbol, tag))
        return output

    def tags_for_symbol(self, market: str, symbol: str) -> list[dict]:
        wanted_market = str(market or MARKET).strip() or MARKET
        wanted_symbol = str(symbol or "").strip().upper()
        if not wanted_symbol:
            raise SymbolTagError("symbolTag.symbolRequired")
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT t.*, m.as_of_date
                FROM qd_symbol_tag_members m
                JOIN qd_symbol_tags t ON t.id = m.tag_id
                WHERE m.market = ? AND m.symbol = ? AND t.status <> 'deprecated'
                ORDER BY t.category, t.sort_order, t.code
                """,
                (wanted_market, wanted_symbol),
            )
            stored = cur.fetchall() or []
            event_rows = self._event_tags_for_symbol(cur, wanted_market, wanted_symbol)
            cur.close()
        items = [_serialize_tag(row) for row in stored]
        for row in event_rows:
            tag = _serialize_tag(row)
            tag["as_of_date"] = _iso(row.get("last_trade_date"))
            items.append(tag)
        return items

    @staticmethod
    def _event_tags_for_symbol(cur, market: str, symbol: str) -> list[dict]:
        cur.execute(
            """
            SELECT t.*, MAX(e.trade_date) AS last_trade_date
            FROM qd_symbol_tags t
            JOIN qd_market_events e
              ON e.market = t.market
             AND e.available_at <= NOW()
             AND e.event_type = ANY(
                   SELECT jsonb_array_elements_text(t.rule_json -> 'event_types')
                 )
            WHERE t.category = 'event'
              AND t.status <> 'deprecated'
              AND e.market = ?
              AND e.symbol = ?
            GROUP BY t.id
            ORDER BY t.sort_order, t.code
            """,
            (market, symbol),
        )
        return cur.fetchall() or []

    # -- quote thresholds --------------------------------------------------
    def quote_members(
        self,
        day: date,
        *,
        only: set[str] | None = None,
        rule_override: dict[str, float] | None = None,
    ) -> list[dict]:
        """Symbols matching the active quote tags for ``day``'s snapshot.

        ``only`` restricts evaluation to the given tag codes; without it the
        result is the union over every active quote tag, which is almost never
        what a caller wants (one screen condition must mean one tag).
        """
        tags = [
            row for row in self.list_tags(category="quote")
            if row["status"] == "active" and (not only or row["code"] in only)
        ]
        if not tags:
            return []
        rows = self.quote_rows(day)
        if not rows:
            return []
        overrides = rule_override or {}
        needs_turnover = any(
            str((tag.get("rule") or {}).get("metric")) == "turnover_rate" for tag in tags
        )
        turnover_rate = self._turnover_rates(rows) if needs_turnover else {}

        matched: dict[tuple[str, str], dict] = {}
        for tag in tags:
            rule = dict(tag.get("rule") or {})
            code = tag["code"]
            if code in overrides and overrides[code] is not None:
                rule["value"] = overrides[code]
            metric = str(rule.get("metric") or "")
            operator = _OPERATORS.get(str(rule.get("op") or "gte"))
            threshold = _float(rule.get("value"))
            if operator is None or threshold is None or metric not in _METRIC_SOURCES:
                continue
            for row in rows:
                raw = turnover_rate.get(row["symbol"]) if metric == "turnover_rate" else row.get(metric)
                if raw is None:
                    continue
                if not operator(float(raw), threshold):
                    continue
                key = (row["market"], row["symbol"])
                entry = matched.get(key)
                if entry is None:
                    entry = {
                        "market": row["market"],
                        "symbol": row["symbol"],
                        "name": row["name"],
                        "as_of_date": day.isoformat(),
                        "rank": None,
                        "metadata": {"quote": {
                            "change_pct": row.get("change_pct"),
                            "amount": row.get("amount"),
                            "price": row.get("price"),
                        }},
                        "tags": [],
                        "matched": {},
                    }
                    matched[key] = entry
                entry["tags"].append(code)
                entry["matched"][code] = round(float(raw), 4)
        return list(matched.values())

    def quote_rows(self, day: date, *, fresh: bool = False) -> list[dict]:
        """Full-market A-share snapshot for ``day``.

        Readers use the stored snapshot for that day: taking one upstream pass
        costs ~70s (120 req/min cap across ~5500 symbols), which no HTTP request
        should ever wait for. ``fresh=True`` forces an upstream fetch, which is
        what the refresh tick uses.
        """
        if not fresh:
            stored = self._stored_quote_rows(day)
            if stored:
                return stored
        with _quote_lock:
            cached = (
                _quote_cache["day"] == day
                and (time.monotonic() - float(_quote_cache["at"] or 0.0))
                < _int_env("SYMBOL_TAG_QUOTE_TICK_SEC", 300)
            )
            if cached and not fresh:
                return _quote_cache["rows"]
        rows = self._fetch_quote_rows()
        with _quote_lock:
            _quote_cache.update({"day": day, "at": time.monotonic(), "rows": rows})
        return rows

    @staticmethod
    def _stored_quote_rows(day: date) -> list[dict]:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT symbol, name, change_pct, amount, volume, price
                FROM qd_symbol_quote_snapshots
                WHERE market = ? AND as_of_date = ?
                ORDER BY symbol
                """,
                (MARKET, day),
            )
            rows = cur.fetchall() or []
            cur.close()
        return [
            {
                "market": MARKET,
                "symbol": str(row.get("symbol") or ""),
                "name": str(row.get("name") or ""),
                "change_pct": _float(row.get("change_pct")),
                "amount": _float(row.get("amount")),
                "volume": _float(row.get("volume")),
                "price": _float(row.get("price")),
            }
            for row in rows
            if row.get("symbol")
        ]

    @staticmethod
    def _store_quote_rows(day: date, rows: list[dict]) -> None:
        payload = [
            (
                MARKET, row["symbol"], row.get("name") or "", day,
                row.get("change_pct"), row.get("amount"),
                row.get("volume"), row.get("price"),
            )
            for row in rows
            if row.get("symbol")
        ]
        if not payload:
            return
        with get_db_connection() as db:
            cur = db.cursor()
            cur.executemany(
                """
                INSERT INTO qd_symbol_quote_snapshots
                  (market, symbol, name, as_of_date, change_pct, amount, volume, price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (market, symbol, as_of_date) DO UPDATE SET
                  name = EXCLUDED.name,
                  change_pct = EXCLUDED.change_pct,
                  amount = EXCLUDED.amount,
                  volume = EXCLUDED.volume,
                  price = EXCLUDED.price,
                  updated_at = NOW()
                """,
                payload,
            )
            db.commit()
            cur.close()

    @staticmethod
    def _fetch_quote_rows() -> list[dict]:
        from app.data_sources import hithink_finance as hithink

        if not hithink.configured():
            return []
        symbols = _cn_symbols()
        if not symbols:
            return []
        # The snapshot endpoint returns no name; the master catalogue does.
        names = _symbol_names()
        rows: list[dict] = []
        for item in _snapshot_tolerant(symbols):
            ticker = hithink.snapshot_to_ticker(item)
            symbol = str(ticker.get("symbol") or "")
            if not symbol:
                continue
            rows.append({
                "market": MARKET,
                "symbol": symbol,
                "name": names.get(symbol, ""),
                "change_pct": ticker.get("changePercent"),
                "amount": ticker.get("turnover"),
                "volume": ticker.get("volume"),
                "price": ticker.get("last"),
            })
        return rows

    @staticmethod
    def _turnover_rates(rows: list[dict]) -> dict[str, float]:
        """Turnover % = traded shares / shares outstanding * 100.

        HiThink's snapshot ``volume`` is already in shares (turnover / price
        matches it to rounding), and the only share count we have is
        ``qd_fundamental_snapshots.shares_outstanding`` — total shares, not the
        free float the exchange uses. The number is therefore a documented
        approximation, consistently biased low versus the official turnover.
        """
        shares = _shares_outstanding()
        if not shares:
            return {}
        rates: dict[str, float] = {}
        for row in rows:
            count = shares.get(row["symbol"])
            volume = _float(row.get("volume"))
            if not count or volume is None:
                continue
            rates[row["symbol"]] = round(float(volume) / float(count) * 100.0, 4)
        return rates

    @staticmethod
    def _attach_quotes(members: list[dict], rows: list[dict] | None = None) -> None:
        """Fill price/change from the day's snapshot; never trigger a fetch."""
        if not members:
            return
        if rows is None:
            with _quote_lock:
                rows = list(_quote_cache["rows"] or [])
        if not rows:
            return
        quotes = {row["symbol"]: row for row in rows}
        for member in members:
            quote = quotes.get(member["symbol"])
            if not quote:
                continue
            member["change_pct"] = quote.get("change_pct")
            member["amount"] = quote.get("amount")
            member["price"] = quote.get("price")

    # -- refresh -----------------------------------------------------------
    def refresh(self, *, categories: Iterable[str] = MATERIALIZED_CATEGORIES) -> dict:
        """Rebuild tag membership from upstream.

        ``event`` is deliberately absent: it reads ``qd_market_events``, which
        ``hithink-events-sync`` already maintains.
        """
        wanted = [str(item).strip().lower() for item in categories if str(item).strip()]
        unknown = [item for item in wanted if item not in MATERIALIZED_CATEGORIES]
        if unknown:
            raise SymbolTagError(f"symbolTag.notRefreshable:{','.join(unknown)}")
        report: dict[str, Any] = {"categories": wanted, "warnings": []}
        sector_categories = [item for item in wanted if item in ("industry", "concept")]
        if sector_categories:
            report["sectors"] = self._refresh_sectors(sector_categories)
        if "quote" in wanted:
            report["quote"] = self._refresh_quote()
        return report

    @staticmethod
    def _refresh_sectors(categories: list[str]) -> dict:
        from scripts.sync_ths_sector_tags import sync as sync_sectors

        tag_names = ["industry" if item == "industry" else "cn_concept" for item in categories]
        limit = _int_env("SYMBOL_TAG_SECTOR_LIMIT", 0) or None
        return sync_sectors(
            tags=tag_names,
            only=[],
            limit=limit,
            as_of=date.today(),
            dry_run=False,
        )

    def _refresh_quote(self) -> dict:
        day = date.today()
        try:
            rows = self.quote_rows(day, fresh=True)
        except Exception as exc:  # noqa: BLE001 - a refresh must degrade, not kill the beat
            logger.warning("Quote snapshot refresh failed: %s", exc)
            return {"status": "failed", "error": str(exc)[:200]}
        if not rows:
            return {"status": "no_data", "scanned": 0, "members": 0}
        self._store_quote_rows(day, rows)
        members = self.quote_members(day)
        by_tag: dict[str, int] = {}
        for member in members:
            for code in member.get("tags") or []:
                by_tag[code] = by_tag.get(code, 0) + 1
        written = self._store_quote_members(day, members)
        return {
            "status": "ok",
            "as_of": day.isoformat(),
            "scanned": len(rows),
            "members": written,
            "by_tag": by_tag,
        }

    @staticmethod
    def _store_quote_members(day: date, members: list[dict]) -> int:
        if not members:
            return 0
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                "SELECT id, code FROM qd_symbol_tags WHERE category = 'quote'"
            )
            tags = {str(row["code"]): int(row["id"]) for row in (cur.fetchall() or [])}
            written = 0
            for member in members:
                for code in member.get("tags") or []:
                    tag_id = tags.get(code)
                    if not tag_id:
                        continue
                    cur.execute(
                        """
                        INSERT INTO qd_symbol_tag_members
                          (tag_id, market, symbol, name, as_of_date, metadata_json)
                        VALUES (?, ?, ?, ?, ?, ?::jsonb)
                        ON CONFLICT (tag_id, market, symbol) DO UPDATE SET
                          name = EXCLUDED.name,
                          as_of_date = EXCLUDED.as_of_date,
                          metadata_json = EXCLUDED.metadata_json
                        """,
                        (
                            tag_id,
                            member["market"],
                            member["symbol"],
                            member.get("name") or "",
                            day,
                            json.dumps(member.get("metadata") or {}, ensure_ascii=False),
                        ),
                    )
                    written += 1
            db.commit()
            cur.close()
        return written


def _snapshot_tolerant(symbols: list[str]) -> list[dict]:
    """Snapshot in batches, dropping the delisted code named in a param error.

    One unknown thscode fails its whole batch upstream, so a single stale symbol
    in the master would otherwise blank out the full-market quote refresh.
    """
    from app.data_sources.hithink_finance import HiThinkParamError, snapshot

    rows: list[dict] = []
    pending = [item for item in symbols if item.upper() not in _REJECTED_SYMBOLS]
    while pending:
        batch = pending[:_SNAPSHOT_BATCH]
        pending = pending[_SNAPSHOT_BATCH:]
        try:
            rows.extend(snapshot(batch))
        except HiThinkParamError as exc:
            match = _REJECTED_RE.search(str(exc))
            if not match or match.group(1).upper() not in {item.upper() for item in batch}:
                raise
            rejected = match.group(1).upper()
            _REJECTED_SYMBOLS.add(rejected)
            logger.warning("HiThink rejected %s; excluding it from the quote snapshot", rejected)
            pending = [item for item in batch if item.upper() != rejected] + pending
    return rows


def _shares_outstanding() -> dict[str, float]:
    """Latest total share count per symbol, keyed by thscode (``600519.SH``).

    ``qd_fundamental_snapshots`` stores bare A-share digits (``600519``) while
    the quote snapshot speaks thscodes, so the keys are rewritten here rather
    than at every call site.
    """
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT DISTINCT ON (symbol) symbol, shares_outstanding
            FROM qd_fundamental_snapshots
            WHERE market = ? AND shares_outstanding IS NOT NULL AND shares_outstanding > 0
            ORDER BY symbol, available_at DESC, period_end DESC
            """,
            (MARKET,),
        )
        rows = cur.fetchall() or []
        cur.close()
    shares: dict[str, float] = {}
    for row in rows:
        digits = str(row.get("symbol") or "").upper()
        if len(digits) == 6 and digits.isdigit():
            shares[f"{digits}{_exchange_suffix(digits)}"] = float(row["shares_outstanding"])
        elif digits:
            shares[digits] = float(row["shares_outstanding"])
    return shares


def _cn_symbols() -> list[str]:
    """Active A-share universe in thscode form (``600519.SH``)."""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT symbol FROM qd_market_symbols
            WHERE market = ? AND is_active = 1 AND asset_class = 'equity'
              AND symbol ~ '^[0-9]{6}$'
            ORDER BY is_hot DESC, symbol
            """,
            (MARKET,),
        )
        rows = cur.fetchall() or []
        cur.close()
    return [
        f"{digits}{_exchange_suffix(digits)}"
        for digits in (str(row.get("symbol") or "") for row in rows)
        if len(digits) == 6
    ]


def _symbol_names() -> dict[str, str]:
    """``600519.SH`` -> display name, from the master catalogue."""
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT symbol, name FROM qd_market_symbols
            WHERE market = ? AND is_active = 1 AND asset_class = 'equity'
              AND symbol ~ '^[0-9]{6}$' AND name <> ''
            """,
            (MARKET,),
        )
        rows = cur.fetchall() or []
        cur.close()
    names: dict[str, str] = {}
    for row in rows:
        digits = str(row.get("symbol") or "")
        if len(digits) != 6:
            continue
        names[f"{digits}{_exchange_suffix(digits)}"] = str(row.get("name") or "")
    return names


def _serialize_tag(row: dict) -> dict:
    return {
        "id": int(row.get("id") or 0),
        "code": str(row.get("code") or ""),
        "name": str(row.get("name") or ""),
        "category": str(row.get("category") or ""),
        "market": str(row.get("market") or ""),
        "source": str(row.get("source") or ""),
        "is_system": bool(row.get("is_system")),
        "status": str(row.get("status") or ""),
        "sort_order": int(row.get("sort_order") or 0),
        "point_in_time": bool(row.get("point_in_time")),
        "rule": _json(row.get("rule_json"), {}),
        "metadata": _json(row.get("metadata_json"), {}),
        "member_count": int(row.get("member_count") or 0),
        "as_of_date": _iso(row.get("as_of_date")),
        "updated_at": _iso(row.get("updated_at")),
        "created_at": _iso(row.get("created_at")),
    }


def _serialize_member(row: dict) -> dict:
    return {
        "market": str(row.get("market") or ""),
        "symbol": str(row.get("symbol") or ""),
        "name": str(row.get("name") or ""),
        "as_of_date": _iso(row.get("as_of_date")),
        "rank": row.get("rank") if "rank" in row else None,
        "metadata": _json(row.get("metadata_json"), {}),
    }


def tag_conditions_floor(conditions: Any) -> str:
    """Earliest usable date for a smart universe, ``''`` when unbounded.

    A smart universe holding any snapshot-only tag must be pinned to the oldest
    snapshot it depends on, otherwise a backtest would see present-day
    membership in the past.
    """
    service = get_symbol_tag_service()
    floor = ""
    for condition in normalize_conditions(conditions):
        tag = service.get_tag(condition["tag_code"])
        if tag.get("point_in_time"):
            continue
        as_of = str(tag.get("as_of_date") or "")
        if as_of > floor:
            floor = as_of
    return floor


_service: Optional[SymbolTagService] = None


def get_symbol_tag_service() -> SymbolTagService:
    global _service
    if _service is None:
        _service = SymbolTagService()
    return _service


__all__ = [
    "CATEGORIES",
    "MATERIALIZED_CATEGORIES",
    "SymbolTagError",
    "SymbolTagService",
    "get_symbol_tag_service",
    "normalize_conditions",
    "normalize_tag_code",
    "tag_conditions_floor",
]
