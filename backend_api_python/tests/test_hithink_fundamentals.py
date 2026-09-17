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
