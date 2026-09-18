"""A-share fundamental mapping from the HiThink API into canonical fields."""
import datetime as dt

from app.data_sources import hithink_finance as hithink
from app.services.market_data_collector import MarketDataCollector


def _stub_api(monkeypatch, *, period="quarterly"):
    monkeypatch.setattr(
        hithink,
        "valuations",
        lambda symbols: {
            "600519.SH": {"thscode": "600519.SH", "pe_ttm": 19.4, "pe_mrq": 17.8, "pb_mrq": 6.3, "ps_ttm": 9.1}
        },
    )
    income = [
        {
            "fiscal_year": 2026,
            "fiscal_period": "Q2",
            "period_end_ms": hithink.date_to_ms(dt.date(2026, 6, 30)),
            "report_date_ms": hithink.date_to_ms(dt.date(2026, 8, 15)),
            "currency": "CNY",
            "operating_income": 90_000.0,
            "net_profit": 45_000.0,
            "basic_eps": 35.9,
        },
        *[
            {
                "period_end_ms": hithink.date_to_ms(dt.date(2025, 6, 30)),
                "report_date_ms": hithink.date_to_ms(dt.date(2025, 8, 15)),
                "operating_income": 89_000.0,
                "net_profit": 44_000.0,
            }
            for _ in range(4)
        ],
    ]
    monkeypatch.setattr(hithink, "income_statements", lambda symbol, **kwargs: income)
    monkeypatch.setattr(
        hithink,
        "balance_sheets",
        lambda symbol, **kwargs: [
            {
                "period_end_ms": hithink.date_to_ms(dt.date(2026, 6, 30)),
                "assets_total": 300_000.0,
                "total_current_assets": 250_000.0,
                "total_debt": 60_000.0,
                "holder_equity_total": 240_000.0,
            }
        ],
    )
    monkeypatch.setattr(
        hithink,
        "cash_flow_statements",
        lambda symbol, **kwargs: [
            {
                "period_end_ms": hithink.date_to_ms(dt.date(2026, 6, 30)),
                "act_cash_flow_net": 80_000.0,
                "pay_fixed_assets_etc_cash": 10_000.0,
            }
        ],
    )
    monkeypatch.setattr(
        hithink,
        "financial_indicators",
        lambda symbol, report: {
            "index_weighted_avg_roe": 16.75,
            "operating_income_yoy_growth_ratio": 1.47,
            "assets_debt_ratio": 25.0,
            "current_ratio": 4.1,
            "sale_net_interest_ratio": 50.0,
        },
    )
    del period


def test_hithink_fundamentals_map_to_canonical_fields(monkeypatch):
    _stub_api(monkeypatch)
    payload = MarketDataCollector()._hithink_fundamental("600519")

    assert payload["pe_ratio"] == 19.4
    assert payload["pb_ratio"] == 6.3
    assert payload["revenue"] == 90_000.0
    assert payload["net_income"] == 45_000.0
    assert payload["shareholder_equity"] == 240_000.0
    assert payload["total_debt"] == 60_000.0
    assert payload["free_cash_flow"] == 70_000.0
    assert payload["return_on_equity"] == 16.75
    assert payload["revenue_growth"] == 1.47
    assert payload["debt_to_equity"] == 0.25
    assert payload["source"] == "hithink_finance"


def test_period_end_is_the_report_period_and_available_at_is_the_disclosure_date(monkeypatch):
    _stub_api(monkeypatch)
    payload = MarketDataCollector()._hithink_fundamental("600519")
    assert payload["period_end"] == dt.date(2026, 6, 30)
    assert payload["available_at"] == dt.date(2026, 8, 15)
    assert payload["available_at"] > payload["period_end"], "never backfill before disclosure"


def test_statements_payload_carries_the_same_period(monkeypatch):
    _stub_api(monkeypatch)
    payload = MarketDataCollector()._hithink_fundamental("600519")
    statements = payload["financial_statements"]
    assert statements["latest_quarter"]["period_end"] == dt.date(2026, 6, 30)
    assert statements["income_statement"]["total_revenue"] == 90_000.0
    assert statements["_meta"]["source"] == "hithink_finance"


def test_report_period_token_uses_quarter_numbering():
    assert hithink.report_period(dt.date(2026, 3, 31)) == "2026-1"
    assert hithink.report_period(dt.date(2026, 6, 30)) == "2026-2"
    assert hithink.report_period(dt.date(2026, 9, 30)) == "2026-3"
    assert hithink.report_period(dt.date(2026, 12, 31)) == "2026-4"


