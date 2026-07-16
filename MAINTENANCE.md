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

## 价值波段扫描定时（每日 20:30）

生产用 systemd timer，北京时间每天 **20:30** 跑一次 `tradingagents-scan --enqueue`（非交易日跳过）。扫描独立于 Web；候选写入分析队列，由 `tradingagents-analyze` 消费。

| 项 | 内容 |
|---|---|
| Service | `tradingagents-scan.service`（oneshot） |
| Timer | `tradingagents-scan.timer` |
| 日志 | `journalctl -u tradingagents-scan.service`；扫描进程另写 `~/.tradingagents/value_swing_scan.log` |
| 状态 | `~/.tradingagents/value_swing_scan.json` |

**首次安装：**

```bash
ssh solo@m.wcc.io
cd /home/solo/TradingAgents-astock && git pull origin dev
.venv/bin/pip install -e . --no-deps   # 确保有 tradingagents-scan 入口
sudo cp deploy/tradingagents-scan.service /etc/systemd/system/
sudo cp deploy/tradingagents-scan.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tradingagents-scan.timer
systemctl list-timers tradingagents-scan.timer
```

**管理：**

```bash
# 下次触发时间
ssh solo@m.wcc.io "systemctl list-timers tradingagents-scan.timer --no-pager"
# 立刻跑一次（不等到 20:30；仍会尊重 --skip-non-trading-day）
ssh solo@m.wcc.io "sudo systemctl start tradingagents-scan.service"
# 最近一次扫描日志
ssh solo@m.wcc.io "sudo journalctl -u tradingagents-scan.service -n 40 --no-hostname"
```

> 关机错过 20:30 时，`Persistent=true` 会在开机后补跑一次。
> Web「价值波段扫描」按钮仍可手动触发同一套后台进程。
> 入队后需 `tradingagents-analyze.service` 在跑，否则队列不会被消费。

## 后台分析 worker（关闭页面也跑完队列）

生产采用「Web 只入队 + 独立 worker 执行」模式：

| 项 | 内容 |
|---|---|
| Web Service | `tradingagents-astock.service`，需设 `Environment="TRADINGAGENTS_ANALYSIS_EXECUTOR=worker"` |
| Worker Service | `tradingagents-analyze.service`（`deploy/` 下有单元模板） |
| 队列文件 | `~/.tradingagents/analysis_queue.json`（Web 与 worker 通过 flock 原子读写） |
| 互斥锁 | `~/.tradingagents/analyze.worker.lock`（全局仅一个 worker） |
| 并发上限 | `TRADINGAGENTS_MAX_PARALLEL`（默认 3，两个 service 都要设一致） |
| 自动续跑上限 | `TRADINGAGENTS_MAX_AUTO_RESUME`（默认 2；lease 回收 + 启动孤儿入队共用） |
| Lease 心跳超时 | `TRADINGAGENTS_QUEUE_LEASE_TTL`（默认 180 秒） |

中断后自动继续（生产 worker）：

1. **优雅停机**：`SIGTERM` 把 in-flight 写回队列（不增加续跑次数），`TimeoutStopSec=30`
2. **Lease**：出队后任务留在 `leases`；心跳超时或进程崩溃后回收并 `resume_count+1` 入队
3. **启动恢复**：有 checkpoint 的 `running` 孤儿自动入队；达上限则标 error，需侧栏手动续

部署 worker 单元后务必：

```bash
sudo cp deploy/tradingagents-analyze.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart tradingagents-analyze.service
```

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

## 美股 GUI（侧栏选「美股」）

侧栏选美股后，分析走 `web/us_bridge`：本进程只做进度桥接，真正跑原版
[TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
子进程（Yahoo Finance 数据），与 A 股代码仓隔离。

| 项 | 内容 |
|---|---|
| 代码目录 | `/home/solo/TradingAgents` |
| 环境变量 | `US_TRADINGAGENTS_ROOT`、`US_TRADINGAGENTS_PYTHON`（写在两个 systemd 单元里） |
| API Key | 由 A 股仓 `.env` / 继承的进程环境注入子进程（子进程也会读 US 仓自己的 `.env`，`override=False`） |

**首次安装美股桥接：**

```bash
ssh solo@m.wcc.io
git clone https://github.com/TauricResearch/TradingAgents.git /home/solo/TradingAgents
cd /home/solo/TradingAgents
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e .
# 可选：把常用 LLM key 同步一份到 US 仓（子进程也会继承 A 股 .env）
# cp /home/solo/TradingAgents-astock/.env /home/solo/TradingAgents/.env

cd /home/solo/TradingAgents-astock
sudo cp deploy/tradingagents-astock.service /etc/systemd/system/
sudo cp deploy/tradingagents-analyze.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart tradingagents-astock.service tradingagents-analyze.service
```

本地开发默认 `US_TRADINGAGENTS_ROOT=/Users/solo/workspace/tradingAgents`；
也可用 `.env` 覆盖（见 `.env.example`）。

## 工作流总结

```
日常:  dev → feat/xxx → PR → dev → 测试 → push → 部署（git pull + restart）
上游:  main → pull upstream → dev → merge → 解决冲突
```
