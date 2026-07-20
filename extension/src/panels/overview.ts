import * as vscode from 'vscode';
import { GaussClient } from '../client';

export class OverviewPanel {
    private panel: vscode.WebviewPanel;
    private client: GaussClient;
    private updateInterval: NodeJS.Timeout | undefined;

    constructor(context: vscode.ExtensionContext, client: GaussClient) {
        this.client = client;
        this.panel = vscode.window.createWebviewPanel(
            'gaussOverview', 'GAUSS Agent Control Center',
            vscode.ViewColumn.One,
            { enableScripts: true, retainContextWhenHidden: true }
        );
        this.panel.onDidDispose(() => this.dispose());
        this.panel.webview.html = this.buildHtml();
        this.panel.webview.onDidReceiveMessage(async (msg) => {
            await this.handleMessage(msg);
        });
        this.updateInterval = setInterval(() => this.refresh(), 2000);
    }

    reveal() { this.panel.reveal(); }

    async refresh() {
        try {
            const status = await this.client.getStatus();
            const budget = await this.client.getBudgetStatus();
            this.panel.webview.postMessage({
                type: 'status',
                tasks: status.tasks || [],
                active_count: status.active_count || 0,
                total_count: status.total_count || 0,
                budget: budget
            });
        } catch {
            this.panel.webview.postMessage({ type: 'disconnected' });
        }
    }

    private async handleMessage(msg: any) {
        try {
            switch (msg.command) {
                case 'submit':
                    await vscode.commands.executeCommand('gauss-agent.submitTask');
                    this.refresh(); break;
                case 'cancel':
                    await this.client.cancelTask(msg.taskId);
                    this.refresh(); break;
                case 'viewLog':
                    const log = await this.client.getLogTail(msg.taskId, 100);
                    this.showLog(msg.taskId, log.stdout || ''); break;
                case 'refresh':
                    this.refresh(); break;
            }
        } catch (e: any) {
            vscode.window.showErrorMessage('GAUSS: ' + e.message);
        }
    }

    private showLog(taskId: string, content: string) {
        vscode.workspace.openTextDocument({ content, language: 'plaintext' })
            .then(d => vscode.window.showTextDocument(d, { preview: true }));
    }

