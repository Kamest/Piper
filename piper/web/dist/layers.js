// W11: dialogs are layers (IntelliJ: a dialog opened from a dialog is its child, the parent stays below).
// modal() replaces the top layer (a confirmation that takes over its dialog, as before), modalOver() puts
// a new layer on top - the one below stays visible, dimmed and inert. closeModal() takes only the top
// layer away and gives the focus back to what opened it; closeAllModals() is for leaving (another session,
// a file, a link). The stack itself is pure (node --test); main.ts owns the DOM of #overlay.
export class LayerStack {
    items = [];
    get depth() {
        return this.items.length;
    }
    top() {
        return this.items[this.items.length - 1]?.layer;
    }
    /** the layers bottom first */
    all() {
        return this.items.map((x) => x.layer);
    }
    /** over: a new layer on top (`back` = the opener); else the top is replaced and keeps its opener.
     *  Returns the replaced layer (to remove from the DOM), if any. */
    open(layer, back, over) {
        if (over || !this.items.length) {
            this.items.push({ layer, back });
            return undefined;
        }
        const t = this.items[this.items.length - 1];
        const old = t.layer;
        t.layer = layer;
        return old;
    }
    /** the top layer goes; its opener gets the focus */
    pop() {
        return this.items.pop();
    }
    /** every layer goes, top first; the bottom one's opener is where the focus returns */
    clear() {
        const out = this.items.reverse();
        this.items = [];
        return out;
    }
}
/** a dialog that redraws itself through `modal` (History, Push): its first draw is a new layer, the later
 *  ones replace that layer instead of piling more on */
export function firstOver(over, replace) {
    let first = true;
    return (...a) => {
        if (first) {
            first = false;
            over(...a);
        }
        else
            replace(...a);
    };
}
//# sourceMappingURL=layers.js.map