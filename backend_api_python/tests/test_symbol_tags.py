"""Symbol tag service: migration shape, screening semantics, and refresh rules.

These run against the project's real database (as its other tests do), so they
cover the SQL the service actually issues instead of a mock of it.

All monkeypatching targets ``SymbolTagService`` *class* attributes, never the
singleton instance: an instance attribute whose value lives on the class leaves
a shadow behind that leaks into every later test in the session.
"""

import datetime as dt
import json
import pathlib

import pytest

from app.services import symbol_tags as module
from app.services.symbol_tags import (
    SymbolTagError,
    get_symbol_tag_service,
    normalize_conditions,
    tag_conditions_floor,
)

SymbolTagService = module.SymbolTagService

MIGRATION = (
    pathlib.Path(__file__).resolve().parent.parent
    / "migrations"
    / "20260918_symbol_tags.sql"
)




def test_migration_is_idempotent_and_destructive_by_design():
    sql = MIGRATION.read_text()
    # Re-running must not duplicate tags or members.
    assert "ON CONFLICT (code) DO NOTHING" in sql
    assert "ON CONFLICT (tag_id, market, symbol) DO NOTHING" in sql
    # The member-count guard must abort before the destructive delete.
    guard = sql.index("symbol tag migration incomplete")
    delete = sql.index("DELETE FROM qd_universes")
    assert guard < delete
    # Only the superseded sectors/boards are removed.
    removed = sql.split("DELETE FROM qd_universes")[1]
    for keep in ("csi300", "watchlist", "sp500"):
        assert f"'{keep}'" not in removed


def test_seeded_tag_definitions_match_the_service_contract():
    sql = MIGRATION.read_text()
    for code in ("event_limit_up", "event_dragon_tiger", "quote_change_up", "quote_amount"):
        assert f"'{code}'" in sql
    # Quote tags must not claim point-in-time membership.
    quote_section = sql.lower().split("quote threshold tags")[1]
    assert "false," in quote_section
    # Event tags must, and they resolve from qd_market_events.
    event_section = sql.split("-- 2. Event tags", 1)[1].split("-- 3.", 1)[0]
    assert "TRUE," in event_section
    assert "event_types" in event_section
    # A metric without a data source is seeded as data_required, not active.
    assert "quote_volume_ratio" in sql
    assert "data_required" in sql


def test_conditions_reject_empty_and_normalize_case():
    with pytest.raises(SymbolTagError):
        normalize_conditions([])
    with pytest.raises(SymbolTagError):
        normalize_conditions([{"tag_code": ""}])
    with pytest.raises(SymbolTagError):
        normalize_conditions([{"tag_code": "a", "value": "not-a-number"}])
    assert normalize_conditions([{"tagCode": "Quote_Change_Up"}]) == [
        {"tag_code": "quote_change_up"}
    ]
    assert normalize_conditions([{"tag_code": "quote_change_up", "value": 5}]) == [
        {"tag_code": "quote_change_up", "value": 5.0}
    ]


# ---------------------------------------------------------------------------
# Screening semantics
# ---------------------------------------------------------------------------


