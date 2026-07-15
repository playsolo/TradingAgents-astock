"""688401-style data gaps: concept/insider/fund-flow/EPS resilience."""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import requests


def test_ths_eps_forecast_returns_empty_on_spa_html(monkeypatch):
    from tradingagents.dataflows import a_stock

    class _Resp:
        text = "<!DOCTYPE HTML><html><title>盈利预测</title></html>"
        encoding = "gbk"

    monkeypatch.setattr(a_stock._requests, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(
        a_stock.pd,
        "read_html",
        MagicMock(side_effect=FileNotFoundError("<!DOCTYPE HTML>...")),
    )

    df = a_stock._ths_eps_forecast("688401")
    assert isinstance(df, pd.DataFrame)
    assert df.empty


def test_get_concept_blocks_falls_back_to_em_when_baidu_403(monkeypatch):
    from tradingagents.dataflows import a_stock

    class _Baidu:
        def json(self):
            return {"ResultCode": 403, "ResultMsg": "forbidden"}

    class _Em:
        def json(self):
            return {
                "ssbk": [
                    {
                        "BOARD_NAME": "半导体",
                        "BOARD_CODE": "1036",
                        "BOARD_RANK": 2,
                    },
                    {
                        "BOARD_NAME": "科创板",
                        "BOARD_CODE": "0590",
                        "BOARD_RANK": 3,
                    },
                ],
                "hxtc": [
                    {
                        "KEYWORD": "掩膜版",
                        "KEY_CLASSIF": "主营业务",
                        "MAINPOINT_CONTENT": "掩膜版龙头",
                    }
                ],
            }

    def fake_requests_get(url, *a, **k):
        if "baidu.com" in url:
            return _Baidu()
        raise AssertionError(url)

    def fake_em_get(url, params=None, headers=None, timeout=15, **kwargs):
        if "CoreConception" in url:
            return _Em()
        raise AssertionError(url)

    monkeypatch.setattr("requests.get", fake_requests_get)
    monkeypatch.setattr(a_stock, "_em_get", fake_em_get)

    text = a_stock.get_concept_blocks("688401")
    assert text.startswith("Error") is False
    assert "Baidu PAE error" not in text
    assert "半导体" in text
    assert "掩膜版" in text or "主营业务" in text
    assert "东财" in text or "eastmoney" in text.lower() or "EM" in text


def test_get_insider_handles_mootdx_dict_and_uses_em_fallback(monkeypatch):
    from tradingagents.dataflows import a_stock

    class _Client:
        def F10(self, symbol, name):
            return {"最新提示": "☆ irrelevant tab without holders"}

    class _Sess:
        def __enter__(self):
            return _Client()

        def __exit__(self, *a):
            return False

    class _EmShare:
        def json(self):
            return {
                "sdgd": [
                    {
                        "END_DATE": "2026-03-31",
                        "HOLDER_RANK": 1,
                        "HOLDER_NAME": "某人",
                        "HOLD_NUM": 1000,
                        "HOLD_NUM_RATIO": 10.5,
                    }
                ],
                "sdltgd": [],
                "jgcc": [
                    {
                        "REPORT_DATE": "2026-03-31",
                        "ORG_TYPE": "基金",
                        "TOTAL_ORG_NUM": 12,
                        "TOTAL_SHARES_RATIO": 3.2,
                    }
                ],
                "gdrs": [],
                "sjkzr": [{"HOLDER_NAME": "实控人甲", "HOLD_RATIO": 30.1}],
            }

    monkeypatch.setattr(a_stock, "_mootdx_client_session", lambda: _Sess())
    monkeypatch.setattr(
        a_stock,
        "_em_get",
        lambda *a, **k: _EmShare(),
    )

    text = a_stock.get_insider_transactions("688401")
    assert "Error retrieving" not in text
    assert "某人" in text or "实控人甲" in text
    assert "股东" in text or "HOLDER" in text or "持股" in text


def test_fund_flow_realtime_prints_mid_and_small(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_EM_MIN_INTERVAL", 0.0)
    a_stock._em_last_call[0] = 0.0

    class _Rt:
        def json(self):
            return {
                "data": {
                    "klines": [
                        # date, main, small, mid, large, super
                        "2026-07-14 15:00,1000000,-100000,200000,300000,500000",
                    ]
                }
            }

    def fake_em(url, params=None, headers=None, timeout=15, **kwargs):
        if "daykline" in url:
            raise requests.exceptions.SSLError("record layer failure")
        return _Rt()

    monkeypatch.setattr(a_stock, "_em_get", fake_em)
    text = a_stock.get_fund_flow("688401", "2026-07-14", include_history=True)
    assert "中单=" in text
    assert "小单=" in text
    assert "历史日度暂不可用" in text
    assert "[数据缺失" not in text


def test_insider_rejects_mootdx_latest_tips_when_em_empty(monkeypatch):
    from tradingagents.dataflows import a_stock

    class _Client:
        def F10(self, symbol, name):
            # Dict key looks like the requested tab, but body is 最新提示 junk
            return {"股东研究": "☆ 最新提示 tab with 股东人数:10995 only"}

    class _Sess:
        def __enter__(self):
            return _Client()

        def __exit__(self, *a):
            return False

    class _EmptyEm:
        def json(self):
            return {}

    monkeypatch.setattr(a_stock, "_mootdx_client_session", lambda: _Sess())
    monkeypatch.setattr(a_stock, "_em_get", lambda *a, **k: _EmptyEm())

    text = a_stock.get_insider_transactions("688401")
    assert "最新提示 tab" not in text
    assert "No insider/shareholder data" in text or text.startswith("Error")


def test_industry_comparison_falls_back_to_concept_boards(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_EM_MIN_INTERVAL", 0.0)
    a_stock._em_last_call[0] = 0.0

    class _BadClist:
        status_code = 502
        text = "<html>502</html>"

        def json(self):
            raise ValueError("no json")

    class _Concept:
        def json(self):
            return {
                "ssbk": [{"BOARD_NAME": "半导体", "BOARD_RANK": 1}],
                "hxtc": [],
            }

    def fake_em(url, params=None, headers=None, timeout=15, **kwargs):
        if "clist" in url:
            return _BadClist()
        if "CoreConception" in url:
            return _Concept()
        raise AssertionError(url)

    monkeypatch.setattr(a_stock, "_em_get", fake_em)
    text = a_stock.get_industry_comparison("688401", "2026-07-14")
    assert "行业对比查询失败" in text
    assert "半导体" in text
    assert "CoreConception" in text or "板块兜底" in text
