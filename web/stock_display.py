"""Helpers for displaying A-share stock identifiers in the web UI."""

from __future__ import annotations

import json
import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any


def _clean_stock_name(name: str) -> str:
    return "".join(ch for ch in str(name) if ch.isprintable()).strip()


# Well-known Chinese ADRs / US listings — overlay Chinese display names.
_US_CN_ALIASES: dict[str, str] = {
    "MNSO": "名创优品",
    "BABA": "阿里巴巴",
    "JD": "京东",
    "PDD": "拼多多",
    "BIDU": "百度",
    "NIO": "蔚来",
    "XPEV": "小鹏汽车",
    "LI": "理想汽车",
    "BILI": "哔哩哔哩",
    "TME": "腾讯音乐",
    "NTES": "网易",
    "IQ": "爱奇艺",
    "ZTO": "中通快递",
    "YUMC": "百胜中国",
    "HTHT": "华住集团",
    "TCOM": "携程",
    "BEKE": "贝壳",
    "FUTU": "富途",
    "TIGR": "老虎证券",
}


_EXCHANGE_LABEL_MARKERS: tuple[str, ...] = (
    "纽约证券交易所",
    "纽约证券",
    "纳斯达克",
    "纽交所",
    "美交所",
    "港交所",
    "上交所",
    "深交所",
    "北交所",
    "证券交易所",
    "交易所",
    "NYSE",
    "NASDAQ",
    "AMEX",
    "NYQ",
    "NMS",
    "HKEX",
    "SSE",
    "SZSE",
    "BSE",
)


class StockNameCache:
    """代码→中文名本地 JSON 缓存（默认 ~/.tradingagents/stock_names.json）。"""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else (
            Path.home() / ".tradingagents" / "stock_names.json"
        )
        self._lock = threading.RLock()
        self._mem: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._mem is not None:
            return self._mem
        if not self.path.exists():
            self._mem = {}
            return self._mem
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            names = data.get("names") if isinstance(data, dict) else {}
            self._mem = {
                str(k).upper(): _clean_stock_name(str(v))
                for k, v in (names or {}).items()
                if str(k).strip() and str(v).strip()
            }
        except (OSError, json.JSONDecodeError, TypeError):
            self._mem = {}
        return self._mem

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "names": self._load()}
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.replace(self.path)

    def get(self, code: str) -> str | None:
        code = str(code or "").strip().upper()
        if not code:
            return None
        with self._lock:
            name = self._load().get(code)
            return name or None

    def set(self, code: str, name: str) -> None:
        code = str(code or "").strip().upper()
        clean = _clean_stock_name(name)
        if not code or not clean:
            return
        with self._lock:
            mem = self._load()
            if mem.get(code) == clean:
                return
            mem[code] = clean
            self._save()

    def find_code_by_name(self, name: str) -> str | None:
        """Reverse lookup code from a Chinese stock name already in the local cache.

        Exact match only — partial hits are unsafe on a sparse cache (could bind the
        wrong ticker while the full market map would disambiguate or reject).
        """
        clean = _clean_stock_name(name).replace(" ", "").replace("　", "")
        if not clean or not _looks_like_stock_name(clean):
            return None
        with self._lock:
            mem = self._load()
            exact = [
                code
                for code, cached in mem.items()
                if cached.replace(" ", "").replace("　", "") == clean
            ]
            if len(exact) == 1:
                return exact[0]
            return None


_NAME_CACHE = StockNameCache()


def lookup_code_by_cached_name(name: str) -> str | None:
    """Fast name→code using ~/.tradingagents/stock_names.json only (no mootdx)."""
    return _NAME_CACHE.find_code_by_name(name)


def _has_chinese(value: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in str(value or ""))


def _looks_like_stock_name(value: str) -> bool:
    """Chinese company names, or English issuer names for US tickers."""
    text = _clean_stock_name(value)
    if not text:
        return False
    if _has_chinese(text):
        return True
    # English issuer names: letters/digits/spaces/&., at least one letter.
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9&.,'\- ]{1,60}", text):
        return True
    return False


