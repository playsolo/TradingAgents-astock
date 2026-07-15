"""价值波段扫描状态持久化（旁路历史：~/.tradingagents/）。

当前状态：``~/.tradingagents/value_swing_scan.json``
历史归档：``~/.tradingagents/value_swing_scans/<scan_id>.json``
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import fcntl  # POSIX only (macOS/Linux) — used for cross-process locking.
except ImportError:  # pragma: no cover — Windows fallback
    fcntl = None  # type: ignore[assignment]

SCAN_STATUS_IDLE = "idle"
SCAN_STATUS_RUNNING = "running"
SCAN_STATUS_COMPLETED = "completed"
SCAN_STATUS_FAILED = "failed"

_DEFAULT_STATUS_PATH = Path.home() / ".tradingagents" / "value_swing_scan.json"
_DEFAULT_ARCHIVE_DIR = Path.home() / ".tradingagents" / "value_swing_scans"
_LOCK = threading.RLock()
_SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _empty_record() -> dict[str, Any]:
    return {
        "version": _SCHEMA_VERSION,
        "status": SCAN_STATUS_IDLE,
        "pid": None,
        "scan_id": None,
        "started_at": None,
        "finished_at": None,
        "max_candidates": None,
        "enqueue_on_success": False,
        "enqueued": None,
        "error": None,
        "result": None,
        "progress": None,
    }


class ValueSwingScanStore:
    """JSON status + archive for value-swing scans."""

    def __init__(
        self,
        path: Path | None = None,
        archive_dir: Path | None = None,
    ):
        if path is not None:
            self.path = Path(path)
        else:
            override = os.getenv("TRADINGAGENTS_VALUE_SWING_SCAN_PATH", "").strip()
            self.path = Path(override) if override else _DEFAULT_STATUS_PATH
        if archive_dir is not None:
            self.archive_dir = Path(archive_dir)
        else:
            override_dir = os.getenv(
                "TRADINGAGENTS_VALUE_SWING_SCANS_DIR", ""
            ).strip()
            self.archive_dir = (
                Path(override_dir) if override_dir else _DEFAULT_ARCHIVE_DIR
            )

    @contextmanager
    def exclusive(self):
        """Cross-process + cross-thread mutual exclusion around a check-and-set.

        Guards the read-then-``mark_running`` window so a cron worker and a Web
        launch cannot both reserve the slot. Uses an advisory ``flock`` on a
        sidecar lock file; degrades to the in-process lock where ``fcntl`` is
        unavailable (e.g. Windows).
        """
        with _LOCK:
            if fcntl is None:
                yield
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_suffix(".lock")
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def load(self) -> dict[str, Any]:
        with _LOCK:
            return self._read()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_record()
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError):
            return _empty_record()
        if not isinstance(data, dict):
            return _empty_record()
        record = _empty_record()
        record.update({k: data.get(k, record[k]) for k in record})
        if record["status"] not in {
            SCAN_STATUS_IDLE,
            SCAN_STATUS_RUNNING,
            SCAN_STATUS_COMPLETED,
            SCAN_STATUS_FAILED,
        }:
            record["status"] = SCAN_STATUS_IDLE
        return record

    def _write(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(record)
        payload["version"] = _SCHEMA_VERSION
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, ensure_ascii=False, indent=2, fp=f)
        tmp.replace(self.path)

    def mark_running(
        self,
        *,
        max_candidates: int,
        enqueue_on_success: bool,
        pid: int,
        scan_id: str | None = None,
    ) -> dict[str, Any]:
        with _LOCK:
            sid = scan_id or datetime.now().strftime("%Y%m%d_%H%M%S")
            prior = self._read()
            record = _empty_record()
            record.update(
                {
                    "status": SCAN_STATUS_RUNNING,
                    "pid": int(pid),
                    "scan_id": sid,
                    "started_at": _utc_now_iso(),
                    "max_candidates": int(max_candidates),
                    "enqueue_on_success": bool(enqueue_on_success),
                    # Keep the last successful result visible so a failed re-run
                    # still shows the previous candidates (honors mark_failed's
                    # "keep prior result" contract).
                    "result": prior.get("result"),
                    "finished_at": prior.get("finished_at"),
                }
            )
            self._write(record)
            return deepcopy(record)

    def mark_completed(
        self,
        result: dict[str, Any],
        *,
        enqueued: int = 0,
    ) -> dict[str, Any]:
        with _LOCK:
            record = self._read()
            if record["status"] != SCAN_STATUS_RUNNING:
                # Allow completing from a fresh process that already wrote running.
                record = record if record.get("scan_id") else _empty_record()
            record["status"] = SCAN_STATUS_COMPLETED
            record["pid"] = None
            record["finished_at"] = _utc_now_iso()
            record["error"] = None
            record["enqueued"] = int(enqueued)
            record["result"] = deepcopy(result)
            if not record.get("scan_id"):
                record["scan_id"] = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._write(record)
            self._archive(record)
            return deepcopy(record)

    def update_progress(self, progress: dict[str, Any]) -> dict[str, Any] | None:
        """Merge a live progress snapshot into the running record.

        No-op unless a scan is currently ``running`` — this prevents a late
        callback from resurrecting an idle/completed record.
        """
        with _LOCK:
            record = self._read()
            if record.get("status") != SCAN_STATUS_RUNNING:
                return None
            record["progress"] = deepcopy(progress)
            self._write(record)
            return deepcopy(record)

    def mark_failed(self, error: str) -> dict[str, Any]:
        with _LOCK:
            record = self._read()
            record["status"] = SCAN_STATUS_FAILED
            record["pid"] = None
            record["finished_at"] = _utc_now_iso()
            record["error"] = str(error)
            record["enqueued"] = 0
            # Keep prior completed result if present; do not invent one.
            self._write(record)
            return deepcopy(record)

    def _archive(self, record: dict[str, Any]) -> None:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        sid = record.get("scan_id") or datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.archive_dir / f"{sid}.json"
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, ensure_ascii=False, indent=2, fp=f)
        tmp.replace(path)


def default_store() -> ValueSwingScanStore:
    return ValueSwingScanStore()