def test_screen_or_within_category_and_and_across(monkeypatch):
    """Two industries OR together; an industry plus an event ANDs."""
    tags = {
        "cn_industry_a": {"code": "cn_industry_a", "category": "industry"},
        "cn_industry_b": {"code": "cn_industry_b", "category": "industry"},
        "event_limit_up": {"code": "event_limit_up", "category": "event"},
    }
    members = {
        "cn_industry_a": [
            {"market": "CNStock", "symbol": "600519.SH", "name": "A", "tags": []},
            {"market": "CNStock", "symbol": "000001.SZ", "name": "B", "tags": []},
        ],
        "cn_industry_b": [
            {"market": "CNStock", "symbol": "000001.SZ", "name": "B", "tags": []},
        ],
        "event_limit_up": [
            {"market": "CNStock", "symbol": "600519.SH", "name": "A", "tags": []},
        ],
    }
    monkeypatch.setattr(SymbolTagService, "_tag_row", lambda self, code: tags[code])
    monkeypatch.setattr(
        SymbolTagService,
        "_members_for_condition",
        lambda self, condition, day, start, end: members[condition["tag"]["code"]],
    )

    both_industries = get_symbol_tag_service().screen(
        [{"tag_code": "cn_industry_a"}, {"tag_code": "cn_industry_b"}],
        as_of="2026-09-17",
        with_quotes=False,
    )
    # OR inside the category: 600519 only in A, 000001 only in B -> both survive.
    assert sorted(item["symbol"] for item in both_industries["items"]) == [
        "000001.SZ", "600519.SH",
    ]

    industry_and_event = get_symbol_tag_service().screen(
        [{"tag_code": "cn_industry_a"}, {"tag_code": "event_limit_up"}],
        as_of="2026-09-17",
        with_quotes=False,
    )
    # AND across categories: only the symbol on the board survives.
    assert [item["symbol"] for item in industry_and_event["items"]] == ["600519.SH"]
    assert sorted(industry_and_event["items"][0]["tags"]) == [
        "cn_industry_a", "event_limit_up",
    ]


def test_screen_returns_empty_when_a_category_matches_nothing(monkeypatch):
    tags = {
        "cn_industry_a": {"code": "cn_industry_a", "category": "industry"},
        "event_limit_up": {"code": "event_limit_up", "category": "event"},
    }
    monkeypatch.setattr(SymbolTagService, "_tag_row", lambda self, code: tags[code])
    monkeypatch.setattr(
        SymbolTagService,
        "_members_for_condition",
        lambda self, condition, day, start, end: (
            [{"market": "CNStock", "symbol": "600519.SH", "name": "A", "tags": []}]
            if condition["tag"]["code"] == "cn_industry_a"
            else []
        ),
    )
    result = get_symbol_tag_service().screen(
        [{"tag_code": "cn_industry_a"}, {"tag_code": "event_limit_up"}],
        as_of="2026-09-17",
        with_quotes=False,
    )
    assert result["items"] == []
    assert result["total"] == 0


def test_screen_paginates_after_the_full_intersection(monkeypatch):
    """Paging must not change *which* symbols match, only how many are returned."""
    tags = {"cn_industry_a": {"code": "cn_industry_a", "category": "industry"}}
    rows = [
        {"market": "CNStock", "symbol": f"60000{index}.SH", "name": str(index), "tags": []}
        for index in range(5)
    ]
    monkeypatch.setattr(SymbolTagService, "_tag_row", lambda self, code: tags[code])
    monkeypatch.setattr(
        SymbolTagService,
        "_members_for_condition",
        lambda self, condition, day, start, end: rows,
    )
    service = get_symbol_tag_service()
    page_one = service.screen([{"tag_code": "cn_industry_a"}], limit=2, offset=0, with_quotes=False)
    page_two = service.screen([{"tag_code": "cn_industry_a"}], limit=2, offset=2, with_quotes=False)
    assert page_one["total"] == page_two["total"] == 5
    assert [item["symbol"] for item in page_one["items"]] == ["600000.SH", "600001.SH"]
    assert [item["symbol"] for item in page_two["items"]] == ["600002.SH", "600003.SH"]
    assert all(item["tags"] == ["cn_industry_a"] for item in page_one["items"] + page_two["items"])


# ---------------------------------------------------------------------------
# Quote thresholds
# ---------------------------------------------------------------------------


def _patch_quote(monkeypatch, rows, rules):
    monkeypatch.setattr(
        SymbolTagService, "quote_rows",
        classmethod(lambda cls, day, fresh=False: rows),
    )
    monkeypatch.setattr(
        SymbolTagService, "list_tags",
        lambda self, **kwargs: rules if kwargs.get("category") == "quote" else [],
    )


