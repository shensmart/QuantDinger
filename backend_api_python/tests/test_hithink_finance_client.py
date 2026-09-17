"""HiThink (同花顺) client contract: envelope codes, retries, symbol mapping."""
import datetime as dt

import pytest
import requests

from app.data_sources import hithink_finance as hithink


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("HITHINK_FINANCE_API_KEY", "test-key")
    monkeypatch.setenv("HITHINK_FINANCE_ENABLED", "true")
    monkeypatch.setattr(hithink, "_cache", None)
    monkeypatch.setattr(hithink, "_limiter", None)


class _FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.text = "{}"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_thscode_round_trip_for_every_exchange():
    assert hithink.to_thscode("SH600519") == "600519.SH"
    assert hithink.to_thscode("600519") == "600519.SH"
    assert hithink.to_thscode("000001.SZ") == "000001.SZ"
    assert hithink.to_thscode("830799.BJ") == "830799.BJ"
    assert hithink.to_thscode("430047") == "430047.BJ"
    assert hithink.to_thscode("") == ""
    assert hithink.to_project_symbol("600519.SH") == "600519.SH"
    assert hithink.to_tencent_code("600519.SH") == "sh600519"
    assert hithink.to_tencent_code("000001.SZ") == "sz000001"


def test_envelope_codes_map_to_typed_errors(monkeypatch):
    cases = {
        2001: hithink.HiThinkUnauthorized,
        2003: hithink.HiThinkForbidden,
        4001: hithink.HiThinkRateLimited,
        1002: hithink.HiThinkParamError,
        5002: hithink.HiThinkUnavailable,
        9999: hithink.HiThinkError,
    }
    for code, expected in cases.items():
        monkeypatch.setattr(
            hithink.requests,
            "get",
            lambda *a, _code=code, **k: _FakeResponse({"code": _code, "message": "boom"}),
        )
        with pytest.raises(expected):
            hithink._request("/api/a-share/prices/snapshot", {"thscodes": "600519.SH"})
    assert hithink.HiThinkUnauthorized.degradable is True
    assert hithink.HiThinkParamError.degradable is False


def test_rate_limited_retries_then_succeeds(monkeypatch):
    monkeypatch.setenv("HITHINK_FINANCE_MAX_RETRY", "3")
    monkeypatch.setattr(hithink.time, "sleep", lambda *_a, **_k: None)
    calls = {"count": 0}

    def fake_get(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return _FakeResponse(None, status=429)
        return _FakeResponse({"code": 0, "data": {"item": [{"thscode": "600519.SH"}]}})

    monkeypatch.setattr(hithink.requests, "get", fake_get)
    data = hithink._request("/api/a-share/prices/snapshot", {"thscodes": "600519.SH"})
    assert calls["count"] == 2
    assert data["item"][0]["thscode"] == "600519.SH"


def test_rate_limited_gives_up_and_stays_degradable(monkeypatch):
    monkeypatch.setenv("HITHINK_FINANCE_MAX_RETRY", "2")
    monkeypatch.setattr(hithink.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(
        hithink.requests, "get", lambda *a, **k: _FakeResponse(None, status=429)
    )
    with pytest.raises(hithink.HiThinkRateLimited):
        hithink._request("/api/a-share/prices/snapshot", {"thscodes": "600519.SH"})


def test_disabled_switch_blocks_calls(monkeypatch):
    monkeypatch.setenv("HITHINK_FINANCE_ENABLED", "false")
    assert hithink.configured() is False
    with pytest.raises(hithink.HiThinkNotConfigured):
        hithink._request("/api/a-share/prices/snapshot", {})


def test_snapshot_normalizes_to_ticker_contract(monkeypatch):
    monkeypatch.setattr(
        hithink.requests,
        "get",
        lambda *a, **k: _FakeResponse(
            {
                "code": 0,
                "data": {
                    "item": [
                        {
                            "thscode": "600519.SH",
                            "last_price": 1266.98,
                            "price_change": 8.98,
                            "price_change_ratio_pct": 0.7138,
                            "open_price": 1257.98,
                            "high_price": 1267.6,
                            "low_price": 1254,
                            "prev_price": 1258,
                            "volume": 1755380,
                            "turnover": 2217338300,
                        }
                    ]
                },
            }
        ),
    )
    ticker = hithink.snapshot_to_ticker(hithink.snapshot(["600519"])[0])
    assert ticker["last"] == pytest.approx(1266.98)
    assert ticker["changePercent"] == pytest.approx(0.71)
    assert ticker["previousClose"] == pytest.approx(1258)
    assert ticker["symbol"] == "600519.SH"
    assert ticker["source"] == "hithink_finance"


def test_daily_bars_slice_windows_over_ten_years(monkeypatch):
    seen = []

    def fake_request(path, params=None, **_kwargs):
        seen.append((params["start"], params["end"]))
        return {"item": []}

    monkeypatch.setattr(hithink, "_request", fake_request)
    hithink.daily_bars("600519", dt.date(2000, 1, 1), dt.date(2026, 1, 1))
    assert len(seen) >= 3
    for start, end in seen:
        assert (end - start) <= 3601 * 86_400_000


def test_trading_day_falls_back_to_weekday_when_calendar_fails(monkeypatch):
    def boom(*_a, **_k):
        raise hithink.HiThinkUnavailable("offline")

    monkeypatch.setattr(hithink, "trading_days", boom)
    assert hithink.is_trading_day(dt.date(2026, 9, 17)) is True  # Thursday
    assert hithink.is_trading_day(dt.date(2026, 9, 19)) is False  # Saturday