def test_hk_never_routes_through_hithink(monkeypatch):
    """HKStock has no HiThink coverage; the A-share tier must not fire."""
    called = {"hithink": False}

    def boom(_symbol):
        called["hithink"] = True
        raise AssertionError("HiThink must not be used for HKStock")

    monkeypatch.setattr(MarketDataCollector, "_hithink_fundamental", staticmethod(boom))
    monkeypatch.setattr(
        "app.data_sources.tencent.fetch_quote", lambda code: ["1", "腾讯控股", "00700", "300", "298", "299"]
    )
    monkeypatch.setattr("app.data_sources.cn_hk_fundamentals.fetch_twelvedata_fundamental", lambda *a, **k: {})
    monkeypatch.setattr("app.data_sources.cn_hk_fundamentals.fetch_hk_fundamental_akshare", lambda *a, **k: {})
    monkeypatch.setattr("app.data_sources.cn_hk_fundamentals.fetch_twelvedata_statements", lambda *a, **k: {})
    monkeypatch.setattr("app.data_sources.cn_hk_fundamentals.fetch_twelvedata_earnings", lambda *a, **k: {})
    monkeypatch.setattr("app.data_sources.cn_hk_fundamentals.fetch_hk_financial_indicators", lambda *a, **k: {})
    monkeypatch.setattr("app.data_sources.cn_hk_fundamentals.fetch_hk_financial_statements", lambda *a, **k: {})
    monkeypatch.setattr(
        MarketDataCollector, "_enrich_hk_fundamental_with_yfinance", lambda *a, **k: None
    )
    MarketDataCollector()._get_cn_hk_fundamental("HKStock", "00700")
    assert called["hithink"] is False


def test_env_switch_disables_the_hithink_fundamental_tier(monkeypatch):
    monkeypatch.setenv("CN_FUNDAMENTAL_PRIMARY_SOURCE", "akshare")
    called = {"hithink": False}

    def boom(_symbol):
        called["hithink"] = True
        return {}

    monkeypatch.setattr(MarketDataCollector, "_hithink_fundamental", staticmethod(boom))
    monkeypatch.setattr(
        "app.data_sources.tencent.fetch_quote", lambda code: ["1", "贵州茅台", "600519", "10", "9", "9.5"]
    )
    monkeypatch.setattr("app.data_sources.cn_hk_fundamentals.fetch_twelvedata_fundamental", lambda *a, **k: {})
    monkeypatch.setattr("app.data_sources.cn_hk_fundamentals.fetch_cn_fundamental_akshare", lambda *a, **k: {})
    monkeypatch.setattr("app.data_sources.cn_hk_fundamentals.fetch_twelvedata_statements", lambda *a, **k: {})
    monkeypatch.setattr("app.data_sources.cn_hk_fundamentals.fetch_twelvedata_earnings", lambda *a, **k: {})
    monkeypatch.setattr("app.data_sources.cn_hk_fundamentals.fetch_cn_financial_indicators", lambda *a, **k: {})
    monkeypatch.setattr("app.data_sources.cn_hk_fundamentals.fetch_cn_financial_statements", lambda *a, **k: {})
    monkeypatch.setattr(
        MarketDataCollector, "_enrich_cn_hk_fundamental_with_yfinance", lambda *a, **k: None
    )
    MarketDataCollector()._get_cn_hk_fundamental("CNStock", "600519")
    assert called["hithink"] is False


def test_roe_falls_back_to_ttm_net_income_over_positive_equity(monkeypatch):
    """HiThink often omits index_weighted_avg_roe; it must be derived, not left NULL."""
    _stub_api(monkeypatch)
    monkeypatch.setattr(hithink, "financial_indicators", lambda symbol, report: {})
    payload = MarketDataCollector()._hithink_fundamental("600519")
    # TTM = 45_000 (latest) + 44_000 * 3 (prior year quarters) over 240_000 equity.
    assert payload["return_on_equity"] == round((45_000.0 + 44_000.0 * 3) / 240_000.0 * 100, 4)