def test_quote_threshold_operator_boundaries(monkeypatch):
    """Exactly-at-threshold matches for gte/lte; missing values never match."""
    rows = [
        {"market": "CNStock", "symbol": "600000.SH", "name": "X",
         "change_pct": 9.8, "amount": 1_000_000_000.0, "volume": 1.0, "price": 10.0},
        {"market": "CNStock", "symbol": "600001.SH", "name": "Y",
         "change_pct": 9.79, "amount": None, "volume": 1.0, "price": 10.0},
        {"market": "CNStock", "symbol": "600002.SH", "name": "Z",
         "change_pct": -9.8, "amount": 2_000_000_000.0, "volume": 1.0, "price": 10.0},
    ]
    _patch_quote(monkeypatch, rows, [
        {"code": "quote_change_up", "status": "active",
         "rule": {"metric": "change_pct", "op": "gte", "value": 9.8}},
        {"code": "quote_change_down", "status": "active",
         "rule": {"metric": "change_pct", "op": "lte", "value": -9.8}},
        {"code": "quote_amount", "status": "active",
         "rule": {"metric": "amount", "op": "gte", "value": 1_000_000_000.0}},
    ])
    service = get_symbol_tag_service()
    members = service.quote_members(dt.date(2026, 9, 18))
    by_symbol = {item["symbol"]: item for item in members}
    assert set(by_symbol) == {"600000.SH", "600002.SH"}
    # 600000.SH satisfies both the change and the amount threshold.
    assert by_symbol["600000.SH"]["tags"] == ["quote_change_up", "quote_amount"]
    assert by_symbol["600002.SH"]["tags"] == ["quote_change_down", "quote_amount"]

    # One condition must mean one tag, not the union of every quote tag.
    only_up = service.quote_members(dt.date(2026, 9, 18), only={"quote_change_up"})
    assert [item["symbol"] for item in only_up] == ["600000.SH"]
    # 600001.SH has no amount at all and must not silently match.
    only_amount = service.quote_members(dt.date(2026, 9, 18), only={"quote_amount"})
    assert [item["symbol"] for item in only_amount] == ["600000.SH", "600002.SH"]


def test_quote_conditions_and_across_each_other(monkeypatch):
    """Two thresholds must intersect, never union (the screener's whole point)."""
    rows = [
        {"market": "CNStock", "symbol": "600000.SH", "name": "both",
         "change_pct": 10.0, "amount": 2e9, "volume": 1.0, "price": 10.0},
        {"market": "CNStock", "symbol": "600001.SH", "name": "amount only",
         "change_pct": 1.0, "amount": 2e9, "volume": 1.0, "price": 10.0},
        {"market": "CNStock", "symbol": "600002.SH", "name": "change only",
         "change_pct": 10.0, "amount": 1.0, "volume": 1.0, "price": 10.0},
    ]
    _patch_quote(monkeypatch, rows, [
        {"code": "quote_change_up", "status": "active",
         "rule": {"metric": "change_pct", "op": "gte", "value": 9.8}},
        {"code": "quote_amount", "status": "active",
         "rule": {"metric": "amount", "op": "gte", "value": 1e9}},
    ])
    service = get_symbol_tag_service()
    single = service.screen(
        [{"tag_code": "quote_change_up"}], as_of="2026-09-18", with_quotes=False
    )
    assert sorted(item["symbol"] for item in single["items"]) == ["600000.SH", "600002.SH"]

    combined = service.screen(
        [{"tag_code": "quote_change_up"}, {"tag_code": "quote_amount"}],
        as_of="2026-09-18",
        with_quotes=False,
    )
    assert [item["symbol"] for item in combined["items"]] == ["600000.SH"]
    assert combined["total"] <= single["total"]


