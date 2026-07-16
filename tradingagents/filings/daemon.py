"""美股 SEC 公告 / 财报预告巡检守护进程。

用法：
  tradingagents-filings              # 前台常驻
  tradingagents-filings --once       # 跑一轮后退出

环境变量：
  FILINGS_POLL_SECONDS       常规轮询间隔（默认 900）
  FILINGS_DENSE_POLL_SECONDS 财报窗口内间隔（默认 300）
  SEC_EDGAR_USER_AGENT       必填级建议：Name email@domain
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import date
from typing import Any

from tradingagents.filings.earnings_calendar import earnings_window_status
from tradingagents.filings.monitor import collect_us_watch_tickers, run_filing_poll_once
from tradingagents.filings.store import FilingSeenStore

logger = logging.getLogger(__name__)


def _poll_interval_seconds(tickers: list[str], *, today: date | None = None) -> float:
    normal = float(os.getenv("FILINGS_POLL_SECONDS", "900"))
    dense = float(os.getenv("FILINGS_DENSE_POLL_SECONDS", "300"))
    today = today or date.today()
    for t in tickers:
        try:
            st = earnings_window_status(t, today=today)
        except Exception:  # noqa: BLE001
            continue
        if st.get("phase") in {"dense", "day_of"}:
            return dense
    return normal


def run_forever() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [filings] %(message)s",
    )
    store = FilingSeenStore()
    logger.info("filings monitor started")
    while True:
        try:
            pairs = collect_us_watch_tickers()
            tickers = [t for t, _ in pairs]
            summary = run_filing_poll_once(seen_store=store, tickers=tickers)
            logger.info(
                "poll done tickers=%d previews=%d filings=%d enqueued=%d",
                len(summary.get("tickers") or []),
                len(summary.get("previews") or []),
                len(summary.get("new_filings") or []),
                len(summary.get("enqueued") or []),
            )
            sleep_s = _poll_interval_seconds(tickers)
        except Exception as exc:  # noqa: BLE001
            logger.exception("poll cycle failed: %s", exc)
            sleep_s = float(os.getenv("FILINGS_POLL_SECONDS", "900"))
        time.sleep(max(30.0, sleep_s))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="US SEC filings / earnings monitor")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle and exit",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [filings] %(message)s",
    )
    if args.once:
        summary = run_filing_poll_once()
        logger.info("once summary: %s", summary)
        return 0
    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
