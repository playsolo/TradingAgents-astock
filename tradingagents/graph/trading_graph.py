# TradingAgents/graph/trading_graph.py

import logging
import os
from pathlib import Path
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple, List, Optional

import yfinance as yf

logger = logging.getLogger(__name__)

from langgraph.prebuilt import ToolNode

from tradingagents.llm_clients import create_llm_client

from tradingagents.agents import *
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.agents.utils.rating import parse_rating
from tradingagents.agents.utils.signal_accuracy import (
    config_fingerprint,
    get_ledger,
    run_accuracy_maintenance,
)
from tradingagents.dataflows.utils import safe_ticker_component
from tradingagents.agents.utils.agent_states import (
    AgentState,
    InvestDebateState,
    RiskDebateState,
)
from tradingagents.dataflows.config import set_config

# Import the new abstract tool methods from agent_utils
from tradingagents.agents.utils.agent_utils import (
    get_stock_data,
    get_indicators,
    get_fundamentals,
    get_balance_sheet,
    get_cashflow,
    get_income_statement,
    get_news,
    get_insider_transactions,
    get_global_news,
    get_profit_forecast,
    get_hot_stocks,
    get_northbound_flow,
    get_concept_blocks,
    get_fund_flow,
    get_dragon_tiger_board,
    get_lockup_expiry,
    get_industry_comparison,
)

from .checkpointer import checkpoint_step, clear_checkpoint, get_checkpointer, thread_id
from .conditional_logic import ConditionalLogic
from .setup import GraphSetup
from .propagation import Propagator
from .reflection import Reflector
from .signal_processing import SignalProcessor