def test_quote_rule_override_changes_the_threshold(monkeypatch):
    rows = [
        {"market": "CNStock", "symbol": "600000.SH", "name": "X",
         "change_pct": 5.0, "amount": 0.0, "volume": 1.0, "price": 10.0},
    ]
    _patch_quote(monkeypatch, rows, [
        {"code": "quote_change_up", "status": "active",
         "rule": {"metric": "change_pct", "op": "gte", "value": 9.8}},
    ])
    service = get_symbol_tag_service()
    assert service.quote_members(dt.date(2026, 9, 18)) == []
    relaxed = service.quote_members(
        dt.date(2026, 9, 18), rule_override={"quote_change_up": 4.0}
    )
    assert [item["symbol"] for item in relaxed] == ["600000.SH"]

    # The override must also apply when reached through `screen`.
    screened = service.screen(
        [{"tag_code": "quote_change_up", "value": 4}],
        as_of="2026-09-18",
        with_quotes=False,
    )
    assert [item["symbol"] for item in screened["items"]] == ["600000.SH"]


def test_data_required_quote_tag_is_never_evaluated(monkeypatch):
    """A tag without a data source must be skipped, not treated as passing."""
    rows = [
        {"market": "CNStock", "symbol": "600000.SH", "name": "X",
         "change_pct": 99.0, "amount": 9e9, "volume": 1.0, "price": 10.0},
    ]
    _patch_quote(monkeypatch, rows, [
        {"code": "quote_volume_ratio", "status": "data_required",
         "rule": {"metric": "volume_ratio", "op": "gte", "value": 2}},
    ])
    assert get_symbol_tag_service().quote_members(dt.date(2026, 9, 18)) == []


def test_turnover_rate_is_skipped_without_a_share_count(monkeypatch):
    rows = [
        {"market": "CNStock", "symbol": "600000.SH", "name": "X",
         "change_pct": 0.0, "amount": 0.0, "volume": 1_000_000.0, "price": 10.0},
    ]
    _patch_quote(monkeypatch, rows, [
        {"code": "quote_turnover_rate", "status": "active",
         "rule": {"metric": "turnover_rate", "op": "gte", "value": 10}},
    ])
    monkeypatch.setattr(module, "_shares_outstanding", lambda: {})
    # No share count means no turnover rate, therefore no match (never a pass).
    assert get_symbol_tag_service().quote_members(dt.date(2026, 9, 18)) == []

    # With a share count the metric is volume / shares * 100.
    monkeypatch.setattr(module, "_shares_outstanding", lambda: {"600000.SH": 1_000_000.0})
    matched = get_symbol_tag_service().quote_members(dt.date(2026, 9, 18))
    assert [item["symbol"] for item in matched] == ["600000.SH"]
    assert matched[0]["matched"] == {"quote_turnover_rate": 100.0}


def test_shares_outstanding_is_keyed_by_thscode(monkeypatch):
    """Fundamental snapshots store bare digits; quotes use `600519.SH`."""

    class _Cur:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return [
                {"symbol": "600519", "shares_outstanding": 1_000.0},
                {"symbol": "000001.SZ", "shares_outstanding": 2_000.0},
            ]

        def close(self):
            pass

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return _Cur()

    monkeypatch.setattr(module, "get_db_connection", lambda: _Db())
    shares = module._shares_outstanding()
    assert shares["600519.SH"] == 1_000.0
    assert shares["000001.SZ"] == 2_000.0


# ---------------------------------------------------------------------------
# Event tags
# ---------------------------------------------------------------------------


def test_event_members_are_point_in_time(monkeypatch):
    captured = {}

    class _Cur:
        def execute(self, sql, params=None):
            captured["sql"] = sql
            captured["params"] = params

        def fetchall(self):
            return [{"market": "CNStock", "symbol": "600519.SH", "name": "A",
                     "trade_date": dt.date(2026, 9, 17), "rank": 1}]

        def close(self):
            pass

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return _Cur()

    monkeypatch.setattr(module, "get_db_connection", lambda: _Db())
    tag = {"market": "CNStock", "rule_json": {"event_types": ["limit_up"]}}
    rows = get_symbol_tag_service()._resolve_event_members(
        tag, start=dt.date(2026, 9, 1), end=dt.date(2026, 9, 17)
    )
    # The availability cutoff must be enforced, not just the trade date.
    assert "available_at <=" in captured["sql"]
    assert "trade_date BETWEEN" in captured["sql"]
    assert "limit_up" in captured["params"]
    assert [row["symbol"] for row in rows] == ["600519.SH"]


