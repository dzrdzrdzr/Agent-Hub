<!--
  GAUSS Agent Control Center — System Design (REVISED)
  Date: 2026-07-20 | Version: v0.2-revised
  Status: Pre-coding, all corrections applied
  Changes from v0.1:
    - Dev/deploy separation (Windows local vs Linux remote)
    - Extension runs on Remote Extension Host, not Windows UI
    - Table count: 9 (not 8), task_locks retained
    - Phase 1 state machine shrunk to 8 states
    - CLINE_COMPLETED semantics fixed; CLINE_SUCCEEDED added
    - Async Cline executor (non-blocking)
    - Process identity: PID+starttime+UID+cmdhash+cwd+ppid+run_id
    - Training ownership: GAUSS_AGENT_TASK_ID env var injection
    - Safety Guard expanded beyond blacklist
    - Phase 1 MVP: 14 acceptance criteria, strict out-of-scope
    - Phase 1 implementation order: 13 steps
    - Phase 1 test matrix + recovery algorithm
-->

# GAUSS Agent Control Center — System Design

---

## Table of Contents

1. [Development vs Deployment](#1-development-vs-deployment)
2. [Repository Structure](#2-repository-structure)
3. [System Architecture](#3-system-architecture)
4. [Module Division and Boundaries](#4-module-division-and-boundaries)
5. [State Machine](#5-state-machine)
6. [Data Tables](#6-data-tables)
7. [IPC Protocol](#7-ipc-protocol)
8. [Process Identity Verification](#8-process-identity-verification)
9. [Training Process Ownership](#9-training-process-ownership)
10. [Safety Guard](#10-safety-guard)
11. [Async Cline Execution](#11-async-cline-execution)
12. [Restart Recovery Algorithm](#12-restart-recovery-algorithm)
13. [Phase 1: Scope & Implementation Order](#13-phase-1-scope--implementation-order)
14. [Phase 1: Test Matrix](#14-phase-1-test-matrix)
15. [Full System Phases (Overview)](#15-full-system-phases-overview)
16. [Risk Checklist](#16-risk-checklist)

---

## 1. Development vs Deployment

### 1.1 Two Environments

| | Development | Deployment |
|---|---|---|
| Machine | Windows (local) | Linux server (Remote-SSH: imed01) |
| Role | Write code, commit, push | Run daemon, execute tasks |
| Code location | D:/tpc/Codex_Cline | ~/gauss-agent/ (synced via Git) |
| VS Code Extension | Source in extension/ | Packaged, installed on Remote Extension Host |
| gauss-agentd | Source in agentd/ | ~/.local/share/gauss-agent/agentd/ |
| SQLite DB | N/A | ~/.local/share/gauss-agent/state.sqlite |
| Unix Socket | N/A | /gauss-agent.sock |
| Logs | N/A | ~/.local/share/gauss-agent/logs/ |
| systemd unit | N/A | ~/.config/systemd/user/gauss-agentd.service |
| Config | N/A | ~/.local/share/gauss-agent/config.yaml |

### 1.2 Deployment Path Mapping

Development (Win): D:/tpc/Codex_Cline  ->  Deployment (Linux): ~/gauss-agent/

| Dev (Windows) | Deploy (Linux) |
|---|---|
| extension/ (VSIX source) | Installed via VS Code Extensions on Remote-SSH |
| agentd/gauss_agentd/ (Python src) | ~/.local/share/gauss-agent/agentd/ |
| docs/ | ~/.local/share/gauss-agent/docs/ |
| scripts/ | ~/.local/share/gauss-agent/scripts/ |
### 1.3 Extension: Must Run on Remote Extension Host

The VS Code extension MUST declare in package.json:

  extensionKind: [workspace]

This ensures the extension runs on the Remote-SSH Extension Host (Linux side),
giving it access to the Unix Socket at /gauss-agent.sock.
Without workspace extensionKind, the extension would run on Windows UI side
and would have NO access to the Linux Unix Socket.


### 1.4 Daemon Installation (on Linux server)

`ash
mkdir -p ~/.local/share/gauss-agent/{agentd,logs,runtime}
cp -r ~/gauss-agent/agentd/gauss_agentd ~/.local/share/gauss-agent/agentd/
cp ~/gauss-agent/scripts/gauss-agentd.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable gauss-agentd
systemctl --user start gauss-agentd
`

### 1.5 Systemd User Service Unit

`ini
[Unit]
Description=GAUSS Agent Control Center Daemon
After=network.target

[Service]
Type=simple
ExecStart=/data7/hanzaidao/miniconda3/envs/GAUSS-SSC/bin/python -m gauss_agentd.main
WorkingDirectory=%h/.local/share/gauss-agent
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=GAUSS_AGENT_HOME=%h/.local/share/gauss-agent

[Install]
WantedBy=default.target
`

## 2. Repository Structure

`
D:/tpc/Codex_Cline/          (Windows dev)
  extension/                  VS Code extension source (TypeScript)
  agentd/                     Python daemon source
  protocol/                   IPC protocol definitions
  docs/                       Design docs
  scripts/                    Install/deploy scripts
  packaging/                  VSIX packaging config

~/.local/share/gauss-agent/  (Linux deploy)
  agentd/                     Installed daemon
  state.sqlite                Task state DB
  gauss-agent.sock            Unix Domain Socket
  logs/                       All runtime logs
  runtime/                    PID files, temp data
  config.yaml                 Daemon config
`

---

## 3. System Architecture

### 3.1 Two Components

Component 1: VS Code Extension (TypeScript, runs on Remote Extension Host)
- Sidebar UI: status panels, operation buttons, notifications, diff viewer
- Stateless: on reconnect, pulls full state from daemon via IPC
- Must declare extensionKind: [workspace] in package.json
- Closing extension / VS Code / SSH does NOT stop tasks or training

Component 2: gauss-agentd daemon (Python)
- Always-running background process
- Linux: systemd user service; Windows: console/Task Scheduler/background process
- Manages task queue, Cline CLI, Codex CLI, training, GPU, monitoring
- Does all mechanical checks: PID, exit code, log polling, GPU detection
- Communicates with extension via IPC (Unix Socket on Linux, Named Pipe/TCP on Windows)

---

## 4. Module Division

### 4.1 Directory Structure

```
D:/tpc/Codex_Cline/          (Windows dev source)
  extension/                  VS Code extension (TypeScript)
    src/
      extension.ts            Activation entry
      client.ts               IPC client (abstracts transport)
      panels/                 Webview panels
      commands.ts             VS Code command handlers
      notifications.ts        Toast + status bar notifications
    package.json              (extensionKind: [workspace])
  agentd/                     Python daemon
    gauss_agentd/
      __init__.py
      main.py                 Entry point
      config.py               Config + Cline path discovery
      db.py                   SQLite ORM + migrations
      server.py               IPC server (Unix Socket / TCP)
      task_manager.py          Task queue + state machine
      cline_executor.py       Async Cline CLI wrapper
      codex_executor.py       Codex CLI wrapper
      process_watcher.py      Cross-platform process monitor
      safety_guard.py         Command + path safety checker
      budget_tracker.py       Call counting + limits
      recovery.py             Restart state reconciliation
    tests/
    pyproject.toml
  protocol/                   IPC JSON-Lines schema
  docs/                       Design docs
  scripts/                    Install/deploy scripts
  packaging/                  Build configs
```

### 4.2 Module Boundaries

| Module | CAN do | CANNOT do |
|---|---|---|
| extension | Render UI, send commands, notifications, log tail | No business state, no process ops, no polling |
| config.py | Load config, auto-discover Cline path | No hardcoding paths |
| server.py | IPC accept, message route, push events | No business logic |
| task_manager | State machine, queue, event dispatch | No direct CLI calls |
| cline_executor | Spawn Cline async, wait exit, collect result | No log polling, no wait for training |
| codex_executor | Review package gen, call Codex, parse output | No full repo scan |
| process_watcher | PID check, exit code, output freshness, cross-platform | No semantic judgment |
| safety_guard | Command allow/block, path check, cwd, symlink, redirect | No semantic review (Codex domain) |
| budget_tracker | Count calls, threshold, block overuse | No business policy |
| recovery | Scan SQLite + process state, fix inconsistencies | No model calls, no reasoning |
| db.py | CRUD, migrations, atomic writes | No business logic |

---

## 5. State Machine

### 5.1 Phase 1 States (8 states)

Phase 1 covers Cline execution lifecycle only. Training/Codex/experiment states deferred to Phase 2-5.

| # | State | Meaning |
|---|---|---|
| 1 | QUEUED | Task waiting in queue |
| 2 | CLINE_STARTING | Cline CLI subprocess spawning |
| 3 | CLINE_RUNNING | Cline actively executing |
| 4 | CLINE_SUCCEEDED | Cline exited 0, result file valid (terminal) |
| 5 | CLINE_FAILED | Cline crashed: non-zero exit, no result (terminal after retries) |
| 6 | CLINE_STALLED | Cline alive but no output > threshold (terminal after retries) |
| 7 | WAITING_APPROVAL | High-risk action needs user approval |
| 8 | CANCELLED | User cancelled (terminal) |

### 5.2 Phase 1 Transition Rules

All transitions logged to `state_transitions` with: task_id, old_state, new_state, timestamp, trigger, pid, model_called, retry_count.

| From | Trigger | To | Model? |
|---|---|---|---|
| QUEUED | Daemon picks up | CLINE_STARTING | No |
| CLINE_STARTING | Subprocess spawned ok | CLINE_RUNNING | No |
| CLINE_STARTING | Spawn failed | CLINE_FAILED | No |
| CLINE_RUNNING | exit=0 + result file valid | CLINE_SUCCEEDED | No |
| CLINE_RUNNING | Process vanished, no marker | CLINE_FAILED | No |
| CLINE_RUNNING | No output > stall_threshold | CLINE_STALLED | No |
| CLINE_FAILED | retry_count < 1 | CLINE_STARTING | No |
| CLINE_STALLED | retry_count < 1 (kill first) | CLINE_STARTING | No |
| CLINE_FAILED | retry_count >= 1 | (terminal) | No |
| CLINE_STALLED | retry_count >= 1 | (terminal) | No |
| CLINE_SUCCEEDED | (terminal) | --- | No |
| ANY | User pauses | WAITING_APPROVAL | No |
| ANY | User cancels | CANCELLED | No |

### 5.3 Full System States (Phase 2-5, deferred)

TRAINING_QUEUED, TRAINING_RUNNING, TRAINING_COMPLETED, TRAINING_FAILED,
TESTING, TEST_PASSED, TEST_FAILED, RESULT_ANALYZING,
READY_FOR_CODEX_REVIEW, CODEX_REVIEWING, CODEX_APPROVED, CODEX_REVISE, CODEX_REJECTED,
PLANNING, COMPLETED, PAUSED

These 15 additional states bring the total to 23. Deferred to Phase 2-5.

---

## 6. Data Tables (SQLite)

9 tables total. Phase 1 implements: tasks, state_transitions, task_locks, kv_store.

### 6.1 Table: tasks
| Column | Type | Desc |
|---|---|---|
| id | TEXT PK | uuid or exp-YYYYMMDD-NNN |
| task_type | TEXT | cline_exec, codex_review, ... |
| priority | INTEGER | Higher = more urgent |
| state | TEXT | From state machine |
| cline_exe_path | TEXT | Resolved absolute Cline CLI path |
| cline_pid | INTEGER | Cline process PID |
| cline_exit_code | INTEGER | Cline exit code |
| cline_cmd_hash | TEXT | SHA256 of full command |
| cline_start_time | REAL | psutil create_time / proc starttime |
| cline_cwd | TEXT | Working directory |
| cline_ppid | INTEGER | Parent PID |
| run_id | TEXT | GAUSS_AGENT_RUN_ID |
| retry_count | INTEGER | Auto-retry counter |
| max_retries | INTEGER | Default 1 (i.e., 2 total attempts) |
| created_at | TEXT | ISO timestamp |
| started_at | TEXT | ISO timestamp |
| completed_at | TEXT | ISO timestamp |
| error_summary | TEXT | If failed/stalled |
| review_required | INTEGER | 1 if needs user approval |

### 6.2 Table: state_transitions
Audit log. Columns: id, task_id, old_state, new_state, timestamp, trigger, pid, log_path, model_called, retry_count.

### 6.3 Table: task_locks
Prevent concurrent conflicts. Columns: lock_name, holder_task_id, acquired_at, expires_at.

### 6.4 Remaining tables (Phase 2-5)
experiments, codex_reviews, error_events, gpu_snapshots, budget_log, kv_store
(Already designed in v0.1. Details preserved, implement later.)

---

## 7. Async Cline Execution

Cline executor MUST NOT block the daemon main event loop.

```
agentd main loop (asyncio)
  |
  +-- receive task from queue
  |
  +-- cline_executor.spawn(task)
  |     |
  |     +-- save PID, PGID, start_time, cmd_hash, run_id to SQLite
  |     +-- inject GAUSS_AGENT_TASK_ID, GAUSS_AGENT_RUN_ID env vars
  |     +-- redirect stdout/stderr to log files (NOT in-memory)
  |     +-- create asyncio subprocess (non-blocking)
  |     +-- return immediately, register callback for exit
  |
  +-- main loop continues:
  |     +-- handle IPC requests from extension
  |     +-- process_watcher polls periodically
  |     +-- handle cancel/stop/timer events
  |
  +-- on Cline exit:
        +-- capture exit code
        +-- validate result file
        +-- classify: SUCCEEDED / FAILED / STALLED
        +-- update SQLite state
        +-- push notification to extension if connected
```

Stall detection: separate timer, fires if stdout/stderr mtime > stall_threshold
(default 300s for Phase 1). Checks file mtime, not inotify polling on output content.

---

## 8. Safety Guard

Beyond command blacklist/whitelist. Multi-layer check before any Cline execution:

### 8.1 Pre-execution Checks

| Check | Description |
|---|---|
| CWD | Must be within allowed project directories |
| Path allowlist | File reads/writes must stay within project |
| Path blocklist | Cannot access protected paths (data/, checkpoints/stable/, conda env) |
| Symlink resolution | Resolve all symlinks; reject if target outside allowlist |
| Redirect targets | Check shell redirect destinations (>, >>) |
| Env var expansion | Check expanded paths in $VAR, %VAR% |
| Shell arguments | Reject risky shell metacharacters (*, ?, [] in dangerous contexts) |
| Git operations | Block git reset --hard, git clean -fd, git push --force |
| Branch protection | Block modifications to main/master branch |
| Experiment overwrite | Block overwriting existing experiment directories |
| External paths | Block access outside workspace |
| Process killing | Block terminating non-owned processes |
| Timeout | Enforce max execution time |
| Network access | Flag commands requiring network (allow if whitelisted) |

### 8.2 Risk Levels

LOW: All checks pass -> execute immediately
MEDIUM: Path near boundary -> log and execute
HIGH: Any check fails -> WAITING_APPROVAL state, user must approve
CRITICAL: Protected path, destructive command -> blocked, notification sent

### 8.3 Implementation

```python
# safety_guard.py
def check_command(cmd: str, cwd: str, env: dict) -> SafetyResult:
    checks = [
        check_cwd_within_project(cwd),
        check_no_protected_paths(cmd),
        check_symlink_escape(cmd, cwd),
        check_no_destructive_commands(cmd),
        check_no_branch_modification(cmd),
        check_redirect_targets(cmd, cwd),
        check_env_expansion(cmd, env),
        check_timeout(cmd),
    ]
    return aggregate(checks)
```

---

## 9. Recovery Algorithm (Restart)

On daemon restart (after crash, reboot, or intentional restart):

```
recovery.run():
  1. Read all tasks from SQLite where state in [CLINE_STARTING, CLINE_RUNNING]
  2. For each task:
     a. Check if PID exists in OS process table
     b. If PID exists:
        - Read /proc/<pid>/stat start_time (Linux) or psutil.create_time (Windows)
        - Compare with stored cline_start_time
        - Compare cmd_hash with current process command line
        - Compare cwd
        - Check GAUSS_AGENT_TASK_ID in process environment
        - If ALL match: process is ours, resume monitoring
        - If ANY mismatch: PID was reused by OS, mark task as CLINE_FAILED
     c. If PID does not exist:
        - Check result file exists and valid -> CLINE_SUCCEEDED
        - Check log file mtime -> estimate termination time
        - No result file -> CLINE_FAILED (mark as 'daemon restart, Cline lost')
  3. For tasks in CLINE_FAILED with retry_count < max_retries:
     - Auto-retry: transition to CLINE_STARTING, increment retry_count
  4. Push summary of recovered states to extension (if connected)
  5. Resume normal event loop
```

Key invariants:
- Never auto-kill a process whose ownership cannot be verified
- Never assume PID belongs to us just because it exists
- Store enough forensic data at spawn time to verify ownership later

---

## 10. IPC Protocol (Phase 1)

### 10.1 Transport
| Platform | Transport | Default Address |
|---|---|---|
| Windows dev | TCP (localhost only) | 127.0.0.1:19876 |
| Linux deploy | Unix Domain Socket | $XDG_RUNTIME_DIR/gauss-agent.sock |

### 10.2 Message Format (JSON-Lines)

Request:  {type:request, id:req-001, method:submit_task, params:{...}}
Response: {type:response, id:req-001, result:{...}}
Push:     {type:push, event:state_changed, data:{task_id, old, new}}

### 10.3 Phase 1 Methods

| Method | Params | Description |
|---|---|---|
| submit_task | {task_type, prompt, cwd?} | Create and queue a new task |
| get_status | {} | Full daemon + all tasks status |
| get_task | {task_id} | Single task detail |
| cancel_task | {task_id} | Cancel a running/queued task |
| get_log_tail | {task_id, lines} | Read last N lines of Cline log |

### 10.4 Phase 1 Push Events

state_changed, cline_succeeded, cline_failed, cline_stalled, approval_needed, agentd_error

---

## 11. Phase 1: Scope and Implementation Order

### 11.1 Acceptance Criteria (14 items)

| # | Criterion |
|---|---|
| AC1 | Extension submits task to daemon via IPC |
| AC2 | Daemon writes task to SQLite (tasks + state_transitions tables) |
| AC3 | Daemon resolves Cline CLI path via auto-discovery (configurable) |
| AC4 | Daemon spawns Cline asynchronously (non-blocking main loop) |
| AC5 | Cline stdout/stderr redirected to log files, not in-memory |
| AC6 | GAUSS_AGENT_TASK_ID and GAUSS_AGENT_RUN_ID injected as env vars |
| AC7 | Extension displays current task state (poll or push) |
| AC8 | Daemon correctly classifies Cline exit: SUCCEEDED (exit 0 + result valid) |
| AC9 | Daemon correctly classifies Cline exit: FAILED (process gone, no result) |
| AC10 | Daemon detects stall (stdout/stderr mtime > 300s) |
| AC11 | FAILED/STALLED auto-retries exactly once (max 1 retry = 2 total attempts) |
| AC12 | Second failure -> terminal state, no further retries |
| AC13 | Daemon restart: recovers state from SQLite + process table, no duplicate execution |
| AC14 | Zero model calls during any status check or state transition |

### 11.2 Explicitly OUT OF SCOPE for Phase 1

- Training process management (TRAINING_* states)
- GPU monitoring or allocation
- Codex CLI calls or review packages
- Error classification engine
- Budget tracking
- Experiment management
- Auto/semi-auto modes (manual only)
- Log panel, GPU panel, experiment panel in extension
- Multi-task concurrency (single task at a time)

### 11.3 Implementation Order (13 steps)

| Step | Task | Key File(s) |
|---|---|---|
| 1 | Python project scaffold (pyproject.toml, __init__.py, package structure) | agentd/pyproject.toml |
| 2 | Config model + directory conventions + Cline path auto-discovery | config.py |
| 3 | SQLite schema creation + migration mechanism | db.py |
| 4 | Task repository (CRUD for tasks table) + transactions | db.py, task_manager.py |
| 5 | State transition validator (enforce valid transitions) | task_manager.py |
| 6 | Async Cline subprocess executor (spawn, env injection, log redirect) | cline_executor.py |
| 7 | Process identity capture (PID, start_time, cmd_hash, cwd, ppid) | process_watcher.py |
| 8 | Stall detection (log mtime check timer) | process_watcher.py |
| 9 | IPC server: TCP for Windows / Unix Socket skeleton for Linux | server.py |
| 10 | VS Code minimal extension client (submit task, show status) | extension/ |
| 11 | Manual test: end-to-end submit -> spawn -> exit -> classify | tests/ |
| 12 | Daemon restart recovery (SQLite + process table reconciliation) | recovery.py |
| 13 | Integration tests + fault injection (kill -9 Cline, rename log file, PID reuse test) | tests/ |

### 11.4 Config Structure (config.yaml)

```yaml
agentd:
  ipc:
    transport: tcp           # tcp (Windows) or unix (Linux)
    tcp_host: 127.0.0.1
    tcp_port: 19876
    unix_socket: $XDG_RUNTIME_DIR/gauss-agent.sock
  cline:
    executable: auto         # auto | /path/to/cline
    timeout_seconds: 600
    stall_threshold_seconds: 300
    max_retries: 1
    env:
      HTTP_PROXY: http://127.0.0.1:17898
      HTTPS_PROXY: http://127.0.0.1:17898
  database:
    path: .agent-control/state.sqlite
  logs:
    dir: .agent-control/logs/
  safety:
    protected_paths: []
    forbidden_commands: []
```

---

## 12. Phase 1: Test Matrix

| # | Test Case | Expected Result |
|---|---|---|
| T1 | Submit valid task via IPC | Task created in SQLite, state=QUEUED |
| T2 | Task moves QUEUED -> CLINE_STARTING -> CLINE_RUNNING | Correct transition sequence logged |
| T3 | Cline exits 0, result file exists and valid | State -> CLINE_SUCCEEDED |
| T4 | Cline exits non-zero, no result file | State -> CLINE_FAILED, retry_count=0 |
| T5 | CLINE_FAILED with retry_count=0 | Auto-retry: -> CLINE_STARTING, retry_count=1 |
| T6 | CLINE_FAILED with retry_count=1 | Terminal, no further retry |
| T7 | Cline running, no stdout/stderr update for 300s | State -> CLINE_STALLED |
| T8 | CLINE_STALLED with retry_count=0 | Kill Cline, auto-retry -> CLINE_STARTING |
| T9 | CLINE_STALLED with retry_count=1 | Terminal, no further action |
| T10 | kill -9 on Cline process | Detected as FAILED (process vanished) |
| T11 | Daemon restart while Cline running | Recovery re-attaches or marks FAILED |
| T12 | Daemon restart, PID reused by another process | Mismatch detected, marks FAILED (no kill) |
| T13 | Extension disconnects, Cline completes | On reconnect, extension gets full state via get_status |
| T14 | Cline path not found | Clear error: 'Cline CLI not found. Install: npm install -g cline' |

### 12.1 Mock Mode

If Cline API key is not configured, mock mode must be supported:
- cline_executor uses a mock subprocess that simulates success/failure/stall
- Controlled by config: `cline.mock: true`
- Mock can be configured to produce specific exit codes and output timing
- All state transitions and IPC work identically in mock mode

---

## 13. Risk Checklist (Updated)

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | Cline CLI behavior unstable across versions | HIGH | Version check on start; timeout + parse fallback; mock mode for dev |
| R2 | PID reuse after daemon restart causes mistaken ownership | HIGH | Multi-field identity: PID+start_time+cmd_hash+cwd+env var. Mismatch -> no kill |
| R3 | Cline executor blocks main loop (sync subprocess.run) | HIGH | MUST use asyncio.subprocess; validated in code review before merge |
| R4 | Windows path escaping (spaces, Chinese chars, special chars) | MEDIUM | Use subprocess list arg form, not shell strings; test with Chinese prompts |
| R5 | PowerShell vs cmd subprocess behavior differences | MEDIUM | Explicitly use cmd.exe /c or direct executable; configurable shell type |
| R6 | IPC port conflict on Windows | LOW | Configurable port; check port availability on start |
| R7 | SQLite concurrent access (WAL mode required) | LOW | Enable WAL mode; single writer pattern via db.py |
| R8 | Extension disconnects silently, daemon pushes to dead socket | LOW | Track connection state; buffer or drop pushes; full refresh on reconnect |
| R9 | Cline API key not configured -> cannot test | MEDIUM | Mock mode works without API key; real API key never in repo |
| R10 | Log file growth unbounded | LOW | Rotate after N MB; auto-clean logs older than 7 days |

---

## Appendix A: Environment Verification (2026-07-20)

| Item | Value |
|---|---|
| OS | Windows 10 AMD64 |
| CWD | D:/tpc/Codex_Cline |
| Conda | base, D:/anaconda |
| Python | 3.11.5 (D:/anaconda/python.exe) |
| Node.js | v24.17.0 |
| npm | 11.13.0 |
| Cline CLI | v3.0.46 |
| Cline path | C:/Users/Administrator/AppData/Roaming/npm/cline.cmd |
| Cline doctor | OK (hub not running, expected) |
| Read-only test | PASSED (listed dir, read DESIGN.md, no modifications) |

## Appendix B: Cline Invocation Template (Windows)

```powershell
# cline_executor constructs:
$env:GAUSS_AGENT_TASK_ID = '550e8400-e29b-41d4-a716-446655440000'
$env:GAUSS_AGENT_RUN_ID = 'a1b2c3d4'
$env:HTTP_PROXY = 'http://127.0.0.1:17898'
$env:HTTPS_PROXY = 'http://127.0.0.1:17898'
cline -p -c D:/tpc/Codex_Cline --timeout 600 --auto-approve true < task_prompt.txt > cline_stdout.log 2> cline_stderr.log
```

---

> **Status**: Design revised. All 8 structural corrections applied.
> **Next**: Begin Phase 1 Step 1: Python project scaffold.
