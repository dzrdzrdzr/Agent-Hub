# Agent Hub

**Planner ↔ Executor bridge for AI coding agents.**

Agent Hub separates *planning* from *execution*: a "planner" agent (Codex, KimiCode)
analyzes code, writes precise prompts, and reviews results — while the "executor"
(Cline CLI, backed by DeepSeek) does the heavy file edits, test runs, and builds.

A small local Python daemon (`agentd/`) manages the Cline subprocess lifecycle
(spawn, stall detection, retry, process-group cleanup); this extension provides
the sidebar UI and a public API for other extensions to submit tasks.

---

## When to use Agent Hub

| Situation | Use Agent Hub? |
|-----------|---------------|
| Multi-file refactor (3+ files) | ✅ Yes — write a detailed prompt, let Cline execute |
| Creating new modules from scratch | ✅ Yes — great for scaffolding and boilerplate |
| Running test suites | ✅ Yes — tests run async, you keep working |
| Long builds or training scripts | ✅ Yes — daemon manages the process lifecycle |
| Applying review feedback to code | ✅ Yes — send the changes as a prompt |
| Single-line fix (one import, typo) | ❌ No — faster to do it yourself |
| Reading/searching code | ❌ No — grep/read is instant |
| Quick `ls`/`git status`/`pip install` | ❌ No — just run the command |

**The pattern:** *you plan, Cline executes.* Your prompt should include file paths,
function names, expected behavior, and validation steps.

---

## What you get

- **Activity-bar icon "Agent Hub"** with a task tree: each task shows prompt text,
  submission time, state (RUNNING / SUCCEEDED / FAILED / STALLED), exit code, retry count.
  Newest tasks first.
- **Click a task** → live-updating conversation view. ANSI codes are stripped for
  readability. Auto-refreshes every 2s while running.
- **Right-click** → cancel, open raw log, or copy task ID.
- **Status-bar item** showing daemon state. Click to start the daemon or open the sidebar.
- **Commands** (Ctrl+Shift+P):
  - `Agent Hub: Delegate Task to Cline` — submit a prompt
  - `Agent Hub: Cancel Running Task` — kill the Cline process tree
  - `Agent Hub: Watch Task Output` — open live session view
  - `Agent Hub: Open Raw Output Log` — open the stdout log file
  - `Agent Hub: Refresh Task List` — resync from daemon
  - `Agent Hub: Start Daemon` — manually start agentd
  - `Agent Hub: Show Daemon Log` — view daemon diagnostics

---

## Example workflow

```
1. You (planner) analyze: read 5 source files, understand the bug
2. You write a prompt:
   "In agentd/agent_hub/server.py, extract the log-reading logic from
    _h_get_log_tail into a standalone _read_tail_lines() function.
    Add unit test in tests/test_server.py. Run pytest to verify."
3. Ctrl+Shift+P → "Agent Hub: Delegate Task to Cline" → paste prompt
4. Watch the sidebar — task goes QUEUED → STARTING → RUNNING
5. Click the task to see live output as Cline edits files and runs tests
6. Task completes → SUCCEEDED or FAILED. Review the output, iterate if needed.
```

---

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `agentHub.pythonPath` | `""` (auto) | Python interpreter for the daemon (needs `pyyaml`+`psutil`) |
| `agentHub.port` | `19876` | TCP port, must match `config.yaml` |
| `agentHub.autoStartDaemon` | `true` | Start daemon on window open |

---

## Calling Agent Hub from another extension (Codex, KimiCode, ...)

### Via command (no dependency needed)
```ts
await vscode.commands.executeCommand('agent-hub.submitTask', {
    prompt: 'refactor the parser module',
    cwd: workspaceRoot, // optional
});
```

### Via the typed API (with events)
```ts
const ext = vscode.extensions.getExtension('agenthub.agent-hub');
const api = ext?.isActive ? ext.exports : await ext?.activate();
const taskId = await api.submitTask('write unit tests for db.py');
api.onDidChangeState(({ task_id, state }) => {
    console.log(`Task ${task_id} → ${state}`);
});
```

API surface: `submitTask(prompt, {cwd?}) → taskId`, `cancelTask(taskId)`,
`getStatus()`, `onDidChangeState`, `version` (0.2.3).

---

## Calling Agent Hub from a CLI agent (no VS Code APIs)

Use `scripts/agent-hub.py` (stdlib-only) or raw JSON-Lines over TCP `127.0.0.1:19876`.
See the workspace `AGENTS.md` for protocol details and the full decision tree on
when to delegate vs. do it yourself.
