"""Symbol tag APIs: catalogue, membership, screening, and smart-universe saving."""

from flask import g, jsonify, request

from app.openapi.blueprint import HumanBlueprint as Blueprint
from app.services.symbol_tags import (
    MATERIALIZED_CATEGORIES,
    SymbolTagError,
    get_symbol_tag_service,
    normalize_conditions,
    tag_conditions_floor,
)
from app.services.universe import UniverseError, get_universe_service
from app.utils.auth import admin_required, login_required
from app.utils.logger import get_logger


logger = get_logger(__name__)
tags_blp = Blueprint("tags", __name__)


def _success(data=None, *, status: int = 200):
    return jsonify({"code": 1, "msg": "success", "data": data}), status


def _failure(exc):
    return jsonify({"code": 0, "msg": exc.code, "data": None}), exc.status_code


@tags_blp.route("", methods=["GET"])
@login_required
def list_tags():
    try:
        items = get_symbol_tag_service().list_tags(
            category=request.args.get("category") or "",
            keyword=request.args.get("keyword") or "",
        )
        return _success({"items": items, "count": len(items)})
    except SymbolTagError as exc:
        return _failure(exc)
    except Exception:
        logger.exception("list symbol tags failed")
        return jsonify({"code": 0, "msg": "symbolTag.listFailed", "data": None}), 500


@tags_blp.route("/<string:code>/members", methods=["GET"])
@login_required
def tag_members(code: str):
    try:
        members = get_symbol_tag_service().resolve_members(
            code,
            as_of=request.args.get("as_of") or request.args.get("asOf"),
            limit=int(request.args.get("limit") or 0),
        )
        return _success({"tag_code": code, "members": members, "count": len(members)})
    except SymbolTagError as exc:
        return _failure(exc)
    except ValueError:
        return jsonify({"code": 0, "msg": "symbolTag.invalidLimit", "data": None}), 400
    except Exception:
        logger.exception("tag members failed code=%s", code)
        return jsonify({"code": 0, "msg": "symbolTag.membersFailed", "data": None}), 500


@tags_blp.route("/screen", methods=["POST"])
@login_required
def screen_tags():
    try:
        payload = request.get_json(silent=True) or {}
        result = get_symbol_tag_service().screen(
            payload.get("conditions") or [],
            as_of=payload.get("as_of") or payload.get("asOf"),
            date_range=payload.get("date_range") or payload.get("dateRange"),
            limit=payload.get("limit") or 0,
            offset=payload.get("offset") or 0,
        )
        return _success(result)
    except SymbolTagError as exc:
        return _failure(exc)
    except Exception:
        logger.exception("tag screen failed")
        return jsonify({"code": 0, "msg": "symbolTag.screenFailed", "data": None}), 500


@tags_blp.route("/screen/save", methods=["POST"])
@login_required
def save_screen():
    """Persist a screen as a ``smart`` universe or a frozen ``manual`` one.

    ``smart`` stores the conditions and resolves them on every use (still cheap
    enough for a backtest candidate list, ~1-2s for one trading day).
    ``manual`` freezes the current result, for a reproducible snapshot.
    """
    try:
        payload = request.get_json(silent=True) or {}
        conditions = normalize_conditions(payload.get("conditions") or [])
        name = str(payload.get("name") or "").strip()
        if not name:
            raise SymbolTagError("symbolTag.invalidName")
        mode = str(payload.get("mode") or payload.get("universe_type") or "smart").strip().lower()
        if mode not in ("smart", "manual"):
            raise SymbolTagError("symbolTag.invalidSaveMode")
        service = get_universe_service()
        if mode == "smart":
            created = service.create_smart(g.user_id, {
                "name": name,
                "market": str(payload.get("market") or "CNStock"),
                "conditions": conditions,
            })
        else:
            result = get_symbol_tag_service().screen(
                conditions,
                as_of=payload.get("as_of") or payload.get("asOf"),
                limit=5000,
            )
            if not result["items"]:
                raise SymbolTagError("symbolTag.emptySelection")
            created = service.create_manual(g.user_id, {
                "name": name,
                "market": str(payload.get("market") or "CNStock"),
                "members": [
                    {"market": item["market"], "symbol": item["symbol"], "name": item.get("name") or ""}
                    for item in result["items"]
                ],
                "metadata": {
                    "source": "symbol_tags",
                    "conditions": conditions,
                    "as_of": result["as_of"],
                },
            })
        return _success(created, status=201)
    except (SymbolTagError, UniverseError) as exc:
        return _failure(exc)
    except Exception:
        logger.exception("save tag screen failed")
        return jsonify({"code": 0, "msg": "symbolTag.saveFailed", "data": None}), 500


