"""Regression: pandas→pyarrow mimalloc SIGSEGV on Streamlit worker threads.

macOS crash reports consistently show:
  pandas.maybe_convert_objects → pyarrow NdarrayToArrow → mi_thread_init → SIGSEGV (exit 139)

``ARROW_DEFAULT_MEMORY_POOL=system`` must be set *before* pyarrow loads.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_configure_arrow_memory_pool_forces_system(monkeypatch):
    monkeypatch.setenv("ARROW_DEFAULT_MEMORY_POOL", "mimalloc")
    from tradingagents.runtime.arrow_safety import configure_arrow_memory_pool

    assert configure_arrow_memory_pool() == "system"
    assert os.environ["ARROW_DEFAULT_MEMORY_POOL"] == "system"


def test_worker_thread_dataframe_survives_in_fresh_process():
    """Subprocess: configure → import pandas/pyarrow → build DF on a worker thread.

    Mirrors Streamlit analysis threads calling mootdx/sina DataFrame paths.
    Must exit 0 (not 139).
    """
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        root = Path(%r)
        sys.path.insert(0, str(root))

        from tradingagents.runtime.arrow_safety import (
            configure_arrow_memory_pool,
            warm_arrow_for_worker_threads,
        )

        assert configure_arrow_memory_pool() == "system"
        warm_arrow_for_worker_threads()

        import threading
        import pandas as pd

        err = []

        def _work():
            try:
                # Object dtype + mixed None triggers maybe_convert_objects → pyarrow.
                df = pd.DataFrame(
                    {
                        "code": ["600354", "002648", None, "600519"],
                        "name": ["敦煌种业", "卫星化学", None, "贵州茅台"],
                        "close": [1.2, 3.4, None, 5.6],
                    }
                )
                _ = df.astype(object)
                _ = pd.DataFrame({"x": list(range(2000)) + [None]})
            except Exception as exc:  # pragma: no cover
                err.append(exc)

        t = threading.Thread(target=_work)
        t.start()
        t.join()
        if err:
            raise err[0]
        print("ok", flush=True)
        """
    ) % str(_PROJECT_ROOT)

    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(_PROJECT_ROOT)},
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"worker-thread DataFrame crashed (exit {proc.returncode}).\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert "ok" in proc.stdout


def test_app_entry_sets_arrow_pool_before_heavy_imports():
    """web/app.py must configure the pool before importing streamlit/pandas."""
    app_src = (_PROJECT_ROOT / "web" / "app.py").read_text(encoding="utf-8")
    # Strip a leading module docstring so import order checks stay stable.
    body = app_src
    if body.lstrip().startswith('"""'):
        end = body.index('"""', 3) + 3
        body = body[end:]
    configure_pos = body.find("configure_arrow_memory_pool")
    streamlit_pos = body.find("import streamlit")
    assert configure_pos != -1, "web/app.py must call configure_arrow_memory_pool"
    assert streamlit_pos != -1
    assert configure_pos < streamlit_pos, (
        "configure_arrow_memory_pool must run before import streamlit"
    )
