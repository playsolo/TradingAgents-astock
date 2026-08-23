"""Shared 5-tier rating vocabulary and a deterministic heuristic parser.

The same five-tier scale (Buy, Overweight, Hold, Underweight, Sell) is used by:
- The Research Manager (investment plan recommendation)
- The Portfolio Manager (final position decision)
- The signal processor (rating extracted for downstream consumers)
- The memory log (rating tag stored alongside each decision entry)

Centralising it here avoids drift between those call sites.

Chinese output: when ``output_language`` is Chinese and a weak model or an
OpenAI-compatible relay falls back from structured output to free-text (see
:func:`tradingagents.agents.utils.structured.invoke_structured_or_freetext`),
the final decision arrives as Chinese prose with **no** English ``Rating:``
header — just a line like ``最终评级：卖出``. The parser therefore also
recognises the Chinese 5-tier vocabulary; without it, every such run silently
defaulted to Hold regardless of the model's actual call (issues #78 / #80).
"""

from __future__ import annotations

import re
from typing import Tuple


# Canonical, ordered 5-tier scale (most bullish to most bearish).
RATINGS_5_TIER: Tuple[str, ...] = (
    "Buy", "Overweight", "Hold", "Underweight", "Sell",
)

_RATING_SET = {r.lower() for r in RATINGS_5_TIER}

# Matches "Rating: X" / "rating - X" / "Rating: **X**" — tolerates markdown
# bold wrappers and either a colon or hyphen separator.
_RATING_LABEL_RE = re.compile(r"rating.*?[:\-][\s*]*(\w+)", re.IGNORECASE)

# Chinese 5-tier vocabulary → canonical English rating.
_CN_RATING_MAP = {
    "强烈买入": "Buy", "买入": "Buy", "买进": "Buy",
    "增持": "Overweight",
    "持有": "Hold", "中性": "Hold", "观望": "Hold", "维持": "Hold",
    "减持": "Underweight",
    "强烈卖出": "Sell", "清仓": "Sell", "卖出": "Sell",
}
# Longest-first so "强烈买入" beats "买入" and "强烈卖出" beats "卖出" at the
# same position (regex alternation is leftmost, first-listed among equals).
_CN_ALT = "|".join(sorted(_CN_RATING_MAP, key=len, reverse=True))

# A labelled Chinese rating, e.g. "最终评级：卖出" / "投资建议: **增持**".
_CN_LABEL_RE = re.compile(
    r"(?:最终评级|评级|投资评级|评级结论|最终投资建议|投资建议|操作建议|"
    r"推荐评级|建议|推荐)\s*[:：\-]\s*\*{0,2}\s*(" + _CN_ALT + r")"
)
# Bare Chinese rating term anywhere (last-resort fallback).
_CN_TERM_RE = re.compile(_CN_ALT)

# Negation guard for step-4 bare-keyword fallback: a negated buy/sell term
# (e.g. "不宜买入", "不建议买入", "并非买入信号", "不是卖出时机") must NOT
# be counted as a rating signal.  Check the ~8 characters immediately preceding
# the matched rating term for any negation word (longest-first so "不宜" beats "不").
_NEGATION_WORDS = re.compile(
    "|".join(sorted([
        "不宜", "不可", "不应", "不建议", "无需", "不是", "并非", "未有", "无法",
        "切勿", "切忌", "杜绝", "严禁", "禁止", "避免", "不", "无", "勿", "忌",
    ], key=len, reverse=True))
)


def parse_rating(text: str, default: str = "Hold") -> str:
    """Heuristically extract a 5-tier rating from English or Chinese prose.

    Pass order (first hit wins; explicit labels always beat bare words):
    1. English ``Rating: X`` label (tolerant of markdown bold).
    2. Chinese rating label, e.g. ``最终评级：卖出`` / ``投资建议: 增持``.
    3. First bare English 5-tier word found anywhere.
    4. First bare Chinese rating term found anywhere (longest match wins).

    Returns a Title-cased canonical rating, or ``default`` if none appears.
    """
    # 1. English explicit label
    for line in text.splitlines():
        m = _RATING_LABEL_RE.search(line)
        if m and m.group(1).lower() in _RATING_SET:
            return m.group(1).capitalize()

    # 2. Chinese explicit label (最终评级：卖出 …)
    m = _CN_LABEL_RE.search(text)
    if m:
        return _CN_RATING_MAP[m.group(1)]

    # 3. Bare English rating word — extract the first alphabetic run from
    #    each token so "**Underweight（减持" and "Underweight，不加仓" are
    #    both recognised as "underweight".
    for line in text.splitlines():
        for word in line.lower().split():
            m = re.match(r"^[^a-z]*([a-z]+)", word)
            if m and m.group(1) in _RATING_SET:
                return m.group(1).capitalize()

    # 4. Bare Chinese rating term (last resort; leftmost, longest at that spot,
    #    but skip negated matches like "不宜买入" or "不是卖出信号").
    #    Check the ~8 chars immediately before the match for negation words.
    for m in _CN_TERM_RE.finditer(text):
        window_start = max(0, m.start() - 8)
        window = text[window_start : m.start()]
        if _NEGATION_WORDS.search(window):
            continue
        return _CN_RATING_MAP[m.group(0)]

    return default
