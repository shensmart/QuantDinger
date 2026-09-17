"""Event storage: idempotency, payload round-trip, point-in-time visibility."""
import datetime as dt
import json

import pandas as pd

from app.services import events_data


class _FakeCursor:
    def __init__(self, store):
        self.store = store
        self._result = []
        self._one = None
        self._sql = ""

    def execute(self, sql, params=()):
        self._sql = " ".join(str(sql).split()).lower()
        if self._sql.startswith("insert into qd_market_events"):
            key = (params[0], params[1], params[2], params[3], params[9])
            self.store[key] = params
            self._one = None
        elif self._sql.startswith("select symbol, event_type"):
            market, symbol, kinds, cutoff = params
            self._result = [
                {
                    "symbol": row[1],
                    "event_type": row[2],
                    "trade_date": row[3],
                    "available_at": row[4],
                    "rank": row[6],
                    "score": row[7],
                    "payload_json": json.loads(row[8]) if isinstance(row[8], str) else row[8],
                }
                for row in self.store.values()
                if row[0] == market
                and row[1] == symbol
                and row[2] in kinds
                and row[4] <= cutoff
            ]
        elif self._sql.startswith("select count(*) as total"):
            self._result = []
            self._one = {"total": len(self.store)}
        else:
            self._result = []
        return self

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._one

    def close(self):
        return None


class _FakeDb:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return _FakeCursor(self.store)

    def commit(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_available_at_is_post_close_shanghai(monkeypatch):
    moment = events_data.available_at(dt.date(2026, 9, 16))
    assert moment.astimezone(dt.timezone(dt.timedelta(hours=8))).hour == 15
    assert moment.tzinfo is not None


def test_upsert_is_idempotent_on_the_unique_key(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(events_data, "get_db_connection", lambda: _FakeDb(store))
    monkeypatch.setattr(events_data, "ensure_schema", lambda: None)
    rows = [
        {
            "market": "CNStock",
            "symbol": "600519.SH",
            "event_type": "limit_up",
            "trade_date": dt.date(2026, 9, 16),
            "available_at": events_data.available_at(dt.date(2026, 9, 16)),
            "name": "贵州茅台",
            "rank": 2,
            "score": 1.0e8,
            "payload": {"continue_day_cnt": 2},
        }
    ]
    assert events_data.upsert_events(rows) == 1
    assert events_data.upsert_events(rows) == 1
    assert len(store) == 1


def test_load_points_respects_the_point_in_time_cutoff(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(events_data, "get_db_connection", lambda: _FakeDb(store))
    monkeypatch.setattr(events_data, "ensure_schema", lambda: None)
    events_data.upsert_events(
        [
            {
                "market": "CNStock",
                "symbol": "600519.SH",
                "event_type": "limit_up",
                "trade_date": dt.date(2026, 9, 16),
                "available_at": events_data.available_at(dt.date(2026, 9, 16)),
                "name": "贵州茅台",
                "payload": {"continue_day_cnt": 2},
            }
        ]
    )
    before = events_data.load_points(
        "CNStock", "600519.SH", ["limit_up"], dt.datetime(2026, 9, 16, 3, 0, tzinfo=dt.timezone.utc)
    )
    after = events_data.load_points(
        "CNStock", "600519.SH", ["limit_up"], dt.datetime(2026, 9, 16, 8, 0, tzinfo=dt.timezone.utc)
    )
    assert before == []
    assert len(after) == 1


def test_payload_round_trip_keeps_upstream_fields(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(events_data, "get_db_connection", lambda: _FakeDb(store))
    monkeypatch.setattr(events_data, "ensure_schema", lambda: None)
    payload = {"continue_day_cnt": 6, "limit_up_reason": "海上风电+清洁能源", "seal_money": 43368956.0}
    events_data.upsert_events(
        [
            {
                "market": "CNStock",
                "symbol": "000993.SZ",
                "event_type": "limit_up",
                "trade_date": dt.date(2026, 9, 16),
                "available_at": events_data.available_at(dt.date(2026, 9, 16)),
                "payload": payload,
            }
        ]
    )
    stored = next(iter(store.values()))
    assert json.loads(stored[8]) == payload


def test_event_value_maps_boards_to_strategy_columns():
    limit_up = events_data.event_value(
        "limit_up", {"continue_day_cnt": 3, "seal_money": 5e7, "price_change_ratio_pct": 10.0}, None, None
    )
    assert limit_up["limit_up"] == 1.0
    assert limit_up["limit_up_days"] == 3.0
    assert limit_up["seal_money"] == 5e7

    dragon = events_data.event_value("dragon_tiger_all", {"net_value": 1.2e8, "net_rate": 0.11}, None, None)
    assert dragon["dragon_tiger"] == 1.0
    assert dragon["dragon_tiger_net"] == 1.2e8

    hot = events_data.event_value("hot_rank", {"rank": 1, "heat": "1941909", "rank_change": 7}, 1, None)
    assert hot["hot_rank"] == 1.0
    assert hot["hot_heat"] == 1941909.0
    assert hot["hot_rank_change"] == 7.0


def test_enrich_frame_zero_fills_before_publication(monkeypatch):
    monkeypatch.setattr(
        events_data,
        "load_points",
        lambda *a, **k: [
            {
                "symbol": "600519.SH",
                "event_type": "limit_up",
                "trade_date": dt.date(2026, 1, 3),
                "available_at": events_data.available_at(dt.date(2026, 1, 3)),
                "rank": 2,
                "score": 1.0e8,
                "payload_json": {"continue_day_cnt": 2, "seal_money": 1.0e8},
            }
        ],
    )
    frame = pd.DataFrame(
        {"open": [1.0] * 5, "high": [1.0] * 5, "low": [1.0] * 5, "close": [1.0] * 5, "volume": [1.0] * 5},
        index=pd.to_datetime(
            ["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05", "2026-01-06"]
        ),
    )
    enriched = events_data.enrich_frame(market="CNStock", symbol="600519.SH", frame=frame)
    assert enriched["limit_up"].tolist() == [0.0, 1.0, 1.0, 1.0, 1.0]
    assert enriched["limit_up_days"].tolist() == [0.0, 2.0, 2.0, 2.0, 2.0]


def test_enrich_frame_survives_a_storage_failure(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(events_data, "load_points", boom)
    frame = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]},
        index=pd.to_datetime(["2026-01-02"]),
    )
    enriched = events_data.enrich_frame(market="CNStock", symbol="600519.SH", frame=frame)
    assert list(enriched.columns) == ["open", "high", "low", "close", "volume"]
