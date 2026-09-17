"""A-share market event boards (limit pools, ladder, dragon-tiger, hot list, anomalies).

Read-only views over ``qd_market_events``; syncing happens in the maintenance
worker, so these endpoints never call the upstream API.

Endpoints:
- GET /api/market-events          - query events by type/date/symbol
- GET /api/market-events/limit-up - limit-up pool for a trade date
- GET /api/market-events/ladder   - limit-up ladder (rolling 30 days)
- GET /api/market-events/dragon-tiger - all/org/hot-money board
- GET /api/market-events/hot-list - THS hot list
- GET /api/market-events/anomaly  - per-stock anomaly explanations
- GET /api/market-events/coverage - stored coverage per event type
"""

from __future__ import annotations

from flask import jsonify, request

from app.data_sources import hithink_finance as hithink
from app.openapi.blueprint import HumanBlueprint as Blueprint
from app.services import events_data
from app.utils.auth import login_required
from app.utils.logger import get_logger

logger = get_logger(__name__)
market_events_blp = Blueprint("market_events", __name__)


def _success(data=None, *, status: int = 200):
    return jsonify({"code": 1, "msg": "success", "data": data}), status


def _failure(message: str, *, status: int = 400):
    return jsonify({"code": 0, "msg": message, "data": None}), status


def _int_arg(name: str, default: int, *, lo: int, hi: int) -> int:
    try:
        value = int(request.args.get(name))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def _date_arg(name: str):
    raw = request.args.get(name)
    if not raw:
        return None
    return hithink.parse_trade_date(raw)


def _query(event_types, *, default_limit=200, single_day=False):
    trade_date = _date_arg("trade_date") or _date_arg("date")
    start = _date_arg("start")
    end = _date_arg("end")
    if single_day and trade_date is None and start is None and end is None:
        # Board endpoints answer for one trade date; default to the newest one we
        # hold instead of dumping a year of rows or an empty page.
        latest = events_data.latest_trade_dates(list(event_types))
        trade_date = max((value for value in latest.values() if value), default=None)
    symbols = [item.strip() for item in (request.args.get("symbols") or "").split(",") if item.strip()]
    return events_data.query_events(
        event_types=list(event_types),
        symbols=symbols,
        trade_date=trade_date,
        start=start,
        end=end,
        limit=_int_arg("limit", default_limit, lo=1, hi=1000),
        offset=_int_arg("offset", 0, lo=0, hi=1_000_000),
    )


@market_events_blp.route("", methods=["GET"])
@login_required
def list_market_events():
    types = [item.strip() for item in (request.args.get("event_types") or "").split(",") if item.strip()]
    unknown = sorted(set(types) - set(events_data.EVENT_TYPES))
    if unknown:
        return _failure(f"unknown event types: {','.join(unknown)}")
    try:
        return _success(_query(types or events_data.EVENT_TYPES))
    except Exception:
        logger.exception("market events query failed")
        return _failure("marketEvents.queryFailed", status=500)


@market_events_blp.route("/limit-up", methods=["GET"])
@login_required
def limit_up_pool():
    return _success(_query(["limit_up"], single_day=True))


@market_events_blp.route("/ladder", methods=["GET"])
@login_required
def limit_up_ladder():
    return _success(_query(["limit_up_ladder"], default_limit=300, single_day=True))


@market_events_blp.route("/dragon-tiger", methods=["GET"])
@login_required
def dragon_tiger():
    board_type = str(request.args.get("board_type") or "all").strip().lower()
    if board_type not in {"all", "org", "hot_money"}:
        return _failure("board_type must be all/org/hot_money")
    event_type = "dragon_tiger_all" if board_type == "all" else f"dragon_tiger_{board_type}"
    payload = _query([event_type], default_limit=1000, single_day=True)
    payload["board_type"] = board_type
    return _success(payload)


@market_events_blp.route("/hot-list", methods=["GET"])
@login_required
def hot_list():
    return _success(_query(["hot_rank"], default_limit=30, single_day=True))


@market_events_blp.route("/anomaly", methods=["GET"])
@login_required
def anomaly_list():
    return _success(_query(["anomaly"], single_day=True))


@market_events_blp.route("/coverage", methods=["GET"])
@login_required
def market_events_coverage():
    return _success(events_data.coverage())
