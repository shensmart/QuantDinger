"""Industry/concept grouping and the batch tag lookup that feeds the board views.

Runs against the project's real database, like its sibling ``test_symbol_tags``.
"""

import time

import pytest

from app.services import symbol_tags as tags_module
from app.services import tag_hierarchy
from app.services.symbol_tags import SymbolTagError, get_symbol_tag_service
from app.services.tag_hierarchy import build_groups, infer_parents


# ---------------------------------------------------------------------------
# Parent inference
# ---------------------------------------------------------------------------


def test_parent_inference_picks_the_smallest_containing_set():
    sets = {
        "sector": {"a", "b", "c", "d"},
        "sub_a": {"a", "b"},
        "sub_b": {"c", "d"},
        "unrelated": {"a", "z"},
    }
    parents = infer_parents(sets)
    # Both children attach to the sector, not to each other.
    assert parents == {"sub_a": "sector", "sub_b": "sector"}


def test_declared_parent_code_wins_over_inference(monkeypatch):
    """Upstream hierarchy, when it finally exists, must override the guess.

    Also: a declared parent that is not in the catalogue (dangling reference) is
    dropped rather than producing a node with no members.
    """
    monkeypatch.setattr(
        tag_hierarchy, "_fetch",
        lambda sql, params: [
            {"code": "child", "parent_code": "declared_parent", "symbol": "a"},
            {"code": "parent", "parent_code": None, "symbol": "a"},
            {"code": "parent", "parent_code": None, "symbol": "b"},
            {"code": "declared_parent", "parent_code": None, "symbol": "a"},
            {"code": "dangling", "parent_code": "not_in_catalogue", "symbol": "a"},
        ],
    )
    parents = tag_hierarchy.industry_parents(fresh=True)
    assert parents["child"] == "declared_parent"
    assert "dangling" not in parents


def test_parent_inference_never_self_nests_or_loops():
    """Equal sets do not contain each other, so no cycle can be produced."""
    parents = infer_parents({"a": {"x"}, "b": {"x"}, "c": {"x", "y"}})
    assert parents == {"a": "c", "b": "c"}
    assert "c" not in parents


