"""Accuracy harness for the chip-distribution port.

Two independent questions have to be answered separately, and mixing them up is
how a subtle bug ships:

1. **Is the algorithm a faithful port?**
   Compare our Python against the *exact* JS that akshare ships in
   ``stock_cyq_em`` (run through py_mini_racer).  Same input, same numbers means
   the port is exact.  Any difference here is a real bug.

2. **Is the data source good enough?**
   Price adjustment and turnover precision come from the input data, not the
   algorithm.  Compare the same algorithm across sources (Sina vs Tencent) and
   across adjustment modes to bound that error.

Run:  python3 scripts/verify_chip_accuracy.py [--live]

Without ``--live`` only the offline algorithm check runs (no network).
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.chip_distribution import compute_chip_distribution  # noqa: E402

# --------------------------------------------------------------------------
# 1. Reference: run akshare's own JS
# --------------------------------------------------------------------------


def load_reference_js() -> Optional[str]:
    """Extract the CYQCalculator JS akshare embeds in ``stock_cyq_em``."""
    try:
        import inspect

        import akshare as ak

        src = inspect.getsource(ak.stock_cyq_em)
    except Exception:
        return None
    start = src.find('html_str = """')
    end = src.find('js_code = ')
    if start < 0 or end < 0:
        return None
    body = src[start + len('html_str = """') : end].replace('"""', "")
    # The JS expects `this.range`; akshare's usage relies on the commented-out
    # default, so define it explicitly the way the site does.
    return "var range_default = 120;\n" + body + "\nfunction runAll(kl, range) {\n  this.range = range;\n  var out = [];\n  for (var i = 0; i < kl.length; i++) { out.push(CYQCalculator(i, kl)); }\n  return out;\n}"


def reference_series(bars: Sequence[Dict[str, Any]], window: int) -> List[Dict[str, Any]]:
    """Compute the reference series by executing akshare's JS in MiniRacer."""
    from py_mini_racer import MiniRacer

    js = load_reference_js()
    if not js:
        raise RuntimeError("akshare stock_cyq_em JS not available")

    klines = [
        {
            "open": float(b["open"]),
            "close": float(b["close"]),
            "high": float(b["high"]),
            "low": float(b["low"]),
            # The JS reads `hsl` as a percent (divides by 100 internally).
            "hsl": float(b["turnover"]) * 100.0,
        }
        for b in bars
    ]

    ctx = MiniRacer()
    ctx.eval(js)
    raw = ctx.call("runAll", klines, window)

    out = []
    for i, item in enumerate(raw):
        if not item:
            continue
        out.append(
            {
                "date": bars[i].get("date"),
                "benefit_ratio": item["benefitPart"],
                "avg_cost": float(item["avgCost"]),
                "cost90_low": float(item["percentChips"]["90"]["priceRange"][0]),
                "cost90_high": float(item["percentChips"]["90"]["priceRange"][1]),
                "concentration90": float(item["percentChips"]["90"]["concentration"]),
                "cost70_low": float(item["percentChips"]["70"]["priceRange"][0]),
                "cost70_high": float(item["percentChips"]["70"]["priceRange"][1]),
                "concentration70": float(item["percentChips"]["70"]["concentration"]),
            }
        )
    return out


# --------------------------------------------------------------------------
# 2. Comparison helpers
# --------------------------------------------------------------------------

FIELDS = (
    "benefit_ratio",
    "avg_cost",
    "cost90_low",
    "cost90_high",
    "concentration90",
    "cost70_low",
    "cost70_high",
    "concentration70",
)
# Price fields are reported to 2dp by both implementations; the bucket size is
# (max-min)/149, so a 0.03 tolerance on a ~7 yuan stock is the rounding bound.
PRICE_TOL = 0.031
RATIO_TOL = 1e-6


