import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as net from 'net';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

const VIEW_ID = 'agent-hub.tasks';
const CONTAINER_ID = 'agent-hub';

export interface AgentHubApi {
    readonly version: string;
    /** Submit a task to the daemon. Returns the task id. */
    submitTask(prompt: string, options?: { cwd?: string }): Promise<string>;
    cancelTask(taskId: string): Promise<void>;
    startGoal(objective: string, options?: { max_iterations?: number; max_failures?: number; completion_criteria?: string; stop_conditions?: string; model_call_budget?: number; cwd?: string }): Promise<any>;
    cancelGoal(goalId: string): Promise<any>;
    deleteGoal(goalId: string): Promise<any>;
    getGoalTasks(goalId: string): Promise<any>;
    waitForEvent(goalId?: string, eventTypes?: string[], timeout?: number): Promise<any>;
    getStatus(): Promise<any>;
    /** Fired when the daemon reports a task state change. */
    onDidChangeState: vscode.Event<{ task_id: string; state: string }>;
    /** Fired when the daemon reports a goal state change. */
    onDidChangeGoalState: vscode.Event<{ goal_id: string; state: string; objective: string }>;
}

let output: vscode.OutputChannel;
let statusBar: vscode.StatusBarItem;
let client: DaemonClient;
let provider: TaskTreeProvider;
let goalsProvider: GoalTreeProvider;
let hubState: 'starting' | 'connected' | 'stopped' = 'stopped';
let lastError = '';
let activeCount = 0;
let cachedPython: string | null | undefined;
let bootPromise: Promise<void> | null = null;
let sessionPanel: vscode.WebviewPanel | null = null;
let sessionTaskId: string | null = null;
let sessionTimer: NodeJS.Timeout | null = null;
let extensionContext: vscode.ExtensionContext;

// ---- Debounce helpers ----
let _debounceRefreshTimer: NodeJS.Timeout | null = null;
const DEBOUNCE_MS = 500;

function debouncedRefresh(): void {
    if (_debounceRefreshTimer) clearTimeout(_debounceRefreshTimer);
    _debounceRefreshTimer = setTimeout(() => {
        _debounceRefreshTimer = null;
        provider.refresh();
    }, DEBOUNCE_MS);
}

function log(msg: string): void {
    output?.appendLine(`[${new Date().toLocaleTimeString()}] ${msg}`);
}

function port(): number {
    return vscode.workspace.getConfiguration('agentHub').get<number>('port', 19876);
}

function autoStart(): boolean {
    return vscode.workspace.getConfiguration('agentHub').get<boolean>('autoStartDaemon', true);
}