def test_event_condition_range_widens_the_window(monkeypatch):
    """A `screen` on an event tag must not materialize members."""
    monkeypatch.setattr(
        SymbolTagService, "get_tag",
        lambda self, code: {
            "id": 1, "code": code, "category": "event", "point_in_time": True,
            "as_of_date": "2026-09-17", "rule": {"event_types": ["limit_up"]},
            "metadata": {}, "member_count": 0, "status": "active",
        },
    )
    called = {}

    def _fake_event(self, tag, *, start, end, limit=0):
        called["range"] = (start, end)
        return [{"market": "CNStock", "symbol": "600519.SH", "name": "A",
                 "as_of_date": "2026-09-17", "rank": 1, "metadata": {}}]

    monkeypatch.setattr(SymbolTagService, "_resolve_event_members", _fake_event)
    result = get_symbol_tag_service().screen(
        [{"tag_code": "event_limit_up"}],
        as_of="2026-09-17",
        date_range=["2026-09-01", "2026-09-17"],
        with_quotes=False,
    )
    assert [item["symbol"] for item in result["items"]] == ["600519.SH"]
    assert called["range"] == (dt.date(2026, 9, 1), dt.date(2026, 9, 17))


def test_screen_rejects_an_inverted_date_range():
    with pytest.raises(SymbolTagError):
        get_symbol_tag_service().screen(
            [{"tag_code": "event_limit_up"}],
            as_of="2026-09-17",
            date_range=["2026-09-18", "2026-09-01"],
        )


def test_event_tags_for_symbol_query_uses_jsonb_rule():
    """The per-symbol lookup must join events through rule_json, not a constant."""
    captured = {}

    class _Cur:
        def execute(self, sql, params=None):
            captured["sql"] = sql

        def fetchall(self):
            return []

        def close(self):
            pass

    SymbolTagService._event_tags_for_symbol(_Cur(), "CNStock", "600519.SH")
    assert "qd_market_events" in captured["sql"]
    assert "jsonb_array_elements_text" in captured["sql"]
    assert "available_at <= NOW()" in captured["sql"]


# ---------------------------------------------------------------------------
# Snapshot caching / refresh
# ---------------------------------------------------------------------------


def test_quote_reads_prefer_the_stored_snapshot(monkeypatch):
    """Screening must never block ~70s on a live full-market snapshot."""
    stored = [{"market": "CNStock", "symbol": "600000.SH", "name": "X",
               "change_pct": 1.0, "amount": 1.0, "volume": 1.0, "price": 1.0}]
    monkeypatch.setattr(
        SymbolTagService, "_stored_quote_rows", staticmethod(lambda day: stored)
    )
    monkeypatch.setattr(
        SymbolTagService,
        "_fetch_quote_rows",
        staticmethod(lambda: (_ for _ in ()).throw(AssertionError("must not fetch"))),
    )
    assert get_symbol_tag_service().quote_rows(dt.date(2026, 9, 18)) == stored


def test_quote_cache_serves_repeated_calls_within_the_tick(monkeypatch):
    calls = {"n": 0}

    def _fetch():
        calls["n"] += 1
        return [{"market": "CNStock", "symbol": "600000.SH", "name": "X",
                 "change_pct": 1.0, "amount": 1.0, "volume": 1.0, "price": 1.0}]

    monkeypatch.setattr(SymbolTagService, "_fetch_quote_rows", staticmethod(_fetch))
    monkeypatch.setattr(SymbolTagService, "_stored_quote_rows", staticmethod(lambda day: []))
    monkeypatch.setattr(module, "_quote_cache", {"day": None, "at": 0.0, "rows": []})
    service = get_symbol_tag_service()
    day = dt.date(2026, 9, 18)
    first = service.quote_rows(day)
    second = service.quote_rows(day)
    assert first == second
    assert calls["n"] == 1


