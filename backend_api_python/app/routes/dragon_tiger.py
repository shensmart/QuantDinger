"""Eastmoney-style dragon-tiger (龙虎榜) views.

The board reads like the pages Chinese users already know (东方财富数据中心 →
龙虎榜): a per-day board with 上榜原因, 机构买卖情况, 每日活跃营业部, and the
近一月/三月/六月/一年 rolling 统计 tables.

Data comes from ``app.data_sources.eastmoney_lhb`` (AkShare → Eastmoney). It is
read on demand and cached briefly per (view, date, window), because these frames
are large and change at most once per trading day.

Endpoints:
- GET /api/market-events/dragon-tiger/daily        - one day, every listing
- GET /api/market-events/dragon-tiger/institutions - 机构买卖情况
- GET /api/market-events/dragon-tiger/branches     - 每日活跃营业部
- GET /api/market-events/dragon-tiger/statistics   - 个股龙虎榜统计 (rolling)
- GET /api/market-events/dragon-tiger/seat-tracking   - 机构席位买卖追踪 (rolling)
- GET /api/market-events/dragon-tiger/branch-ranking - 证券营业部上榜统计 (rolling)
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from flask import jsonify, request

from app.data_sources import eastmoney_lhb
from app.data_sources import hithink_finance as hithink
from app.openapi.blueprint import HumanBlueprint as Blueprint
from app.utils.auth import login_required
from app.utils.logger import get_logger

logger = get_logger(__name__)
dragon_tiger_blp = Blueprint("dragon_tiger", __name__)

CACHE_TTL_SECONDS = 180
_MAX_ENTRIES = 64

_cache: Dict[str, Any] = {}
_cache_lock = threading.Lock()


def _success(data=None, *, status: int = 200):
    return jsonify({"code": 1, "msg": "success", "data": data}), status


def _failure(message: str, *, status: int = 400):
    return jsonify({"code": 0, "msg": message, "data": None}), status


def _cached(key: str, producer: Callable[[], List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    now = time.time()
    with _cache_lock:
        entry = _cache.get(key)
        if entry and entry[0] > now:
            return entry[1]
    rows = producer()
    with _cache_lock:
        if len(_cache) >= _MAX_ENTRIES:
            for stale in [name for name, value in _cache.items() if value[0] <= now]:
                _cache.pop(stale, None)
            if len(_cache) >= _MAX_ENTRIES:
                _cache.pop(next(iter(_cache)), None)
        _cache[key] = (now + CACHE_TTL_SECONDS, rows)
    return rows


def _default_trade_date() -> str:
    """Empty request -> most recent trade date already stored in the event table."""
    from app.services import events_data

    latest = events_data.latest_trade_dates(["dragon_tiger_all"])
    found = latest.get("dragon_tiger_all")
    if found:
        return found.isoformat()
    return hithink.shanghai_today().isoformat()


def _trade_date_arg() -> Optional[str]:
    raw = (request.args.get("trade_date") or request.args.get("date") or "").strip()
    if not raw:
        return _default_trade_date()
    parsed = hithink.parse_trade_date(raw)
    return parsed.isoformat() if parsed else None


def _limit_arg(default: int = 200) -> int:
    try:
        value = int(request.args.get("limit"))
    except (TypeError, ValueError):
        return default
    return max(1, min(2000, value))


def _window_arg() -> str:
    return (request.args.get("window") or "近一月").strip()


@dragon_tiger_blp.route("/dragon-tiger/daily", methods=["GET"])
@login_required
def dragon_tiger_daily():
    """Every dragon-tiger listing of one trade date, with 上榜原因."""
    day = _trade_date_arg()
    if not day:
        return _failure("invalid trade_date")
    rows = _cached(f"daily:{day}", lambda: eastmoney_lhb.daily_detail(day))
    limit = _limit_arg(500)
    return _success({"trade_date": day, "source": eastmoney_lhb.SOURCE, "total": len(rows), "items": rows[:limit]})


@dragon_tiger_blp.route("/dragon-tiger/institutions", methods=["GET"])
@login_required
def dragon_tiger_institutions():
    """机构买卖情况: institutional seats per stock for one trade date."""
    day = _trade_date_arg()
    if not day:
        return _failure("invalid trade_date")
    rows = _cached(f"seats:{day}", lambda: eastmoney_lhb.institutional_seats(day))
    return _success({"trade_date": day, "source": eastmoney_lhb.SOURCE, "total": len(rows), "items": rows[:_limit_arg(500)]})


@dragon_tiger_blp.route("/dragon-tiger/branches", methods=["GET"])
@login_required
def dragon_tiger_branches():
    """每日活跃营业部: per-branch activity for one trade date."""
    day = _trade_date_arg()
    if not day:
        return _failure("invalid trade_date")
    rows = _cached(f"branches:{day}", lambda: eastmoney_lhb.active_branches(day))
    return _success({"trade_date": day, "source": eastmoney_lhb.SOURCE, "total": len(rows), "items": rows[:_limit_arg(500)]})


@dragon_tiger_blp.route("/dragon-tiger/statistics", methods=["GET"])
@login_required
def dragon_tiger_statistics():
    """个股龙虎榜统计 over 近一月/近三月/近六月/近一年."""
    window = _window_arg()
    if window not in eastmoney_lhb.STATISTIC_WINDOWS:
        return _failure("window must be one of " + "/".join(eastmoney_lhb.STATISTIC_WINDOWS))
    rows = _cached(f"stock-stat:{window}", lambda: eastmoney_lhb.stock_statistics(window))
    return _success({"window": window, "source": eastmoney_lhb.SOURCE, "total": len(rows), "items": rows[:_limit_arg(500)]})


@dragon_tiger_blp.route("/dragon-tiger/seat-tracking", methods=["GET"])
@login_required
def dragon_tiger_seat_tracking():
    """机构席位买卖追踪 over a rolling window."""
    window = _window_arg()
    if window not in eastmoney_lhb.STATISTIC_WINDOWS:
        return _failure("window must be one of " + "/".join(eastmoney_lhb.STATISTIC_WINDOWS))
    rows = _cached(f"inst-stat:{window}", lambda: eastmoney_lhb.institution_statistics(window))
    return _success({"window": window, "source": eastmoney_lhb.SOURCE, "total": len(rows), "items": rows[:_limit_arg(500)]})


@dragon_tiger_blp.route("/dragon-tiger/branch-ranking", methods=["GET"])
@login_required
def dragon_tiger_branch_ranking():
    """证券营业部上榜统计 over a rolling window."""
    window = _window_arg()
    if window not in eastmoney_lhb.STATISTIC_WINDOWS:
        return _failure("window must be one of " + "/".join(eastmoney_lhb.STATISTIC_WINDOWS))
    rows = _cached(f"branch-stat:{window}", lambda: eastmoney_lhb.branch_statistics(window))
    return _success({"window": window, "source": eastmoney_lhb.SOURCE, "total": len(rows), "items": rows[:_limit_arg(500)]})
