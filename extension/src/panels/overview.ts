import * as vscode from 'vscode';
import { GaussClient } from '../client';

export class OverviewPanel {
    private panel: vscode.WebviewPanel;
    private client: GaussClient;
    private updateInterval: NodeJS.Timeout | undefined;

    constructor(context: vscode.ExtensionContext, client: GaussClient) {
        this.client = client;
        this.panel = vscode.window.createWebviewPanel(
            'gaussOverview',
            'GAUSS Agent Overview',
            vscode.ViewColumn.One,
            { enableScripts: true, retainContextWhenHidden: true }
        );
        this.panel.onDidDispose(() => this.dispose());
        this.render();
        this.updateInterval = setInterval(() => this.refresh(), 3000);
    }

    reveal() {
        this.panel.reveal();
    }

    async refresh() {
        try {
            const status = await this.client.getStatus();
            this.panel.webview.postMessage({ type: 'status', data: status });
        } catch {
            this.panel.webview.postMessage({ type: 'error', data: 'Disconnected' });
        }
    }

    private render() {
        this.panel.webview.html = this.getHtml();
        this.panel.webview.onDidReceiveMessage(async (msg) => {
            switch (msg.command) {
                case 'cancel':
                    await this.client.cancelTask(msg.taskId);
                    this.refresh();
                    break;
                case 'viewLog':
                    const log = await this.client.getLogTail(msg.taskId);
                    vscode.window.showInformationMessage(
                        `Log tail for ${msg.taskId}:\n${(log.stdout || '').slice(-500)}`
                    );
                    break;
                case 'submit':
                    await vscode.commands.executeCommand('gauss-agent.submitTask');
                    break;
            }
        });
    }

    private getHtml(): string {
        return `<!DOCTYPE html>
<html>
<head>
<style>
  body { font-family: var(--vscode-font-family); padding: 10px; font-size: 12px; color: var(--vscode-foreground); }
  .header { font-size: 14px; font-weight: bold; margin-bottom: 10px; }
  .task { border: 1px solid var(--vscode-panel-border); border-radius: 4px; padding: 8px; margin-bottom: 8px; }
  .task-state { font-weight: bold; }
  .state-QUEUED { color: var(--vscode-charts-blue); }
  .state-CLINE_STARTING, .state-CLINE_RUNNING { color: var(--vscode-charts-yellow); }
  .state-CLINE_SUCCEEDED { color: var(--vscode-charts-green); }
  .state-CLINE_FAILED, .state-CLINE_STALLED { color: var(--vscode-charts-red); }
  .state-CANCELLED { color: var(--vscode-disabledForeground); }
  button { margin-right: 5px; margin-top: 4px; }
  .info { color: var(--vscode-descriptionForeground); font-size: 11px; }
</style>
</head>
<body>
  <div class="header">GAUSS Agent Control Center</div>
  <div id="status">Connecting...</div>
  <div id="tasks"></div>
  <div style="margin-top:10px">
    <button onclick="submit()">+ New Task</button>
  </div>
<script>
const vscode = acquireVsCodeApi();
function submit() { vscode.postMessage({command:'submit'}); }
function cancel(id) { vscode.postMessage({command:'cancel', taskId:id}); }
function viewLog(id) { vscode.postMessage({command:'viewLog', taskId:id}); }

window.addEventListener('message', e => {
  const msg = e.data;
  if (msg.type === 'status') {
    const d = msg.data;
    document.getElementById('status').innerHTML =
      'Active: ' + d.active_count + ' / Total: ' + d.total_count;
    let html = '';
    (d.tasks || []).forEach(t => {
      html += '<div class="task">';
      html += '<div><span class="task-state state-' + t.state + '">' + t.state + '</span>';
      html += ' <span class="info">' + (t.id || '').slice(-16) + '</span></div>';
      html += '<div class="info">Retries: ' + (t.retry_count||0) + '/' + (t.max_retries||1) + '</div>';
      html += '<div class="info">' + (t.created_at || '') + '</div>';
      if (['CLINE_STARTING','CLINE_RUNNING'].includes(t.state)) {
        html += '<button onclick="cancel('' + t.id + '')">Cancel</button>';
      }
      if (t.log_stdout) {
        html += '<button onclick="viewLog('' + t.id + '')">Log</button>';
      }
      html += '</div>';
    });
    document.getElementById('tasks').innerHTML = html || '<div class="info">No tasks yet</div>';
  } else if (msg.type === 'error') {
    document.getElementById('status').innerHTML = '<span style="color:red">Disconnected</span>';
  }
});
</script>
</body></html>`;
    }

    dispose() {
        if (this.updateInterval) {
            clearInterval(this.updateInterval);
        }
        this.panel.dispose();
    }
}
