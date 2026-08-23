"""Post-analysis extraction of a structured action plan from PM prose.

After the Portfolio Manager finishes, a second LLM pass reads only the
「最终投资计划 / 操作建议」section (not the bull/bear debate) and returns a
typed ``FinalActionPlan`` for sidebar signal + price levels.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from tradingagents.agents.schemas import FinalActionPlan, PortfolioRating
from tradingagents.agents.utils.structured import bind_structured

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# Authoritative rating labels: the bold **Rating** header (canonical English PM
# markdown) or 最终评级 (Chinese final rating). Distinctive enough that a stray
# "rating: hold" substring in later prose won't be mistaken for the header.
_AUTH_RATING_RE = re.compile(
    r"(?:^|\n)[^\n]*(?:最终评级|\*\*Rating\*\*)\s*\**\s*[：:][^\n]*",
    re.IGNORECASE,
)

# Bold rating-word heading: some PM runs produce a heading like
#   # **Underweight（减持 / 低配）**
#   **减持**
# without a "Rating:" or "评级：" label prefix.  This pattern recognises the
# five-tier vocabulary wrapped in bold at the beginning of a line and ranks it
# as authoritative — below explicit Rating/最终评级 headers but above
# loose bare-word anchors.
_BOLD_RATING_RE = re.compile(
    r"(?:^|\n)(?:#{0,3}\s*)?\*{1,2}\s*("
    + "|".join(sorted(["Buy", "Overweight", "Hold", "Underweight", "Sell",
                       "买入", "增持", "持有", "减持", "卖出",
                       "买进", "中性", "观望", "维持", "清仓",
                       "强烈买入", "强烈卖出"], key=len, reverse=True))
    + r")"
)

# Loose fallback: any bare 评级 / Rating label. Matched per-line (no DOTALL)
# so finditer yields every label and we can take the last.
_RATING_ANCHOR_RE = re.compile(
    r"(?:^|\n)[^\n]*(?:评级|Rating)\s*\**\s*[：:][^\n]*",
    re.IGNORECASE,
)

_SIDEBAR_FROM_RATING = {
    "Buy": "Buy",
    "Overweight": "Overweight",
    "Hold": "Hold",
    "Underweight": "Underweight",
    "Sell": "Sell",
}

_EXTRACT_PROMPT = """\
You extract a structured trading action plan from the FINAL investment-plan \
section below. Use only facts stated in the text.

Rules:
1. **Rating** — must be exactly one of Buy / Overweight / Hold / Underweight / Sell \
    (map 买入→Buy, 增持/超配→Overweight, 持有/观望→Hold, 减持→Underweight, 卖出/清仓→Sell).\
    \n   IMPORTANT: If the text explicitly says "Underweight", "减持", or "减仓" anywhere \
    in the rating section, the rating MUST be Underweight. Never \
    infer Overweight or Buy from a price range that is stated in a \
    "reduce / trim / 减仓 / 反弹离场" context.\n
2. **Price levels** —\
    \n   - reduce_low / reduce_high: extract if the text states a price zone FOR SELLING / \
    REDUCING / 减仓 / 减持 (e.g. "反弹至370-380元进一步减仓" → reduce_low=370, reduce_high=380).\
    \n   - buy_zone_low / buy_zone_high: extract ONLY if the text explicitly says \
    non-holders should BUY / ENTER / 建仓 / 买入 at a price range. If the range is stated \
    in a "减仓 / 卖出 / 反弹离场 / 减持" context, it is NOT a buy zone — leave these null.\
    \n   - reentry_low / reentry_high: extract ONLY if the text describes a future \
    RE-ENTRY / 回补 / 加仓 condition (e.g. "若调整至XX可加仓"). \
    A "反弹减仓" range is NOT a re-entry zone — leave these null.\
    \n   - stop_loss: extract only if numeric and explicitly described as stop-loss / 止损 / 离场线.\
    \n   - watch_support: extract only if named as support / 支撑 level.\n
