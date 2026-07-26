"""不依赖 Web 的观察守护进程。

用法：
  tradingagents-watch              # 前台常驻，交易日到点执行
  tradingagents-watch --once       # 若当前落在观察窗则跑一次后退出

与 Streamlit 内嵌调度通过 ~/.tradingagents/watchlist.scheduler.lock 互斥。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from tradingagents.watchlist.calendar import (
    OBSERVE_SLOTS,
    US_OBSERVE_SLOTS,
    active_observe_slots,
)
from tradingagents.watchlist.lockfile import LOCK_PATH, acquire_scheduler_lock
from tradingagents.watchlist.observe import observe_all
from tradingagents.watchlist.scheduler import build_quick_llm
from tradingagents.watchlist.store import WatchlistStore, iter_user_stores

logger = logging.getLogger(__name__)

_POLL_SECONDS = 30

_PROVIDER_QUICK_DEFAULTS = {
    "deepseek": "deepseek-v4-flash",
    "minimax": "MiniMax-M3",
    "qwen": "qwen3.5-flash",
    "glm": "glm-4-flash",
    "openai": "gpt-4.1-mini",
    "anthropic": "claude-sonnet-4-6",
}
_PROVIDER_DEEP_DEFAULTS = {
    "deepseek": "deepseek-v4-flash",
    "minimax": "MiniMax-M3",
    "qwen": "qwen3.5-plus",
    "glm": "glm-4-plus",
    "openai": "gpt-5.4",
    "anthropic": "claude-sonnet-4-6",
}


def config_from_env() -> dict[str, Any]:
    provider = (os.getenv("DEFAULT_LLM_PROVIDER") or "").strip().lower()
    if not provider:
        if os.getenv("DEEPSEEK_API_KEY"):
            provider = "deepseek"
        elif os.getenv("MINIMAX_API_KEY"):
            provider = "minimax"
        else:
            provider = "deepseek"
    quick = (
        os.getenv("WATCHLIST_QUICK_LLM")
        or os.getenv("QUICK_THINK_LLM")
        or _PROVIDER_QUICK_DEFAULTS.get(provider, "deepseek-v4-flash")
    )
    deep = (
        os.getenv("WATCHLIST_DEEP_LLM")
        or os.getenv("DEEP_THINK_LLM")
        or _PROVIDER_DEEP_DEFAULTS.get(provider, "deepseek-v4-pro")
    )
    backend = (os.getenv("BACKEND_URL") or "").strip() or None
    return {
        "llm_provider": provider,
        "quick_think_llm": quick,
        "deep_think_llm": deep,
        "backend_url": backend,
    }


def run_slot_once(
    *,
    store: WatchlistStore | None = None,
    config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """若当前时刻命中观察窗则执行一次；否则 noop。

    ``store=None`` 时遍历全部用户观察池（含遗留全局文件）。
    """
    cfg = config if config is not None else config_from_env()
    dt = now or datetime.now()
    active = active_observe_slots(dt)
    if not active:
        return {"slot_key": None, "observed": {}}
    llm = build_quick_llm(cfg)
    stores = [store] if store is not None else list(iter_user_stores())
    observed: dict[str, Any] = {}
    keys: list[str] = []
    for market, key in active:
        keys.append(key)
        for s in stores:
            batch = observe_all(
                s,
                llm=llm,
                slot_key=key,
                analysis_config=cfg or None,
                market=market,
                now=dt,
            )
            observed.update(batch.alerts)
    return {"slot_key": ",".join(keys), "observed": observed}


def run_forever(
    *,
    store: WatchlistStore | None = None,
    config: dict[str, Any] | None = None,
    poll_seconds: int = _POLL_SECONDS,
) -> None:
    """常驻轮询（需已持有调度锁）。``store=None`` 时遍历全部用户观察池。"""
    cfg = config if config is not None else config_from_env()
    last_slots: set[str] = set()
    stop = threading.Event()
    cn_slots = " / ".join(f"{h:02d}:{m:02d}" for h, m in OBSERVE_SLOTS)
    us_slots = " / ".join(f"{h:02d}:{m:02d}" for h, m in US_OBSERVE_SLOTS)
    logger.info(
        "watchlist daemon running — CN %s (Beijing); US %s (ET)",
        cn_slots,
        us_slots,
    )
    while not stop.is_set():
        try:
            now = datetime.now()
            active = active_observe_slots(now)
            for market, key in active:
                if key in last_slots:
                    continue
                stores = [store] if store is not None else list(iter_user_stores())
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
                        "slot %s (%s) — %d symbols across %d watchlists",
                        key,
                        market,
                        total,
                        len(stores),
                    )
                else:
                    logger.info("slot %s (%s) — watchlists empty, skip", key, market)
                last_slots.add(key)
            active_keys = {k for _, k in active}
            last_slots &= active_keys
        except Exception:
            logger.exception("daemon tick failed")
        stop.wait(poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A股/美股观察池守护进程（不依赖 Streamlit Web）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="仅在当前处于观察窗口时执行一次后退出",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="打印 DEBUG 日志",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 加载项目根 .env（若存在）
    try:
        from dotenv import load_dotenv

        root = Path(__file__).resolve().parents[2]
        load_dotenv(root / ".env", override=True)
    except Exception:
        pass

    lock = acquire_scheduler_lock()
    if lock is None:
        logger.error(
            "无法启动：已有观察调度在运行（Web 或另一个 tradingagents-watch）。"
            "若确认无进程，可删除 %s 后重试。",
            LOCK_PATH,
        )
        return 1

    try:
        if args.once:
            result = run_slot_once()
            if result["slot_key"] is None:
                logger.info("当前不在观察窗口，退出")
            else:
                n_alert = sum(len(v) for v in result["observed"].values())
                logger.info(
                    "slot %s done — %d symbols, %d alerts",
                    result["slot_key"],
                    len(result["observed"]),
                    n_alert,
                )
            return 0
        run_forever()
        return 0
    finally:
        try:
            lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
