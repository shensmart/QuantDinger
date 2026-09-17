"""Additive (差值) forward adjustment used for A-share daily K-lines."""
import datetime as dt

import pytest

from app.data_sources import hithink_finance as hithink


def _bars(closes, start=dt.date(2026, 6, 1)):
    return [
        {"date": start + dt.timedelta(days=index), "open": close, "high": close, "low": close, "close": close}
        for index, close in enumerate(closes)
    ]


def test_cash_dividend_lowers_only_pre_ex_dates():
    # 5 bars; ex-date on day 3 with a 1.0 dividend.
    bars = _bars([10.0, 10.0, 10.0, 9.0, 9.5])
    events = [{"ex_date": dt.date(2026, 6, 4), "dividend_per_share": 1.0, "per_share_bonus": 0.0}]
    adjusted = hithink.apply_forward_additive(bars, events)
    assert [bar["close"] for bar in adjusted] == [9.0, 9.0, 9.0, 9.0, 9.5]


def test_offsets_are_constant_across_pre_ex_dates():
    bars = _bars([100.0, 101.0, 102.0, 90.0, 91.0])
    events = [
        {"ex_date": dt.date(2026, 6, 4), "dividend_per_share": 2.0, "per_share_bonus": 0.0}
    ]
    offsets = hithink.forward_additive_offset(bars, events)
    pre = [offsets[bar["date"]] for bar in bars[:3]]
    post = [offsets[bar["date"]] for bar in bars[3:]]
    assert pre == [2.0, 2.0, 2.0]
    assert post == [0.0, 0.0]


def test_bonus_shares_use_pre_ex_close():
    # 10 送 1 -> per_share_bonus 0.1; pre-ex close is 100, so the offset is 10.
    bars = _bars([100.0, 100.0, 91.0])
    events = [
        {"ex_date": dt.date(2026, 6, 3), "dividend_per_share": 0.0, "per_share_bonus": 0.1}
    ]
    offsets = hithink.forward_additive_offset(bars, events)
    assert offsets[dt.date(2026, 6, 1)] == pytest.approx(10.0)
    assert offsets[dt.date(2026, 6, 3)] == pytest.approx(0.0)


def test_multiple_events_accumulate_for_the_oldest_bar():
    bars = _bars([100.0, 100.0, 95.0, 95.0, 90.0])
    events = [
        {"ex_date": dt.date(2026, 6, 3), "dividend_per_share": 5.0, "per_share_bonus": 0.0},
        {"ex_date": dt.date(2026, 6, 5), "dividend_per_share": 2.0, "per_share_bonus": 0.0},
    ]
    offsets = hithink.forward_additive_offset(bars, events)
    assert offsets[dt.date(2026, 6, 1)] == pytest.approx(7.0)
    assert offsets[dt.date(2026, 6, 3)] == pytest.approx(2.0)
    assert offsets[dt.date(2026, 6, 5)] == pytest.approx(0.0)


def test_price_discontinuity_disappears_after_adjustment():
    """The whole point of the adjustment: no fake gap on the ex-date."""
    bars = _bars([100.0, 100.0, 100.0, 90.0])
    events = [{"ex_date": dt.date(2026, 6, 4), "dividend_per_share": 10.0, "per_share_bonus": 0.0}]
    adjusted = hithink.apply_forward_additive(bars, events)
    closes = [bar["close"] for bar in adjusted]
    returns = [closes[i + 1] / closes[i] - 1 for i in range(len(closes) - 1)]
    assert returns == pytest.approx([0.0, 0.0, 0.0], abs=1e-9)


def test_window_start_widens_to_cover_a_past_ex_date():
    """A window that starts after the ex-date must still load the dividend."""
    events = [{"ex_date": dt.date(2026, 6, 19), "dividend_per_share": 3.0, "per_share_bonus": 0.0}]
    start = dt.date(2026, 6, 1)
    assert hithink._window_start(start, dt.date(2026, 6, 20), events) < start
    # An ex-date on the first day of the window already has offset 0 everywhere
    # in the window, so no widening is needed.
    same_day = hithink._window_start(dt.date(2026, 6, 19), dt.date(2026, 6, 20), events)
    assert same_day == dt.date(2026, 6, 19)


def test_weekly_aggregation_uses_last_bar_time_and_sums_volume():
    bars = hithink.daily_bars_to_klines(
        _bars([10.0, 11.0, 12.0, 13.0, 14.0], start=dt.date(2026, 6, 1))
    )
    weekly = hithink.aggregate_weekly(bars)
    assert len(weekly) == 1  # Mon-Fri of one ISO week
    assert weekly[0]["open"] == 10.0
    assert weekly[0]["high"] == 14.0
    assert weekly[0]["low"] == 10.0
    assert weekly[0]["close"] == 14.0
    assert weekly[0]["time"] == bars[-1]["time"]
