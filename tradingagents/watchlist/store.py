"""观察池 JSON 持久化：~/.tradingagents/watchlist.json"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from tradingagents.watchlist.models import Alert, ObservationBriefing, WatchItem

_DEFAULT_PATH = Path.home() / ".tradingagents" / "watchlist.json"
_LOCK = threading.RLock()


class WatchlistStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else _DEFAULT_PATH

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


def default_store() -> WatchlistStore:
    return WatchlistStore()
