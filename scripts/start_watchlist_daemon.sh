#!/usr/bin/env bash
# 不打开 Web 时，在登录会话里常驻观察守护（交易日 09:35 / 13:05 / 15:05）。
# 用法：
#   ./scripts/start_watchlist_daemon.sh
#   nohup ./scripts/start_watchlist_daemon.sh >> ~/.tradingagents/watchlist-daemon.log 2>&1 &

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p "${HOME}/.tradingagents"
exec "${ROOT}/.venv/bin/python" -m tradingagents.watchlist.daemon "$@"
