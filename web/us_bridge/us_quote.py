"""Bridge shim for US session quotes.

The worker may run with the US checkout's ``tradingagents`` on ``sys.path``
(A-stock package stripped). Load the shared implementation by file path from
this A-stock repo so injection still works in that environment.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_us_session_quote() -> ModuleType:
    try:
        from tradingagents.dataflows import us_session_quote as mod

        return mod
    except ImportError:
        pass

    candidate = (
        Path(__file__).resolve().parents[2]
        / "tradingagents"
        / "dataflows"
        / "us_session_quote.py"
    )
    if not candidate.is_file():
        raise ImportError(f"us_session_quote not found at {candidate}")
    spec = importlib.util.spec_from_file_location(
        "tradingagents_us_session_quote", candidate
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load us_session_quote from {candidate}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_us_session_quote()
fetch_us_session_quote = _mod.fetch_us_session_quote
last_tradable_price = _mod.last_tradable_price
format_us_session_quote_block = _mod.format_us_session_quote_block

__all__ = [
    "fetch_us_session_quote",
    "last_tradable_price",
    "format_us_session_quote_block",
]
