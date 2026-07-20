import * as vscode from 'vscode';
import * as cp from 'child_process';
import * as net from 'net';

let statusBar: vscode.StatusBarItem;
let socket: net.Socket | null = null;
let buffer = '';
let reqId = 0;
const pending = new Map<string, { resolve: Function; reject: Function }>();

async function connect(): Promise<net.Socket> {
    if (socket && !socket.destroyed) return socket;
    return new Promise((resolve, reject) => {
        socket = new net.Socket();
        socket.connect(19876, '127.0.0.1', () => resolve(socket!));
        socket.on('error', (err) => reject(err));
        socket.on('data', (data: Buffer) => {
            buffer += data.toString('utf-8');
            const lines = buffer.split('\n');
            buffer = lines.pop() || '';
            for (const line of lines) {
                if (!line.trim()) continue;
                try {
                    const msg = JSON.parse(line);
                    if (msg.type === 'response' && msg.id && pending.has(msg.id)) {
                        const p = pending.get(msg.id)!;
                        pending.delete(msg.id);
                        msg.error ? p.reject(new Error(msg.error)) : p.resolve(msg.result);
                    }
                } catch { }
            }
        });
    });
}

async function request(method: string, params: any = {}): Promise<any> {
    const sock = await connect();
    const id = `req-${++reqId}`;
    return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        sock.write(JSON.stringify({ type: 'request', id, method, params }) + '\n');
        setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error('timeout')); } }, 30000);
    });
}

async function rpc(m: string, p: any = {}) { return request(m, p); }

async function startDaemon(): Promise<boolean> {
    const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    if (!root) return false;
    return new Promise((resolve) => {
        const p = cp.spawn('python', ['-B', '-m', 'agentd.gauss_agentd.main'], {
            cwd: root, detached: true, stdio: 'ignore', windowsHide: true,
        });
        p.on('error', () => resolve(false));
        p.unref();
        setTimeout(() => resolve(true), 4000);
    });
}

async function ensureDaemon(): Promise<void> {
    try {
        const s = new net.Socket();
        await new Promise<void>((res, rej) => {
            s.setTimeout(2000);
            s.connect(19876, '127.0.0.1', () => { s.destroy(); res(); });
            s.on('error', () => { s.destroy(); rej(); });
            s.on('timeout', () => { s.destroy(); rej(); });
        });
    } catch { await startDaemon(); }
}

class TaskItem extends vscode.TreeItem {
    constructor(label: string, desc: string, tip: string, public state: string, public taskId: string) {
        super(label, vscode.TreeItemCollapsibleState.None);
        this.description = desc;
        this.tooltip = tip;
        const icons: Record<string,string> = {
            'CLINE_SUCCEEDED':'pass','CLINE_FAILED':'error','CLINE_STALLED':'warning',
            'CLINE_RUNNING':'sync~spin','CLINE_STARTING':'sync~spin',
            'QUEUED':'circle-outline','CANCELLED':'circle-slash'
        };
        this.iconPath = new vscode.ThemeIcon(icons[state] || 'circle-outline');
    }
}

class TaskTreeProvider implements vscode.TreeDataProvider<TaskItem> {
    private _onDidChange = new vscode.EventEmitter<TaskItem | undefined>();
    readonly onDidChangeTreeData = this._onDidChange.event;
    refresh() { this._onDidChange.fire(undefined); }
    getTreeItem(element: TaskItem): vscode.TreeItem { return element; }
    async getChildren(): Promise<TaskItem[]> {
        try {
            const s: any = await rpc('get_status');
            const tasks = s.tasks || [];
            statusBar.text = s.active_count > 0 ? `$(sync~spin) GAUSS (${s.active_count})` : `$(check) GAUSS`;
            return tasks.map((t: any) => new TaskItem(
                `${t.id.slice(-12)} - ${t.state.replace('CLINE_','')}`,
                `exit=${t.cline_exit_code ?? '?'} r${t.retry_count||0}/${t.max_retries||1}`,
                `${t.state} | PID:${t.cline_pid||'?'} | ${t.error_summary||''}`,
                t.state, t.id
            ));
        } catch {
            statusBar.text = '$(error) GAUSS';
            return [new TaskItem('Disconnected','','','CANCELLED','')];
        }
    }
}

export async function activate(context: vscode.ExtensionContext) {
    statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusBar.text = '$(sync~spin) GAUSS starting...';
    statusBar.show();
    context.subscriptions.push(statusBar);
    await ensureDaemon();
    const provider = new TaskTreeProvider();
    vscode.window.registerTreeDataProvider('gauss-agent.tasks', provider);
    context.subscriptions.push(
        vscode.commands.registerCommand('gauss-agent.submitTask', async () => {
            const prompt = await vscode.window.showInputBox({ prompt: 'Cline task prompt', placeHolder: 'Describe what to do...' });
            if (prompt) { try { await rpc('submit_task',{prompt}); provider.refresh(); } catch(e:any) { vscode.window.showErrorMessage(e.message); } }
        }),
        vscode.commands.registerCommand('gauss-agent.cancelTask', async (item?: TaskItem) => {
            if (item) { await rpc('cancel_task',{task_id:item.taskId}); provider.refresh(); }
        }),
        vscode.commands.registerCommand('gauss-agent.refresh', () => provider.refresh()),
    );
    setInterval(() => provider.refresh(), 3000);
}

export function deactivate() { if (socket) { socket.destroy(); socket = null; } }