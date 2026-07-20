import * as vscode from 'vscode';
import { GaussClient } from './client';
import { OverviewPanel } from './panels/overview';

let client: GaussClient;
let overviewPanel: OverviewPanel | undefined;
let statusBar: vscode.StatusBarItem;

export function activate(context: vscode.ExtensionContext) {
    const host = vscode.workspace.getConfiguration('gauss-agent').get('host', '127.0.0.1');
    const port = vscode.workspace.getConfiguration('gauss-agent').get('port', 19876);

    client = new GaussClient(host, port);

    // Status bar
    statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusBar.text = '$(pulse) GAUSS Agent';
    statusBar.command = 'gauss-agent.showOverview';
    statusBar.show();
    context.subscriptions.push(statusBar);

    // Commands
    context.subscriptions.push(
        vscode.commands.registerCommand('gauss-agent.submitTask', async () => {
            const prompt = await vscode.window.showInputBox({
                prompt: 'Enter task prompt',
                placeHolder: 'Describe what Cline should do...'
            });
            if (prompt) {
                try {
                    const result = await client.submitTask(prompt);
                    vscode.window.showInformationMessage(
                        `Task submitted: ${result.task_id}`
                    );
                    overviewPanel?.refresh();
                } catch (e: any) {
                    vscode.window.showErrorMessage(`Failed: ${e.message}`);
                }
            }
        }),
        vscode.commands.registerCommand('gauss-agent.showOverview', () => {
            if (overviewPanel) {
                overviewPanel.reveal();
            } else {
                overviewPanel = new OverviewPanel(context, client);
            }
        }),
        vscode.commands.registerCommand('gauss-agent.cancelTask', async () => {
            const status = await client.getStatus();
            const tasks = status.tasks || [];
            const active = tasks.filter((t: any) =>
                !['CLINE_SUCCEEDED','CLINE_FAILED','CLINE_STALLED','CANCELLED'].includes(t.state)
            );
            if (active.length === 0) {
                vscode.window.showInformationMessage('No active tasks to cancel.');
                return;
            }
            const items: vscode.QuickPickItem[] = active.map((t: any) => ({
                label: `${t.id} (${t.state})`,
                description: t.id
            }));
            const pick = await vscode.window.showQuickPick(items, {
                placeHolder: 'Select task to cancel'
            });
            if (pick && pick.description) {
                await client.cancelTask(pick.description);
                vscode.window.showInformationMessage(`Cancelled: ${pick.description}`);
                overviewPanel?.refresh();
            }
        }),
        vscode.commands.registerCommand('gauss-agent.refreshStatus', async () => {
            try {
                const status = await client.getStatus();
                const tasks = status.tasks || [];
                const active = tasks.filter((t: any) =>
                    ['CLINE_STARTING','CLINE_RUNNING'].includes(t.state)
                );
                statusBar.text = active.length > 0
                    ? `$(sync~spin) GAUSS Agent (${active.length} running)`
                    : `$(check) GAUSS Agent`;
                overviewPanel?.refresh();
            } catch {
                statusBar.text = `$(error) GAUSS Agent (disconnected)`;
            }
        })
    );

    // Auto-refresh every 5 seconds
    const interval = setInterval(() => {
        vscode.commands.executeCommand('gauss-agent.refreshStatus');
    }, 5000);
    context.subscriptions.push({ dispose: () => clearInterval(interval) });

    // Initial refresh
    vscode.commands.executeCommand('gauss-agent.refreshStatus');

    vscode.window.showInformationMessage('GAUSS Agent Control Center activated');
}

export function deactivate() {
    if (client) {
        client.close();
    }
    if (overviewPanel) {
        overviewPanel.dispose();
    }
}
