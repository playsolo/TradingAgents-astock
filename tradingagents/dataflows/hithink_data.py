"""HiThink-enhanced data tools (Phase 1).

Returns formatted text for LangChain agents. When ``HITHINK_ENABLED`` is off or
the API is unavailable, returns an explicit EmptyOK message so analysts fall
back to legacy tools without ``[数据缺失]`` markers.
"""

from __future__ import annotations

import logging
from typing import Annotated

from tradingagents.dataflows.feature_snapshot import (
    build_market_regime_snapshot,
    build_stock_feature_snapshot,
    format_market_regime_text,
    format_stock_features_text,
)
from tradingagents.dataflows.hithink_client import (
    HiThinkAPIError,
    code_to_thscode,
    get_hithink_client,
    is_hithink_enabled,
)
from tradingagents.dataflows.utils import safe_ticker_component

logger = logging.getLogger(__name__)

_DISABLED_MSG = (
    "HiThink 金融数据 API 未启用（需设置 HITHINK_ENABLED=true 且配置 "
    "HITHINK_FINANCE_API_KEY）。请改用其他已有工具获取数据；"
    "勿标注 [数据缺失]，这不是工具故障。"
)


def _disabled(label: str) -> str:
    return f"# {label}\n{_DISABLED_MSG}"


def _api_error(label: str, exc: Exception) -> str:
    return (
        f"# {label}\n"
        f"HiThink API 调用失败: {exc}\n"
        "请改用其他已有工具；仅当无替代数据源时才标注 [数据缺失: hithink]."
    )


def get_hithink_market_sentiment(
    ticker: Annotated[str, "A-stock code (e.g. 688017)"],
    curr_date: Annotated[str, "Date YYYY-MM-DD (for header only)"] = "",
) -> str:
    """Structured attention metrics: hot rank, heat, skyrocket, anomaly tags."""
    if not is_hithink_enabled():
        return _disabled("Market Sentiment (HiThink)")

    code = safe_ticker_component(ticker)
    try:
        snap = build_stock_feature_snapshot(code, include_financials=False)
        if snap is None:
            return _disabled("Market Sentiment (HiThink)")

        lines = [
            f"# Market Sentiment (HiThink) | {code}",
            f"# Analysis date: {curr_date or 'today'}",
            f"# Source: {snap.source} | Retrieved: {snap.retrieved_at}",
            "",
        ]
        if snap.hot_rank is not None:
            lines.append("## Hot-stock list (24h)")
            lines.append(f"  rank: {snap.hot_rank}")
            lines.append(f"  heat: {snap.hot_heat}")
            lines.append(f"  rank_change: {snap.hot_rank_change}")
        else:
            lines.append("## Hot-stock list: not in top ranks (or list empty)")

        lines.append(f"\n## Skyrocket list: {'yes' if snap.in_skyrocket else 'no'}")

        if snap.anomaly_tags or snap.anomaly_keywords or snap.anomaly_content:
            lines.append("\n## Today's anomaly (HiThink)")
            if snap.anomaly_tags:
                lines.append(f"  tags: {', '.join(snap.anomaly_tags)}")
            if snap.anomaly_keywords:
                lines.append(f"  keywords: {', '.join(snap.anomaly_keywords)}")
            if snap.anomaly_content:
                snippet = snap.anomaly_content[:500]
                lines.append(f"  analysis: {snippet}")

        cli = get_hithink_client()
        try:
            trend = cli.get(
                "/api/a-share/special-data/hot-stock-rank-trend",
                {
                    "thscode": snap.thscode,
                    "start_date": _rank_trend_start(),
                    "end_date": _rank_trend_end(),
                },
            )
            items = list((trend or {}).get("item") or [])
            if items:
                lines.append("\n## Hot-rank trend (recent)")
                for row in items[-5:]:
                    lines.append(
                        f"  {row.get('date')}: rank {row.get('rank')}"
                    )
        except HiThinkAPIError as exc:
            logger.debug("hot rank trend skipped for %s: %s", code, exc)

        return "\n".join(lines)
    except Exception as exc:
        logger.warning("get_hithink_market_sentiment failed for %s: %s", code, exc)
        return _api_error("Market Sentiment (HiThink)", exc)


