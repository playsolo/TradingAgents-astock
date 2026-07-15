# 维护指南

## Remote 结构

| Remote | URL |
|---|---|
| `origin` | `git@github.com:playsolo/TradingAgents-astock.git`（你的 fork） |
| `upstream` | `git@github.com:simonlin1212/TradingAgents-astock.git`（来源） |

## 分支策略

- `main` — 稳定发布版，与 upstream/main 对齐
- `dev` — 日常开发集成分支，包含你所有改动
- `feat/*` / `fix/*` — 功能/修复分支，从 dev 切出，PR 合并回 dev

## 日常操作

### 日常开发

```bash
git checkout dev
git checkout -b feat/xxx   # 或 fix/xxx
# ... 改代码 ...
git add <files>
git commit -m "feat: xxx"
git push -u origin feat/xxx
# 在 GitHub 上提 PR → dev
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

## 工作流总结

```
日常:  dev → feat/xxx → PR → dev → 测试 → main → tag → push
上游:  main → pull upstream → dev → merge → 解决冲突
```
