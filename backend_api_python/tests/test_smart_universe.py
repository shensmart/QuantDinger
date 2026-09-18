"""Smart universes: condition-backed pools and their point-in-time contract."""

import datetime as dt

import pytest

from app.services.symbol_tags import SymbolTagError
from app.services.universe import (
    EDITABLE_UNIVERSE_TYPES,
    SUPPORTED_UNIVERSE_TYPES,
    UniverseError,
    UniverseService,
)


def test_smart_is_a_supported_and_editable_type():
    assert "smart" in SUPPORTED_UNIVERSE_TYPES
    assert "smart" in EDITABLE_UNIVERSE_TYPES


def test_smart_universe_stores_conditions_and_a_snapshot_floor(monkeypatch):
    service = UniverseService()
    captured = {}

    class _Cur:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params

        def fetchone(self):
            return {"id": 42}

        def close(self):
            pass

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return _Cur()

        def commit(self):
            pass

    monkeypatch.setattr("app.services.universe.get_db_connection", lambda: _Db())
    monkeypatch.setattr(
        service, "get_universe", lambda user_id, universe_id: {"id": universe_id}
    )
    monkeypatch.setattr(
        "app.services.symbol_tags.tag_conditions_floor", lambda conditions: "2026-09-01"
    )

    result = service.create_smart(1, {
        "name": "涨停 且 半导体",
        "conditions": [{"tag_code": "event_limit_up"}],
    })
    assert result == {"id": 42, "universe_id": 42} or result["id"] == 42

    import json
    metadata = json.loads(captured["params"][-1])
    assert metadata["source"] == "symbol_tags"
    assert metadata["conditions"] == [{"tag_code": "event_limit_up"}]
    # A snapshot-only tag pins the pool to the snapshot date.
    assert metadata["snapshot_only"] is True
    assert metadata["snapshot_as_of"] == "2026-09-01"
    assert metadata["point_in_time"] is False


def test_smart_universe_pure_event_conditions_are_point_in_time(monkeypatch):
    service = UniverseService()
    captured = {}

    class _Cur:
        def execute(self, sql, params=None):
            captured["params"] = params

        def fetchone(self):
            return {"id": 7}

        def close(self):
            pass

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return _Cur()

        def commit(self):
            pass

    monkeypatch.setattr("app.services.universe.get_db_connection", lambda: _Db())
    monkeypatch.setattr(service, "get_universe", lambda user_id, universe_id: {"id": universe_id})
    monkeypatch.setattr(
        "app.services.symbol_tags.tag_conditions_floor", lambda conditions: ""
    )
    service.create_smart(1, {"name": "涨停池", "conditions": [{"tag_code": "event_limit_up"}]})

    import json
    metadata = json.loads(captured["params"][-1])
    assert metadata["point_in_time"] is True
    assert metadata["snapshot_only"] is False
    assert metadata["snapshot_as_of"] == ""


def test_smart_universe_rejects_empty_conditions(monkeypatch):
    service = UniverseService()
    with pytest.raises(UniverseError):
        service.create_smart(1, {"name": "empty", "conditions": []})


def test_smart_members_resolve_through_the_tag_service(monkeypatch):
    service = UniverseService()
    universe = {
        "code": "limit-up-abc",
        "metadata_json": {"conditions": [{"tag_code": "event_limit_up"}]},
    }
    captured = {}

    class _FakeTagService:
        def screen(self, conditions, **kwargs):
            captured["conditions"] = conditions
            captured.update(kwargs)
            return {
                "items": [
                    {"market": "CNStock", "symbol": "600519.SH", "name": "贵州茅台",
                     "rank": 1, "tags": ["event_limit_up"]}
                ],
                "total": 1,
            }

    monkeypatch.setattr(
        "app.services.symbol_tags.get_symbol_tag_service", lambda: _FakeTagService()
    )
    members = service._smart_members(
        universe, as_of=dt.date(2026, 9, 17), start=dt.date(2026, 9, 1)
    )
    assert [item["symbol"] for item in members] == ["600519.SH"]
    assert members[0]["metadata"] == {"tags": ["event_limit_up"]}
    # A backtest pool must ask for the whole window, not one day.
    assert captured["date_range"] == [dt.date(2026, 9, 1), dt.date(2026, 9, 17)]
    assert captured["with_quotes"] is False


def test_smart_universe_without_conditions_resolves_empty(monkeypatch):
    service = UniverseService()
    assert service._smart_members({"code": "x", "metadata_json": {}}, as_of=dt.date(2026, 9, 17)) == []


def test_tag_reference_is_recognized_and_normalized():
    from app.services.strategy_v2.contract import DiscoveryContext
    from app.services.strategy_v2.instruments import (
        InstrumentParseError,
        normalize_pool_reference,
    )

    assert normalize_pool_reference("tag:Event_Limit_Up") == "TAG:event_limit_up"
    assert normalize_pool_reference("POOL:My-Pool") == "POOL:my-pool"
    with pytest.raises(InstrumentParseError):
        normalize_pool_reference("TAG:")

    context = DiscoveryContext()
    context.set_universe(pool="TAG:event_limit_up")
    assert context.universe_reference == "TAG:event_limit_up"


def test_stale_universe_reference_fails_loudly():
    """Sector universes are gone, so an old POOL: reference must error."""
    from app.services.strategy_v2.contract import StrategyManifest
    from app.services.strategy_v2.service import StrategyV2BacktestService

    class _Manifest:
        class universe:
            kind = "dynamic"
            reference = "POOL:cn_industry_881101_ti"

    service = StrategyV2BacktestService()
    service.universe_service = type("U", (), {"list_universes": lambda self, uid: []})()
    with pytest.raises(Exception) as excinfo:
        service.resolve_candidates(
            user_id=1,
            manifest=_Manifest(),
            start_date=dt.datetime(2026, 1, 1),
            end_date=dt.datetime(2026, 9, 17),
        )
    assert "strategyV2.universeNotFound" in str(excinfo.value)


def test_tag_reference_rejects_early_backtests_for_snapshot_tags(monkeypatch):
    from app.services.strategy_v2.service import StrategyV2BacktestService

    class _Manifest:
        class universe:
            kind = "dynamic"
            reference = "TAG:cn_industry_881101_ti"

    class _FakeTagService:
        def get_tag(self, code):
            return {
                "code": code, "category": "industry", "point_in_time": False,
                "as_of_date": "2026-09-17", "status": "active", "rule": {},
            }

    monkeypatch.setattr(
        "app.services.symbol_tags.get_symbol_tag_service", lambda: _FakeTagService()
    )
    service = StrategyV2BacktestService()
    with pytest.raises(Exception) as excinfo:
        service.resolve_candidates(
            user_id=1,
            manifest=_Manifest(),
            start_date=dt.datetime(2026, 1, 1),
            end_date=dt.datetime(2026, 9, 17),
        )
    assert "strategyV2.universeHistoryUnavailable" in str(excinfo.value)
