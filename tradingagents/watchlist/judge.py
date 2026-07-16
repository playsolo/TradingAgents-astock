"""轻量 LLM：相对基准给出建议立场 / 仓位 / 新重大风险 + 盘面小结与三情景。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from tradingagents.agents.utils.rating import RATINGS_5_TIER, parse_rating
from tradingagents.watchlist.models import (
    LEAN_CHOICES,
    Baseline,
    MarketSnapshot,
    ObservationBriefing,
    ScenarioOutlook,
)

logger = logging.getLogger(__name__)

_SCENARIO_KEYS = ("optimistic", "neutral", "pessimistic")
_MAX_BRIEF = 240
_MAX_SHORT = 120

# 有实时个股资金时，板块「今日净流入/出」旧闻会污染模型，从 prompt 里拿掉。
_STALE_FUND_HEADLINE = re.compile(
    r"(净流[入出]|主力资金.{0,8}撤离|资金今日撤离|(?:板块|行业).{0,12}资金)"
)
_GENERIC_STOCK_OUTFLOW_LIST = re.compile(
    r"(?:^|[，、；。\n])[^，、；。\n]*?(?:净流出榜|流出前列|大额净流出)[^，、；。\n]*"
)
_GENERIC_STOCK_INFLOW_LIST = re.compile(
    r"(?:^|[，、；。\n])[^，、；。\n]*?(?:净流入榜|流入前列|大额净流入)[^，、；。\n]*"
)
_SECTOR_CONTEXT = re.compile(r"(板块|行业|北向|沪股通|深股通|沪深港通)")


def _stock_flow_claim_patterns(name: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    safe = re.escape((name or "").strip()) or "____"
    outflow = re.compile(
        rf"(?:^|[，、；。\n])[^，、；。\n]*?(?:个股|该股|本公司|本票|标的|{safe}|\d{{6}})"
        rf"[^，、；。\n]*?(?:净流出|资金流出)[^，、；。\n]*"
    )
    inflow = re.compile(
        rf"(?:^|[，、；。\n])[^，、；。\n]*?(?:个股|该股|本公司|本票|标的|{safe}|\d{{6}})"
        rf"[^，、；。\n]*?(?:净流入|资金流入)[^，、；。\n]*"
    )
    return outflow, inflow


def _drop_unanchored_main_flow_clauses(
    text: str, *, drop_outflow: bool
) -> str:
    """删掉未写明板块/北向的「主力净流入/出」短句，避免与个股快照对着干。"""
    target = "主力净流出" if drop_outflow else "主力净流入"
    kept: list[str] = []
    # 按标点切分，保留分隔符
    parts = re.split(r"([，、；。\n]+)", text)
    i = 0
    while i < len(parts):
        clause = parts[i]
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        i += 2
        if (
            target in clause
            and not _SECTOR_CONTEXT.search(clause)
            and not re.search(r"(?:个股|该股|本公司|本票|标的|\d{6})", clause)
        ):
            continue
        kept.append(clause + sep)
    return "".join(kept)


def _canonical_fund_flow_zh(snapshot: MarketSnapshot) -> str | None:
    if snapshot.main_net_inflow is None:
        return None
    wan = snapshot.main_net_inflow / 1e4
    if snapshot.main_net_inflow > 0:
        return f"个股主力净流入{wan:.0f}万元（实时）"
    if snapshot.main_net_inflow < 0:
        return f"个股主力净流出{abs(wan):.0f}万元（实时）"
    return "个股主力资金持平（实时）"


def _headlines_for_prompt(
    headlines: list[str], *, has_realtime_flow: bool
) -> list[str]:
    if not has_realtime_flow:
        return list(headlines)
    return [h for h in headlines if not _STALE_FUND_HEADLINE.search(h)]


def _strip_contradictory_stock_flow_claims(
    text: str, *, main_net_inflow: float, name: str
) -> str:
    cleaned = text
    outflow_pat, inflow_pat = _stock_flow_claim_patterns(name)
    if main_net_inflow > 0:
        cleaned = outflow_pat.sub("", cleaned)
        cleaned = _GENERIC_STOCK_OUTFLOW_LIST.sub("", cleaned)
        cleaned = _drop_unanchored_main_flow_clauses(cleaned, drop_outflow=True)
    elif main_net_inflow < 0:
        cleaned = inflow_pat.sub("", cleaned)
        cleaned = _GENERIC_STOCK_INFLOW_LIST.sub("", cleaned)
        cleaned = _drop_unanchored_main_flow_clauses(cleaned, drop_outflow=False)
    cleaned = re.sub(r"[，、]{2,}", "，", cleaned)
    cleaned = re.sub(r"[。；]{2,}", "。", cleaned)
    cleaned = re.sub(r"^[，、；。]+|[，、；。]+$", "", cleaned)
    cleaned = cleaned.strip(" ，、；")
    return cleaned if cleaned else ""


def _enforce_fund_flow_truth(
    text: str,
    snapshot: MarketSnapshot,
    *,
    require_fact: bool = False,
) -> str:
    """把模型写成的个股资金方向硬对齐到快照；盘面/摘要必要时前置标准句。"""
    if snapshot.main_net_inflow is None:
        return text
    if not text:
        return text
    fact = _canonical_fund_flow_zh(snapshot)
    if not fact:
        return text
    cleaned = _strip_contradictory_stock_flow_claims(
        text,
        main_net_inflow=snapshot.main_net_inflow,
        name=snapshot.name,
    )
    if require_fact:
        if not cleaned:
            return fact
        if fact not in cleaned and "个股主力净" not in cleaned:
            return f"{fact}。{cleaned}"
        return cleaned
    # lean_reason / scenario：只删矛盾，整句被删光时用标准事实填回
    if not cleaned:
        return fact
    return cleaned


def judge_vs_baseline(
    baseline: Baseline,
    snapshot: MarketSnapshot,
    *,
    llm: Any,
    as_of: str | None = None,
) -> dict[str, Any]:
    """返回结构化判断；LLM 失败时回退为维持基准 + 确定性盘面 briefing。"""
    fallback = _fallback_judgment(baseline, snapshot)
    if llm is None:
        return fallback

    prompt_headlines = _headlines_for_prompt(
        snapshot.headlines,
        has_realtime_flow=snapshot.main_net_inflow is not None,
    )
    headlines = "\n".join(f"- {h}" for h in prompt_headlines) or "- （无近期标题）"
    risks = "、".join(baseline.major_risks) or "（无）"
    turnover = (
        f"{snapshot.turnover_pct}"
        if snapshot.turnover_pct is not None
        else "未知"
    )
    is_us = (baseline.market or "CN") == "US"
    if snapshot.main_net_inflow is None:
        fund_flow_line = "未知（美股无此项）" if is_us else "未知（无可靠实时数据）"
    else:
        wan = snapshot.main_net_inflow / 1e4
        direction = "净流入" if snapshot.main_net_inflow > 0 else (
            "净流出" if snapshot.main_net_inflow < 0 else "持平"
        )
        fund_flow_line = f"主力{direction} {wan:.0f} 万元（以快照为准）"
    as_of_line = (as_of or "").strip() or "未知"
    market_label = "美股" if is_us else "A 股"
    fund_rules = (
        "- 美股无主力净流入字段；market_brief / summary 聚焦价量与消息面，勿编造资金流向。\n"
        if is_us
        else (
            "- 个股资金流向必须以快照数值为准；标题里的板块资金新闻可能过时，标题中的「今日」不可信。\n"
            "- 快照资金为未知时，不得编造个股净流入或净流出。\n"
            "- market_brief / summary 须点明快照中的个股主力净流入或净流出金额，禁止与快照方向相反。\n"
        )
    )
    risk_examples = (
        "财报爆雷/监管诉讼/大额减持/指引大幅下修"
        if is_us
        else "立案/业绩变脸/减持暴增/解禁冲击"
    )
    prompt = f"""你是 {market_label}交易员的轻量盯盘助手。只做相对「基准分析」的增量判断，不要重写完整研报。

