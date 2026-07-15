"""价值波段选股策略 — L0/L1/L2 三阶漏斗筛选。

使用方法::

    from tradingagents.strategies.value_swing import run_value_swing_scan
    result = run_value_swing_scan()
    for candidate in result.candidates:
        print(candidate.code, candidate.signal_score)

设计要点：
- L0: 全A腾讯批量报价（~10s），筛流动性/ST/上市时长
- L1: 双阶段 — L1a 秒级(仅 PE/PB 判定)，L1b 后置(财务验证)
- L2: 催化剂信号评分，取 top N
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd
import requests

from tradingagents.dataflows.a_stock import (
    _build_name_code_map,
    _tencent_quote,
    get_fund_flow,
)

logger = logging.getLogger(__name__)

# ── 市场排除 ────────────────────────────────────────────────────────────────


def _is_stock_excluded_by_prefix(code: str) -> bool:
    """北交所 (8xxx) 排除，科创板 (688) 纳入。"""
    return code.startswith("8")


# ── 可调参数 ────────────────────────────────────────────────────────────────

# L0
_MIN_VOLUME_WAN: float = 3000.0       # 最低成交额（万元）
_MIN_LISTED_MONTHS: int = 6             # 最短上市月数

# L1a: 快速估值判定（只用腾讯报价已有的 PE/PB，零额外 HTTP）
_PE_TTM_MAX: float = 30.0               # PE(TTM) ≤ 30
_PB_MAX: float = 2.5                    # PB ≤ 2.5
_PE_MIN: float = 3.0                    # PE 太低（<3）可能有财务异常

# L1b: 后置财务验证（仅对有深度分析价值的候选执行）
_MAX_DEBT_RATIO: float = 0.65           # 资产负债率 ≤ 65%
_MAX_DEBT_RATIO_STAR: float = 0.45      # 科创板 ≤ 45%
_MIN_REVENUE_GROWTH: float = 0.0        # 营收同比 > 0%
_MIN_AMPLITUDE_20D: float = 0.02        # 20日振幅 > 2%

# L2
_MAX_CANDIDATES: int = 15
_TENCENT_BATCH_SIZE = 800
_L1B_BATCH_SIZE = 30                    # L1b 验证的候选上限（防耗时过长）

# ── 进度上报 ────────────────────────────────────────────────────────────────

# 每个阶段占整体进度的百分比区间（累进，非磁盘/时间估算，仅供 UI 展示）
_PCT_L0_START = 2       # L0 刚开始（批量报价前）
_PCT_L0_DONE = 15       # L0 完成
_PCT_L1A_DONE = 20      # L1a 完成
_PCT_L1B_DONE = 55      # L1b 完成
_PCT_L2_DONE = 100      # L2 完成

_STAGE_LABELS = {
    "L0": "全市场流动性/ST 筛选",
    "L1a": "PE/PB 快速估值",
    "L1b": "财务验证",
    "L2": "催化剂评分",
    "done": "完成",
}

# 进度回调：接收一个进度快照 dict。
ProgressCb = Callable[[dict[str, Any]], None]
# 个股级回调：(code, name, index_1based, stage_total)
ItemCb = Callable[[str, str, int, int], None]


class _ScanProgress:
    """Accumulates funnel counts and forwards progress snapshots to a callback."""

    def __init__(self, cb: ProgressCb | None, total_stocks: int):
        self._cb = cb
        self.total_stocks = total_stocks
        self.l0_passed = 0
        self.l1a_passed = 0
        self.l1b_passed = 0
        self.l2_passed = 0

    def emit(
        self,
        stage: str,
        percent: float,
        *,
        code: str = "",
        name: str = "",
        stage_index: int = 0,
        stage_total: int = 0,
    ) -> None:
        if self._cb is None:
            return
        snapshot = {
            "stage": stage,
            "stage_label": _STAGE_LABELS.get(stage, stage),
            "total_stocks": self.total_stocks,
            "l0_passed": self.l0_passed,
            "l1a_passed": self.l1a_passed,
            "l1b_passed": self.l1b_passed,
            "l2_passed": self.l2_passed,
            "current_code": code,
            "current_name": name,
            "stage_index": int(stage_index),
            "stage_total": int(stage_total),
            "percent": max(0, min(100, int(percent))),
        }
        try:
            self._cb(snapshot)
        except Exception:  # noqa: BLE001 — progress must never break a scan
            logger.debug("progress callback failed", exc_info=True)


def _interp(lo: float, hi: float, index: int, total: int) -> float:
    """Linear percent within a stage band [lo, hi]."""
    if total <= 0:
        return hi
    return lo + (hi - lo) * (index / total)


# ── 数据结构 ────────────────────────────────────────────────────────────────


@dataclass
class StockInfo:
    code: str
    name: str = ""
    price: float = 0.0
    volume_wan: float = 0.0
    listed_months: int = 999
    pe_ttm: float = 0.0
    pb: float = 0.0
    # L1b 财务（后置填充）
    debt_ratio: float | None = None
    revenue_growth: float | None = None
    amplitude_20d: float | None = None
    # L2 催化剂
    northbound_net_3d: float | None = None
    fund_flow_main_3d: float | None = None
    dragon_tiger_inst_net: float | None = None
    above_ma20: bool = False
    near_ma250: bool = False
    # L2 消息催化剂（新增）
    news_found: bool = False          # 近期是否有重大个股新闻
    hot_topic_match: bool = False     # 是否属于热点题材
    concept_active: bool = False      # 概念板块近期活跃
    signal_score: int = 0
    exclude_reason: str = ""


@dataclass
class ScanResult:
    scan_date: str
    total_stocks: int = 0
    l0_passed: int = 0
    l1a_passed: int = 0
    l1b_passed: int = 0
    l2_passed: int = 0
    candidates: list[StockInfo] = field(default_factory=list)
    duration_seconds: float = 0.0


# ── L0: 全市场种子 ──────────────────────────────────────────────────────────


def _get_all_cn_codes() -> list[str]:
    """从 mootdx 获取全 A 股票代码列表。"""
    _, c2n = _build_name_code_map()
    return sorted(c2n.keys())


def _tencent_volume_wan(price: float, turnover_pct: float, mcap_yi: float) -> float:
    """估算成交额（万元）。"""
    if price <= 0 or turnover_pct <= 0 or mcap_yi <= 0:
        return 0.0
    return mcap_yi * 10000 * (turnover_pct / 100)


def _estimate_listed_months(code: str) -> int:
    """估算上市月数。当前简化实现：默认 999（"已上市"），不阻塞扫描。"""
    return 999


def run_l0_filter() -> list[StockInfo]:
    """L0：全量种子过滤。

    执行：腾讯批量报价（7 批 HTTP，~10s）→ 筛选。
    返回通过 L0 的 StockInfo 列表。
    """
    all_codes = _get_all_cn_codes()
    logger.info("L0: 全量种子 %d 只", len(all_codes))

    all_quotes: dict[str, dict] = {}
    for i in range(0, len(all_codes), _TENCENT_BATCH_SIZE):
        batch = all_codes[i: i + _TENCENT_BATCH_SIZE]
        try:
            all_quotes.update(_tencent_quote(batch))
        except Exception as e:
            logger.warning("L0 腾讯报价 batch %d 失败: %s", i // _TENCENT_BATCH_SIZE, e)
        time.sleep(0.25)

    logger.info("L0: 腾讯报价返回 %d 只", len(all_quotes))

    passed: list[StockInfo] = []
    _, c2n = _build_name_code_map()

    for code in all_codes:
        q = all_quotes.get(code)
        if q is None:
            continue

        name = c2n.get(code, "")
        price = q.get("price", 0)
        pe_ttm = q.get("pe_ttm", 0)
        pb = q.get("pb", 0)
        vol_wan = _tencent_volume_wan(price, q.get("turnover_pct", 0), q.get("mcap_yi", 0))

        info = StockInfo(code=code, name=name, price=price, volume_wan=vol_wan, pe_ttm=pe_ttm, pb=pb)

        if "ST" in name.upper() or "退" in name:
            continue
        if pe_ttm <= 0:
            continue
        if vol_wan < _MIN_VOLUME_WAN:
            continue
        if _is_stock_excluded_by_prefix(code):
            continue

        listed = _estimate_listed_months(code)
        info.listed_months = listed
        if listed < _MIN_LISTED_MONTHS:
            continue

        passed.append(info)

    logger.info("L0: 通过 %d 只", len(passed))
    return passed


# ── L1a: 快速估值判定（零额外 HTTP）───────────────────────────────────────


def run_l1a_filter(stocks: list[StockInfo]) -> list[StockInfo]:
    """L1a：只用腾讯报价已有的 PE/PB 判定。

    条件：3 ≤ PE(TTM) ≤ 30 且 PB ≤ 2.5。
    秒级，不发起任何额外 HTTP。
    """
    passed: list[StockInfo] = []
    for info in stocks:
        if info.pe_ttm < _PE_MIN or info.pe_ttm > _PE_TTM_MAX:
            continue
        if info.pb > _PB_MAX:
            continue
        passed.append(info)
    logger.info("L1a: 通过 %d 只", len(passed))
    return passed


# ── L1b: 财务验证（后置，仅对前 N 只候选）────────────────────────────────

_SINA_SESSION: requests.Session | None = None
def _sina_session() -> requests.Session:
    global _SINA_SESSION
    if _SINA_SESSION is None:
        import requests
        _SINA_SESSION = requests.Session()
        _SINA_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})
    return _SINA_SESSION


def _get_sina_financial(code: str, report_type: str) -> pd.DataFrame:
    """从新浪财经获取财务报表（复用 Session Keep-Alive）。"""
    paper_code = f"{'sh' if code.startswith('6') else 'sz'}{code}"
    source_map = {"利润表": "lrb", "资产负债表": "fzb"}
    source = source_map.get(report_type, "lrb")
    params = {"paperCode": paper_code, "source": source, "type": "0", "page": "1", "num": "20"}
    try:
        r = _sina_session().get(
            "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022",
            params=params,
            timeout=5,
        )
        d = r.json()
        result = d.get("result", {}).get("data", {})
        if not isinstance(result, dict):
            return pd.DataFrame()
        items = result.get(source, [])
        if isinstance(items, list) and items:
            return pd.DataFrame(items)
        report_list = result.get("report_list", {})
        if not report_list:
            return pd.DataFrame()
        rows = []
        for date_str, vals in report_list.items():
            if isinstance(vals, dict):
                vals["报告日"] = date_str
                rows.append(vals)
        return pd.DataFrame(rows) if rows else pd.DataFrame()
    except Exception as e:
        logger.debug("新浪财报失败 %s/%s: %s", code, report_type, e)
        return pd.DataFrame()


def _calc_revenue_growth(code: str) -> float | None:
    """计算最新年报营收同比。"""
    df = _get_sina_financial(code, "利润表")
    if df.empty or "营业收入" not in df.columns:
        return None
    try:
        revenues = [float(r["营业收入"]) for _, r in df.iterrows()
                     if float(r.get("营业收入", 0)) > 0]
        if len(revenues) >= 2:
            return (revenues[0] - revenues[1]) / revenues[1]
    except Exception:
        pass
    return None


def _calc_debt_ratio(code: str) -> float | None:
    """计算最新资产负债率。"""
    df = _get_sina_financial(code, "资产负债表")
    if df.empty:
        return None
    try:
        for _, row in df.iterrows():
            assets = float(row.get("资产总计", 0))
            liab = float(row.get("负债合计", 0))
            if assets > 0:
                return liab / assets
    except Exception:
        pass
    return None


def _calc_amplitude_20d(code: str) -> float | None:
    """20 日日均振幅。"""
    try:
        from tradingagents.dataflows.a_stock import _sina_kline_fallback

        df = _sina_kline_fallback(code)
        if df is None or df.empty or len(df) < 5:
            return None
        recent = df.tail(min(20, len(df)))
        amps = []
        for _, r in recent.iterrows():
            try:
                o, h, l_ = float(r["Open"]), float(r["High"]), float(r["Low"])
                if o > 0:
                    amps.append((h - l_) / o)
            except (TypeError, ValueError):
                continue
        return sum(amps) / len(amps) if amps else None
    except Exception as e:
        logger.debug("振幅失败 %s: %s", code, e)
    return None


def run_l1b_filter(
    stocks: list[StockInfo],
    *,
    on_item: ItemCb | None = None,
) -> list[StockInfo]:
    """L1b：财务深度验证（后置，仅对前 N 只候选）。

    检查营收增长、负债率、振幅。仅当对应字段为 None 时才发起 HTTP。
    ``on_item`` 在每只股票验证前回调，用于进度上报。
    """
    passed: list[StockInfo] = []
    sample = stocks[:_L1B_BATCH_SIZE]
    total = len(sample)
    for idx, info in enumerate(sample, 1):
        if on_item is not None:
            on_item(info.code, info.name, idx, total)
        logger.debug("L1b: %s %s", info.code, info.name)

        # 以下为「尽可能」验证，HTTP 超时/失败不会排除
        if info.revenue_growth is None:
            try:
                info.revenue_growth = _calc_revenue_growth(info.code)
            except Exception:
                info.revenue_growth = None
        if info.revenue_growth is not None and info.revenue_growth < _MIN_REVENUE_GROWTH:
            info.exclude_reason = f"营收增长{info.revenue_growth * 100:.1f}%"
            continue

        if info.debt_ratio is None:
            try:
                info.debt_ratio = _calc_debt_ratio(info.code)
            except Exception:
                info.debt_ratio = None
        max_debt = _MAX_DEBT_RATIO_STAR if info.code.startswith("688") else _MAX_DEBT_RATIO
        if info.debt_ratio is not None and info.debt_ratio > max_debt:
            info.exclude_reason = f"负债率{info.debt_ratio * 100:.1f}%"
            continue

        if info.amplitude_20d is None:
            try:
                info.amplitude_20d = _calc_amplitude_20d(info.code)
            except Exception:
                info.amplitude_20d = None
        if info.amplitude_20d is not None and info.amplitude_20d < _MIN_AMPLITUDE_20D:
            info.exclude_reason = f"振幅{info.amplitude_20d * 100:.1f}%"
            continue

        passed.append(info)

    # L1b 未覆盖的（超过 _L1B_BATCH_SIZE 的候选），直接通过（用 L1a 的结果）
    passed.extend(stocks[_L1B_BATCH_SIZE:])

    logger.info("L1b: 通过 %d 只 (含 %d 只未验证)", len(passed), len(stocks[_L1B_BATCH_SIZE:]))
    return passed


# ── L2: 催化剂信号 ──────────────────────────────────────────────────────────


def _check_northbound_3d(code: str) -> float | None:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        from tradingagents.dataflows.a_stock import get_northbound_flow
        text = get_northbound_flow(today, include_history=True)
        for line in text.split("\n"):
            if "Total=" in line:
                try:
                    return float(line.split("Total=")[1].split()[0])
                except (ValueError, IndexError):
                    continue
    except Exception as e:
        logger.debug("北向失败: %s", e)
    return None


def _check_fund_flow_3d(code: str) -> float | None:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        text = get_fund_flow(code, today, include_history=True)
        for line in text.split("\n"):
            line = line.strip()
            if "主力净流入=" in line:
                try:
                    return float(line.split("主力净流入=")[1].replace("万元", "").strip())
                except (ValueError, IndexError):
                    continue
    except Exception as e:
        logger.debug("资金流失败 %s: %s", code, e)
    return None


def _check_dragon_tiger_institution(code: str) -> float | None:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        from tradingagents.dataflows.a_stock import get_dragon_tiger_board
        text = get_dragon_tiger_board(code, today, look_back_days=30)
        for line in text.split("\n"):
            line = line.strip()
            if "机构买入" in line and "净额" in line:
                try:
                    net = float(line.split("净额")[1].replace("万", "").strip())
                    return net if abs(net) >= 100 else None
                except (ValueError, IndexError):
                    continue
    except Exception as e:
        logger.debug("龙虎榜失败 %s: %s", code, e)
    return None


def _check_ma_support(code: str) -> tuple[bool, bool]:
    try:
        from tradingagents.dataflows.a_stock import _sina_kline_fallback

        df = _sina_kline_fallback(code)
        if df is None or df.empty or len(df) < 20:
            return (False, False)
        closes = pd.to_numeric(df["Close"], errors="coerce").dropna().values
        if len(closes) < 20:
            return (False, False)
        cp = float(closes[-1])
        ma20 = float(closes[-20:].mean())
        above = cp >= ma20 * 0.98
        near250 = False
        if len(closes) >= 250:
            ma250 = float(closes[-250:].mean())
            if ma250 > 0:
                near250 = abs(cp - ma250) / ma250 < 0.08
        return (above, near250)
    except Exception as e:
        logger.debug("均线失败 %s: %s", code, e)
    return (False, False)


# ── L2 消息催化剂 ────────────────────────────────────────────────────────────

_CACHED_HOT_STOCKS: tuple[str, dict[str, list[str]]] | None = None
"""缓存当天的同花顺热股题材，(date, {题材标签: [股票代码, ...]})。"""

_CACHED_GLOBAL_NEWS: tuple[str, list[str]] | None = None
"""缓存当天财联社快讯标题，(date, [title, ...])。"""


def _cache_date() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load_hot_stocks() -> dict[str, list[str]]:
    """获取当天同花顺热股题材，返回 {题材标签: [股票代码, ...]}。"""
    global _CACHED_HOT_STOCKS
    today = _cache_date()
    if _CACHED_HOT_STOCKS is not None and _CACHED_HOT_STOCKS[0] == today:
        return _CACHED_HOT_STOCKS[1]

    from tradingagents.dataflows.a_stock import get_hot_stocks

    try:
        text = get_hot_stocks(today)
        result: dict[str, list[str]] = {}
        for line in text.split("\n"):
            line = line.strip()
            if "|" not in line:
                continue
            parts = line.split("|")
            if len(parts) < 2:
                continue
            code_name = parts[0].strip()
            tags_str = parts[1].strip()
            code = code_name.split()[0] if code_name else ""
            if not code or not tags_str:
                continue
            for tag in tags_str.split("+"):
                tag = tag.strip()
                if tag:
                    result.setdefault(tag, []).append(code)
        _CACHED_HOT_STOCKS = (today, result)
        logger.info("消息催化剂: 加载 %d 个热股题材", len(result))
        return result
    except Exception:
        logger.debug("热股题材加载失败", exc_info=True)
        # 不缓存空结果，下次重试
        return {}


def _load_global_news() -> list[str]:
    """获取当天财联社快讯标题列表。"""
    global _CACHED_GLOBAL_NEWS
    today = _cache_date()
    if _CACHED_GLOBAL_NEWS is not None and _CACHED_GLOBAL_NEWS[0] == today:
        return _CACHED_GLOBAL_NEWS[1]

    import requests

    try:
        url = "https://www.cls.cn/nodeapi/telegraphList"
        r = requests.get(
            url,
            params={"rn": "30", "page": "1"},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.cls.cn/"},
            timeout=8,
        )
        d = r.json()
        result: list[str] = []
        for item in d.get("data", {}).get("roll_data", []):
            title = item.get("title", "") or item.get("brief", "")
            if title:
                result.append(title)
        _CACHED_GLOBAL_NEWS = (today, result)
        logger.info("消息催化剂: 加载 %d 条财联社快讯", len(result))
        return result
    except Exception:
        logger.debug("财联社快讯加载失败", exc_info=True)
        return []


def _check_news_catalyst(code: str) -> bool:
    """检查个股是否有近期重大新闻（东财个股新闻）。"""
    from tradingagents.dataflows.a_stock import get_news

    today = datetime.now().strftime("%Y-%m-%d")
    # 看近 3 天
    from datetime import timedelta
    start = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    try:
        text = get_news(code, start, today)
        if text and "No news" not in text:
            return True
    except Exception:
        pass
    return False


def _check_hot_topic_match(code: str, hot_stocks: dict[str, list[str]]) -> bool:
    """检查个股是否出现在当天热股题材中。"""
    for stocks_in_tag in hot_stocks.values():
        if code in stocks_in_tag:
            return True
    return False


def _check_concept_catalyst(code: str, global_news: list[str], hot_stocks: dict[str, list[str]]) -> bool:
    """检查概念板块活跃度：若有新能源/半导体/AI等热门概念相关新闻则活跃。

    兜底：如果财联社无快讯但热股题材有数据，说明市场仍存在活跃概念。
    """
    HOT_CONCEPT_KEYWORDS = {"新能源", "半导体", "人工智能", "AI", "芯片",
                            "机器人", "低空经济", "量子", "算力", "数据要素",
                            "创新药", "光伏", "储能", "无人驾驶", "消费电子"}
    # 快讯中有热门概念词
    for title in global_news:
        for kw in HOT_CONCEPT_KEYWORDS:
            if kw in title:
                return True
    # 兜底：快讯为空但有热股题材 → 市场仍有概念活跃
    if not global_news and hot_stocks:
        return True
    return False


def run_l2_filter_impl(stocks: list[StockInfo], max_candidates: int = _MAX_CANDIDATES) -> list[StockInfo]:
    """L2 纯逻辑评分。"""
    scored: list[StockInfo] = []
    for info in stocks:
        s = 0
        if info.northbound_net_3d and info.northbound_net_3d > 0:
            s += 1
        if info.fund_flow_main_3d and info.fund_flow_main_3d > 0:
            s += 1
        if info.dragon_tiger_inst_net and info.dragon_tiger_inst_net >= 100:
            s += 1
        if info.above_ma20:
            s += 1
        if info.near_ma250:
            s += 1
        # 消息催化剂
        if info.news_found:
            s += 1
        if info.hot_topic_match:
            s += 1
        if info.concept_active:
            s += 1
        info.signal_score = s
        scored.append(info)
    scored.sort(key=lambda x: x.signal_score, reverse=True)
    return scored[:max_candidates]


def _safe_call(fn, code, default=None):
    """安全调用网络函数，任何异常返回 default。"""
    try:
        return fn(code)
    except Exception as e:
        logger.debug("网络调用失败 %s: %s", code, e)
        return default


def _is_stock_code(code: str) -> bool:
    """判断是否为真正的 A 股个股代码（非指数、非板块）。"""
    # 指数：000001(上证), 399xxx(深证/中小板/创业板指数)
    if code == "000001":  # 上证指数
        return False
    if code.startswith("399"):  # 各类深证指数
        return False
    if len(code) != 6:
        return False
    # 标准 A 股：60xxxx(沪), 00xxxx(深主板), 30xxxx(创业板), 68xxxx(科创板)
    first_two = code[:2]
    if first_two in ("60", "68", "30"):
        return True
    if first_two.startswith("00"):
        return True
    return False


def run_l2_filter(
    stocks: list[StockInfo],
    max_candidates: int = _MAX_CANDIDATES,
    *,
    on_item: ItemCb | None = None,
) -> list[StockInfo]:
    """L2 含网络调用。仅对成交量前 80 只 A 股个股执行 HTTP 检测。

    包含消息催化剂：
    - 个股新闻检测（东财新闻，近 3 天）
    - 热点题材匹配（同花顺热股）
    - 概念板块活跃度（财联社快讯关键词）
    ``on_item`` 在每只股票检测前回调，用于进度上报。
    """
    _L2_PROCESS_LIMIT = 80

    real_stocks = [s for s in stocks if _is_stock_code(s.code)]
    ranked = sorted(real_stocks, key=lambda s: s.volume_wan, reverse=True)
    to_process = ranked[:_L2_PROCESS_LIMIT]

    # 共享数据：一次性加载
    northbound_val = _safe_call(_check_northbound_3d, "ALL")
    hot_stocks = _load_hot_stocks()
    global_news = _load_global_news()

    # 个股级别检测
    total = len(to_process)
    for idx, info in enumerate(to_process, 1):
        if on_item is not None:
            on_item(info.code, info.name, idx, total)
        logger.debug("L2 HTTP: %s %s", info.code, info.name)

        info.northbound_net_3d = northbound_val
        info.above_ma20, info.near_ma250 = _safe_call(_check_ma_support, info.code, default=(False, False))

        # 消息催化剂
        info.news_found = _safe_call(_check_news_catalyst, info.code, default=False)
        info.hot_topic_match = _check_hot_topic_match(info.code, hot_stocks)
        info.concept_active = _check_concept_catalyst(info.code, global_news, hot_stocks)

        # 以下暂不启用（东财 SSL 代理问题）
        # info.fund_flow_main_3d = _safe_call(_check_fund_flow_3d, info.code)
        # info.dragon_tiger_inst_net = _safe_call(_check_dragon_tiger_institution, info.code)

    return run_l2_filter_impl(stocks, max_candidates=max_candidates)


# ── 扫描入口 ──────────────────────────────────────────────────────────────


def run_value_swing_scan(
    max_candidates: int = _MAX_CANDIDATES,
    *,
    progress_cb: ProgressCb | None = None,
) -> ScanResult:
    """执行一轮完整扫描。

    ``progress_cb`` 若提供，会在各阶段切换与个股验证时收到进度快照
    （stage / 各阶通过数 / 当前个股 / 百分比）。
    """
    ts = time.time()
    scan_date = datetime.now().strftime("%Y-%m-%d")
    result = ScanResult(scan_date=scan_date)
    logger.info("═══ 价值波段扫描 %s ═══", scan_date)

    result.total_stocks = len(_get_all_cn_codes())
    prog = _ScanProgress(progress_cb, result.total_stocks)
    prog.emit("L0", _PCT_L0_START)

    l0 = run_l0_filter()
    result.l0_passed = len(l0)
    prog.l0_passed = len(l0)
    prog.emit("L0", _PCT_L0_DONE)

    l1a = run_l1a_filter(l0)
    result.l1a_passed = len(l1a)
    prog.l1a_passed = len(l1a)
    prog.emit("L1a", _PCT_L1A_DONE)

    def _on_l1b(code: str, name: str, idx: int, total: int) -> None:
        prog.emit(
            "L1b",
            _interp(_PCT_L1A_DONE, _PCT_L1B_DONE, idx, total),
            code=code,
            name=name,
            stage_index=idx,
            stage_total=total,
        )

    l1b = run_l1b_filter(l1a, on_item=_on_l1b)
    result.l1b_passed = len(l1b)
    prog.l1b_passed = len(l1b)
    prog.emit("L1b", _PCT_L1B_DONE)

    def _on_l2(code: str, name: str, idx: int, total: int) -> None:
        prog.emit(
            "L2",
            _interp(_PCT_L1B_DONE, _PCT_L2_DONE, idx, total),
            code=code,
            name=name,
            stage_index=idx,
            stage_total=total,
        )

    l2 = run_l2_filter(l1b, max_candidates=max_candidates, on_item=_on_l2)
    result.l2_passed = len(l2)
    result.candidates = l2
    prog.l2_passed = len(l2)
    prog.emit("done", _PCT_L2_DONE)

    result.duration_seconds = time.time() - ts
    logger.info("═══ %d→%d→%d→%d 候选, %.0fs ═══",
                result.l0_passed, result.l1a_passed, result.l1b_passed,
                result.l2_passed, result.duration_seconds)
    return result


# ── 推荐映射 ──────────────────────────────────────────────────────────────


SCAN_RECOMMENDATION_STRONG = "强烈推荐"
SCAN_RECOMMENDATION_BUY = "推荐"
SCAN_RECOMMENDATION_WATCH = "关注"


def map_scan_recommendation(pm_rating: str, signal_score: int) -> str:
    rating_upper = pm_rating.strip().upper()
    if rating_upper in ("BUY",) and signal_score >= 4:
        return SCAN_RECOMMENDATION_STRONG
    if rating_upper in ("BUY", "OVERWEIGHT") and signal_score >= 2:
        return SCAN_RECOMMENDATION_BUY
    if rating_upper == "HOLD" and signal_score >= 3:
        return SCAN_RECOMMENDATION_WATCH
    return ""


def build_scan_summary_row(info: StockInfo, recommendation: str) -> dict[str, Any]:
    return {
        "code": info.code,
        "name": info.name,
        "recommendation": recommendation,
        "signal_score": info.signal_score,
        "pe_ttm": round(info.pe_ttm, 1),
        "pb": round(info.pb, 2),
        "price": info.price,
        "debt_ratio": round(info.debt_ratio * 100, 1) if info.debt_ratio else None,
        "revenue_growth": round(info.revenue_growth * 100, 1) if info.revenue_growth else None,
        "above_ma20": info.above_ma20,
        "near_ma250": info.near_ma250,
        "news_found": info.news_found,
        "hot_topic_match": info.hot_topic_match,
        "concept_active": info.concept_active,
    }
