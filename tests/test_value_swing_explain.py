"""价值波段：选股规则快照与入选因子拆解。"""

from tradingagents.strategies.value_swing import (
    StockInfo,
    l2_factor_hits,
    l2_score_max,
    selection_rules_snapshot,
    why_selected_line,
)


def test_l2_score_max_counts_only_active_factors():
    """休眠因子（主力/龙虎）不计入展示满分。"""
    assert l2_score_max() == 7
    assert l2_score_max(only_active=False) == 9


def test_selection_rules_snapshot_exposes_thresholds_and_active_l2():
    snap = selection_rules_snapshot()
    assert "成交额" in snap["l0"][0] or any("3000" in x for x in snap["l0"])
    assert any("PE" in x for x in snap["l1a"])
    assert any("负债" in x for x in snap["l1b"])
    assert snap["l2"]["score_max"] == 7
    assert "站上MA20" in snap["l2"]["active"]
    assert "远期估值更便宜" in snap["l2"]["active"]
    assert any("主力" in x or "龙虎" in x for x in snap["l2"]["dormant"])


def test_l2_factor_hits_marks_hit_and_dormant():
    info = StockInfo(
        code="000651",
        above_ma20=True,
        near_ma250=False,
        news_found=True,
        hot_topic_match=True,
        concept_active=False,
        northbound_net_3d=1.0,
        signal_score=4,
    )
    hits = l2_factor_hits(info)
    by_key = {h["key"]: h for h in hits}
    assert by_key["above_ma20"]["hit"] is True
    assert by_key["near_ma250"]["hit"] is False
    assert by_key["news_found"]["hit"] is True
    assert by_key["fund_flow"]["active"] is False
    assert by_key["fund_flow"]["hit"] is False


def test_l2_factor_hits_accepts_dict_candidate():
    hits = l2_factor_hits(
        {
            "above_ma20": True,
            "near_ma250": True,
            "news_found": False,
            "hot_topic_match": False,
            "concept_active": True,
            "northbound_net_3d": -1,
        }
    )
    hit_labels = [h["label"] for h in hits if h["hit"]]
    assert "站上MA20" in hit_labels
    assert "接近年线" in hit_labels
    assert any("概念活跃" in label for label in hit_labels)
    assert "北向" not in "".join(hit_labels) or all(
        not h["hit"] for h in hits if h["key"] == "northbound"
    )


def test_why_selected_line_lists_active_hits_only():
    line = why_selected_line(
        {
            "above_ma20": True,
            "near_ma250": True,
            "news_found": True,
            "hot_topic_match": False,
            "concept_active": False,
            "northbound_net_3d": 2.0,
            "fund_flow_main_3d": 1.0,  # dormant even if set
        }
    )
    assert "站上MA20" in line
    assert "接近年线" in line
    assert "个股新闻" in line or "新闻" in line
    assert "主力" not in line


def test_why_selected_line_includes_exp_penalty():
    line = why_selected_line(
        {
            "above_ma20": True,
            "exp_score_delta": -1,
            "exp_label": "一致预期暗示盈利下滑",
            "exp_hit": False,
        }
    )
    assert "站上MA20" in line
    assert "盈利下滑" in line


def test_exp_hit_factor():
    hits = l2_factor_hits({"exp_hit": True, "above_ma20": False})
    by_key = {h["key"]: h for h in hits}
    assert by_key["exp_hit"]["hit"] is True
    assert by_key["exp_hit"]["active"] is True
