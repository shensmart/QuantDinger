"""Read-only A-share market event boards (limit pools, dragon-tiger, hot list)."""
from __future__ import annotations

from flask import request

from app.data_sources import hithink_finance as hithink
from app.services import events_data
from app.utils.agent_auth import SCOPE_R, agent_required

from . import agent_v1_bp
from ._helpers import clip_int, envelope, error


def _date_arg(name: str):
    raw = request.args.get(name)
    if not raw:
        return None
    parsed = hithink.parse_trade_date(raw)
    if parsed is None:
        return "invalid"
    return parsed


def _latest_trade_date(event_types) -> object:
    """Newest stored trade date for the board, so a bare call is not a year dump."""
    latest = events_data.latest_trade_dates(list(event_types))
    return max((value for value in latest.values() if value), default=None)


@agent_v1_bp.route("/events", methods=["GET"])
@agent_required(SCOPE_R)
def list_market_events():
    trade_date = _date_arg("trade_date") or _date_arg("date")
    if trade_date == "invalid":
        return error(400, "invalid trade_date", http=400)
    start = _date_arg("start")
    end = _date_arg("end")
    if start == "invalid" or end == "invalid":
        return error(400, "invalid start/end", http=400)
    event_types = [item.strip() for item in (request.args.get("event_types") or "").split(",") if item.strip()]
    symbols = [item.strip() for item in (request.args.get("symbols") or "").split(",") if item.strip()]
    try:
        page = events_data.query_events(
            event_types=event_types,
            symbols=symbols,
            trade_date=trade_date or None,
            start=start or None,
            end=end or None,
            limit=clip_int(request.args.get("limit"), default=200, lo=1, hi=1000),
            offset=clip_int(request.args.get("offset"), default=0, lo=0, hi=1_000_000),
        )
    except ValueError as exc:
        return error(400, str(exc), http=400)
    return envelope(page)


@agent_v1_bp.route("/events/limit-up", methods=["GET"])
@agent_required(SCOPE_R)
def list_limit_up_pool():
    trade_date = _date_arg("trade_date") or _date_arg("date")
    if trade_date == "invalid":
        return error(400, "invalid trade_date", http=400)
    page = events_data.query_events(
        event_types=["limit_up"],
        trade_date=trade_date or _latest_trade_date(["limit_up"]) or hithink.shanghai_today(),
        limit=clip_int(request.args.get("limit"), default=200, lo=1, hi=1000),
        offset=clip_int(request.args.get("offset"), default=0, lo=0, hi=1_000_000),
    )
    return envelope(page)


@agent_v1_bp.route("/events/dragon-tiger", methods=["GET"])
@agent_required(SCOPE_R)
def list_dragon_tiger():
    trade_date = _date_arg("trade_date") or _date_arg("date")
    if trade_date == "invalid":
        return error(400, "invalid trade_date", http=400)
    board_type = str(request.args.get("board_type") or "all").strip().lower()
    event_type = f"dragon_tiger_{board_type}" if board_type in {"org", "hot_money"} else "dragon_tiger_all"
    page = events_data.query_events(
        event_types=[event_type],
        trade_date=trade_date or _latest_trade_date([event_type]) or hithink.shanghai_today(),
        limit=clip_int(request.args.get("limit"), default=200, lo=1, hi=1000),
        offset=clip_int(request.args.get("offset"), default=0, lo=0, hi=1_000_000),
    )
    page["board_type"] = board_type
    return envelope(page)


@agent_v1_bp.route("/events/hot-list", methods=["GET"])
@agent_required(SCOPE_R)
def list_hot_list():
    trade_date = _date_arg("trade_date") or _date_arg("date")
    if trade_date == "invalid":
        return error(400, "invalid trade_date", http=400)
    page = events_data.query_events(
        event_types=["hot_rank"],
        trade_date=trade_date or _latest_trade_date(["hot_rank"]) or hithink.shanghai_today(),
        limit=clip_int(request.args.get("limit"), default=30, lo=1, hi=1000),
        offset=clip_int(request.args.get("offset"), default=0, lo=0, hi=1_000_000),
    )
    return envelope(page)


@agent_v1_bp.route("/events/coverage", methods=["GET"])
@agent_required(SCOPE_R)
def market_events_coverage():
    return envelope(events_data.coverage())
