"""价值波段扫描结果卡片 / 自适应网格 HTML。"""

from web.components.value_swing_scanner import (
    _CARD_GRID_MAX_COLS,
    _candidate_card_html,
    _candidate_grid_html,
    _candidate_signal_labels,
)


def _cand(**overrides):
    base = {
        "code": "000651",
        "name": "格力电器",
        "signal_score": 5,
        "pe_ttm": 10.7,
        "pb": 2.13,
        "price": 39.83,
        "above_ma20": True,
        "near_ma250": True,
        "news_found": True,
        "concept_active": False,
        "hot_topic_match": False,
    }
    base.update(overrides)
    return base


def test_signal_labels_omit_emoji_noise():
    labels = _candidate_signal_labels(_cand())
    assert "站上MA20" in labels
    assert "接近年线" in labels
    assert "有新闻" in labels
    assert all("📰" not in s and "🔥" not in s for s in labels)


def test_card_html_is_vertical_tile():
    html = _candidate_card_html(_cand(name='Foo<script>x</script>'))
    assert "000651" in html
    assert "Foo&lt;script&gt;x&lt;/script&gt;" in html
    assert "flex-direction:column" in html
    assert "信号 5/10" in html
    assert "强烈推荐" in html


def test_grid_html_auto_fill_capped_at_three_columns():
    html = _candidate_grid_html([_cand(), _cand(code="000002", name="万科A")])
    assert "display:grid" in html
    assert "auto-fill" in html
    assert f"100% / {_CARD_GRID_MAX_COLS}" in html
    assert html.count("000651") == 1
    assert "000002" in html


def test_empty_grid_is_empty_string():
    assert _candidate_grid_html([]) == ""
