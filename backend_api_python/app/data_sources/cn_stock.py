"""
中国A股数据源 — 多层 fallback

有 TWELVE_DATA_API_KEY:
  所有周期 → Twelve Data（主） → 同花顺日K → 腾讯日/周线 → yfinance → AkShare

无限流 Key（默认）:
  分钟/小时 → yfinance → AkShare
  日/周线 → 同花顺官方 API → 腾讯 fqkline → yfinance → AkShare

同花顺只提供日线，周线由日线本地聚合；复权默认用官方事件流做差值前复权
（对齐东财/同花顺软件口径），可用 CN_KLINE_ADJUST 切换为 none/forward/backward。
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Dict, List, Any, Optional

from app.data_sources.base import BaseDataSource
from app.data_sources import hithink_finance as hithink
from app.data_sources.tencent import normalize_cn_code, fetch_quote, parse_quote_to_ticker, fetch_kline, tencent_kline_rows_to_dicts
from app.data_sources.asia_stock_kline import (
    normalize_chart_timeframe,
    fetch_twelvedata_klines,
    fetch_yfinance_klines,
    fetch_akshare_minute_klines,
    fetch_akshare_weekly_klines,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

_MINUTE_TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1H", "4H")


def _primary_source() -> str:
    return str(os.getenv("CN_KLINE_PRIMARY_SOURCE") or "hithink").strip().lower()


def _default_adjust() -> str:
    return str(os.getenv("CN_KLINE_ADJUST") or "forward_additive").strip().lower()


def _hithink_window(timeframe: str, limit: int, before_time: Optional[int]) -> tuple[date, date]:
    """Translate (limit, before_time) into a [start, end] daily window."""
    if before_time:
        end = datetime.fromtimestamp(int(before_time)).date() - timedelta(days=1)
    else:
        end = hithink.shanghai_today()
    per_bar_days = 7 if timeframe == "1W" else 1
    span = min(3650, int(max(1, limit) * per_bar_days * 1.9) + 40)
    return end - timedelta(days=span), end


class CNStockDataSource(BaseDataSource):
    """A股数据源（TwelveData + 同花顺 + Tencent + yfinance + AkShare）"""

    name = "CNStock/multi-source"

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        code = normalize_cn_code(symbol)

        # Tier 1: HiThink realtime snapshot (~3s delay)
        if hithink.configured():
            try:
                rows = hithink.snapshot([code])
                if rows:
                    ticker = hithink.snapshot_to_ticker(rows[0])
                    ticker["name"] = self._symbol_name(symbol, ticker["symbol"])
                    return ticker
            except hithink.HiThinkError as exc:
                logger.warning("HiThink ticker failed for %s (%s); falling back to Tencent", code, exc)

        parts = fetch_quote(code)
        if not parts:
            return {"last": 0, "symbol": code}
        t = parse_quote_to_ticker(parts)
        return {
            "last": t.get("last", 0),
            "change": t.get("change", 0),
            "changePercent": t.get("changePercent", 0),
            "high": t.get("high", 0),
            "low": t.get("low", 0),
            "open": t.get("open", 0),
            "previousClose": t.get("previousClose", 0),
            "name": t.get("name", ""),
            "symbol": code,
        }

    @staticmethod
    def _symbol_name(symbol: str, fallback: str) -> str:
        try:
            from app.services.symbol_name import resolve_symbol_name

            return resolve_symbol_name("CNStock", symbol) or fallback
        except Exception:
            return fallback

    def get_kline(
        self,
        symbol: str,
        timeframe: str,
        limit: int,
        before_time: Optional[int] = None,
        after_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        code = normalize_cn_code(symbol)
        tf = normalize_chart_timeframe(timeframe)
        lim = max(int(limit or 300), 1)

        # Tier 1: Twelve Data (paid, most reliable)
        rows = fetch_twelvedata_klines(
            is_hk=False, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
        )
        if rows:
            return self.filter_and_limit(
                rows,
                limit=lim,
                before_time=before_time,
                after_time=after_time,
                truncate=(after_time is None),
            )

        # Tier 2: HiThink official API — daily only (weekly is aggregated locally)
        if tf in ("1D", "1W") and _primary_source() == "hithink" and hithink.configured():
            rows = self._hithink_klines(code, tf, lim, before_time=before_time)
            if rows:
                return self.filter_and_limit(
                    rows,
                    limit=lim,
                    before_time=before_time,
                    after_time=after_time,
                    truncate=(after_time is None),
                )

        # Tier 3: Tencent for daily/weekly (fast, free)
        if tf in ("1D", "1W"):
            tf_map = {"1D": "day", "1W": "week"}
            period = tf_map.get(tf, "day")
            raw_rows = fetch_kline(code, period=period, count=lim, adj="qfq")
            out = tencent_kline_rows_to_dicts(raw_rows)
            if out:
                return self.filter_and_limit(
                    out,
                    limit=lim,
                    before_time=before_time,
                    after_time=after_time,
                    truncate=(after_time is None),
                )

        # Tier 4: yfinance (works when Yahoo not rate-limited)
        rows = fetch_yfinance_klines(
            is_hk=False, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
        )
        if rows:
            return self.filter_and_limit(
                rows,
                limit=lim,
                before_time=before_time,
                after_time=after_time,
                truncate=(after_time is None),
            )

        # Tier 5: AkShare (fragile overseas, last resort) — never for HiThink
        if tf in _MINUTE_TIMEFRAMES:
            rows = fetch_akshare_minute_klines(
                is_hk=False, tencent_code=code, timeframe=tf, limit=lim, before_time=before_time
            )
        elif tf == "1W":
            rows = fetch_akshare_weekly_klines(
                is_hk=False, tencent_code=code, limit=lim, before_time=before_time
            )
        else:
            rows = []

        return self.filter_and_limit(
            rows,
            limit=lim,
            before_time=before_time,
            after_time=after_time,
            truncate=(after_time is None),
        )

    @staticmethod
    def _hithink_klines(
        code: str,
        timeframe: str,
        limit: int,
        *,
        before_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        try:
            start, end = _hithink_window(timeframe, limit, before_time)
            bars = hithink.adjusted_daily_bars(code, start, end, adjust=_default_adjust())
            if not bars:
                return []
            klines = hithink.daily_bars_to_klines(bars)
            if timeframe == "1W":
                klines = hithink.aggregate_weekly(klines)
            return klines
        except hithink.HiThinkError as exc:
            logger.warning(
                "HiThink kline failed for %s tf=%s (%s); falling back to Tencent/yfinance",
                code,
                timeframe,
                exc,
            )
            return []
