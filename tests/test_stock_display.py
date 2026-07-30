"""Tests for web stock display labels."""

import web.stock_display as stock_display


def test_generate_markdown_uses_display_label(monkeypatch):
    from web.pdf_export import generate_markdown

    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: None)
    state = {"market_report": "600602 云赛智联 技术面分析报告"}

    markdown = generate_markdown(state, "600602", "2026-06-05", "hold")

    assert "- **股票代码**：600602 云赛智联" in markdown
    assert "600602 云赛智联 技术面分析报告" in markdown


def test_stock_display_label_resolves_code_to_name(monkeypatch):
    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: "退市博元")

    assert stock_display.stock_display_label("600370") == "600370 退市博元"
    assert stock_display.stock_display_label("SH600370") == "600370 退市博元"
    assert stock_display.stock_display_label("600370.SH") == "600370 退市博元"


def test_format_list_ticker_label_includes_name_and_suffix(monkeypatch):
    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: "卫星化学")

    assert (
        stock_display.format_list_ticker_label("002648", "2026-07-14")
        == "002648 卫星化学  ·  2026-07-14"
    )
    assert (
        stock_display.format_list_ticker_label("002648", "基准 Hold", "仓位 10%")
        == "002648 卫星化学  ·  基准 Hold  ·  仓位 10%"
    )


def test_stock_display_label_removes_invisible_name_chars(monkeypatch):
    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: "*ST三房\x00")

    assert stock_display.stock_display_label("600370") == "600370 *ST三房"


def test_stock_display_label_resolves_name_input(monkeypatch):
    monkeypatch.setattr(stock_display, "_resolve_display_code", lambda ticker: "600370")
    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: "退市博元")

    assert stock_display.stock_display_label("退市博元") == "600370 退市博元"


def test_stock_display_label_falls_back_to_state_name(monkeypatch):
    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: None)

    assert (
        stock_display.stock_display_label("600370", {"company_name": "退市博元"})
        == "600370 退市博元"
    )


def test_stock_display_label_falls_back_to_original_input_name(monkeypatch):
    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: None)

    assert (
        stock_display.stock_display_label("600370", {"stock_input": "退市博元"})
        == "600370 退市博元"
    )


def test_stock_display_label_falls_back_to_report_code_name(monkeypatch):
    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: None)

    state = {
        "stock_input": "600602",
        "market_report": "# 600602 云赛智联 技术面分析报告\n**标的：600602 云赛智联**",
    }

    assert stock_display.stock_display_label("600602", state) == "600602 云赛智联"


def test_stock_display_label_falls_back_to_nested_report_code_name(monkeypatch):
    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: None)

    state = {
        "risk_debate_state": {
            "judge_decision": "对600602 云赛智联给出Sell评级。",
        },
    }

    assert stock_display.stock_display_label("600602", state) == "600602 云赛智联"


def test_stock_display_label_falls_back_to_code(monkeypatch):
    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: None)

    assert stock_display.stock_display_label("600370") == "600370"


def test_rejects_valuation_jargon_as_stock_name():
    for junk in ("静态", "动态", "年化", "年华", "稀释", "摊薄", "静态市盈率约"):
        assert not stock_display._is_plausible_stock_name(junk, "601899")


def test_a_share_resolved_name_beats_jargon_extract(monkeypatch):
    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: "紫金矿业")

    state = {"fundamentals_report": "601899 静态 市盈率约 15 倍，紫金矿业现金流稳健"}
    assert stock_display.stock_display_label("601899", state) == "601899 紫金矿业"
    assert stock_display._extract_stock_name_from_text(
        "601899", "601899 静态 市盈率约 15 倍"
    ) is None


def test_prefer_display_name_keeps_resolved_over_shorter_extract():
    assert (
        stock_display._prefer_display_name("紫金矿业", "静态") == "紫金矿业"
    )
    assert (
        stock_display._prefer_display_name("Micron Technology", "稀释")
        == "Micron Technology"
    )