3. **Do NOT invent prices**. If a level is not explicitly numeric in the text, leave it null.\n
4. **holders_action / non_holders_action**: short phrases capturing the stated guidance.\n
5. **summary**: 1–2 sentences for the operational conclusion.\n
6. **Critical**: When the text says to "减仓" or "减持" at a certain price range, \
    that range goes into reduce_low/reduce_high — NEVER into buy_zone or reentry. \
    The overall rating in such a text is Underweight or Sell, never Overweight or Buy.

TEXT:
{section}
"""


_SECTION_HEADER_LINE_RE = re.compile(
    r"(?:^|\n)(#{0,3}\s*三[、．.][^\n]*(?:投资计划|操作建议|投资决策|操作指令)[^\n]*)",
)


def isolate_action_section(text: str) -> str:
    """Return the final-plan section, dropping bull/bear debate preamble when possible.

    When several ``三、`` headings appear (e.g. an earlier 「投资决策回顾」 recap
    followed by the real 「最终投资计划」), the LAST matching heading wins so the
    debate prose in between is not fed to the extractor.
    """
    cleaned = _THINK_RE.sub("", text or "").strip()
    if not cleaned:
        return ""

    header_matches = list(_SECTION_HEADER_LINE_RE.finditer(cleaned))
    if header_matches:
        return cleaned[header_matches[-1].start() :].lstrip("\n").strip()

    # Prefer an authoritative rating header over a bare-label fallback so the
    # canonical first-line **Rating** is not shadowed by later prose.
    auth = list(_AUTH_RATING_RE.finditer(cleaned))
    if auth:
        return cleaned[auth[-1].start() :].strip()

    # Bold-rating heading: recognize patterns like
    #   # **Underweight（减持 / 低配）** / **减持** / **Sell**
    # that signal the PM's final decision without a "Rating:" label prefix.
    # Prefer the LAST occurrence — the PM's own heading appears after any
    # debate-summary labels.
    bold = list(_BOLD_RATING_RE.finditer(cleaned))
    if bold:
        return cleaned[bold[-1].start() :].strip()

    loose = list(_RATING_ANCHOR_RE.finditer(cleaned))
    if loose:
        return cleaned[loose[-1].start() :].strip()

    return cleaned


def rating_to_sidebar_signal(rating: str | PortfolioRating) -> str:
    """Return the canonical 5-tier rating for sidebar grouping."""
    value = rating.value if isinstance(rating, PortfolioRating) else str(rating or "")
    return _SIDEBAR_FROM_RATING.get(value.strip().capitalize(), "Hold")


# ── Regex fallback: parse PM structured markdown directly (no second LLM) ──

_RATING_RE = re.compile(
    r"(?:\*\*)?Rating(?:\*\*)?\s*[：:]\s*\*?\*?\s*([A-Za-z]+)",
    re.IGNORECASE,
)
_SUMMARY_RE = re.compile(
    r"(?:\*\*)?Executive\s+Summary(?:\*\*)?\s*[：:]\s*(.+?)(?=\n\n|\n\*\*|\Z)",
    re.IGNORECASE | re.DOTALL,
)
# Chinese label fallback: the model may output 评级 in Chinese.
# NOTE: every \s in the original pattern was replaced with [ \t] so that
# newlines are NEVER consumed — otherwise a bare "上调评级：\n  1. xxx买入席位"
# can leak the following line's content into the capture group.
_CN_RATING_RE = re.compile(
    r"(?:评级|投资评级|最终评级|最终裁决)[ \t]*\**[ \t]*[：:][ \t]*\*?\*?[ \t]*([^\n]+)",
)


def fallback_extract_action_plan(final_trade_decision: str) -> dict[str, Any] | None:
    """Regex-parse PM structured markdown when the LLM extraction is unavailable.

    MiniMax M3 with thinking mode rejects ``with_structured_output`` (API
    error: "Thinking mode does not support this tool_choice"), so the
    LLM-based ``extract_action_plan`` silently returns None. This fallback
    reads the PM's own structured markdown (produced by ``render_pm_decision``
    from the ``PortfolioDecision`` schema) without a second API call.
    """
    text = final_trade_decision or ""
    if not text.strip():
        return None

    # Isolate the final plan section first.  Without this, debate-prose labels
    # like "最终评级：Overweight（从Buy降下来）" (bull-summary) shadow the
    # PM's own "评级：Underweight（减持）" at the end of the document because
    # re.search() finds the *first* match.
    section = isolate_action_section(text)

    # 1. Rating: try English label first, then Chinese (within the isolated section)
    rating = None
    m = _RATING_RE.search(section)
    if m:
        rating = m.group(1).strip().capitalize()
    if not rating or rating.lower() not in {"buy", "overweight", "hold", "underweight", "sell"}:
        m = _CN_RATING_RE.search(section)
        if m:
            cn_label = m.group(1).strip()
            # First check if the label itself is an English rating word
            cn_lower = cn_label.lower()
            direct_map = {"buy": "Buy", "overweight": "Overweight", "hold": "Hold",
                          "underweight": "Underweight", "sell": "Sell"}
            # Longest-first to prevent "buy" matching inside "从Buy降下来"
            for en, en_label in sorted(direct_map.items(), key=lambda kv: len(kv[0]), reverse=True):
                if en in cn_lower:
                    rating = en_label
                    break
            if not rating:
                cn_map = {
                    "买入": "Buy", "增持": "Overweight", "超配": "Overweight",
                    "持有": "Hold", "减持": "Underweight", "卖出": "Sell",
                    "观望": "Hold", "中性": "Hold", "标配": "Hold",
                }
                for cn, en in cn_map.items():
                    if cn in cn_label:
                        rating = en
                        break
    if not rating:
        # Final fallback: extract from a bold rating-word heading like
        #   # **Underweight（减持 / 低配）**  /  **减持**  /  **Sell**
        # These appear when the PM uses a heading format rather than
        # a labeled "Rating: X" or "评级：X" line.
        m = _BOLD_RATING_RE.search(section)
        if m:
            raw = m.group(1).strip()
            raw_lower = raw.lower()
            # English first
            for en in ("buy", "overweight", "hold", "underweight", "sell"):
                if en in raw_lower:
                    rating = en.capitalize()
                    break
            if not rating:
                cn_map = {
                    "买入": "Buy", "买进": "Buy", "强烈买入": "Buy",
                    "增持": "Overweight",
                    "持有": "Hold", "中性": "Hold", "观望": "Hold", "维持": "Hold",
                    "减持": "Underweight",
                    "卖出": "Sell", "清仓": "Sell", "强烈卖出": "Sell",
                }
                for cn, en in cn_map.items():
                    if cn in raw:
                        rating = en
                        break
    if not rating:
        return None

    # 2. Summary (1-2 sentence operational conclusion)
    summary = ""
    m = _SUMMARY_RE.search(text)
    if m:
        summary = m.group(1).strip()

    # 3. Best-effort holder/non-holder actions from text
    holders_action = ""
    non_holders_action = ""
    horizon = None

    # Extract time horizon if present
    horizon_m = re.search(
        r"(?:\*\*)?Time\s+Horizon(?:\*\*)?\s*[：:]\s*([^\n]+)",
        text, re.IGNORECASE,
    )
    if horizon_m:
        horizon = horizon_m.group(1).strip()

    # Extract holders/non-holders actions from body text
    # (these come from the Portfolio Manager's prose body, not the schema header)
    holders_m = re.search(
        r"(?:持仓者|已持有者|已持仓).*?[：:]\s*([^\n。]+)",
        text,
    )
    if holders_m:
        holders_action = holders_m.group(1).strip().rstrip("*").strip()

    non_holders_m = re.search(
        r"(?:未持仓者|新建仓位|非持有者).*?[：:]\s*([^\n。]+)",
        text,
    )
    if non_holders_m:
        non_holders_action = non_holders_m.group(1).strip().rstrip("*").strip()

    # 4. Best-effort price levels extraction — use the isolated action
    # section (same as the LLM path) to avoid false positives from debate
    # prose, <think> blocks, and non-price numbers like time horizons.
    from tradingagents.agents.schemas import ActionPlanLevels

    levels = ActionPlanLevels()
    _extract_fallback_price_levels(isolate_action_section(text), levels)

    return {
        "rating": rating,
        "holders_action": holders_action,
        "non_holders_action": non_holders_action,
        "levels": levels.model_dump(mode="json"),
        "horizon": horizon,
        "summary": summary,
    }


# Price patterns for fallback extraction — the second number in a range must
# be followed by 元, punctuation, whitespace, or end-of-string.  This prevents
# false captures of time horizons ("6-12个月"), percentages ("15-20%"),
# quantities ("1.66-1.97亿", "300-500万股"), etc.
_BUY_LOW_RE = re.compile(
    r"(?:买入|建仓|介入|回补)"
    r".*?"
    r"(\d+(?:\.\d+)?)"
    r"\s*[,~\-—至到]\s*"
    r"(\d+(?:\.\d+)?)"
    r"(?:\s*元|(?=[\s。，、；）\)\n]|$))"
)
_REDUCE_LOW_RE = re.compile(
    r"(?:减持|减仓|离场|退出|卖出)"
    r".*?"
    r"(\d+(?:\.\d+)?)"
    r"\s*[,~\-—至到]\s*"
    r"(\d+(?:\.\d+)?)"
    r"(?:\s*元|(?=[\s。，、；）\)\n]|$))"
)
_SINGLE_PRICE_RE = re.compile(r"(?:(?:买入|介入|建仓)[^\d\n]*|低位)[^\d\n]*(\d+(?:\.\d+)?)\s*元")
_STOP_LOSS_RE = re.compile(r"(?:止损|离场位|硬止损|清仓位)[^\d]*(\d+(?:\.\d+)?)\s*元")


def _extract_fallback_price_levels(text: str, levels) -> None:
    """Try to extract price levels from PM prose text."""
    # Buy zone
    m = _BUY_LOW_RE.search(text)
    if m:
        try:
            levels.buy_zone_low = float(m.group(1))
            levels.buy_zone_high = float(m.group(2))
        except (ValueError, TypeError):
            pass

    # Reduce/sell zone
    m = _REDUCE_LOW_RE.search(text)
    if m:
        try:
            levels.reduce_low = float(m.group(1))
            levels.reduce_high = float(m.group(2))
        except (ValueError, TypeError):
            pass

    # Single entry price → buy_zone
    if levels.buy_zone_low is None and levels.buy_zone_high is None:
        m = _SINGLE_PRICE_RE.search(text)
        if m:
            try:
                price = float(m.group(1))
                levels.buy_zone_low = price * 0.95
                levels.buy_zone_high = price * 1.05
            except (ValueError, TypeError):
                pass

    # Stop loss
    m = _STOP_LOSS_RE.search(text)
    if m:
        try:
            levels.stop_loss = float(m.group(1))
        except (ValueError, TypeError):
            pass


def extract_action_plan(llm: Any, final_trade_decision: str) -> Optional[dict[str, Any]]:
    """Run a structured LLM pass on the isolated plan section.

    Returns a JSON-serialisable dict on success, or ``None`` if the provider
    cannot bind structured output / the call fails (caller keeps heuristic fallback).
    """
    section = isolate_action_section(final_trade_decision)
    if not section.strip():
        return None

    structured = bind_structured(llm, FinalActionPlan, "Action Plan Extractor")
    prompt = _EXTRACT_PROMPT.format(section=section)

    if structured is None:
        return None

    try:
        result = structured.invoke(prompt)
    except Exception as exc:
        logger.warning(
            "Action plan extract failed (%s); leaving action_plan unset",
            exc,
        )
        return None

    if not isinstance(result, FinalActionPlan):
        try:
            result = FinalActionPlan.model_validate(result)
        except Exception as exc:
            logger.warning("Action plan validate failed (%s)", exc)
            return None

    return result.model_dump(mode="json")