    private buildHtml(): string {
        return `<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>GAUSS Agent Control Center</title>
<style>
:root{--bg:var(--vscode-editor-background,#1e1e1e);--fg:var(--vscode-foreground,#d4d4d4);--border:var(--vscode-panel-border,#3c3c3c);--accent:#00d4ff;--accent2:#7b2ff7;--green:#00ff88;--yellow:#ffcc00;--red:#ff4444;--dim:var(--vscode-descriptionForeground,#888)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:var(--vscode-font-family);font-size:12px;color:var(--fg);background:var(--bg);padding:12px;line-height:1.5}
.header{display:flex;align-items:center;gap:10px;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--border)}
.header h2{font-size:15px;font-weight:600;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.header .dot{width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.stats-bar{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px}
.stat-card{background:var(--vscode-editor-inactiveSelectionBackground,#2a2a2a);border-radius:6px;padding:10px;text-align:center}
.stat-card .value{font-size:22px;font-weight:700}
.stat-card .label{font-size:10px;color:var(--dim);margin-top:2px;text-transform:uppercase}
.stat-card.running .value{color:var(--yellow)}.stat-card.done .value{color:var(--green)}
.stat-card.failed .value{color:var(--red)}.stat-card.total .value{color:var(--accent)}
.task-list{margin-bottom:14px}
.task-card{border:1px solid var(--border);border-radius:6px;padding:10px;margin-bottom:6px}
.task-card:hover{border-color:var(--accent)}
.task-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.task-id{font-family:monospace;font-size:11px;color:var(--dim)}
.state-badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;text-transform:uppercase}
.state-QUEUED{background:#1a3a5c;color:#4da6ff}
.state-CLINE_STARTING{background:#3a2a0a;color:var(--yellow)}
.state-CLINE_RUNNING{background:#2a3a0a;color:#88cc00;animation:pulse-bg 1.5s infinite}
@keyframes pulse-bg{0%,100%{opacity:1}50%{opacity:.7}}
.state-CLINE_SUCCEEDED{background:#0a2a1a;color:var(--green)}
.state-CLINE_FAILED{background:#3a0a0a;color:var(--red)}
.state-CLINE_STALLED{background:#3a1a0a;color:#ff8800}
.state-WAITING_APPROVAL{background:#2a1a3a;color:var(--accent2)}
.state-CANCELLED{background:#1a1a1a;color:var(--dim)}
.task-info{display:grid;grid-template-columns:repeat(2,1fr);gap:4px;font-size:11px}
.task-info .kv{display:flex;gap:4px}
.task-info .key{color:var(--dim)}.task-info .val{color:var(--fg);font-weight:500}
.task-actions{margin-top:6px;display:flex;gap:4px}
button{border:1px solid var(--border);background:var(--vscode-button-secondaryBackground,#3c3c3c);color:var(--fg);padding:3px 10px;border-radius:4px;font-size:11px;cursor:pointer}
button:hover{background:var(--vscode-button-secondaryHoverBackground,#505050)}
button.primary{background:linear-gradient(135deg,var(--accent),var(--accent2));border:none;color:#fff;font-weight:600}
button.danger{border-color:var(--red);color:var(--red)}
button.danger:hover{background:var(--red);color:#fff}
.empty-state{text-align:center;padding:30px;color:var(--dim);font-size:13px}
.empty-state .big{font-size:40px;margin-bottom:8px}
.budget-bar{margin-bottom:14px;padding:8px 10px;background:var(--vscode-editor-inactiveSelectionBackground,#2a2a2a);border-radius:6px;font-size:11px}
.budget-bar .caller{font-weight:600;color:var(--accent)}
.budget-bar .used{color:var(--yellow)}
.footer{border-top:1px solid var(--border);padding-top:8px;font-size:10px;color:var(--dim);display:flex;justify-content:space-between}
</style></head><body>
<div class="header"><div class="dot"></div><h2>GAUSS Agent Control Center</h2></div>
<div class="stats-bar">
<div class="stat-card total"><div class="value" id="total-count">0</div><div class="label">Total Tasks</div></div>
<div class="stat-card running"><div class="value" id="active-count">0</div><div class="label">Active</div></div>
<div class="stat-card done"><div class="value" id="done-count">0</div><div class="label">Succeeded</div></div>
<div class="stat-card failed"><div class="value" id="failed-count">0</div><div class="label">Failed</div></div>
</div>
<div class="budget-bar" id="budget-bar" style="display:none">Budget: <span id="budget-content"></span></div>
<div class="task-list" id="task-list"><div class="empty-state"><div class="big">&#128640;</div><div>No tasks yet. Submit a task to get started.</div></div></div>
<div style="text-align:center;margin-bottom:10px">
<button class="primary" onclick="post('submit')">+ New Task</button>
<button onclick="post('refresh')">Refresh</button>
</div>
<div class="footer"><span id="connection-status">Connecting...</span><span id="update-time"></span></div>
<script>
const vsc=acquireVsCodeApi();
function post(c,d){vsc.postMessage({command:c,...(d||{})})}
function esc(s){return(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function ago(ts){if(!ts)return'';var d=new Date(ts),s=Math.floor((new Date()-d)/1000);if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m';if(s<86400)return Math.floor(s/3600)+'h';return d.toLocaleDateString()}
function render(d){var t=d.tasks||[];ge('total-count').textContent=d.total_count||0;ge('active-count').textContent=d.active_count||0;ge('done-count').textContent=t.filter(function(x){return x.state==='CLINE_SUCCEEDED'}).length;ge('failed-count').textContent=t.filter(function(x){return x.state==='CLINE_FAILED'||x.state==='CLINE_STALLED'}).length;
var b=d.budget||{},bd=ge('budget-bar'),bc=ge('budget-content');
if(b.cline||b.codex){bd.style.display='block';var p=[];for(var k in b){p.push('<span class=caller>'+k+'</span>: <span class=used>'+(b[k].daily||0)+'</span> today')}bc.innerHTML=p.join(' | ')}else{bd.style.display='none'}
var l=ge('task-list');
if(!t.length){l.innerHTML='<div class=empty-state><div class=big>&#128640;</div><div>No tasks yet.</div></div>'}else{
var h='';t.slice().reverse().forEach(function(x){h+='<div class=task-card>';
h+='<div class=task-header><span class=task-id>'+esc((x.id||'').slice(-20))+'</span><span class=\"state-badge state-'+x.state+'\">'+x.state.replace('CLINE_','')+'</span></div>';
h+='<div class=task-info>';
if(x.cline_pid)h+='<div class=kv><span class=key>PID</span><span class=val>'+x.cline_pid+'</span></div>';
if(x.cline_exit_code!=null)h+='<div class=kv><span class=key>Exit</span><span class=val>'+x.cline_exit_code+'</span></div>';
h+='<div class=kv><span class=key>Retries</span><span class=val>'+(x.retry_count||0)+'/'+(x.max_retries||1)+'</span></div>';
h+='<div class=kv><span class=key>Created</span><span class=val>'+ago(x.created_at)+'</span></div>';
if(x.started_at)h+='<div class=kv><span class=key>Started</span><span class=val>'+ago(x.started_at)+'</span></div>';
if(x.completed_at)h+='<div class=kv><span class=key>Done</span><span class=val>'+ago(x.completed_at)+'</span></div>';
if(x.task_type)h+='<div class=kv><span class=key>Type</span><span class=val>'+esc(x.task_type)+'</span></div>';
if(x.error_summary)h+='<div class=kv style=grid-column:1/-1><span class=key>Error</span><span class=val style=color:var(--red)>'+esc(x.error_summary)+'</span></div>';
h+='</div><div class=task-actions>';
if(['CLINE_STARTING','CLINE_RUNNING','QUEUED'].indexOf(x.state)>=0)h+='<button class=danger onclick=post(\"cancel\",{taskId:\"'+x.id+'\"})>Cancel</button>';
if(x.state==='WAITING_APPROVAL'){h+='<button class=primary onclick=post(\"approve\",{taskId:\"'+x.id+'\"})>Approve</button>';h+='<button class=danger onclick=post(\"cancel\",{taskId:\"'+x.id+'\"})>Reject</button>'}
if(x.log_stdout)h+='<button onclick=post(\"viewLog\",{taskId:\"'+x.id+'\"})>Log</button>';
h+='</div></div>'});l.innerHTML=h}
ge('connection-status').innerHTML='<span style=color:var(--green)>&#9679;</span> Connected';
ge('update-time').textContent=new Date().toLocaleTimeString()}
function ge(id){return document.getElementById(id)}
window.addEventListener('message',function(e){var m=e.data;if(m.type==='status')render(m);else if(m.type==='disconnected'){ge('connection-status').innerHTML='<span style=color:var(--red)>&#9679;</span> Disconnected'}});
</script></body></html>`;
    }

    dispose() {
        if (this.updateInterval) clearInterval(this.updateInterval);
        this.panel.dispose();
    }
}
