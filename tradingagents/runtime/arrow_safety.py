"""Avoid pyarrow mimalloc TLS SIGSEGV on non-main threads (macOS / Streamlit).

Crash signature from DiagnosticReports:
  EXC_BAD_ACCESS / SIGSEGV at mi_heap_main → mi_thread_init
  via pandas.maybe_convert_objects → pyarrow NdarrayToArrow

Setting ``ARROW_DEFAULT_MEMORY_POOL=system`` *before* libarrow loads forces the
system malloc pool and sidesteps mimalloc thread-local heap init failures on
Streamlit / analysis worker threads.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_ARROW_POOL_ENV = "ARROW_DEFAULT_MEMORY_POOL"
_SAFE_POOL = "system"
_warmed = False


def configure_arrow_memory_pool() -> str:
    """Force Arrow to use the system allocator. Call before importing pyarrow."""
    os.environ[_ARROW_POOL_ENV] = _SAFE_POOL
    return _SAFE_POOL


def warm_arrow_for_worker_threads() -> None:
    """Import pyarrow with the safe pool and exercise a conversion once.

    Safe to call from the main thread at process start, and again at the start
    of each analysis worker thread (idempotent for env; per-thread warm-up is
    cheap and initializes allocator TLS on that thread when needed).
    """
    global _warmed
    configure_arrow_memory_pool()
    try:
        import pandas as pd
        import pyarrow  # noqa: F401 — ensure load order after env is set

        # Object/None mixes are what trigger maybe_convert_objects → Arrow.
        pd.DataFrame({"_warm": ["a", None, "b"], "_n": [1, None, 2]})
    except Exception as exc:  # pragma: no cover — defensive; never fail startup
        logger.warning("Arrow/pandas warm-up skipped: %s", exc)
        return
    _warmed = True


def ensure_arrow_safe_for_current_thread() -> None:
    """Call at the start of analysis worker threads before dataframe work."""
    configure_arrow_memory_pool()
    warm_arrow_for_worker_threads()
