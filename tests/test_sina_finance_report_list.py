"""Sina CompanyFinanceService 2022 API now returns report_list, not top-level fzb/lrb/llb."""

from __future__ import annotations

import pandas as pd


def _sample_fzb_payload():
    return {
        "result": {
            "status": {"code": 0},
            "data": {
                "report_count": "2",
                "report_date": [
                    {"date_value": "20260331", "date_description": "2026一季报"},
                    {"date_value": "20251231", "date_description": "2025年报"},
                ],
                "report_list": {
                    "20260331": {
                        "rType": "合并期末",
                        "publish_date": "20260414",
                        "data": [
                            {
                                "item_field": "TOTASSET",
                                "item_title": "资产总计",
                                "item_value": "74646139221.440000",
                            },
                            {
                                "item_field": "TOTLIAB",
                                "item_title": "负债合计",
                                "item_value": "39173361530.660000",
                            },
                        ],
                    },
                    "20251231": {
                        "rType": "合并期末",
                        "publish_date": "20260320",
                        "data": [
                            {
                                "item_field": "TOTASSET",
                                "item_title": "资产总计",
                                "item_value": "70000000000.000000",
                            },
                            {
                                "item_field": "TOTLIAB",
                                "item_title": "负债合计",
                                "item_value": "35000000000.000000",
                            },
                        ],
                    },
                },
            },
        }
    }


def test_get_financial_report_sina_parses_report_list(monkeypatch):
    from tradingagents.dataflows import a_stock

    class _Resp:
        def json(self):
            return _sample_fzb_payload()

    monkeypatch.setattr(a_stock._requests, "get", lambda *a, **k: _Resp())

    df = a_stock._get_financial_report_sina("002648", "资产负债表", "quarterly", "2026-07-14")

    assert not df.empty
    assert "报告日" in df.columns
    assert "资产总计" in df.columns
    assert "负债合计" in df.columns
    assert len(df) == 2
    q1 = df[df["报告日"] == pd.Timestamp("2026-03-31")].iloc[0]
    assert float(q1["资产总计"]) == 74646139221.44
    assert float(q1["负债合计"]) == 39173361530.66


def test_get_balance_sheet_includes_debt_ratio_hint(monkeypatch):
    from tradingagents.dataflows import a_stock

    class _Resp:
        def json(self):
            return _sample_fzb_payload()

    monkeypatch.setattr(a_stock._requests, "get", lambda *a, **k: _Resp())

    text = a_stock.get_balance_sheet("002648", "quarterly", "2026-07-14")

    assert "No balance sheet data" not in text
    assert "资产总计" in text
    assert "负债合计" in text
    assert "资产负债率" in text
    assert "52.48%" in text


def test_annual_freq_keeps_only_december_reports(monkeypatch):
    from tradingagents.dataflows import a_stock

    class _Resp:
        def json(self):
            return _sample_fzb_payload()

    monkeypatch.setattr(a_stock._requests, "get", lambda *a, **k: _Resp())

    df = a_stock._get_financial_report_sina("002648", "资产负债表", "annual", "2026-07-14")

    assert len(df) == 1
    assert pd.Timestamp(df.iloc[0]["报告日"]).month == 12


def test_old_flat_fzb_list_still_works(monkeypatch):
    """Keep legacy payload shape working if Sina temporarily regresses."""
    from tradingagents.dataflows import a_stock

    payload = {
        "result": {
            "data": {
                "fzb": [
                    {"报告日": "2025-12-31", "资产总计": "1", "负债合计": "0.5"},
                ]
            }
        }
    }

    class _Resp:
        def json(self):
            return payload

    monkeypatch.setattr(a_stock._requests, "get", lambda *a, **k: _Resp())

    df = a_stock._get_financial_report_sina("002648", "资产负债表", "quarterly", "2026-07-14")
    assert len(df) == 1
    assert str(df.iloc[0]["资产总计"]) == "1"
