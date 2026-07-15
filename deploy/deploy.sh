#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# TradingAgents-Astock 部署脚本
#
# 基于 MAINTENANCE.md 的日常开发 → 部署流程：
#   1. 本地测试
#   2. 提交 & push 到 dev
#   3. SSH 到服务器 pull & restart service
#   4. 验证部署 (HTTP 200)
# =============================================================================

# --- 配置（按需修改） -------------------------------------------------------
REMOTE_USER="solo"
REMOTE_HOST="m.wcc.io"
REMOTE_DIR="/home/solo/TradingAgents-astock"
SERVICE_NAME="tradingagents-astock.service"
LOCAL_BRANCH="${LOCAL_BRANCH:-dev}"
REMOTE_BRANCH="${REMOTE_BRANCH:-dev}"
SSH_PORT="${SSH_PORT:-22}"
TEST_CMD="${TEST_CMD:-python -m pytest tests/ -v}"
VERIFY_URL="${VERIFY_URL:-https://m.wcc.io/}"
VERIFY_EXPECTED_CODE="${VERIFY_EXPECTED_CODE:-200}"

# --- 颜色 -------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# --- 辅助函数 ---------------------------------------------------------------
die()   { err "$@"; exit 1; }

run_step() {
    local step="$1"; shift
    info "──────────────────────────────────────────────"
    info "步骤: $step"
    info "──────────────────────────────────────────────"
}

# =============================================================================
# 主流程
# =============================================================================

run_step "1/6  检查当前分支"
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
info "当前分支: ${CURRENT_BRANCH}"
if [ "$CURRENT_BRANCH" = "$LOCAL_BRANCH" ]; then
    ok "已在目标分支 ${LOCAL_BRANCH} 上"
else
    warn "当前不在 ${LOCAL_BRANCH} 分支上 (当前: ${CURRENT_BRANCH})"
    info "正在切换到 ${LOCAL_BRANCH} ..."
    git checkout "$LOCAL_BRANCH"
    ok "已切换到 ${LOCAL_BRANCH}"
fi

# 检查是否有未提交的改动
if ! git diff --quiet --exit-code; then
    warn "存在未暂存的改动，将一并提交"
fi
if ! git diff --cached --quiet --exit-code; then
    warn "存在已暂存但未提交的改动"
fi

run_step "2/6  运行本地测试"
info "执行: ${TEST_CMD}"
if eval "$TEST_CMD"; then
    ok "本地测试通过"
else
    die "本地测试失败，中止部署"
fi

run_step "3/6  提交 & Push"
# 检查是否有任何改动需要提交
if git diff --quiet --exit-code && git diff --cached --quiet --exit-code; then
    info "工作区干净，无新改动需要提交"
else
    # 检查是否有 staged 改动
    if git diff --cached --quiet --exit-code; then
        # 没有 staged 改动，提示用户输入 commit message
        if [ $# -ge 1 ]; then
            COMMIT_MSG="$1"
        else
            echo ""
            read -r -p "请输入 commit message: " COMMIT_MSG
            if [ -z "$COMMIT_MSG" ]; then
                die "commit message 不能为空"
            fi
        fi
        git add -A
        git commit -m "$COMMIT_MSG"
        ok "已提交: ${COMMIT_MSG}"
    else
        info "存在已暂存的改动，直接提交（跳过 git add -A）"
        git commit
    fi
fi
git push origin "$LOCAL_BRANCH"
ok "已推送到 origin/${LOCAL_BRANCH}"

run_step "4/6  SSH 到服务器执行: git pull + restart"
info "连接 ${REMOTE_USER}@${REMOTE_HOST} ..."
REMOTE_COMMANDS=$(cat <<CMDEOF
set -e
cd "${REMOTE_DIR}"
echo "[远程] 当前目录: \$(pwd)"
echo "[远程] Pull ${REMOTE_BRANCH} ..."
git pull origin "${REMOTE_BRANCH}"
echo "[远程] 重启 ${SERVICE_NAME} ..."
sudo systemctl restart "${SERVICE_NAME}"
echo "[远程] Service 状态:"
systemctl is-active --quiet "${SERVICE_NAME}" && echo "  active" || echo "  inactive (非预期!)"
CMDEOF
)
ssh "${REMOTE_USER}@${REMOTE_HOST}" -p "${SSH_PORT}" "${REMOTE_COMMANDS}" || {
    die "服务器部署失败，请检查连接或日志: ssh ${REMOTE_USER}@${REMOTE_HOST} sudo journalctl -u ${SERVICE_NAME} -n 50"
}
ok "服务器部署完成"

run_step "5/6  等待服务启动"
info "等待 3 秒让服务完成启动 ..."
sleep 3

run_step "6/6  验证部署"
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 10 --max-time 15 "${VERIFY_URL}")
if [ "$HTTP_CODE" = "${VERIFY_EXPECTED_CODE}" ]; then
    ok "验证通过 — ${VERIFY_URL} 返回 ${HTTP_CODE}"
else
    warn "验证结果异常 — ${VERIFY_URL} 返回 ${HTTP_CODE} (期望 ${VERIFY_EXPECTED_CODE})"
    info "查看服务日志:"
    info "  ssh ${REMOTE_USER}@${REMOTE_HOST} sudo journalctl -u ${SERVICE_NAME} -n 50 --no-hostname"
fi

echo ""
echo -e "${GREEN}====== 部署完成 ======${NC}"
echo ""
echo "服务日志命令:"
echo "  ssh ${REMOTE_USER}@${REMOTE_HOST} sudo journalctl -u ${SERVICE_NAME} -f --no-hostname -n 50"
echo "状态命令:"
echo "  ssh ${REMOTE_USER}@${REMOTE_HOST} systemctl status ${SERVICE_NAME}"
