"""灰区软判：价量偏离落在软/硬阈值之间时，用 quick LLM 投票。

不确定、解析失败、无 LLM → 一律建议 full_reeval（偏风控）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from tradingagents.watchlist.models import Baseline

logger = logging.getLogger(__name__)

VOTE_FULL = "full_reeval"
VOTE_REUSE = "reuse"
VOTE_UNCERTAIN = "uncertain"

_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


@dataclass(frozen=True)
class GrayZoneVote:
    decision: str  # full_reeval | reuse | uncertain
    reason: str = ""
    confidence: float = 0.0

    @property
    def prefers_full(self) -> bool:
        return self.decision != VOTE_REUSE or self.confidence < 0.6


def _extract_json(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    m = _JSON_RE.search(raw)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def parse_gray_zone_response(text: str) -> GrayZoneVote:
    """Parse LLM JSON; anything unclear → uncertain (caller maps to full)."""
    data = _extract_json(text)
    if not data:
        return GrayZoneVote(decision=VOTE_UNCERTAIN, reason="parse_failed")
    raw = str(data.get("decision") or data.get("vote") or "").strip().lower()
    if raw in {"full", "full_reeval", "reeval", "全量", "重新评估"}:
        decision = VOTE_FULL
    elif raw in {"reuse", "skip", "incremental", "伪增量", "沿用", "跳过"}:
        decision = VOTE_REUSE
    else:
        decision = VOTE_UNCERTAIN
    try:
        confidence = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(data.get("reason") or data.get("rationale") or "").strip()[:200]
    return GrayZoneVote(decision=decision, reason=reason, confidence=confidence)


def ask_gray_zone_llm(
    *,
    llm: Any,
    anchor: Baseline,
    current_price: float,
    move_pct: float,
    headlines: list[str] | None = None,
) -> GrayZoneVote:
    """Ask quick LLM whether thesis still holds enough to reuse calibration."""
    if llm is None:
        return GrayZoneVote(decision=VOTE_UNCERTAIN, reason="no_llm")

    heads = [str(h).strip() for h in (headlines or []) if str(h).strip()][:6]
    head_block = "\n".join(f"- {h}" for h in heads) if heads else "- （无标题）"
    prompt = f"""你是风控路由助手。根据校准锚点与今日盘面，判断是否需要「全量重新评估」。

校准锚点：
- 代码 {anchor.ticker} 分析日 {anchor.trade_date} 立场 {anchor.stance}
- 基准价 {anchor.baseline_price} 现价 {current_price:g} 偏离 {move_pct:.2f}%
- 止损 {anchor.stop_loss}
- 原逻辑：{anchor.thesis_summary or "（无）"}
- 原风险：{"；".join(anchor.major_risks) if anchor.major_risks else "（无）"}

近期标题：
{head_block}

规则：
- 若原逻辑可能被削弱、催化失效、或需翻盘式重估 → decision=full_reeval
- 仅价量正常波动、逻辑仍成立 → decision=reuse
- 说不清 → decision=uncertain
- 宁可全量，不要漏掉该翻盘

只输出 JSON：{{"decision":"full_reeval|reuse|uncertain","confidence":0.0到1.0,"reason":"一句话"}}"""

    try:
        resp = llm.invoke(prompt)
        text = getattr(resp, "content", None) or str(resp)
        return parse_gray_zone_response(str(text))
    except Exception:  # noqa: BLE001
        logger.warning("gray-zone LLM failed for %s", anchor.ticker, exc_info=True)
        return GrayZoneVote(decision=VOTE_UNCERTAIN, reason="llm_error")
