"""System event pools and the backfill driver."""
import datetime as dt

from app.services import events_data, hithink_events_sync


def test_pool_definitions_cover_the_four_boards():
    codes = {item[0] for item in hithink_events_sync.UNIVERSE_POOLS}
    assert codes == {
        "hithink_limit_up",
        "hithink_limit_up_ladder",
        "hithink_dragon_tiger",
        "hithink_hot_rank",
    }
    assert all(item[2] in events_data.EVENT_TYPES for item in hithink_events_sync.UNIVERSE_POOLS)


def test_refresh_pools_marks_snapshot_only_and_sets_history_from(monkeypatch):
    captured = []

    class _FakeService:
        def upsert_system_universe(self, **kwargs):
            captured.append(kwargs)
            return {"code": kwargs["code"], "members": len(kwargs["members"])}

    monkeypatch.setattr("app.services.universe.UniverseService", _FakeService)
    monkeypatch.setattr(
        events_data,
        "query_events",
        lambda **kwargs: {
            "items": [
                {"market": "CNStock", "symbol": "600519.SH", "name": "贵州茅台", "rank": 1},
                {"market": "CNStock", "symbol": "000001.SZ", "name": "平安银行", "rank": 2},
            ],
            "total": 2,
        },
    )
    monkeypatch.setattr(hithink_events_sync, "_earliest_event_date", lambda: dt.date(2026, 9, 1))

    day = dt.date(2026, 9, 16)
    report = hithink_events_sync.refresh_universe_pools(day)

    assert report["failures"] == []
    assert len(captured) == 4
    for call in captured:
        assert call["metadata"]["snapshot_only"] is True
        assert call["metadata"]["snapshot_as_of"] == "2026-09-01"
        assert call["valid_from"] == dt.date(2026, 9, 1)
        assert len(call["members"]) == 2


def test_history_from_is_clamped_to_the_backfill_window(monkeypatch):
    monkeypatch.setenv("HITHINK_EVENTS_BACKFILL_DAYS", "30")
    monkeypatch.setattr(hithink_events_sync, "_earliest_event_date", lambda: dt.date(2020, 1, 1))
    assert hithink_events_sync._pool_history_from(dt.date(2026, 9, 16)) == dt.date(2026, 8, 17)


def test_history_from_uses_stored_events_when_they_are_newer(monkeypatch):
    monkeypatch.setenv("HITHINK_EVENTS_BACKFILL_DAYS", "365")
    monkeypatch.setattr(hithink_events_sync, "_earliest_event_date", lambda: dt.date(2026, 9, 1))
    assert hithink_events_sync._pool_history_from(dt.date(2026, 9, 16)) == dt.date(2026, 9, 1)


def test_due_now_requires_close_time_trading_day_and_missing_rows(monkeypatch):
    moment = dt.datetime(2026, 9, 16, 15, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    monkeypatch.setenv("HITHINK_EVENTS_SYNC_HOUR", "15")
    monkeypatch.setenv("HITHINK_EVENTS_SYNC_MINUTE", "10")
    monkeypatch.setattr(hithink_events_sync, "is_trading_day", lambda day: True, raising=False)
    monkeypatch.setattr(hithink_events_sync, "_sync_hhmm", lambda: (15, 10))
    monkeypatch.setattr(
        "app.data_sources.hithink_finance.is_trading_day", lambda day: True
    )
    monkeypatch.setattr(hithink_events_sync, "last_synced_trade_date", lambda: dt.date(2026, 9, 15))
    assert hithink_events_sync.due_now(moment) is True

    monkeypatch.setattr(hithink_events_sync, "last_synced_trade_date", lambda: dt.date(2026, 9, 16))
    assert hithink_events_sync.due_now(moment) is False

    monkeypatch.setattr(hithink_events_sync, "last_synced_trade_date", lambda: dt.date(2026, 9, 15))
    earlier = dt.datetime(2026, 9, 16, 14, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    assert hithink_events_sync.due_now(earlier) is False

    monkeypatch.setattr("app.data_sources.hithink_finance.is_trading_day", lambda day: False)
    assert hithink_events_sync.due_now(moment) is False


def test_sync_disabled_skips_everything(monkeypatch):
    monkeypatch.setenv("HITHINK_EVENTS_SYNC_ENABLED", "false")
    assert hithink_events_sync.sync_enabled() is False
    assert hithink_events_sync.schedule_due() == {"skipped": "disabled"}
    assert hithink_events_sync.run_sync(event_types=["limit_up"]) == {"skipped": "disabled"}
