"""价值波段扫描结果卡片 / 自适应网格 HTML。"""

from web.components.value_swing_scanner import (
    _CARD_GRID_MAX_COLS,
    _analysis_vs_scan_note,
    _candidate_card_html,
    _candidate_grid_html,
)


def _cand(**overrides):
    base = {
        "code": "000651",
        "name": "格力电器",
        "signal_score": 5,
        "score_max": 6,
        "pe_ttm": 10.7,
        "pb": 2.13,
        "price": 39.83,
        "above_ma20": True,
        "near_ma250": True,
        "news_found": True,
        "concept_active": False,
        "hot_topic_match": False,
        "why": "站上MA20 · 接近年线 · 近3日个股新闻",
    }
    base.update(overrides)
    return base


def test_card_html_shows_why_and_score_max():
    html = _candidate_card_html(_cand(name="Foo<script>x</script>"))
    assert "000651" in html
    assert "Foo&lt;script&gt;x&lt;/script&gt;" in html
    assert "信号 5/6" in html
    assert "入选原因" in html
    assert "站上MA20" in html
    assert "尚未深度分析" in html
    for i, line in enumerate(html.splitlines(), 1):
        assert line.strip() != "", f"blank line at {i}"


def test_card_html_backfills_action_plan():
    html = _candidate_card_html(
        _cand(
            analysis={
                "rating": "Buy",
                "horizon": "3-5个交易日",
                "summary": "估值仍具吸引力",
                "non_holders_action": "回踩可建仓",
                "date": "2026-07-14",
                "path": "/tmp/x.json",
            }
        )
    )
    assert "操作建议" in html
    assert "买入" in html
    assert "回踩可建仓" in html
    assert "查看报告" in html
    assert "view=history" in html
    assert "尚未深度分析" not in html


def test_card_html_flags_scan_analysis_disagreement():
    assert _analysis_vs_scan_note("强烈推荐", "Sell") == "与扫描信号分歧"
    html = _candidate_card_html(
        _cand(
            analysis={
                "rating": "Underweight",
                "horizon": "1周",
                "summary": "风险加大",
                "date": "2026-07-14",
            }
        )
    )
    assert "与扫描信号分歧" in html


def test_grid_html_auto_fill_capped_at_three_columns():
    html = _candidate_grid_html([_cand(), _cand(code="000002", name="万科A")])
    assert "display:grid" in html
    assert "auto-fill" in html
    assert f"100% / {_CARD_GRID_MAX_COLS}" in html
    assert html.count("000651") == 1
    assert "000002" in html


def test_empty_grid_is_empty_string():
    assert _candidate_grid_html([]) == ""
