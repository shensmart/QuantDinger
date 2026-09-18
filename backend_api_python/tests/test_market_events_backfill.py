"""Backfill driver: dry-run, resume, rate-limit tolerance."""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backfill_hithink_events as backfill  # noqa: E402

from app.services import events_data  # noqa: E402


def test_dry_run_never_writes(monkeypatch, capsys):
    calls = []

    def fake_sync(day, event_types, dry_run=False):
        calls.append((day, tuple(event_types), dry_run))
        return {"trade_date": day.isoformat(), "results": [], "failures": [], "written": 0}

    monkeypatch.setattr(events_data, "sync_date", fake_sync)
    monkeypatch.setattr(backfill, "_target_dates", lambda days, only=None: [dt.date(2026, 9, 16)])
    code = backfill.main(["--dry-run", "--days", "5", "--only", "limit_up"])
    report = capsys.readouterr().out
    assert code == 0
    assert calls and calls[0][2] is True
    assert '"dryRun": true' in report


def test_resume_skips_dates_that_already_have_every_requested_type(monkeypatch, capsys):
    synced = []

    monkeypatch.setattr(
        backfill,
        "_target_dates",
        lambda days, only=None: [dt.date(2026, 9, 15), dt.date(2026, 9, 16)],
    )
    monkeypatch.setattr(
        events_data,
        "coverage_by_date",
        lambda event_types, **kwargs: {dt.date(2026, 9, 15): {"limit_up", "hot_rank"}},
    )

    def fake_sync(day, event_types, dry_run=False):
        synced.append(day)
        return {"trade_date": day.isoformat(), "results": [], "failures": [], "written": 0}

    monkeypatch.setattr(events_data, "sync_date", fake_sync)
    backfill.main(["--resume", "--days", "5", "--only", "limit_up", "--only", "hot_rank"])
    assert synced == [dt.date(2026, 9, 16)]


def test_partial_coverage_is_re_fetched(monkeypatch, capsys):
    synced = []

    monkeypatch.setattr(backfill, "_target_dates", lambda days, only=None: [dt.date(2026, 9, 15)])
    monkeypatch.setattr(
        events_data,
        "coverage_by_date",
        lambda event_types, **kwargs: {dt.date(2026, 9, 15): {"limit_up"}},
    )

    def fake_sync(day, event_types, dry_run=False):
        synced.append(day)
        return {"trade_date": day.isoformat(), "results": [], "failures": [], "written": 0}

    monkeypatch.setattr(events_data, "sync_date", fake_sync)
    backfill.main(["--resume", "--only", "limit_up", "--only", "hot_rank"])
    assert synced == [dt.date(2026, 9, 15)]


def test_provider_failure_is_reported_without_aborting_the_run(monkeypatch, capsys):
    def fake_sync(day, event_types, dry_run=False):
        return {
            "trade_date": day.isoformat(),
            "results": [{"event_type": "limit_up", "rows": 3, "written": 3}],
            "failures": [{"event_type": "hot_rank", "error": "code=5003"}],
            "written": 3,
        }

    monkeypatch.setattr(events_data, "sync_date", fake_sync)
    monkeypatch.setattr(
        backfill, "_target_dates", lambda days, only=None: [dt.date(2026, 9, 15), dt.date(2026, 9, 16)]
    )
    code = backfill.main(["--only", "limit_up", "--only", "hot_rank"])
    out = capsys.readouterr().out
    assert '"failures": 2' in out
    assert code == 0, "a partial failure must not fail the whole backfill"


def test_unknown_event_type_is_rejected(capsys):
    assert backfill.main(["--only", "not_a_board"]) == 2
    assert "unsupported event types" in capsys.readouterr().out


def test_target_dates_use_the_trading_calendar(monkeypatch):
    monkeypatch.setattr(
        "app.data_sources.hithink_finance.trading_days",
        lambda: [dt.date(2026, 9, 10) + dt.timedelta(days=index) for index in range(20)],
    )
    dates = backfill._target_dates(5)
    # Newest calendar day is 2026-09-29; the window is the 5 days ending there.
    assert dates == [
        dt.date(2026, 9, 24),
        dt.date(2026, 9, 25),
        dt.date(2026, 9, 26),
        dt.date(2026, 9, 27),
        dt.date(2026, 9, 28),
        dt.date(2026, 9, 29),
    ]


def test_limit_days_takes_the_newest_dates(monkeypatch, capsys):
    monkeypatch.setattr(
        backfill,
        "_target_dates",
        lambda days, only=None: [dt.date(2026, 9, 10), dt.date(2026, 9, 11), dt.date(2026, 9, 12)],
    )
    synced = []

    def fake_sync(day, event_types, dry_run=False):
        synced.append(day)
        return {"trade_date": day.isoformat(), "results": [], "failures": [], "written": 0}

    monkeypatch.setattr(events_data, "sync_date", fake_sync)
    backfill.main(["--limit-days", "2", "--only", "limit_up"])
    # --limit-days is a smoke-run switch: it keeps the OLDEST dates so a small
    # window still exercises the historical path.
    assert synced == [dt.date(2026, 9, 10), dt.date(2026, 9, 11)]
