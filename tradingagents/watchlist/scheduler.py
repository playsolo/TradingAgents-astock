"""观察调度：可嵌在 Streamlit，也可由 tradingagents-watch 守护进程独占。

通过 ~/.tradingagents/watchlist.scheduler.lock 互斥，避免 Web 与守护双开重复跑。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, TextIO

from tradingagents.watchlist.calendar import slot_key_for
from tradingagents.watchlist.observe import observe_all
from tradingagents.watchlist.store import WatchlistStore, default_store

logger = logging.getLogger(__name__)

_POLL_SECONDS = 30
_started = False
_lock = threading.Lock()
_lock_fh: TextIO | None = None


def build_quick_llm(config: dict[str, Any] | None) -> Any:
    if not config:
        return None
    try:
        from tradingagents.llm_clients import create_llm_client

        client = create_llm_client(
            provider=config.get("llm_provider", "deepseek"),
            model=config.get("quick_think_llm", "deepseek-chat"),
            base_url=config.get("backend_url"),
        )
        return client.get_llm()
    except Exception as e:
        logger.warning("watchlist scheduler LLM init failed: %s", e)
        return None


def _loop(
    store: WatchlistStore,
    config_provider: Callable[[], dict[str, Any] | None],
    stop_event: threading.Event,
) -> None:
    last_slot: str | None = None
    while not stop_event.is_set():
        try:
            from datetime import datetime

            now = datetime.now()
            key = slot_key_for(now)
            if key and key != last_slot:
                items = [i for i in store.list_items() if i.enabled]
                if items:
                    logger.info("watchlist slot %s — observing %d names", key, len(items))
                    cfg = config_provider() or {}
                    llm = build_quick_llm(cfg)
                    observe_all(
                        store, llm=llm, slot_key=key, analysis_config=cfg or None
                    )
                last_slot = key
        except Exception:
            logger.exception("watchlist scheduler tick failed")
        stop_event.wait(_POLL_SECONDS)


def start_watchlist_scheduler(
    *,
    store: WatchlistStore | None = None,
    config_provider: Callable[[], dict[str, Any] | None] | None = None,
) -> None:
    """幂等启动后台守护线程。WATCHLIST_SCHEDULER=0 可关闭。

    若独立守护 tradingagents-watch 已占锁，则跳过（避免重复观察）。
    """
    global _started, _lock_fh
    if os.getenv("WATCHLIST_SCHEDULER", "1").strip() in {"0", "false", "off"}:
        return
    with _lock:
        if _started:
            return
        from tradingagents.watchlist.lockfile import acquire_scheduler_lock

        fh = acquire_scheduler_lock()
        if fh is None:
            logger.info(
                "watchlist in-process scheduler skipped — "
                "lock held (likely tradingagents-watch daemon)"
            )
            return
        _lock_fh = fh
        _started = True
        stop_event = threading.Event()
        thread = threading.Thread(
            target=_loop,
            args=(store or default_store(), config_provider or (lambda: None), stop_event),
            name="watchlist-scheduler",
            daemon=True,
        )
        thread.start()
        logger.info("watchlist scheduler started (slots Mon–Fri 09:35/13:05/15:05)")