def test_roe_stays_null_for_negative_equity(monkeypatch):
    """A sign-flipped ROE on insolvent companies is worse than a missing value."""
    _stub_api(monkeypatch)
    monkeypatch.setattr(hithink, "financial_indicators", lambda symbol, report: {})
    monkeypatch.setattr(
        hithink,
        "balance_sheets",
        lambda symbol, **kwargs: [
            {
                "period_end_ms": hithink.date_to_ms(dt.date(2026, 6, 30)),
                "assets_total": 300_000.0,
                "total_debt": 340_000.0,
                "holder_equity_total": -40_000.0,
            }
        ],
    )
    payload = MarketDataCollector()._hithink_fundamental("600519")
    assert payload["shareholder_equity"] == -40_000.0
    assert "return_on_equity" not in payload
    assert "roe" not in payload


def test_roe_prefers_deducted_variant_when_weighted_is_absent(monkeypatch):
    _stub_api(monkeypatch)
    monkeypatch.setattr(
        hithink,
        "financial_indicators",
        lambda symbol, report: {
            "index_weighted_avg_roe": None,
            "index_deduct_weighted_avg_roe": 9.25,
            "calculate_operating_income_yoy_growth_ratio": 3.5,
        },
    )
    payload = MarketDataCollector()._hithink_fundamental("600519")
    assert payload["return_on_equity"] == 9.25
    assert payload["revenue_growth"] == 3.5


def test_bj_shares_take_valuation_and_shares_from_the_tencent_quote(monkeypatch):
    """BJ shares: HiThink has no valuation or share count; Tencent fills both."""
    from app.services.market_data_collector import _apply_tencent_quote_fundamentals

    parts = [""] * 47
    parts[1], parts[3], parts[39], parts[45], parts[46] = "新睿电子", "95.75", "60.02", "32.94", "7.65"
    result = {}
    assert _apply_tencent_quote_fundamentals(result, parts) is True
    assert result["pe_ratio"] == 60.02
    assert result["pb_ratio"] == 7.65
    assert result["market_cap"] == 3_294_000_000.0
    assert result["shares_outstanding"] == round(32.94 * 100_000_000 / 95.75, 0)


def test_tencent_quote_fallback_reports_incomplete_without_a_quote(monkeypatch):
    from app.services.market_data_collector import _apply_tencent_quote_fundamentals

    assert _apply_tencent_quote_fundamentals({}, None) is False
    assert _apply_tencent_quote_fundamentals({}, ["1", "短"]) is False


def test_valuations_bisects_an_unknown_thscode_out_of_the_batch(monkeypatch):
    """One delisted code must not blank valuation for the rest of its batch."""
    good = {
        "000001.SZ": {"thscode": "000001.SZ", "pe_ttm": 5.2},
        "000002.SZ": {"thscode": "000002.SZ", "pe_ttm": 8.1},
        "600519.SH": {"thscode": "600519.SH", "pe_ttm": 19.4},
    }

    def fake_request(path, params=None, *, ttl=None):
        requested = [code for code in str((params or {}).get("thscodes") or "").split(",") if code]
        if "000004.SZ" in requested:
            raise hithink.HiThinkError("code=3001 Target not found in A-share code table", code=3001, path=path)
        return {"item": [good[code] for code in requested if code in good]}

    monkeypatch.setattr(hithink, "_request", fake_request)
    result = hithink.valuations(["000001", "000002", "000004", "600519"])
    assert set(result) == {"000001.SZ", "000002.SZ", "600519.SH"}


def test_valuations_returns_empty_when_every_code_is_unknown(monkeypatch):
    def fake_request(path, params=None, *, ttl=None):
        raise hithink.HiThinkError("code=3001 Target not found", code=3001, path=path)

    monkeypatch.setattr(hithink, "_request", fake_request)
    assert hithink.valuations(["000004", "002808"]) == {}


def test_valuations_does_not_bisect_a_provider_outage(monkeypatch):
    """Rate limits and outages must surface, not be swallowed by the bisection."""
    calls = {"n": 0}

    def fake_request(path, params=None, *, ttl=None):
        calls["n"] += 1
        raise hithink.HiThinkUnavailable("HTTP 503", path=path)

    monkeypatch.setattr(hithink, "_request", fake_request)
    try:
        hithink.valuations(["000001", "000002", "600519"])
    except hithink.HiThinkUnavailable:
        pass
    else:
        raise AssertionError("provider outage must propagate")
    assert calls["n"] == 1, "an outage must not trigger per-symbol retry storms"