def _is_exchange_or_market_label(value: str) -> bool:
    text = _clean_stock_name(value)
    if not text:
        return False
    compact = re.sub(r"[\s　]+", "", text)
    upper = compact.upper()
    for marker in _EXCHANGE_LABEL_MARKERS:
        if marker.isascii():
            if upper == marker.upper() or upper.startswith(marker.upper()):
                return True
        elif marker in compact:
            return True
    return False


def _is_plausible_stock_name(value: str, code: str) -> bool:
    text = _clean_stock_name(value)
    if not text or text.upper() == str(code or "").strip().upper():
        return False
    if not _looks_like_stock_name(text):
        return False
    if _is_exchange_or_market_label(text):
        return False
    # Avoid treating report headings / price action phrases as a stock name
    # when older states only contain the plain code in stock_input.
    non_name_markers = (
        "技术面",
        "技术分析",
        "市场情绪",
        "新闻舆情",
        "基本面",
        "政策分析",
        "游资追踪",
        "风险评估",
        "投资建议",
        "交易决策",
        "最终决策",
        "分析报告",
        "报告",
        "当前",
        "最新",
        "走势",
        "偏弱",
        "偏强",
        "上涨",
        "下跌",
        "行业",
        "评级",
        "报收",
        "计价",
        "汇率",
        "情绪",
        "差距",
        "压力",
        "指标",
        "相对",
        "强弱",
        "低配",
        "高配",
        "构成",
        "影响",
        "美元",
        "倍",
    )
    if any(marker in text for marker in non_name_markers):
        return False
    # Numeric / ratio fragments: "7.5倍PE的差距"
    if re.search(r"\d", text) and not text.lstrip().startswith(("*", "ST", "*ST")):
        return False

    if not _has_chinese(text):
        banned_en = {
            "LIMITED",
            "LTD",
            "INC",
            "CORP",
            "GROUP",
            "HOLDING",
            "HOLDINGS",
            "ADR",
            "THE",
            "AND",
            "OF",
            "CLASS",
            "STOCK",
            "SHARE",
            "EQUITY",
            "COMPANY",
        }
        if text.upper() in banned_en:
            return False
        # Prefer multi-word issuer names; reject tiny generic tokens.
        words = [w for w in re.split(r"\s+", text) if w]
        if len(words) == 1 and len(text) < 4:
            return False
    else:
        # Company names are usually short; long Chinese phrases are prose.
        compact = re.sub(r"[\s　]+", "", text)
        cn_chars = sum(1 for ch in compact if "一" <= ch <= "鿿")
        if cn_chars > 8:
            return False
    return True


def _is_a_share_code(code: str) -> bool:
    return bool(re.match(r"^[036]\d{5}$", str(code or "").strip()))


def _is_us_style_ticker(code: str) -> bool:
    text = str(code or "").strip().upper()
    if not text or _is_a_share_code(text):
        return False
    # US / international equity tickers: letters, optional .-/^ (BRK.B, ^GSPC)
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.\-^]{0,14}", text))


def _normalize_display_code(ticker: str) -> str:
    code = str(ticker or "").strip().upper()
    for suffix in (".SH", ".SZ", ".BJ"):
        if code.endswith(suffix):
            code = code[: -len(suffix)]
            break
    for prefix in ("SH", "SZ", "BJ"):
        if code.startswith(prefix):
            code = code[len(prefix) :]
            break
    return code


@lru_cache(maxsize=1024)
def _resolve_display_code(ticker: str) -> str:
    code = _normalize_display_code(ticker)
    if re.match(r"^[036]\d{5}$", code):
        return code

    if any("一" <= ch <= "鿿" for ch in code):
        try:
            from tradingagents.dataflows.a_stock import resolve_ticker

            return resolve_ticker(code)
        except Exception:
            return code

    return code


@lru_cache(maxsize=2048)
def _tencent_name(code: str) -> str | None:
    """单码腾讯行情取名（HTTP，秒级）；列表 UI 必须走这条，禁止同步拉 mootdx 全市场。"""
    if not re.match(r"^[036]\d{5}$", code):
        return None
    try:
        from tradingagents.dataflows.a_stock import _tencent_quote

        q = (_tencent_quote([code]) or {}).get(code) or {}
        name = _clean_stock_name(str(q.get("name") or ""))
        return name or None
    except Exception:
        return None


