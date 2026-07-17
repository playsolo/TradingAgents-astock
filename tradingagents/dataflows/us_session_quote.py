"""US regular / extended-hours quote for analysis context injection.

Daily OHLCV bars only reflect the regular session. After earnings (and during
pre/post market) the last tradable price can diverge sharply — inject that
explicitly so analysts do not treat the session close as "current".
"""

from __future__ import annotations

from typing import Any


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def fetch_us_session_quote(symbol: str) -> dict[str, Any]:
    """Fetch Yahoo session quote fields for ``symbol``.

    Returns a dict with at least ``symbol``. Missing fields are omitted rather
    than set to None so formatters can stay simple. Never raises.
    """
    code = str(symbol or "").strip().upper()
    out: dict[str, Any] = {"symbol": code}
    if not code:
        return out

    try:
        import yfinance as yf
    except Exception:
        return out

    info: dict[str, Any] = {}
    try:
        ticker = yf.Ticker(code)
        raw = getattr(ticker, "info", None)
        if callable(raw):
            raw = raw()
        if isinstance(raw, dict):
            info = raw
    except Exception:
        info = {}

    # fast_info can fill gaps when .info is rate-limited / sparse
    try:
        ticker = yf.Ticker(code)
        fast = getattr(ticker, "fast_info", None)
        if fast is not None:
            mapping = {
                "lastPrice": "regularMarketPrice",
                "previousClose": "regularMarketPreviousClose",
                "marketState": "marketState",
            }
            for src, dest in mapping.items():
                if dest in info and info.get(dest) is not None:
                    continue
                try:
                    val = (
                        fast[src]
                        if hasattr(fast, "__getitem__")
                        else getattr(fast, src, None)
                    )
                except Exception:
                    val = getattr(fast, src, None)
                if val is not None:
                    info.setdefault(dest, val)
    except Exception:
        pass

    field_map = {
        "regular_market_price": "regularMarketPrice",
        "previous_close": "regularMarketPreviousClose",
        "post_market_price": "postMarketPrice",
        "pre_market_price": "preMarketPrice",
        "post_market_change_pct": "postMarketChangePercent",
        "pre_market_change_pct": "preMarketChangePercent",
        "market_state": "marketState",
        "currency": "currency",
        "exchange": "exchange",
        "short_name": "shortName",
    }
    for dest, src in field_map.items():
        if src in ("marketState", "currency", "exchange", "shortName"):
            text = _as_str(info.get(src))
            if text is not None:
                out[dest] = text
            continue
        number = _as_float(info.get(src))
        if number is not None:
            out[dest] = number

    return out


def last_tradable_price(quote: dict[str, Any]) -> tuple[float | None, str]:
    """Pick the best last-trade reference and a short label for it."""
    state = str(quote.get("market_state") or "").upper()
    post = _as_float(quote.get("post_market_price"))
    pre = _as_float(quote.get("pre_market_price"))
    regular = _as_float(quote.get("regular_market_price"))

    if state in {"POST", "POSTPOST", "CLOSED"} and post is not None:
        # CLOSED often still exposes the last post-market print after the bell.
        return post, "post_market"
    if state in {"PRE", "PREPRE"} and pre is not None:
        return pre, "pre_market"
    # If Yahoo omitted marketState but post differs from regular, prefer post.
    if (
        post is not None
        and regular is not None
        and abs(post - regular) / max(abs(regular), 1e-9) >= 0.002
    ):
        return post, "post_market"
    if post is not None and regular is None:
        return post, "post_market"
    if (
        pre is not None
        and regular is not None
        and abs(pre - regular) / max(abs(regular), 1e-9) >= 0.002
    ):
        return pre, "pre_market"
    if regular is not None:
        return regular, "regular_session"
    if post is not None:
        return post, "post_market"
    if pre is not None:
        return pre, "pre_market"
    return None, "unknown"


def _fmt_price(value: float) -> str:
    """Format a USD-style print without scientific notation."""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def format_us_session_quote_block(
    symbol: str, quote: dict[str, Any] | None = None
) -> str:
    """Markdown block for instrument_context / first human turn."""
    data = quote if quote is not None else fetch_us_session_quote(symbol)
    code = str(data.get("symbol") or symbol or "").strip().upper()
    if not code:
        return ""

    regular = _as_float(data.get("regular_market_price"))
    previous = _as_float(data.get("previous_close"))
    post = _as_float(data.get("post_market_price"))
    pre = _as_float(data.get("pre_market_price"))
    state = _as_str(data.get("market_state")) or "UNKNOWN"
    last, last_src = last_tradable_price(data)

    if last is None and regular is None and post is None and pre is None:
        return ""

    lines = [
        f"## Live US session quote for {code} (authoritative for last trade / entry levels)",
        f"- Market state: {state}",
    ]
    if previous is not None:
        lines.append(f"- Previous close: {_fmt_price(previous)}")
    if regular is not None:
        lines.append(f"- Regular session price/close: {_fmt_price(regular)}")
    if post is not None:
        pct = _as_float(data.get("post_market_change_pct"))
        pct_txt = f" ({pct:+.2f}% vs regular)" if pct is not None else ""
        lines.append(f"- Post-market last: {_fmt_price(post)}{pct_txt}")
    if pre is not None:
        pct = _as_float(data.get("pre_market_change_pct"))
        pct_txt = f" ({pct:+.2f}% vs prior close)" if pct is not None else ""
        lines.append(f"- Pre-market last: {_fmt_price(pre)}{pct_txt}")
    if last is not None:
        lines.append(f"- Last tradable reference: {_fmt_price(last)} ({last_src})")

    lines.extend(
        [
            "",
            "IMPORTANT:",
            "- Daily OHLCV and technical indicators (SMA/EMA/MACD/RSI) use regular-session bars only.",
            "- When an extended-hours (pre/post) price is present and differs from the regular close,",
            "  treat the extended-hours print as the latest tradable reference for entry / stop /",
            "  re-entry levels. Do NOT describe the regular close as \"current price\" in that case.",
            "- If news says the stock moved after earnings or guidance, reconcile levels against the",
            "  extended-hours quote above before recommending buy/add zones.",
        ]
    )
    return "\n".join(lines)
