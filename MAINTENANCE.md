# 维护指南

## Remote 结构

| Remote | URL |
|---|---|
| `origin` | `git@github.com:playsolo/TradingAgents-astock.git`（你的 fork） |
| `upstream` | `git@github.com:simonlin1212/TradingAgents-astock.git`（来源） |

## 服务器

| 项目 | 内容 |
|---|---|
| 地址 | `m.wcc.io` |
| 用户 | `solo`（sudo 免密） |
| 代码目录 | `/home/solo/TradingAgents-astock` |
| Service | `tradingagents-astock.service` |
| Web | Nginx → Streamlit (`127.0.0.1:8501`) |
| 访问 | **https://m.wcc.io** |

## 分支策略

- `main` — 稳定发布版，与 upstream/main 对齐
- `dev` — 日常开发集成分支，包含你所有改动
- `feat/*` / `fix/*` — 功能/修复分支，从 dev 切出，PR 合并回 dev

## 完整开发部署流程（Cursor Agent 执行）

### 日常开发 → 部署

```bash
# 1. 在本地 dev 上开发
git checkout dev
git checkout -b feat/xxx   # 或 fix/xxx
# ... 改代码 ...

# 2. 本地测试
python -m pytest tests/ -v

# 3. 提交到 dev
git checkout dev
git merge feat/xxx
git push origin dev

# 4. 部署到服务器
ssh solo@m.wcc.io "cd /home/solo/TradingAgents-astock && git pull origin dev && sudo systemctl restart tradingagents-astock.service"

# 5. 验证部署
curl -s -o /dev/null -w '%{http_code}' https://m.wcc.io/   # 应返回 200
```

### 快速修复（小改动直接上 dev）

```bash
# 1. 本地改代码
git checkout dev
# ... 改代码 ...
python -m pytest tests/ -v -k <相关测试>

# 2. 提交 & 推送
git add -A && git commit -m "fix: xxx"
git push origin dev

# 3. 部署
ssh solo@m.wcc.io "cd /home/solo/TradingAgents-astock && git pull origin dev && sudo systemctl restart tradingagents-astock.service"
```

### 发布新版

```bash
git checkout main
git merge dev
git tag v0.2.19            # 版本号递进
git push origin main --tags
```

### 拉取上游更新（simonlin1212 的更新）

```bash
# 确保当前没有未提交的改动
git checkout main
git pull upstream main
git checkout dev
git merge main             # 合并 upstream 的改动到 dev，解决冲突
```

上游是 A 股特化版本，和你的改动主要在数据层（`tradingagents/dataflows/`）和策略层（`tradingagents/strategies/`）可能有冲突，merge 时注意检查。

### 首次克隆后恢复工作

```bash
git clone git@github.com:playsolo/TradingAgents-astock.git
cd TradingAgents-astock
git remote add upstream git@github.com:simonlin1212/TradingAgents-astock.git
git checkout dev
```

## 服务器管理命令

```bash
# 查看状态
ssh solo@m.wcc.io "systemctl status tradingagents-astock.service"

# 重启
ssh solo@m.wcc.io "sudo systemctl restart tradingagents-astock.service"

# 查看日志
ssh solo@m.wcc.io "sudo journalctl -u tradingagents-astock.service -f --no-hostname -n 50"

# 更新代码 + 重启（一键）
ssh solo@m.wcc.io "cd /home/solo/TradingAgents-astock && git pull origin dev && sudo systemctl restart tradingagents-astock.service"
```

## 工作流总结

```
日常:  dev → feat/xxx → PR → dev → 测试 → push → 部署（git pull + restart）
上游:  main → pull upstream → dev → merge → 解决冲突
```
