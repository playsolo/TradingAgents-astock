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
| Service | `tradingagents-astock.service`（**直接跑 `web/app.py`，不要跑 `web/launch.py`**） |
| Web | Nginx → Streamlit (`127.0.0.1:8501`) |
| 访问 | **https://m.wcc.io** |

> 注意：`web/launch.py` 是 CLI 入口（内部再起一个 streamlit）。systemd 必须跑 `streamlit run web/app.py`，否则浏览器每次加载会卡在 Running 黑屏，并不断派生新进程。

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

## 信号准确率定时结算（每日 21:00）

生产用 systemd timer，北京时间每天 **21:00** 跑一次 `tradingagents accuracy`（回填 + 结算 1/5/20 日方向命中）。

| 项 | 内容 |
|---|---|
| Service | `tradingagents-accuracy.service`（oneshot） |
| Timer | `tradingagents-accuracy.timer` |
| 日志 | `journalctl -u tradingagents-accuracy.service` |

**首次安装：**

```bash
ssh solo@m.wcc.io
cd /home/solo/TradingAgents-astock && git pull origin dev
sudo cp deploy/tradingagents-accuracy.service /etc/systemd/system/
sudo cp deploy/tradingagents-accuracy.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradingagents-accuracy.timer
systemctl list-timers tradingagents-accuracy.timer
```

**管理：**

```bash
# 下次触发时间
ssh solo@m.wcc.io "systemctl list-timers tradingagents-accuracy.timer --no-pager"
# 立刻跑一次（不等到 21:00）
ssh solo@m.wcc.io "sudo systemctl start tradingagents-accuracy.service"
# 查看最近一次结算日志
ssh solo@m.wcc.io "sudo journalctl -u tradingagents-accuracy.service -n 40 --no-hostname"
```

> 关机错过 21:00 时，`Persistent=true` 会在开机后补跑一次。
> Web「立即结算」与分析开跑时的 settle 仍可用，与 timer 互补。

## 后台分析 worker（关闭页面也跑完队列）

生产采用「Web 只入队 + 独立 worker 执行」模式：

| 项 | 内容 |
|---|---|
| Web Service | `tradingagents-astock.service`，需设 `Environment="TRADINGAGENTS_ANALYSIS_EXECUTOR=worker"` |
| Worker Service | `tradingagents-analyze.service`（`deploy/` 下有单元模板） |
| 队列文件 | `~/.tradingagents/analysis_queue.json`（Web 与 worker 通过 flock 原子读写） |
| 互斥锁 | `~/.tradingagents/analyze.worker.lock`（全局仅一个 worker） |
| 并发上限 | `TRADINGAGENTS_MAX_PARALLEL`（默认 3，两个 service 都要设一致） |

**首次安装 worker：**

```bash
ssh solo@m.wcc.io
cd /home/solo/TradingAgents-astock && git pull origin dev
# 重装 console 入口（新增 tradingagents-analyze）
.venv/bin/pip install -e . --no-deps
sudo cp deploy/tradingagents-analyze.service /etc/systemd/system/
# Web 单元加 EXECUTOR=worker（编辑 /etc/systemd/system/tradingagents-astock.service，
# 或参考 deploy/tradingagents-astock.service）
sudo systemctl daemon-reload
sudo systemctl enable --now tradingagents-analyze.service
sudo systemctl restart tradingagents-astock.service
```

**worker 管理命令：**

```bash
ssh solo@m.wcc.io "systemctl status tradingagents-analyze.service"
ssh solo@m.wcc.io "sudo journalctl -u tradingagents-analyze.service -f --no-hostname -n 50"
# 手动把当前队列耗尽后退出（调试用）
ssh solo@m.wcc.io "cd /home/solo/TradingAgents-astock && .venv/bin/tradingagents-analyze --once -v"
```

> 前提：admin 已在 Web 里保存过模型配置（`~/.tradingagents/model_config.json`），
> worker 无 Streamlit session，模型来源只认该文件（缺失时回退到 `.env`）。

## 工作流总结

```
日常:  dev → feat/xxx → PR → dev → 测试 → push → 部署（git pull + restart）
上游:  main → pull upstream → dev → merge → 解决冲突
```
