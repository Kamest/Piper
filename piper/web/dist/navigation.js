/** Halo navigation: browser-local pins, independent of module availability and session pins. */
export const NAV_TOOLS = ['prod', 'db', 'host', 'usage', 'time', 'audit', 'stacks', 'keys'];
export const TOOL_PINS_KEY = 'piper.navigation.pins';
const DEFAULT_PINS = ['db', 'host'];
export function parseToolPins(raw) {
    try {
        const value = JSON.parse(raw ?? 'null');
        return Array.isArray(value) ? NAV_TOOLS.filter((id) => value.includes(id)) : [...DEFAULT_PINS];
    }
    catch {
        return [...DEFAULT_PINS];
    }
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