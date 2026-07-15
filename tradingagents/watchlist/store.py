"""观察池 JSON 持久化（按用户隔离）。

- 指定用户：``~/.tradingagents/watchlist/<username>.json``
- 未指定用户（关闭鉴权 / 无会话 / 后台旧数据）：``~/.tradingagents/watchlist.json``

后台守护通过 :func:`iter_user_stores` 遍历所有用户 + 遗留全局文件。
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from pathlib import Path

from tradingagents.watchlist.models import Alert, ObservationBriefing, WatchItem

_LOCK = threading.RLock()

# 用户名检验：与 UserManager._validate_username 对齐（isidentifier），
# 额外拦截空格/斜杠/../ 等路径穿越字符。isidentifier() 接受 Unicode 标识符
# （如中文、希腊字母），但排除空格、斜杠、点号等文件系统不安全字符。
_SAFE_USERNAME = re.compile(r"^[^\s/\\\x00-\x1f]+$")


def _ensure_safe_username(username: str) -> None:
    """检验 username 是否适合作为文件名（即不含路径穿越字符）。"""
    if not username:
        msg = "Username required for per-user watchlist"
        raise ValueError(msg)
    # Python identifier check rejects spaces, dots, slashes — plus the regex
    # catches any edge case isidentifier() might miss.
    if not username.isidentifier() or not _SAFE_USERNAME.match(username):
        msg = f"Invalid watchlist username: {username!r}"
        raise ValueError(msg)


def _legacy_path() -> Path:
    return Path.home() / ".tradingagents" / "watchlist.json"


def _user_dir() -> Path:
    return Path.home() / ".tradingagents" / "watchlist"


def _user_path(username: str) -> Path:
    _ensure_safe_username(username)
    return _user_dir() / f"{username}.json"


class WatchlistStore:
    def __init__(self, path: Path | None = None, *, username: str | None = None):
        if path is not None:
            self.path = Path(path)
        elif username:
            self.path = _user_path(username)
        else:
            self.path = _legacy_path()

    def _read(self) -> list[WatchItem]:
        if not self.path.exists():
            return []
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, dict):
            return []
        items = data.get("items") or []
        out: list[WatchItem] = []
        for raw in items:
            if isinstance(raw, dict) and "baseline" in raw:
                try:
                    out.append(WatchItem.from_dict(raw))
                except (KeyError, TypeError, ValueError):
                    continue
        return out

    def _write(self, items: list[WatchItem]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "items": [i.to_dict() for i in items]}
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def list_items(self) -> list[WatchItem]:
        with _LOCK:
            return self._read()

    def get(self, ticker: str) -> WatchItem | None:
        t = ticker.upper()
        with _LOCK:
            for item in self._read():
                if item.baseline.ticker == t:
                    return item
        return None

    def add(self, item: WatchItem) -> None:
        with _LOCK:
            items = [i for i in self._read() if i.baseline.ticker != item.baseline.ticker]
            items.insert(0, item)
            self._write(items)

    def remove(self, ticker: str) -> bool:
        t = ticker.upper()
        with _LOCK:
            items = self._read()
            kept = [i for i in items if i.baseline.ticker != t]
            if len(kept) == len(items):
                return False
            self._write(kept)
            return True

    def set_enabled(self, ticker: str, enabled: bool) -> None:
        t = ticker.upper()
        with _LOCK:
            items = self._read()
            for item in items:
                if item.baseline.ticker == t:
                    item.enabled = enabled
            self._write(items)

    def append_alerts(self, ticker: str, alerts: list[Alert], max_keep: int = 50) -> None:
        t = ticker.upper()
        with _LOCK:
            items = self._read()
            for item in items:
                if item.baseline.ticker == t:
                    item.alerts = (alerts + item.alerts)[:max_keep]
            self._write(items)

    def mark_observed(
        self,
        ticker: str,
        observed_at: str,
        *,
        slot_key: str | None = None,
        summary: str | None = None,
        briefing: ObservationBriefing | None = None,
    ) -> None:
        t = ticker.upper()
        with _LOCK:
            items = self._read()
            for item in items:
                if item.baseline.ticker == t:
                    item.last_observed_at = observed_at
                    if slot_key is not None:
                        item.last_slot_key = slot_key
                    if summary is not None:
                        item.last_summary = summary
                    if briefing is not None:
                        item.last_briefing = briefing
            self._write(items)


def default_store(username: str | None = None) -> WatchlistStore:
    """返回某用户的观察池 store；``username=None`` 走遗留全局文件。"""
    return WatchlistStore(username=username)


def iter_user_stores() -> Iterator[WatchlistStore]:
    """遍历所有用户的观察池，外加尚未迁移的遗留全局文件（供后台守护使用）。"""
    seen: set[Path] = set()
    user_dir = _user_dir()
    if user_dir.exists():
        for f in sorted(user_dir.glob("*.json")):
            resolved = f.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield WatchlistStore(f)
    legacy = _legacy_path()
    if legacy.exists() and legacy.resolve() not in seen:
        yield WatchlistStore(legacy)


def migrate_legacy_watchlist(username: str) -> bool:
    """把多用户改造前的全局 ``watchlist.json`` 一次性迁移到 ``username`` 名下。

    仅当目标用户文件尚不存在且遗留文件存在时执行（幂等）。返回是否发生迁移。
    """
    target = _user_path(username)
    legacy = _legacy_path()
    with _LOCK:
        if target.exists() or not legacy.exists():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        legacy.replace(target)
    return True
