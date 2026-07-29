"""CLI：独立运行策略扫描（关 Web 后仍可完成）。

用法：
  tradingagents-scan                         # 默认：价值 + 成长都跑
  tradingagents-scan --strategy value_swing
  tradingagents-scan --strategy growth_accel
  tradingagents-scan --strategy both --enqueue
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
    STRATEGY_ALL,
    STRATEGY_BOTH,
    STRATEGY_GROWTH_ACCEL,
    STRATEGY_TURNAROUND,
    STRATEGY_VALUE_SWING,
    default_store,
    expand_strategies,
)
from tradingagents.watchlist.calendar import CN_TZ, is_cn_trading_day

logger = logging.getLogger(__name__)

_STRATEGY_LABELS = {
    STRATEGY_VALUE_SWING: "价值波段",
    STRATEGY_GROWTH_ACCEL: "成长加速",
    STRATEGY_TURNAROUND: "错杀反转",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="策略漏斗扫描（独立进程）")
    p.add_argument(
        "--strategy",
        choices=(STRATEGY_VALUE_SWING, STRATEGY_GROWTH_ACCEL, STRATEGY_TURNAROUND, STRATEGY_BOTH, STRATEGY_ALL),
        default=STRATEGY_BOTH,
        help="both=价值+成长都跑；all=三条策略全跑（默认 both）",
    )
    p.add_argument(
        "--max-candidates",
        type=int,
        default=15,
        help="各策略 L2 候选上限（默认 15）",
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


def _print_one_result(strategy: str, record: dict) -> None:
    label = _STRATEGY_LABELS.get(strategy, strategy)
    result = record.get("result") or {}
    print(
        f"[{label}] 扫描完成 {result.get('scan_date')}: "
        f"{result.get('l0_passed')}→{result.get('l1a_passed')}→"
        f"{result.get('l1b_passed')}→{result.get('l2_passed')} 候选, "
        f"{result.get('duration_seconds', 0):.0f}s"
        + (
            f", 已入队 {record.get('enqueued', 0)} 只"
            if record.get("enqueued")
            else ""
        )
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = build_parser().parse_args(argv)
    strategies = expand_strategies(args.strategy)
    if args.skip_non_trading_day:
        now = datetime.now(CN_TZ)
        if not is_cn_trading_day(now):
            logger.info(
                "非交易日 %s，跳过扫描 strategies=%s",
                now.date(),
                ",".join(strategies),
            )
            print(f"非交易日 {now.date()}，跳过扫描")
            return 0

    failed = 0
    for strategy in strategies:
        label = _STRATEGY_LABELS.get(strategy, strategy)
        logger.info("开始扫描：%s", label)
        store = default_store(strategy)
        record = run_scan_job(
            max_candidates=args.max_candidates,
            enqueue_on_success=args.enqueue,
            scan_store=store,
            strategy=strategy,
        )
        if record.get("status") != SCAN_STATUS_COMPLETED:
            print(f"[{label}] 扫描失败: {record.get('error')}", file=sys.stderr)
            failed += 1
            continue
        _print_one_result(strategy, record)

    if failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
