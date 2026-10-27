class WorkerConnection {
    signal;
    worker = new Worker(new URL('./mobileworker.js', import.meta.url), { type: 'module' });
    pending = new Map();
    serial = 0;
    ended = false;
    abort = () => this.stop(this.signal.reason ?? new Error('Mobile connection replaced'));
    constructor(signal) {
        this.signal = signal;
        this.worker.onmessage = ({ data }) => {
            if (this.ended)
                return;
            const slot = this.pending.get(data.id);
            if (!slot)
                return;
            this.pending.delete(data.id);
            if (data.ok)
                slot.resolve(data.value);
            else
                slot.reject(new Error('Mobile transport operation failed'));
        };
        this.worker.onerror = () => this.stop(new Error('Mobile transport stopped'));
        this.worker.onmessageerror = () => this.stop(new Error('Invalid mobile transport response'));
        signal.addEventListener('abort', this.abort, { once: true });
        if (signal.aborted)
            this.abort();
    }
    stop(reason) {
        if (this.ended)
            return;
        this.ended = true;
        this.signal.removeEventListener('abort', this.abort);
        this.worker.terminate();
        for (const slot of this.pending.values())
            slot.reject(reason);
        this.pending.clear();
    }
    rpc(request, transfer = []) {
        if (this.ended)
            return Promise.reject(new Error('Mobile connection ended'));
        if (this.pending.size >= 256) {
            this.stop(new Error('Mobile transport queue full'));
            return Promise.reject(new Error('Mobile transport queue full'));
        }
        const id = ++this.serial;
        return new Promise((resolve, reject) => {
            this.pending.set(id, { resolve, reject });
            try {
                this.worker.postMessage({ ...request, id }, transfer);
            }
            catch {
                this.stop(new Error('Mobile transport unavailable'));
            }
        });
    }
    async connect(endpoint, relay, key) {
        const bytes = new Uint8Array(key);
        await this.rpc({ op: 'connect', endpoint, relay, key: bytes }, [bytes.buffer]);
    }
    async open() {
        const stream = await this.rpc({ op: 'open' });
        if (typeof stream !== 'number')
            throw new Error('Invalid mobile stream');
        const invoke = (op) => this.rpc({ op, stream });
        return {
            write: async (data) => {
                const bytes = new Uint8Array(data);
                await this.rpc({ op: 'write', stream, data: bytes }, [bytes.buffer]);
            },
            read: async (count) => {
                const value = await this.rpc({ op: 'read', stream, count });
                if (!(value instanceof Uint8Array))
                    throw new Error('Invalid mobile response');
                return value;
            },
            read_to_end: async () => {
                const value = await invoke('read_to_end');
                if (!(value instanceof Uint8Array))
                    throw new Error('Invalid mobile response');
                return value;
            },
            finish: async () => { await invoke('finish'); },
            cancel: async () => { if (!this.ended)
                await invoke('cancel'); },
        };
    }
    async close() { this.stop(new Error('Mobile connection closed')); }
}
/** Abort owns the worker lifetime, including initialization, dial and every pending stream RPC. */
export async function createMobileConnection(endpoint, relay, key, signal) {
    signal.throwIfAborted();
    const connection = new WorkerConnection(signal);
    try {
        await connection.connect(endpoint, relay, key);
        signal.throwIfAborted();
        return connection;
    }
    catch (error) {
        await connection.close();
        throw error;
    }
}
//# sourceMappingURL=mobileworkerclient.js.map