# Agent Hub：OpenAI Codex / ChatGPT 编程 Agent 监督系统

[![CI](https://github.com/dzrdzrdzr/Agent-Hub/actions/workflows/ci.yml/badge.svg)](https://github.com/dzrdzrdzr/Agent-Hub/actions/workflows/ci.yml)
[![MIT License](https://img.shields.io/badge/license-MIT-74c7a2)](../LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab)](../agentd/pyproject.toml)

**面向长时间 OpenAI Codex / Codex CLI 编程任务的本地监督系统，也适用于通过 ChatGPT 登录的 Codex 配置，以及 Cline/DeepSeek 执行器。它负责持久化任务、检测卡死、恢复进程、监控训练，让任务在 VS Code 或 SSH 断开后仍可继续。**

规划者可以是 OpenAI Codex 或其他推理 Agent；执行者可以是使用 DeepSeek 等模型的 Cline CLI。等待、PID 检查、日志监控、重试和状态保存均由本地程序完成，不需要规划模型持续消耗 Token。

[English](../README.md) · [架构设计](../DESIGN.md) · [VS Code 扩展](../extension/README.md) · [机器可读摘要](../llms.txt) · [提交问题](https://github.com/dzrdzrdzr/Agent-Hub/issues/new?template=bug_report.yml)

> 当前状态：**Alpha**。守护进程、CLI、Goal 编排、恢复机制、训练监控和 VS Code 侧边栏已经实现，但仍主要面向能够检查本地进程与日志的技术用户。

> 本项目为独立社区项目，与 OpenAI、ChatGPT、Codex、Cline、DeepSeek、Microsoft 或 Visual Studio Code 无官方隶属或背书关系。

## 常见搜索问题

### VS Code 或 SSH 断开后，怎么让 OpenAI Codex 继续运行？

Agent Hub 通过本地 daemon 启动并跟踪任务。Executor 和训练进程可以独立于当前 VS Code/SSH 会话继续运行；daemon 重启后还可以重新附着到仍然存活的任务。

### Codex 使用 ChatGPT 登录时还能用吗？

可以。Agent Hub 调用已安装的 Codex CLI，不替换 Codex 的模型提供方和登录凭据。ChatGPT/OpenAI 身份验证仍由 Codex 自己的配置管理，Agent Hub 只管理进程、Goal、日志、状态和预算。

### 怎么让 Codex 负责规划，让 Cline/DeepSeek 持续改代码？

启动一个 Goal。Codex 只在需要制定计划、审查结果和决定下一步时介入；Cline/DeepSeek 负责受限执行，daemon 负责等待、监控、恢复和有限重试。

### 怎么监控长时间 AI 编程任务而不反复消耗 Token？

Agent Hub 使用本地进程状态、SQLite、退出码、时间戳和日志更新时间做机械检查，这些操作不调用大模型。

## 它解决什么问题

长时间 Agent 任务经常不是败在模型能力，而是败在运行管理：

- VS Code、ChatGPT、Codex 或 SSH 断开后任务丢失；
- Executor 卡住但不退出；
- Agent 启动训练后自身先结束；
- Planner 不断轮询日志，浪费 Token；
- daemon 重启后不知道原任务是否仍存活；
- 自动重试误启动同一任务两次。

Agent Hub 把这些机械工作交给本地守护进程：

```text
规划：OpenAI Codex / Codex CLI / 其他推理 Agent
                     │
                     ▼
               Agent Hub daemon
       持久化 · Watchdog · 恢复 · 预算
                     │
                     ▼
执行：Cline CLI / DeepSeek / 其他执行模型
                     │
                     └── 测试、构建、训练进程
```

只有真正需要判断下一步时才调用规划模型；等待、PID 检查、日志监控、重试和状态保存都不需要 LLM。

## 已实现能力

- SQLite 保存任务、Goal、事件、状态变化和预算。
- Cline 子进程启动、卡死检测、有限重试和进程组取消。
- OpenAI Codex 规划执行与 Planner/Executor Goal 闭环。
- daemon 重启后重新附着仍存活的任务，避免盲目重复启动。
- 训练进程监控与结果整理。
- `127.0.0.1:19876` 上的 JSON-Lines IPC。
- 仅依赖 Python 标准库的 CLI 客户端。
- VS Code 中的 Goal、Task、实时输出、取消和 daemon 状态。
- 网络、保护路径、禁止命令和执行时长等安全限制。
- 可重复的 Mock 演示与测试模式。

## 五分钟演示

不需要真实 Cline、DeepSeek、OpenAI Codex 或 ChatGPT 账户：

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

## 接入真实 Codex 和 Executor

要求：

- Python 3.10 或更新版本；
- Cline CLI 位于 `PATH`，或在配置中写绝对路径；
- 使用 Goal 规划闭环时需要 OpenAI Codex CLI；
- Codex 已完成自身身份验证，包括适用时的 ChatGPT 登录；
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
| `agent-hub goal "objective"` | 启动 Codex 规划/执行闭环 |
| `agent-hub status` | 查看任务 |
| `agent-hub log TASK_ID` | 查看任务输出 |
| `agent-hub cancel TASK_ID` | 取消任务及其进程组 |
| `agent-hub delete-goal GOAL_ID` | 删除已结束 Goal 及相关记录 |

使用 `--json` 可输出机器可读结果。

## VS Code 扩展

扩展可携带 daemon 源码并自动启动，提供：

- Goals 与 Tasks 两个视图；
- Codex、Cline、DeepSeek、测试、构建和训练的实时输出；
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
- OpenAI、ChatGPT、Codex、Cline 和 DeepSeek 凭据保留在各自提供方的配置中；
- `.agent-hub/`、日志、`.env`、`config.local.yaml` 不进入 Git。

自动修改重要工程前，应配置 `protected_paths`、`forbidden_commands`，并使用版本控制和备份。详见 [SECURITY.md](../SECURITY.md)。

## 搜索与机器可读入口

仓库已提供：

- [`llms.txt`](../llms.txt)：项目身份、别名、能力和规范链接；
- [`docs/index.html`](index.html)：可作为 GitHub Pages 发布的结构化搜索页；
- [`AGENTS.md`](../AGENTS.md)：供 OpenAI Codex 和其他 Agent 直接读取的接入说明；
- [`sitemap.xml`](sitemap.xml)：静态站点 Sitemap。

常用搜索词包括：**OpenAI Codex Agent 监督系统**、**ChatGPT Codex 长时间任务**、**SSH 断开后继续运行 Codex**、**Codex Cline DeepSeek 编排**、**AI 编程 Agent 看门狗**、**Codex 训练进程监控**。

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

Agent Hub 不保证 OpenAI Codex、ChatGPT、Cline、DeepSeek 或其他 Executor 一定写出正确代码。它提供的是持久化、可观测、恢复、有限重试和清晰的 Planner/Executor 边界。

如果这个项目解决了无人值守的 Codex/Agent 工作流，Star 能帮助其他 OpenAI Codex 和 ChatGPT 用户找到它。
