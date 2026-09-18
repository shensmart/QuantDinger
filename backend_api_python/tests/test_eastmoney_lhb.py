"""Eastmoney dragon-tiger client: field mapping and best-effort degradation."""
import datetime as dt

import pandas as pd

from app.data_sources import eastmoney_lhb as lhb


class _FakeAk:
    """Minimal AkShare stand-in returning the upstream Chinese column names."""

    def __init__(self):
        self.calls = []

    def stock_lhb_detail_em(self, start_date, end_date):
        self.calls.append(("detail", start_date, end_date))
        return pd.DataFrame([
            {
                "序号": 1, "代码": "000978", "名称": "桂林旅游", "上榜日": "2026-09-17",
                "解读": "主力做T，成功率32.89%", "收盘价": 6.5, "涨跌幅": -9.32,
                "龙虎榜净买额": -63952179.0, "龙虎榜买入额": 101169700.0,
                "龙虎榜卖出额": 165121879.0, "龙虎榜成交额": 266291579.0,
                "市场总成交额": 619000000.0, "净买额占总成交比": -10.33,
                "成交额占总成交比": 43.02, "换手率": 35.24, "流通市值": 2.4e9,
                "上榜原因": "日跌幅偏离值达到7%的前5只证券", "上榜后1日": None,
            },
            {
                "序号": 2, "代码": "600519", "名称": "贵州茅台", "上榜日": "2026-09-17",
                "解读": "", "收盘价": 1266.98, "涨跌幅": 0.71,
                "龙虎榜净买额": 1e8, "龙虎榜买入额": 2e8, "龙虎榜卖出额": 1e8,
                "龙虎榜成交额": 3e8, "市场总成交额": 2e9, "净买额占总成交比": 5.0,
                "成交额占总成交比": 15.0, "换手率": 0.5, "流通市值": 1.5e12,
                "上榜原因": "日振幅值达到15%的前5只证券", "上榜后1日": None,
            },
        ])

    def stock_lhb_jgmmtj_em(self, start_date, end_date):
        self.calls.append(("jgmmtj", start_date, end_date))
        return pd.DataFrame([
            {"序号": 1, "代码": "300243", "名称": "瑞丰高材", "收盘价": 21.22, "涨跌幅": 6.9018,
             "买方机构数": 4, "卖方机构数": 2, "机构买入总额": 314012649.84,
             "机构卖出总额": 102475113.0, "机构买入净额": 211537557.65,
             "市场总成交额": 1.7e9, "机构净买额占总成交额比": 12.99, "换手率": 22.1,
             "流通市值": 3.1e9, "上榜原因": "日换手率达到20%的前5只证券", "上榜日期": "2026-09-17"},
            # same stock + same day, different reason -> must be de-duplicated
            {"序号": 2, "代码": "300243", "名称": "瑞丰高材", "收盘价": 21.22, "涨跌幅": 6.9018,
             "买方机构数": 4, "卖方机构数": 2, "机构买入总额": 314012649.84,
             "机构卖出总额": 102475113.0, "机构买入净额": 211537557.65,
             "市场总成交额": 1.7e9, "机构净买额占总成交额比": 12.99, "换手率": 22.1,
             "流通市值": 3.1e9, "上榜原因": "日涨幅偏离值达到7%的前5只证券", "上榜日期": "2026-09-17"},
        ])

    def stock_lhb_hyyyb_em(self, start_date, end_date):
        self.calls.append(("hyyyb", start_date, end_date))
        return pd.DataFrame([
            {"序号": 1, "营业部名称": "中国国际金融股份有限公司上海分公司", "上榜日": "2026-09-17",
             "买入个股数": 7, "卖出个股数": 8, "买入总金额": 691469370.77,
             "卖出总金额": 237666702.62, "总买卖净额": 453802668.15,
             "买入股票": "华脉科技 海马汽车 博汇科技", "营业部代码": "10088113"},
        ])

    def stock_lhb_stock_statistic_em(self, symbol):
        self.calls.append(("stock_stat", symbol))
        return pd.DataFrame([
            {"序号": 1, "代码": "600127", "名称": "金健米业", "最近上榜日": "2026-09-17",
             "收盘价": 13.02, "涨跌幅": 9.9662, "上榜次数": 22, "龙虎榜净买额": 338760606.19,
             "龙虎榜买入额": 6019276466.79, "龙虎榜卖出额": 5680515860.0,
             "龙虎榜总成交额": 11699792326.0, "买方机构次数": 0, "卖方机构次数": 0,
             "机构买入净额": 0.0, "机构买入总额": 0.0, "机构卖出总额": 0.0,
             "近1个月涨跌幅": 121.8, "近3个月涨跌幅": 127.2, "近6个月涨跌幅": 73.4,
             "近1年涨跌幅": 86.8},
        ])

    def stock_lhb_jgstatistic_em(self, symbol):
        self.calls.append(("jgstat", symbol))
        return pd.DataFrame([
            {"序号": 1, "代码": "300189", "名称": "神农种业", "收盘价": 6.68, "涨跌幅": 8.6179,
             "龙虎榜成交金额": 8055886422.77, "上榜次数": 17, "机构买入额": 1312992586.06,
             "机构买入次数": 53, "机构卖出额": 1443501000.0, "机构卖出次数": 53,
             "机构净买额": -130508700.0, "近1个月涨跌幅": 43.04, "近3个月涨跌幅": 36.05,
             "近6个月涨跌幅": -24.18, "近1年涨跌幅": 40.34},
        ])

    def stock_lhb_traderstatistic_em(self, symbol):
        self.calls.append(("trader_stat", symbol))
        return pd.DataFrame([
            {"序号": 1, "营业部名称": "深股通专用", "龙虎榜成交金额": 93133179188.38,
             "上榜次数": 399, "买入额": 50550811906.87, "买入次数": 396,
             "卖出额": 42582367281.51, "卖出次数": 398},
        ])


