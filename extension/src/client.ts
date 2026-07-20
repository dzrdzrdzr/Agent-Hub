import * as net from 'net';

export class GaussClient {
    private host: string;
    private port: number;
    private socket: net.Socket | null = null;
    private buffer: string = '';
    private reqId: number = 0;
    private pending: Map<string, { resolve: Function; reject: Function }> = new Map();

    constructor(host: string = '127.0.0.1', port: number = 19876) {
        this.host = host;
        this.port = port;
    }

    private async connect(): Promise<net.Socket> {
        if (this.socket && !this.socket.destroyed) {
            return this.socket;
        }
        return new Promise((resolve, reject) => {
            this.socket = new net.Socket();
            this.socket.connect(this.port, this.host, () => {
                resolve(this.socket!);
            });
            this.socket.on('error', (err) => {
                reject(err);
            });
            this.socket.on('data', (data: Buffer) => {
                this.buffer += data.toString('utf-8');
                const lines = this.buffer.split('\n');
                this.buffer = lines.pop() || '';
                for (const line of lines) {
                    if (!line.trim()) continue;
                    try {
                        const msg = JSON.parse(line);
                        if (msg.type === 'response' && msg.id) {
                            const pending = this.pending.get(msg.id);
                            if (pending) {
                                this.pending.delete(msg.id);
                                if (msg.error) {
                                    pending.reject(new Error(msg.error));
                                } else {
                                    pending.resolve(msg.result);
                                }
                            }
                        }
                    } catch {}
                }
            });
        });
    }

    private async request(method: string, params: any = {}): Promise<any> {
        const sock = await this.connect();
        const id = `req-${++this.reqId}`;
        return new Promise((resolve, reject) => {
            this.pending.set(id, { resolve, reject });
            const msg = JSON.stringify({ type: 'request', id, method, params }) + '\n';
            sock.write(msg);
            // Timeout after 30s
            setTimeout(() => {
                if (this.pending.has(id)) {
                    this.pending.delete(id);
                    reject(new Error(`Request ${method} timed out`));
                }
            }, 30000);
        });
    }

    async submitTask(prompt: string): Promise<any> {
        return this.request('submit_task', { prompt, task_type: 'cline_exec' });
    }

    async getStatus(): Promise<any> {
        return this.request('get_status', {});
    }

    async getTask(taskId: string): Promise<any> {
        return this.request('get_task', { task_id: taskId });
    }

    async cancelTask(taskId: string): Promise<any> {
        return this.request('cancel_task', { task_id: taskId });
    }

    async getLogTail(taskId: string, lines: number = 50): Promise<any> {
        return this.request('get_log_tail', { task_id: taskId, lines });
    }

    async ping(): Promise<any> {
        return this.request('ping', {});
    }

    close() {
        if (this.socket) {
            this.socket.destroy();
            this.socket = null;
        }
    }
}
