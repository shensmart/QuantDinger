"""Eastmoney dragon-tiger (龙虎榜) board data via AkShare.

HiThink's ``dragon-tiger-list`` exposes only the top-level board: one net-buy
figure per stock plus a trader aggregate. The Eastmoney views that Chinese
users actually read the board for — 每日活跃营业部 (per-branch activity),
机构买卖 (institutional seat buys/sells) and the 近一月/三月/六月/一年 rolling
统计 — are richer and available from AkShare without an extra key.

This module is the single exit point for those extra views. Every call is
best-effort: a failure returns an empty list so a page degrades to "no data"
instead of a 500.

Units: the upstream frame is denominated in 元 and keeps its own precision; we
convert to 元 floats and round for display. ``%`` columns arrive already scaled
by 100 (e.g. ``3.8126`` means 3.81%).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from app.data_sources.asia_stock_kline import _bypass_proxy
from app.utils.logger import get_logger

logger = get_logger(__name__)

SOURCE = "eastmoney_lhb_via_akshare"

STATISTIC_WINDOWS = ("近一月", "近三月", "近六月", "近一年")


def _ak():
    import akshare as ak  # type: ignore

    return ak


def _float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _int(value: Any) -> Optional[int]:
    number = _float(value)
    return int(number) if number is not None else None


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


def _date_arg(value: Any) -> str:
    """AkShare wants yyyymmdd."""
    if isinstance(value, dt.datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, dt.date):
        return value.strftime("%Y%m%d")
    text = str(value or "").strip()
    if not text:
        raise ValueError("date is required")
    if len(text) == 8 and text.isdigit():
        return text
    return text.replace("-", "").replace("/", "")[:8]


def _symbol6(value: Any) -> str:
    """``000001`` / ``000001.SZ`` / ``sz000001`` -> ``000001``."""
    text = _text(value).upper()
    if "." in text:
        text = text.split(".", 1)[0]
    if text[:2] in {"SH", "SZ", "BJ"}:
        text = text[2:]
    return text


def _symbol_from_code(code: Any) -> str:
    six = _symbol6(code)
    if len(six) != 6 or not six.isdigit():
        return ""
    if six.startswith("6"):
        return f"{six}.SH"
    if six.startswith(("4", "8", "92")):
        return f"{six}.BJ"
    return f"{six}.SZ"


def _row(frame, index: int) -> Dict[str, Any]:
    return {str(key): frame.iloc[index][key] for key in frame.columns}


# ---------------------------------------------------------------------------
# Per-day boards
# ---------------------------------------------------------------------------

def daily_detail(trade_date: dt.date) -> List[Dict[str, Any]]:
    """Every dragon-tiger listing of one trade date (one row per 上榜原因)."""
    day = _date_arg(trade_date)
    try:
        frame = _ak().stock_lhb_detail_em(start_date=day, end_date=day)
    except Exception as exc:  # noqa: BLE001 - board data is optional enrichment
        logger.warning("eastmoney daily dragon-tiger failed %s: %s", day, exc)
        return []
    if frame is None or getattr(frame, "empty", True):
        return []

    output: List[Dict[str, Any]] = []
    for index, _unused in enumerate(range(len(frame))):
        row = _row(frame, index)
        symbol = _symbol_from_code(row.get("代码"))
        if not symbol:
            continue
        listed_day = _text(row.get("上榜日"))
        output.append({
            "symbol": symbol,
            "code": _symbol6(row.get("代码")),
            "name": _text(row.get("名称")),
            "trade_date": listed_day or trade_date.isoformat(),
            "close": _float(row.get("收盘价")),
            "change_pct": _float(row.get("涨跌幅")),
            "net_buy": _float(row.get("龙虎榜净买额")),
            "buy_amount": _float(row.get("龙虎榜买入额")),
            "sell_amount": _float(row.get("龙虎榜卖出额")),
            "total_amount": _float(row.get("龙虎榜成交额")),
            "market_amount": _float(row.get("市场总成交额")),
            "net_ratio": _float(row.get("净买额占总成交比")),
            "amount_ratio": _float(row.get("成交额占总成交比")),
            "turnover_ratio": _float(row.get("换手率")),
            "float_market_cap": _float(row.get("流通市值")),
            "reason": _text(row.get("上榜原因")),
            "explanation": _text(row.get("解读")),
            "source": SOURCE,
        })
    return output


def institutional_seats(trade_date: dt.date) -> List[Dict[str, Any]]:
    """机构买卖情况 for one trade date: seat counts and buy/sell totals."""
    day = _date_arg(trade_date)
    try:
        frame = _ak().stock_lhb_jgmmtj_em(start_date=day, end_date=day)
    except Exception as exc:  # noqa: BLE001
        logger.warning("eastmoney institutional dragon-tiger failed %s: %s", day, exc)
        return []
    if frame is None or getattr(frame, "empty", True):
        return []

    output: List[Dict[str, Any]] = []
    seen: set[tuple] = set()
    for index in range(len(frame)):
        row = _row(frame, index)
        symbol = _symbol_from_code(row.get("代码"))
        if not symbol:
            continue
        listed = _text(row.get("上榜日期")) or trade_date.isoformat()
        # The same stock can appear per 上榜原因; keep the first row per day so
        # the seat counts are not double counted.
        key = (symbol, listed)
        if key in seen:
            continue
        seen.add(key)
        output.append({
            "symbol": symbol,
            "code": _symbol6(row.get("代码")),
            "name": _text(row.get("名称")),
            "trade_date": listed,
            "close": _float(row.get("收盘价")),
            "change_pct": _float(row.get("涨跌幅")),
            "buy_seats": _int(row.get("买方机构数")),
            "sell_seats": _int(row.get("卖方机构数")),
            "org_buy": _float(row.get("机构买入总额")),
            "org_sell": _float(row.get("机构卖出总额")),
            "org_net": _float(row.get("机构买入净额")),
            "market_amount": _float(row.get("市场总成交额")),
            "org_net_ratio": _float(row.get("机构净买额占总成交额比")),
            "turnover_ratio": _float(row.get("换手率")),
            "float_market_cap": _float(row.get("流通市值")),
            "reason": _text(row.get("上榜原因")),
            "source": SOURCE,
        })
    return output


def active_branches(trade_date: dt.date) -> List[Dict[str, Any]]:
    """每日活跃营业部 for one trade date."""
    day = _date_arg(trade_date)
    try:
        frame = _ak().stock_lhb_hyyyb_em(start_date=day, end_date=day)
    except Exception as exc:  # noqa: BLE001
        logger.warning("eastmoney active branches failed %s: %s", day, exc)
        return []
    if frame is None or getattr(frame, "empty", True):
        return []

    output: List[Dict[str, Any]] = []
    for index in range(len(frame)):
        row = _row(frame, index)
        name = _text(row.get("营业部名称"))
        if not name:
            continue
        output.append({
            "branch": name,
            "branch_code": _text(row.get("营业部代码")),
            "trade_date": _text(row.get("上榜日")) or trade_date.isoformat(),
            "buy_stocks": _int(row.get("买入个股数")),
            "sell_stocks": _int(row.get("卖出个股数")),
            "buy_amount": _float(row.get("买入总金额")),
            "sell_amount": _float(row.get("卖出总金额")),
            "net_amount": _float(row.get("总买卖净额")),
            "stocks": _text(row.get("买入股票")).split(),
            "source": SOURCE,
        })
    return output


# ---------------------------------------------------------------------------
# Rolling statistics (近一月 / 近三月 / 近六月 / 近一年)
# ---------------------------------------------------------------------------

def _window(value: Any) -> str:
    text = _text(value) or "近一月"
    if text not in STATISTIC_WINDOWS:
        raise ValueError(f"unsupported window: {value}")
    return text


def stock_statistics(window: str = "近一月") -> List[Dict[str, Any]]:
    """个股龙虎榜统计 over a rolling window."""
    selected = _window(window)
    try:
        frame = _ak().stock_lhb_stock_statistic_em(symbol=selected)
    except Exception as exc:  # noqa: BLE001
        logger.warning("eastmoney stock dragon-tiger statistics failed: %s", exc)
        return []
    if frame is None or getattr(frame, "empty", True):
        return []

    output: List[Dict[str, Any]] = []
    for index in range(len(frame)):
        row = _row(frame, index)
        symbol = _symbol_from_code(row.get("代码"))
        if not symbol:
            continue
        output.append({
            "symbol": symbol,
            "code": _symbol6(row.get("代码")),
            "name": _text(row.get("名称")),
            "last_listed": _text(row.get("最近上榜日")),
            "close": _float(row.get("收盘价")),
            "change_pct": _float(row.get("涨跌幅")),
            "list_count": _int(row.get("上榜次数")),
            "net_buy": _float(row.get("龙虎榜净买额")),
            "buy_amount": _float(row.get("龙虎榜买入额")),
            "sell_amount": _float(row.get("龙虎榜卖出额")),
            "total_amount": _float(row.get("龙虎榜总成交额")),
            "buy_seat_count": _int(row.get("买方机构次数")),
            "sell_seat_count": _int(row.get("卖方机构次数")),
            "org_net": _float(row.get("机构买入净额")),
            "org_buy": _float(row.get("机构买入总额")),
            "org_sell": _float(row.get("机构卖出总额")),
            "return_1m": _float(row.get("近1个月涨跌幅")),
            "return_3m": _float(row.get("近3个月涨跌幅")),
            "return_6m": _float(row.get("近6个月涨跌幅")),
            "return_1y": _float(row.get("近1年涨跌幅")),
            "window": selected,
            "source": SOURCE,
        })
    return output


def institution_statistics(window: str = "近一月") -> List[Dict[str, Any]]:
    """机构席位买卖追踪 over a rolling window."""
    selected = _window(window)
    try:
        frame = _ak().stock_lhb_jgstatistic_em(symbol=selected)
    except Exception as exc:  # noqa: BLE001
        logger.warning("eastmoney institution statistics failed: %s", exc)
        return []
    if frame is None or getattr(frame, "empty", True):
        return []

    output: List[Dict[str, Any]] = []
    for index in range(len(frame)):
        row = _row(frame, index)
        symbol = _symbol_from_code(row.get("代码"))
        if not symbol:
            continue
        output.append({
            "symbol": symbol,
            "code": _symbol6(row.get("代码")),
            "name": _text(row.get("名称")),
            "close": _float(row.get("收盘价")),
            "change_pct": _float(row.get("涨跌幅")),
            "total_amount": _float(row.get("龙虎榜成交金额")),
            "list_count": _int(row.get("上榜次数")),
            "org_buy": _float(row.get("机构买入额")),
            "org_buy_count": _int(row.get("机构买入次数")),
            "org_sell": _float(row.get("机构卖出额")),
            "org_sell_count": _int(row.get("机构卖出次数")),
            "org_net": _float(row.get("机构净买额")),
            "return_1m": _float(row.get("近1个月涨跌幅")),
            "return_3m": _float(row.get("近3个月涨跌幅")),
            "return_6m": _float(row.get("近6个月涨跌幅")),
            "return_1y": _float(row.get("近1年涨跌幅")),
            "window": selected,
            "source": SOURCE,
        })
    return output


def branch_statistics(window: str = "近一月") -> List[Dict[str, Any]]:
    """证券营业部上榜统计 over a rolling window."""
    selected = _window(window)
    try:
        frame = _ak().stock_lhb_traderstatistic_em(symbol=selected)
    except Exception as exc:  # noqa: BLE001
        logger.warning("eastmoney branch statistics failed: %s", exc)
        return []
    if frame is None or getattr(frame, "empty", True):
        return []

    output: List[Dict[str, Any]] = []
    for index in range(len(frame)):
        row = _row(frame, index)
        name = _text(row.get("营业部名称"))
        if not name:
            continue
        output.append({
            "branch": name,
            "total_amount": _float(row.get("龙虎榜成交金额")),
            "list_count": _int(row.get("上榜次数")),
            "buy_amount": _float(row.get("买入额")),
            "buy_count": _int(row.get("买入次数")),
            "sell_amount": _float(row.get("卖出额")),
            "sell_count": _int(row.get("卖出次数")),
            "window": selected,
            "source": SOURCE,
        })
    return output


__all__ = [
    "SOURCE",
    "STATISTIC_WINDOWS",
    "daily_detail",
    "institutional_seats",
    "active_branches",
    "stock_statistics",
    "institution_statistics",
    "branch_statistics",
]
