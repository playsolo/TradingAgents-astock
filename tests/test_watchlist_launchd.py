"""launchd plist 模板应声明 KeepAlive / RunAtLoad。"""

from pathlib import Path
import plistlib


def test_watchlist_plist_template_keeps_alive():
    root = Path(__file__).resolve().parents[1]
    template = root / "scripts" / "com.tradingagents.watchlist.plist.template"
    assert template.exists()
    data = plistlib.loads(template.read_bytes())
    assert data["Label"] == "com.tradingagents.watchlist"
    assert data["KeepAlive"] is True
    assert data["RunAtLoad"] is True
    assert "ProgramArguments" in data
