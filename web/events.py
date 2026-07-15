"""Web 侧事件发射适配：从 ProgressTracker 终态汇总后写入 inbox。"""

from __future__ import annotations

from tradingagents.inbox import (
    emit_analysis_complete,
    emit_analysis_failed,
)
from web.progress import PIPELINE_STAGES, ProgressTracker

_STAGE_ID_TO_NAME = {s["id"]: s["name"] for s in PIPELINE_STAGES}


def _missing_data_warnings(stage_reports: dict[str, str] | None) -> list[str]:
    """从已渲染的分阶段报告里挑出仍带「数据缺失」硬标记的阶段名。

    用 runner 铺平后的 ``stage_reports``（stage_id → 文本），天然覆盖
    market/news/fundamentals 等字符串阶段以及 debate/risk 这类原始结构为 dict、
    但渲染文本已存入 stage_reports 的阶段。
    """
    if not stage_reports:
        return []
    try:
        from web.report_repair import has_hard_missing_data
    except Exception:
        return []

    labels: list[str] = []
    for stage_id, text in stage_reports.items():
        if isinstance(text, str) and has_hard_missing_data(text):
            labels.append(_STAGE_ID_TO_NAME.get(stage_id, stage_id))
    return labels


def notify_tracker_terminal(tracker: ProgressTracker) -> None:
    """Emit an inbox event for a finished or failed run.

    完成优先于错误：``mark_complete`` 之后的清理若抛错会触发 ``mark_error``，
    此时 ``is_complete`` 仍为真——报告已产出，应记为成功而非失败。
    """
    ticker = (tracker.ticker or "").strip()
    trade_date = (tracker.trade_date or "").strip()
    if not ticker or not trade_date:
        return
    if tracker.is_complete:
        warnings = _missing_data_warnings(tracker.stage_reports)
        emit_analysis_complete(
            ticker,
            trade_date,
            tracker.signal or "N/A",
            warnings=warnings,
        )
        return
    if tracker.error:
        emit_analysis_failed(ticker, trade_date, tracker.error)