function workspaceRoot(): string | undefined {
    return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

interface DaemonLaunch {
    sourceRoot: string;
    runtimeRoot: string;
}

function isDaemonRoot(root: string): boolean {
    return fs.existsSync(path.join(root, 'agentd', 'agent_hub', 'main.py'))
        && fs.existsSync(path.join(root, 'config.yaml'));
}

function resolveDaemonLaunch(): DaemonLaunch {
    const configured = vscode.workspace.getConfiguration('agentHub')
        .get<string>('daemonRoot', '').trim();
    if (configured) {
        const sourceRoot = path.resolve(configured);
        if (!isDaemonRoot(sourceRoot)) {
            throw new Error(`agentHub.daemonRoot is invalid: ${sourceRoot}`);
        }
        return { sourceRoot, runtimeRoot: sourceRoot };
    }

    const workspace = workspaceRoot();
    if (workspace && isDaemonRoot(workspace)) {
        return { sourceRoot: workspace, runtimeRoot: workspace };
    }

    const bundled = path.join(extensionContext.extensionPath, 'daemon');
    if (!isDaemonRoot(bundled)) {
        throw new Error('Bundled Agent Hub daemon is missing; reinstall the VSIX or set agentHub.daemonRoot.');
    }
    const runtimeRoot = extensionContext.globalStorageUri.fsPath;
    fs.mkdirSync(runtimeRoot, { recursive: true });
    return { sourceRoot: bundled, runtimeRoot };
}

function isTerminal(state: string): boolean {
    return ['CLINE_SUCCEEDED', 'CLINE_FAILED', 'CLINE_STALLED', 'CANCELLED'].includes(state);
}

function isGoalTerminal(state: string): boolean {
    return ['GOAL_COMPLETED', 'GOAL_FAILED', 'GOAL_CANCELLED'].includes(state);
}

function fmtTime(iso?: string): string {
    if (!iso) return '??:??';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '??:??';
    const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    return d.toDateString() === new Date().toDateString() ? hm : `${d.getMonth() + 1}-${d.getDate()} ${hm}`;
}

function escapeHtml(s: string): string {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function stripAnsi(s: string): string {
    return s.replace(/\x1b\[[0-9;]*m/g, '');
}

class DaemonClient {
    private socket: net.Socket | null = null;
    private buffer = '';
    private reqId = 0;
    private pending = new Map<string, { resolve: (v: any) => void; reject: (e: any) => void; timer: NodeJS.Timeout }>();
    private reconnectDelay = 2000;
    private reconnectTimer: NodeJS.Timeout | null = null;
    private connecting: Promise<void> | null = null;
    private disposed = false;
    private _onDidChangeState = new vscode.EventEmitter<{ task_id: string; state: string }>();
    readonly onDidChangeState = this._onDidChangeState.event;
    private _onDidChangeGoalState = new vscode.EventEmitter<{ goal_id: string; state: string; objective: string }>();
    readonly onDidChangeGoalState = this._onDidChangeGoalState.event;
    private _onDidChangeConnection = new vscode.EventEmitter<boolean>();
    readonly onDidChangeConnection = this._onDidChangeConnection.event;

    constructor(private getPort: () => number) { }

    get connected(): boolean { return !!this.socket && !this.socket.destroyed; }

    async waitForEvent(goalId?: string, eventTypes?: string[], timeout?: number): Promise<any> {
        const reqTimeout = timeout ? timeout + 10000 : 3600000;
        return this.request('wait_for_event', {
            goal_id: goalId,
            event_types: eventTypes,
            timeout,
        }, reqTimeout);
    }

    async acknowledgeEvent(eventId: string): Promise<boolean> {
        const r = await this.request('acknowledge_event', { event_id: eventId }, 5000);
        return r?.acknowledged === true;
    }

    ensureConnected(): Promise<void> {
        if (this.connected) return Promise.resolve();
        if (this.connecting) return this.connecting;
        this.connecting = this.open()
            .catch((error) => {
                this.scheduleReconnect();
                throw error;
            })
            .finally(() => { this.connecting = null; });
        return this.connecting;
    }

    private scheduleReconnect(): void {
        if (this.disposed || this.connected || this.reconnectTimer) return;
        const delay = this.reconnectDelay;
        this.reconnectTimer = setTimeout(() => {
            this.reconnectTimer = null;
            this.ensureConnected().catch(() => { /* the failed attempt schedules the next one */ });
        }, delay);
        this.reconnectDelay = Math.min(this.reconnectDelay * 2, 30000);
    }

    private open(): Promise<void> {
        const p = this.getPort();
        return new Promise<void>((resolve, reject) => {
            const s = new net.Socket();
            let settled = false;
            const fail = (e: any) => {
                if (settled) return;
                settled = true;
                s.destroy();
                reject(e instanceof Error ? e : new Error(String(e)));
            };
            s.setTimeout(3000);
            s.once('timeout', () => fail(new Error(`connect to 127.0.0.1:${p} timed out`)));
            s.once('error', fail);
            s.connect(p, '127.0.0.1', () => {
                if (settled) { s.destroy(); return; }
                settled = true;
                s.setTimeout(0); // no inactivity timeout on the established socket
                this.attach(s);
                resolve();
            });
        });
    }

    private attach(s: net.Socket): void {
        if (this.socket) this.socket.destroy();
        if (this.reconnectTimer) {
            clearTimeout(this.reconnectTimer);
            this.reconnectTimer = null;
        }
        this.socket = s;
        this.buffer = '';
        this.reconnectDelay = 2000;
        s.on('data', (data: Buffer) => this.onData(data));
        s.on('error', (e) => log(`socket error: ${e.message}`));
        s.on('close', () => this.onClose());
        this._onDidChangeConnection.fire(true);
    }

    private onData(data: Buffer): void {
        this.buffer += data.toString('utf-8');
        const lines = this.buffer.split('\n');
        this.buffer = lines.pop() || '';
        for (const line of lines) {
            if (!line) continue;
            try {
                const msg = JSON.parse(line);
                if (msg.type === 'response' && msg.id !== undefined) {
                    const entry = this.pending.get(String(msg.id));
                    if (entry) {
                        clearTimeout(entry.timer);
                        this.pending.delete(String(msg.id));
                        if (msg.error) entry.reject(new Error(msg.error));
                        else entry.resolve(msg.result);
                    }
                } else if (msg.type === 'push') {
                    this.handlePush(msg.event, msg.data);
                }
            } catch { /* skip malformed lines */ }
        }
    }

    private onClose(): void {
        if (this.socket) { this.socket.destroy(); this.socket = null; }
        // Reject all pending
        for (const [, entry] of this.pending) {
            clearTimeout(entry.timer);
            entry.reject(new Error('disconnected'));
        }
        this.pending.clear();
        this._onDidChangeConnection.fire(false);
        this.scheduleReconnect();
    }

    private handlePush(event: string, data: any): void {
        if (event === 'state_changed') {
            this._onDidChangeState.fire(data);
        } else if (event === 'goal_state_changed') {
            this._onDidChangeGoalState.fire(data);
        } else if (event === 'goal_deleted') {
            this._onDidChangeGoalState.fire(data);
        }
    }

    request(method: string, params: any = {}, timeoutMs: number = 10000): Promise<any> {
        return this.ensureConnected().then(() => {
            const id = String(++this.reqId);
            return new Promise<any>((resolve, reject) => {
                const timer = setTimeout(() => {
                    this.pending.delete(id);
                    reject(new Error(`request ${method} timed out`));
                }, timeoutMs);
                this.pending.set(id, { resolve, reject, timer });
                const payload = JSON.stringify({ type: 'request', id, method, params }) + '\n';
                try {
                    this.socket!.write(payload);
                } catch (e: any) {
                    clearTimeout(timer);
                    this.pending.delete(id);
                    reject(e);
                }
            });
        });
    }

    dispose(): void {
        this.disposed = true;
        if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
        if (this.socket) { this.socket.destroy(); this.socket = null; }
        for (const [, entry] of this.pending) { clearTimeout(entry.timer); }
        this.pending.clear();
    }
}

class TaskTreeProvider implements vscode.TreeDataProvider<TaskItem> {
    private _onDidChangeTreeData = new vscode.EventEmitter<TaskItem | undefined>();
    readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

    refresh(): void { this._onDidChangeTreeData.fire(undefined); }

    getTreeItem(element: TaskItem): vscode.TreeItem {
        return element;
    }

    async getChildren(): Promise<TaskItem[]> {
        if (!client.connected) return [
                new TaskItem('Daemon not connected', 'Click the status bar icon or run "Agent Hub: Start Daemon"', 'disconnected'),
            ];
        try {
            const s = await client.request('get_status', { active_only: true, limit: 100 }, 5000);
            activeCount = s.active_count ?? s.tasks?.length ?? 0;
            const budget = s.budget || {};

            // ── System overview section ──
            const sysItems: TaskItem[] = [];

            // Daemon status
            const daemonLabel = hubState === 'connected' ? 'Daemon connected' : 'Daemon reconnecting';
            sysItems.push(new TaskItem(daemonLabel, `v0.3.0 | workspace: ${(workspaceRoot() || '').split('/').pop() || '?'}`, 'daemon'));

            // Budget
            const clineDaily = budget.cline?.daily ?? '?';
            const codexDaily = budget.codex?.daily ?? '?';
            const budgetDate = budget.cline?.date ?? '';
            sysItems.push(new TaskItem(
                `Budget today`,
                `cline: ${clineDaily} calls | codex: ${codexDaily} calls${budgetDate ? ' | ' + budgetDate : ''}`,
                'budget',
            ));

            // Active counts
            let activeGoalCount = 0;
            try {
                const gr = await client.request('list_goals', { include_terminal: false }, 3000);
                activeGoalCount = (gr?.goals || []).length;
            } catch { /* ignore */ }
            sysItems.push(new TaskItem(
                `Active: ${activeCount} tasks, ${activeGoalCount} goals`,
                lastError ? `last error: ${lastError}` : 'system healthy',
                lastError ? 'warning' : 'healthy',
            ));

            const tasks: any[] = s.tasks || [];
            if (tasks.length === 0) {
                sysItems.push(new TaskItem('No tasks yet', 'Ctrl+Shift+P → "Agent Hub: Delegate Task to Cline"', 'empty'));
                return sysItems;
            }
            const taskItems = tasks.map((t: any) => {
                const id = t.id || '';
                const state = t.state || 'UNKNOWN';
                const prompt = (t.prompt || '').slice(0, 60);
                const exitCode = t.cline_exit_code;
                const pid = t.cline_pid;
                const retry = `${t.retry_count || 0}/${t.max_retries || 0}`;
                const training = t.training_state || '';
                const goalId = t.goal_id;

                // Build rich label
                let label = `${id.slice(-12)}`;
                if (exitCode !== null && exitCode !== undefined) {
                    label += ` exit=${exitCode}`;
                }
                if (state === 'CLINE_RUNNING' && pid) {
                    label += ` pid=${pid}`;
                }

                // Build description line
                const parts: string[] = [state];
                if (training) parts.push(`train:${training}`);
                parts.push(`retry:${retry}`);
                const desc = parts.join(' | ');

                // Build tooltip
                const tt: string[] = [
                    `**Task**: ${id}`,
                    `**State**: ${state}`,
                    `**Prompt**: ${prompt}`,
                ];
                if (exitCode !== null && exitCode !== undefined) tt.push(`**Exit**: ${exitCode}`);
                if (pid) tt.push(`**PID**: ${pid}`);
                if (goalId) tt.push(`**Goal**: ${goalId.slice(-12)}`);
                if (training) tt.push(`**Training**: ${training}`);
                tt.push(`**Retries**: ${retry}`);

                const item = new TaskItem(label, desc, state, id, tt);
                item.exitCode = exitCode;
                item.pid = pid;
                item.trainingState = training;
                item.goalId = goalId;
                item.retryInfo = retry;
                return item;
            });
            return [...sysItems, ...taskItems];
        } catch {
            return [
                new TaskItem('Connection lost', 'Daemon may have stopped. Run "Agent Hub: Start Daemon"', 'error'),
            ];
        }
    }
}

class TaskItem extends vscode.TreeItem {
    trainingState?: string;
    exitCode?: number;
    goalId?: string;
    pid?: number;
    retryInfo?: string;
    constructor(
        public readonly label: string,
        public readonly description: string,
        public readonly state: string,
        public readonly taskId?: string,
        public readonly tooltipLines: string[] = [],
    ) {
        super(label, vscode.TreeItemCollapsibleState.None);
        this.tooltip = new vscode.MarkdownString(tooltipLines.join('  \n'));
        this.contextValue = taskId ? 'task' : 'status';
        // Icon mapping
        switch (state) {
            // System status items
            case 'daemon': this.iconPath = new vscode.ThemeIcon('vm-active'); break;
            case 'budget': this.iconPath = new vscode.ThemeIcon('graph'); break;
            case 'healthy': this.iconPath = new vscode.ThemeIcon('heart'); break;
            // Task states
            case 'CLINE_RUNNING': this.iconPath = new vscode.ThemeIcon('sync~spin'); break;
            case 'CLINE_SUCCEEDED': this.iconPath = new vscode.ThemeIcon('pass'); break;
            case 'CLINE_FAILED': this.iconPath = new vscode.ThemeIcon('error'); break;
            case 'CLINE_STALLED': this.iconPath = new vscode.ThemeIcon('warning'); break;
            case 'CANCELLED': this.iconPath = new vscode.ThemeIcon('circle-slash'); break;
            case 'WAITING_APPROVAL': this.iconPath = new vscode.ThemeIcon('key'); break;
            case 'TRAINING_RUNNING': this.iconPath = new vscode.ThemeIcon('beaker'); break;
            case 'TRAINING_SUCCEEDED': this.iconPath = new vscode.ThemeIcon('beaker'); break;
            case 'TRAINING_FAILED': this.iconPath = new vscode.ThemeIcon('warning'); break;
            default: this.iconPath = new vscode.ThemeIcon('circle-outline');
        }
        if (taskId) {
            this.command = { command: 'agent-hub.openTask', title: 'Open', arguments: [taskId] };
        }
    }
}

function setStatus(state: 'starting' | 'connected' | 'stopped'): void {
    hubState = state;
    if (state === 'connected') {
        statusBar.text = `$(check) Agent Hub`;
        statusBar.tooltip = 'Agent Hub daemon connected';
    } else if (state === 'starting') {
        statusBar.text = `$(sync~spin) Agent Hub starting...`;
        statusBar.tooltip = 'Agent Hub daemon starting';
    } else {
        statusBar.text = `$(circle-slash) Agent Hub`;
        statusBar.tooltip = lastError || 'Agent Hub daemon stopped';
    }
}

async function findPython(): Promise<string> {
    if (cachedPython) return cachedPython;
    const cfg = vscode.workspace.getConfiguration('agentHub').get<string>('pythonPath', '');
    // Configured pythonPath takes priority — validate it with real imports
    if (cfg && cfg.trim()) {
        const p = cfg.trim();
        try {
            const test = cp.spawnSync(p, ['-c', 'import yaml; import psutil; print("ok")'], { timeout: 10000 });
            if (test.status === 0 && test.stdout.toString().trim() === 'ok') {
                cachedPython = p;
                log(`findPython: configured pythonPath OK: ${p}`);
                return p;
            }
            log(`findPython: configured pythonPath ${p} failed yaml/psutil check: ${test.stderr?.toString() || test.error?.message || 'unknown'}`);
        } catch (e: any) { log(`findPython: configured pythonPath ${p} spawn error: ${e?.message || e}`); }
        // Fall through to auto-detect
    }
    // Try common paths with import validation
    for (const p of ['python3', 'python', '/usr/bin/python3', '/usr/bin/python']) {
        try {
            const r = cp.spawnSync(p, ['-c', 'import sys, yaml, psutil; print(sys.executable)'], { timeout: 5000 });
            if (r.status === 0 && r.stdout) {
                cachedPython = r.stdout.toString().trim();
                log(`findPython: auto-detected ${cachedPython}`);
                return cachedPython;
            }
        } catch { /* continue */ }
    }
    throw new Error('No Python with yaml and psutil found. Set agentHub.pythonPath to a compatible interpreter.');
}

async function ensureDaemon(forceStart: boolean = false): Promise<void> {
    if (client.connected) return;
    if (bootPromise) return bootPromise;

    if (!forceStart && !autoStart()) {
        throw new Error('Daemon not running and autoStartDaemon is off');
    }

    bootPromise = (async () => {
        try {
            await client.ensureConnected();
            // Verify workspace matches
            const ping: any = await client.request('ping', {}, 3000).catch(() => null);
            if (ping && ping.workspace) {
                const root = workspaceRoot();
                if (root && path.normalize(ping.workspace) !== path.normalize(root)) {
                    log(`Workspace mismatch: daemon at ${ping.workspace}, extension at ${root}`);
                    // Non-fatal warning — the user may have intentionally connected
                    // to a different workspace's daemon
                }
            }
            bootPromise = null;
            return;
        } catch {
            // Daemon not running — try to start it
        }

        const launch = resolveDaemonLaunch();
        const cwd = launch.runtimeRoot;

        const python = await findPython();
        setStatus('starting');
        log(`Starting daemon with ${python}...`);

        const configPath = path.join(launch.sourceRoot, 'config.yaml');
        const env: any = { ...process.env };
        env.PYTHONPATH = path.join(launch.sourceRoot, 'agentd');
        env.AGENT_HUB_CONFIG = configPath;

        let child: cp.ChildProcess;
        try {
            child = cp.spawn(python, ['-B', '-u', '-m', 'agent_hub.main'], {
                cwd,
                env,
                stdio: 'ignore',
                detached: true,
            });
        } catch (e: any) {
            throw new Error(`Unable to spawn daemon with ${python}: ${e?.message || e}`);
        }
        child.once('error', (e) => log(`Daemon process error: ${e.message}`));
        child.unref();

        // Wait for daemon to come up (exponential backoff, max 15s)
        for (let i = 0; i < 7; i++) {
            await new Promise(r => setTimeout(r, Math.min(500 * Math.pow(2, i), 3000)));
            try {
                await client.ensureConnected();
                bootPromise = null;
                setStatus('connected');
                return;
            } catch { /* retry */ }
        }
        bootPromise = null;
        throw new Error('Daemon failed to start within timeout');
    })();

    try {
        await bootPromise;
    } catch (e: any) {
        bootPromise = null;
        lastError = e?.message || String(e);
        log(`Daemon startup failed: ${lastError}`);
        setStatus('stopped');
        throw e;
    }
}

async function renderSession(): Promise<void> {
    if (!sessionPanel || !sessionTaskId) return;
    try {
        const t = await client.request('get_task', { task_id: sessionTaskId }, 3000);
        const tail = await client.request('get_log_tail', { task_id: sessionTaskId, lines: 200 }, 5000);
        const logText = stripAnsi(tail?.stdout || '');
        const state = t?.state || 'UNKNOWN';
        const prompt = escapeHtml((t?.prompt || '').slice(0, 200));
        sessionPanel.webview.html = `<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
body{font-family:var(--vscode-editor-font-family,monospace);font-size:var(--vscode-editor-font-size,13px);color:var(--vscode-editor-foreground);background:var(--vscode-editor-background);padding:12px;margin:0;}
.header{padding:8px;background:var(--vscode-sideBar-background,#252526);border-radius:4px;margin-bottom:12px;}
.header .state{padding:2px 8px;border-radius:3px;font-size:11px;font-weight:bold;}
.state-running{background:#1a7f3733;color:#3fb950;}
.state-failed{background:#cf222e33;color:#f85149;}
.state-done{background:#1a7f3733;color:#3fb950;}
pre{white-space:pre-wrap;word-break:break-all;margin:0;padding:8px;background:var(--vscode-textBlockQuote-background,#1e1e1e);border-radius:4px;max-height:calc(100vh - 180px);overflow-y:auto;}
.prompt{font-size:12px;opacity:0.8;margin-bottom:8px;}
</style></head><body>
<div class="header">
  <strong>${escapeHtml(sessionTaskId.slice(-12))}</strong>
  <span class="state state-${state === 'CLINE_RUNNING' ? 'running' : state === 'CLINE_FAILED' ? 'failed' : 'done'}">${escapeHtml(state)}</span>
  <div class="prompt">${prompt}</div>
</div>
<pre>${escapeHtml(logText) || '<em>Waiting for output...</em>'}</pre>
</body></html>`;
    } catch { /* ignore render errors */ }
}

async function openGoalDetail(goalId: string, context: vscode.ExtensionContext): Promise<void> {
    if (!goalId) return;

    const panel = vscode.window.createWebviewPanel(
        'agentHub.goalDetail', `Goal: ${goalId.slice(-12)}`,
        vscode.ViewColumn.One, { enableScripts: false, retainContextWhenHidden: true },
    );

    const render = async () => {
        let html = `<html><head><style>
            body { font-family: var(--vscode-editor-font-family); font-size: 13px; padding: 20px; color: var(--vscode-foreground); }
            h2 { border-bottom: 1px solid var(--vscode-panel-border); padding-bottom: 8px; }
            .section { margin: 16px 0; }
            .label { font-weight: bold; color: var(--vscode-textLink-foreground); }
            pre { background: var(--vscode-textCodeBlock-background); padding: 12px; border-radius: 4px; overflow-x: auto; max-height: 300px; }
            table { border-collapse: collapse; width: 100%; }
            th, td { border: 1px solid var(--vscode-panel-border); padding: 6px 10px; text-align: left; }
            th { background: var(--vscode-toolbar-hoverBackground); }
            .state-badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 11px; font-weight: bold; }
            .terminal { color: var(--vscode-errorForeground); }
            .active { color: var(--vscode-textLink-foreground); }
        </style></head><body>`;

        try {
            const [goalResp, tasksResp] = await Promise.all([
                client.request('get_goal', { goal_id: goalId }, 5000),
                client.request('get_goal_tasks', { goal_id: goalId }, 5000),
            ]);
            const g = goalResp || {};
            const tasks = tasksResp?.tasks || [];

            const state = g.state || '?';
            html += `<h2>${escapeHtml(g.objective || goalId)}</h2>`;
            html += `<div class="section"><span class="label">State:</span> <span class="${isGoalTerminal(state) ? 'terminal' : 'active'}">${escapeHtml(state)}</span></div>`;
            html += `<div class="section"><span class="label">ID:</span> ${escapeHtml(goalId)}</div>`;
            if (g.workspace_cwd) html += `<div class="section"><span class="label">Workspace:</span> ${escapeHtml(g.workspace_cwd)}</div>`;
            if (g.completion_criteria) html += `<div class="section"><span class="label">Completion Criteria:</span> <pre>${escapeHtml(g.completion_criteria)}</pre></div>`;
            if (g.stop_conditions) html += `<div class="section"><span class="label">Stop Conditions:</span> <pre>${escapeHtml(g.stop_conditions)}</pre></div>`;

            html += `<div class="section">`;
            html += `<span class="label">Iterations:</span> ${g.iteration_count || 0}/${g.max_iterations || '?'} &nbsp;`;
            html += `<span class="label">Failures:</span> ${g.failure_count || 0}/${g.max_failures || '?'} &nbsp;`;
            html += `<span class="label">Model Calls:</span> ${g.accumulated_model_calls || 0}/${g.model_call_budget || '?'}`;
            html += `</div>`;

            if (g.latest_codex_decision) {
                try {
                    const decision = typeof g.latest_codex_decision === 'string'
                        ? JSON.parse(g.latest_codex_decision) : g.latest_codex_decision;
                    html += `<div class="section"><span class="label">Latest Codex Decision:</span>`;
                    html += `<pre>${escapeHtml(JSON.stringify(decision, null, 2))}</pre></div>`;
                } catch { html += `<div class="section"><span class="label">Latest Codex Decision:</span> ${escapeHtml(String(g.latest_codex_decision))}</div>`; }
            }

            if (g.error_summary) html += `<div class="section"><span class="label">Errors:</span> <pre>${escapeHtml(g.error_summary)}</pre></div>`;
            if (g.orchestrator_step) html += `<div class="section"><span class="label">Orchestrator Step:</span> ${escapeHtml(g.orchestrator_step)}</div>`;

            html += `<h3>Linked Tasks (${tasks.length})</h3>`;
            if (tasks.length > 0) {
                html += `<table><tr><th>ID</th><th>State</th><th>Type</th><th>Prompt</th><th>Exit Code</th></tr>`;
                for (const t of tasks) {
                    html += `<tr><td>${escapeHtml((t.id || '').slice(-12))}</td>`;
                    html += `<td>${escapeHtml(t.state || '?')}</td>`;
                    html += `<td>${escapeHtml(t.task_type || '?')}</td>`;
                    html += `<td>${escapeHtml((t.prompt || '').slice(0, 60))}</td>`;
                    html += `<td>${t.cline_exit_code !== null ? t.cline_exit_code : '-'}</td></tr>`;
                }
                html += `</table>`;
            } else {
                html += `<p>No linked tasks.</p>`;
            }

        } catch (e: any) {
            html += `<p class="terminal">Error loading goal: ${escapeHtml(e.message || String(e))}</p>`;
        }

        html += `</body></html>`;
        panel.webview.html = html;
    };

    await render();
}

async function openTask(taskId: string): Promise<void> {
    sessionTaskId = taskId;
    if (sessionPanel) {
        sessionPanel.reveal();
    } else {
        sessionPanel = vscode.window.createWebviewPanel(
            'agentHub.session',
            `Agent Hub: ${taskId.slice(-12)}`,
            vscode.ViewColumn.Two,
            { enableScripts: false, retainContextWhenHidden: true },
        );
        sessionPanel.onDidDispose(() => {
            sessionPanel = null;
            sessionTaskId = null;
            if (sessionTimer) { clearInterval(sessionTimer); sessionTimer = null; }
        });
    }
    await renderSession();
    // Auto-refresh session every 2s while running
    if (sessionTimer) clearInterval(sessionTimer);
    sessionTimer = setInterval(() => {
        if (sessionTaskId) void renderSession();
    }, 2000);
}

async function openTaskLog(taskId?: string, logPath?: string): Promise<void> {
    if (!taskId && !logPath) return;
    // Try to get log path from daemon if not provided
    if (!logPath && taskId && client.connected) {
        try {
            const t = await client.request('get_task', { task_id: taskId }, 5000);
            logPath = t?.log_stdout || undefined;
        } catch { }
    }
    if (logPath && fs.existsSync(logPath)) {
        await vscode.window.showTextDocument(vscode.Uri.file(logPath), { preview: true });
        return;
    }
    vscode.window.showInformationMessage('Agent Hub: no session log yet — the task has not produced output.');
}


// ---- Goals Tree View ----
class GoalItem extends vscode.TreeItem {
    constructor(
        public readonly goalId: string,
        label: string,
        state: string,
        public readonly iterationCount: number,
        public readonly failureCount: number,
        public readonly taskCount: number,
        public readonly maxIterations: number,
        public readonly maxFailures: number,
        public readonly modelCalls: number,
        collapsibleState: vscode.TreeItemCollapsibleState,
        public readonly isTaskChild?: boolean,
        public readonly taskChildData?: any,
        public readonly workspaceCwd?: string,
    ) {
        super(label, collapsibleState);
        if (isTaskChild) {
            this.description = (taskChildData?.state || '?') + ' | ' + ((taskChildData?.prompt || '').slice(0, 40));
            this.contextValue = 'task';
            this.command = { command: 'agent-hub.openTask', title: 'Watch Task', arguments: [taskChildData?.id] };
            this.iconPath = new vscode.ThemeIcon('file-code');
        } else {
            this.description = `${state} | iter=${iterationCount}/${maxIterations} | fail=${failureCount}/${maxFailures} | tasks=${taskCount} | mcalls=${modelCalls}`;
            if (workspaceCwd) {
                this.tooltip = `ID: ${goalId}\nObjective: ${label}\nWorkspace: ${workspaceCwd}`;
            } else {
                this.tooltip = `ID: ${goalId}\nObjective: ${label}`;
            }
            // Assign context value based on terminal vs active state for
            // conditional context-menu visibility (delete only for terminal).
            const terminalStates = ['GOAL_COMPLETED', 'GOAL_FAILED', 'GOAL_CANCELLED'];
            if (goalId) {
                this.contextValue = terminalStates.includes(state) ? 'goalTerminal' : 'goalActive';
            } else {
                this.contextValue = 'goalStatus';
            }
            this.command = { command: 'agent-hub.openGoalDetail', title: 'Goal Detail', arguments: [goalId] };
            if (state === 'GOAL_COMPLETED') { this.iconPath = new vscode.ThemeIcon('check'); }
            else if (state === 'GOAL_FAILED' || state === 'GOAL_CANCELLED') { this.iconPath = new vscode.ThemeIcon('error'); }
            else if (state === 'empty') { this.iconPath = new vscode.ThemeIcon('info'); }
            else if (state === 'disconnected') { this.iconPath = new vscode.ThemeIcon('debug-disconnect'); }
            else if (state.includes('EXECUTING') || state.includes('RUNNING') || state.includes('PLANNING')) {
                this.iconPath = new vscode.ThemeIcon('sync~spin');
            }
            else { this.iconPath = new vscode.ThemeIcon('circle-outline'); }
        }
    }
}

class GoalTreeProvider implements vscode.TreeDataProvider<GoalItem> {
    private _onDidChange = new vscode.EventEmitter<GoalItem | undefined>();
    readonly onDidChangeTreeData = this._onDidChange.event;

    refresh(): void { this._onDidChange.fire(undefined); }

    async getChildren(element?: GoalItem): Promise<GoalItem[]> {
        if (element) {
            // Expand a goal → show its linked tasks
            if (element.isTaskChild) return [];
            try {
                const result = await client.request('get_goal_tasks', { goal_id: element.goalId }, 5000);
                const tasks = result.tasks || [];
                return tasks.map((t: any) => new GoalItem(
                    t.id, (t.prompt || '').slice(0, 60), t.state || '?',
                    0, 0, 0, 0, 0, 0,
                    vscode.TreeItemCollapsibleState.None, true, t,
                ));
            } catch {
                return [];
            }
        }
        // Root level
        if (!client.connected) { return [
            new GoalItem('', 'Daemon not connected', 'disconnected', 0, 0, 0, 0, 0, 0, vscode.TreeItemCollapsibleState.None),
        ]; }
        try {
            const r = await client.request('list_goals', { include_terminal: true }, 5000);
            const goals = r?.goals || [];
            if (goals.length === 0) { return [
                new GoalItem('', 'No active goals', 'empty', 0, 0, 0, 0, 0, 0, vscode.TreeItemCollapsibleState.None),
            ]; }
            return goals.map((g: any) => new GoalItem(
                g.id, g.objective?.slice(0, 60) || g.id,
                g.state, g.iteration_count || 0, g.failure_count || 0,
                g.task_count ?? 0,
                g.max_iterations || 5, g.max_failures || 3,
                g.accumulated_model_calls || 0,
                vscode.TreeItemCollapsibleState.Collapsed,
                false, undefined, g.workspace_cwd || '',
            ));
        } catch { return []; }
    }

    getTreeItem(element: GoalItem): GoalItem { return element; }
}

export function activate(context: vscode.ExtensionContext): AgentHubApi {
    extensionContext = context;
    output = vscode.window.createOutputChannel('Agent Hub');
    statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusBar.name = 'Agent Hub';
    statusBar.show();
    client = new DaemonClient(() => port());
    provider = new TaskTreeProvider();
	goalsProvider = new GoalTreeProvider();

    const api: AgentHubApi = {
        version: '0.2.8',
        submitTask: async (prompt: string, options?: { cwd?: string }) => {
            await ensureDaemon(true);
            const cwd = options?.cwd ?? workspaceRoot();
            if (!cwd) throw new Error('No workspace folder open and no cwd was supplied');
            const r = await client.request('submit_task', { prompt, cwd }, 15000);
            debouncedRefresh();
            return String(r.task_id);
        },
        cancelTask: async (taskId: string) => {
            await client.request('cancel_task', { task_id: taskId });
            debouncedRefresh();
        },
        startGoal: async (objective: string, options?: { max_iterations?: number; max_failures?: number; completion_criteria?: string; stop_conditions?: string; model_call_budget?: number; cwd?: string }) => {
            await ensureDaemon(true);
            const cwd = options?.cwd ?? workspaceRoot();
            if (!cwd) throw new Error('No workspace folder open and no cwd was supplied');
            const params: any = { objective, cwd };
            if (options?.max_iterations !== undefined) params.max_iterations = options.max_iterations;
            if (options?.max_failures !== undefined) params.max_failures = options.max_failures;
            if (options?.completion_criteria !== undefined) params.completion_criteria = options.completion_criteria;
            if (options?.stop_conditions !== undefined) params.stop_conditions = options.stop_conditions;
            if (options?.model_call_budget !== undefined) params.model_call_budget = options.model_call_budget;
            const r = await client.request('start_goal', params, 15000);
            debouncedRefresh();
            goalsProvider.refresh();
            return r;
        },
        cancelGoal: async (goalId: string) => {
            await client.request('cancel_goal', { goal_id: goalId }, 10000);
            goalsProvider.refresh();
        },
        deleteGoal: async (goalId: string) => {
            const r = await client.request('delete_goal', { goal_id: goalId }, 15000);
            goalsProvider.refresh();
            provider.refresh();
            return r;
        },
        getGoalTasks: async (goalId: string) => {
            return await client.request('get_goal_tasks', { goal_id: goalId }, 5000);
        },
        waitForEvent: (goalId?: string, eventTypes?: string[], timeout?: number) => {
            return client.waitForEvent(goalId, eventTypes, timeout);
        },
        getStatus: () => client.request('get_status', {}, 5000),
        onDidChangeState: client.onDidChangeState,
        onDidChangeGoalState: client.onDidChangeGoalState,
    };

    context.subscriptions.push(
        output,
        statusBar,
        vscode.window.registerTreeDataProvider(VIEW_ID, provider),
        vscode.window.registerTreeDataProvider('agent-hub.goals', goalsProvider),
        vscode.commands.registerCommand('agent-hub.startGoal', async () => {
            const objective = await vscode.window.showInputBox({
                prompt: 'Goal objective',
                placeHolder: 'Describe the research goal...',
            });
            if (!objective) return;
            try {
                await ensureDaemon(true);
                const cwd = workspaceRoot();
                if (!cwd) throw new Error('Open a workspace folder before starting a goal');
                const r = await client.request('start_goal', {
                    objective,
                    cwd,
                    max_iterations: 5,
                    max_failures: 3,
                }, 15000);
                vscode.window.showInformationMessage(`Agent Hub: goal ${r.id.slice(-12)} started`);
                debouncedRefresh();
                goalsProvider.refresh();
            } catch (e: any) {
                vscode.window.showErrorMessage(`Agent Hub: ${e?.message || e}`);
            }
        }),
        vscode.commands.registerCommand('agent-hub.cancelGoal', async (arg?: any) => {
            const goalId = arg?.goalId ?? arg?.goal_id ?? (typeof arg === 'string' ? arg : undefined);
            if (!goalId) return;
            try {
                await client.request('cancel_goal', { goal_id: goalId }, 10000);
                goalsProvider.refresh();
            } catch (e: any) {
                vscode.window.showErrorMessage(`Agent Hub: ${e?.message || e}`);
            }
        }),
        vscode.commands.registerCommand('agent-hub.deleteGoal', async (arg?: any) => {
            const goalId = arg?.goalId ?? arg?.goal_id ?? (typeof arg === 'string' ? arg : undefined);
            if (!goalId) return;
            const confirm = await vscode.window.showWarningMessage(
                `Delete goal ${goalId.slice(-12)} and all linked records? This removes its tasks, events, model-call records, and Agent Hub log files. Project outputs are kept. This action cannot be undone.`,
                { modal: true },
                'Delete',
            );
            if (confirm !== 'Delete') return;
            try {
                const r = await client.request('delete_goal', { goal_id: goalId }, 15000);
                vscode.window.showInformationMessage(
                    `Goal ${goalId.slice(-12)} deleted (${r.deleted_task_count} tasks removed).`);
                goalsProvider.refresh();
                provider.refresh();
            } catch (e: any) {
                vscode.window.showErrorMessage(`Agent Hub: ${e?.message || e}`);
            }
        }),
        vscode.commands.registerCommand('agent-hub.openGoalDetail', async (arg: any) => {
            const goalId = arg?.goalId ?? arg?.goal_id ?? (typeof arg === 'string' ? arg : undefined);
            if (goalId) await openGoalDetail(String(goalId), context);
        }),
        vscode.commands.registerCommand('agent-hub.refreshGoals', () => goalsProvider.refresh()),
        vscode.commands.registerCommand('agent-hub.submitTask', async (arg?: any) => {
            let prompt: string | undefined;
            let cwd: string | undefined;
            if (typeof arg === 'string') {
                prompt = arg;
            } else if (arg && typeof arg.prompt === 'string') {
                prompt = arg.prompt;
                if (typeof arg.cwd === 'string') cwd = arg.cwd;
            }
            if (!prompt) {
                prompt = await vscode.window.showInputBox({
                    prompt: 'Task prompt for the coding agent CLI',
                    placeHolder: 'Describe what to do...',
                });
            }
            if (!prompt) return;
            try {
                const id = await api.submitTask(prompt, { cwd });
                vscode.window.showInformationMessage(`Agent Hub: task ${id.slice(-12)} submitted`);
            } catch (e: any) {
                vscode.window.showErrorMessage(`Agent Hub: ${e?.message || e}`);
            }
        }),
        vscode.commands.registerCommand('agent-hub.cancelTask', async (arg?: any) => {
            const taskId = arg?.taskId ?? arg?.task_id ?? (typeof arg === 'string' ? arg : undefined);
            if (!taskId) return;
            try {
                await api.cancelTask(String(taskId));
            } catch (e: any) {
                vscode.window.showErrorMessage(`Agent Hub: ${e?.message || e}`);
            }
        }),
        vscode.commands.registerCommand('agent-hub.openTask', openTask),
        vscode.commands.registerCommand('agent-hub.openTaskLog', openTaskLog),
        vscode.commands.registerCommand('agent-hub.refresh', () => provider.refresh()),
        vscode.commands.registerCommand('agent-hub.startDaemon', async () => {
            await vscode.window.withProgress(
                { location: vscode.ProgressLocation.Window, title: 'Agent Hub: starting daemon' },
                async () => {
                    try {
                        await ensureDaemon(true);
                    } catch (e: any) {
                        vscode.window.showErrorMessage(`Agent Hub: ${e?.message || e}`);
                    }
                },
            );
        }),
        vscode.commands.registerCommand('agent-hub.showLog', () => output.show()),
        vscode.workspace.onDidChangeConfiguration((e) => {
            if (e.affectsConfiguration('agentHub')) cachedPython = undefined;
        }),
        { dispose: () => client.dispose() },
    );

    client.onDidChangeConnection((up) => {
        setStatus(up ? 'connected' : 'stopped');
        debouncedRefresh();
        goalsProvider.refresh();
    });
    client.onDidChangeState((e) => {
        debouncedRefresh();
        goalsProvider.refresh();
        if (sessionTaskId && e.task_id === sessionTaskId) void renderSession();
    });
    client.onDidChangeGoalState(() => {
        goalsProvider.refresh();
        debouncedRefresh();
    });

    setStatus('stopped');
    const timer = setInterval(() => {
        if (client.connected) {
            debouncedRefresh();
        } else if (autoStart()) {
            void ensureDaemon().catch(() => { /* logged and shown in status */ });
        }
    }, 10000);
    context.subscriptions.push({ dispose: () => clearInterval(timer) });

    // Boot in the background: never block activation on the daemon.
    void ensureDaemon().catch(() => { /* logged and shown in status */ });

    return api;
}

export function deactivate(): void {
    client?.dispose();
}
