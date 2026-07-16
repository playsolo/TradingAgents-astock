"""Persist seen SEC accessions and earnings preview alerts."""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path
from typing import Any

_DEFAULT_PATH = Path.home() / ".tradingagents" / "filings_seen.json"
_LOCK = threading.Lock()


class FilingSeenStore:
    """Dedupe store: accession → metadata; earnings preview keys → date."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_PATH

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "accessions": {}, "earnings_previews": {}}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "accessions": {}, "earnings_previews": {}}
        if not isinstance(data, dict):
            return {"version": 1, "accessions": {}, "earnings_previews": {}}
        data.setdefault("version", 1)
        data.setdefault("accessions", {})
        data.setdefault("earnings_previews", {})
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=f"{self.path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            tmp = Path(f.name)
        tmp.replace(self.path)

    def has_accession(self, accession: str) -> bool:
        acc = (accession or "").strip()
        if not acc:
            return False
        with _LOCK:
            return acc in (self._read().get("accessions") or {})

    def mark_accession(self, accession: str, meta: dict[str, Any] | None = None) -> None:
        acc = (accession or "").strip()
        if not acc:
            return
        with _LOCK:
            data = self._read()
            accessions = data.setdefault("accessions", {})
            accessions[acc] = meta or {}
            # Cap growth
            if len(accessions) > 5000:
                # Drop oldest by insertion order (Py3.7+ dict order)
                for key in list(accessions.keys())[: len(accessions) - 4000]:
                    accessions.pop(key, None)
            self._write(data)

    def has_earnings_preview(self, ticker: str, earnings_date: str) -> bool:
        key = f"{(ticker or '').strip().upper()}|{earnings_date}"
        with _LOCK:
            return key in (self._read().get("earnings_previews") or {})

    def mark_earnings_preview(self, ticker: str, earnings_date: str) -> None:
        key = f"{(ticker or '').strip().upper()}|{earnings_date}"
        if not key.startswith("|"):
            with _LOCK:
                data = self._read()
                previews = data.setdefault("earnings_previews", {})
                previews[key] = True
                if len(previews) > 2000:
                    for k in list(previews.keys())[: len(previews) - 1500]:
                        previews.pop(k, None)
                self._write(data)
