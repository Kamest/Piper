/* tslint:disable */
/* eslint-disable */
/**
 * The `ReadableStreamType` enum.
 *
 * *This API requires the following crate features to be activated: `ReadableStreamType`*
 */

export type ReadableStreamType = "bytes";

export class IntoUnderlyingByteSource {
    private constructor();
    free(): void;
    [Symbol.dispose](): void;
    cancel(): void;
    pull(controller: ReadableByteStreamController): Promise<any>;
    start(controller: ReadableByteStreamController): void;
    readonly autoAllocateChunkSize: number;
    readonly type: ReadableStreamType;
}

export class IntoUnderlyingSink {
    private constructor();
    free(): void;
    [Symbol.dispose](): void;
    abort(reason: any): Promise<any>;
    close(): Promise<any>;
    write(chunk: any): Promise<any>;
}

export class IntoUnderlyingSource {
    private constructor();
    free(): void;
    [Symbol.dispose](): void;
    cancel(): void;
    pull(controller: ReadableStreamDefaultController): Promise<any>;
}

export class MobileConnection {
    private constructor();
    free(): void;
    [Symbol.dispose](): void;
    close(): Promise<void>;
    /**
     * Explicit relay and pinned server; no DNS discovery or ambient public relay.
     */
    static connect(server: string, relay: string, secret: Uint8Array): Promise<MobileConnection>;
    open(): Promise<MobileStream>;
}

export class MobileStream {
    private constructor();
    free(): void;
    [Symbol.dispose](): void;
    cancel(): Promise<void>;
    finish(): Promise<void>;
    read(count: number): Promise<Uint8Array>;
    read_to_end(): Promise<Uint8Array>;
    write(data: Uint8Array): Promise<void>;
}

export type InitInput = RequestInfo | URL | Response | BufferSource | WebAssembly.Module;

export interface InitOutput {
    readonly memory: WebAssembly.Memory;
    readonly __wbg_mobileconnection_free: (a: number, b: number) => void;
    readonly __wbg_mobilestream_free: (a: number, b: number) => void;
    readonly mobileconnection_close: (a: number) => number;
    readonly mobileconnection_connect: (a: number, b: number, c: number, d: number, e: number, f: number) => number;
    readonly mobileconnection_open: (a: number) => number;
    readonly mobilestream_cancel: (a: number) => number;
    readonly mobilestream_finish: (a: number) => number;
    readonly mobilestream_read: (a: number, b: number) => number;
    readonly mobilestream_read_to_end: (a: number) => number;
    readonly mobilestream_write: (a: number, b: number, c: number) => number;
    readonly ring_core_0_17_14__bn_mul_mont: (a: number, b: number, c: number, d: number, e: number, f: number) => void;
    readonly __wbg_intounderlyingbytesource_free: (a: number, b: number) => void;
    readonly __wbg_intounderlyingsink_free: (a: number, b: number) => void;
    readonly __wbg_intounderlyingsource_free: (a: number, b: number) => void;
    readonly intounderlyingbytesource_autoAllocateChunkSize: (a: number) => number;
    readonly intounderlyingbytesource_cancel: (a: number) => void;
    readonly intounderlyingbytesource_pull: (a: number, b: number) => number;
    readonly intounderlyingbytesource_start: (a: number, b: number) => void;
    readonly intounderlyingbytesource_type: (a: number) => number;
    readonly intounderlyingsink_abort: (a: number, b: number) => number;
    readonly intounderlyingsink_close: (a: number) => number;
    readonly intounderlyingsink_write: (a: number, b: number) => number;
    readonly intounderlyingsource_cancel: (a: number) => void;
    readonly intounderlyingsource_pull: (a: number, b: number) => number;
    readonly __wasm_bindgen_func_elem_3411: (a: number, b: number, c: number, d: number) => void;
    readonly __wasm_bindgen_func_elem_3471: (a: number, b: number, c: number, d: number) => void;
    readonly __wasm_bindgen_func_elem_2807: (a: number, b: number, c: number) => void;
    readonly __wasm_bindgen_func_elem_2807_2: (a: number, b: number, c: number) => void;
    readonly __wasm_bindgen_func_elem_2807_3: (a: number, b: number, c: number) => void;
    readonly __wasm_bindgen_func_elem_1497: (a: number, b: number) => void;
    readonly __wasm_bindgen_func_elem_4593: (a: number, b: number) => void;
    readonly __wbindgen_export: (a: number, b: number) => number;
    readonly __wbindgen_export2: (a: number, b: number, c: number, d: number) => number;
    readonly __wbindgen_export3: (a: number) => void;
    readonly __wbindgen_export4: (a: number, b: number) => void;
    readonly __wbindgen_export5: (a: number, b: number, c: number) => void;
    readonly __wbindgen_add_to_stack_pointer: (a: number) => number;
}

export type SyncInitInput = BufferSource | WebAssembly.Module;

/**
 * Instantiates the given `module`, which can either be bytes or
 * a precompiled `WebAssembly.Module`.
 *
 * @param {{ module: SyncInitInput }} module - Passing `SyncInitInput` directly is deprecated.
 *
 * @returns {InitOutput}
 */
export function initSync(module: { module: SyncInitInput } | SyncInitInput): InitOutput;

/**
 * If `module_or_path` is {RequestInfo} or {URL}, makes a request and
 * for everything else, calls `WebAssembly.instantiate` directly.
 *
 * @param {{ module_or_path: InitInput | Promise<InitInput> }} module_or_path - Passing `InitInput` directly is deprecated.
 *
 * @returns {Promise<InitOutput>}
 */
export default function __wbg_init (module_or_path?: { module_or_path: InitInput | Promise<InitInput> } | InitInput | Promise<InitInput>): Promise<InitOutput>;
