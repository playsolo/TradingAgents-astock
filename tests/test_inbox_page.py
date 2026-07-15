"""站内事件中心页面纯函数：图标与相对时间。"""

from __future__ import annotations

from web.components.inbox_page import relative_time, severity_icon


def test_severity_icon_maps_known_levels():
    assert severity_icon("info")
    assert severity_icon("warning") != severity_icon("info")
    assert severity_icon("error") != severity_icon("warning")


def test_severity_icon_falls_back_for_unknown():
    assert severity_icon("nope") == severity_icon("nope")  # stable, no crash


def test_relative_time_buckets():
    now = 1_000_000.0
    assert relative_time(now - 10, now=now) == "刚刚"
    assert relative_time(now - 120, now=now) == "2 分钟前"
    assert relative_time(now - 3 * 3600, now=now) == "3 小时前"
    assert relative_time(now - 2 * 86400, now=now) == "2 天前"


def test_relative_time_handles_future_or_bad_value():
    now = 1_000_000.0
    assert relative_time(now + 100, now=now) == "刚刚"
    assert relative_time(None, now=now) == ""