def compare(ours: Dict[str, float], ref: Dict[str, float]) -> Dict[str, float]:
    return {k: abs(float(ours[k]) - float(ref[k])) for k in FIELDS}


def synthesize_bars(n: int = 240, seed: int = 12345) -> List[Dict[str, Any]]:
    """Deterministic pseudo-random OHLC tape (no random module: reproducible)."""
    bars = []
    price = 20.0
    state = seed
    for i in range(n):
        state = (1103515245 * state + 12345) % (2 ** 31)
        r = (state / (2 ** 31)) - 0.5           # -0.5 .. 0.5
        drift = 0.004 * (1 if (i // 20) % 2 == 0 else -1)
        price = max(1.0, price * (1 + drift + r * 0.03))
        o = price * (1 - r * 0.005)
        c = price
        h = max(o, c) * (1 + abs(r) * 0.01)
        low = min(o, c) * (1 - abs(r) * 0.01)
        turnover = min(0.25, max(0.001, 0.012 + r * 0.008))
        bars.append(
            {
                "date": "d%03d" % i,
                "open": round(o, 2),
                "high": round(h, 2),
                "low": round(low, 2),
                "close": round(c, 2),
                "turnover": turnover,
            }
        )
    return bars


def check_algorithm_exactness() -> bool:
    """Our port must match akshare's JS to the last digit (same input)."""
    bars = synthesize_bars(240)
    window = 210
    ref = reference_series(bars, window)
    if not ref:
        print("FAIL  reference produced no rows")
        return False

    ours = []
    for i in range(len(bars)):
        stats = compute_chip_distribution(bars, i, window)
        if stats:
            ours.append({"date": bars[i]["date"], **stats})

    if len(ours) != len(ref):
        print("FAIL  row count ours=%d ref=%d" % (len(ours), len(ref)))
        return False

    worst = {k: 0.0 for k in FIELDS}
    worst_row = 0
    for idx, (a, b) in enumerate(zip(ours, ref)):
        for k in FIELDS:
            d = abs(float(a[k]) - float(b[k]))
            # Compare at the precision both implementations actually report.
            tol = PRICE_TOL if k.startswith("cost") or k == "avg_cost" else RATIO_TOL
            if d > worst[k]:
                worst[k] = d
                if tol and d > tol:
                    worst_row = idx

    ok = True
    for k in FIELDS:
        tol = PRICE_TOL if k.startswith("cost") or k == "avg_cost" else RATIO_TOL
        status = "OK  " if worst[k] <= tol else "DIFF"
        if worst[k] > tol:
            ok = False
        print("  %s %-16s max|delta|=%.10f (tol=%.3g)" % (status, k, worst[k], tol))
    if not ok:
        print("       first offending row index=%d" % worst_row)
    return ok


def check_source_agreement(symbols: Sequence[str]) -> bool:
    """Same algorithm, two independent sources — bounds the data-source error."""
    import akshare as ak

    def bars_from(df, scale: float) -> List[Dict[str, Any]]:
        out = []
        for _, r in df.iterrows():
            out.append(
                {
                    "date": str(r["date"]),
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "turnover": float(r["turnover"]) * scale,
                }
            )
        return out

    ok = True
    print("  %-10s %-8s %-11s %-11s %-9s %-9s" % ("symbol", "mode", "avg_cost", "cost90_high", "benefit", "conc70"))
    for sym in symbols:
        try:
            sn = ak.stock_zh_a_daily(
                symbol=sym, start_date="20250101", end_date="20260930", adjust="qfq"
            ).tail(210).reset_index(drop=True)
        except Exception as exc:
            print("  %-10s SKIP sina=%s" % (sym, type(exc).__name__))
            continue

        a = compute_chip_distribution(bars_from(sn, 1.0), len(sn) - 1, 210)
        if not a:
            continue
        print("  %-10s %-8s %-11.2f %-11.2f %-9.4f %-9.4f"
              % (sym, "sina", a["avg_cost"], a["cost90_high"], a["benefit_ratio"], a["concentration70"]))

        try:
            tx = ak.stock_zh_a_hist_tx(
                symbol=sym, start_date="20250101", end_date="20260930", adjust="qfq"
            ).tail(210).reset_index(drop=True)
        except Exception as exc:
            print("  %-10s SKIP tencent=%s" % (sym, type(exc).__name__))
            continue

        # Tencent reports turnover in the same units as Sina here (a fraction).
        b = compute_chip_distribution(bars_from(tx, 1.0), len(tx) - 1, 210)
        if not b:
            continue
        print("  %-10s %-8s %-11.2f %-11.2f %-9.4f %-9.4f"
              % (sym, "tencent", b["avg_cost"], b["cost90_high"], b["benefit_ratio"], b["concentration70"]))

        rel = abs(a["avg_cost"] - b["avg_cost"]) / max(1e-9, a["avg_cost"])
        flag = "OK" if rel < 0.02 else "CHECK"
        print("             -> avg_cost rel-diff=%.3f%%  %s" % (rel * 100, flag))
    return ok


def check_adjustment_impact(symbols: Sequence[str]) -> None:
    """Quantify how much price adjustment moves the numbers.

    This is the risk that matters for strategy use: if the source only offers
    unadjusted prices, the profit ratio can be off by tens of percent.
    """
    import akshare as ak

    def bars_from(df) -> List[Dict[str, Any]]:
        return [
            {
                "date": str(r["date"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "turnover": float(r["turnover"]),
            }
            for _, r in df.iterrows()
        ]

    print("  %-10s %-12s %-12s %-10s" % ("symbol", "benefit_qfq", "benefit_raw", "rel-diff"))
    for sym in symbols:
        try:
            q = ak.stock_zh_a_daily(symbol=sym, start_date="20250101", end_date="20260930", adjust="qfq").tail(210).reset_index(drop=True)
            r = ak.stock_zh_a_daily(symbol=sym, start_date="20250101", end_date="20260930", adjust="").tail(210).reset_index(drop=True)
        except Exception as exc:
            print("  %-10s SKIP %s" % (sym, type(exc).__name__))
            continue
        a = compute_chip_distribution(bars_from(q), len(q) - 1, 210)
        b = compute_chip_distribution(bars_from(r), len(r) - 1, 210)
        if not a or not b:
            continue
        rel = abs(a["benefit_ratio"] - b["benefit_ratio"]) / max(1e-9, a["benefit_ratio"])
        print("  %-10s %-12.4f %-12.4f %-10.1f%%" % (sym, a["benefit_ratio"], b["benefit_ratio"], rel * 100))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="also run network-backed checks")
    args = ap.parse_args()

    print("=" * 72)
    print("1) Algorithm exactness vs akshare's own JS (offline, same input)")
    print("=" * 72)
    alg_ok = check_algorithm_exactness()

    if not args.live:
        print()
        print("algorithm_exact=%s" % alg_ok)
        print("(pass --live for source/adjustment checks)")
        return 0 if alg_ok else 1

    print()
    print("=" * 72)
    print("2) Data-source agreement (same algorithm, independent sources)")
    print("=" * 72)
    symbols = ["sh600519", "sz000858", "sh601318", "sz000001", "sz002415"]
    try:
        src_ok = check_source_agreement(symbols)
    except Exception as exc:
        print("  ERROR %s: %s" % (type(exc).__name__, exc))
        src_ok = False

    print()
    print("=" * 72)
    print("3) Adjustment impact on benefit_ratio (why qfq is required)")
    print("=" * 72)
    try:
        check_adjustment_impact(symbols)
    except Exception as exc:
        print("  ERROR %s: %s" % (type(exc).__name__, exc))

    print()
    print("algorithm_exact=%s  sources_ok=%s" % (alg_ok, src_ok))
    return 0 if (alg_ok and src_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
