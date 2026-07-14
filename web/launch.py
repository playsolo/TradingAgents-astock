"""Launch the TradingAgents web UI via `tradingagents-web` command."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    # Force Arrow system allocator before the Streamlit child imports pyarrow.
    os.environ.setdefault("ARROW_DEFAULT_MEMORY_POOL", "system")
    app_path = Path(__file__).parent / "app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app_path)])


if __name__ == "__main__":
    main()
