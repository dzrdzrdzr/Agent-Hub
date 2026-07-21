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
    getStatus(): Promise<any>;
    /** Fired when the daemon reports a task state change. */
    onDidChangeState: vscode.Event<{ task_id: string; state: string }>;
}

let output: vscode.OutputChannel;
let statusBar: vscode.StatusBarItem;
let client: DaemonClient;
let provider: TaskTreeProvider;
let hubState: 'starting' | 'connected' | 'stopped' = 'stopped';
let lastError = '';
let activeCount = 0;
let cachedPython: string | null | undefined;
let bootPromise: Promise<void> | null = null;
let sessionPanel: vscode.WebviewPanel | null = null;
let sessionTaskId: string | null = null;
let sessionTimer: NodeJS.Timeout | null = null;

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

function isTerminal(state: string): boolean {
    return ['CLINE_SUCCEEDED', 'CLINE_FAILED', 'CLINE_STALLED', 'CANCELLED'].includes(state);
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
    private _onDidChangeConnection = new vscode.EventEmitter<boolean>();
    readonly onDidChangeConnection = this._onDidChangeConnection.event;

    constructor(private getPort: () => number) { }

    get connected(): boolean { return !!this.socket && !this.socket.destroyed; }

    ensureConnected(): Promise<void> {
        if (this.connected) return Promise.resolve();
        if (this.connecting) return this.connecting;
        this.connecting = this.open().finally(() => { this.connecting = null; });
        return this.connecting;
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

        if (!this.disposed) {
            this.reconnectTimer = setTimeout(() => {
                this.reconnectTimer = null;
                this.ensureConnected().catch(() => {});
            }, this.reconnectDelay);
            this.reconnectDelay = Math.min(this.reconnectDelay * 2, 30000);
        }
    }

    private handlePush(event: string, data: any): void {
        if (event === 'state_changed') {
            this._onDidChangeState.fire(data);
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
            const tasks: any[] = s.tasks || [];
            if (tasks.length === 0) return [
                new TaskItem('No tasks yet', 'Ctrl+Shift+P → "Agent Hub: Delegate Task to Cline"', 'empty'),
                new TaskItem('You plan, Cline executes', 'Write a detailed prompt with file paths and expected changes', 'empty'),
            ];
            return tasks.map((t: any) => {
                const id = t.id || '';
                const state = t.state || 'UNKNOWN';
                const label = `${id.slice(-12)} [${state}]`;
                const prompt = (t.prompt || '').slice(0, 80);
                return new TaskItem(label, prompt, state, id);
            });
        } catch {
            return [
                new TaskItem('Connection lost', 'Daemon may have stopped. Run "Agent Hub: Start Daemon"', 'error'),
            ];
        }
    }
}

class TaskItem extends vscode.TreeItem {
    constructor(
        public readonly label: string,
        public readonly description: string,
        public readonly state: string,
        public readonly taskId?: string,
    ) {
        super(label, vscode.TreeItemCollapsibleState.None);
        this.tooltip = description || state;
        this.contextValue = taskId ? 'task' : 'status';
        // Icon mapping
        switch (state) {
            case 'CLINE_RUNNING': this.iconPath = new vscode.ThemeIcon('sync~spin'); break;
            case 'CLINE_SUCCEEDED': this.iconPath = new vscode.ThemeIcon('pass'); break;
            case 'CLINE_FAILED': this.iconPath = new vscode.ThemeIcon('error'); break;
            case 'CLINE_STALLED': this.iconPath = new vscode.ThemeIcon('warning'); break;
            case 'CANCELLED': this.iconPath = new vscode.ThemeIcon('circle-slash'); break;
            case 'WAITING_APPROVAL': this.iconPath = new vscode.ThemeIcon('key'); break;
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
    if (cfg && fs.existsSync(cfg)) { cachedPython = cfg; return cfg; }
    // Try common paths
    for (const p of ['python3', 'python', '/usr/bin/python3', '/usr/bin/python']) {
        try {
            const r = cp.spawnSync(p, ['-c', 'import sys; print(sys.executable)'], { timeout: 3000 });
            if (r.status === 0 && r.stdout) {
                cachedPython = r.stdout.toString().trim();
                return cachedPython;
            }
        } catch { /* continue */ }
    }
    cachedPython = 'python3';
    return 'python3';
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

        const cwd = workspaceRoot();
        if (!cwd) throw new Error('No workspace folder open');

        const python = await findPython();
        setStatus('starting');
        log(`Starting daemon with ${python}...`);

        const daemonScript = path.join(cwd, 'agentd', 'agent_hub', 'main.py');
        const configPath = path.join(cwd, 'config.yaml');
        const env: any = { ...process.env };
        env.PYTHONPATH = path.join(cwd, 'agentd');
        env.AGENT_HUB_CONFIG = configPath;

        const child = cp.spawn(python, ['-B', '-u', '-m', 'agent_hub.main'], {
            cwd,
            env,
            stdio: 'ignore',
            detached: true,
        });
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
    } catch (e) {
        bootPromise = null;
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

export function activate(context: vscode.ExtensionContext): AgentHubApi {
    output = vscode.window.createOutputChannel('Agent Hub');
    statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusBar.name = 'Agent Hub';
    statusBar.show();
    client = new DaemonClient(() => port());
    provider = new TaskTreeProvider();

    const api: AgentHubApi = {
        version: '0.2.3',
        submitTask: async (prompt: string, options?: { cwd?: string }) => {
            await ensureDaemon(true);
            const r = await client.request('submit_task', { prompt, cwd: options?.cwd ?? workspaceRoot() }, 15000);
            debouncedRefresh();
            return String(r.task_id);
        },
        cancelTask: async (taskId: string) => {
            await client.request('cancel_task', { task_id: taskId });
            debouncedRefresh();
        },
        getStatus: () => client.request('get_status', {}, 5000),
        onDidChangeState: client.onDidChangeState,
    };

    context.subscriptions.push(
        output,
        statusBar,
        vscode.window.registerTreeDataProvider(VIEW_ID, provider),
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
    });
    client.onDidChangeState((e) => {
        debouncedRefresh();
        if (sessionTaskId && e.task_id === sessionTaskId) void renderSession();
    });

    setStatus('stopped');
    const timer = setInterval(() => {
        if (client.connected) debouncedRefresh();
    }, 3000);
    context.subscriptions.push({ dispose: () => clearInterval(timer) });

    // Boot in the background: never block activation on the daemon.
    void ensureDaemon().catch(() => { /* status bar already reflects the failure */ });

    return api;
}

export function deactivate(): void {
    client?.dispose();
}
