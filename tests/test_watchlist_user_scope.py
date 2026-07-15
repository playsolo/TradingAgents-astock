"""观察池按用户隔离：每个用户只看到自己的标的。"""

from pathlib import Path

import pytest

from tradingagents.watchlist import store as store_mod
from tradingagents.watchlist.models import Baseline, WatchItem
from tradingagents.watchlist.store import (
    WatchlistStore,
    default_store,
    iter_user_stores,
    migrate_legacy_watchlist,
)


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _item(ticker="002648"):
    base = Baseline(
        ticker=ticker,
        trade_date="2026-07-13",
        market="CN",
        stance="Hold",
        position_pct=10.0,
        baseline_price=20.0,
        entry_price=None,
        stop_loss=None,
        thesis_summary="t",
        major_risks=[],
        log_path="",
    )
    return WatchItem(baseline=base, enabled=True, alerts=[], last_observed_at=None)


def test_per_user_stores_are_isolated(fake_home):
    alice = default_store("alice")
    bob = default_store("bob")

    alice.add(_item("002648"))
    bob.add(_item("300750"))

    assert [i.baseline.ticker for i in alice.list_items()] == ["002648"]
    assert [i.baseline.ticker for i in bob.list_items()] == ["300750"]

    # Distinct files under the per-user directory.
    assert alice.path != bob.path
    assert alice.path == fake_home / ".tradingagents" / "watchlist" / "alice.json"


def test_no_username_uses_legacy_global(fake_home):
    legacy = default_store()
    assert legacy.path == fake_home / ".tradingagents" / "watchlist.json"


def test_invalid_username_rejected(fake_home):
    with pytest.raises(ValueError):
        default_store("../../etc/passwd")
    with pytest.raises(ValueError):
        default_store("a/b")


def test_iter_user_stores_covers_all_users_and_legacy(fake_home):
    default_store("alice").add(_item("002648"))
    default_store("bob").add(_item("300750"))
    # A leftover legacy global file (pre-multiuser install).
    legacy = default_store()
    legacy.add(_item("600000"))

    paths = {s.path.name for s in iter_user_stores()}
    assert paths == {"alice.json", "bob.json", "watchlist.json"}


def test_migrate_legacy_watchlist_moves_once(fake_home):
    legacy = default_store()
    legacy.add(_item("600000"))
    legacy_path = legacy.path
    assert legacy_path.exists()

    migrated = migrate_legacy_watchlist("admin")
    assert migrated is True
    assert not legacy_path.exists()

    admin = default_store("admin")
    assert [i.baseline.ticker for i in admin.list_items()] == ["600000"]

    # Idempotent: second call is a no-op (legacy gone).
    assert migrate_legacy_watchlist("admin") is False


def test_migrate_legacy_watchlist_skips_when_target_exists(fake_home):
    default_store("admin").add(_item("300750"))
    legacy = default_store()
    legacy.add(_item("600000"))

    assert migrate_legacy_watchlist("admin") is False
    # Admin's own data untouched.
    assert [i.baseline.ticker for i in default_store("admin").list_items()] == ["300750"]
    assert legacy.path.exists()


def test_default_store_none_matches_module_legacy_dir(fake_home):
    # Guards against accidental drift between helpers and module paths.
    assert store_mod._legacy_path() == fake_home / ".tradingagents" / "watchlist.json"
    assert store_mod._user_dir() == fake_home / ".tradingagents" / "watchlist"


def test_daemon_run_slot_once_observes_all_users(fake_home, monkeypatch):
    """store=None → 后台守护遍历全部用户观察池。"""
    from tradingagents.watchlist import daemon as daemon_mod
    from tradingagents.watchlist import observe as observe_mod
    from tradingagents.watchlist.models import MarketSnapshot

    default_store("alice").add(_item("002648"))
    default_store("bob").add(_item("300750"))

    monkeypatch.setattr(
        observe_mod,
        "fetch_snapshot",
        lambda *_a, **_k: MarketSnapshot(price=21.0, change_pct=1.0, name="x"),
    )
    monkeypatch.setattr(
        observe_mod,
        "judge_vs_baseline",
        lambda *_a, **_k: {
            "suggested_stance": "Hold",
            "suggested_position_pct": 10.0,
            "new_major_risks": [],
            "summary": "ok",
            "market_brief": "",
            "lean": "neutral",
            "lean_reason": "",
            "scenarios": {},
        },
    )
    monkeypatch.setattr(daemon_mod, "build_quick_llm", lambda *_a, **_k: None)

    from datetime import datetime

    result = daemon_mod.run_slot_once(
        config={}, now=datetime(2026, 7, 14, 9, 36)
    )
    assert result["slot_key"] == "2026-07-14T09:35"
    # Both users' tickers observed in the same slot.
    assert set(result["observed"]) == {"002648", "300750"}
