/** Independent GUI/editor/terminal preferences. Missing or invalid policy keeps the product defaults. */
const SIZES = {
  ui_font_size: { default: 13, choices: ['12', '13', '14', '15', '16'] },
  editor_font_size: { default: 12.5, choices: ['11', '12', '12.5', '14', '16', '18'] },
  term_font_size: { default: 13.5, choices: ['12', '13.5', '15', '16', '18', '20'] },
};
export type FontSizeKey = keyof typeof SIZES;
export function isFontSetting(key: string): key is FontSizeKey { return Object.hasOwn(SIZES, key); }
export function fontSize(key: FontSizeKey, value: unknown): number {
  return typeof value === 'string' && SIZES[key].choices.includes(value) ? Number(value) : SIZES[key].default;
}

const editorListeners = new Set<() => void>();
let editorSize = SIZES.editor_font_size.default;
/** A live CodeMirror/merge view measures its new glyphs without replacing document or selection. */
export function onEditorFontSize(measure: () => void): () => void {
  editorListeners.add(measure);
  return () => { editorListeners.delete(measure); };
}

export function applyFontSizes(flags?: { ui_font_size?: string; editor_font_size?: string }): void {
  const ui = fontSize('ui_font_size', flags?.ui_font_size);
  const editor = fontSize('editor_font_size', flags?.editor_font_size);
  const style = document.documentElement.style;
  if (ui === SIZES.ui_font_size.default) style.removeProperty('--ui-scale');
  else style.setProperty('--ui-scale', String(ui / SIZES.ui_font_size.default));
  if (editor === SIZES.editor_font_size.default) {
    style.removeProperty('--editor-fs');
    style.removeProperty('--editor-fs-ratio');
  } else {
    style.setProperty('--editor-fs', `${editor}px`);
    style.setProperty('--editor-fs-ratio', String(editor / SIZES.editor_font_size.default));
  }
  if (editor !== editorSize) {
    editorSize = editor;
    editorListeners.forEach(measure => measure());
  }
}
