// Before the rename Piper was "the hole orchestrator" and kept its browser state under `orch.*`
// (token, rail order, pins, composer templates, ...). Copied once to `piper.*` - imported first
// by main.ts, so every module reads the new keys already filled.
try {
    const ls = window.localStorage;
    const old = Array.from({ length: ls.length }, (_, i) => ls.key(i)).filter((k) => !!k?.startsWith('orch.'));
    for (const k of old) {
        if (ls.getItem('piper.' + k.slice(5)) === null) {
            ls.setItem('piper.' + k.slice(5), ls.getItem(k) ?? '');
        }
    }
}
catch { /* no storage (private window): nothing to carry over */ }
export {};
//# sourceMappingURL=legacy.js.map