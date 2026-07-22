# Agent Hub

> **让 Codex 低消耗指挥 DeepSeek，持续完成模型修改、训练、调参和结果审查闭环的常驻 Agent 控制系统。**

---

## 目标

大模型做复杂工程时有一个根本矛盾：**最强的规划模型（Codex、KimiCode）token 昂贵，最强的执行模型（DeepSeek、Cline）需要持续运行数小时甚至数天。** 人类工程师不可能盯着终端看几个小时。

Agent Hub 解决的就是这个分工问题：

```
Codex / KimiCode          →  制定目标、审查修改、分析实验、决定下一步
     │                           (只在真正需要判断时介入)
     │ 委派任务
     ▼
Agent Hub (守护进程)       →  持续监控、等待、调度、恢复
     │                           (把所有机械工作自动化)
     │ 调度执行
     ▼
Cline + DeepSeek           →  改代码、跑测试、启动训练、整理结果
                                 (持续执行数小时，无需人工监护)
```

**一句话概括**：你负责"要做什么"，Agent Hub 负责盯着 DeepSeek 执行，你只需要回来看结果。

具体能力：

- **检查 Cline 是否完成、卡死或暴毙**——持续 stall 检测 + PID 追踪
- **检查训练是否运行、结束或异常**——监控子进程、日志 mtime、退出码
- **自动恢复和有限重试**——daemon 重启后 re-attach 到存活任务，不重复启动
- **VS Code 或 SSH 断开后继续运行**——Cline 和训练进程独立于 daemon 生命周期
- **等待过程中不消耗 Codex 或 DeepSeek token**——只有状态变化时才推送通知

---

## 架构

```
┌─────────────────────────────────┐
│  VS Code 扩展 (extension/)      │  ← 侧边栏 UI：提交任务、查看状态、实时输出
│  CLI 客户端 (scripts/)          │  ← 任何 shell/脚本 都能提交任务
└───────────┬─────────────────────┘
            │ TCP 127.0.0.1:19876 (JSON-Lines)
┌───────────▼─────────────────────┐
│  守护进程 agentd/ (Python)      │
│  ┌───────────────────────────┐  │
│  │ IPC Server (server.py)    │  │  ← 请求/响应 + 推送
│  │ Task Manager              │  │  ← 状态机引擎
│  │ Cline Executor            │  │  ← 异步 spawn, stall 监控
│  │ Recovery 模块             │  │  ← 重启恢复
│  │ Watchdog (60s)            │  │  ← 僵尸进程检测
│  │ Janitor (每小时)          │  │  ← 清理过期任务
│  │ Safety Guard              │  │  ← 危险命令拦截
│  │ Budget Tracker            │  │  ← 预算控制
│  └───────────────────────────┘  │
│  SQLite (state.sqlite)          │
└───────────┬─────────────────────┘
            │ 子进程管理
┌───────────▼─────────────────────┐
│  Cline CLI (node)               │  ← DeepSeek 等模型执行代码
│  ├─ 训练脚本 (fork)             │  ← 后台训练，独立于 Cline
│  └─ 测试/数据处理               │
└─────────────────────────────────┘
```

### 状态机

```
QUEUED → CLINE_STARTING → CLINE_RUNNING → CLINE_SUCCEEDED
                ↑              ↓    ↓
                │    CLINE_FAILED    CLINE_STALLED
                │         ↓              ↓
                └─── retry loop ─────────┘
```

---

## 快速开始

### 1. 启动守护进程

```bash
cd /path/to/Codex_Cline
PYTHONPATH="$PWD/agentd" AGENT_HUB_CONFIG="$PWD/config.yaml" \
  nohup python -B -u -m agent_hub.main \
  >> .agent-hub/logs/agentd.spawn.log 2>&1 &
```

### 2. 提交任务（任意终端）

```bash
# 验证 daemon 存活
python3 scripts/agent-hub.py ping

# 提交任务；未写 --cwd 时自动使用调用命令所在目录
python3 scripts/agent-hub.py submit "你的 prompt（越具体越好）"

# 从任意项目目录启动一个 GOAL
python3 /path/to/Codex_Cline/scripts/agent-hub.py goal "完成当前项目的测试修复"

# 5. 删除已完成的目标（清理数据库和日志）
python3 scripts/agent-hub.py delete-goal <goal-id>

# 查看状态
python3 scripts/agent-hub.py status

# 查看输出
python3 scripts/agent-hub.py log <task-id>

# 取消任务
python3 scripts/agent-hub.py cancel <task-id>
```

### 3. VS Code 扩展（可选）

安装扩展后，侧边栏直接操作，无需手动敲命令。任务和 GOAL 会携带当前
VS Code workspace 的绝对目录；daemon 可以运行在 Agent Hub 自己的目录，
实际 Cline 子进程会在目标项目目录执行。VSIX 内含 daemon 源码，因此从其他
工程首次打开时也能自动启动；开发时可用 `agentHub.daemonRoot` 指定本仓库。

---

## 项目结构

```
Codex_Cline/
  agentd/              Python 守护进程 (agent_hub)
    agent_hub/
      main.py          入口，主循环 + watchdog + janitor
      server.py        IPC server (TCP JSON-Lines)
      task_manager.py  状态机引擎
      cline_executor.py Cline 子进程管理 + stall 监控
      recovery.py      重启恢复逻辑
      process_watcher.py 进程身份验证（加权匹配）
      safety_guard.py  危险命令拦截
      budget_tracker.py 预算控制
      db.py            SQLite 数据层
      config.py        配置加载
    tests/             9 个测试，全部通过
  extension/           VS Code 扩展 (TypeScript)
  scripts/
    agent-hub.py       CLI 客户端 (stdlib only)
  config.yaml          守护进程配置
  DESIGN.md            系统设计文档
```

---

## 配置 (config.yaml)

```yaml
agentd:
  cline:
    kill_on_shutdown: false     # daemon 退出不杀 Cline（默认）
    stall_threshold_seconds: 300
    max_retries: 1
    retention_days: 30
  safety:
    allow_network: false        # 默认禁止 Cline 访问网络
```

完整配置见 `config.yaml`。

---

## 运行测试

```bash
cd agentd/
PATH="/path/to/cline/bin:$PATH" python -m pytest tests/ -v -p no:dash
# 预期: 9 passed
```

---

## 设计原则

- **零模型调用用于机械检查**——PID、退出码、日志轮询不消耗 token
- **进程组终止**——杀任务时连带所有子进程
- **加权进程身份**——env var > cmdline tail > start_time，不因 shebang 脚本误判
- **per-task 锁**——spawn/exit/stall/stop 持锁执行，消除竞态
- **原子 SQLite 写入**——状态变更使用事务
- **并发 TCP 推送**——慢客户端超时 2s 自动驱逐
- **不重复启动同一任务**——双重启动保护

---

## 许可

MIT
