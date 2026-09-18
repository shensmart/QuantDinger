"""Industry/concept grouping and hierarchy for board views (涨跌榜).

``qd_symbol_tag_members`` stores flat ``(tag, symbol)`` pairs and the upstream
catalogue returns only ``thscode``/``name`` -- there is no parent column to read.
The THS industry catalogue is nevertheless genuinely two-level: ``种植业与林业``
holds 30 symbols and its members are exactly the union of ``种子生产`` /
``粮食种植`` / ``林业`` / ... The parent link is therefore *derived* from
member-set inclusion (a tag is the child of the smallest tag whose member set
strictly contains it), so no upstream field and no re-sync are needed.

Concepts stay flat: their member sets overlap arbitrarily, so the same rule
would invent meaningless parents.

Two queries per request, never per row:
1. the requested symbols' tags (bounded by the route's 500-symbol cap);
2. the industry catalogue, only to derive parents, cached in-process.

ponytail: the parent inference is an O(n^2) subset scan over the ~320 industry
tags, run once per cache window (~20ms). Store ``metadata_json.parent_code`` at
sync time instead if the tag count ever grows by an order of magnitude.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Iterable

from app.services.symbol_tags import SymbolTagError
from app.utils.db import get_db_connection
from app.utils.logger import get_logger

logger = get_logger(__name__)

MARKET = "CNStock"
DEFAULT_CATEGORIES = ("industry", "concept")
MAX_SYMBOLS = 500
# A self-referencing or over-nested upstream snapshot must not turn the tree walk
# into an unbounded recursion.
MAX_DEPTH = 3

_cache_lock = threading.Lock()
_catalogue_cache: dict[str, Any] = {"at": 0.0, "loaded": False, "parents": {}}


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(str(os.getenv(name) or "").strip() or default))
    except (TypeError, ValueError):
        return default


def _fetch(sql: str, params: Iterable[Any]) -> list[dict]:
    with get_db_connection() as db:
        cur = db.cursor()
        cur.execute(sql, tuple(params))
        rows = cur.fetchall() or []
        cur.close()
    return [dict(row) for row in rows]


def normalize_symbols(symbols: Any) -> list[str]:
    """Upper-case, de-duplicate, and bound a symbol list from a query string."""
    clean: list[str] = []
    seen: set[str] = set()
    for item in symbols or []:
        symbol = str(item or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        clean.append(symbol)
    if not clean:
        raise SymbolTagError("symbolTag.symbolRequired")
    if len(clean) > MAX_SYMBOLS:
        raise SymbolTagError("symbolTag.tooManySymbols")
    return clean


def normalize_categories(categories: Any) -> list[str]:
    wanted = [
        name for name in ("industry", "concept")
        if name in {str(item or "").strip().lower() for item in (categories or DEFAULT_CATEGORIES)}
    ]
    if not wanted:
        raise SymbolTagError("symbolTag.invalidCategory")
    return wanted


def infer_parents(member_sets: dict[str, set[str]]) -> dict[str, str]:
    """``code -> parent code`` by strict member-set inclusion.

    The parent is the *smallest* strictly larger set that contains the tag, which
    is what separates a second-level industry from its whole sector. Ties cannot
    happen: two different sets of equal size cannot both strictly contain one
    another.
    """
    codes = sorted(member_sets, key=lambda code: len(member_sets[code]))
    parents: dict[str, str] = {}
    for code in codes:
        own = member_sets[code]
        if not own:
            continue
        best: str | None = None
        best_size = 0
        for other in codes:
            if other == code:
                continue
            size = len(member_sets[other])
            if size <= len(own) or (best is not None and size >= best_size):
                continue
            if own <= member_sets[other]:
                best, best_size = other, size
        if best:
            parents[code] = best
    return parents


def industry_parents(*, fresh: bool = False) -> dict[str, str]:
    """Cached ``code -> parent code`` for every industry tag.

    An explicit ``metadata_json.parent_code`` written by the sync script wins;
    anything without one is derived from member-set inclusion. Today upstream
    ships no parent field, so every link takes the derived path -- the override
    is what makes a future upstream field a one-line sync change instead of a
    rewrite here.
    """
    ttl = _int_env("SYMBOL_TAG_TREE_CACHE_SEC", 900)
    now = time.monotonic()
    with _cache_lock:
        if _catalogue_cache["loaded"] and not fresh and (now - _catalogue_cache["at"]) < ttl:
            return dict(_catalogue_cache["parents"])
    rows = _fetch(
        """
        SELECT t.code, t.metadata_json ->> 'parent_code' AS parent_code, m.symbol
        FROM qd_symbol_tag_members m
        JOIN qd_symbol_tags t ON t.id = m.tag_id
        WHERE m.market = ? AND t.category = 'industry' AND t.status <> 'deprecated'
        """,
        (MARKET,),
    )
    member_sets: dict[str, set[str]] = {}
    declared: dict[str, str] = {}
    for row in rows:
        code = str(row["code"])
        member_sets.setdefault(code, set()).add(str(row["symbol"]))
        explicit = str(row.get("parent_code") or "").strip()
        if explicit and explicit != code:
            declared[code] = explicit
    parents = {code: parent for code, parent in declared.items() if parent in member_sets}
    derived = infer_parents(member_sets)
    for code, parent in derived.items():
        if code not in declared:
            parents[code] = parent
    with _cache_lock:
        _catalogue_cache.update({"at": now, "loaded": True, "parents": parents})
    logger.info(
        "Industry parents: %s declared upstream, %s derived from member overlap (%s tags)",
        len(declared), len(parents) - len(declared), len(member_sets),
    )
    return dict(parents)


def _display_name(raw: str) -> str:
    # The synced tag name carries its own prefix (``Industry: 白酒``); the UI
    # already labels the category, so strip it once here.
    for prefix in ("Industry:", "Concept:"):
        if raw.startswith(prefix):
            return raw[len(prefix):].strip()
    return raw


def _sort_key(node: dict) -> tuple:
    # Industries before concepts (the industry tree reads top-down), then biggest
    # group first; name as the tie-break for a stable order.
    return (0 if node["category"] == "industry" else 1, -int(node["count"]), node["name"], node["code"])


def build_groups(market: str, symbols: Any, categories: Any = DEFAULT_CATEGORIES) -> dict:
    """Group ``symbols`` by their industry/concept tags, with the industry tree.

    A symbol member of N tags appears under all N nodes (no "primary tag"
    attribution). Nodes carry the *directly* tagged symbols; ``count`` is the
    subtree total, so a parent whose own tag has no extra members still reports
    how many stocks it covers.
    """
    wanted = normalize_categories(categories)
    clean = normalize_symbols(symbols)
    market = str(market or MARKET).strip() or MARKET

    rows = _fetch(
        """
        SELECT m.symbol, t.code, t.name, t.category
        FROM qd_symbol_tag_members m
        JOIN qd_symbol_tags t ON t.id = m.tag_id
        WHERE m.market = ? AND m.symbol = ANY(?) AND t.status <> 'deprecated'
          AND t.category = ANY(?)
        ORDER BY t.category, t.sort_order, t.code, m.symbol
        """,
        (market, clean, wanted),
    )

    names: dict[tuple[str, str], str] = {}
    direct: dict[tuple[str, str], list[str]] = {}
    tagged: set[str] = set()
    for row in rows:
        key = (str(row["category"]), str(row["code"]))
        names.setdefault(key, str(row["name"] or row["code"]))
        direct.setdefault(key, []).append(str(row["symbol"]))
        tagged.add(str(row["symbol"]))

    nodes: dict[tuple[str, str], dict] = {}

    def ensure(key: tuple[str, str]) -> dict:
        node = nodes.get(key)
        if node is None:
            category, code = key
            node = {
                "key": f"{category}:{code}",
                "code": code,
                "name": _display_name(names.get(key, code)),
                "category": category,
                "count": 0,
                "children": [],
                "symbols": [],
            }
            nodes[key] = node
        return node

    for key, members in direct.items():
        ensure(key)["symbols"] = sorted(set(members))

    if "industry" in wanted:
        parents = industry_parents()
        for category, code in [key for key in nodes if key[0] == "industry"]:
            child = ensure((category, code))
            seen_chain = {code}
            cursor = parents.get(code)
            depth = 0
            while cursor and cursor not in seen_chain and depth < MAX_DEPTH:
                seen_chain.add(cursor)
                parent = ensure(("industry", cursor))
                if child not in parent["children"]:
                    parent["children"].append(child)
                child = parent
                cursor = parents.get(cursor)
                depth += 1

    computed: set[str] = set()

    def total(node: dict, depth: int = 0) -> set[str]:
        symbols = set(node["symbols"])
        if depth <= MAX_DEPTH and node["key"] not in computed:
            for child in node["children"]:
                symbols |= total(child, depth + 1)
        node["count"] = len(symbols)
        computed.add(node["key"])
        return symbols

    linked = {id(child) for node in nodes.values() for child in node["children"]}
    roots = [node for node in nodes.values() if id(node) not in linked]
    for node in roots:
        total(node)
    # Defence in depth: a hand-edited/self-referencing snapshot could leave a
    # node unreachable from any root, and its count must still be filled in.
    for node in nodes.values():
        if node["key"] not in computed:
            node["count"] = len(set(node["symbols"]))
            computed.add(node["key"])
    for node in nodes.values():
        node["children"].sort(key=_sort_key)
    roots.sort(key=_sort_key)

    budget = _int_env("SYMBOL_TAG_TREE_MAX_NODES", 500)
    kept: list[dict] = []
    used = 0
    for node in roots:
        size = 1 + _descendant_count(node)
        if kept and used + size > budget:
            break
        kept.append(node)
        used += size
    truncated = len(kept) < len(roots)

    has_tree = any(node["children"] for node in kept)
    return {
        "hierarchy": "industry_tree" if has_tree else "flat",
        "nodes": kept,
        "unmatched": [symbol for symbol in clean if symbol not in tagged],
        "truncated": truncated,
    }


def _descendant_count(node: dict) -> int:
    return sum(1 + _descendant_count(child) for child in node["children"])
