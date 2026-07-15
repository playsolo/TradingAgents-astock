"""成长加速选股策略 — 与价值波段并行的第二套漏斗。

L0 流动性（允许亏损）→ L1a 成交额预筛 → L1b 绝对利润/亏损拐点
→ L2 轻前景加权，输出 top 15。

不设 PE/PB 硬顶；主排序键为归母净利 TTM 增速。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from tradingagents.dataflows.a_stock import _build_name_code_map
from tradingagents.strategies.value_swing import (
    _MIN_LISTED_MONTHS,
    _TENCENT_BATCH_SIZE,
    _estimate_listed_months,
    _get_all_cn_codes,
    _is_stock_code,
    _is_stock_excluded_by_prefix,
    _load_hot_stocks,
    _tencent_volume_wan,
)

logger = logging.getLogger(__name__)

# ── 可调参数（与 PRD 定稿对齐）──────────────────────────────────────────────

_MIN_VOLUME_WAN: float = 3000.0
_L1_PROCESS_LIMIT: int = 120          # 仅对成交额前 N 只做财报深挖
_MAX_CANDIDATES: int = 15

_MIN_NP_YOY: float = 0.50             # 已盈利：TTM 归母净利 YoY ≥ 50%
_STRONG_NP_YOY: float = 1.00          # ≥100% 强信号
_MIN_NP_ABS: float = 1e8              # 归母净利 TTM ≥ 1 亿元

_MIN_REV_YOY_LOSS: float = 0.30       # 亏损轨：营收 TTM YoY ≥ 30%
_MIN_REV_ABS_LOSS: float = 1e9        # 亏损轨：营收 TTM ≥ 10 亿元
_MIN_LOSS_NARROW: float = 0.30        # 亏损额同比收窄 ≥ 30%

_NP_COL = "归属于母公司所有者的净利润"
_REV_COL = "营业收入"

_GROWTH_THEME_KEYWORDS = (
    "算力", "光通信", "光模块", "人工智能", "AI", "半导体", "芯片",
    "机器人", "液冷", "服务器", "先进封装", "CPO", "新能源", "创新药",
)

ProgressCb = Callable[[dict[str, Any]], None]
ItemCb = Callable[[str, str, int, int], None]

_PCT_L0_START = 2
_PCT_L0_DONE = 15
_PCT_L1A_DONE = 25
_PCT_L1B_DONE = 80
_PCT_L2_DONE = 100

_STAGE_LABELS = {
    "L0": "全市场流动性/ST 筛选（含亏损）",
    "L1a": "成交额预筛",
    "L1b": "利润增速 / 亏损拐点",
    "L2": "前景轻加权排序",
    "done": "完成",
}


@dataclass
class GrowthStockInfo:
    code: str
    name: str = ""
    price: float = 0.0
    volume_wan: float = 0.0
    listed_months: int = 999
    pe_ttm: float = 0.0
    pb: float = 0.0
    # TTM 指标
    np_ttm: float | None = None
    np_ttm_prior: float | None = None
    np_ttm_yoy: float | None = None
    rev_ttm: float | None = None
    rev_ttm_prior: float | None = None
    rev_ttm_yoy: float | None = None
    # 分类与加权
    track: str = ""  # "profit" | "loss"
    turnaround: bool = False
    loss_narrowed: bool = False
    revenue_accel: bool = False
    growth_theme: bool = False
    high_liquidity: bool = False
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
    candidates: list[GrowthStockInfo] = field(default_factory=list)
    duration_seconds: float = 0.0
    strategy: str = "growth_accel"


def selection_rules_snapshot() -> dict[str, Any]:
    return {
        "strategy": "growth_accel",
        "name": "成长加速",
        "l0": [
            "剔除 ST/退市标识",
            f"成交额 ≥ {_MIN_VOLUME_WAN:g} 万元",
            "允许 PE≤0（亏损可入池）",
            "不设 PE/PB 上限",
            "排除北交所 8xxx",
            f"上市 ≥ {_MIN_LISTED_MONTHS} 个月",
        ],
        "l1a": [
            f"按成交额取前 {_L1_PROCESS_LIMIT} 只做财报深挖",
        ],
        "l1": [
            f"已盈利：归母净利 TTM YoY ≥ {_MIN_NP_YOY * 100:g}% 且绝对额 ≥ {_MIN_NP_ABS / 1e8:g} 亿",
            f"强信号：净利 TTM YoY ≥ {_STRONG_NP_YOY * 100:g}%",
            f"亏损成长：营收 TTM YoY ≥ {_MIN_REV_YOY_LOSS * 100:g}% 且营收 ≥ {_MIN_REV_ABS_LOSS / 1e8:g} 亿，"
            f"且亏损收窄 ≥ {_MIN_LOSS_NARROW * 100:g}% 或由亏转盈",
            "指标缺失 → 不入池（fail-closed）",
        ],
        "l1b": [
            "同 L1 增长核（见左列细化）",
        ],
        "l2": {
            "score_max": 6,
            "active": [
                "净利TTM高增",
                "强增速≥100%",
                "亏损拐点/收窄",
                "近季加速",
                "成长题材",
                "高流动性",
            ],
            "dormant": [],
            "top_n": _MAX_CANDIDATES,
            "note": "主排序：信号分 → 净利TTM增速；不设估值硬顶",
        },
    }


def l2_score_max(*, only_active: bool = True) -> int:
    return 6


# ── TTM 计算（国内报表为累计 YTD）────────────────────────────────────────────


def _lookup_ytd(series: pd.Series, year: int, month: int) -> float | None:
    for ts, val in series.items():
        if int(ts.year) == year and int(ts.month) == month:
            try:
                return float(val)
            except (TypeError, ValueError):
                return None
    return None


def compute_ttm_pair(
    ytd: pd.Series,
) -> tuple[float | None, float | None, float | None]:
    """从累计口径季报序列计算 (TTM, 上年同期 TTM, YoY)。

    桥接：最新为 Q 月时，TTM = 上年年报 + 本年 YTD(Q) − 上年 YTD(Q)。
    prior≤0 时 YoY 记为 None（由增长轨判定转正/收窄）。
    """
    if ytd is None or ytd.empty:
        return None, None, None
    s = ytd.dropna().sort_index(ascending=False)
    if s.empty:
        return None, None, None

    latest_ts = pd.Timestamp(s.index[0])
    latest_val = float(s.iloc[0])
    y, m = int(latest_ts.year), int(latest_ts.month)

    if m == 12:
        ttm = latest_val
        prior = _lookup_ytd(s, y - 1, 12)
    else:
        fy_prev = _lookup_ytd(s, y - 1, 12)
        ytd_prev = _lookup_ytd(s, y - 1, m)
        if fy_prev is None or ytd_prev is None:
            return None, None, None
        ttm = fy_prev + latest_val - ytd_prev
        fy_prev2 = _lookup_ytd(s, y - 2, 12)
        ytd_prev2 = _lookup_ytd(s, y - 2, m)
        if fy_prev2 is None or ytd_prev2 is None:
            prior = None
        else:
            prior = fy_prev2 + ytd_prev - ytd_prev2

    if ttm is None:
        return None, None, None

    yoy: float | None
    if prior is None or prior <= 0:
        yoy = None
    else:
        yoy = (ttm - prior) / prior
    return ttm, prior, yoy


def evaluate_growth_track(
    *,
    np_ttm: float | None,
    np_yoy: float | None,
    rev_ttm: float | None,
    rev_yoy: float | None,
    np_prior: float | None,
) -> tuple[bool, str, str]:
    """返回 (是否入池, track, exclude_reason)。"""
    if np_ttm is None or rev_ttm is None:
        return False, "", "指标缺失"

    if np_ttm > 0:
        track = "profit"
        if np_ttm < _MIN_NP_ABS:
            return False, track, "利润规模不足"
        # 由亏转盈：直接放行（绝对值已过门槛）
        if np_prior is not None and np_prior <= 0:
            return True, track, ""
        if np_yoy is None or np_yoy < _MIN_NP_YOY:
            return False, track, f"净利增速不足"
        return True, track, ""

    track = "loss"
    if rev_ttm < _MIN_REV_ABS_LOSS:
        return False, track, "营收规模不足"
    if rev_yoy is None or rev_yoy < _MIN_REV_YOY_LOSS:
        return False, track, "营收增速不足"
    if np_prior is not None and np_prior < 0 and np_ttm < 0:
        narrow = (abs(np_prior) - abs(np_ttm)) / abs(np_prior)
        if narrow >= _MIN_LOSS_NARROW:
            return True, track, ""
    return False, track, "亏损未明显收窄"


def compute_growth_signal_score(info: GrowthStockInfo) -> int:
    s = 0
    if info.track == "profit":
        if info.np_ttm_yoy is not None and info.np_ttm_yoy >= _STRONG_NP_YOY:
            s += 3
        elif info.np_ttm_yoy is not None and info.np_ttm_yoy >= _MIN_NP_YOY:
            s += 2
        elif info.turnaround:
            s += 3
        else:
            s += 1
    elif info.track == "loss":
        if info.loss_narrowed or info.turnaround:
            s += 2
        else:
            s += 1
    if info.revenue_accel:
        s += 1
    if info.growth_theme:
        s += 1
    if info.high_liquidity:
        s += 1
    return s


def why_selected_line(candidate: GrowthStockInfo | dict[str, Any]) -> str:
    if isinstance(candidate, dict):
        track = candidate.get("track") or ""
        yoy = candidate.get("np_ttm_yoy")
        bits = []
        if track == "profit" and yoy is not None:
            bits.append(f"净利TTM {float(yoy) * 100:.0f}%")
        elif track == "loss":
            bits.append("亏损收窄/成长")
        if candidate.get("turnaround"):
            bits.append("由亏转盈")
        if candidate.get("revenue_accel"):
            bits.append("近季加速")
        if candidate.get("growth_theme"):
            bits.append("成长题材")
        if candidate.get("high_liquidity"):
            bits.append("高流动性")
        return " · ".join(bits) if bits else "成长加速入池"
    bits: list[str] = []
    if candidate.track == "profit" and candidate.np_ttm_yoy is not None:
        bits.append(f"净利TTM {candidate.np_ttm_yoy * 100:.0f}%")
    elif candidate.track == "loss":
        bits.append("亏损收窄/成长")
    if candidate.turnaround:
        bits.append("由亏转盈")
    if candidate.revenue_accel:
        bits.append("近季加速")
    if candidate.growth_theme:
        bits.append("成长题材")
    if candidate.high_liquidity:
        bits.append("高流动性")
    return " · ".join(bits) if bits else "成长加速入池"


def l2_factor_hits(candidate: GrowthStockInfo | dict[str, Any]) -> list[dict[str, Any]]:
    def _get(key: str, default=False):
        if isinstance(candidate, dict):
            return candidate.get(key, default)
        return getattr(candidate, key, default)

    yoy = _get("np_ttm_yoy", None)
    track = _get("track", "")
    return [
        {
            "key": "np_growth",
            "label": "净利TTM高增",
            "active": True,
            "hit": track == "profit" and yoy is not None and float(yoy) >= _MIN_NP_YOY,
        },
        {
            "key": "strong_growth",
            "label": "强增速≥100%",
            "active": True,
            "hit": yoy is not None and float(yoy) >= _STRONG_NP_YOY,
        },
        {
            "key": "loss_inflection",
            "label": "亏损拐点/收窄",
            "active": True,
            "hit": bool(_get("loss_narrowed") or _get("turnaround")),
        },
        {
            "key": "revenue_accel",
            "label": "近季加速",
            "active": True,
            "hit": bool(_get("revenue_accel")),
        },
        {
            "key": "growth_theme",
            "label": "成长题材",
            "active": True,
            "hit": bool(_get("growth_theme")),
        },
        {
            "key": "high_liquidity",
            "label": "高流动性",
            "active": True,
            "hit": bool(_get("high_liquidity")),
        },
    ]


# ── 进度 ────────────────────────────────────────────────────────────────────


class _ScanProgress:
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
        except Exception:  # noqa: BLE001
            logger.debug("growth progress callback failed", exc_info=True)


def _interp(lo: float, hi: float, index: int, total: int) -> float:
    if total <= 0:
        return hi
    return lo + (hi - lo) * (index / total)


# ── L0 / L1a ────────────────────────────────────────────────────────────────


def run_l0_filter() -> list[GrowthStockInfo]:
    """L0：流动性 + 非 ST；允许 PE≤0。"""
    from tradingagents.dataflows.a_stock import _tencent_quote

    all_codes = _get_all_cn_codes()
    logger.info("成长 L0: 全量种子 %d 只", len(all_codes))

    all_quotes: dict[str, dict] = {}
    for i in range(0, len(all_codes), _TENCENT_BATCH_SIZE):
        batch = all_codes[i: i + _TENCENT_BATCH_SIZE]
        try:
            all_quotes.update(_tencent_quote(batch))
        except Exception as e:
            logger.warning("成长 L0 腾讯报价 batch 失败: %s", e)
        time.sleep(0.25)

    passed: list[GrowthStockInfo] = []
    _, c2n = _build_name_code_map()

    for code in all_codes:
        q = all_quotes.get(code)
        if q is None:
            continue
        name = c2n.get(code, "")
        price = q.get("price", 0)
        pe_ttm = q.get("pe_ttm", 0) or 0
        pb = q.get("pb", 0) or 0
        vol_wan = _tencent_volume_wan(price, q.get("turnover_pct", 0), q.get("mcap_yi", 0))
        info = GrowthStockInfo(
            code=code, name=name, price=price, volume_wan=vol_wan, pe_ttm=pe_ttm, pb=pb
        )
        if "ST" in name.upper() or "退" in name:
            continue
        if vol_wan < _MIN_VOLUME_WAN:
            continue
        if _is_stock_excluded_by_prefix(code):
            continue
        if not _is_stock_code(code):
            continue
        listed = _estimate_listed_months(code)
        info.listed_months = listed
        if listed < _MIN_LISTED_MONTHS:
            continue
        passed.append(info)

    logger.info("成长 L0: 通过 %d 只", len(passed))
    return passed


def run_l1a_prefilter(stocks: list[GrowthStockInfo]) -> list[GrowthStockInfo]:
    ranked = sorted(stocks, key=lambda s: s.volume_wan, reverse=True)
    out = ranked[:_L1_PROCESS_LIMIT]
    logger.info("成长 L1a: 预筛 %d 只", len(out))
    return out


# ── 财报拉取与 L1b ───────────────────────────────────────────────────────────


def _series_from_income(df: pd.DataFrame, col: str) -> pd.Series:
    if df is None or df.empty or col not in df.columns or "报告日" not in df.columns:
        return pd.Series(dtype=float)
    dates = pd.to_datetime(df["报告日"], errors="coerce")
    vals = pd.to_numeric(df[col], errors="coerce")
    s = pd.Series(vals.values, index=dates)
    return s.dropna()


def _fetch_income_metrics(code: str) -> dict[str, float | None | bool]:
    from tradingagents.dataflows.a_stock import _get_financial_report_sina

    df = _get_financial_report_sina(code, "利润表", "quarterly", limit=16)
    np_s = _series_from_income(df, _NP_COL)
    rev_s = _series_from_income(df, _REV_COL)
    np_ttm, np_prior, np_yoy = compute_ttm_pair(np_s)
    rev_ttm, rev_prior, rev_yoy = compute_ttm_pair(rev_s)

    # 近季加速：最新 YTD 同比（若报表带不到 item_tongbi，用同月上年 YTD）
    accel = False
    if not rev_s.empty and len(rev_s) >= 2:
        latest_ts = pd.Timestamp(rev_s.sort_index(ascending=False).index[0])
        cur = _lookup_ytd(rev_s, int(latest_ts.year), int(latest_ts.month))
        prev = _lookup_ytd(rev_s, int(latest_ts.year) - 1, int(latest_ts.month))
        if cur is not None and prev is not None and prev > 0 and rev_yoy is not None:
            near_yoy = (cur - prev) / prev
            accel = near_yoy > rev_yoy + 0.05

    return {
        "np_ttm": np_ttm,
        "np_ttm_prior": np_prior,
        "np_ttm_yoy": np_yoy,
        "rev_ttm": rev_ttm,
        "rev_ttm_prior": rev_prior,
        "rev_ttm_yoy": rev_yoy,
        "revenue_accel": accel,
    }


def run_l1_growth_filter_impl(stocks: list[GrowthStockInfo]) -> list[GrowthStockInfo]:
    """纯判定（测试用）：假定 TTM 字段已填好。"""
    passed: list[GrowthStockInfo] = []
    for info in stocks:
        ok, track, reason = evaluate_growth_track(
            np_ttm=info.np_ttm,
            np_yoy=info.np_ttm_yoy,
            rev_ttm=info.rev_ttm,
            rev_yoy=info.rev_ttm_yoy,
            np_prior=info.np_ttm_prior,
        )
        if not ok:
            info.exclude_reason = reason
            continue
        info.track = track
        info.turnaround = (
            info.np_ttm is not None
            and info.np_ttm > 0
            and info.np_ttm_prior is not None
            and info.np_ttm_prior <= 0
        )
        if (
            track == "loss"
            and info.np_ttm is not None
            and info.np_ttm_prior is not None
            and info.np_ttm < 0
            and info.np_ttm_prior < 0
        ):
            narrow = (abs(info.np_ttm_prior) - abs(info.np_ttm)) / abs(info.np_ttm_prior)
            info.loss_narrowed = narrow >= _MIN_LOSS_NARROW
        passed.append(info)
    return passed


def run_l1b_filter(
    stocks: list[GrowthStockInfo],
    *,
    on_item: ItemCb | None = None,
) -> list[GrowthStockInfo]:
    passed: list[GrowthStockInfo] = []
    total = len(stocks)
    for idx, info in enumerate(stocks, 1):
        if on_item is not None:
            on_item(info.code, info.name, idx, total)
        try:
            metrics = _fetch_income_metrics(info.code)
        except Exception as e:
            logger.debug("成长 L1b 财报失败 %s: %s", info.code, e)
            info.exclude_reason = "指标缺失"
            continue
        info.np_ttm = metrics["np_ttm"]  # type: ignore[assignment]
        info.np_ttm_prior = metrics["np_ttm_prior"]  # type: ignore[assignment]
        info.np_ttm_yoy = metrics["np_ttm_yoy"]  # type: ignore[assignment]
        info.rev_ttm = metrics["rev_ttm"]  # type: ignore[assignment]
        info.rev_ttm_prior = metrics["rev_ttm_prior"]  # type: ignore[assignment]
        info.rev_ttm_yoy = metrics["rev_ttm_yoy"]  # type: ignore[assignment]
        info.revenue_accel = bool(metrics["revenue_accel"])

        ok, track, reason = evaluate_growth_track(
            np_ttm=info.np_ttm,
            np_yoy=info.np_ttm_yoy,
            rev_ttm=info.rev_ttm,
            rev_yoy=info.rev_ttm_yoy,
            np_prior=info.np_ttm_prior,
        )
        if not ok:
            info.exclude_reason = reason
            continue
        info.track = track
        info.turnaround = (
            info.np_ttm is not None
            and info.np_ttm > 0
            and info.np_ttm_prior is not None
            and info.np_ttm_prior <= 0
        )
        if (
            track == "loss"
            and info.np_ttm is not None
            and info.np_ttm_prior is not None
            and info.np_ttm < 0
            and info.np_ttm_prior < 0
        ):
            narrow = (abs(info.np_ttm_prior) - abs(info.np_ttm)) / abs(info.np_ttm_prior)
            info.loss_narrowed = narrow >= _MIN_LOSS_NARROW
        passed.append(info)
        time.sleep(0.05)

    logger.info("成长 L1b: 通过 %d 只", len(passed))
    return passed


# ── L2 ──────────────────────────────────────────────────────────────────────


def _check_growth_theme(code: str, hot_stocks: dict[str, list[str]]) -> bool:
    for tag, codes in hot_stocks.items():
        if code not in codes:
            continue
        if any(kw in tag for kw in _GROWTH_THEME_KEYWORDS):
            return True
    return False


def run_l2_rank_impl(
    stocks: list[GrowthStockInfo],
    max_candidates: int = _MAX_CANDIDATES,
) -> list[GrowthStockInfo]:
    scored: list[GrowthStockInfo] = []
    for info in stocks:
        info.signal_score = compute_growth_signal_score(info)
        scored.append(info)
    scored.sort(
        key=lambda x: (
            x.signal_score,
            x.np_ttm_yoy if x.np_ttm_yoy is not None else -999.0,
            x.volume_wan,
        ),
        reverse=True,
    )
    return scored[:max_candidates]


def run_l2_filter(
    stocks: list[GrowthStockInfo],
    max_candidates: int = _MAX_CANDIDATES,
    *,
    on_item: ItemCb | None = None,
) -> list[GrowthStockInfo]:
    hot = _load_hot_stocks()
    if stocks:
        vol_cut = sorted((s.volume_wan for s in stocks), reverse=True)
        mid = vol_cut[len(vol_cut) // 2] if vol_cut else 0.0
    else:
        mid = 0.0

    total = len(stocks)
    for idx, info in enumerate(stocks, 1):
        if on_item is not None:
            on_item(info.code, info.name, idx, total)
        info.growth_theme = _check_growth_theme(info.code, hot)
        info.high_liquidity = info.volume_wan >= mid
    return run_l2_rank_impl(stocks, max_candidates=max_candidates)


# ── 扫描入口 ────────────────────────────────────────────────────────────────


def run_growth_accel_scan(
    max_candidates: int = _MAX_CANDIDATES,
    *,
    progress_cb: ProgressCb | None = None,
) -> ScanResult:
    ts = time.time()
    scan_date = datetime.now().strftime("%Y-%m-%d")
    result = ScanResult(scan_date=scan_date)
    logger.info("═══ 成长加速扫描 %s ═══", scan_date)

    result.total_stocks = len(_get_all_cn_codes())
    prog = _ScanProgress(progress_cb, result.total_stocks)
    prog.emit("L0", _PCT_L0_START)

    l0 = run_l0_filter()
    result.l0_passed = len(l0)
    prog.l0_passed = len(l0)
    prog.emit("L0", _PCT_L0_DONE)

    l1a = run_l1a_prefilter(l0)
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
    logger.info(
        "═══ 成长 %d→%d→%d→%d 候选, %.0fs ═══",
        result.l0_passed,
        result.l1a_passed,
        result.l1b_passed,
        result.l2_passed,
        result.duration_seconds,
    )
    return result
