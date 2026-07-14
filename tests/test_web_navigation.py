"""URL query 导航：刷新后应恢复 view。"""

from web.navigation import history_path_for, parse_view_params, view_query


def test_view_query_watch():
    assert view_query("watch") == {"view": "watch"}


def test_view_query_history():
    assert view_query("history", ticker="002648", date="2026-07-14") == {
        "view": "history",
        "ticker": "002648",
        "date": "2026-07-14",
    }


def test_parse_view_params_defaults_home():
    assert parse_view_params({})["view"] == "home"
    assert parse_view_params({"view": "watch"})["view"] == "watch"


def test_history_path_for_builds_expected_path(tmp_path, monkeypatch):
    from web import navigation as nav

    log_root = tmp_path / "logs"
    target = (
        log_root
        / "002648"
        / "TradingAgentsStrategy_logs"
        / "full_states_log_2026-07-14.json"
    )
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(nav, "_results_dir", lambda: log_root)

    assert history_path_for("002648", "2026-07-14") == str(target)
    assert history_path_for("002648", "2099-01-01") is None
