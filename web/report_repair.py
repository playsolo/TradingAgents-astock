"""Retry missing data sources and regenerate individual analyst report sections."""

from __future__ import annotations

import json
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

MISSING_DATA_MARKER = "[数据缺失"

# Cap tool↔LLM rounds for a single-section regenerate (matches typical analyst depth).
MAX_ANALYST_TOOL_ROUNDS = 8

# LLM often copies soft/partial tool gaps as report-level [数据缺失], which keeps the
# repair banner up even after probes succeed. Scrub these when classifying hard gaps
# and after a successful probe regen.
_SOFT_MISSING_PATTERNS = (
    re.compile(
        r"\[数据缺失[：:][^\]]*(?:未检索到|未发现|未获取到|未披露|未返回|"
        r"无公开|暂无|无记录|无变动|无明显)[^\]]*\]"
    ),
    re.compile(
        r"\[数据缺失[：:][^\]]*(?:无分析师|无机构覆盖|无覆盖|一致预期|"
        r"profit_forecast|No analyst)[^\]]*\]"
    ),
    re.compile(
        r"\[数据缺失[：:][^\]]*(?:股权质押|关联交易|商誉|各高管|持股数|"
        r"工具未提供|不在工具)[^\]]*\]"
    ),
    re.compile(r"\[数据缺失[：:][^\]]*(?:近20日|历史日度|主力资金历史)[^\]]*\]"),
    re.compile(
        r"\[数据缺失[：:][^\]]*(?:行业对比|行业横向|HTTP\s*502|全市场.{0,8}行业)[^\]]*\]"
    ),
    re.compile(r"\[数据缺失[：:][^\]]*(?:具体涨跌幅|龙虎榜明细)[^\]]*\]"),
    re.compile(r"\[数据缺失\](?!\s*[:：])"),  # bare marker with no detail
    # Malformed markers without the usual ": " separator still trip substring checks.
    re.compile(r"\[数据缺失(?![：:\]])[^\]]*\]"),
)


SECTION_TO_ANALYST = {
    "market_report": "market",
    "sentiment_report": "social",
    "news_report": "news",
    "fundamentals_report": "fundamentals",
    "policy_report": "policy",
    "hot_money_report": "hot_money",
    "lockup_report": "lockup",
}


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str


@dataclass(frozen=True)
class BatchRepairResult:
    state: dict[str, Any]
    regenerated: list[str]
    probe_failed: list[str]
    errors: list[str]
    detail: str


def has_missing_data(text: str | None) -> bool:
    return MISSING_DATA_MARKER in (text or "")


def scrub_soft_missing_markers(text: str) -> str:
    """Remove soft/false-positive [数据缺失] phrases after a successful probe regen."""
    cleaned = text or ""
    for pattern in _SOFT_MISSING_PATTERNS:
        cleaned = pattern.sub("（该项无硬性数据缺口，已按可得信息表述）", cleaned)
    return cleaned


def has_hard_missing_data(text: str | None) -> bool:
    """True when remaining markers look like real tool/field failures after scrub."""
    return has_missing_data(scrub_soft_missing_markers(text))


def _tool_payload_ok(text: str) -> bool:
    """True when the tool returned usable content (partial OK is allowed).

    Total failures usually start with ``Error`` / ``No ``. Embedded
    ``[数据缺失: …]`` lines (e.g. fund-flow history SSL) do not fail the probe
    so the user can still regenerate with whatever data is available.
    """
    if not text or not str(text).strip():
        return False
    head = str(text).strip()[:80]
    if head.startswith("Error") or head.startswith("No "):
        return False
    return True


def _call_probe_tool(name: str, fn: Callable[[], str]) -> tuple[bool, str]:
    try:
        payload = fn()
    except Exception as exc:  # noqa: BLE001 — surface any fetch failure to UI
        return False, f"{name}: {type(exc).__name__}: {exc}"
    ok = _tool_payload_ok(payload)
    preview = str(payload).replace("\n", " ")[:160]
    return ok, f"{name}: {'ok' if ok else 'fail'} — {preview}"


def _invoke_tool(tool_obj, payload: dict[str, Any]) -> str:
    """Invoke a LangChain @tool or plain callable and coerce to str."""
    if hasattr(tool_obj, "invoke"):
        return str(tool_obj.invoke(payload))
    return str(tool_obj(**payload))


