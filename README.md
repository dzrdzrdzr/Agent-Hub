# Agent Hub — OpenAI Codex and ChatGPT Coding-Agent Supervisor

[![CI](https://github.com/dzrdzrdzr/Agent-Hub/actions/workflows/ci.yml/badge.svg)](https://github.com/dzrdzrdzr/Agent-Hub/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-74c7a2)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab)](agentd/pyproject.toml)
[![VS Code](https://img.shields.io/badge/VS%20Code-extension-4aa3ff)](extension)

**A local process supervisor for long-running OpenAI Codex and Codex CLI jobs—including Codex profiles authenticated through ChatGPT—and for Cline/DeepSeek executors. Agent Hub persists tasks, detects stalls, recovers processes, monitors training, and keeps work running after VS Code or SSH disconnects.**

The planner can be OpenAI Codex or another reasoning agent. The executor can be Cline CLI backed by DeepSeek or another configured provider. Waiting, process checks, log polling, retry bookkeeping, and state persistence run locally without consuming planner-model calls.

[中文说明](docs/README.zh-CN.md) · [Architecture](DESIGN.md) · [Extension guide](extension/README.md) · [Machine-readable summary](llms.txt) · [Report a bug](https://github.com/dzrdzrdzr/Agent-Hub/issues/new?template=bug_report.yml)

> Project status: **alpha**. The daemon, CLI, goal orchestration, recovery, training monitoring, and VS Code sidebar are implemented. The project targets technical users who can inspect local processes and logs.

> This is an independent community project. It is not affiliated with or endorsed by OpenAI, ChatGPT, Codex, Cline, DeepSeek, Microsoft, or Visual Studio Code.

## Common questions this project answers

### How do I keep OpenAI Codex running after VS Code or SSH disconnects?

Agent Hub launches and tracks work through a local daemon. Executor and training processes can continue independently of the current VS Code or SSH session, and recovery can reattach after the daemon restarts.

### Can it supervise Codex CLI when Codex uses ChatGPT authentication?

Yes. Agent Hub launches the installed Codex CLI and does not replace its provider credentials. Authentication remains in Codex's own profile; Agent Hub manages process lifecycle, goals, logs, state, and budgets.

### How do I let Codex plan while Cline or DeepSeek performs long edits?

Start a goal. The planner reviews state and decides the next task only when judgment is required. Cline/DeepSeek performs bounded execution while the daemon handles waiting, monitoring, retry, and recovery.

### How do I monitor long AI coding-agent jobs without repeatedly spending tokens?

Agent Hub uses operating-system process checks, SQLite state, timestamps, exit codes, and log modification times for mechanical monitoring. These checks do not require an LLM call.

## The problem

Long coding-agent jobs often fail for operational reasons rather than model quality:

- VS Code, ChatGPT, Codex, or an SSH session disconnects;
- an executor stalls without exiting;
- a training process outlives the agent that launched it;
- the planner wastes tokens polling logs;
- a daemon restart loses track of work;
- a retry starts the same task twice.

Agent Hub moves those mechanics into a local supervisor.

```text
Planner: OpenAI Codex / Codex CLI / another reasoning agent
                         │
                         │ goals, reviews, next-step decisions
                         ▼
                   Agent Hub daemon
          persistence · watchdog · recovery · budgets
                         │
                         │ bounded execution
                         ▼
Executor: Cline CLI / DeepSeek / configured coding agent
                         │
                         └── tests, builds, training processes
```

## What is implemented

- SQLite-backed tasks, goals, events, transitions, and budgets.
- Cline process spawning with stall detection, bounded retries, and process-group cancellation.
- Codex planner execution and planner/executor goal orchestration.
- Recovery that reattaches to live work after daemon restart without blindly relaunching it.
- Training-process monitoring and result packaging.
- Local JSON-Lines IPC over `127.0.0.1:19876`.
- A stdlib-only CLI client.
- A VS Code sidebar for goals, tasks, live output, cancellation, and daemon status.
- Safety controls for network access, protected paths, forbidden commands, and execution time.
- Mock mode for deterministic demonstrations and tests.

## Five-minute demo

This demonstration does not require a live Cline, DeepSeek, OpenAI Codex, or ChatGPT account.

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

Keep the daemon running and open another terminal:

```bash
source .venv/bin/activate
agent-hub ping
agent-hub goal "Create a deterministic mock task and complete it"
agent-hub status
```

Runtime state is written under `.agent-hub/`, which is ignored by Git.

## Real Codex and executor setup

Prerequisites:

- Python 3.10 or newer;
- Cline CLI available on `PATH`, or an absolute path in `config.local.yaml`;
- OpenAI Codex CLI when using planner-driven goals;
- the normal Codex authentication already configured, including ChatGPT authentication when applicable;
- Linux or WSL as the primary tested daemon environment;
- VS Code Remote SSH when using the bundled remote extension workflow.

Install:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e "./agentd[dev]"

agent-hub init --path config.local.yaml
export AGENT_HUB_CONFIG="$PWD/config.local.yaml"
```

Review `config.local.yaml`, then start the daemon from the repository root:

```bash
agent-hub-daemon
```

Verify and submit work:

```bash
agent-hub ping
agent-hub submit "Run the test suite, fix the first real failure, and report changed files"
agent-hub status
```

Start a planner/executor goal:

```bash
agent-hub goal \
  "Make the current project pass its tests without weakening assertions" \
  --completion-criteria "All existing tests pass" \
  --stop-conditions "Stop after three failed iterations" \
  --max-iterations 6 \
  --max-failures 3
```

## CLI

| Command | Purpose |
| --- | --- |
| `agent-hub init` | Write a safe local configuration template |
| `agent-hub ping` | Check daemon availability |
| `agent-hub submit "prompt"` | Delegate one executor task |
| `agent-hub goal "objective"` | Start an OpenAI Codex planner / executor goal |
| `agent-hub status` | List current tasks |
| `agent-hub log TASK_ID` | Read task output |
| `agent-hub cancel TASK_ID` | Cancel a task and its process group |
| `agent-hub delete-goal GOAL_ID` | Remove a completed goal and related records |

Use `--json` for machine-readable output.

## VS Code extension

The extension bundles the daemon source and can start it automatically. It provides:

- separate **Goals** and **Tasks** views;
- live Codex, Cline, DeepSeek, test, build, and training output;
- daemon status and logs;
- submit, cancel, retry, delete, and refresh commands;
- an extension API for other planner tools.

Build a development VSIX:

```bash
cd extension
npm ci
npm run package
```

Install the generated package with **Extensions: Install from VSIX...**.

## Configuration

The repository's `config.yaml` is a safe generic baseline. Keep machine-specific paths and environment variables in the ignored `config.local.yaml`.

```yaml
agentd:
  ipc:
    transport: tcp
    tcp_host: 127.0.0.1
    tcp_port: 19876
  cline:
    executable: auto
    timeout_seconds: 600
    stall_threshold_seconds: 300
    max_retries: 1
    env: {}
    kill_on_shutdown: false
  safety:
    allow_network: false
    protected_paths: []
    forbidden_commands: []
    max_execution_time_seconds: 3600
```

Point the daemon to a custom file with:

```bash
export AGENT_HUB_CONFIG="$PWD/config.local.yaml"
```

## Safety model

Agent Hub runs tools on the local machine and must be treated like any other automation runner.

Defaults are deliberately conservative:

- IPC binds to loopback;
- network access is disabled in the safety policy;
- task execution has a time limit;
- daemon shutdown does not automatically kill independent executor or training processes;
- OpenAI, ChatGPT, Codex, Cline, and DeepSeek credentials remain in their provider configuration, not this repository;
- local state, logs, `.env`, and `config.local.yaml` are ignored.

Before allowing an agent to modify a valuable workspace, configure `protected_paths`, `forbidden_commands`, version control, and backups. See [SECURITY.md](SECURITY.md).

## Search and machine-readable discovery

For ChatGPT, Codex, coding agents, and web crawlers, the repository provides:

- [`llms.txt`](llms.txt): concise identity, aliases, capabilities, installation, and canonical links;
- [`docs/index.html`](docs/index.html): a static metadata-rich landing page ready for GitHub Pages;
- [`AGENTS.md`](AGENTS.md): direct integration and repository instructions for OpenAI Codex and other agents;
- [`docs/sitemap.xml`](docs/sitemap.xml): sitemap for the optional GitHub Pages site.

Useful search phrases include **OpenAI Codex agent supervisor**, **ChatGPT Codex long-running tasks**, **keep Codex running after SSH disconnect**, **Codex Cline DeepSeek orchestration**, **AI coding-agent watchdog**, and **Codex training-process monitor**.

## Development

Run daemon tests:

```bash
python -m pip install -e "./agentd[dev]"
AGENT_HUB_CONFIG="$PWD/config.test.yaml" pytest agentd/tests -q
```

Compile the extension:

```bash
cd extension
npm ci
npm run compile
```

Or run:

```bash
make test
```

CI tests Python 3.10–3.12, compiles the VS Code extension, and performs a VSIX packaging smoke test.

## Repository layout

```text
Agent-Hub/
├── agentd/                 Python daemon package
│   ├── agent_hub/          state, recovery, execution and orchestration
│   └── tests/
├── extension/              VS Code extension
├── scripts/                bootstrap and compatibility CLI wrapper
├── config.yaml             safe default configuration
├── config.test.yaml        deterministic mock configuration
├── AGENTS.md               Codex and agent integration instructions
└── DESIGN.md               detailed architecture
```

## Scope

Agent Hub does not guarantee that OpenAI Codex, ChatGPT, Cline, DeepSeek, or another executor will produce correct code. It provides operational control: persistence, observability, recovery, bounded retries, process supervision, and a clean planner/executor boundary.

Contributions should preserve those invariants and include tests for process lifecycle or state changes. See [CONTRIBUTING.md](CONTRIBUTING.md).

If this solves a real unattended-agent workflow, starring the repository helps other OpenAI Codex and ChatGPT users discover it.
