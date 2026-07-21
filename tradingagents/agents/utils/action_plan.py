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

# Loose fallback: any bare 评级 / Rating label. Matched per-line (no DOTALL)
# so finditer yields every label and we can take the last.
_RATING_ANCHOR_RE = re.compile(
    r"(?:^|\n)[^\n]*(?:评级|Rating)\s*\**\s*[：:][^\n]*",
    re.IGNORECASE,
)

_SIDEBAR_FROM_RATING = {
    "Buy": "Buy",
    "Overweight": "Buy",
    "Hold": "Hold",
    "Underweight": "Sell",
    "Sell": "Sell",
}

_EXTRACT_PROMPT = """\
You extract a structured trading action plan from the FINAL investment-plan \
section below. Use only facts stated in the text.

Rules:
- rating must be one of Buy / Overweight / Hold / Underweight / Sell \
    (map 买入→Buy, 增持/超配→Overweight, 持有/观望→Hold, 减持→Underweight, 卖出/清仓→Sell).
- Do NOT invent prices. If a level is not explicitly numeric in the text, leave it null.
- holders_action / non_holders_action: short phrases capturing the stated guidance.
- summary: 1–2 sentences for the operational conclusion.
- buy_zone_low / buy_zone_high: if the text states a price range where \
    non-holders should re-enter / buy (e.g. "回调至12-14元可分批建仓" or \
    "re-entry zone 12-15"), extract the numeric bounds. Otherwise leave null.

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

    loose = list(_RATING_ANCHOR_RE.finditer(cleaned))
    if loose:
        return cleaned[loose[-1].start() :].strip()

    return cleaned


def rating_to_sidebar_signal(rating: str | PortfolioRating) -> str:
    """Collapse 5-tier rating to the Buy / Sell / Hold sidebar buckets."""
    value = rating.value if isinstance(rating, PortfolioRating) else str(rating or "")
    return _SIDEBAR_FROM_RATING.get(value.strip().capitalize(), "Hold")


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