def _mootdx_name_if_cached(code: str) -> str | None:
    """仅当名称映射已在内存建好时读取；绝不触发 _build_name_code_map。"""
    try:
        from tradingagents.dataflows import a_stock

        c2n = getattr(a_stock, "_code_to_name", None)
        if not c2n:
            return None
        name = _clean_stock_name(str(c2n.get(code, "")))
        return name or None
    except Exception:
        return None


@lru_cache(maxsize=1024)
def _yfinance_name(code: str) -> str | None:
    """Resolve a US-style ticker's issuer name via yfinance (cached)."""
    if not _is_us_style_ticker(code):
        return None
    try:
        import yfinance as yf
        from tradingagents.dataflows.stockstats_utils import yf_retry

        ticker_obj = yf.Ticker(code.upper())
        info = yf_retry(lambda: ticker_obj.info) or {}
        for key in ("displayName", "shortName", "longName"):
            name = _clean_stock_name(str(info.get(key) or ""))
            if name and _looks_like_stock_name(name) and not _is_exchange_or_market_label(name):
                return name
    except Exception:
        return None
    return None


@lru_cache(maxsize=1024)
def resolve_stock_name(ticker: str) -> str | None:
    """Return a display name for a ticker when local/market data can resolve it.

    A股顺序：本地 JSON 缓存 → 腾讯行情（命中后回写）→ 已在内存的 mootdx 映射。
    美股顺序：本地 JSON 缓存 → 中文别名表 → yfinance short/long name。
    """
    code = _resolve_display_code(ticker)
    if not code:
        return None

    cached = _NAME_CACHE.get(code)
    if cached:
        return cached

    if _is_a_share_code(code):
        name = _tencent_name(code) or _mootdx_name_if_cached(code)
        if name:
            _NAME_CACHE.set(code, name)
        return name or None

    if _is_us_style_ticker(code):
        alias = _US_CN_ALIASES.get(code.upper())
        if alias:
            _NAME_CACHE.set(code, alias)
            return alias
        name = _yfinance_name(code)
        if name:
            _NAME_CACHE.set(code, name)
        return name or None

    return None


def remember_resolved_name(code: str, raw_name: str) -> None:
    """Backfill local cache after a slow resolve so next click is instant."""
    clean = _clean_stock_name(raw_name)
    if not clean or _is_exchange_or_market_label(clean):
        return
    if not (_looks_like_stock_name(clean) or _has_chinese(clean)):
        return
    _NAME_CACHE.set(code, clean)