基准（{baseline.trade_date}）：
- 股票: {baseline.ticker} {snapshot.name}
- 立场: {baseline.stance}
- 建议仓位: {baseline.position_pct if baseline.position_pct is not None else "未知"}%
- 基准价: {baseline.baseline_price}
- 止损: {baseline.stop_loss}
- 逻辑摘要: {baseline.thesis_summary}
- 已知重大风险: {risks}

当前快照（观察日 {as_of_line}）：
- 现价: {snapshot.price}（涨跌 {snapshot.change_pct}%）
- 换手: {turnover}
- 个股主力资金: {fund_flow_line}
- 近期标题:
{headlines}

请严格输出一个 JSON 对象（不要 markdown 围栏），字段：
{{
  "suggested_stance": "Buy|Overweight|Hold|Underweight|Sell 之一",
  "suggested_position_pct": 数字或 null,
  "new_major_risks": ["相对基准新增的重大风险标签，无则 []"],
  "summary": "不超过120字的中文结论",
  "watch_point": "今日只看一点",
  "avoid": "不建议做什么",
  "market_brief": "不超过120字的当日盘面小结（价量、相对基准、消息面要点）",
  "lean": "optimistic|neutral|pessimistic 三选一，表示今日主倾向",
  "lean_reason": "不超过80字，说明为何倾向该情景",
  "scenarios": {{
    "optimistic": {{"view": "乐观情景一句话", "reason": "理由一句话"}},
    "neutral": {{"view": "中性情景一句话", "reason": "理由一句话"}},
    "pessimistic": {{"view": "悲观情景一句话", "reason": "理由一句话"}}
  }}
}}
规则：
- 没有实质变化时立场与仓位应接近基准；new_major_risks 仅填高影响事件（{risk_examples}等），不要把日常波动当重大风险。
- lean 必须是 optimistic / neutral / pessimistic 之一；无明确方向时用 neutral。
- 三个 scenarios 都要给，即便今日倾向明确，也要写清另外两种情景的触发条件式理由。
- 「今日/当日」均相对观察日 {as_of_line}，不要与基准日混淆。
{fund_rules}"""
    try:
        resp = llm.invoke(prompt)
        text = _content_to_text(resp)
        data = _parse_json_object(text)
        stance = parse_rating(
            str(data.get("suggested_stance") or baseline.stance),
            default=baseline.stance,
        )
        if stance not in RATINGS_5_TIER:
            stance = baseline.stance
        pos = data.get("suggested_position_pct")
        if pos is not None:
            try:
                pos = float(pos)
            except (TypeError, ValueError):
                pos = baseline.position_pct
        risks_out = data.get("new_major_risks") or []
        if not isinstance(risks_out, list):
            risks_out = []
        briefing = _normalize_briefing_fields(data, snapshot)
        summary = _enforce_fund_flow_truth(
            str(data.get("summary") or "")[:_MAX_BRIEF],
            snapshot,
            require_fact=True,
        )
        lean_reason = _enforce_fund_flow_truth(
            str(briefing.get("lean_reason") or ""),
            snapshot,
            require_fact=False,
        )
        return {
            "suggested_stance": stance,
            "suggested_position_pct": pos,
            "new_major_risks": [str(r).strip() for r in risks_out if str(r).strip()],
            "summary": summary[:_MAX_BRIEF],
            "watch_point": str(data.get("watch_point") or "")[:_MAX_SHORT],
            "avoid": str(data.get("avoid") or "")[:_MAX_SHORT],
            **briefing,
            "lean_reason": (lean_reason or briefing["lean_reason"])[:_MAX_SHORT],
        }
    except Exception as e:
        logger.warning("watchlist judge failed for %s: %s", baseline.ticker, e)
        return fallback


def briefing_from_judgment(judgment: dict[str, Any]) -> ObservationBriefing:
    """将 judge 返回的 dict 转为 ObservationBriefing。"""
    scenarios_raw = judgment.get("scenarios") or {}
    scenarios = {}
    for key in _SCENARIO_KEYS:
        raw = scenarios_raw.get(key) if isinstance(scenarios_raw, dict) else None
        if isinstance(raw, dict):
            scenarios[key] = ScenarioOutlook(
                view=str(raw.get("view") or "")[:_MAX_SHORT],
                reason=str(raw.get("reason") or "")[:_MAX_SHORT],
            )
        else:
            scenarios[key] = ScenarioOutlook(view="", reason="")
    lean = str(judgment.get("lean") or "neutral").strip().lower()
    if lean not in LEAN_CHOICES:
        lean = "neutral"
    return ObservationBriefing(
        market_brief=str(judgment.get("market_brief") or "")[:_MAX_BRIEF],
        lean=lean,
        lean_reason=str(judgment.get("lean_reason") or "")[:_MAX_SHORT],
        scenarios=scenarios,
    )


def _fallback_judgment(baseline: Baseline, snapshot: MarketSnapshot) -> dict[str, Any]:
    brief_parts = [
        f"现价 {snapshot.price:g}（{snapshot.change_pct:+.2f}%）",
    ]
    fact = _canonical_fund_flow_zh(snapshot)
    if fact:
        brief_parts.append(fact)
    if snapshot.turnover_pct is not None:
        brief_parts.append(f"换手 {snapshot.turnover_pct}")
    if baseline.baseline_price:
        delta = (snapshot.price - baseline.baseline_price) / baseline.baseline_price * 100
        brief_parts.append(f"相对基准 {delta:+.2f}%")
    brief_parts.append("轻量复核跳过（模型不可用），仅做价格等确定性检查。")
    market_brief = "；".join(brief_parts)
    return {
        "suggested_stance": baseline.stance,
        "suggested_position_pct": baseline.position_pct,
        "new_major_risks": [],
        "summary": "轻量复核跳过（模型不可用），仅做价格等确定性检查。",
        "watch_point": "",
        "avoid": "",
        "market_brief": market_brief,
        "lean": "neutral",
        "lean_reason": "模型不可用，默认中性。",
        "scenarios": {
            "optimistic": {
                "view": "价格回稳并修复相对基准的折价",
                "reason": "需等待模型可用后细化；现仅保留占位情景。",
            },
            "neutral": {
                "view": "围绕现价窄幅震荡",
                "reason": "缺乏模型判断，以观望为主。",
            },
            "pessimistic": {
                "view": "继续偏离基准并扩大回撤",
                "reason": "需结合后续行情与模型复核确认。",
            },
        },
    }


def _normalize_briefing_fields(data: dict[str, Any], snapshot: MarketSnapshot) -> dict[str, Any]:
    lean = str(data.get("lean") or "neutral").strip().lower()
    if lean not in LEAN_CHOICES:
        lean = "neutral"
    lean_reason = str(data.get("lean_reason") or "")[:_MAX_SHORT]
    market_brief = str(data.get("market_brief") or "").strip()
    if not market_brief:
        market_brief = (
            f"现价 {snapshot.price:g}（{snapshot.change_pct:+.2f}%）"
            f"{'；换手 ' + str(snapshot.turnover_pct) if snapshot.turnover_pct is not None else ''}。"
        )
    market_brief = _enforce_fund_flow_truth(
        market_brief, snapshot, require_fact=True
    )
    lean_reason = _enforce_fund_flow_truth(
        lean_reason, snapshot, require_fact=False
    )
    scenarios_raw = data.get("scenarios") or {}
    scenarios: dict[str, dict[str, str]] = {}
    if not isinstance(scenarios_raw, dict):
        scenarios_raw = {}
    for key in _SCENARIO_KEYS:
        raw = scenarios_raw.get(key) or {}
        if not isinstance(raw, dict):
            raw = {}
        view = str(raw.get("view") or "").strip()[:_MAX_SHORT]
        reason = _enforce_fund_flow_truth(
            str(raw.get("reason") or "").strip(),
            snapshot,
            require_fact=False,
        )[:_MAX_SHORT]
        if not view:
            view = {"optimistic": "偏多演绎", "neutral": "震荡整理", "pessimistic": "偏空演绎"}[
                key
            ]
        if not reason:
            reason = "模型未给出明确理由，需结合盘面继续观察。"
        scenarios[key] = {"view": view, "reason": reason}
    return {
        "market_brief": market_brief[:_MAX_BRIEF],
        "lean": lean,
        "lean_reason": lean_reason or "未给出倾向理由。",
        "scenarios": scenarios,
    }


def _content_to_text(resp: Any) -> str:
    content = getattr(resp, "content", resp)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # 只剥围栏，不要用非贪婪 \{.*?\}（会在 nested scenarios 的第一个 } 截断）
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object in model response")
    return json.loads(cleaned[start : end + 1])