def test_refresh_quote_persists_the_snapshot_it_evaluated(monkeypatch):
    stored: list = []
    row = {"market": "CNStock", "symbol": "600000.SH", "name": "X",
           "change_pct": 9.9, "amount": 2e9, "volume": 1.0, "price": 10.0}
    monkeypatch.setattr(
        SymbolTagService, "_store_quote_rows",
        staticmethod(lambda day, rows: stored.append((day, len(rows)))),
    )
    monkeypatch.setattr(
        SymbolTagService, "quote_rows", classmethod(lambda cls, day, fresh=False: [row])
    )
    monkeypatch.setattr(
        SymbolTagService, "list_tags",
        lambda self, **kwargs: (
            [{"id": 1, "code": "quote_change_up", "status": "active",
              "rule": {"metric": "change_pct", "op": "gte", "value": 9.8}}]
            if kwargs.get("category") == "quote" else []
        ),
    )
    monkeypatch.setattr(
        SymbolTagService, "_store_quote_members",
        staticmethod(lambda day, members: len(members)),
    )
    report = get_symbol_tag_service()._refresh_quote()
    assert report["status"] == "ok"
    assert stored == [(dt.date.today(), 1)]
    assert report["by_tag"] == {"quote_change_up": 1}


def test_refresh_quote_reports_a_failing_upstream_instead_of_raising(monkeypatch):
    monkeypatch.setattr(
        SymbolTagService, "quote_rows",
        classmethod(lambda cls, day, fresh=False: (_ for _ in ()).throw(RuntimeError("down"))),
    )
    report = get_symbol_tag_service()._refresh_quote()
    assert report["status"] == "failed"
    assert "down" in report["error"]


def test_delisted_symbol_does_not_blank_the_snapshot(monkeypatch):
    """One invalid thscode upstream must not kill the full-market refresh."""
    from app.data_sources.hithink_finance import HiThinkParamError

    calls = []

    def _snapshot(batch):
        calls.append(list(batch))
        if "000004.SZ" in batch:
            raise HiThinkParamError("Unknown A-share thscode: 000004.SZ")
        return [{"thscode": code, "last_price": 1.0} for code in batch]

    monkeypatch.setattr("app.data_sources.hithink_finance.snapshot", _snapshot)
    monkeypatch.setattr(module, "_REJECTED_SYMBOLS", set())
    rows = module._snapshot_tolerant(["000004.SZ", "600519.SH"])
    assert [item["thscode"] for item in rows] == ["600519.SH"]
    assert "000004.SZ" in module._REJECTED_SYMBOLS
    assert len(calls) == 2

    # A rejected code is remembered and never retried.
    module._snapshot_tolerant(["000004.SZ", "000001.SZ"])
    assert calls[-1] == ["000001.SZ"]


def test_unknown_symbol_error_is_not_swallowed(monkeypatch):
    """Only a delisted-code param error may be recovered from."""
    from app.data_sources.hithink_finance import HiThinkParamError

    def _snapshot(batch):
        raise HiThinkParamError("thscodes is required")

    monkeypatch.setattr("app.data_sources.hithink_finance.snapshot", _snapshot)
    monkeypatch.setattr(module, "_REJECTED_SYMBOLS", set())
    with pytest.raises(HiThinkParamError):
        module._snapshot_tolerant(["600519.SH"])


# ---------------------------------------------------------------------------
# Catalogue contract
# ---------------------------------------------------------------------------


def test_conditions_floor_uses_the_oldest_snapshot(monkeypatch):
    monkeypatch.setattr(
        SymbolTagService, "get_tag",
        lambda self, code: {
            "event_limit_up": {"code": code, "point_in_time": True, "as_of_date": ""},
            "cn_industry_a": {"code": code, "point_in_time": False, "as_of_date": "2026-09-01"},
            "cn_industry_b": {"code": code, "point_in_time": False, "as_of_date": "2026-09-10"},
        }[code],
    )
    assert tag_conditions_floor([{"tag_code": "event_limit_up"}]) == ""
    assert tag_conditions_floor(
        [{"tag_code": "cn_industry_a"}, {"tag_code": "cn_industry_b"}]
    ) == "2026-09-10"


