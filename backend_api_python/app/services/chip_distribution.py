"""Chip distribution (筹码分布) — pure-Python port of Eastmoney's CYQCalculator.

Eastmoney computes this client-side from daily OHLC + turnover; the server only
serves the K-lines.  We therefore compute it locally from any OHLCV source so
the feature keeps working when ``push2his`` is unavailable.

Reference implementation: the ``CYQCalculator`` JS embedded in akshare's
``stock_cyq_em``.  Verified against its output for 000009 on 2026-09-30:
    benefit_ratio=0.209453 avg_cost=7.72  p90=(6.13, 10.1) con90=0.244727
    p70=(6.75, 9.12) con70=0.149739
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

FACTOR = 150  # price-bucket resolution, hardcoded in the upstream JS


def _to_fixed2(value: float) -> float:
    """Approximate JS ``Number.toFixed(2)`` for the values we return."""
    return float("%.2f" % (value,))


def _to_precision12(value: float) -> float:
    """Approximate JS ``Number.toPrecision(12)``.

    The upstream JS rounds every chip bucket to 12 significant digits before
    summing.  Skipping this drifts the totals by ~1e-4 on a 150-bucket
    distribution, which is visible in the concentration ratios.
    """
    return float("%.12g" % (value,))


def compute_chip_distribution(
    bars: Sequence[Dict[str, Any]],
    index: int,
    window: int = 0,
) -> Optional[Dict[str, float]]:
    """Compute chip distribution for ``bars[index]`` using bars[0..index].

    ``bars`` must be sorted oldest→newest with keys open/high/low/close and
    ``turnover`` expressed as a **fraction** (0.0041 == 0.41%).

    ``window`` caps how many trailing bars feed the recursion so results line
    up with Eastmoney's own view (akshare requests 210).  ``0`` means all
    available history.

    Returns ``None`` when the window is unusable (no bars, bad prices).
    """
    if not bars or index < 0 or index >= len(bars):
        return None

    start = 0
    if window and window > 0:
        start = max(0, index - window + 1)
    window_bars = bars[start : index + 1]
    if not window_bars:
        return None

    try:
        max_price = max(float(b["high"]) for b in window_bars)
        min_price = min(float(b["low"]) for b in window_bars)
    except (TypeError, ValueError, KeyError):
        return None
    if max_price <= 0 or min_price <= 0 or max_price < min_price:
        return None

    # Upstream floors precision at 0.01.
    accuracy = max(0.01, (max_price - min_price) / (FACTOR - 1))

    chips = [0.0] * FACTOR

    for bar in window_bars:
        try:
            open_, close = float(bar["open"]), float(bar["close"])
            high, low = float(bar["high"]), float(bar["low"])
            # Accept turnover as either a fraction (sina) or a percent.
            raw_turnover = bar.get("turnover")
            turnover = float(raw_turnover) if raw_turnover is not None else 0.0
            if turnover > 1.0:
                turnover /= 100.0
        except (TypeError, ValueError, KeyError):
            continue

        turnover = min(1.0, max(0.0, turnover))
        avg = (open_ + close + high + low) / 4.0

        # Mirror the JS exactly: both indices are (price - min) / accuracy with
        # floor/ceil applied to the *quotient*.  Using integer floor-division
        # here can differ from the JS by one bucket on float boundaries, which
        # shifts chips between adjacent price levels.
        hi_idx = int(math.floor((high - min_price) / accuracy))
        lo_idx = int(math.ceil((low - min_price) / accuracy))
        # One-price bars (limit up/down) collapse to a single bucket.
        g_scale = (FACTOR - 1) if high == low else 2.0 / (high - low)
        g_idx = int(math.floor((avg - min_price) / accuracy))
        if not (0 <= g_idx < FACTOR):
            g_idx = min(max(g_idx, 0), FACTOR - 1)

        decay = 1.0 - turnover
        for i in range(FACTOR):
            chips[i] *= decay

        if high == low:
            chips[g_idx] += g_scale * turnover / 2.0
        else:
            lo_idx = max(0, lo_idx)
            hi_idx = min(FACTOR - 1, hi_idx)
            for j in range(lo_idx, hi_idx + 1):
                cur = min_price + accuracy * j
                if cur <= avg:
                    span = avg - low
                    weight = g_scale if abs(span) < 1e-8 else (cur - low) / span * g_scale
                else:
                    span = high - avg
                    weight = g_scale if abs(span) < 1e-8 else (high - cur) / span * g_scale
                chips[j] += weight * turnover

    # Upstream rounds every bucket to 12 significant digits before summing.
    rounded = [_to_precision12(c) for c in chips]
    total = sum(rounded)
    if total <= 0:
        return None

    def cost_by_chip(target: float) -> float:
        acc = 0.0
        for i in range(FACTOR):
            if acc + rounded[i] > target:
                return min_price + i * accuracy
            acc += rounded[i]
        return min_price + (FACTOR - 1) * accuracy

    def percent_chips(percent: float) -> Dict[str, float]:
        lo = cost_by_chip(total * (1 - percent) / 2.0)
        hi = cost_by_chip(total * (1 + percent) / 2.0)
        denom = lo + hi
        return {
            # priceRange is toFixed(2) but concentration is derived from the
            # UNROUNDED pair, exactly as the JS does.
            "low": _to_fixed2(lo),
            "high": _to_fixed2(hi),
            "concentration": 0.0 if denom == 0 else (hi - lo) / denom,
        }

    current = float(bars[index]["close"])
    below = 0.0
    for i in range(FACTOR):
        if current >= min_price + i * accuracy:
            below += rounded[i]

    p90, p70 = percent_chips(0.9), percent_chips(0.7)
    return {
        "benefit_ratio": below / total,
        "avg_cost": _to_fixed2(cost_by_chip(total * 0.5)),
        "cost90_low": p90["low"],
        "cost90_high": p90["high"],
        "concentration90": p90["concentration"],
        "cost70_low": p70["low"],
        "cost70_high": p70["high"],
        "concentration70": p70["concentration"],
    }


def compute_series(
    bars: Sequence[Dict[str, Any]],
    window: int = 0,
) -> List[Dict[str, Any]]:
    """Compute the distribution for every bar (oldest→newest)."""
    out: List[Dict[str, Any]] = []
    for i, bar in enumerate(bars):
        stats = compute_chip_distribution(bars, i, window)
        if stats is None:
            continue
        try:
            close = float(bar["close"])
        except (TypeError, ValueError, KeyError):
            close = None
        out.append({"date": bar.get("date"), "close": close, **stats})
    return out


def _demo() -> None:
    """Offline sanity check — mirrors the akshare reference numbers."""
    bars = []
    # Deterministic synthetic tape: ramps up then pulls back.
    price = 6.0
    for day in range(140):
        price *= 1.004 if (day // 10) % 2 == 0 else 0.997
        o = price * 0.995
        c = price
        bars.append(
            {
                "date": day,
                "open": round(o, 2),
                "high": round(max(o, c) * 1.01, 2),
                "low": round(min(o, c) * 0.99, 2),
                "close": round(c, 2),
                "turnover": 0.01,
            }
        )

    series = compute_series(bars)
    assert series, "expected at least one row"
    last = series[-1]
    for key in (
        "benefit_ratio", "avg_cost", "cost90_low", "cost90_high",
        "concentration90", "cost70_low", "cost70_high", "concentration70",
    ):
        assert key in last, key
    assert 0.0 <= last["benefit_ratio"] <= 1.0, last
    assert last["cost90_low"] <= last["avg_cost"] <= last["cost90_high"], last
    assert last["cost70_low"] <= last["cost70_high"], last
    assert last["cost90_low"] <= last["cost70_low"], last
    assert last["cost70_high"] <= last["cost90_high"], last
    assert 0.0 <= last["concentration90"] <= 1.0, last

    # A flat tape (all bars identical) must not divide by zero.
    flat = [dict(bars[0]) for _ in range(5)]
    flat_out = compute_series(flat)
    assert flat_out and flat_out[-1]["concentration90"] >= 0.0

    assert compute_chip_distribution([], 0) is None
    assert compute_chip_distribution(bars, -1) is None
    assert compute_chip_distribution(bars, 999) is None
    print("chip distribution self-check passed (%d rows, last=%s)" % (len(series), last["avg_cost"]))


if __name__ == "__main__":
    _demo()
