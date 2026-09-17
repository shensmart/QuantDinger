"""Strategy V2 event API: dependency discovery, manifest, binding, readiness."""
import datetime as dt

import pandas as pd
import pytest

from app.services.strategy_v2 import StrategyV2BacktestRunner
from app.services.strategy_v2.contract import StrategyV2ContractError, compile_strategy_v2
from app.services.strategy_v2.readiness import validate_events


CODE = """
def initialize(context):
    context.set_universe(["CNStock:600519"])
    context.subscribe(frequency="1d")

def handle_data(context, data):
    events = get_events(["limit_up", "hot_rank"])
    context.log("events=%s" % len(events))
"""


def test_event_dependencies_are_discovered_and_serialized():
    manifest = compile_strategy_v2(CODE).manifest
    assert manifest.event_dependencies == ("hot_rank", "limit_up")
    assert manifest.metadata()["eventDependencies"] == ["hot_rank", "limit_up"]


def test_get_events_is_bound_in_backtest_and_live_namespaces():
    """Both _bind_runtime_api copies must expose get_events (regression guard)."""
    import inspect

    from app.services.strategy_v2 import runtime

    source = inspect.getsource(runtime)
    assert source.count('"get_events": ctx.get_events,') == 2


def test_events_reach_the_strategy_namespace():
    frame = pd.DataFrame(
        {
            "open": [10.0] * 5,
            "high": [10.0] * 5,
            "low": [10.0] * 5,
            "close": [10.0] * 5,
            "volume": [100.0] * 5,
            "limit_up": [0.0, 1.0, 1.0, 1.0, 1.0],
            "hot_rank": [0.0, 3.0, 3.0, 3.0, 3.0],
        },
        index=pd.date_range("2026-01-01", periods=5, freq="D", tz="UTC"),
    )
    result = StrategyV2BacktestRunner(
        code=CODE,
        frames={"CNStock:600519": frame},
        initial_capital=10000,
    ).run()
    assert result["logs"]
    assert result["logs"][0].startswith("events=")


def test_get_events_only_reads_the_visible_bar():
    frame = pd.DataFrame(
        {
            "open": [10.0] * 4,
            "high": [10.0] * 4,
            "low": [10.0] * 4,
            "close": [10.0] * 4,
            "volume": [100.0] * 4,
            "limit_up": [0.0, 0.0, 1.0, 1.0],
        },
        index=pd.date_range("2026-01-01", periods=4, freq="D", tz="UTC"),
    )
    code = """
def initialize(context):
    context.set_universe(["CNStock:600519"])
    context.subscribe(frequency="1d")

def handle_data(context, data):
    events = get_events(["limit_up"])
    context.log("v=%s" % events.iloc[0]["limit_up"])
"""
    result = StrategyV2BacktestRunner(
        code=code, frames={"CNStock:600519": frame}, initial_capital=10000
    ).run()
    values = [log.split("=")[1] for log in result["logs"]]
    # Bar D sees only events published on or before D-1 (board rows are stamped
    # with the trade date, so the bar-3 board first shows up on bar 4).
    assert values == ["0.0", "0.0", "0.0", "1.0"]


def test_validate_events_rejects_a_column_that_is_always_zero():
    frame = pd.DataFrame(
        {"limit_up": [0.0, 0.0]}, index=pd.date_range("2026-01-01", periods=2, freq="D")
    )
    with pytest.raises(StrategyV2ContractError) as exc:
        validate_events({"CNStock:600519": frame}, {"limit_up"})
    assert "strategyV2.eventsUnavailable" in str(exc.value)


def test_validate_events_accepts_populated_columns():
    frame = pd.DataFrame(
        {"limit_up": [0.0, 1.0]}, index=pd.date_range("2026-01-01", periods=2, freq="D")
    )
    validate_events({"CNStock:600519": frame}, {"limit_up"})


def test_validate_events_rejects_unknown_event_fields():
    frame = pd.DataFrame(
        {"limit_up": [1.0]}, index=pd.date_range("2026-01-01", periods=1, freq="D")
    )
    with pytest.raises(StrategyV2ContractError) as exc:
        validate_events({"CNStock:600519": frame}, {"dividend_yield"})
    assert "strategyV2.eventsUnavailable" in str(exc.value)


def test_validate_events_honours_as_of_cutoff():
    frame = pd.DataFrame(
        {"limit_up": [0.0, 1.0]}, index=pd.date_range("2026-01-01", periods=2, freq="D")
    )
    with pytest.raises(StrategyV2ContractError):
        validate_events({"CNStock:600519": frame}, {"limit_up"}, as_of=dt.date(2026, 1, 1))
