# Agent Hub - Agent Instructions

## Project Structure
```
Codex_Cline/           (workspace root; runs on Linux over SSH)
  agentd/              Python daemon (agent-hub-daemon, package: agent_hub)
  extension/           VS Code extension "Agent Hub" (TypeScript, id: agenthub.agent-hub)
  docs/                Documentation (historical, GAUSS-era)
  scripts/             Install/deploy scripts + agent-hub.py CLI client
  config.yaml          Daemon configuration
  DESIGN.md            System design document (historical)
  .agent-hub/          Runtime data (gitignored)
  .agent-control/      Legacy runtime data from the GAUSS era (gitignored, safe to delete)
```

## What Agent Hub Does

Agent Hub is a **task scheduling daemon** that lets any agent (Codex, KimiCode, Cline, shell)
submit coding tasks to a `cline` CLI subprocess. It manages the full lifecycle:

```
QUEUED → CLINE_STARTING → CLINE_RUNNING → CLINE_SUCCEEDED / CLINE_FAILED / CLINE_STALLED
              ↑                                    ↑
              └──── retry loop ────────────────────┘
```

Key behaviors to understand:

- **Daemon survives restarts**: by default (`kill_on_shutdown: false`), stopping the daemon
  does NOT kill running Cline processes. They keep executing. On restart, recovery re-attaches
  monitors to surviving processes via `attach_monitor()`.
- **Process identity is weighted**: `AGENT_HUB_TASK_ID` env var is the strongest ownership
  signal; cmdline tail-match (last 5 args) is the fallback. This fixes the shebang-wrapping
  issue where `cline` (a `node` shebang script) would be misidentified.
- **Per-task locking**: spawn, exit, stall, and stop all acquire a per-task `asyncio.Lock`
  before mutating state — no more double-spawn races.
- **Stall detection is continuous**: the monitor loops every ~75s (stall_threshold/4) until
  the task finishes, not just one check.
- **Process group termination**: `terminate_process_tree()` kills the Cline process *and*
  all its children (shell, training scripts, etc.) via `os.killpg()`.
- **Watchdog (60s)**: reconciles DB tasks in RUNNING state against the OS process table;
  auto-retries tasks whose PID has died.
- **Janitor (hourly)**: deletes terminal tasks older than `retention_days` (default 30d)
  and their log files, keeping DB size bounded.
- **TCP push is concurrent**: `asyncio.gather` + 2s timeout per client; slow/stuck
  clients are evicted rather than blocking all others.
- **Log tail uses reverse-seek**: reads last N lines from the end of the file without
  loading the entire file into memory.

## Using Agent Hub from ANY agent session (Codex, KimiCode, Cline, shell)

The daemon runs on this host and accepts task submissions from anything that can
open a TCP socket. You do NOT need VS Code APIs.

### Easiest: the CLI client (stdlib-only, any python3)
```bash
python3 scripts/agent-hub.py ping                 # is the daemon up?
python3 scripts/agent-hub.py submit "your prompt" # submit a task -> prints task id
python3 scripts/agent-hub.py status               # all tasks, newest first
python3 scripts/agent-hub.py log TASK_ID          # conversation/output of a task
python3 scripts/agent-hub.py cancel TASK_ID
python3 scripts/agent-hub.py status --json        # machine-readable output
```
If the daemon is not running, start it (from the workspace root):
```bash
PYTHONPATH="$PWD/agentd" AGENT_HUB_CONFIG="$PWD/config.yaml" \
  nohup /data7/hanzaidao/miniconda3/envs/GAUSS-SSC/bin/python -B -u -m agent_hub.main \
  >> .agent-hub/logs/agentd.spawn.log 2>&1 &
```
(Any python with `pyyaml` + `psutil` works; the one above also has the `cline` CLI.)

### Raw protocol (if you cannot use the script)
TCP `127.0.0.1:19876`, JSON-Lines. Send one request per line:
`{"type":"request","id":"1","method":"submit_task","params":{"prompt":"...","cwd":"/abs/path"}}`
Methods:

| Method | Params | Notes |
|--------|--------|-------|
| `ping` | `{}` | Returns `{pong, version, workspace}` — verify workspace match |
| `submit_task` | `{prompt, cwd?, task_type?}` | Spawns Cline; returns `{task_id, state}` |
| `get_status` | `{active_only?, limit?, offset?}` | Paginated; `active_only` uses SQL-side filter |
| `get_task` | `{task_id}` | Full task record |
| `cancel_task` | `{task_id}` | Kills process tree |
| `approve_task` | `{task_id}` | Approve WAITING_APPROVAL → spawn |
| `get_log_tail` | `{task_id, lines?}` | Reverse-seek, efficient for large logs |
| `get_budget_status` | `{}` | Budget tracker status |

Responses: `{"type":"response","id":"1","result":...}`; the daemon
also interleaves `{"type":"push","event":"state_changed","data":{...}}` events —
skip lines whose `type` is not `"response"` or whose `id` doesn't match your request.

### From a VS Code extension
`vscode.commands.executeCommand('agent-hub.submitTask', {prompt, cwd})`, or use the
exported API of extension `agenthub.agent-hub` (`submitTask/cancelTask/getStatus/
onDidChangeState`). See `extension/README.md`.
The extension verifies workspace match on first ping and debounces refresh at 500ms.