class TradingAgentsGraph:
    """Main class that orchestrates the trading agents framework."""

    def __init__(
        self,
        selected_analysts=["market", "social", "news", "fundamentals", "policy", "hot_money", "lockup"],
        debug=False,
        config: Dict[str, Any] = None,
        callbacks: Optional[List] = None,
    ):
        """Initialize the trading agents graph and components.

        Args:
            selected_analysts: List of analyst types to include
            debug: Whether to run in debug mode
            config: Configuration dictionary. If None, uses default config
            callbacks: Optional list of callback handlers (e.g., for tracking LLM/tool stats)
        """
        self.debug = debug
        self.config = config or DEFAULT_CONFIG
        self.callbacks = callbacks or []

        # Update the interface's config
        set_config(self.config)

        # Create necessary directories
        os.makedirs(self.config["data_cache_dir"], exist_ok=True)
        os.makedirs(self.config["results_dir"], exist_ok=True)

        # Initialize LLMs with provider-specific thinking configuration
        llm_kwargs = self._get_provider_kwargs()

        # Add callbacks to kwargs if provided (passed to LLM constructor)
        if self.callbacks:
            llm_kwargs["callbacks"] = self.callbacks

        deep_client = create_llm_client(
            provider=self.config["llm_provider"],
            model=self.config["deep_think_llm"],
            base_url=self.config.get("backend_url"),
            **llm_kwargs,
        )
        quick_client = create_llm_client(
            provider=self.config["llm_provider"],
            model=self.config["quick_think_llm"],
            base_url=self.config.get("backend_url"),
            **llm_kwargs,
        )

        self.deep_thinking_llm = deep_client.get_llm()
        self.quick_thinking_llm = quick_client.get_llm()
        
        self.memory_log = TradingMemoryLog(self.config)
        self.accuracy_ledger = get_ledger(self.config)

        # Create tool nodes
        self.tool_nodes = self._create_tool_nodes()

        # Initialize components
        self.conditional_logic = ConditionalLogic(
            max_debate_rounds=self.config["max_debate_rounds"],
            max_risk_discuss_rounds=self.config["max_risk_discuss_rounds"],
        )
        self.graph_setup = GraphSetup(
            self.quick_thinking_llm,
            self.deep_thinking_llm,
            self.tool_nodes,
            self.conditional_logic,
        )

        self.propagator = Propagator()
        self.reflector = Reflector(self.quick_thinking_llm)
        self.signal_processor = SignalProcessor(self.quick_thinking_llm)

        # State tracking
        self.curr_state = None
        self.ticker = None
        self.log_states_dict = {}  # date to full state dict

        # Set up the graph: keep the workflow for recompilation with a checkpointer.
        self.workflow = self.graph_setup.setup_graph(selected_analysts)
        self.graph = self.workflow.compile()
        self._checkpointer_ctx = None

    def _get_provider_kwargs(self) -> Dict[str, Any]:
        """Get provider-specific kwargs for LLM client creation."""
        kwargs = {}
        provider = self.config.get("llm_provider", "").lower()

        if provider == "google":
            thinking_level = self.config.get("google_thinking_level")
            if thinking_level:
                kwargs["thinking_level"] = thinking_level

        elif provider == "openai":
            reasoning_effort = self.config.get("openai_reasoning_effort")
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort

        elif provider == "anthropic":
            effort = self.config.get("anthropic_effort")
            if effort:
                kwargs["effort"] = effort

        # Configurable LLM retry budget forwarded to every provider.
        max_retries = self.config.get("llm_max_retries")
        if max_retries is not None:
            kwargs["max_retries"] = max_retries

        return kwargs

    def _create_tool_nodes(self) -> Dict[str, ToolNode]:
        """Create tool nodes for different data sources using abstract methods."""
        return {
            "market": ToolNode(
                [
                    # Core stock data tools
                    get_stock_data,
                    # Technical indicators
                    get_indicators,
                ]
            ),
            "social": ToolNode(
                [
                    # News tools for social media analysis
                    get_news,
                ]
            ),
            "news": ToolNode(
                [
                    # News and insider information
                    get_news,
                    get_global_news,
                    get_insider_transactions,
                ]
            ),
            "fundamentals": ToolNode(
                [
                    get_fundamentals,
                    get_balance_sheet,
                    get_cashflow,
                    get_income_statement,
                    get_profit_forecast,
                    get_industry_comparison,
                ]
            ),
            "policy": ToolNode(
                [
                    get_news,
                    get_global_news,
                ]
            ),
            "hot_money": ToolNode(
                [
                    get_stock_data,
                    get_news,
                    get_insider_transactions,
                    get_hot_stocks,
                    get_northbound_flow,
                    get_concept_blocks,
                    get_fund_flow,
                    get_dragon_tiger_board,
                    get_industry_comparison,
                ]
            ),
            "lockup": ToolNode(
                [
                    get_insider_transactions,
                    get_news,
                    get_fundamentals,
                    get_lockup_expiry,
                ]
            ),
        }

    def _fetch_returns(
        self, ticker: str, trade_date: str, holding_days: int = 5
    ) -> Tuple[Optional[float], Optional[float], Optional[int]]:
        """Fetch raw and alpha return for ticker over holding_days from trade_date.

        Returns (raw_return, alpha_return, actual_holding_days) or
        (None, None, None) if price data is unavailable (too recent, delisted,
        or network error).
        """
        try:
            start = datetime.strptime(trade_date, "%Y-%m-%d")
            end = start + timedelta(days=holding_days + 7)  # buffer for weekends/holidays
            end_str = end.strftime("%Y-%m-%d")

            stock = yf.Ticker(ticker).history(start=trade_date, end=end_str)
            benchmark = yf.Ticker("000300.SS").history(start=trade_date, end=end_str)

            if len(stock) < 2 or len(benchmark) < 2:
                return None, None, None

            actual_days = min(holding_days, len(stock) - 1, len(benchmark) - 1)
            raw = float(
                (stock["Close"].iloc[actual_days] - stock["Close"].iloc[0])
                / stock["Close"].iloc[0]
            )
            bench_ret = float(
                (benchmark["Close"].iloc[actual_days] - benchmark["Close"].iloc[0])
                / benchmark["Close"].iloc[0]
            )
            alpha = raw - bench_ret
            return raw, alpha, actual_days
        except Exception as e:
            logger.warning(
                "Could not resolve outcome for %s on %s (will retry next run): %s",
                ticker, trade_date, e,
            )
            return None, None, None

    def settle_accuracy(self, *, sync_memory: bool = True) -> List[dict]:
        """Auto-settle due horizons for all enrolled signals (direction hit)."""
        # Keep in-memory ledger handle fresh after maintenance writes.
        result = run_accuracy_maintenance(self.config, sync_memory=sync_memory)
        self.accuracy_ledger = get_ledger(self.config)
        return list(result.get("events") or [])

    def _resolve_pending_entries(self, ticker: str) -> None:
        """Settle accuracy for all tickers, then enrich same-ticker memory via LLM.

        Accuracy ledger settle is the authority for direction-hit tracking and no
        longer depends on re-running the same ticker.  LLM reflections still run
        only for this ticker's remaining pending memory entries (if any) so prompt
        context stays rich without settling the whole book via paid calls.
        """
        try:
            self.settle_accuracy(sync_memory=True)
        except Exception:
            logger.exception("Accuracy settle failed; continuing with memory resolve")

        pending = [
            e
            for e in self.memory_log.get_pending_entries()
            if str(e["ticker"]).upper() == str(ticker).upper()
        ]
        if not pending:
            return

        updates = []
        for entry in pending:
            raw, alpha, days = self._fetch_returns(ticker, entry["date"])
            if raw is None:
                continue
            reflection = self.reflector.reflect_on_final_decision(
                final_decision=entry.get("decision", ""),
                raw_return=raw,
                alpha_return=alpha,
            )
            updates.append({
                "ticker": ticker,
                "trade_date": entry["date"],
                "raw_return": raw,
                "alpha_return": alpha,
                "holding_days": days,
                "reflection": reflection,
            })

        if updates:
            self.memory_log.batch_update_with_outcomes(updates)

    def propagate(self, company_name, trade_date, extra_past_context: str = ""):
        """Run the trading agents graph for a company on a specific date.

        When ``checkpoint_enabled`` is set in config, the graph is recompiled
        with a per-ticker SqliteSaver so a crashed run can resume from the last
        successful node on a subsequent invocation with the same ticker+date.

        ``extra_past_context`` is prepended to memory-log past_context (e.g. watchlist baseline).
        """
        return self._run_graph(
            company_name, trade_date, extra_past_context=extra_past_context
        )

    def prepare_graph_run(
        self,
        company_name,
        trade_date,
        callbacks: Optional[List] = None,
        extra_past_context: str = "",
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], Optional[int]]:
        """Prepare graph input/args for a fresh or resumed run.

        Returns ``(initial_state, args, checkpoint_step)``. When a checkpoint
        already exists, ``initial_state`` is ``None`` so LangGraph resumes the
        existing thread instead of replaying completed nodes.
        """
        self.ticker = company_name

        # Resolve any pending memory-log entries for this ticker before the pipeline runs.
        self._resolve_pending_entries(company_name)

        checkpoint_enabled = self.config.get("checkpoint_enabled")
        resume_step = None

        # Recompile with a checkpointer if the user opted in.
        if checkpoint_enabled:
            self._checkpointer_ctx = get_checkpointer(
                self.config["data_cache_dir"], company_name
            )
            saver = self._checkpointer_ctx.__enter__()
            self.graph = self.workflow.compile(checkpointer=saver)

            resume_step = checkpoint_step(
                self.config["data_cache_dir"], company_name, str(trade_date)
            )
            if resume_step is not None:
                logger.info(
                    "Resuming from step %d for %s on %s",
                    resume_step,
                    company_name,
                    trade_date,
                )
            else:
                logger.info("Starting fresh for %s on %s", company_name, trade_date)

        args = self.propagator.get_graph_args(callbacks=callbacks)

        # Inject thread_id so same ticker+date resumes, different date starts fresh.
        if checkpoint_enabled:
            tid = thread_id(company_name, str(trade_date))
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = tid

        if checkpoint_enabled and resume_step is not None:
            return None, args, resume_step

        # Initialize state only for fresh runs. Passing a new initial state to
        # LangGraph would start a new run and replay completed nodes.
        past_context = self.memory_log.get_past_context(company_name)
        extra = (extra_past_context or "").strip()
        if extra:
            past_context = f"{extra}\n\n{past_context}".strip() if past_context else extra
        init_agent_state = self.propagator.create_initial_state(
            company_name, trade_date, past_context=past_context
        )
        return init_agent_state, args, resume_step

    def finalize_graph_run(self, company_name, trade_date, final_state):
        """Persist a completed run and clear its checkpoint."""
        self.curr_state = final_state

        # Second-pass extract: structured action JSON from the final-plan section
        # (not debate prose). Failure is non-fatal — heuristic signal still works.
        plan = None
        rating_to_sidebar_signal = None
        try:
            from tradingagents.agents.utils.action_plan import (
                extract_action_plan,
                rating_to_sidebar_signal as _rating_to_sidebar,
            )

            rating_to_sidebar_signal = _rating_to_sidebar
            plan = extract_action_plan(
                self.quick_thinking_llm,
                str(final_state.get("final_trade_decision") or ""),
            )
            if plan:
                final_state["action_plan"] = plan
        except Exception:
            logger.exception("Action plan extract raised; continuing without it")
            plan = None

        # Log state to disk.
        self._log_state(trade_date, final_state)

        # Store decision for deferred reflection + enroll in accuracy ledger.
        decision_text = final_state["final_trade_decision"]
        self.memory_log.store_decision(
            ticker=company_name,
            trade_date=trade_date,
            final_trade_decision=decision_text,
        )
        rating = None
        plan = final_state.get("action_plan")
        if isinstance(plan, dict) and plan.get("rating"):
            rating = str(plan["rating"])
        if not rating:
            rating = parse_rating(str(decision_text or ""))
        try:
            self.accuracy_ledger.enroll(
                ticker=company_name,
                trade_date=str(trade_date),
                rating=rating,
                config_fp=config_fingerprint(self.config),
                decision=str(decision_text or ""),
                source="live",
            )
        except Exception:
            logger.exception("Accuracy ledger enroll failed; analysis result still saved")

        # Clear checkpoint on successful completion to avoid stale state.
        if self.config.get("checkpoint_enabled"):
            clear_checkpoint(
                self.config["data_cache_dir"], company_name, str(trade_date)
            )

        if plan and plan.get("rating") and rating_to_sidebar_signal is not None:
            return rating_to_sidebar_signal(plan["rating"])
        raw_signal = self.process_signal(final_state["final_trade_decision"])
        # Collapse 5-tier (Overweight/Underweight) to the 3-tier sidebar buckets
        # so the live signal matches the disk-derived history signal.
        if rating_to_sidebar_signal is not None:
            return rating_to_sidebar_signal(raw_signal)
        return raw_signal

    def close_graph_run(self) -> None:
        """Close the active checkpointer context, if any."""
        if self._checkpointer_ctx is not None:
            self._checkpointer_ctx.__exit__(None, None, None)
            self._checkpointer_ctx = None
            self.graph = self.workflow.compile()

    def _run_graph(self, company_name, trade_date, extra_past_context: str = ""):
        """Execute the graph and write the resulting state to disk and memory log."""
        init_agent_state, args, _ = self.prepare_graph_run(
            company_name, trade_date, extra_past_context=extra_past_context
        )

        try:
            if self.debug:
                trace = []
                for chunk in self.graph.stream(init_agent_state, **args):
                    if len(chunk["messages"]) == 0:
                        pass
                    else:
                        chunk["messages"][-1].pretty_print()
                        trace.append(chunk)
                final_state = trace[-1]
            else:
                final_state = self.graph.invoke(init_agent_state, **args)

            signal = self.finalize_graph_run(company_name, trade_date, final_state)
            return final_state, signal
        finally:
            self.close_graph_run()

    def _log_state(self, trade_date, final_state):
        """Log the final state to a JSON file."""
        self.log_states_dict[str(trade_date)] = {
            "company_of_interest": final_state["company_of_interest"],
            "trade_date": final_state["trade_date"],
            "market_report": final_state["market_report"],
            "sentiment_report": final_state["sentiment_report"],
            "news_report": final_state["news_report"],
            "fundamentals_report": final_state["fundamentals_report"],
            "policy_report": final_state.get("policy_report", ""),
            "hot_money_report": final_state.get("hot_money_report", ""),
            "lockup_report": final_state.get("lockup_report", ""),
            "data_quality_summary": final_state.get("data_quality_summary", ""),
            "investment_debate_state": {
                "bull_history": final_state["investment_debate_state"]["bull_history"],
                "bear_history": final_state["investment_debate_state"]["bear_history"],
                "history": final_state["investment_debate_state"]["history"],
                "current_response": final_state["investment_debate_state"][
                    "current_response"
                ],
                "judge_decision": final_state["investment_debate_state"][
                    "judge_decision"
                ],
            },
            "trader_investment_decision": final_state["trader_investment_plan"],
            "risk_debate_state": {
                "aggressive_history": final_state["risk_debate_state"]["aggressive_history"],
                "conservative_history": final_state["risk_debate_state"]["conservative_history"],
                "neutral_history": final_state["risk_debate_state"]["neutral_history"],
                "history": final_state["risk_debate_state"]["history"],
                "judge_decision": final_state["risk_debate_state"]["judge_decision"],
            },
            "investment_plan": final_state["investment_plan"],
            "final_trade_decision": final_state["final_trade_decision"],
        }
        if final_state.get("action_plan"):
            self.log_states_dict[str(trade_date)]["action_plan"] = final_state[
                "action_plan"
            ]

        # Save to file. Reject ticker values that would escape the
        # results directory when joined as a path component.
        safe_ticker = safe_ticker_component(self.ticker)
        directory = Path(self.config["results_dir"]) / safe_ticker / "TradingAgentsStrategy_logs"
        directory.mkdir(parents=True, exist_ok=True)

        log_path = directory / f"full_states_log_{trade_date}.json"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(self.log_states_dict[str(trade_date)], f, indent=4)

    def process_signal(self, full_signal):
        """Process a signal to extract the core decision."""
        return self.signal_processor.process_signal(full_signal)
