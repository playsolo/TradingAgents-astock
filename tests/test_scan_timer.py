"""Value-swing scan systemd timer/unit template checks."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TIMER = ROOT / "deploy" / "tradingagents-scan.timer"
SERVICE = ROOT / "deploy" / "tradingagents-scan.service"


def test_scan_timer_runs_daily_at_2030_shanghai():
    text = TIMER.read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 20:30:00" in text
    assert "Persistent=true" in text
    assert "Unit=tradingagents-scan.service" in text
    # Documented assumption: host TZ is Asia/Shanghai (m.wcc.io)
    assert "Asia/Shanghai" in text


def test_scan_service_oneshot_enqueues_and_skips_weekend():
    text = SERVICE.read_text(encoding="utf-8")
    assert "Type=oneshot" in text
    assert "tradingagents-scan" in text
    assert "--enqueue" in text
    assert "--skip-non-trading-day" in text
    assert "TZ=Asia/Shanghai" in text
    assert "TimeoutStartSec=10800" in text