def _probe_callables(
    section_key: str, ticker: str, trade_date: str
) -> list[tuple[str, Callable[[], str]]]:
    from tradingagents.agents.utils.agent_utils import (
        get_balance_sheet,
        get_cashflow,
        get_dragon_tiger_board,
        get_fund_flow,
        get_fundamentals,
        get_income_statement,
        get_insider_transactions,
        get_lockup_expiry,
        get_northbound_flow,
    )

    analyst = SECTION_TO_ANALYST.get(section_key)
    if analyst == "fundamentals":
        return [
            (
                "get_fundamentals",
                lambda: _invoke_tool(
                    get_fundamentals, {"ticker": ticker, "curr_date": trade_date}
                ),
            ),
            (
                "get_balance_sheet",
                lambda: _invoke_tool(
                    get_balance_sheet,
                    {"ticker": ticker, "freq": "quarterly", "curr_date": trade_date},
                ),
            ),
            (
                "get_cashflow",
                lambda: _invoke_tool(
                    get_cashflow,
                    {"ticker": ticker, "freq": "quarterly", "curr_date": trade_date},
                ),
            ),
            (
                "get_income_statement",
                lambda: _invoke_tool(
                    get_income_statement,
                    {"ticker": ticker, "freq": "quarterly", "curr_date": trade_date},
                ),
            ),
        ]
    if analyst == "hot_money":
        return [
            (
                "get_fund_flow",
                lambda: _invoke_tool(
                    get_fund_flow, {"ticker": ticker, "curr_date": trade_date}
                ),
            ),
            (
                "get_northbound_flow",
                lambda: _invoke_tool(
                    get_northbound_flow, {"curr_date": trade_date}
                ),
            ),
            (
                "get_insider_transactions",
                lambda: _invoke_tool(get_insider_transactions, {"ticker": ticker}),
            ),
            (
                "get_dragon_tiger_board",
                lambda: _invoke_tool(
                    get_dragon_tiger_board,
                    {"ticker": ticker, "curr_date": trade_date},
                ),
            ),
        ]
    if analyst == "lockup":
        return [
            (
                "get_insider_transactions",
                lambda: _invoke_tool(get_insider_transactions, {"ticker": ticker}),
            ),
            (
                "get_lockup_expiry",
                lambda: _invoke_tool(
                    get_lockup_expiry, {"ticker": ticker, "curr_date": trade_date}
                ),
            ),
        ]
    return []


def section_supports_repair(section_key: str) -> bool:
    """True when the section has a data probe + regenerate path."""
    return section_key in {
        "fundamentals_report",
        "hot_money_report",
        "lockup_report",
    }


def list_repairable_missing_sections(state: dict[str, Any]) -> list[str]:
    """Return repairable report keys that currently contain hard [数据缺失].

    Soft / EmptyOK markers (无覆盖、无增减持、行业 502 等) are scrubbed first so
    the UI banner only appears for likely tool failures.
    """
    keys: list[str] = []
    for section_key in (
        "fundamentals_report",
        "hot_money_report",
        "lockup_report",
    ):
        if has_hard_missing_data(str(state.get(section_key) or "")):
            keys.append(section_key)
    return keys


def probe_section_data(
    section_key: str, ticker: str, trade_date: str
) -> ProbeResult:
    """Re-fetch the section's critical tools; ok only if all succeed."""
    callables = _probe_callables(section_key, ticker, trade_date)
    if not callables:
        return ProbeResult(
            ok=False,
            detail=f"{section_key}: 无预置探针，请直接完整重跑该分析",
        )

    lines: list[str] = []
    all_ok = True
    for name, fn in callables:
        ok, line = _call_probe_tool(name, fn)
        lines.append(line)
        if not ok:
            all_ok = False

    return ProbeResult(ok=all_ok, detail="\n".join(lines))


_PERSIST_KEYS = (
    "company_of_interest",
    "trade_date",
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
    "policy_report",
    "hot_money_report",
    "lockup_report",
    "data_quality_summary",
    "investment_debate_state",
    "trader_investment_decision",
    "risk_debate_state",
    "investment_plan",
    "final_trade_decision",
)


