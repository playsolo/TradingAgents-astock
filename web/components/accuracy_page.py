"""Web page: direction-hit accuracy tracker (internal tool)."""

from __future__ import annotations

import streamlit as st

from tradingagents.agents.utils.signal_accuracy import get_ledger, run_accuracy_maintenance
from tradingagents.default_config import DEFAULT_CONFIG


def _pct(rate) -> str:
    if rate is None:
        return "—"
    return f"{rate * 100:.1f}%"


def _overlay_admin_config(cfg: dict) -> dict:
    out = dict(cfg)
    try:
        from tradingagents.auth.model_config import load_model_config, model_config_exists

        if model_config_exists():
            admin = load_model_config()
            for k in (
                "llm_provider",
                "deep_think_llm",
                "quick_think_llm",
                "backend_url",
                "max_debate_rounds",
                "max_risk_discuss_rounds",
            ):
                if admin.get(k) is not None:
                    out[k] = admin[k]
    except Exception:
        pass
    return out


def render_accuracy_page() -> None:
    st.markdown("### 📊 信号准确率")
    st.caption(
        "跟踪分析结果的方向命中（做多后涨 / 做空后跌 / 持有落在 ±0.5% 带宽内）。"
        "结算窗口：1 / 5 / 20 个交易日。"
        "生产环境每日 **21:00（北京时间）** 由 systemd timer 自动结算；"
        "也可在此手动「立即结算」，或 CLI：`tradingagents accuracy`。"
    )

    cfg = _overlay_admin_config(dict(DEFAULT_CONFIG))

    cols = st.columns([1, 1, 2])
    if cols[0].button("立即结算", use_container_width=True, type="primary", key="acc_settle"):
        with st.spinner("正在回填历史并拉取行情结算…"):
            result = run_accuracy_maintenance(cfg, sync_memory=True)
            migrated = result.get("migrated") or {}
            n = len(result.get("events") or [])
            extra = ""
            if not migrated.get("skipped"):
                extra = (
                    f"；回填 memory={migrated.get('from_memory', 0)} "
                    f"logs={migrated.get('from_logs', 0)}"
                )
            st.success(f"结算完成：新事件 {n} 条{extra}")
            st.rerun()

    if cols[1].button("强制重新回填", use_container_width=True, key="acc_remigrate"):
        with st.spinner("重新扫描 memory 与历史日志…"):
            result = run_accuracy_maintenance(
                cfg, force_remigrate=True, sync_memory=True
            )
            migrated = result.get("migrated") or {}
            st.success(
                f"回填完成：memory={migrated.get('from_memory', 0)} "
                f"logs={migrated.get('from_logs', 0)}；"
                f"新结算 {len(result.get('events') or [])} 条"
            )
            st.rerun()

    ledger = get_ledger(cfg)
    summary = ledger.summary()
    st.markdown(
        f"样本 **{summary['total_records']}** · "
        f"未结算 horizon 槽 **{summary['pending_horizons']}** · "
        f"ε={summary['eps'] * 100:.1f}%"
    )

    by_h = summary.get("by_horizon") or {}
    metric_cols = st.columns(len(by_h) or 1)
    for i, (h, cell) in enumerate(sorted(by_h.items(), key=lambda x: int(x[0]))):
        with metric_cols[i]:
            st.metric(
                f"{h}日命中率",
                _pct(cell.get("hit_rate")),
                help=f"命中 {cell.get('hits', 0)} / 已结算 {cell.get('settled', 0)}",
            )
            st.caption(f"{cell.get('hits', 0)}/{cell.get('settled', 0)}")

    st.markdown("#### 按方向")
    by_dir = summary.get("by_direction") or {}
    if not by_dir:
        st.info("暂无已结算样本。完成分析并过了对应交易日后点「立即结算」。")
    else:
        rows = []
        for direction, horizons in sorted(by_dir.items()):
            for h, cell in sorted(horizons.items(), key=lambda x: int(x[0])):
                rows.append(
                    {
                        "方向": direction,
                        "窗口": f"{h}d",
                        "命中": cell.get("hits", 0),
                        "已结算": cell.get("settled", 0),
                        "命中率": _pct(cell.get("hit_rate")),
                    }
                )
        st.dataframe(rows, use_container_width=True, hide_index=True)

    st.markdown("#### 按配置指纹")
    by_cfg = summary.get("by_config") or {}
    if by_cfg:
        cfg_rows = []
        for fp, horizons in sorted(by_cfg.items()):
            for h, cell in sorted(horizons.items(), key=lambda x: int(x[0])):
                cfg_rows.append(
                    {
                        "config": fp,
                        "窗口": f"{h}d",
                        "命中": cell.get("hits", 0),
                        "已结算": cell.get("settled", 0),
                        "命中率": _pct(cell.get("hit_rate")),
                    }
                )
        st.dataframe(cfg_rows, use_container_width=True, hide_index=True)

    st.markdown("#### 明细（最近 50）")
    records = list(reversed(ledger.records()))[:50]
    if not records:
        st.caption("账本为空。")
        return
    detail_rows = []
    for rec in records:
        for h in ("1", "5", "20"):
            cell = (rec.get("horizons") or {}).get(h) or {}
            ret = cell.get("return")
            detail_rows.append(
                {
                    "日期": rec.get("trade_date"),
                    "代码": rec.get("ticker"),
                    "评级": rec.get("rating"),
                    "方向": rec.get("direction"),
                    "窗口": f"{h}d",
                    "状态": cell.get("status"),
                    "收益": f"{ret:+.2%}" if isinstance(ret, (int, float)) else "—",
                    "命中": (
                        "✓"
                        if cell.get("hit") is True
                        else ("✗" if cell.get("hit") is False else "—")
                    ),
                    "config": rec.get("config_fingerprint") or "",
                }
            )
    st.dataframe(detail_rows, use_container_width=True, hide_index=True)
