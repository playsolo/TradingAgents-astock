#!/usr/bin/env bash
# 安装 macOS 用户级 launchd 服务：登录自启 + KeepAlive 崩溃重启。
# 用法：
#   ./scripts/install_watchlist_launchd.sh          # 安装并加载
#   ./scripts/install_watchlist_launchd.sh uninstall  # 卸载
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.tradingagents.watchlist"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
TEMPLATE="${ROOT}/scripts/com.tradingagents.watchlist.plist.template"
PYTHON="${ROOT}/.venv/bin/python"

if [[ ! -x "${PYTHON}" ]]; then
  echo "找不到 ${PYTHON}，请先在项目根目录创建 .venv 并 pip install -e ." >&2
  exit 1
fi

cmd="${1:-install}"

unload_if_loaded() {
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  launchctl unload "${PLIST_DST}" 2>/dev/null || true
}

if [[ "${cmd}" == "uninstall" ]]; then
  unload_if_loaded
  rm -f "${PLIST_DST}"
  echo "已卸载 ${LABEL}"
  exit 0
fi

# 避免与人手 nohup 的旧守护抢锁
pkill -f "tradingagents.watchlist.daemon" 2>/dev/null || true
sleep 1

mkdir -p "${HOME}/Library/LaunchAgents" "${HOME}/.tradingagents"

# 渲染绝对路径进 plist
tmp="$(mktemp)"
sed \
  -e "s|__PYTHON__|${PYTHON}|g" \
  -e "s|__PROJECT_ROOT__|${ROOT}|g" \
  -e "s|__HOME__|${HOME}|g" \
  "${TEMPLATE}" > "${tmp}"

# 校验为合法 plist
plutil -lint "${tmp}" >/dev/null

unload_if_loaded
cp "${tmp}" "${PLIST_DST}"
rm -f "${tmp}"

# 现代 macOS 用 bootstrap；失败再退回 load
if ! launchctl bootstrap "gui/$(id -u)" "${PLIST_DST}" 2>/dev/null; then
  launchctl load -w "${PLIST_DST}"
fi
# 确保立即拉起
launchctl kickstart -k "gui/$(id -u)/${LABEL}" 2>/dev/null || true

sleep 1
if launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | grep -q "state = running"; then
  echo "已安装并运行：${LABEL}"
elif pgrep -f "tradingagents.watchlist.daemon" >/dev/null; then
  echo "已安装：${LABEL}（进程已在跑）"
else
  echo "已写入 ${PLIST_DST}，请检查日志："
  echo "  ${HOME}/.tradingagents/watchlist-daemon.log"
  echo "  ${HOME}/.tradingagents/watchlist-daemon.err.log"
  exit 1
fi

echo "日志：${HOME}/.tradingagents/watchlist-daemon.log"
echo "卸载：${ROOT}/scripts/install_watchlist_launchd.sh uninstall"
