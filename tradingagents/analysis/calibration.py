"""全量校准锚点：伪增量只挂在最近一次 full_reeval，不滚雪球。"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from tradingagents.watchlist.models import Baseline

_LOCK = threading.RLock()

_DEFAULT_PATH = Path(
    os.getenv(
        "TRADINGAGENTS_CALIBRATION_PATH",
        str(Path.home() / ".tradingagents" / "calibration_anchors.json"),
    )
)


def _key(ticker: str, market: str) -> str:
    return f"{(market or 'CN').upper()}:{(ticker or '').strip().upper()}"


class CalibrationStore:
    """Persist last full-reeval baseline per (market, ticker)."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else _DEFAULT_PATH

    def _read(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        anchors = data.get("anchors")
        return anchors if isinstance(anchors, dict) else {}

    def _write(self, anchors: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "anchors": anchors}
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def get(self, ticker: str, market: str = "CN") -> Baseline | None:
        with _LOCK:
            raw = self._read().get(_key(ticker, market))
        if not isinstance(raw, dict):
            return None
        try:
            return Baseline.from_dict(raw)
        except (KeyError, TypeError, ValueError):
            return None

    def save(self, baseline: Baseline) -> None:
        with _LOCK:
            anchors = self._read()
            anchors[_key(baseline.ticker, baseline.market)] = baseline.to_dict()
            self._write(anchors)

    def delete(self, ticker: str, market: str = "CN") -> bool:
        with _LOCK:
            anchors = self._read()
            key = _key(ticker, market)
            if key not in anchors:
                return False
            del anchors[key]
            self._write(anchors)
            return True


def default_calibration_store() -> CalibrationStore:
    return CalibrationStore()
