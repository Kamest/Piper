/** Halo navigation: browser-local pins, independent of module availability and session pins. */
export const NAV_TOOLS = ['inbox', 'prod', 'db', 'http', 'host', 'usage', 'time', 'audit', 'stacks', 'notes', 'keys'];
export const TOOL_PINS_KEY = 'piper.navigation.pins';
const DEFAULT_PINS = ['inbox', 'db', 'host', 'stacks', 'notes'];
/** pinnable since the stored `{pins}` object: a bare array was saved before they could be unpinned, so they stay */
const PINNED_BEFORE = ['inbox'];
/** `{pins: [...]}`; a bare array is the older format (the Inbox was always on the rail then) */
export function parseToolPins(raw) {
    try {
        const value = JSON.parse(raw ?? 'null');
        if (Array.isArray(value))
            return NAV_TOOLS.filter((id) => value.includes(id) || PINNED_BEFORE.includes(id));
        const pins = value && typeof value === 'object' ? value.pins : undefined;
        return Array.isArray(pins) ? NAV_TOOLS.filter((id) => pins.includes(id)) : [...DEFAULT_PINS];
    }
    catch {
        return [...DEFAULT_PINS];
    }
}
export function serializeToolPins(pins) {
    return JSON.stringify({ pins });
}
/** Canonical order stays stable when tools are pinned, disabled and re-enabled. */
export function toggleToolPin(pins, id, on) {
    return NAV_TOOLS.filter((key) => key === id ? on : pins.includes(key));
}
export function hiddenToolState(tools, pins) {
    const hidden = tools.filter((t) => !pins.includes(t.id));
    return { active: hidden.find((t) => t.active)?.id ?? null,
        danger: hidden.some((t) => t.active && t.danger), count: hidden.reduce((n, t) => n + (t.count ?? 0), 0) };
}
//# sourceMappingURL=navigation.js.map