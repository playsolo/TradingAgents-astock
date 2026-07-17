"""确定性变更检测（立场 / 仓位 / 价格 / 止损 / 新风险）。"""

from __future__ import annotations

from datetime import datetime

from tradingagents.watchlist.models import Alert, Baseline, MarketSnapshot

# 与产品确认一致
DEFAULT_POSITION_THRESHOLD_PP = 5.0
DEFAULT_PRICE_THRESHOLD_PCT = 5.0


def detect_changes(
    baseline: Baseline,
    snapshot: MarketSnapshot,
    *,
    suggested_stance: str,
    suggested_position_pct: float | None,
    new_major_risks: list[str],
    position_threshold_pp: float = DEFAULT_POSITION_THRESHOLD_PP,
    price_threshold_pct: float = DEFAULT_PRICE_THRESHOLD_PCT,
    observed_at: str | None = None,
) -> list[Alert]:
    """相对基准检测需上报的变化。无变化返回空列表。"""
    now = observed_at or datetime.now().isoformat(timespec="seconds")
    alerts: list[Alert] = []

    sug = (suggested_stance or baseline.stance).strip()
    if sug.lower() != baseline.stance.lower():
        alerts.append(
            Alert(
                kind="stance",
                title="立场变化",
                detail=f"{baseline.stance} → {sug}",
                observed_at=now,
            )
        )

    if (
        baseline.position_pct is not None
        and suggested_position_pct is not None
        and abs(suggested_position_pct - baseline.position_pct) > position_threshold_pp
    ):
        alerts.append(
            Alert(
                kind="position",
                title="建议仓位变动",
                detail=(
                    f"{baseline.position_pct:g}% → {suggested_position_pct:g}% "
                    f"（阈值 {position_threshold_pp:g} 个百分点）"
                ),
                observed_at=now,
            )
        )

    if baseline.baseline_price and baseline.baseline_price > 0 and snapshot.price > 0:
        deviation = abs(snapshot.price - baseline.baseline_price) / baseline.baseline_price * 100
        if deviation > price_threshold_pct:
            direction = "上涨" if snapshot.price > baseline.baseline_price else "下跌"
            alerts.append(
                Alert(
                    kind="price",
                    title="价格偏离基准",
                    detail=(
                        f"基准 {baseline.baseline_price:g} → 现价 {snapshot.price:g} "
                        f"（{direction} {deviation:.1f}%，阈值 {price_threshold_pct:g}%）"
                    ),
                    observed_at=now,
                )
            )

    if baseline.stop_loss is not None and snapshot.price > 0 and snapshot.price < baseline.stop_loss:
        alerts.append(
            Alert(
                kind="stop_loss",
                title="跌破基准止损",
                detail=f"现价 {snapshot.price:g} < 止损 {baseline.stop_loss:g}",
                observed_at=now,
            )
        )

    if baseline.entry_price is not None and snapshot.price > 0 and snapshot.price <= baseline.entry_price:
        alerts.append(
            Alert(
                kind="entry",
                title="触及入场价",
                detail=(
                    f"现价 {snapshot.price:g} ≤ 入场价 {baseline.entry_price:g}，"
                    "可复核是否执行买入"
                ),
                observed_at=now,
            )
        )

    known = {r.strip() for r in baseline.major_risks if r and r.strip()}
    fresh = [r.strip() for r in new_major_risks if r and r.strip() and r.strip() not in known]
    if fresh:
        alerts.append(
            Alert(
                kind="risk",
                title="新增重大风险",
                detail="；".join(fresh),
                observed_at=now,
            )
        )

    return alerts
