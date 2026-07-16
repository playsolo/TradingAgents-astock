"""SEC EDGAR client: ticker→CIK, recent filings, document excerpts.

Requires a descriptive User-Agent (SEC fair-access policy). Override with
``SEC_EDGAR_USER_AGENT`` (e.g. ``TradingAgents-Astock/0.2 (you@example.com)``).
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

_DEFAULT_UA = "TradingAgents-Astock/0.2 (filings-monitor; contact@localhost)"
_MIN_INTERVAL = float(os.getenv("SEC_EDGAR_MIN_INTERVAL", "0.2"))

_lock = threading.Lock()
_last_request_at = 0.0
_ticker_cache: dict[str, str] | None = None
_ticker_cache_at = 0.0
_TICKER_CACHE_TTL = 24 * 3600

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


def _headers() -> dict[str, str]:
    return {
        "User-Agent": user_agent(),
        "Accept-Encoding": "gzip, deflate",
        "Host": "www.sec.gov",
    }


def _headers_data() -> dict[str, str]:
    return {
        "User-Agent": user_agent(),
        "Accept-Encoding": "gzip, deflate",
        "Host": "data.sec.gov",
    }


def _http_get(url: str, *, data_host: bool = False, timeout: float = 30.0) -> requests.Response:
    _throttle()
    headers = _headers_data() if data_host else _headers()
    # Host header must match URL host; rebuild without forcing wrong Host.
    headers = {"User-Agent": user_agent(), "Accept-Encoding": "gzip, deflate"}
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp


def _http_get_json(url: str, *, data_host: bool = False) -> Any:
    return _http_get(url, data_host=data_host).json()


def lookup_cik(ticker: str) -> str | None:
    """Return zero-padded CIK for ``ticker``, or None if unknown."""
    global _ticker_cache, _ticker_cache_at
    sym = (ticker or "").strip().upper()
    if not sym:
        return None

    now = time.time()
    with _lock:
        if _ticker_cache is None or (now - _ticker_cache_at) > _TICKER_CACHE_TTL:
            payload = _http_get_json(_TICKERS_URL, data_host=False)
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
            _ticker_cache_at = now
        cache = _ticker_cache

    return cache.get(sym)


def extract_recent_filings(
    recent: dict[str, Any],
    *,
    forms: frozenset[str] | set[str] = TARGET_FORMS,
    limit: int = 10,
    cik: str | None = None,
) -> list[dict[str, Any]]:
    """Parse the columnar ``filings.recent`` block from a submissions JSON."""
    forms_u = {f.upper() for f in forms}
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    form_list = recent.get("form") or []
    docs = recent.get("primaryDocument") or []
    descs = recent.get("primaryDocDescription") or []

    n = min(len(accessions), len(dates), len(form_list))
    out: list[dict[str, Any]] = []
    for i in range(n):
        form = str(form_list[i] or "").strip().upper()
        if form not in forms_u:
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
                "cik": pad_cik(cik) if cik else "",
            }
        )
        if len(out) >= limit:
            break
    return out


def list_recent_filings(
    ticker: str,
    *,
    forms: frozenset[str] | set[str] = TARGET_FORMS,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Fetch recent target-form filings for ``ticker`` from EDGAR submissions."""
    cik = lookup_cik(ticker)
    if not cik:
        return []
    data = _http_get_json(_SUBMISSIONS_URL.format(cik=cik), data_host=True)
    recent = (data.get("filings") or {}).get("recent") or {}
    if not isinstance(recent, dict):
        return []
    return extract_recent_filings(recent, forms=forms, limit=limit, cik=cik)


def _html_to_text(html: str) -> str:
    text = _SCRIPT_RE.sub(" ", html)
    text = _STYLE_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#160;", " ")
    )
    return _WS_RE.sub(" ", text).strip()


def filing_document_url(cik: str, accession: str, primary_document: str) -> str:
    cik_int = str(int(pad_cik(cik)))
    acc_nodash = accession.replace("-", "")
    doc = primary_document.lstrip("/")
    return _ARCHIVES_URL.format(cik_int=cik_int, acc_nodash=acc_nodash, doc=doc)


def fetch_filing_excerpt(
    *,
    cik: str,
    accession: str,
    primary_document: str,
    max_chars: int = 12000,
) -> str:
    """Download a primary filing document and return plain-text excerpt."""
    if not primary_document:
        return ""
    url = filing_document_url(cik, accession, primary_document)
    resp = _http_get(url, data_host=False, timeout=60.0)
    raw = resp.text or ""
    if "<html" in raw.lower() or "<body" in raw.lower() or "<" in raw[:200]:
        text = _html_to_text(raw)
    else:
        text = _WS_RE.sub(" ", raw).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... ({len(text) - max_chars} chars truncated)"
    return text


def format_filings_report(
    ticker: str,
    filings: list[dict[str, Any]],
    *,
    excerpts: dict[str, str] | None = None,
) -> str:
    excerpts = excerpts or {}
    if not filings:
        return f"No recent SEC filings (10-K/10-Q/8-K) found for {ticker}."

    lines = [f"## SEC filings for {ticker.upper()}", ""]
    for f in filings:
        acc = f.get("accession") or ""
        cik = f.get("cik") or ""
        doc = f.get("primary_document") or ""
        lines.append(f"### {f.get('form')} — {f.get('filing_date')}")
        if f.get("description"):
            lines.append(f"Description: {f['description']}")
        lines.append(f"Accession: {acc}")
        if cik and doc:
            lines.append(f"Link: {filing_document_url(cik, acc, doc)}")
        excerpt = excerpts.get(acc) or ""
        if excerpt:
            lines.append("")
            lines.append("Excerpt:")
            lines.append(excerpt)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def get_sec_filings_report(
    ticker: str,
    *,
    form_types: str = "10-K,10-Q,8-K",
    limit: int = 3,
    include_excerpts: bool = True,
    max_chars_per_filing: int = 8000,
) -> str:
    """High-level helper used by tools and the filing monitor."""
    forms = {p.strip().upper() for p in (form_types or "").split(",") if p.strip()}
    if not forms:
        forms = set(TARGET_FORMS)
    # Always allow amendments matching base forms.
    expanded = set(forms)
    for f in list(forms):
        expanded.add(f"{f}/A" if not f.endswith("/A") else f)

    filings = list_recent_filings(ticker, forms=expanded, limit=limit)
    excerpts: dict[str, str] = {}
    if include_excerpts:
        for f in filings:
            cik = f.get("cik") or ""
            acc = f.get("accession") or ""
            doc = f.get("primary_document") or ""
            if not (cik and acc and doc):
                continue
            try:
                excerpts[acc] = fetch_filing_excerpt(
                    cik=cik,
                    accession=acc,
                    primary_document=doc,
                    max_chars=max_chars_per_filing,
                )
            except Exception as exc:  # noqa: BLE001 — surface in report
                excerpts[acc] = f"(failed to fetch excerpt: {exc})"
    return format_filings_report(ticker, filings, excerpts=excerpts)
