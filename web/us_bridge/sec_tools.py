"""Self-contained SEC tools for the US bridge worker (no A-stock imports).

Duplicated lightly from ``tradingagents.filings.sec_edgar`` so the worker can
run under the US checkout PYTHONPATH after stripping tradingagents-astock.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

import requests

TARGET_FORMS = frozenset({"10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "8-K/A"})

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_ARCHIVES_URL = (
    "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc}"
)

_DEFAULT_UA = "TradingAgents-Astock/0.2 (us-bridge; contact@localhost)"
_MIN_INTERVAL = float(os.getenv("SEC_EDGAR_MIN_INTERVAL", "0.2"))
_lock = threading.Lock()
_last_request_at = 0.0
_ticker_cache: dict[str, str] | None = None

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.I | re.S)
_STYLE_RE = re.compile(r"<style\b[^>]*>.*?</style>", re.I | re.S)
_WS_RE = re.compile(r"\s+")


def user_agent() -> str:
    return (os.getenv("SEC_EDGAR_USER_AGENT") or _DEFAULT_UA).strip() or _DEFAULT_UA


def pad_cik(cik: str | int) -> str:
    return str(cik).strip().zfill(10)


def _throttle() -> None:
    global _last_request_at
    with _lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL - (now - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _http_get(url: str, timeout: float = 30.0) -> requests.Response:
    _throttle()
    resp = requests.get(
        url,
        headers={"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp


def lookup_cik(ticker: str) -> str | None:
    global _ticker_cache
    sym = (ticker or "").strip().upper()
    if not sym:
        return None
    with _lock:
        if _ticker_cache is None:
            payload = _http_get(_TICKERS_URL).json()
            mapping: dict[str, str] = {}
            if isinstance(payload, dict):
                for row in payload.values():
                    if not isinstance(row, dict):
                        continue
                    t = str(row.get("ticker") or "").strip().upper()
                    cik = row.get("cik_str")
                    if t and cik is not None:
                        mapping[t] = pad_cik(cik)
            _ticker_cache = mapping
        cache = _ticker_cache
    return cache.get(sym)


def _extract(recent: dict[str, Any], *, forms: set[str], limit: int, cik: str) -> list[dict[str, Any]]:
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    form_list = recent.get("form") or []
    docs = recent.get("primaryDocument") or []
    descs = recent.get("primaryDocDescription") or []
    n = min(len(accessions), len(dates), len(form_list))
    out: list[dict[str, Any]] = []
    for i in range(n):
        form = str(form_list[i] or "").strip().upper()
        if form not in forms:
            continue
        acc = str(accessions[i] or "").strip()
        if not acc:
            continue
        out.append(
            {
                "form": form,
                "accession": acc,
                "filing_date": str(dates[i] or "").strip(),
                "primary_document": str(docs[i] if i < len(docs) else "").strip(),
                "description": str(descs[i] if i < len(descs) else "").strip(),
                "cik": cik,
            }
        )
        if len(out) >= limit:
            break
    return out


def _html_to_text(html: str) -> str:
    text = _SCRIPT_RE.sub(" ", html)
    text = _STYLE_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def filing_document_url(cik: str, accession: str, primary_document: str) -> str:
    cik_int = str(int(pad_cik(cik)))
    return _ARCHIVES_URL.format(
        cik_int=cik_int,
        acc_nodash=accession.replace("-", ""),
        doc=primary_document.lstrip("/"),
    )


def fetch_excerpt(cik: str, accession: str, primary_document: str, max_chars: int = 8000) -> str:
    if not primary_document:
        return ""
    url = filing_document_url(cik, accession, primary_document)
    raw = _http_get(url, timeout=60.0).text or ""
    text = _html_to_text(raw) if "<" in raw[:500] else _WS_RE.sub(" ", raw).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... ({len(text) - max_chars} chars truncated)"
    return text


def get_sec_filings_report(
    ticker: str,
    form_types: str = "10-K,10-Q,8-K",
    limit: int = 3,
    include_excerpts: bool = True,
) -> str:
    forms = {p.strip().upper() for p in (form_types or "").split(",") if p.strip()} or set(TARGET_FORMS)
    expanded = set(forms)
    for f in list(forms):
        if not f.endswith("/A"):
            expanded.add(f"{f}/A")

    cik = lookup_cik(ticker)
    if not cik:
        return f"No CIK found for ticker {ticker}."

    data = _http_get(_SUBMISSIONS_URL.format(cik=cik)).json()
    recent = (data.get("filings") or {}).get("recent") or {}
    filings = _extract(recent, forms=expanded, limit=int(limit) or 3, cik=cik)
    if not filings:
        return f"No recent SEC filings (10-K/10-Q/8-K) found for {ticker}."

    lines = [f"## SEC filings for {ticker.upper()}", ""]
    for f in filings:
        acc = f["accession"]
        lines.append(f"### {f['form']} — {f['filing_date']}")
        if f.get("description"):
            lines.append(f"Description: {f['description']}")
        lines.append(f"Accession: {acc}")
        doc = f.get("primary_document") or ""
        if doc:
            lines.append(f"Link: {filing_document_url(cik, acc, doc)}")
        if include_excerpts and doc:
            try:
                excerpt = fetch_excerpt(cik, acc, doc)
                if excerpt:
                    lines.append("")
                    lines.append("Excerpt:")
                    lines.append(excerpt)
            except Exception as exc:  # noqa: BLE001
                lines.append(f"(excerpt failed: {exc})")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def make_get_sec_filings_tool():
    """Return a LangChain ``@tool`` bound to ``get_sec_filings_report``."""
    from typing import Annotated

    from langchain_core.tools import tool

    @tool
    def get_sec_filings(
        ticker: Annotated[str, "US ticker symbol, e.g. AAPL"],
        form_types: Annotated[
            str, "Comma-separated SEC forms, default 10-K,10-Q,8-K"
        ] = "10-K,10-Q,8-K",
        limit: Annotated[int, "Max filings to return"] = 3,
    ) -> str:
        """Fetch recent SEC filings (10-K/10-Q/8-K) with plain-text excerpts from EDGAR."""
        return get_sec_filings_report(
            ticker,
            form_types=form_types,
            limit=limit,
            include_excerpts=True,
        )

    return get_sec_filings
