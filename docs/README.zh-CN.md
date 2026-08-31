# Agent Hub

[![CI](https://github.com/dzrdzrdzr/Agent-Hub/actions/workflows/ci.yml/badge.svg)](https://github.com/dzrdzrdzr/Agent-Hub/actions/workflows/ci.yml)
[![MIT License](https://img.shields.io/badge/license-MIT-74c7a2)](../LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab)](../agentd/pyproject.toml)

**面向长时间 AI 编程任务的本地监督系统：持久化任务、检测卡死、恢复进程、监控训练，并让规划模型把执行工作交给 Cline/DeepSeek，而不需要一直保持 VS Code 或 SSH 在线。**

[English](../README.md) · [架构设计](../DESIGN.md) · [VS Code 扩展](../extension/README.md) · [提交问题](https://github.com/dzrdzrdzr/Agent-Hub/issues/new?template=bug_report.yml)

> 当前状态：**Alpha**。守护进程、CLI、Goal 编排、恢复机制和 VS Code 侧边栏已经实现，但仍主要面向能够检查本地进程与日志的技术用户。

## 它解决什么问题

长时间 Agent 任务经常不是败在模型能力，而是败在运行管理：

- VS Code 或 SSH 断开后任务丢失；
- Executor 卡住但不退出；
- Agent 启动训练后自身先结束；
- Planner 不断轮询日志，浪费 Token；
- daemon 重启后不知道原任务是否仍存活；
- 自动重试误启动同一任务两次。

Agent Hub 把这些机械工作交给本地守护进程：

```text
规划：Codex / 其他推理 Agent
              │
              ▼
        Agent Hub daemon
  持久化 · Watchdog · 恢复 · 预算
              │
              ▼
执行：Cline CLI / DeepSeek
              │
              └── 测试、构建、训练进程
```

只有真正需要判断下一步时才调用规划模型；等待、PID 检查、日志监控、重试和状态保存都不需要 LLM。

## 已实现能力

- SQLite 保存任务、Goal、事件、状态变化和预算。
- Cline 子进程启动、卡死检测、有限重试和进程组取消。
- daemon 重启后重新附着仍存活的任务，避免盲目重复启动。
- 训练进程监控与结果整理。
- Planner/Executor Goal 闭环。
- `127.0.0.1:19876` 上的 JSON-Lines IPC。
- 仅依赖 Python 标准库的 CLI 客户端。
- VS Code 中的 Goal、Task、实时输出、取消和 daemon 状态。
- 网络、保护路径、禁止命令和执行时长等安全限制。
- 可重复的 Mock 演示与测试模式。

## 五分钟演示

不需要真实 Cline 或 Codex 账户：

```bash
git clone https://github.com/dzrdzrdzr/Agent-Hub.git
cd Agent-Hub

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e "./agentd[dev]"

cp config.test.yaml config.local.yaml
export AGENT_HUB_CONFIG="$PWD/config.local.yaml"

agent-hub-daemon
```

保留 daemon 终端，再打开一个终端：

```bash
source .venv/bin/activate
agent-hub ping
agent-hub goal "创建一个确定性的 Mock 任务并完成它"
agent-hub status
```

运行状态写入 `.agent-hub/`，该目录不会提交到 Git。

## 接入真实 Executor

要求：

- Python 3.10 或更新版本；
- Cline CLI 位于 `PATH`，或在配置中写绝对路径；
- 使用 Goal 规划闭环时需要 Codex CLI；
- daemon 的主要测试环境是 Linux/WSL；
- VS Code Remote SSH 可通过扩展使用。

安装并生成本地配置：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e "./agentd[dev]"

agent-hub init --path config.local.yaml
export AGENT_HUB_CONFIG="$PWD/config.local.yaml"
agent-hub-daemon
```

另一个终端中：

```bash
agent-hub ping
agent-hub submit "运行测试，修复第一个真实失败，并列出修改文件"
agent-hub status
```

启动多步 Goal：

```bash
agent-hub goal \
  "在不削弱断言的前提下让当前项目通过测试" \
  --completion-criteria "现有测试全部通过" \
  --stop-conditions "连续失败三次后停止" \
  --max-iterations 6 \
  --max-failures 3
```

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `agent-hub init` | 生成安全的本机配置模板 |
| `agent-hub ping` | 检查 daemon |
| `agent-hub submit "prompt"` | 提交单次执行任务 |
| `agent-hub goal "objective"` | 启动规划/执行闭环 |
| `agent-hub status` | 查看任务 |
| `agent-hub log TASK_ID` | 查看任务输出 |
| `agent-hub cancel TASK_ID` | 取消任务及其进程组 |
| `agent-hub delete-goal GOAL_ID` | 删除已结束 Goal 及相关记录 |

使用 `--json` 可输出机器可读结果。

## VS Code 扩展

扩展可携带 daemon 源码并自动启动，提供：

- Goals 与 Tasks 两个视图；
- 实时任务输出；
- daemon 状态和日志；
- 提交、取消、删除、刷新等操作；
- 供其他 Planner 扩展调用的 API。

构建 VSIX：

```bash
cd extension
npm ci
npm run package
```

## 配置与安全

仓库中的 `config.yaml` 是通用安全基线。本机路径、代理和环境变量应写入已忽略的 `config.local.yaml`。

默认策略：

- IPC 只监听回环地址；
- 安全策略默认禁止网络；
- 执行有时限；
- daemon 退出默认不杀死独立的 Executor/训练进程；
- 凭据放在模型提供方自己的配置中；
- `.agent-hub/`、日志、`.env`、`config.local.yaml` 不进入 Git。

自动修改重要工程前，应配置 `protected_paths`、`forbidden_commands`，并使用版本控制和备份。详见 [SECURITY.md](../SECURITY.md)。

## 开发

```bash
python -m pip install -e "./agentd[dev]"
AGENT_HUB_CONFIG="$PWD/config.test.yaml" pytest agentd/tests -q

cd extension
npm ci
npm run compile
```

也可以在仓库根目录运行：

```bash
make test
```

Agent Hub 不保证 Executor 一定写出正确代码。它提供的是持久化、可观测、恢复、有限重试和清晰的 Planner/Executor 边界。
