"""Sync Tonghuashun industry and concept universes from HiThink Financial API.

This module is mounted into the backend container and can be run either by the
API process or from a short-lived container. It creates one system universe per
industry/concept and stores the current constituent snapshot as membership.

The source endpoint returns present-day constituents, not point-in-time
membership. Each universe therefore carries ``snapshot_only`` and
``snapshot_as_of`` metadata so the backtest readiness check prevents accidental
historical use.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from typing import Any, Iterable

import requests

ROOT = "/app"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.utils.db import get_db_connection
from app.utils.logger import get_logger


logger = get_logger(__name__)

SOURCE = "hithink_finance"
SOURCE_NAME = "HiThink Financial API"
SYSTEM_MARKET = "CNStock"
UNIVERSE_TYPE = "market"

DEFAULT_BASE_URL = "https://fuyao.aicubes.cn"
CATALOG_PATH = "/api/a-share-index/catalog/ths-index-list"
CONSTITUENTS_PATH = "/api/a-share-index/constituents/ths-stock-list"

TAGS = {
    "industry": ("cn_industry", "Industry"),
    "cn_concept": ("cn_concept", "Concept"),
}

_CODE_RE = re.compile(r"[^a-z0-9]+")
_THSCODE_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$", re.IGNORECASE)


class SectorSyncError(RuntimeError):
    pass


def _api_key() -> str:
    value = str(os.getenv("HITHINK_FINANCE_API_KEY") or "").strip()
    if not value:
        raise SectorSyncError(
            "HITHINK_FINANCE_API_KEY is not configured in backend.env"
        )
    return value


def _base_url() -> str:
    return (
        str(os.getenv("HITHINK_FINANCE_BASE_URL") or DEFAULT_BASE_URL)
        .strip()
        .rstrip("/")
    )


def _request(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.get(
        f"{_base_url()}{path}",
        params=params or {},
        headers={
            "X-api-key": _api_key(),
            "Accept": "application/json",
            "User-Agent": "QuantDinger/4.0",
        },
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    if int(payload.get("code") or 0) != 0:
        raise SectorSyncError(
            f"{SOURCE_NAME} request failed: {payload.get('code')} "
            f"{payload.get('message') or 'unknown error'}"
        )
    return payload


def fetch_catalog(tag: str) -> list[dict[str, str]]:
    payload = _request(CATALOG_PATH, {"tag": tag})
    items = ((payload.get("data") or {}).get("item") or [])
    output: list[dict[str, str]] = []
    for item in items:
        thscode = str(item.get("thscode") or "").strip().upper()
        name = str(item.get("name") or "").strip()
        if not thscode or not name:
            continue
        output.append({"thscode": thscode, "name": name})
    return sorted(output, key=lambda item: (item["name"], item["thscode"]))


def fetch_constituents(thscode: str) -> list[dict[str, Any]]:
    payload = _request(CONSTITUENTS_PATH, {"thscode": thscode})
    items = ((payload.get("data") or {}).get("item") or [])
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        raw_thscode = str(item.get("thscode") or "").strip().upper()
        ticker = str(item.get("ticker") or "").strip()
        name = str(item.get("name") or "").strip()
        match = _THSCODE_RE.match(raw_thscode)
        if not match:
            continue
        symbol = f"{match.group(1)}.{match.group(2).upper()}"
        if symbol in seen:
            continue
        seen.add(symbol)
        output.append({
            "market": SYSTEM_MARKET,
            "symbol": symbol,
            "name": name or ticker or symbol,
            "exchange_id": "",
            "market_type": "spot",
            "instrument_id": raw_thscode,
            "settle_currency": "CNY",
            "rank": len(output) + 1,
            "metadata": {
                "source": SOURCE,
                "source_thscode": raw_thscode,
                "source_ticker": ticker,
            },
        })
    return output


def _universe_code(prefix: str, thscode: str) -> str:
    suffix = _CODE_RE.sub("_", thscode.lower()).strip("_")
    return f"{prefix}_{suffix}"[:80]


def _replace_members(cur: Any, universe_id: int, members: list[dict[str, Any]]) -> None:
    cur.execute(
        "DELETE FROM qd_universe_members WHERE universe_id = ?",
        (int(universe_id),),
    )
    for member in members:
        cur.execute(
            """
            INSERT INTO qd_universe_members
              (universe_id, market, symbol, name, exchange_id, market_type,
               instrument_id, settle_currency, valid_from, valid_to,
               member_weight, member_rank, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?::jsonb)
            """,
            (
                int(universe_id),
                member["market"],
                member["symbol"],
                member["name"],
                member["exchange_id"],
                member["market_type"],
                member["instrument_id"],
                member["settle_currency"],
                date(1900, 1, 1),
                member.get("rank"),
                json.dumps(member.get("metadata") or {}, ensure_ascii=False),
            ),
        )


def upsert_universe(
    *,
    tag: str,
    thscode: str,
    display_name: str,
    members: list[dict[str, Any]],
    as_of: date,
) -> dict[str, Any]:
    prefix, label = TAGS[tag]
    code = _universe_code(prefix, thscode)
    name = f"{label}: {display_name}"
    metadata = {
        "source": SOURCE,
        "source_name": SOURCE_NAME,
        "tag": tag,
        "thscode": thscode,
        "snapshot_only": True,
        "snapshot_as_of": as_of.isoformat(),
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "member_count": len(members),
    }
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO qd_universes
              (user_id, code, name, name_i18n_key, market, universe_type,
               source, source_ref, is_system, status, metadata_json)
            VALUES
              (NULL, ?, ?, '', 'CNStock', 'market', ?, ?, TRUE, 'active', ?::jsonb)
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
            (
                code,
                name,
                SOURCE,
                thscode,
                json.dumps(metadata, ensure_ascii=False),
            ),
        )
        row = cur.fetchone() or {}
        universe_id = int(row.get("id") or 0)
        if not universe_id:
            raise SectorSyncError(f"failed to upsert universe {code}")
        _replace_members(cur, universe_id, members)
        db.commit()
        cur.close()
    return {
        "universe_id": universe_id,
        "code": code,
        "name": name,
        "thscode": thscode,
        "members": len(members),
    }


def _selected_sectors(
    tag: str,
    *,
    only: Iterable[str],
    limit: int | None,
) -> list[dict[str, str]]:
    catalog = fetch_catalog(tag)
    wanted = {str(item).strip().upper() for item in only if str(item).strip()}
    if wanted:
        catalog = [
            item for item in catalog
            if item["thscode"] in wanted or item["name"] in wanted
        ]
    if limit is not None:
        catalog = catalog[: max(0, int(limit))]
    return catalog


def sync(
    *,
    tags: Iterable[str],
    only: Iterable[str],
    limit: int | None,
    as_of: date,
    dry_run: bool,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for tag in tags:
        if tag not in TAGS:
            raise SectorSyncError(f"unsupported tag: {tag}")
        sectors = _selected_sectors(tag, only=only, limit=limit)
        logger.info(
            "Syncing %s sectors from %s: catalog=%s",
            tag,
            SOURCE_NAME,
            len(sectors),
        )
        for index, sector in enumerate(sectors, start=1):
            thscode = sector["thscode"]
            name = sector["name"]
            try:
                members = fetch_constituents(thscode)
                if not members:
                    raise SectorSyncError("empty constituent list")
                if dry_run:
                    results.append({
                        "code": _universe_code(TAGS[tag][0], thscode),
                        "name": f"{TAGS[tag][1]}: {name}",
                        "thscode": thscode,
                        "members": len(members),
                    })
                else:
                    results.append(upsert_universe(
                        tag=tag,
                        thscode=thscode,
                        display_name=name,
                        members=members,
                        as_of=as_of,
                    ))
                if index % 25 == 0:
                    logger.info("%s sync progress: %s/%s", tag, index, len(sectors))
            except Exception as exc:
                logger.exception("Sector sync failed tag=%s thscode=%s", tag, thscode)
                failures.append({
                    "tag": tag,
                    "thscode": thscode,
                    "name": name,
                    "error": str(exc)[:300],
                })
    return {
        "source": SOURCE,
        "as_of": as_of.isoformat(),
        "dry_run": bool(dry_run),
        "synced": len(results),
        "failed": len(failures),
        "results": results,
        "failures": failures,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync THS industry/concept universes from HiThink Financial API"
    )
    parser.add_argument(
        "--tag",
        action="append",
        choices=sorted(TAGS),
        help="catalog tag to sync; repeatable (default: industry and cn_concept)",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="limit to a thscode or display name; repeatable",
    )
    parser.add_argument("--limit", type=int, default=None, help="maximum sectors per tag")
    parser.add_argument("--as-of", default=date.today().isoformat(), help="snapshot date")
    parser.add_argument("--dry-run", action="store_true", help="fetch without writing")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    tags = args.tag or list(TAGS)
    as_of = date.fromisoformat(str(args.as_of))
    report = sync(
        tags=tags,
        only=args.only,
        limit=args.limit,
        as_of=as_of,
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
