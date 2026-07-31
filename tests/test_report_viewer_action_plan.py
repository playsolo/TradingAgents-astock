"""Pure formatting helpers behind the report-page action-plan card."""

from __future__ import annotations

from web.components.report_viewer import (
    action_plan_card_html,
    action_plan_rating_style,
    format_action_plan_levels,
)


def test_format_levels_labels_and_orders_present_values():
    levels = {
        "reduce_low": 101.0,
        "reduce_high": 102.5,
        "stop_loss": 95.66,
        "watch_support": 96.0,
        "reentry_low": 100.0,
        "reentry_high": 101.0,
    }
    rows = format_action_plan_levels(levels)
    assert rows == [
        ("减持区间", "101 – 102.5"),
        ("止损位", "95.66"),
        ("关注支撑", "96"),
        ("回补区间", "100 – 101"),
    ]


def test_format_levels_skips_nulls_and_empty():
    assert format_action_plan_levels({}) == []
    assert format_action_plan_levels(
        {"reduce_low": None, "reduce_high": None, "stop_loss": None}
    ) == []


def test_format_levels_single_bound_zone():
    # Only one side of a zone stated → show that single value, no dash.
    assert format_action_plan_levels({"reduce_high": 102.5}) == [
        ("减持区间", "102.5"),
    ]
    assert format_action_plan_levels({"reentry_low": 100.0}) == [
        ("回补区间", "100"),
    ]


def test_rating_style_maps_five_tier_to_color_and_chinese():
    assert action_plan_rating_style("Buy") == ("#22c55e", "买入")
    assert action_plan_rating_style("Overweight") == ("#10b981", "增持")
    assert action_plan_rating_style("Hold") == ("#fbbf24", "持有")
    assert action_plan_rating_style("Underweight") == ("#f97316", "减持")
    assert action_plan_rating_style("Sell") == ("#ef4444", "卖出")


def test_rating_style_unknown_falls_back_to_neutral():
    color, label = action_plan_rating_style("weird")
    assert color == "#fbbf24"
    assert label == "weird"


def test_card_html_escapes_untrusted_plan_text():
    plan = {
        "rating": "Underweight",
        "summary": "<script>alert('xss')</script>逢高减仓",
        "horizon": "<b>1-2w</b>",
    }
    html = action_plan_card_html(plan)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<b>1-2w</b>" not in html
    assert "&lt;b&gt;1-2w&lt;/b&gt;" in html
    # The plain summary text still survives (escaped context).
    assert "逢高减仓" in html


def test_card_html_omits_horizon_when_absent():
    html = action_plan_card_html({"rating": "Hold", "summary": "观望"})
    assert "时限" not in html
    assert "观望" in html
