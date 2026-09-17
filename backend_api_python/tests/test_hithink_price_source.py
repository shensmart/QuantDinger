"""CN K-line source routing: HiThink primary, no HiThink for minute bars, fallback."""
import datetime as dt

import pytest

from app.data_sources import cn_stock
from app.data_sources import hithink_finance as hithink


@pytest.fixture(autouse=True)
def _primary(monkeypatch):
    monkeypatch.setenv("CN_KLINE_PRIMARY_SOURCE", "hithink")
    monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "test-key")
    monkeypatch.setenv("HITHINK_FINANCE_ENABLED", "true")


def _bars(start=dt.date(2026, 6, 1), count=5):
    return [
        {
            "date": start + dt.timedelta(days=index),
            "open": 10.0 + index,
            "high": 11.0 + index,
            "low": 9.0 + index,
            "close": 10.5 + index,
            "volume": 1000.0,
        }
        for index in range(count)
    ]


def test_daily_kline_uses_hithink_first(monkeypatch):
    monkeypatch.setattr(hithink, "adjusted_daily_bars", lambda *a, **k: _bars())
    monkeypatch.setattr(
        cn_stock, "fetch_twelvedata_klines", lambda **kwargs: []
    )
    monkeypatch.setattr(
        cn_stock,
        "fetch_kline",
        lambda *a, **k: pytest.fail("Tencent must not be reached when HiThink works"),
    )
    rows = cn_stock.CNStockDataSource().get_kline("600519", "1D", 5)
    assert len(rows) == 5
    assert rows[0]["close"] == pytest.approx(10.5)


def test_minute_kline_never_calls_hithink(monkeypatch):
    monkeypatch.setattr(
        cn_stock, "fetch_twelvedata_klines", lambda **kwargs: []
    )
    monkeypatch.setattr(
        hithink, "adjusted_daily_bars", lambda *a, **k: pytest.fail("HiThink has no minute data")
    )
    monkeypatch.setattr(
        cn_stock,
        "fetch_yfinance_klines",
        lambda **kwargs: [{"time": 1, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}],
    )
    rows = cn_stock.CNStockDataSource().get_kline("600519", "5m", 5)
    assert len(rows) == 1


def test_hithink_failure_degrades_to_tencent(monkeypatch):
    def boom(*_a, **_k):
        raise hithink.HiThinkRateLimited("429")

    monkeypatch.setattr(hithink, "adjusted_daily_bars", boom)
    monkeypatch.setattr(cn_stock, "fetch_twelvedata_klines", lambda **kwargs: [])
    monkeypatch.setattr(
        cn_stock,
        "fetch_kline",
        lambda *a, **k: [["2026-06-01", "10", "10.5", "11", "9", "1000"]],
    )
    rows = cn_stock.CNStockDataSource().get_kline("600519", "1D", 5)
    assert rows and rows[0]["close"] == pytest.approx(10.5)


def test_primary_switch_back_to_tencent_skips_hithink(monkeypatch):
    monkeypatch.setenv("CN_KLINE_PRIMARY_SOURCE", "tencent")
    monkeypatch.setattr(
        hithink, "adjusted_daily_bars", lambda *a, **k: pytest.fail("disabled by env")
    )
    monkeypatch.setattr(cn_stock, "fetch_twelvedata_klines", lambda **kwargs: [])
    monkeypatch.setattr(
        cn_stock,
        "fetch_kline",
        lambda *a, **k: [["2026-06-01", "10", "10.5", "11", "9", "1000"]],
    )
    rows = cn_stock.CNStockDataSource().get_kline("600519", "1D", 5)
    assert len(rows) == 1


def test_weekly_kline_is_aggregated_locally(monkeypatch):
    monkeypatch.setattr(hithink, "adjusted_daily_bars", lambda *a, **k: _bars(count=10))
    monkeypatch.setattr(cn_stock, "fetch_twelvedata_klines", lambda **kwargs: [])
    rows = cn_stock.CNStockDataSource().get_kline("600519", "1W", 5)
    assert rows
    assert all(row["high"] >= row["close"] for row in rows)


def test_ticker_uses_hithink_then_falls_back(monkeypatch):
    monkeypatch.setattr(
        hithink,
        "snapshot",
        lambda codes: [
            {
                "thscode": "600519.SH",
                "last_price": 1266.98,
                "prev_price": 1258.0,
                "open_price": 1257.98,
                "high_price": 1267.6,
                "low_price": 1254.0,
                "volume": 100,
                "turnover": 1000,
            }
        ],
    )
    ticker = cn_stock.CNStockDataSource().get_ticker("600519")
    assert ticker["last"] == pytest.approx(1266.98)
    assert ticker["source"] == "hithink_finance"

    def boom(_codes):
        raise hithink.HiThinkUnavailable("down")

    monkeypatch.setattr(hithink, "snapshot", boom)
    monkeypatch.setattr(
        cn_stock, "fetch_quote", lambda code: ["1", "茅台", "600519", "10", "9", "9.5"]
    )
    fallback = cn_stock.CNStockDataSource().get_ticker("600519")
    assert fallback["last"] == pytest.approx(10.0)
    assert "source" not in fallback