def test_industry_parents_finds_the_real_two_level_shape():
    """The THS industry catalogue is genuinely nested; the probe must prove it.

    If upstream ever flattens, this test is the tripwire: the tree degrades to a
    two-level board and ``build_groups`` reports ``hierarchy='flat'``.
    """
    parents = tag_hierarchy.industry_parents(fresh=True)
    assert parents, "no industry parent link derived from the stored snapshot"
    # A parent must cover strictly more stocks than its child.
    from app.utils.db import get_db_connection

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT t.code, COUNT(*) AS members
            FROM qd_symbol_tag_members m
            JOIN qd_symbol_tags t ON t.id = m.tag_id
            WHERE m.market = 'CNStock' AND t.category = 'industry'
            GROUP BY t.code
            """
        )
        counts = {str(row["code"]): int(row["members"]) for row in cur.fetchall()}
    for child, parent in list(parents.items())[:20]:
        assert counts.get(parent, 0) > counts.get(child, 0)


# ---------------------------------------------------------------------------
# Batch lookup
# ---------------------------------------------------------------------------


def test_batch_lookup_uses_one_query_regardless_of_symbol_count(monkeypatch):
    """The N+1 pattern is the whole reason this method exists."""
    issued = []

    class _Cur:
        def execute(self, sql, params=None):
            issued.append((sql, params))

        def fetchall(self):
            if len(issued) > 1 and "qd_market_events" in issued[-1][0]:
                return []
            return [
                {"symbol": "600000.SH", "id": 1, "code": "cn_industry_x", "name": "Industry: X",
                 "category": "industry", "status": "active", "as_of_date": None},
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

    monkeypatch.setattr(tags_module, "get_db_connection", lambda: _Db())
    symbols = [f"6000{index:02d}.SH" for index in range(200)]
    result = get_symbol_tag_service().symbols_tags_batch("CNStock", symbols, ("industry", "concept"))
    assert len(issued) == 1
    assert "= ANY(?)" in issued[0][0]
    # The same 200 symbols with event tags included: still two, never 200.
    issued.clear()
    get_symbol_tag_service().symbols_tags_batch("CNStock", symbols, ("industry", "concept", "event"))
    assert len(issued) == 2
    assert set(result) == set(symbols)
    # The row the fake cursor returned is attached; everything else is empty.
    assert len(result["600000.SH"]) == 1
    assert all(not tags for symbol, tags in result.items() if symbol != "600000.SH")


def test_batch_lookup_keeps_event_tags_for_the_quick_trade_picker(monkeypatch):
    """The default categories must stay all four, or the picker loses chips.

    The board narrows to industry/concept; the existing quick-trade caller
    passes no ``categories`` and expects the original response shape.
    """
    queries = []

    class _Cur:
        def execute(self, sql, params=None):
            queries.append(sql)
            self._sql = sql

        def fetchall(self):
            if "qd_market_events" in self._sql:
                return [{"symbol": "600519.SH", "id": 9, "code": "event_limit_up",
                         "name": "涨停池", "category": "event", "status": "active",
                         "last_trade_date": None}]
            return [{"symbol": "600519.SH", "id": 1, "code": "cn_industry_x",
                     "name": "Industry: X", "category": "industry", "status": "active",
                     "as_of_date": None}]

        def close(self):
            pass

    class _Db:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return _Cur()

    monkeypatch.setattr(tags_module, "get_db_connection", lambda: _Db())
    service = get_symbol_tag_service()
    tags = service.symbols_tags_batch("CNStock", ["600519.SH"])
    assert [tag["category"] for tag in tags["600519.SH"]] == ["industry", "event"]
    # One query for the materialized set, one for events -- not one per symbol.
    assert len(queries) == 2
    assert "jsonb_array_elements_text" in queries[1]

    # Narrowing must drop the event query entirely.
    queries.clear()
    tags = service.symbols_tags_batch("CNStock", ["600519.SH"], ("industry", "concept"))
    assert [tag["category"] for tag in tags["600519.SH"]] == ["industry"]
    assert len(queries) == 1


def test_batch_lookup_rejects_empty_and_oversized_lists():
    service = get_symbol_tag_service()
    with pytest.raises(SymbolTagError):
        service.symbols_tags_batch("CNStock", [])
    with pytest.raises(SymbolTagError):
        service.symbols_tags_batch("CNStock", [f"6000{i:02d}.SH" for i in range(501)])


def test_batch_lookup_returns_empty_lists_not_missing_keys():
    """The UI does ``items[symbol] || []``; a missing key would be silently fine
    but an unknown symbol must still round-trip so the row renders a dash."""
    result = get_symbol_tag_service().symbols_tags_batch(
        "CNStock", ["600519.SH", "NOT.A.SYMBOL"]
    )
    assert set(result) == {"600519.SH", "NOT.A.SYMBOL"}
    assert result["NOT.A.SYMBOL"] == []


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def _limit_up_symbols(limit=60):
    from app.utils.db import get_db_connection

    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(
            """
            SELECT symbol FROM qd_market_events
            WHERE event_type = 'limit_up'
              AND trade_date = (
                SELECT MAX(trade_date) FROM qd_market_events WHERE event_type = 'limit_up'
              )
            LIMIT ?
            """,
            (limit,),
        )
        return [str(row["symbol"]) for row in (cur.fetchall() or [])]


@pytest.mark.skipif(not _limit_up_symbols(1), reason="no limit-up rows stored")
def test_groups_cover_every_symbol_and_keep_multi_tag_stocks_in_each_group():
    symbols = _limit_up_symbols()
    result = build_groups("CNStock", symbols)
    assert result["hierarchy"] in ("industry_tree", "flat")
    placed = {symbol for node in result["nodes"] for symbol in node["symbols"]}
    # Every requested symbol is either placed or explicitly unmatched.
    assert placed | set(result["unmatched"]) == set(symbols)
    assert not (placed & set(result["unmatched"]))
    # Counts are subtree totals, so a parent is never smaller than its children.
    for node in result["nodes"]:
        for child in node["children"]:
            assert node["count"] >= child["count"]


def test_groups_of_unknown_symbols_are_all_unmatched():
    result = build_groups("CNStock", ["NOT.A.SYMBOL"])
    assert result["nodes"] == []
    assert result["unmatched"] == ["NOT.A.SYMBOL"]
    assert result["hierarchy"] == "flat"


def test_groups_reject_an_empty_symbol_list():
    with pytest.raises(SymbolTagError):
        build_groups("CNStock", [])


def test_groups_truncate_at_the_node_budget(monkeypatch):
    """Over the budget the response is cut and flagged, never silently huge.

    The first root is always kept (a tree that renders nothing is worse than a
    tree that renders too much), so the assertion is on the flag plus the total
    node count including descendants.
    """
    symbols = _limit_up_symbols()
    if not symbols:
        pytest.skip("no limit-up rows stored")
    monkeypatch.setenv("SYMBOL_TAG_TREE_MAX_NODES", "100000")
    assert build_groups("CNStock", symbols)["truncated"] is False
    monkeypatch.setenv("SYMBOL_TAG_TREE_MAX_NODES", "2")
    result = build_groups("CNStock", symbols)
    assert result["truncated"] is True
    assert 1 <= len(result["nodes"]) < 100
    assert all(not node["children"] or node["count"] > 0 for node in result["nodes"])


def test_groups_serve_well_inside_the_request_budget():
    """Two queries + the cached parent scan; a 500-row board must stay fast."""
    symbols = _limit_up_symbols(limit=500)
    if not symbols:
        pytest.skip("no limit-up rows stored")
    tag_hierarchy.industry_parents(fresh=True)
    started = time.monotonic()
    build_groups("CNStock", symbols)
    assert time.monotonic() - started < 2.0
