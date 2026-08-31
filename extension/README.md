# Agent Hub for VS Code

**Monitor goals and long-running coding-agent tasks from a VS Code sidebar while a local daemon handles persistence, stall detection, recovery, and process lifecycle.**

This extension is the UI layer of the [Agent Hub](https://github.com/dzrdzrdzr/Agent-Hub) project. It bundles the Python daemon source and can start it automatically for the current local or Remote SSH extension host.

## Install from source

From the repository:

```bash
cd extension
npm ci
npm run package
```

Install the generated VSIX through **Extensions: Install from VSIX...**. For Remote SSH, install it on the remote extension host.

## Main workflow

1. Open the **Agent Hub** activity-bar view.
2. Use **Agent Hub: Start Goal** for a planner/executor loop, or **Delegate Task to Cline** for one bounded task.
3. Watch state and live output in the sidebar.
4. Cancel stalled or unwanted work without losing the task record.
5. Inspect daemon logs when process discovery or recovery fails.

## Commands

| Command | Purpose |
| --- | --- |
| `Agent Hub: Start Goal` | Start a multi-step objective |
| `Agent Hub: Delegate Task to Cline` | Submit one executor prompt |
| `Agent Hub: Watch Task Output` | Open live task output |
| `Agent Hub: Cancel Running Task` | Stop a task process group |
| `Agent Hub: Start Daemon` | Start the bundled or configured daemon |
| `Agent Hub: Show Daemon Log` | Open daemon diagnostics |

## Settings

| Setting | Default | Description |
| --- | --- | --- |
| `agentHub.pythonPath` | auto | Python interpreter with `pyyaml` and `psutil` |
| `agentHub.daemonRoot` | bundled | Optional external Agent Hub root |
| `agentHub.port` | `19876` | Local IPC port |
| `agentHub.autoStartDaemon` | `true` | Start the daemon when the extension host starts |

## Extension API

```ts
const extension = vscode.extensions.getExtension("agenthub.agent-hub");
const api = extension?.isActive ? extension.exports : await extension?.activate();

const taskId = await api.submitTask("run tests and fix the first failure");
const goal = await api.startGoal("make the current project pass its tests", {
  cwd: workspaceRoot,
});

api.onDidChangeState(({ task_id, state }) => {
  console.log(`${task_id} -> ${state}`);
});
```

The daemon and protocol documentation live in the repository-level [README](https://github.com/dzrdzrdzr/Agent-Hub#readme), [AGENTS.md](https://github.com/dzrdzrdzr/Agent-Hub/blob/main/AGENTS.md), and [DESIGN.md](https://github.com/dzrdzrdzr/Agent-Hub/blob/main/DESIGN.md).
