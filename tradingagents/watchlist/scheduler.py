"""观察调度：可嵌在 Streamlit，也可由 tradingagents-watch 守护进程独占。

通过 ~/.tradingagents/watchlist.scheduler.lock 互斥，避免 Web 与守护双开重复跑。
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, TextIO

from tradingagents.watchlist.calendar import active_observe_slots
from tradingagents.watchlist.observe import observe_all
from tradingagents.watchlist.store import WatchlistStore, iter_user_stores

logger = logging.getLogger(__name__)

_POLL_SECONDS = 30
_started = False
_lock = threading.Lock()
_lock_fh: TextIO | None = None


def build_quick_llm(config: dict[str, Any] | None) -> Any:
    if not config:
        return None
    try:
        from tradingagents.llm_clients import create_llm_client_with_fallback

        client = create_llm_client_with_fallback(
            provider=config.get("llm_provider", "deepseek"),
            model=config.get("quick_think_llm", "deepseek-chat"),
            base_url=config.get("backend_url"),
            fallback_chain=config.get("fallback_chain") or [],
        )
        return client.get_llm()
    except Exception as e:
        logger.warning("watchlist scheduler LLM init failed: %s", e)
        return None


def _loop(
    store: WatchlistStore | None,
    config_provider: Callable[[], dict[str, Any] | None],
    stop_event: threading.Event,
) -> None:
    last_slots: set[str] = set()
    while not stop_event.is_set():
        try:
            from datetime import datetime

            now = datetime.now()
            active = active_observe_slots(now)
            for market, key in active:
                if key in last_slots:
                    continue
                stores = [store] if store is not None else list(iter_user_stores())
                cfg = config_provider() or {}
                llm = build_quick_llm(cfg)
                total = 0
                for s in stores:
                    items = [
                        i
                        for i in s.list_items()
                        if i.enabled and (i.baseline.market or "CN") == market
                    ]
                    if not items:
                        continue
                    total += len(items)
                    observe_all(
                        s,
                        llm=llm,
                        slot_key=key,
                        analysis_config=cfg or None,
                        market=market,
                    )
                if total:
                    logger.info(
                        "watchlist slot %s (%s) — observing %d names across %d watchlists",
                        key,
                        market,
                        total,
                        len(stores),
                    )
                last_slots.add(key)
            # Drop keys that are no longer active so the next day's same HH:MM can fire.
            active_keys = {k for _, k in active}
            last_slots &= active_keys
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
            args=(store, config_provider or (lambda: None), stop_event),
            name="watchlist-scheduler",
            daemon=True,
        )
        thread.start()
        logger.info(
            "watchlist scheduler started "
            "(CN 09:35/13:05/15:05 Beijing; US 09:35/12:05/16:05 ET)"
        )
