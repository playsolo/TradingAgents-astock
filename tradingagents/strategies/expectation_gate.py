"""一致预期质量闸门（窄池 L2）。

仅在 L1 入围后的窄池拉东财一致预期；覆盖度不足不加不减。
价值轨：Forward PE 相对 TTM 的便宜/恶化；成长轨：实际增速 vs 一致隐含增速。
不做全市场扫描；无修订序列时不做「下修一票否决」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MIN_ANALYSTS: int = 3

# 价值：fwd_pe / pe_ttm
_VALUE_CHEAP_RATIO: float = 0.85
_VALUE_WEAK_RATIO: float = 1.15

# 成长：实际 YoY − 一致隐含 CAGR
_GROWTH_GAP_BONUS: float = 0.20
_GROWTH_OVERHANG_IMPLIED: float = 0.30
_GROWTH_OVERHANG_LAG: float = 0.15


@dataclass
class ConsensusSnapshot:
    analysts: int = 0
    fy1_eps: float | None = None
    fy2_eps: float | None = None
    fwd_pe: float | None = None
    implied_cagr: float | None = None
    usable: bool = False
    low_coverage: bool = False


@dataclass
class ExpectationGateResult:
    score_delta: int = 0
    hit: bool = False
    label: str = ""
    reason: str = ""
    low_coverage: bool = False
    fwd_pe: float | None = None
    implied_cagr: float | None = None
    analysts: int = 0


def build_consensus_snapshot(
    records: list[dict[str, Any]],
    *,
    org_count: int = 0,
    price: float = 0.0,
) -> ConsensusSnapshot:
    """从 ``{year, eps, analysts}`` 记录构建快照。"""
    analysts = int(org_count or 0)
    if not analysts and records:
        try:
            analysts = int(records[0].get("analysts") or 0)
        except (TypeError, ValueError):
            analysts = 0

    low_coverage = analysts > 0 and analysts < MIN_ANALYSTS
    by_year: list[tuple[str, float]] = []
    for item in records:
        year = str(item.get("year") or "").strip()
        try:
            eps = float(item.get("eps"))
        except (TypeError, ValueError):
            continue
        if not year:
            continue
        by_year.append((year, eps))
    by_year.sort(key=lambda x: x[0])

    fy1 = by_year[0][1] if by_year else None
    fy2 = by_year[1][1] if len(by_year) >= 2 else None

    fwd_pe: float | None = None
    if price and price > 0 and fy1 is not None and fy1 > 0:
        fwd_pe = price / fy1

    implied: float | None = None
    if fy1 is not None and fy2 is not None and fy1 > 0 and fy2 > 0:
        implied = fy2 / fy1 - 1.0

    usable = (
        analysts >= MIN_ANALYSTS
        and fy1 is not None
        and fy1 > 0
        and fwd_pe is not None
    )
    return ConsensusSnapshot(
        analysts=analysts,
        fy1_eps=fy1,
        fy2_eps=fy2,
        fwd_pe=fwd_pe,
        implied_cagr=implied,
        usable=usable,
        low_coverage=low_coverage or (analysts > 0 and analysts < MIN_ANALYSTS),
    )


def score_value_expectation(
    *,
    pe_ttm: float,
    snap: ConsensusSnapshot,
) -> ExpectationGateResult:
    """价值轨：Forward PE 相对自身 TTM 明显更便宜 → +1；暗示盈利下滑 → −1。"""
    base = ExpectationGateResult(
        low_coverage=snap.low_coverage,
        fwd_pe=snap.fwd_pe,
        implied_cagr=snap.implied_cagr,
        analysts=snap.analysts,
    )
    if snap.low_coverage and not snap.usable:
        base.reason = "覆盖度不足，跳过预期闸门"
        return base
    if not snap.usable or snap.fwd_pe is None:
        base.reason = "无可用一致预期"
        return base
    if pe_ttm is None or pe_ttm <= 0:
        base.reason = "无有效 PE(TTM)"
        return base

    ratio = snap.fwd_pe / pe_ttm
    if ratio <= _VALUE_CHEAP_RATIO:
        return ExpectationGateResult(
            score_delta=1,
            hit=True,
            label="远期估值更便宜",
            reason=f"FwdPE {snap.fwd_pe:.1f} / PE(TTM) {pe_ttm:.1f} = {ratio:.2f}",
            low_coverage=False,
            fwd_pe=snap.fwd_pe,
            implied_cagr=snap.implied_cagr,
            analysts=snap.analysts,
        )
    if ratio >= _VALUE_WEAK_RATIO:
        return ExpectationGateResult(
            score_delta=-1,
            hit=False,
            label="一致预期暗示盈利下滑",
            reason=f"FwdPE {snap.fwd_pe:.1f} / PE(TTM) {pe_ttm:.1f} = {ratio:.2f}",
            low_coverage=False,
            fwd_pe=snap.fwd_pe,
            implied_cagr=snap.implied_cagr,
            analysts=snap.analysts,
        )
    base.reason = "远期估值中性带"
    return base


def score_growth_expectation(
    *,
    actual_yoy: float | None,
    snap: ConsensusSnapshot,
    track: str = "profit",
) -> ExpectationGateResult:
    """成长轨：实际增速显著高于一致隐含 → +1；预期透支 → −1。亏损轨跳过。"""
    base = ExpectationGateResult(
        low_coverage=snap.low_coverage,
        fwd_pe=snap.fwd_pe,
        implied_cagr=snap.implied_cagr,
        analysts=snap.analysts,
    )
    if track != "profit":
        base.reason = "亏损轨跳过预期闸门"
        return base
    if snap.low_coverage and not snap.usable:
        base.reason = "覆盖度不足，跳过预期闸门"
        return base
    if not snap.usable:
        base.reason = "无可用一致预期"
        return base
    if snap.implied_cagr is None or actual_yoy is None:
        base.reason = "缺隐含增速或实际增速"
        return base

    implied = float(snap.implied_cagr)
    actual = float(actual_yoy)
    # 预期透支优先（质量闸门）
    if implied >= _GROWTH_OVERHANG_IMPLIED and actual < implied - _GROWTH_OVERHANG_LAG:
        return ExpectationGateResult(
            score_delta=-1,
            hit=False,
            label="一致预期偏高/实际放缓",
            reason=f"实际 {actual:.0%} vs 隐含 {implied:.0%}",
            low_coverage=False,
            fwd_pe=snap.fwd_pe,
            implied_cagr=snap.implied_cagr,
            analysts=snap.analysts,
        )
    gap = actual - implied
    if gap >= _GROWTH_GAP_BONUS:
        return ExpectationGateResult(
            score_delta=1,
            hit=True,
            label="实际增速高于一致预期",
            reason=f"预期差 {gap:.0%}（实际 {actual:.0%} − 隐含 {implied:.0%}）",
            low_coverage=False,
            fwd_pe=snap.fwd_pe,
            implied_cagr=snap.implied_cagr,
            analysts=snap.analysts,
        )
    base.reason = "预期差中性"
    return base


def fetch_consensus_snapshot(code: str, price: float) -> ConsensusSnapshot:
    """窄池拉东财一致预期（走 ``_em_get`` 限流）。失败返回空快照。"""
    try:
        from tradingagents.dataflows.a_stock import _em_eps_forecast

        records, org = _em_eps_forecast(code)
        return build_consensus_snapshot(records, org_count=org, price=price)
    except Exception:
        return ConsensusSnapshot()


def apply_gate_fields(target: Any, result: ExpectationGateResult) -> None:
    """把闸门结果写入 StockInfo / GrowthStockInfo 字段。"""
    target.exp_score_delta = int(result.score_delta)
    target.exp_hit = bool(result.hit)
    target.exp_label = str(result.label or "")
    target.exp_low_coverage = bool(result.low_coverage)
    target.exp_fwd_pe = result.fwd_pe
    target.exp_implied_cagr = result.implied_cagr
    target.exp_analysts = int(result.analysts or 0)
