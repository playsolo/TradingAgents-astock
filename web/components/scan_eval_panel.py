"""Scan backtest panel for strategy scanner Web UI."""

from __future__ import annotations

import streamlit as st

from tradingagents.evaluation.scan_backtest import (
    compare_scan_periods,
    evaluate_scan_history,
)


def _pct(val: float | None, *, signed: bool = False) -> str:
    if val is None:
        return "—"
    if signed:
        return f"{val * 100:+.2f}%"
    return f"{val * 100:.1f}%"


def render_scan_eval_panel(*, strategy: str = "value_swing") -> None:
    """Historical hit-rate / excess-return summary from archived scans."""
    st.markdown("#### 📈 历史命中率（扫描回测）")
    st.caption(
        "基于归档扫描候选，按入选日收盘价测算 5/20 交易日收益；"
        "超额 = 个股收益 − 沪深300(000300)。"
        "样本不足时指标显示为 —。"
    )

    col_a, col_b, col_c = st.columns([1, 1, 1])
    with col_a:
        archive_limit = st.number_input(
            "归档扫描数",
            min_value=5,
            max_value=200,
            value=40,
            step=5,
            key=f"scan_eval_limit_{strategy}",
        )
    with col_b:
        compare_mode = st.checkbox(
            "对比 HiThink 上线前后",
            value=False,
            key=f"scan_eval_compare_{strategy}",
            help="以 Phase 2 默认切分日 2026-08-23 为界；可改下方日期。",
        )
    with col_c:
        run = st.button("刷新回测", key=f"scan_eval_run_{strategy}", type="secondary")

    cutoff = st.text_input(
        "切分日期 (YYYY-MM-DD)",
        value="2026-08-23",
        key=f"scan_eval_cutoff_{strategy}",
        disabled=not compare_mode,
    )

    if not run and f"_scan_eval_report_{strategy}" not in st.session_state:
        st.info("点击「刷新回测」加载归档扫描的后续表现。")
        return

    if run:
        with st.spinner("正在拉取行情并结算…"):
            if compare_mode:
                report = compare_scan_periods(
                    strategy,
                    before_until=cutoff,
                    after_since=cutoff,
                    archive_limit=int(archive_limit),
                )
            else:
                report = evaluate_scan_history(
                    strategy, archive_limit=int(archive_limit)
                )
        st.session_state[f"_scan_eval_report_{strategy}"] = report

    report = st.session_state.get(f"_scan_eval_report_{strategy}")
    if not report:
        return

    if compare_mode and "before" in report:
        delta = report.get("median_excess_20d_delta")
        b = (report.get("before") or {}).get("aggregate", {}).get("20", {})
        a = (report.get("after") or {}).get("aggregate", {}).get("20", {})
        m1, m2, m3 = st.columns(3)
        m1.metric(
            f"上线前 20日超额中位数 (≤{cutoff})",
            _pct(b.get("median_excess"), signed=True),
            help=f"样本 {b.get('n', 0)}",
        )
        m2.metric(
            f"上线后 20日超额中位数 (≥{cutoff})",
            _pct(a.get("median_excess"), signed=True),
            help=f"样本 {a.get('n', 0)}",
        )
        m3.metric("超额中位数变化", _pct(delta, signed=True))
        report = report.get("after") or report

    agg = report.get("aggregate") or {}
    total = report.get("total_candidate_rows") or 0
    scans = report.get("evaluable_scans") or 0
    st.caption(f"可评估扫描 **{scans}** 次 · 候选观测 **{total}** 条")

    if not agg:
        st.warning("归档中暂无可结算样本（可能扫描日过近或行情拉取失败）。")
        return

    hcols = st.columns(len(agg) or 1)
    for i, (h, cell) in enumerate(sorted(agg.items(), key=lambda x: int(x[0]))):
        with hcols[i]:
            st.metric(
                f"{h}日超额中位数",
                _pct(cell.get("median_excess"), signed=True),
                help=f"绝对收益中位数 {_pct(cell.get('median_return'), signed=True)}",
            )
            hit = cell.get("hit_rate")
            cap = f"上涨占比 {_pct(hit)} · n={cell.get('n', 0)}"
            if cell.get("drop_rate") is not None:
                cap += f" · 5日大跌≤{int(-8)}%占比 {_pct(cell.get('drop_rate'))}"
            st.caption(cap)

    ic = report.get("score_excess_ic_20d")
    if ic is not None:
        st.caption(f"信号分 vs 20日超额 Spearman IC：**{ic:+.3f}**")

    by_factor = report.get("by_factor") or {}
    if by_factor:
        st.markdown("**单因子 20日超额（命中 vs 未命中）**")
        rows = []
        for key in sorted(by_factor):
            cell = by_factor[key]
            rows.append(
                {
                    "因子": cell.get("label") or key,
                    "命中n": cell.get("hit_n"),
                    "命中超额": _pct(cell.get("hit_median_excess_20d"), signed=True),
                    "未命中n": cell.get("miss_n"),
                    "未命中超额": _pct(cell.get("miss_median_excess_20d"), signed=True),
                }
            )
        st.dataframe(rows, use_container_width=True, hide_index=True)

    recent = report.get("recent_scans") or []
    if recent:
        with st.expander("最近扫描结算明细", expanded=False):
            st.dataframe(recent, use_container_width=True, hide_index=True)
