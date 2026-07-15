"""CLI flags for detached / scheduled value-swing scans."""

from datetime import datetime
from zoneinfo import ZoneInfo

from tradingagents.strategies import scan_cli


def test_skip_non_trading_day_exits_zero_on_weekend(monkeypatch, capsys):
    saturday = datetime(2026, 7, 18, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(scan_cli, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: saturday)}))
    called = {"run": False}

    def boom(*_a, **_k):
        called["run"] = True
        raise AssertionError("scan must not run on weekend")

    monkeypatch.setattr(scan_cli, "run_scan_job", boom)
    assert scan_cli.main(["--skip-non-trading-day", "--enqueue"]) == 0
    assert called["run"] is False
    assert "非交易日" in capsys.readouterr().out


def test_skip_non_trading_day_runs_on_weekday(monkeypatch):
    monday = datetime(2026, 7, 13, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(scan_cli, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: monday)}))

    def fake_run(**_kwargs):
        return {
            "status": scan_cli.SCAN_STATUS_COMPLETED,
            "result": {
                "scan_date": "2026-07-13",
                "l0_passed": 10,
                "l1a_passed": 5,
                "l1b_passed": 3,
                "l2_passed": 2,
                "duration_seconds": 1.0,
            },
            "enqueued": 0,
        }

    monkeypatch.setattr(scan_cli, "run_scan_job", fake_run)
    monkeypatch.setattr(scan_cli, "default_store", lambda *_a, **_k: object())
    assert scan_cli.main(["--skip-non-trading-day"]) == 0


def test_strategy_growth_accel_flag_forwarded(monkeypatch):
    monday = datetime(2026, 7, 13, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(scan_cli, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: monday)}))
    seen = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return {
            "status": scan_cli.SCAN_STATUS_COMPLETED,
            "result": {
                "scan_date": "2026-07-13",
                "l0_passed": 1,
                "l1a_passed": 1,
                "l1b_passed": 1,
                "l2_passed": 1,
                "duration_seconds": 1.0,
            },
            "enqueued": 0,
        }

    monkeypatch.setattr(scan_cli, "run_scan_job", fake_run)
    monkeypatch.setattr(scan_cli, "default_store", lambda *_a, **_k: object())
    assert scan_cli.main(["--strategy", "growth_accel"]) == 0
    assert seen.get("strategy") == "growth_accel"


def test_default_strategy_runs_both(monkeypatch):
    monday = datetime(2026, 7, 13, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(scan_cli, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: monday)}))
    strategies: list[str] = []

    def fake_run(**kwargs):
        strategies.append(kwargs["strategy"])
        return {
            "status": scan_cli.SCAN_STATUS_COMPLETED,
            "result": {
                "scan_date": "2026-07-13",
                "l0_passed": 1,
                "l1a_passed": 1,
                "l1b_passed": 1,
                "l2_passed": 1,
                "duration_seconds": 1.0,
            },
            "enqueued": 0,
        }

    monkeypatch.setattr(scan_cli, "run_scan_job", fake_run)
    monkeypatch.setattr(scan_cli, "default_store", lambda *_a, **_k: object())
    assert scan_cli.main([]) == 0
    assert strategies == ["value_swing", "growth_accel"]
