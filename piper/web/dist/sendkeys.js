// W13: two send classes, one helper. A chat field (a short answer: the queue reply, the
// Conversation bar, the Feed) sends with ⏎ and breaks a line with ⇧⏎; a document field (the composer, a new
// session's task, a review prompt, a line note, a GitLab comment, the inbox Resume task) sends with ⌘⏎ and
// breaks a line with ⏎. ⌘⏎ sends everywhere (IntelliJ: AI chat vs. the commit dialog). The hint and the key
// test come from here, so they cannot disagree; one notation: ⏎ ⇧⏎ ⌘⏎.
export function sendHint(mode) {
    return mode === 'chat' ? '⏎ sends · ⇧⏎ new line' : '⌘⏎ sends · ⏎ new line';
}
/** does this keydown send? (never while an IME composes; Ctrl stands for ⌘ off a Mac) */
export function isSend(e, mode) {
    if (e.key !== 'Enter' || e.isComposing)
        return false;
    if (e.metaKey || e.ctrlKey)
        return !e.altKey;
    return mode === 'chat' && !e.shiftKey && !e.altKey;
}
//# sourceMappingURL=sendkeys.js.map