def _curated_persistable_state(
    state: dict[str, Any], existing: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Project runtime/LangGraph state onto the history JSON schema.

    Live ``tracker.final_state`` may contain non-JSON ``messages`` and alternate
    field names (e.g. ``trader_investment_plan``). Never dump it raw to disk.
    """
    base = dict(existing or {})
    for key in _PERSIST_KEYS:
        if key in state:
            base[key] = state[key]

    # Normalize trader field alias used by AgentState vs logged history.
    if "trader_investment_decision" not in base or not base.get(
        "trader_investment_decision"
    ):
        alias = state.get("trader_investment_plan") or base.get(
            "trader_investment_decision", ""
        )
        if alias:
            base["trader_investment_decision"] = alias

    # Drop anything that cannot round-trip through JSON.
    return json.loads(json.dumps(base, ensure_ascii=False, default=str))


def save_analysis_state(
    path: str,
    state: dict[str, Any],
    *,
    update_keys: list[str] | None = None,
) -> None:
    """Atomically merge curated fields into a full_states_log JSON file.

    When ``update_keys`` is set, only those fields from ``state`` are applied on
    top of the on-disk document — preventing a stale in-memory snapshot from
    regressing other analyst sections written by a concurrent full run.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] | None = None
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = None
        except (OSError, json.JSONDecodeError):
            existing = None

    if update_keys is not None:
        patch_source = {k: state[k] for k in update_keys if k in state}
        curated = _curated_persistable_state(patch_source, existing)
    else:
        curated = _curated_persistable_state(state, existing)
    payload = json.dumps(curated, ensure_ascii=False, indent=4)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(target.parent),
        delete=False,
        suffix=".tmp",
    ) as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    tmp_path.replace(target)


def _build_quick_llm(config: dict[str, Any]):
    from tradingagents.llm_clients import create_llm_client

    client = create_llm_client(
        provider=config.get("llm_provider", "openai"),
        model=config.get("quick_think_llm", "gpt-4o-mini"),
        base_url=config.get("backend_url"),
    )
    return client.get_llm()


def _tools_for(analyst: str) -> list:
    from tradingagents.agents.utils.agent_utils import (
        get_balance_sheet,
        get_cashflow,
        get_concept_blocks,
        get_dragon_tiger_board,
        get_fund_flow,
        get_fundamentals,
        get_global_news,
        get_hot_stocks,
        get_income_statement,
        get_industry_comparison,
        get_indicators,
        get_insider_transactions,
        get_lockup_expiry,
        get_news,
        get_northbound_flow,
        get_profit_forecast,
        get_stock_data,
    )

    mapping = {
        "market": [get_stock_data, get_indicators],
        "social": [get_news],
        "news": [get_news, get_global_news, get_insider_transactions],
        "fundamentals": [
            get_fundamentals,
            get_balance_sheet,
            get_cashflow,
            get_income_statement,
            get_profit_forecast,
            get_industry_comparison,
        ],
        "policy": [get_news, get_global_news],
        "hot_money": [
            get_stock_data,
            get_news,
            get_insider_transactions,
            get_hot_stocks,
            get_northbound_flow,
            get_concept_blocks,
            get_fund_flow,
            get_dragon_tiger_board,
            get_industry_comparison,
        ],
        "lockup": [
            get_insider_transactions,
            get_news,
            get_fundamentals,
            get_lockup_expiry,
        ],
    }
    tools = mapping.get(analyst)
    if not tools:
        raise ValueError(f"unsupported analyst for regenerate: {analyst}")
    return tools


def _tool_node_for(analyst: str):
    """Legacy helper kept for callers; prefer `_tools_for` + `_execute_tool_calls`."""
    from langgraph.prebuilt import ToolNode

    return ToolNode(_tools_for(analyst))


def _tool_name(tool_obj) -> str:
    return str(getattr(tool_obj, "name", None) or getattr(tool_obj, "__name__", ""))


def _execute_tool_calls(tools: list, ai_message) -> list:
    """Run tool_calls without LangGraph ToolNode (avoids required runtime/'N/A')."""
    from langchain_core.messages import ToolMessage

    by_name = {_tool_name(t): t for t in tools}
    results = []
    for call in getattr(ai_message, "tool_calls", None) or []:
        if isinstance(call, dict):
            name = call.get("name") or ""
            args = call.get("args") or {}
            call_id = call.get("id") or ""
        else:
            name = getattr(call, "name", "") or ""
            args = getattr(call, "args", {}) or {}
            call_id = getattr(call, "id", "") or ""

        tool_obj = by_name.get(name)
        if tool_obj is None:
            content = f"Error: unknown tool '{name}'"
        else:
            try:
                content = _invoke_tool(tool_obj, dict(args))
            except Exception as exc:  # noqa: BLE001 — surface to the analyst loop
                content = f"Error invoking {name}: {type(exc).__name__}: {exc}"
        results.append(
            ToolMessage(content=str(content), tool_call_id=call_id, name=name)
        )
    return results