## Configuration (config.yaml)

```yaml
agentd:
  cline:
    kill_on_shutdown: false    # Default: do NOT kill Cline on daemon stop
    retention_days: 30         # Janitor: delete terminal tasks older than this
    max_task_history: 1000     # Janitor: warn if active tasks exceed this
    stall_threshold_seconds: 300
    max_retries: 1
    # ... (timeout, mock, env, etc.)
```

Never set `kill_on_shutdown: true` unless you explicitly want the old behavior
where daemon exit terminates all running tasks.

## Build Commands
```bash
# Python daemon (deps: pyyaml, psutil; run from workspace root)
PYTHONPATH=agentd AGENT_HUB_CONFIG=config.yaml python -B -u -m agent_hub.main

# VS Code extension
cd extension/ && npm install && npm run compile && npm run package

# Tests (pytest + pytest-asyncio required; add cline to PATH)
cd agentd/ && PATH="/data7/hanzaidao/miniconda3/envs/GAUSS-SSC/bin:$PATH" python -m pytest tests/ -v -p no:dash
# Expected: 9 passed
```

## Environment Notes (SSH host)
- Use the conda env Python designated for this project (currently GAUSS-SSC); it has `cline` CLI alongside `node`.
- The extension picks the interpreter via `agentHub.pythonPath` (.vscode/settings.json) or auto-detection; never hardcode absolute paths in code.
- Daemon listens on 127.0.0.1:19876 (TCP, JSON-Lines). Singleton per workspace: probe the port before spawning.

## Coding Rules
- No hardcoded paths (use config or auto-discovery)
- All subprocess calls use asyncio (non-blocking)
- Process ownership verified before any kill (weighted: env var > cmdline tail > start_time)
- Zero model calls for mechanical checks (PID, exit code, log polling)
- Status writes use atomic SQLite transactions (`PRAGMA busy_timeout=5000; synchronous=NORMAL`)
- All state transitions logged with trigger + timestamp
- Per-task `asyncio.Lock` must be held for any state mutation (spawn/exit/stall/stop)
- API keys NEVER committed to repo
- Extension activate() must never block on the daemon (boot in background)
- Extension tree refresh must use 500ms debounce to avoid push-storm

## Forbidden Operations
- git reset --hard
- rm -rf (outside workspace)
- pip install without user approval
- Auto-push to remote
- Kill processes not owned by Agent Hub
- Write outside workspace directory
- Terminate Cline processes on daemon shutdown (unless kill_on_shutdown=true)
- Use full cmdline hash for process identity (broken on shebang scripts like `cline`)

## Definition of Done
- All Python tests pass (9/9)
- Daemon starts and listens on IPC
- VS Code extension compiles without errors
- VSIX package generated (extension/agent-hub-*.vsix)
- End-to-end IPC ping works (verify `workspace` in response)
- Mock mode demo works
- Linux deploy files present
- Recovery re-attaches monitoring for surviving tasks
- Watchdog catches zombie processes
- Janitor cleans old terminal tasks

## Workflow: When to Delegate to Agent Hub

Agent Hub exists so that a **planning/reviewing agent** (you, Codex/KimiCode) can stay
lightweight while **heavy execution** is offloaded to Cline CLI (backed by DeepSeek or
another coding model). When working on ANY project in this workspace, use this decision tree:

### Delegate to Agent Hub when:
- **Multi-file refactors** — touching 3+ files, especially across different modules
- **Code generation from scratch** — creating new modules, scaffolding, boilerplate
- **Running test suites** — `pytest`, `npm test`, or any test runner that may take >30s
- **Starting long-running processes** — training scripts, data processing, builds
- **Applying review feedback** — you've reviewed code and need changes executed
- **Parallel work** — submit multiple tasks and let Cline handle them concurrently

### Do NOT delegate when:
- **Single-line fixes** — changing one import, one typo, one config value
- **Reading/analyzing code** — you can grep and read faster than spawning a subprocess
- **Quick shell commands** — `ls`, `git status`, `cat`, package installs
- **The daemon is down** — start it first, then delegate

### Workflow pattern
```
1. YOU (planner) analyze the codebase, read files, understand the problem
2. YOU write a clear, specific prompt describing what to change
3. YOU submit to Agent Hub: python3 scripts/agent-hub.py submit "your detailed prompt"
4. Agent Hub spawns Cline CLI → Cline does the actual file edits, runs tests
5. YOU monitor: python3 scripts/agent-hub.py status / log TASK_ID
6. YOU review the result, iterate if needed
```

### Example session
```bash
# You've analyzed the code and know exactly what needs to change
python3 scripts/agent-hub.py submit \
  "Refactor agentd/agent_hub/server.py: extract _h_get_log_tail into a separate
   async function, add rate limiting (max 10 calls/sec), and update the handler
   registration in _handlers property. Run pytest after."

# Check progress
python3 scripts/agent-hub.py status

# Review output
python3 scripts/agent_hub.py log task-a1b2c3d4
```

The key insight: **you plan, Cline executes**. Your prompts to Agent Hub should be as
specific as possible — include file paths, function names, expected behavior, and
validation steps. The more detail you give, the better Cline performs.
