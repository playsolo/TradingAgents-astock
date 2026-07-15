"""独立后台分析 worker。

Web 只负责把 :class:`~web.analysis_queue.AnalysisJob` 写入磁盘队列，真正的
多 Agent 分析由本包的守护进程 ``tradingagents-analyze`` 消费执行。这样关闭
浏览器后队列仍会被耗尽（并发上限 ``TRADINGAGENTS_MAX_PARALLEL``）。
"""

from tradingagents.analyze_worker.mode import (
    analysis_executor,
    is_worker_mode,
)

__all__ = ["analysis_executor", "is_worker_mode"]