def _iter_text_values(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_text_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_text_values(item)


def _clean_extracted_name(name: str) -> str:
    cleaned = _clean_stock_name(name)
    cleaned = cleaned.strip("`_[]【】")
    cleaned = re.sub(r"[*_`]+$", "", cleaned).strip()
    for marker in (
        "给出",
        "当前",
        "技术",
        "市场",
        "新闻",
        "基本面",
        "政策",
        "风险",
        "投资",
        "最终",
        "报告",
        "评级",
    ):
        if marker in cleaned:
            cleaned = cleaned.split(marker, 1)[0]
    # 「交易」曾误切「纽约证券交易所」→「纽约证券」；仅在非交易所语境下裁切。
    if "交易" in cleaned and "交易所" not in cleaned and "证券交易所" not in cleaned:
        cleaned = cleaned.split("交易", 1)[0]
    return cleaned.strip("，。；：:、（）()")


def _extract_stock_name_from_text(code: str, text: str) -> str | None:
    code_pattern = re.escape(code)
    patterns = (
        rf"(?<!\d){code_pattern}\s*[（(]\s*([^\s，。；：:、（）()|]{{2,16}})\s*[）)]",
        rf"(?:标的\s*[：:]\s*)?(?<!\d){code_pattern}\s+([^\s，。；：:、（）()|]{{2,16}})",
        rf"([^\s，。；：:、（）()|]{{2,16}})\s*[（(]\s*{code_pattern}\s*[）)]",
        # English issuer + Chinese alias: MINISO（名创优品）
        rf"[A-Za-z][A-Za-z0-9&.,'\- ]{{1,40}}[（(]\s*([\u4e00-\u9fff*ST·]{{2,8}})\s*[）)]",
    )
    chinese_hits: list[str] = []
    english_hits: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            name = _clean_extracted_name(match.group(1))
            if not _is_plausible_stock_name(name, code):
                continue
            if _has_chinese(name):
                chinese_hits.append(name)
            else:
                english_hits.append(name)
    if chinese_hits:
        # Prefer compact company names (e.g. 名创优品) over longer prose fragments.
        chinese_hits.sort(key=lambda n: (len(re.sub(r"[\s　]+", "", n)), n))
        return chinese_hits[0]
    return english_hits[0] if english_hits else None


# Prefer company-overview sections before market reports (often carry exchange parens).
_NAME_EXTRACT_PRIORITY_KEYS = (
    "fundamentals_report",
    "news_report",
    "policy_report",
    "hot_money_report",
    "lockup_report",
    "sentiment_report",
    "investment_plan",
    "trader_investment_plan",
    "trader_investment_decision",
    "final_trade_decision",
    "data_quality_summary",
    "market_report",
)


def _extract_stock_name_from_state(code: str, final_state: dict) -> str | None:
    for key in ("stock_name", "company_name", "stock_input", "raw_ticker", "input_ticker"):
        name = _clean_extracted_name(str(final_state.get(key, "")))
        if _is_plausible_stock_name(name, code):
            return name

    company = _clean_extracted_name(str(final_state.get("company_of_interest", "")))
    if _is_plausible_stock_name(company, code):
        return company

    english_hit: str | None = None
    for key in (*_NAME_EXTRACT_PRIORITY_KEYS, *_REPORT_DICT_KEYS):
        if key not in final_state:
            continue
        for text in _iter_text_values(final_state[key]):
            name = _extract_stock_name_from_text(code, text)
            if not name:
                continue
            if _has_chinese(name):
                return name
            if english_hit is None:
                english_hit = name
    return english_hit


def _prefer_display_name(*candidates: str | None) -> str | None:
    """Prefer a compact Chinese company name when multiple candidates exist."""
    chinese_company: list[str] = []
    chinese_other: list[str] = []
    english: list[str] = []
    for raw in candidates:
        name = _clean_stock_name(raw or "")
        if not name or not _looks_like_stock_name(name) or _is_exchange_or_market_label(name):
            continue
        if not _is_plausible_stock_name(name, ""):
            # still allow already-resolved English issuer names from yfinance
            if not _has_chinese(name) and _looks_like_stock_name(name):
                english.append(name)
            continue
        if _has_chinese(name):
            compact = re.sub(r"[\s　]+", "", name)
            if re.fullmatch(r"[*ST]*[\u4e00-\u9fff]{2,8}", compact):
                chinese_company.append(name)
            else:
                chinese_other.append(name)
        else:
            english.append(name)
    if chinese_company:
        chinese_company.sort(key=lambda n: len(re.sub(r"[\s　]+", "", n)))
        return chinese_company[0]
    if chinese_other:
        return chinese_other[0]
    return english[0] if english else None


def stock_display_label(ticker: str, final_state: dict | None = None) -> str:
    """Format a stock as 'code name', falling back to the code when the name is unknown."""
    code = _resolve_display_code(ticker)
    resolved = resolve_stock_name(code)
    extracted = _extract_stock_name_from_state(code, final_state) if final_state else None
    # resolved first so CN aliases / cache win ties over flaky report snippets
    name = _prefer_display_name(resolved, extracted)

    if name and name != code:
        if _has_chinese(name) or _is_us_style_ticker(code):
            _NAME_CACHE.set(code, name)
        return f"{code} {name}"
    return code


_SIGNAL_STYLES: dict[str, tuple[str, str, str]] = {
    "Buy": ("#22c55e", "🟢", "买入"),
    "Sell": ("#ef4444", "🔴", "卖出"),
    "Hold": ("#fbbf24", "🟡", "持有"),
}


def signal_text_tag(signal: str) -> str:
    """Return an inline emoji + text tag for the signal (safe for st.button labels)."""
    s = signal.upper() if signal else ""
    if "BUY" in s:
        _, icon, cn = _SIGNAL_STYLES["Buy"]
        return f"{icon} {cn}"
    if "SELL" in s:
        _, icon, cn = _SIGNAL_STYLES["Sell"]
        return f"{icon} {cn}"
    if "HOLD" in s:
        _, icon, cn = _SIGNAL_STYLES["Hold"]
        return f"{icon} {cn}"
    return ""


def signal_html_tag(signal: str) -> str:
    """Return an inline HTML badge for the signal, e.g. '<span style="...">买入</span>'."""
    s = signal.upper() if signal else ""
    if "BUY" in s:
        color, icon, cn = _SIGNAL_STYLES["Buy"]
    elif "SELL" in s:
        color, icon, cn = _SIGNAL_STYLES["Sell"]
    elif "HOLD" in s:
        color, icon, cn = _SIGNAL_STYLES["Hold"]
    else:
        return ""
    return (
        f'<span style="'
        f'display:inline-block;'
        f'background:{color}22;'
        f'color:{color};'
        f'font-weight:700;'
        f'font-size:0.7rem;'
        f'padding:1px 8px;'
        f'border-radius:10px;'
        f'border:1px solid {color}55;'
        f'margin-left:4px;">'
        f'{icon}{cn}</span>'
    )


def format_list_ticker_label(ticker: str, *parts: str) -> str:
    """Sidebar / 观察池列表用：`代码 名称 · 附加信息`。"""
    head = stock_display_label(ticker)
    suffix = [p for p in parts if p]
    if not suffix:
        return head
    return "  ·  ".join([head, *suffix])


def stock_display_parts(ticker: str, final_state: dict | None = None) -> tuple[str, str | None]:
    """Return the resolved display code and optional stock name."""
    code = _resolve_display_code(ticker)
    label = stock_display_label(code, final_state)
    if label == code:
        return code, None
    return code, label.removeprefix(code).strip() or None


def normalize_stock_mentions(text: str, ticker: str, final_state: dict | None = None) -> str:
    """Render code/name mentions in report text as the unified 'code name' label."""
    if not text:
        return text

    code, name = stock_display_parts(ticker, final_state)
    if not name:
        return text

    label = f"{code} {name}"
    name_pattern = re.escape(name)
    code_pattern = re.escape(code)

    normalized = re.sub(rf"(?<!\d){code_pattern}\s*{name_pattern}", label, text)

    def replace_code(match: re.Match[str]) -> str:
        following = normalized[match.end() : match.end() + len(name) + 8]
        if re.match(rf"\s*{name_pattern}", following):
            return match.group(0)
        return label

    normalized = re.sub(rf"(?<!\d){code_pattern}(?!\d)", replace_code, normalized)

    def replace_name(match: re.Match[str]) -> str:
        prefix = normalized[max(0, match.start() - len(code) - 8) : match.start()]
        if re.search(rf"{code_pattern}\s*$", prefix):
            return match.group(0)
        return label

    return re.sub(name_pattern, replace_name, normalized)


_REPORT_TEXT_KEYS = (
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
    "policy_report",
    "hot_money_report",
    "lockup_report",
    "data_quality_summary",
    "trader_investment_plan",
    "trader_investment_decision",
    "investment_plan",
    "final_trade_decision",
)

_REPORT_DICT_KEYS = (
    "investment_debate_state",
    "risk_debate_state",
)


def _normalize_report_value(value: Any, ticker: str, final_state: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return normalize_stock_mentions(value, ticker, final_state)
    if isinstance(value, dict):
        return {
            key: _normalize_report_value(item, ticker, final_state)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _normalize_report_value(item, ticker, final_state)
            for item in value
        ]
    return value


def normalize_report_state_mentions(final_state: dict[str, Any], ticker: str) -> dict[str, Any]:
    """Normalize generated report fields in-place before saving/displaying them."""
    for key in _REPORT_TEXT_KEYS:
        if key in final_state:
            final_state[key] = _normalize_report_value(final_state[key], ticker, final_state)

    for key in _REPORT_DICT_KEYS:
        if key in final_state:
            final_state[key] = _normalize_report_value(final_state[key], ticker, final_state)

    return final_state
