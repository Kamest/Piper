// Piper's CodeMirror 6 bundle: one ES module with what files.ts needs.
export { EditorState, StateField, StateEffect, Compartment, RangeSetBuilder, RangeSet, Prec, Transaction, EditorSelection, Text, Facet } from '@codemirror/state';
export type { Extension, Range, ChangeSpec, SelectionRange } from '@codemirror/state';
export { EditorView, Decoration, ViewPlugin, keymap, lineNumbers, highlightActiveLine, highlightActiveLineGutter, drawSelection, dropCursor,
  rectangularSelection, crosshairCursor, highlightSpecialChars, gutter, GutterMarker, WidgetType, showPanel, hoverTooltip, showTooltip, scrollPastEnd } from '@codemirror/view';
export type { DecorationSet, ViewUpdate, KeyBinding, Panel, Tooltip, PluginValue } from '@codemirror/view';
export { defaultKeymap, history, historyKeymap, indentWithTab, toggleComment, selectAll, undo, redo, moveLineUp, moveLineDown, copyLineDown, deleteLine,
  insertNewlineAndIndent, indentMore, indentLess, cursorDocStart } from '@codemirror/commands';
export { syntaxHighlighting, HighlightStyle, bracketMatching, foldGutter, foldKeymap, codeFolding, foldService, foldEffect, unfoldEffect, foldAll, unfoldAll,
  foldedRanges, foldable, indentOnInput, indentUnit, StreamLanguage, syntaxTree, LanguageSupport, ensureSyntaxTree } from '@codemirror/language';
export { search, searchKeymap, highlightSelectionMatches, selectNextOccurrence, SearchQuery, setSearchQuery, getSearchQuery, findNext, findPrevious,
  openSearchPanel, closeSearchPanel, searchPanelOpen } from '@codemirror/search';
export { linter, lintGutter, setDiagnostics, forEachDiagnostic } from '@codemirror/lint';
export type { Diagnostic } from '@codemirror/lint';
export { closeBrackets, closeBracketsKeymap } from '@codemirror/autocomplete';
export { tags } from '@lezer/highlight';
export { java } from '@codemirror/lang-java';
export { javascript } from '@codemirror/lang-javascript';
export { xml } from '@codemirror/lang-xml';
export { sql, MySQL, PostgreSQL } from '@codemirror/lang-sql';
export { yaml } from '@codemirror/lang-yaml';
export { markdown } from '@codemirror/lang-markdown';
export { json } from '@codemirror/lang-json';
export { python } from '@codemirror/lang-python';
export { html } from '@codemirror/lang-html';
export { css } from '@codemirror/lang-css';
export { properties } from '@codemirror/legacy-modes/mode/properties';
export { shell } from '@codemirror/legacy-modes/mode/shell';
export { groovy } from '@codemirror/legacy-modes/mode/groovy';
export { kotlin, scala } from '@codemirror/legacy-modes/mode/clike';
export { go } from '@codemirror/legacy-modes/mode/go';
export { toml } from '@codemirror/legacy-modes/mode/toml';
export { dockerFile } from '@codemirror/legacy-modes/mode/dockerfile';
export { diff } from '@codemirror/legacy-modes/mode/diff';
export { unifiedMergeView, MergeView, goToNextChunk, goToPreviousChunk } from '@codemirror/merge';
