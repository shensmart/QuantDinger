"""System event pools and the backfill driver."""
import datetime as dt
import pathlib

from app.services import events_data, hithink_events_sync


def test_event_tags_cover_the_boards():
    """Boards are event tags now, mapped to qd_market_events.event_type."""
    from app.services.symbol_tags import CATEGORIES

    expected = {
        "event_limit_up": ["limit_up"],
        "event_limit_down": ["limit_down"],
        "event_limit_break": ["limit_break"],
        "event_limit_up_ladder": ["limit_up_ladder"],
        "event_hot_rank": ["hot_rank"],
    }
    migration = (pathlib.Path(__file__).resolve().parent.parent
                 / "migrations" / "20260918_symbol_tags.sql").read_text()
    for code, event_types in expected.items():
        assert f"'{code}'" in migration
        for event_type in event_types:
            assert f'"{event_type}"' in migration
    assert "event" in CATEGORIES
    assert all(item in events_data.EVENT_TYPES for item in ("limit_up", "limit_up_ladder", "hot_rank"))


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
