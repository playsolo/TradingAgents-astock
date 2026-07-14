"""观察调度进程互斥锁。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TextIO

LOCK_PATH = Path.home() / ".tradingagents" / "watchlist.scheduler.lock"


def acquire_scheduler_lock() -> TextIO | None:
    """非阻塞独占锁；拿不到返回 None。"""
    import fcntl

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid={os.getpid()}\n")
    fh.flush()
    return fh