def test_unknown_tag_is_a_404():
    with pytest.raises(SymbolTagError) as excinfo:
        get_symbol_tag_service().get_tag("definitely-not-a-tag")
    assert excinfo.value.status_code == 404


def test_invalid_category_is_rejected():
    with pytest.raises(SymbolTagError):
        get_symbol_tag_service().list_tags(category="not-a-category")


def test_refresh_rejects_non_materialized_categories():
    with pytest.raises(SymbolTagError):
        get_symbol_tag_service().refresh(categories=["event"])


# ---------------------------------------------------------------------------
# Refresh scheduling
# ---------------------------------------------------------------------------


def test_schedule_tick_survives_a_stale_upstream_calendar(monkeypatch):
    """HiThink's calendar lags today, so the tick must not go permanently idle.

    Regression: with ``is_trading_day(today) == False`` the quote refresh never
    ran, because the upstream rolling calendar ends on the previous session.
    """
    from app.services import symbol_tag_schedule as schedule

    tz = dt.timezone(dt.timedelta(hours=8))
    friday = dt.date(2026, 9, 18)
    monday_before = dt.date(2026, 9, 14)
    monkeypatch.setattr(
        "app.data_sources.hithink_finance.is_trading_day", lambda day: day == monday_before
    )
    monkeypatch.setattr(
        "app.data_sources.hithink_finance.trading_days", lambda: [monday_before]
    )
    monkeypatch.setattr(
        "app.data_sources.hithink_finance.shanghai_now",
        lambda: dt.datetime(2026, 9, 18, 14, 0, tzinfo=tz),
    )

    # A weekday past the calendar's last known entry is provisionally tradeable.
    assert schedule._is_working_day(friday) is True
    assert "quote" in schedule.due_categories()
    # A weekend is still a weekend.
    assert schedule._is_working_day(dt.date(2026, 9, 19)) is False
    assert schedule.due_categories(dt.datetime(2026, 9, 19, 14, 0, tzinfo=tz)) == []

    monkeypatch.setattr(
        "app.data_sources.hithink_finance.trading_days",
        lambda: (_ for _ in ()).throw(RuntimeError("calendar down")),
    )
    # A calendar outage must not stop the tick either.
    assert schedule._is_working_day(friday) is True


def test_sector_refresh_runs_once_per_day_after_the_close(monkeypatch):
    from app.services import symbol_tag_schedule as schedule

    tz = dt.timezone(dt.timedelta(hours=8))
    monkeypatch.setattr("app.data_sources.hithink_finance.is_trading_day", lambda day: True)
    before = dt.datetime(2026, 9, 18, 15, 0, tzinfo=tz)
    after = dt.datetime(2026, 9, 18, 15, 45, tzinfo=tz)

    assert schedule.due_categories(before, last_run=dt.date(2026, 9, 17)) == ["quote"]
    assert schedule.due_categories(after, last_run=dt.date(2026, 9, 17)) == [
        "industry", "concept", "quote",
    ]
    # Already synced today -> no second constituent crawl.
    assert schedule.due_categories(after, last_run=dt.date(2026, 9, 18)) == ["quote"]


def test_smart_pool_metadata_shape_matches_the_service():
    """Conditions are the smart universe's whole state; keep the shape stable."""
    metadata = {
        "source": "symbol_tags",
        "conditions": json.loads(json.dumps([{"tag_code": "event_limit_up"}])),
        "point_in_time": True,
        "snapshot_only": False,
        "snapshot_as_of": "",
    }
    assert metadata["conditions"][0]["tag_code"] == "event_limit_up"
    assert metadata["source"] == "symbol_tags"