@tags_blp.route("/symbols", methods=["GET"])
@login_required
def symbols_tags():
    """Batch tag lookup for lists (watchlist, search results).

    One request per screen instead of one per row: a 200-symbol watchlist would
    otherwise issue 200 round trips just to render tag chips.
    """
    try:
        raw = request.args.get("symbols") or ""
        symbols = [item.strip().upper() for item in raw.split(",") if item.strip()]
        if not symbols:
            raise SymbolTagError("symbolTag.symbolRequired")
        if len(symbols) > 500:
            raise SymbolTagError("symbolTag.tooManySymbols")
        market = request.args.get("market") or "CNStock"
        service = get_symbol_tag_service()
        return _success({
            "market": market,
            "items": {symbol: service.tags_for_symbol(market, symbol) for symbol in symbols},
        })
    except SymbolTagError as exc:
        return _failure(exc)
    except Exception:
        logger.exception("batch symbol tags failed")
        return jsonify({"code": 0, "msg": "symbolTag.symbolFailed", "data": None}), 500


@tags_blp.route("/symbols/<string:symbol>", methods=["GET"])
@login_required
def symbol_tags(symbol: str):
    try:
        items = get_symbol_tag_service().tags_for_symbol(
            request.args.get("market") or "CNStock",
            symbol,
        )
        return _success({"symbol": symbol, "items": items, "count": len(items)})
    except SymbolTagError as exc:
        return _failure(exc)
    except Exception:
        logger.exception("symbol tags failed symbol=%s", symbol)
        return jsonify({"code": 0, "msg": "symbolTag.symbolFailed", "data": None}), 500


@tags_blp.route("/admin/overview", methods=["GET"])
@login_required
@admin_required
def tag_overview():
    try:
        service = get_symbol_tag_service()
        tags = service.list_tags()
        by_category: dict[str, int] = {}
        for tag in tags:
            by_category[tag["category"]] = by_category.get(tag["category"], 0) + 1
        return _success({
            "by_category": by_category,
            "tags": tags,
            "refreshable_categories": list(MATERIALIZED_CATEGORIES),
        })
    except SymbolTagError as exc:
        return _failure(exc)
    except Exception:
        logger.exception("tag overview failed")
        return jsonify({"code": 0, "msg": "symbolTag.overviewFailed", "data": None}), 500


@tags_blp.route("/admin/sync", methods=["POST"])
@login_required
@admin_required
def sync_tags():
    try:
        payload = request.get_json(silent=True) or {}
        requested = payload.get("categories") or list(MATERIALIZED_CATEGORIES)
        categories = [str(item).strip().lower() for item in requested if str(item).strip()]
        report = get_symbol_tag_service().refresh(categories=categories)
        return _success(report)
    except SymbolTagError as exc:
        return _failure(exc)
    except Exception:
        logger.exception("tag sync failed")
        return jsonify({"code": 0, "msg": "symbolTag.syncFailed", "data": None}), 500


@tags_blp.route("/<string:code>/conditions/floor", methods=["GET"])
@login_required
def condition_floor(code: str):
    """Earliest usable date for a condition set; used by the strategy IDE."""
    try:
        conditions = normalize_conditions([{"tag_code": code}])
        return _success({"tag_code": code, "floor": tag_conditions_floor(conditions)})
    except SymbolTagError as exc:
        return _failure(exc)
    except Exception:
        logger.exception("tag condition floor failed code=%s", code)
        return jsonify({"code": 0, "msg": "symbolTag.floorFailed", "data": None}), 500
