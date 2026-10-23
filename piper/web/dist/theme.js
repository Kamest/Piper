// Colours come from Piper's own web/themes.json (embedded in the boot blob).
// Piper defaults to the dark theme (terminal first,
// 9.5) and follows the OS only when the dark/light pair differs from the default.
export function applyTheme(set, prefer = 'dark') {
    const sys = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
    const scheme = prefer === 'auto' ? sys : prefer;
    const name = set?.auto?.[scheme] ?? scheme;
    const theme = set?.themes?.[name];
    const root = document.documentElement;
    root.dataset['scheme'] = theme?.scheme ?? scheme;
    if (theme)
        for (const [k, v] of Object.entries(theme.tokens))
            root.style.setProperty('--' + k, v);
    return theme?.scheme ?? scheme;
}
/** Indigo's key in Piper's themes file */
const INDIGO = 'indigo';
/** N1: the Settings › Interface choice (policy theme): auto = the OS's light / dark, a name = that theme -
 *  "indigo" (a light theme of its own) stands for both schemes */
export function applyChoice(set, choice) {
    if (choice === 'indigo' && set?.themes?.[INDIGO])
        return applyTheme({ ...set, auto: { dark: INDIGO, light: INDIGO } }, 'light');
    return applyTheme(set, choice === 'auto' || choice === 'light' ? choice : 'dark');
}
/** xterm.js colours derived from the same tokens */
export function terminalTheme() {
    const css = getComputedStyle(document.documentElement);
    const v = (n, d) => css.getPropertyValue('--' + n).trim() || d;
    return {
        background: v('well', '#111214'),
        foreground: v('fg', '#ececed'),
        cursor: v('fg', '#ececed'),
        cursorAccent: v('well', '#111214'),
        selectionBackground: v('neutral-bg', '#2a2b2f'),
    };
}
//# sourceMappingURL=theme.js.map