"""观察快照：资金流注入 + 新闻标题清洗。"""

from __future__ import annotations

from tradingagents.watchlist.snapshot import _extract_headlines, fetch_snapshot


def test_extract_headlines_prefers_markdown_titles():
    raw = """## 002648 News
### 30.88亿元主力资金今日撤离基础化工板块 (source: 证券时报网)
-11957.37 600309 万华化学 -1.63 -8574.20 002648
Link: http://example.com/a
### 卫星化学预计上半年净利润增长 (source: 界面新闻)
卫星化学（002648）预计上半年净利润大幅增长，一体化优势显现。
"""
    headlines = _extract_headlines(raw, limit=8)
    assert headlines[0].startswith("30.88亿元主力资金")
    assert "002648" not in headlines[0]
    assert any("净利润" in h for h in headlines)
    assert not any(h.startswith("-11957") for h in headlines)
    assert any("一体化优势" in h for h in headlines)


def test_fetch_snapshot_includes_realtime_main_net_inflow(monkeypatch):
    monkeypatch.setattr(
        "tradingagents.dataflows.a_stock._tencent_quote",
        lambda *_a, **_k: {
            "002648": {
                "price": 23.64,
                "change_pct": 7.11,
                "name": "卫星化学",
                "pe_ttm": 8.0,
                "turnover_pct": 1.89,
            }
        },
    )
    monkeypatch.setattr(
        "tradingagents.dataflows.a_stock.get_news",
        lambda *_a, **_k: "### 基础化工板块资金净流出超三十亿 (source: x)\nbody",
    )
    monkeypatch.setattr(
        "tradingagents.dataflows.a_stock.get_realtime_main_net_inflow",
        lambda ticker: 121_320_000.0 if ticker == "002648" else None,
    )

    snap = fetch_snapshot("002648")
    assert snap.price == 23.64
    assert snap.main_net_inflow == 121_320_000.0
    assert snap.headlines and "资金净流出" in snap.headlines[0]