def test_normalize_stock_mentions_adds_name_without_duplicates(monkeypatch):
    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: "*ST三房")

    text = (
        "600370 技术面分析报告\n"
        "标的：600370 -> 分析日期\n"
        "600370 *ST三房 已经补全。\n"
        "*ST三房 最新可用交易日为 2026-06-02。"
    )

    assert stock_display.normalize_stock_mentions(text, "600370") == (
        "600370 *ST三房 技术面分析报告\n"
        "标的：600370 *ST三房 -> 分析日期\n"
        "600370 *ST三房 已经补全。\n"
        "600370 *ST三房 最新可用交易日为 2026-06-02。"
    )


def test_normalize_report_state_mentions_updates_generated_fields(monkeypatch):
    monkeypatch.setattr(stock_display, "resolve_stock_name", lambda ticker: "*ST三房")
    state = {
        "company_of_interest": "600370",
        "market_report": "600370 技术面分析报告",
        "investment_debate_state": {
            "bull_history": "看多 600370",
            "round": 1,
        },
        "risk_debate_state": {
            "judge_decision": "*ST三房 风险偏高",
        },
    }

    result = stock_display.normalize_report_state_mentions(state, "600370")

    assert result is state
    assert state["company_of_interest"] == "600370"
    assert state["market_report"] == "600370 *ST三房 技术面分析报告"
    assert state["investment_debate_state"]["bull_history"] == "看多 600370 *ST三房"
    assert state["investment_debate_state"]["round"] == 1
    assert state["risk_debate_state"]["judge_decision"] == "600370 *ST三房 风险偏高"


def test_clean_stock_name_strips_xd_xr_dr_prefix():
    assert stock_display._clean_stock_name("XD华夏银") == "华夏银"
    assert stock_display._clean_stock_name("XR平安银") == "平安银"
    assert stock_display._clean_stock_name("DR兴业银") == "兴业银"
    assert stock_display._clean_stock_name("xd中国银行") == "中国银行"
    assert stock_display._clean_stock_name("xr招商银") == "招商银"
    assert stock_display._clean_stock_name("XD 农业银行") == "农业银行"
    # Unprefixed names are untouched
    assert stock_display._clean_stock_name("华夏银行") == "华夏银行"
    assert stock_display._clean_stock_name("贵州茅台") == "贵州茅台"
    assert stock_display._clean_stock_name("UiPath Inc.") == "UiPath Inc."


def test_weighted_approx_rejected_as_stock_name():
    """加权近似 (weighted approximate) is a report phrase, never a stock name."""
    assert stock_display.is_junk_stock_name("加权近似")
    assert stock_display.is_junk_stock_name("加权")
    assert stock_display.is_junk_stock_name("近似")


def test_resolve_stock_name_prefers_mootdx_over_truncated_tencent(monkeypatch, tmp_path):
    """During XD/XR/DR periods Tencent truncates names; mootdx has the full name."""
    # Simulate: Tencent returns "XD华夏银" → _clean_stock_name strips XD → "华夏银"
    monkeypatch.setattr(
        stock_display,
        "_tencent_name",
        lambda code: "华夏银",
    )
    monkeypatch.setattr(
        stock_display,
        "_mootdx_name_if_cached",
        lambda code: "华夏银行",
    )
    cache = stock_display.StockNameCache(tmp_path / "names.json")
    monkeypatch.setattr(stock_display, "_NAME_CACHE", cache)
    stock_display.resolve_stock_name.cache_clear()

    assert stock_display.resolve_stock_name("600015") == "华夏银行"


def test_resolve_stock_name_falls_back_to_tencent_when_mootdx_unavailable(monkeypatch, tmp_path):
    """When mootdx map is not built yet, Tencent (even truncated) is used."""
    monkeypatch.setattr(
        stock_display,
        "_tencent_name",
        lambda code: "华夏银",
    )
    monkeypatch.setattr(
        stock_display,
        "_mootdx_name_if_cached",
        lambda code: None,
    )
    cache = stock_display.StockNameCache(tmp_path / "names.json")
    monkeypatch.setattr(stock_display, "_NAME_CACHE", cache)
    stock_display.resolve_stock_name.cache_clear()

    assert stock_display.resolve_stock_name("600015") == "华夏银"
