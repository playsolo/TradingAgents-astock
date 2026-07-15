"""CLI：独立运行价值波段扫描（关 Web 后仍可完成）。

用法：
  tradingagents-scan
  tradingagents-scan --max-candidates 15 --enqueue
  tradingagents-scan --enqueue --skip-non-trading-day
  python -m tradingagents.strategies.scan_cli --enqueue
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from tradingagents.strategies.scan_runner import run_scan_job
from tradingagents.strategies.scan_store import (
    SCAN_STATUS_COMPLETED,
    default_store,
)
from tradingagents.watchlist.calendar import CN_TZ, is_cn_trading_day

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="价值波段三阶漏斗扫描（独立进程）")
    p.add_argument(
        "--max-candidates",
        type=int,
        default=15,
        help="L2 候选上限（默认 15）",
    )
    p.add_argument(
        "--enqueue",
        action="store_true",
        help="扫描成功后自动写入分析队列（夜间无人值守用）",
    )
    p.add_argument(
        "--skip-non-trading-day",
        action="store_true",
        help="非交易日（周末）直接 exit 0，不扫描（定时任务用）",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)
    if args.skip_non_trading_day:
        now = datetime.now(CN_TZ)
        if not is_cn_trading_day(now):
            logger.info("非交易日 %s，跳过价值波段扫描", now.date())
            print(f"非交易日 {now.date()}，跳过扫描")
            return 0
    store = default_store()
    record = run_scan_job(
        max_candidates=args.max_candidates,
        enqueue_on_success=args.enqueue,
        scan_store=store,
    )
    if record.get("status") != SCAN_STATUS_COMPLETED:
        print(f"扫描失败: {record.get('error')}", file=sys.stderr)
        return 1
    result = record.get("result") or {}
    print(
        f"扫描完成 {result.get('scan_date')}: "
        f"{result.get('l0_passed')}→{result.get('l1a_passed')}→"
        f"{result.get('l1b_passed')}→{result.get('l2_passed')} 候选, "
        f"{result.get('duration_seconds', 0):.0f}s"
        + (
            f", 已入队 {record.get('enqueued', 0)} 只"
            if args.enqueue
            else ""
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