def _analyst_factory(analyst: str):
    from tradingagents.agents.analysts.fundamentals_analyst import (
        create_fundamentals_analyst,
    )
    from tradingagents.agents.analysts.hot_money_tracker import create_hot_money_tracker
    from tradingagents.agents.analysts.lockup_watcher import create_lockup_watcher
    from tradingagents.agents.analysts.market_analyst import create_market_analyst
    from tradingagents.agents.analysts.news_analyst import create_news_analyst
    from tradingagents.agents.analysts.policy_analyst import create_policy_analyst
    from tradingagents.agents.analysts.social_media_analyst import (
        create_social_media_analyst,
    )

    factories = {
        "market": create_market_analyst,
        "social": create_social_media_analyst,
        "news": create_news_analyst,
        "fundamentals": create_fundamentals_analyst,
        "policy": create_policy_analyst,
        "hot_money": create_hot_money_tracker,
        "lockup": create_lockup_watcher,
    }
    factory = factories.get(analyst)
    if factory is None:
        raise ValueError(f"unsupported analyst for regenerate: {analyst}")
    return factory


def _run_analyst_tool_loop(
    *,
    analyst: str,
    ticker: str,
    trade_date: str,
    config: dict[str, Any],
    base_state: dict[str, Any],
) -> str:
    """Re-run one analyst node with its tools until a final report is produced."""
    llm = _build_quick_llm(config)
    node = _analyst_factory(analyst)(llm)
    tools = _tools_for(analyst)
    report_key = next(
        key for key, name in SECTION_TO_ANALYST.items() if name == analyst
    )

    working: dict[str, Any] = {
        **base_state,
        "company_of_interest": ticker,
        "trade_date": trade_date,
        "messages": [("human", ticker)],
        report_key: "",
    }

    for _ in range(MAX_ANALYST_TOOL_ROUNDS):
        out = node(working)
        msg = out["messages"][-1]
        working["messages"] = list(working.get("messages") or []) + [msg]
        for key, value in out.items():
            if key != "messages":
                working[key] = value

        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            report = working.get(report_key) or getattr(msg, "content", "") or ""
            return str(report)

        tool_messages = _execute_tool_calls(tools, msg)
        working["messages"] = list(working["messages"]) + list(tool_messages)

    raise RuntimeError(
        f"{analyst} regenerate exceeded {MAX_ANALYST_TOOL_ROUNDS} tool rounds"
    )


def _refresh_quality_gate(state: dict[str, Any], config: dict[str, Any]) -> str:
    from tradingagents.agents.quality_gate import create_quality_gate

    llm = _build_quick_llm(config)
    out = create_quality_gate(llm)(state)
    return str(out.get("data_quality_summary") or "")


def _build_deep_llm(config: dict[str, Any]):
    from tradingagents.llm_clients import create_llm_client

    client = create_llm_client(
        provider=config.get("llm_provider", "openai"),
        model=config.get("deep_think_llm") or config.get("quick_think_llm", "gpt-4o-mini"),
        base_url=config.get("backend_url"),
    )
    return client.get_llm()


