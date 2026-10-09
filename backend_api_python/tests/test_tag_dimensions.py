"""Catalogue hierarchy and cross-dimensional screens; no live upstream calls."""

import datetime as dt

import pytest

from scripts import sync_ths_sector_tags as sync
from app.services import symbol_tags as tags
from app.services import symbol_tag_schedule as schedule


def test_industry_catalogue_keeps_order_and_parents_before_filtering(monkeypatch):
    items = [
        {"thscode": "881101.TI", "name": "种植业与林业"},
        {"thscode": "884001.TI", "name": "种子生产"},
        {"thscode": "884002.TI", "name": "粮食种植"},
        {"thscode": "881102.TI", "name": "养殖业"},
        {"thscode": "884005.TI", "name": "海洋捕捞"},
    ]
    monkeypatch.setattr(sync, "_request", lambda *a: {"data": {"item": items}})
    catalogue = sync.fetch_catalog("industry")
    assert [r["thscode"] for r in catalogue] == [r["thscode"] for r in items]
    assert catalogue[0]["metadata"]["level"] == 1
    assert catalogue[0]["metadata"]["parent_code"] == ""
    assert catalogue[1]["metadata"]["level"] == 2
    assert catalogue[1]["metadata"]["parent_code"] == "cn_industry_881101_ti"
    assert catalogue[4]["metadata"]["parent_code"] == "cn_industry_881102_ti"
    only_child = sync._selected_sectors("industry", only=["884005.TI"], limit=1)
    assert only_child[0]["metadata"] == catalogue[4]["metadata"]


@pytest.mark.parametrize("items", [
    [],
    [{"thscode": "884001.TI", "name": "orphan"}],
    [{"thscode": "881101.TI", "name": "parent"}, {"thscode": "999001.TI", "name": "unknown"}],
    [{"thscode": "881101.TI", "name": "parent"}, {"thscode": "881101.TI", "name": "duplicate"}],
    [{"thscode": "881101.TI", "name": ""}],
])
def test_invalid_catalogue_fails_before_any_writes(monkeypatch, items):
    monkeypatch.setattr(sync, "_request", lambda *a: {"data": {"item": items}})
    with pytest.raises(sync.SectorSyncError):
        sync.fetch_catalog("industry")


def test_concept_code_ranges_are_flat_not_parent_and_child(monkeypatch):
    monkeypatch.setattr(sync, "_request", lambda *a: {"data": {"item": [
        {"thscode": "885413.TI", "name": "创投"},
        {"thscode": "886036.TI", "name": "时空大数据"},
    ]}})
    assert [r["metadata"] for r in sync.fetch_catalog("cn_concept")] == [
        {"level": 1, "parent_code": ""}, {"level": 1, "parent_code": ""},
    ]


def test_sync_passes_hierarchy_to_upsert(monkeypatch):
    monkeypatch.setattr(sync, "_request", lambda *a: {"data": {"item": [
        {"thscode": "881101.TI", "name": "种植业与林业"},
        {"thscode": "884001.TI", "name": "种子生产"},
    ]}})
    monkeypatch.setattr(sync, "fetch_constituents", lambda c: [{"symbol": "600000.SH"}])
    written = []
    monkeypatch.setattr(sync, "upsert_tag", lambda **kwargs: written.append(kwargs) or {})
    report = sync.sync(tags=["industry"], only=[], limit=None, as_of=dt.date(2026, 10, 9), dry_run=False)
    assert report["failed"] == 0 and report["synced"] == 2
    assert written[1]["hierarchy"]["parent_code"] == "cn_industry_881101_ti"


@pytest.mark.parametrize("data", [{}, {"item": None}, {"item": {}}, {"item": [{"thscode": "invalid"}]}])
def test_invalid_constituents_cannot_clear_members(monkeypatch, data):
    monkeypatch.setattr(sync, "_request", lambda *a: {"data": data})
    with pytest.raises(sync.SectorSyncError):
        sync.fetch_constituents("883401.TI")


def test_empty_feature_board_is_valid_but_empty_industry_is_not(monkeypatch):
    monkeypatch.setattr(sync, "_request", lambda *a: {"data": {"item": []}})
    assert sync.fetch_constituents("883401.TI") == []
    monkeypatch.setattr(sync, "fetch_catalog", lambda tag: [
        {"thscode": "883401.TI", "name": "empty", "metadata": {"level": 1}},
    ])
    written = []
    monkeypatch.setattr(sync, "upsert_tag", lambda **kw: written.append(kw) or {})
    report = sync.sync(tags=["tszs", "industry"], only=[], limit=None, as_of=dt.date(2026, 10, 9), dry_run=False)
    assert report["synced"] == 1 and report["failed"] == 1
    assert len(written) == 1 and written[0]["members"] == []


def test_refresh_and_schedule_include_all_sector_dimensions(monkeypatch):
    captured = {}
    monkeypatch.setattr(sync, "sync", lambda **kwargs: captured.update(kwargs) or {})
    monkeypatch.setenv("SYMBOL_TAG_SECTOR_LIMIT", "0")
    tags.SymbolTagService()._refresh_sectors(list(tags.SECTOR_CATEGORIES))
    assert captured["tags"] == ["industry", "cn_concept", "region", "tszs"]
    assert captured["limit"] is None
    monkeypatch.setattr("app.data_sources.hithink_finance.is_trading_day", lambda day: True)
    assert schedule.due_categories(dt.datetime(2026, 10, 9, 17), last_run=dt.date(2026, 10, 8)) == list(tags.SECTOR_CATEGORIES)
    assert set(tags.SECTOR_CATEGORIES) <= set(tags.MATERIALIZED_CATEGORIES)


def test_serialized_tags_expose_hierarchy_and_keep_flat_metadata():
    child = tags._serialize_tag({"code": "child", "metadata_json": '{"parent_code":"parent","level":2}'})
    assert child["parent_code"] == "parent" and child["level"] == 2
    assert child["metadata"] == {"parent_code": "parent", "level": 2}
    flat = tags._serialize_tag({"category": "concept", "metadata_json": {}})
    assert flat["parent_code"] == "" and flat["level"] == 1


def test_cross_dimension_or_and_members_and_matched_tags(monkeypatch):
    definitions = {
        "industry": "industry", "region_a": "region", "region_b": "region",
        "feature": "feature", "concept_885": "concept", "concept_886": "concept",
    }
    members = {
        "industry": {"600000.SH", "600001.SH", "600002.SH"},
        "region_a": {"600000.SH"}, "region_b": {"600001.SH", "600002.SH"},
        "feature": {"600001.SH", "600002.SH"},
        "concept_885": {"600000.SH"}, "concept_886": {"600001.SH"},
    }
    monkeypatch.setattr(tags.SymbolTagService, "_tag_row", lambda self, code: {"code": code, "category": definitions[code]})
    monkeypatch.setattr(tags.SymbolTagService, "_members_for_condition", lambda self, c, *a: [
        {"market": "CNStock", "symbol": symbol} for symbol in members[c["tag_code"]]
    ])
    result = tags.SymbolTagService().screen(
        [{"tag_code": code} for code in definitions], as_of="2026-10-09", with_quotes=False,
    )
    assert result["total"] == 1
    assert result["items"][0]["symbol"] == "600001.SH"
    assert set(result["items"][0]["tags"]) == {"industry", "region_b", "feature", "concept_886"}
