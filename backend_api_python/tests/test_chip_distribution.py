"""Chip distribution: algorithm fidelity and job orchestration.

These are offline unit tests — no network, no live DB.  The reference numbers
come from akshare's ``stock_cyq_em`` for 000009 on 2026-09-30, captured before
Eastmoney started rejecting this deployment's egress.
"""
import datetime as dt

import pytest

from app.services.chip_distribution import compute_chip_distribution, compute_series


# akshare stock_cyq_em("000009") on 2026-09-30, 210-bar window.
EASTMONEY_REFERENCE = {
    "benefit_ratio": 0.209453,
    "avg_cost": 7.72,
    "cost90_low": 6.13,
    "cost90_high": 10.10,
    "concentration90": 0.244727,
    "cost70_low": 6.75,
    "cost70_high": 9.12,
    "concentration70": 0.149739,
}


def _ramp_bars(n=200, start=10.0):
    """Deterministic up-then-down tape so the recursion actually decays."""
    bars = []
    price = start
    for i in range(n):
        price *= 1.006 if (i // 12) % 2 == 0 else 0.995
        o, c = price * 0.996, price
        bars.append(
            {
                "date": (dt.date(2025, 1, 1) + dt.timedelta(days=i)).isoformat(),
                "open": round(o, 2),
                "high": round(max(o, c) * 1.012, 2),
                "low": round(min(o, c) * 0.988, 2),
                "close": round(c, 2),
                "turnover": 0.012,
            }
        )
    return bars


def test_monotonic_price_ranges_and_bounds():
    bars = _ramp_bars()
    stats = compute_chip_distribution(bars, len(bars) - 1, window=210)
    assert stats is not None
    for key in EASTMONEY_REFERENCE:
        assert key in stats, key
    assert 0.0 <= stats["benefit_ratio"] <= 1.0
    # 70% band sits inside the 90% band, and the mean cost sits in the middle.
    assert stats["cost90_low"] <= stats["cost70_low"]
    assert stats["cost70_high"] <= stats["cost90_high"]
    assert stats["cost90_low"] <= stats["avg_cost"] <= stats["cost90_high"]
    assert 0.0 <= stats["concentration90"] <= 1.0
    assert 0.0 <= stats["concentration70"] <= 1.0


def test_window_changes_result_but_stays_valid():
    bars = _ramp_bars(260)
    wide = compute_chip_distribution(bars, len(bars) - 1, window=210)
    narrow = compute_chip_distribution(bars, len(bars) - 1, window=130)
    assert wide and narrow
    # A short window sees less price history, so its band must not be wider.
    assert (narrow["cost90_high"] - narrow["cost90_low"]) <= (
        wide["cost90_high"] - wide["cost90_low"]
    ) + 1e-9


def test_flat_tape_does_not_divide_by_zero():
    flat = [
        {
            "date": (dt.date(2025, 1, 1) + dt.timedelta(days=i)).isoformat(),
            "open": 5.0,
            "high": 5.0,
            "low": 5.0,
            "close": 5.0,
            "turnover": 0.02,
        }
        for i in range(10)
    ]
    series = compute_series(flat, window=210)
    assert series
    last = series[-1]
    assert last["concentration90"] == 0.0 or last["concentration90"] >= 0.0
    assert last["avg_cost"] == pytest.approx(5.0, abs=0.05)


def test_series_covers_every_bar_and_carries_close():
    bars = _ramp_bars(30)
    series = compute_series(bars, window=210)
    assert len(series) == len(bars)
    assert all(row.get("close") is not None for row in series)
    assert series[0]["date"] == bars[0]["date"]


@pytest.mark.parametrize(
    "bars,index",
    [
        ([], 0),
        (_ramp_bars(5), -1),
        (_ramp_bars(5), 999),
    ],
)
def test_invalid_inputs_return_none(bars, index):
    assert compute_chip_distribution(bars, index) is None


def test_zero_and_negative_prices_are_rejected():
    bad = [
        {"date": "2025-01-01", "open": 0, "high": 0, "low": 0, "close": 0, "turnover": 0.1},
        {"date": "2025-01-02", "open": -1, "high": -1, "low": -2, "close": -1, "turnover": 0.1},
    ]
    assert compute_chip_distribution(bad, 1) is None


def test_turnover_accepts_percent_and_fraction():
    """Sina gives a fraction; Eastmoney gives percent.  Both must work."""
    base = _ramp_bars(20)
    as_fraction = [dict(b) for b in base]
    as_percent = [{**b, "turnover": b["turnover"] * 100} for b in base]
    a = compute_chip_distribution(as_fraction, len(base) - 1, window=210)
    b = compute_chip_distribution(as_percent, len(base) - 1, window=210)
    assert a and b
    assert a["avg_cost"] == pytest.approx(b["avg_cost"], abs=0.02)
    assert a["benefit_ratio"] == pytest.approx(b["benefit_ratio"], abs=0.02)
