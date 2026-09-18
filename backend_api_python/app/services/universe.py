"""Point-in-time strategy universe management."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from app.services.market.watchlist import VALID_MARKETS, normalize_symbol
from app.services.market_context import MarketContext
from app.services.symbol_name import normalize_crypto_symbol
from app.utils.db import get_db_connection


STATIC_START = date(1900, 1, 1)
SUPPORTED_UNIVERSE_TYPES = frozenset({
    "manual", "watchlist", "index", "etf", "market_cap", "market", "smart",
})
EDITABLE_UNIVERSE_TYPES = frozenset({"manual", "smart"})
# A smart universe stores tag conditions instead of members.
SMART_SOURCE = "symbol_tags"
MAX_MANUAL_MEMBERS = 5000
_CODE_RE = re.compile(r"[^a-z0-9_-]+")


class UniverseError(ValueError):
    """A stable API-facing universe validation error."""

    def __init__(self, code: str, *, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def parse_as_of(value: Any) -> date:
    """Normalize an as-of value without accepting time-zone ambiguous strings."""
    if value is None or value == "":
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise UniverseError("universe.invalidAsOf") from exc


def normalize_universe_code(name: str) -> str:
    """Build a stable user-universe code from an arbitrary display name."""
    cleaned = _CODE_RE.sub("-", str(name or "").strip().lower()).strip("-_")
    return cleaned[:48] or "manual"


def normalize_member(raw: dict, *, default_market: str = "") -> dict:
    """Validate and canonicalize one universe member."""
    if not isinstance(raw, dict):
        raise UniverseError("universe.invalidMember")
    market = str(raw.get("market") or default_market or "").strip()
    if market not in VALID_MARKETS:
        raise UniverseError("universe.invalidMarket")
    symbol = normalize_symbol(raw.get("symbol"))
    if market == "Crypto":
        symbol = normalize_crypto_symbol(symbol)
        if "/" not in symbol:
            raise UniverseError("universe.invalidCryptoSymbol")
    if not symbol or len(symbol) > 80:
        raise UniverseError("universe.invalidSymbol")

    context = MarketContext.from_mapping({
        "market": market,
        "symbol": symbol,
        "exchange_id": raw.get("exchange_id") or raw.get("exchangeId") or "",
        "market_type": raw.get("market_type") or raw.get("marketType") or "",
        "instrument_id": raw.get("instrument_id") or raw.get("instrumentId") or "",
        "settle_currency": raw.get("settle_currency") or raw.get("settleCurrency") or "",
    })
    return {
        "market": market,
        "symbol": symbol,
        "name": str(raw.get("name") or "").strip()[:160],
        "exchange_id": context.exchange_id,
        "market_type": context.market_type,
        "instrument_id": context.instrument_id,
        "settle_currency": context.settle_currency,
        "weight": _optional_float(raw.get("weight")),
        "rank": _optional_int(raw.get("rank")),
        "metadata": raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
    }


def normalize_members(raw_members: Iterable[dict], *, default_market: str = "") -> list[dict]:
    """Normalize, deduplicate, and deterministically order universe members."""
    if not isinstance(raw_members, (list, tuple)):
        raise UniverseError("universe.membersRequired")
    if len(raw_members) > MAX_MANUAL_MEMBERS:
        raise UniverseError("universe.tooManyMembers")
    deduped: dict[tuple, dict] = {}
    for raw in raw_members:
        member = normalize_member(raw, default_market=default_market)
        key = (
            member["market"], member["symbol"], member["exchange_id"],
            member["market_type"], member["instrument_id"],
        )
        deduped[key] = member
    return [deduped[key] for key in sorted(deduped)]


def member_content_hash(members: Iterable[dict]) -> str:
    """Hash only canonical execution identity and optional index metadata."""
    canonical = []
    for member in members:
        canonical.append({
            "market": member.get("market") or "",
            "symbol": member.get("symbol") or "",
            "exchange_id": member.get("exchange_id") or "",
            "market_type": member.get("market_type") or "",
            "instrument_id": member.get("instrument_id") or "",
            "weight": member.get("weight"),
            "rank": member.get("rank"),
        })
    canonical.sort(key=lambda item: (
        item["market"], item["symbol"], item["exchange_id"],
        item["market_type"], item["instrument_id"],
    ))
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class UniverseService:
    """CRUD, point-in-time resolution, and immutable snapshot operations."""

    def list_universes(self, user_id: int) -> list[dict]:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT u.*,
                       (SELECT MIN(m.valid_from) FROM qd_universe_members m WHERE m.universe_id = u.id) AS history_from,
                       CASE WHEN u.universe_type = 'watchlist' THEN
                         (SELECT COUNT(*) FROM qd_watchlist w WHERE w.user_id = ?)
                       ELSE
                         (SELECT COUNT(*) FROM qd_universe_members m
                          WHERE m.universe_id = u.id
                            AND m.valid_from <= CURRENT_DATE
                            AND (m.valid_to IS NULL OR m.valid_to > CURRENT_DATE))
                       END AS member_count
                FROM qd_universes u
                WHERE (u.is_system = TRUE OR u.user_id = ?)
                  AND u.status <> 'deprecated'
                ORDER BY u.is_system DESC, u.id ASC
                """,
                (int(user_id), int(user_id)),
            )
            rows = cur.fetchall() or []
            cur.close()
        items = [_serialize_universe(row) for row in rows]
        for item in items:
            if item.get("source") == "symbol_master":
                item["member_count"] = len(self._symbol_master_members(item.get("source_ref") or ""))
            elif item.get("source") == SMART_SOURCE:
                # Counts would need a live screen (and possibly a quote fetch);
                # the manager UI shows the condition summary instead.
                item["member_count"] = 0
                item["history_from"] = str((item.get("metadata") or {}).get("snapshot_as_of") or "")
        return items

    def get_universe(self, user_id: int, universe_id: int) -> dict:
        with get_db_connection() as db:
            row = self._get_visible_universe(db, user_id, universe_id)
            cur = db.cursor()
            cur.execute("SELECT MIN(valid_from) AS history_from FROM qd_universe_members WHERE universe_id = ?", (universe_id,))
            row = dict(row, **(cur.fetchone() or {}))
            cur.close()
        if row.get("source") == SMART_SOURCE:
            # Resolving a smart pool can hit the live quote snapshot; the
            # universe detail view only needs the stored conditions.
            return _serialize_universe(row)
        row['member_count'] = len(self.resolve_members(user_id, universe_id))
        return _serialize_universe(row)

    def create_manual(self, user_id: int, payload: dict) -> dict:
        name = str((payload or {}).get("name") or "").strip()
        market = str((payload or {}).get("market") or "").strip()
        if not name or len(name) > 160:
            raise UniverseError("universe.invalidName")
        if market not in VALID_MARKETS and market != "Mixed":
            raise UniverseError("universe.invalidMarket")
        default_market = "" if market == "Mixed" else market
        members = normalize_members((payload or {}).get("members") or [], default_market=default_market)
        code = f"{normalize_universe_code(name)}-{uuid.uuid4().hex[:10]}"
        metadata = (payload or {}).get("metadata") if isinstance((payload or {}).get("metadata"), dict) else {}

        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                INSERT INTO qd_universes
                  (user_id, code, name, market, universe_type, source, is_system, status, metadata_json)
                VALUES (?, ?, ?, ?, 'manual', 'manual', FALSE, 'active', ?)
                RETURNING id
                """,
                (int(user_id), code, name, market, json.dumps(metadata, ensure_ascii=False)),
            )
            created = cur.fetchone() or {}
            universe_id = int(created.get("id") or 0)
            self._replace_static_members(cur, universe_id, members)
            db.commit()
            cur.close()
        return self.get_universe(user_id, universe_id)

    def create_smart(self, user_id: int, payload: dict) -> dict:
        """Create a condition-backed universe (a saved tag screen).

        Conditions live in ``metadata_json.conditions``; membership is resolved
        on read, so the pool tracks tag refreshes instead of freezing a list.
        The oldest non-point-in-time tag date is stored as ``history_from`` so a
        backtest starting before the snapshot is rejected, not silently given
        present-day members.
        """
        from app.services.symbol_tags import (
            SymbolTagError,
            normalize_conditions,
            tag_conditions_floor,
        )

        name = str((payload or {}).get("name") or "").strip()
        market = str((payload or {}).get("market") or "CNStock").strip()
        if not name or len(name) > 160:
            raise UniverseError("universe.invalidName")
        if market not in VALID_MARKETS and market != "Mixed":
            raise UniverseError("universe.invalidMarket")
        try:
            conditions = normalize_conditions((payload or {}).get("conditions") or [])
            floor = tag_conditions_floor(conditions)
        except SymbolTagError as exc:
            raise UniverseError(exc.code) from exc
        metadata = {
            "source": SMART_SOURCE,
            "conditions": conditions,
            "point_in_time": not floor,
            "snapshot_only": bool(floor),
            "snapshot_as_of": floor,
        }
        code = f"{normalize_universe_code(name)}-{uuid.uuid4().hex[:10]}"
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                INSERT INTO qd_universes
                  (user_id, code, name, market, universe_type, source, is_system,
                   status, metadata_json)
                VALUES (?, ?, ?, ?, 'smart', ?, FALSE, 'active', ?::jsonb)
                RETURNING id
                """,
                (int(user_id), code, name, market, SMART_SOURCE,
                 json.dumps(metadata, ensure_ascii=False)),
            )
            created = cur.fetchone() or {}
            universe_id = int(created.get("id") or 0)
            db.commit()
            cur.close()
        return self.get_universe(user_id, universe_id)

    def update_smart(self, user_id: int, universe_id: int, payload: dict) -> dict:
        from app.services.symbol_tags import (
            SymbolTagError,
            normalize_conditions,
            tag_conditions_floor,
        )

        with get_db_connection() as db:
            universe = self._get_visible_universe(db, user_id, universe_id, require_owner=True)
            if str(universe.get("universe_type")) != "smart":
                raise UniverseError("universe.readOnly", status_code=409)
            metadata = _json_value(universe.get("metadata_json"), {})
            name = str((payload or {}).get("name") or universe.get("name") or "").strip()
            if not name or len(name) > 160:
                raise UniverseError("universe.invalidName")
            raw_conditions = (payload or {}).get("conditions")
            if raw_conditions is None:
                conditions = metadata.get("conditions") or []
            else:
                try:
                    conditions = normalize_conditions(raw_conditions)
                except SymbolTagError as exc:
                    raise UniverseError(exc.code) from exc
            try:
                floor = tag_conditions_floor(conditions)
            except SymbolTagError as exc:
                raise UniverseError(exc.code) from exc
            metadata.update({
                "source": SMART_SOURCE,
                "conditions": conditions,
                "point_in_time": not floor,
                "snapshot_only": bool(floor),
                "snapshot_as_of": floor,
            })
            cur = db.cursor()
            cur.execute(
                "UPDATE qd_universes SET name = ?, metadata_json = ?::jsonb, updated_at = NOW() WHERE id = ?",
                (name, json.dumps(metadata, ensure_ascii=False), int(universe_id)),
            )
            db.commit()
            cur.close()
        return self.get_universe(user_id, universe_id)

    def clone_system(self, user_id: int, universe_id: int, *, name: str = "") -> dict:
        with get_db_connection() as db:
            source = self._get_visible_universe(db, user_id, universe_id)
        if not source.get("is_system"):
            raise UniverseError("universe.cloneSystemOnly", status_code=409)
        members = self.resolve_members(user_id, universe_id)
        clone_name = str(name or source.get("name") or source.get("code") or "").strip()
        if not clone_name:
            raise UniverseError("universe.invalidName")
        return self.create_manual(user_id, {
            "name": clone_name,
            "market": source.get("market") or "Mixed",
            "members": members,
            "metadata": {
                "cloned_from_universe_id": int(universe_id),
                "cloned_from_code": source.get("code") or "",
                "cloned_at": datetime.now(timezone.utc).isoformat(),
            },
        })

    def system_overview(self) -> dict:
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT u.*,
                       COUNT(m.id) FILTER (
                         WHERE m.valid_from <= CURRENT_DATE
                           AND (m.valid_to IS NULL OR m.valid_to > CURRENT_DATE)
                       ) AS member_count,
                       MIN(m.valid_from) AS history_from,
                       MAX(COALESCE(m.source_version, '')) AS source_version
                FROM qd_universes u
                LEFT JOIN qd_universe_members m ON m.universe_id = u.id
                WHERE u.is_system = TRUE
                  AND u.status <> 'deprecated'
                  AND u.universe_type <> 'watchlist'
                GROUP BY u.id
                ORDER BY u.id
                """
            )
            rows = cur.fetchall() or []
            cur.close()
        items = []
        for row in rows:
            item = _serialize_universe(row)
            if item.get("source") == "symbol_master":
                item["member_count"] = len(self._symbol_master_members(item.get("source_ref") or ""))
            item["history_from"] = _iso(row.get("history_from"))
            item["source_version"] = str(row.get("source_version") or "")
            items.append(item)
        return {
            "universes": items,
            "syncable_codes": [
                "csi300", "csi500", "sp500", "nasdaq100", "crypto_top100",
                "hk_etf", "us_etf",
                "hk_hsi_core50", "hk_tech30", "hk_china_enterprises50", "hk_high_dividend50",
            ],
        }

    def replace_manual_members(self, user_id: int, universe_id: int, raw_members: list) -> list[dict]:
        with get_db_connection() as db:
            universe = self._get_visible_universe(db, user_id, universe_id, require_owner=True)
            if universe.get("universe_type") not in EDITABLE_UNIVERSE_TYPES:
                raise UniverseError("universe.readOnly", status_code=409)
            default_market = "" if universe.get("market") == "Mixed" else str(universe.get("market") or "")
            members = normalize_members(raw_members, default_market=default_market)
            cur = db.cursor()
            self._replace_static_members(cur, int(universe_id), members)
            cur.execute("UPDATE qd_universes SET updated_at = NOW() WHERE id = ?", (int(universe_id),))
            db.commit()
            cur.close()
        return self.resolve_members(user_id, universe_id, as_of=STATIC_START)

    def upsert_system_universe(
        self,
        *,
        code: str,
        name: str,
        members: list,
        source: str,
        source_ref: str = "",
        metadata: dict | None = None,
        market: str = "CNStock",
        universe_type: str = "market",
        valid_from: date | None = None,
    ) -> dict:
        """Create/refresh one system universe and swap its members wholesale.

        ``valid_from`` is the earliest date the snapshot may be used for; board
        snapshots pass the start of their stored history so a backtest before
        that date is rejected instead of silently seeing present-day members.
        """
        normalized_code = normalize_universe_code(code)
        if not normalized_code:
            raise UniverseError("universe.invalidCode")
        normalized_members = normalize_members(members, default_market=market)
        payload = json.dumps(dict(metadata or {}), ensure_ascii=False)
        start = valid_from or STATIC_START
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                INSERT INTO qd_universes
                  (user_id, code, name, name_i18n_key, market, universe_type,
                   source, source_ref, is_system, status, metadata_json)
                VALUES
                  (NULL, ?, ?, '', ?, ?, ?, ?, TRUE, 'active', ?::jsonb)
                ON CONFLICT (code) WHERE is_system = TRUE
                DO UPDATE SET
                  user_id = NULL,
                  name = EXCLUDED.name,
                  market = EXCLUDED.market,
                  universe_type = EXCLUDED.universe_type,
                  source = EXCLUDED.source,
                  source_ref = EXCLUDED.source_ref,
                  status = EXCLUDED.status,
                  metadata_json = EXCLUDED.metadata_json,
                  updated_at = NOW()
                RETURNING id
                """,
                (normalized_code, str(name)[:160], market, universe_type, source, str(source_ref)[:160], payload),
            )
            row = cur.fetchone() or {}
            universe_id = int(row.get("id") or 0)
            if not universe_id:
                raise UniverseError("universe.upsertFailed", status_code=500)
            self._replace_static_members(cur, universe_id, normalized_members, valid_from=start)
            db.commit()
            cur.close()
        return {
            "universe_id": universe_id,
            "code": normalized_code,
            "name": str(name),
            "source_ref": str(source_ref),
            "members": len(normalized_members),
            "valid_from": start.isoformat(),
        }

    def resolve_members(self, user_id: int, universe_id: int, *, as_of: Any = None) -> list[dict]:
        as_of_date = parse_as_of(as_of)
        with get_db_connection() as db:
            universe = self._get_visible_universe(db, user_id, universe_id)
            cur = db.cursor()
            if universe.get("universe_type") == "watchlist":
                cur.execute(
                    """
                    SELECT market, symbol, name, exchange_id, market_type,
                           instrument_id, settle_currency
                    FROM qd_watchlist
                    WHERE user_id = ?
                    ORDER BY market, symbol, exchange_id, market_type, instrument_id
                    """,
                    (int(user_id),),
                )
            elif universe.get("source") == SMART_SOURCE:
                cur.close()
                return self._smart_members(universe, as_of=as_of_date)
            elif universe.get("source") == "symbol_master":
                cur.close()
                return self._symbol_master_members(str(universe.get("source_ref") or ""))
            else:
                cur.execute(
                    """
                    SELECT market, symbol, name, exchange_id, market_type,
                           instrument_id, settle_currency,
                           member_weight AS weight, member_rank AS rank,
                           valid_from, valid_to, source_version, metadata_json AS metadata
                    FROM qd_universe_members
                    WHERE universe_id = ?
                      AND valid_from <= ?
                      AND (valid_to IS NULL OR valid_to > ?)
                    ORDER BY market, symbol, exchange_id, market_type, instrument_id
                    """,
                    (int(universe_id), as_of_date, as_of_date),
                )
            rows = cur.fetchall() or []
            cur.close()
        return [_serialize_member(row) for row in rows]

    @staticmethod
    def _smart_members(universe: dict, *, as_of: date, start: date | None = None) -> list[dict]:
        """Resolve a smart universe's stored tag conditions into members.

        Event conditions are evaluated across ``[start, as_of]`` so a backtest
        sees every symbol that was on the board during its window, matching
        ``candidate_members`` semantics for stored pools.
        """
        from app.services.symbol_tags import get_symbol_tag_service

        conditions = (_json_value(universe.get("metadata_json"), {}) or {}).get("conditions") or []
        if not conditions:
            return []
        result = get_symbol_tag_service().screen(
            conditions,
            as_of=as_of,
            date_range=[start or as_of, as_of],
            limit=MAX_MANUAL_MEMBERS,
            with_quotes=False,
        )
        return [
            {
                "market": item["market"],
                "symbol": item["symbol"],
                "name": item.get("name") or "",
                "exchange_id": "",
                "market_type": "spot",
                "instrument_id": "",
                "settle_currency": "",
                "weight": None,
                "rank": item.get("rank"),
                "source_version": str(universe.get("code") or ""),
                "valid_from": None,
                "valid_to": None,
                "metadata": {"tags": item.get("tags") or []},
            }
            for item in result["items"]
        ]

    @staticmethod
    def _symbol_master_members(source_ref: str) -> list[dict]:
        parts = str(source_ref or "").split(":")
        if len(parts) != 3:
            return []
        market, scope, asset_class = parts
        query = """
            SELECT market, symbol, name, exchange AS exchange_id, market_type,
                   instrument_id, settle_currency
            FROM qd_market_symbols
            WHERE market = ? AND is_active = 1 AND asset_class = ?
        """
        if scope == "hot":
            query += " AND is_hot = 1"
        query += " ORDER BY sort_order DESC, symbol"
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(query, (market, asset_class))
            rows = cur.fetchall() or []
            cur.close()
        return [_serialize_member(row) for row in rows]

    def candidate_members(
        self,
        user_id: int,
        universe_id: int,
        *,
        start: Any,
        end: Any,
    ) -> list[dict]:
        """Return the union of members active at any time in a date range."""
        start_date = parse_as_of(start)
        end_date = parse_as_of(end)
        if end_date < start_date:
            raise UniverseError("universe.invalidDateRange")
        with get_db_connection() as db:
            universe = self._get_visible_universe(db, user_id, universe_id)
        if universe.get("source") == SMART_SOURCE:
            return self._smart_members(universe, as_of=end_date, start=start_date)
        if universe.get("universe_type") == "watchlist" or universe.get("source") == "symbol_master":
            return self.resolve_members(user_id, universe_id, as_of=end_date)
        with get_db_connection() as db:
            cur = db.cursor()
            cur.execute(
                """
                SELECT market, symbol, name, exchange_id, market_type,
                       instrument_id, settle_currency,
                       member_weight AS weight, member_rank AS rank,
                       valid_from, valid_to, source_version, metadata_json AS metadata
                FROM qd_universe_members
                WHERE universe_id = ?
                  AND valid_from <= ?
                  AND (valid_to IS NULL OR valid_to > ?)
                ORDER BY market, symbol, exchange_id, market_type, instrument_id, valid_from
                """,
                (int(universe_id), end_date, start_date),
            )
            rows = cur.fetchall() or []
            cur.close()
        deduped = {}
        for row in rows:
            member = _serialize_member(row)
            key = (
                member["market"], member["symbol"], member["exchange_id"],
                member["market_type"], member["instrument_id"],
            )
            if key not in deduped:
                deduped[key] = member
        return [deduped[key] for key in sorted(deduped)]

    def create_snapshot(self, user_id: int, universe_id: int, *, as_of: Any = None) -> dict:
        as_of_date = parse_as_of(as_of)
        members = self.resolve_members(user_id, universe_id, as_of=as_of_date)
        content_hash = member_content_hash(members)
        snapshot_id = str(uuid.uuid4())
        source_versions = sorted({
            str(member.get("source_version") or "") for member in members
            if member.get("source_version")
        })
        source_version = ",".join(source_versions)[:120]

        with get_db_connection() as db:
            self._get_visible_universe(db, user_id, universe_id)
            cur = db.cursor()
            cur.execute(
                """
                INSERT INTO qd_universe_snapshots
                  (snapshot_id, universe_id, user_id, as_of_date, source_version,
                   content_hash, member_count, members_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (universe_id, user_id, as_of_date, content_hash)
                DO UPDATE SET source_version = excluded.source_version
                RETURNING snapshot_id, created_at
                """,
                (
                    snapshot_id, int(universe_id), int(user_id), as_of_date,
                    source_version, content_hash, len(members),
                    json.dumps(members, ensure_ascii=False),
                ),
            )
            row = cur.fetchone() or {}
            db.commit()
            cur.close()
        return {
            "snapshot_id": str(row.get("snapshot_id") or snapshot_id),
            "universe_id": int(universe_id),
            "as_of": as_of_date.isoformat(),
            "content_hash": content_hash,
            "member_count": len(members),
            "members": members,
            "source_version": source_version,
            "created_at": _iso(row.get("created_at")),
        }

    @staticmethod
    def _get_visible_universe(db, user_id: int, universe_id: int, *, require_owner: bool = False) -> dict:
        cur = db.cursor()
        if require_owner:
            cur.execute(
                "SELECT * FROM qd_universes WHERE id = ? AND user_id = ? AND is_system = FALSE",
                (int(universe_id), int(user_id)),
            )
        else:
            cur.execute(
                "SELECT * FROM qd_universes WHERE id = ? AND (is_system = TRUE OR user_id = ?)",
                (int(universe_id), int(user_id)),
            )
        row = cur.fetchone()
        cur.close()
        if not row:
            raise UniverseError("universe.notFound", status_code=404)
        return row

    @staticmethod
    def _replace_static_members(cur, universe_id: int, members: list[dict], *, valid_from: date | None = None) -> None:
        start = valid_from or STATIC_START
        cur.execute("DELETE FROM qd_universe_members WHERE universe_id = ?", (int(universe_id),))
        for member in members:
            cur.execute(
                """
                INSERT INTO qd_universe_members
                  (universe_id, market, symbol, name, exchange_id, market_type,
                   instrument_id, settle_currency, valid_from, valid_to,
                   member_weight, member_rank, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    int(universe_id), member["market"], member["symbol"], member["name"],
                    member["exchange_id"], member["market_type"], member["instrument_id"],
                    member["settle_currency"], start, member.get("weight"),
                    member.get("rank"), json.dumps(member.get("metadata") or {}, ensure_ascii=False),
                ),
            )


def _serialize_universe(row: dict) -> dict:
    return {
        "id": int(row.get("id") or 0),
        "user_id": int(row.get("user_id")) if row.get("user_id") is not None else None,
        "code": str(row.get("code") or ""),
        "name": str(row.get("name") or ""),
        "name_i18n_key": str(row.get("name_i18n_key") or ""),
        "market": str(row.get("market") or ""),
        "universe_type": str(row.get("universe_type") or ""),
        "source": str(row.get("source") or ""),
        "source_ref": str(row.get("source_ref") or ""),
        "is_system": bool(row.get("is_system")),
        "status": str(row.get("status") or ""),
        "member_count": int(row.get("member_count") or 0),
        "history_from": _iso(row.get("history_from")),
        "metadata": _json_value(row.get("metadata_json"), {}),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _serialize_member(row: dict) -> dict:
    return {
        "market": str(row.get("market") or ""),
        "symbol": str(row.get("symbol") or ""),
        "name": str(row.get("name") or ""),
        "exchange_id": str(row.get("exchange_id") or ""),
        "market_type": str(row.get("market_type") or ""),
        "instrument_id": str(row.get("instrument_id") or ""),
        "settle_currency": str(row.get("settle_currency") or ""),
        "weight": _optional_float(row.get("weight")),
        "rank": _optional_int(row.get("rank")),
        "source_version": str(row.get("source_version") or ""),
        "valid_from": _iso(row.get("valid_from")),
        "valid_to": _iso(row.get("valid_to")),
        "metadata": _json_value(row.get("metadata"), {}),
    }


def _json_value(value: Any, fallback: Any) -> Any:
    if value is None or value == "":
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise UniverseError("universe.invalidWeight") from exc


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise UniverseError("universe.invalidRank") from exc


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


_service: Optional[UniverseService] = None


def get_universe_service() -> UniverseService:
    global _service
    if _service is None:
        _service = UniverseService()
    return _service