def _rank_trend_start() -> str:
    from datetime import datetime, timedelta

    return (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")


def _rank_trend_end() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d")


def get_hithink_auction_signal(
    ticker: Annotated[str, "A-stock code"],
) -> str:
    """Opening auction strength: pct, volume ratio, turnover, unmatched."""
    if not is_hithink_enabled():
        return _disabled("Auction Signal (HiThink)")

    code = safe_ticker_component(ticker)
    thscode = code_to_thscode(code)
    try:
        cli = get_hithink_client()
        rows = cli.auction_snapshot([thscode])
        bench = cli.auction_short_term_benchmark()
        lines = [
            f"# Auction Signal (HiThink) | {code} ({thscode})",
            f"# Source: hithink-finance",
            "",
        ]
        if rows:
            a = rows[0]
            lines.append("## Stock auction snapshot")
            for key, label in (
                ("auction_pct", "竞价涨跌幅(%)"),
                ("auction_volume_ratio", "竞价量比"),
                ("auction_turnover_pct", "竞价换手(%)"),
                ("auction_yesterday_ratio_pct", "相对昨量(%)"),
                ("auction_amount", "竞价成交额"),
                ("auction_unmatched", "未匹配量"),
                ("data_status", "数据状态"),
            ):
                if a.get(key) is not None:
                    lines.append(f"  {label}: {a[key]}")
        else:
            lines.append("## Stock auction: no row returned (非竞价时段或未就绪)")

        if bench:
            lines.append("\n## Short-term benchmark (market sample, top 5)")
            for row in bench[:5]:
                tags = row.get("tags") or []
                tag_str = ",".join(tags) if isinstance(tags, list) else str(tags)
                lines.append(
                    f"  {row.get('ticker')} {row.get('name')}: "
                    f"auction_pct={row.get('auction_pct')} tags={tag_str}"
                )
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("get_hithink_auction_signal failed for %s: %s", code, exc)
        return _api_error("Auction Signal (HiThink)", exc)


def get_hithink_financial_quality(
    ticker: Annotated[str, "A-stock code"],
) -> str:
    """Cash conversion, FCF margin, accrual ratio from HiThink financials."""
    if not is_hithink_enabled():
        return _disabled("Financial Quality (HiThink)")

    code = safe_ticker_component(ticker)
    try:
        snap = build_stock_feature_snapshot(code, include_financials=True)
        if snap is None:
            return _disabled("Financial Quality (HiThink)")

        lines = [
            f"# Financial Quality (HiThink) | {code}",
            f"# Source: {snap.source} | Retrieved: {snap.retrieved_at}",
            "",
        ]
        if snap.financial_quality:
            lines.append("## Derived ratios (latest quarterly, HiThink 三表)")
            for key, val in snap.financial_quality.items():
                lines.append(f"  {key}: {val}")
            lines.append(
                "\n口径: cash_conversion=经营现金流/净利润; "
                "fcf_margin=(经营现金流-资本开支)/营收; "
                "accrual_ratio=(净利润-经营现金流)/总资产"
            )
        else:
            lines.append("无可用财报派生指标（可能未披露或 API 空返回）")

        cli = get_hithink_client()
        try:
            indicators = cli.financial_indicators(
                snap.thscode, _latest_report_period()
            )
            abilities = indicators.get("abilities") or []
            if abilities:
                lines.append(f"\n## Official indicators ({indicators.get('report')})")
                for block in abilities:
                    ability = block.get("ability", "")
                    lines.append(f"  [{ability}]")
                    for ind in (block.get("indicators") or [])[:6]:
                        lines.append(
                            f"    {ind.get('index_id')}: {ind.get('value')}"
                        )
        except HiThinkAPIError as exc:
            logger.debug("financial indicators skipped: %s", exc)

        return "\n".join(lines)
    except Exception as exc:
        logger.warning("get_hithink_financial_quality failed for %s: %s", code, exc)
        return _api_error("Financial Quality (HiThink)", exc)


def _latest_report_period() -> str:
    """Best-effort current report period string ``YYYY-Q``."""
    from datetime import datetime

    now = datetime.now()
    q = (now.month - 1) // 3 + 1
    if q == 1 and now.month <= 4:
        return f"{now.year - 1}-4"
    return f"{now.year}-{max(1, q - 1)}"


def get_hithink_short_term_structure(
    ticker: Annotated[str, "A-stock code"],
    trade_date: Annotated[str, "Date YYYY-MM-DD"] = "",
) -> str:
    """Limit-up/down/break pools, ladder, dragon-tiger org vs hot-money."""
    if not is_hithink_enabled():
        return _disabled("Short-term Structure (HiThink)")

    code = safe_ticker_component(ticker)
    thscode = code_to_thscode(code)
    try:
        cli = get_hithink_client()
        lines = [
            f"# Short-term Structure (HiThink) | {code}",
            f"# Trade date context: {trade_date or 'latest'}",
            "",
        ]

        # Market-wide structure
        regime = build_market_regime_snapshot(client=cli)
        if regime:
            lines.append("## Market structure")
            lines.append(f"  limit_up_count: {regime.limit_up_count}")
            lines.append(f"  limit_down_count: {regime.limit_down_count}")
            lines.append(f"  limit_break_count: {regime.limit_break_count}")
            lines.append(f"  max_continue_board(sample): {regime.max_continue_board}")
            lines.append(
                f"  dragon_tiger org_net_total: {regime.org_net_total} | "
                f"hot_money_net_total: {regime.hot_money_net_total}"
            )

        # Stock in limit-up pool today?
        try:
            pool = cli.limit_up_pool(size=200, page=1)
            hit = None
            for row in pool.get("item") or []:
                if row.get("thscode") == thscode or row.get("ticker") == code:
                    hit = row
                    break
            if hit:
                lines.append("\n## Stock in limit-up pool")
                for k in (
                    "continue_day_cnt",
                    "continue_day_text",
                    "limit_up_time",
                    "seal_money",
                    "limit_up_reason",
                    "price_change_ratio_pct",
                ):
                    if hit.get(k) is not None:
                        lines.append(f"  {k}: {hit[k]}")
            else:
                lines.append("\n## Stock in limit-up pool: no")
        except HiThinkAPIError as exc:
            lines.append(f"\n## Limit-up pool: fetch failed ({exc})")

        # Dragon-tiger for stock if on latest board
        try:
            dt = cli.dragon_tiger_list(board_type="all")
            for row in dt.get("stock_items") or []:
                if row.get("thscode") == thscode or row.get("ticker") == code:
                    lines.append("\n## Dragon-tiger (latest board)")
                    for k in (
                        "net_value",
                        "org_net_value",
                        "hot_money_net_value",
                        "limit_reason",
                        "concept_list",
                    ):
                        if row.get(k) is not None:
                            lines.append(f"  {k}: {row[k]}")
                    break
        except HiThinkAPIError as exc:
            logger.debug("dragon-tiger stock lookup: %s", exc)

        anomalies = cli.anomaly_analysis_stock([thscode])
        if anomalies:
            row = anomalies[0]
            lines.append("\n## Today anomaly")
            lines.append(f"  tag: {row.get('tag_name')}")
            lines.append(f"  keywords: {row.get('keyword_list')}")

        return "\n".join(lines)
    except Exception as exc:
        logger.warning("get_hithink_short_term_structure failed for %s: %s", code, exc)
        return _api_error("Short-term Structure (HiThink)", exc)


def get_hithink_valuation_snapshot(
    ticker: Annotated[str, "A-stock code"],
) -> str:
    """PE/PB/PS/PCF snapshot from HiThink valuations API."""
    if not is_hithink_enabled():
        return _disabled("Valuation Snapshot (HiThink)")

    code = safe_ticker_component(ticker)
    thscode = code_to_thscode(code)
    try:
        cli = get_hithink_client()
        rows = cli.valuations_snapshot([thscode])
        if not rows:
            return (
                f"# Valuation Snapshot (HiThink) | {code}\n"
                "无估值数据返回（可能停牌或未披露）"
            )
        v = rows[0]
        lines = [
            f"# Valuation Snapshot (HiThink) | {code} ({thscode})",
            f"# Source: hithink-finance",
            "",
        ]
        for key in ("name", "pe_ttm", "pe_mrq", "pb_mrq", "ps_ttm", "pcf_ttm"):
            val = v.get(key)
            if val is not None:
                lines.append(f"  {key}: {val}")
        lines.append(
            "\n注: null 表示上游未披露；负数可能反映亏损，勿自动取绝对值。"
        )
        return "\n".join(lines)
    except Exception as exc:
        logger.warning("get_hithink_valuation_snapshot failed for %s: %s", code, exc)
        return _api_error("Valuation Snapshot (HiThink)", exc)


def get_hithink_market_regime(
    curr_date: Annotated[str, "Date YYYY-MM-DD, empty for today"] = "",
) -> str:
    """Market-wide limit-up/down/break counts, hot list, dragon-tiger totals."""
    if not is_hithink_enabled():
        return _disabled("Market Regime (HiThink)")

    try:
        snap = build_market_regime_snapshot()
        if snap is None:
            return _disabled("Market Regime (HiThink)")
        header = f"# Market Regime (HiThink) | {curr_date or 'today'}\n"
        return header + format_market_regime_text(snap).split("\n", 1)[-1]
    except Exception as exc:
        logger.warning("get_hithink_market_regime failed: %s", exc)
        return _api_error("Market Regime (HiThink)", exc)
