"""分析执行模式开关。

``TRADINGAGENTS_ANALYSIS_EXECUTOR``：

* ``web``（默认）：分析在 Streamlit 进程内的 daemon 线程执行（旧行为，适合本地开发）。
* ``worker``：Web 只入队，分析交给独立进程 ``tradingagents-analyze`` 执行。

保持默认 ``web`` 是为了不破坏本地开发与既有测试；生产环境在 systemd 里显式设为
``worker``。
"""

from __future__ import annotations

import os

_ENV = "TRADINGAGENTS_ANALYSIS_EXECUTOR"


def analysis_executor() -> str:
    """Return ``"worker"`` or ``"web"`` (default)."""
    value = (os.getenv(_ENV) or "web").strip().lower()
    return "worker" if value == "worker" else "web"


def is_worker_mode() -> bool:
    """True when analysis execution is delegated to the standalone worker."""
    return analysis_executor() == "worker"
