"""Free-tier multi-source failover: EPS / industry / fund-flow / soft missing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import requests


def test_get_profit_forecast_prefers_eastmoney_predict(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(
        a_stock,
        "_eastmoney_datacenter",
        lambda *a, **k: [
            {
                "SECURITY_CODE": "002050",
                "SECURITY_NAME_ABBR": "三花智控",
                "RATING_ORG_NUM": 18,
                "YEAR1": 2025,
                "EPS1": 0.9655,
                "YEAR2": 2026,
                "EPS2": 1.1315,
                "YEAR3": 2027,
                "EPS3": 1.324,
            }
        ],
    )
    monkeypatch.setattr(
        a_stock,
        "_ths_eps_forecast",
        MagicMock(side_effect=AssertionError("THS should not be needed")),
    )
    monkeypatch.setattr(a_stock, "_tencent_quote", lambda codes: {})

    text = a_stock.get_profit_forecast("002050", "2026-07-15")
    assert "No analyst coverage" not in text
    assert "Eastmoney" in text or "东财" in text
    assert "EPS=" in text
    assert "18" in text
    assert "2026" in text or "FY2026" in text


def test_get_profit_forecast_falls_back_to_ths_when_em_empty(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_eastmoney_datacenter", lambda *a, **k: [])
    monkeypatch.setattr(
        a_stock,
        "_ths_eps_forecast",
        lambda code: pd.DataFrame(
            {
                0: ["2026", "2027"],
                1: [5, 4],
                2: [1.0, 1.2],
                3: [1.1, 1.3],
                4: [1.2, 1.4],
            }
        ),
    )
    monkeypatch.setattr(a_stock, "_tencent_quote", lambda codes: {})

    text = a_stock.get_profit_forecast("002050", "2026-07-15")
    assert "No analyst coverage" not in text
    assert "同花顺" in text
    assert "EPS=" in text


def test_get_profit_forecast_empty_ok_when_no_coverage(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_eastmoney_datacenter", lambda *a, **k: [])
    monkeypatch.setattr(a_stock, "_ths_eps_forecast", lambda code: pd.DataFrame())

    text = a_stock.get_profit_forecast("999999", "2026-07-15")
    assert "No analyst coverage" in text
    assert text.startswith("Error") is False


def test_industry_comparison_tries_push2delay_before_concept(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_EM_MIN_INTERVAL", 0.0)
    a_stock._em_last_call[0] = 0.0
    hosts_seen: list[str] = []

    class _Bad:
        status_code = 502
        text = "<html>502</html>"

        def json(self):
            raise ValueError("no json")

    class _Good:
        status_code = 200
        text = '{"data":{"diff":[{"f14":"家用电器","f3":1.2,"f104":10,"f105":5,"f140":"三花"}]}}'

        def json(self):
            return {
                "data": {
                    "diff": [
                        {
                            "f14": "家用电器",
                            "f3": 1.2,
                            "f104": 10,
                            "f105": 5,
                            "f140": "三花",
                        }
                    ]
                }
            }

    def fake_em(url, params=None, headers=None, timeout=15, **kwargs):
        hosts_seen.append(url)
        if "push2.eastmoney.com" in url and "push2delay" not in url:
            return _Bad()
        if "push2delay.eastmoney.com" in url:
            return _Good()
        raise AssertionError(url)

    monkeypatch.setattr(a_stock, "_em_get", fake_em)
    text = a_stock.get_industry_comparison("002050", "2026-07-15")
    assert "家用电器" in text
    assert "全行业表现" in text
    assert any("push2delay" in u for u in hosts_seen)
    assert "行业对比查询失败" not in text


def test_fund_flow_history_falls_back_to_sina(monkeypatch):
    from tradingagents.dataflows import a_stock

    monkeypatch.setattr(a_stock, "_EM_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(a_stock, "_EM_RETRY_BASE_DELAY", 0.0)
    a_stock._em_last_call[0] = 0.0

    class _Rt:
        def json(self):
            return {
                "data": {
                    "klines": [
                        "2026-07-15 15:00,1000000,0,0,200000,800000",
                    ]
                }
            }

    def fake_em(url, params=None, headers=None, timeout=15, **kwargs):
        if "daykline" in url:
            raise requests.exceptions.SSLError("record layer failure")
        return _Rt()

    monkeypatch.setattr(a_stock, "_em_get", fake_em)
    monkeypatch.setattr(
        a_stock,
        "_sina_fund_flow_history",
        lambda code, days=20: [
            {
                "date": "2026-07-14",
                "main_net": -91458559.97,
                "net_amount": -49254791.28,
            },
            {
                "date": "2026-07-13",
                "main_net": -189117343.23,
                "net_amount": -166994459.52,
            },
        ],
    )

    text = a_stock.get_fund_flow("002050", "2026-07-15", include_history=True)
    assert "Realtime Minute Flow" in text
    assert "Historical Daily Fund Flow" in text
    assert "sina" in text.lower() or "新浪" in text
    assert "2026-07-14" in text
    assert "历史日度暂不可用" not in text


def test_has_hard_missing_data_ignores_soft_markers():
    from web.report_repair import has_hard_missing_data, has_missing_data

    soft = (
        "[数据缺失: 无分析师覆盖]\n"
        "[数据缺失：股权质押比例]\n"
        "[数据缺失: 行业横向对比HTTP 502]\n"
        "[数据缺失：未发现控股股东近6个月减持行为]\n"
        "[数据缺失]\n"
    )
    assert has_missing_data(soft) is True
    assert has_hard_missing_data(soft) is False
    assert has_hard_missing_data("[数据缺失: 资产负债表]") is True


def test_list_repairable_ignores_soft_only_sections():
    from web.report_repair import list_repairable_missing_sections

    state = {
        "fundamentals_report": "[数据缺失: 无分析师覆盖]\n[数据缺失：股权质押比例]",
        "hot_money_report": "[数据缺失: 行业横向对比HTTP 502]",
        "lockup_report": "[数据缺失：未发现近6个月减持]",
        "market_report": "[数据缺失: PE]",
    }
    assert list_repairable_missing_sections(state) == []

    state["fundamentals_report"] = "[数据缺失: 资产负债率]"
    assert list_repairable_missing_sections(state) == ["fundamentals_report"]
