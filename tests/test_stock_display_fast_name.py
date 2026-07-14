"""列表取名不得触发 mootdx 全市场映射（会卡住 Streamlit）。"""

import web.stock_display as stock_display


def test_format_list_label_uses_tencent_not_mootdx_map(monkeypatch):
    calls = {"map": 0, "tencent": 0}

    def boom():
        calls["map"] += 1
        raise AssertionError("must not call _build_name_code_map in list UI")

    def fake_tencent(codes):
        calls["tencent"] += 1
        code = codes[0]
        return {code: {"name": "卫星化学", "price": 1.0}}

    monkeypatch.setattr(stock_display, "_tencent_name", lambda code: fake_tencent([code])[code]["name"])
    # 即使 resolve_stock_name 走慢路径也不该被 format_list 触发
    monkeypatch.setattr(
        "tradingagents.dataflows.a_stock._build_name_code_map",
        boom,
        raising=False,
    )

    label = stock_display.format_list_ticker_label("002648", "2026-07-14")
    assert label == "002648 卫星化学  ·  2026-07-14"
    assert calls["map"] == 0


def test_resolve_stock_name_prefers_tencent(monkeypatch):
    monkeypatch.setattr(stock_display, "_tencent_name", lambda code: "贵州茅台")
    monkeypatch.setattr(
        stock_display,
        "_mootdx_name_if_cached",
        lambda code: (_ for _ in ()).throw(AssertionError("should not need mootdx")),
    )
    assert stock_display.resolve_stock_name("600519") == "贵州茅台"
