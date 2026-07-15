"""Accuracy settle systemd timer/unit template checks."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMER = ROOT / "deploy" / "tradingagents-accuracy.timer"
SERVICE = ROOT / "deploy" / "tradingagents-accuracy.service"


def test_accuracy_timer_runs_daily_at_2100_shanghai():
    text = TIMER.read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 21:00:00" in text
    assert "Persistent=true" in text
    assert "Unit=tradingagents-accuracy.service" in text
    # Documented assumption: host TZ is Asia/Shanghai (m.wcc.io)
    assert "Asia/Shanghai" in text


def test_accuracy_service_oneshot_runs_cli():
    text = SERVICE.read_text(encoding="utf-8")
    assert "Type=oneshot" in text
    assert "tradingagents accuracy" in text
    assert "TZ=Asia/Shanghai" in text