def _install(monkeypatch):
    fake = _FakeAk()
    monkeypatch.setattr(lhb, "_ak", lambda: fake)
    return fake


def test_daily_detail_maps_every_column_and_normalizes_symbols(monkeypatch):
    fake = _install(monkeypatch)
    rows = lhb.daily_detail(dt.date(2026, 9, 17))
    assert fake.calls[0] == ("detail", "20260917", "20260917")
    assert len(rows) == 2

    first = rows[0]
    assert first["symbol"] == "000978.SZ"
    assert first["code"] == "000978"
    assert first["name"] == "桂林旅游"
    assert first["net_buy"] == -63952179.0
    assert first["reason"] == "日跌幅偏离值达到7%的前5只证券"
    assert first["turnover_ratio"] == 35.24
    assert first["source"] == lhb.SOURCE


def test_shanghai_codes_map_to_sh(monkeypatch):
    _install(monkeypatch)
    rows = lhb.daily_detail(dt.date(2026, 9, 17))
    assert rows[1]["symbol"] == "600519.SH"


def test_institutional_seats_deduplicate_repeated_listing_reasons(monkeypatch):
    _install(monkeypatch)
    rows = lhb.institutional_seats(dt.date(2026, 9, 17))
    assert len(rows) == 1, "same stock/day must not be counted twice"
    assert rows[0]["buy_seats"] == 4
    assert rows[0]["sell_seats"] == 2
    assert rows[0]["org_net"] == 211537557.65


def test_active_branches_split_the_stock_list(monkeypatch):
    _install(monkeypatch)
    rows = lhb.active_branches(dt.date(2026, 9, 17))
    assert rows[0]["branch"].startswith("中国国际金融")
    assert rows[0]["stocks"] == ["华脉科技", "海马汽车", "博汇科技"]
    assert rows[0]["net_amount"] == 453802668.15


def test_rolling_statistics_pass_the_window_through(monkeypatch):
    fake = _install(monkeypatch)
    rows = lhb.stock_statistics("近三月")
    assert fake.calls[-1] == ("stock_stat", "近三月")
    assert rows[0]["list_count"] == 22
    assert rows[0]["window"] == "近三月"
    assert rows[0]["net_buy"] == 338760606.19


def test_every_statistics_window_is_supported(monkeypatch):
    fake = _install(monkeypatch)
    for window in lhb.STATISTIC_WINDOWS:
        assert lhb.stock_statistics(window)
        assert lhb.institution_statistics(window)
        assert lhb.branch_statistics(window)
    assert len(fake.calls) == 12


def test_unknown_window_is_rejected(monkeypatch):
    _install(monkeypatch)
    try:
        lhb.stock_statistics("近五年")
    except ValueError as exc:
        assert "unsupported window" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_provider_failure_degrades_to_an_empty_list(monkeypatch):
    class _Broken:
        def stock_lhb_detail_em(self, **_kwargs):
            raise RuntimeError("eastmoney unreachable")

    monkeypatch.setattr(lhb, "_ak", lambda: _Broken())
    assert lhb.daily_detail(dt.date(2026, 9, 17)) == []


def test_empty_frame_degrades_to_an_empty_list(monkeypatch):
    class _Empty:
        def stock_lhb_detail_em(self, **_kwargs):
            return pd.DataFrame()

    monkeypatch.setattr(lhb, "_ak", lambda: _Empty())
    assert lhb.daily_detail(dt.date(2026, 9, 17)) == []


def test_date_accepts_multiple_formats(monkeypatch):
    fake = _install(monkeypatch)
    for value in (dt.date(2026, 9, 17), "2026-09-17", "2026/09/17", "20260917"):
        lhb.daily_detail(value)
    assert [call[1] for call in fake.calls] == ["20260917"] * 4