def refresh_downstream_decisions(
    state: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Re-run Research Manager → Trader → Portfolio Manager on repaired reports.

    Analyst-section repair alone leaves the original investment_plan / final
    decision (often arguing 「数据 C 级崩塌」) untouched; refresh so the UI
    investment advice matches the repaired evidence.
    """
    from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
    from tradingagents.agents.managers.research_manager import create_research_manager
    from tradingagents.agents.trader.trader import create_trader

    deep = _build_deep_llm(config)
    quick = _build_quick_llm(config)
    working = dict(state)

    note = (
        "\n\n[Data-repair note] Analyst sections were regenerated after a successful "
        "data-source probe. Re-evaluate using the *current* reports and quality gate; "
        "do not treat earlier C-grade / missing-data conclusions as binding if the "
        "repaired evidence no longer supports them."
    )
    debate = dict(working.get("investment_debate_state") or {})
    if debate.get("history"):
        debate["history"] = str(debate["history"]) + note
        working["investment_debate_state"] = debate

    rm_out = create_research_manager(deep)(working)
    working.update(rm_out)
    if working.get("investment_plan"):
        working["investment_plan"] = str(working["investment_plan"]) + note

    trader_out = create_trader(quick)(working)
    working.update(trader_out)
    # History JSON uses trader_investment_decision
    if working.get("trader_investment_plan") and not working.get(
        "trader_investment_decision"
    ):
        working["trader_investment_decision"] = working["trader_investment_plan"]

    risk = dict(working.get("risk_debate_state") or {})
    if risk.get("history"):
        risk["history"] = str(risk["history"]) + note
        working["risk_debate_state"] = risk

    pm_out = create_portfolio_manager(deep)(working)
    working.update(pm_out)
    return working


def regenerate_section(
    *,
    state: dict[str, Any],
    section_key: str,
    config: dict[str, Any],
    log_path: str | None = None,
    refresh_quality: bool = True,
) -> dict[str, Any]:
    """Regenerate one analyst section, optionally refresh quality gate / persist."""
    analyst = SECTION_TO_ANALYST.get(section_key)
    if analyst is None:
        raise ValueError(f"unknown section: {section_key}")

    ticker = str(state.get("company_of_interest") or "").strip()
    trade_date = str(state.get("trade_date") or "").strip()
    if not ticker or not trade_date:
        raise ValueError("state missing company_of_interest / trade_date")

    report = _run_analyst_tool_loop(
        analyst=analyst,
        ticker=ticker,
        trade_date=trade_date,
        config=config,
        base_state=state,
    )
    report = scrub_soft_missing_markers(str(report))
    updated = dict(state)
    updated[section_key] = report
    update_keys = [section_key]
    if refresh_quality:
        updated["data_quality_summary"] = _refresh_quality_gate(updated, config)
        update_keys.append("data_quality_summary")

    if log_path:
        save_analysis_state(
            log_path,
            updated,
            update_keys=update_keys,
        )
        logger.info("regenerated %s and saved to %s", section_key, log_path)

    return updated


def repair_all_missing_sections(
    *,
    state: dict[str, Any],
    config: dict[str, Any],
    log_path: str | None = None,
) -> BatchRepairResult:
    """Probe every repairable missing section; auto-regenerate those that succeed."""
    ticker = str(state.get("company_of_interest") or "").strip()
    trade_date = str(state.get("trade_date") or "").strip()
    if not ticker or not trade_date:
        raise ValueError("state missing company_of_interest / trade_date")

    targets = list_repairable_missing_sections(state)
    working = dict(state)
    regenerated: list[str] = []
    probe_failed: list[str] = []
    errors: list[str] = []
    detail_parts: list[str] = []

    for section_key in targets:
        probe = probe_section_data(section_key, ticker, trade_date)
        detail_parts.append(f"### {section_key}\n{probe.detail}")
        if not probe.ok:
            probe_failed.append(section_key)
            continue
        try:
            working = regenerate_section(
                state=working,
                section_key=section_key,
                config=config,
                log_path=None,
                refresh_quality=False,
            )
            regenerated.append(section_key)
        except Exception as exc:  # noqa: BLE001
            msg = f"{section_key}: {type(exc).__name__}: {exc}"
            errors.append(msg)
            detail_parts.append(f"regen error — {msg}")
            logger.exception("batch regenerate failed for %s", section_key)

    if regenerated:
        try:
            working = refresh_downstream_decisions(working, config)
            detail_parts.append("### downstream\nrefreshed investment_plan / trader / final_trade_decision")
        except Exception as exc:  # noqa: BLE001
            msg = f"downstream: {type(exc).__name__}: {exc}"
            errors.append(msg)
            detail_parts.append(f"downstream refresh error — {msg}")
            logger.exception("downstream decision refresh failed")

        working["data_quality_summary"] = _refresh_quality_gate(working, config)
        if log_path:
            save_analysis_state(
                log_path,
                working,
                update_keys=[
                    *regenerated,
                    "data_quality_summary",
                    "investment_plan",
                    "trader_investment_plan",
                    "trader_investment_decision",
                    "final_trade_decision",
                    "investment_debate_state",
                    "risk_debate_state",
                ],
            )

    return BatchRepairResult(
        state=working,
        regenerated=regenerated,
        probe_failed=probe_failed,
        errors=errors,
        detail="\n\n".join(detail_parts),
    )
