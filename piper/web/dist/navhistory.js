// Back / forward through the GUI (policy nav_history): one history of the places the user moved to -
// a session, its view, a file and line - walked by the mouse's side buttons and ⌘[ / ⌘] outside Files.
/** the same place: a file a few lines apart is still the same stop (a click, not a jump) */
export function samePlace(a, b) {
    if (a.session !== b.session || a.view !== b.view)
        return false;
    if (!a.file || !b.file)
        return !a.file && !b.file;
    return a.file.project === b.file.project && a.file.path === b.file.path && Math.abs(a.file.line - b.file.line) < 3;
}
export class NavHistory {
    max;
    back = [];
    fwd = [];
    cur = null;
    constructor(max = 50) {
        this.max = max;
    }
    /** the user arrived at `p`; a new stop clears forward (true), the same stop only refreshes it (false) */
    visit(p) {
        if (this.cur && samePlace(this.cur, p)) {
            this.cur = p;
            return false;
        }
        if (this.cur) {
            this.back.push(this.cur);
            if (this.back.length > this.max)
                this.back.shift();
        }
        this.fwd = [];
        this.cur = p;
        return true;
    }
    goBack() {
        const p = this.back.pop();
        if (!p)
            return null;
        if (this.cur)
            this.fwd.push(this.cur);
        this.cur = p;
        return p;
    }
    goForward() {
        const p = this.fwd.pop();
        if (!p)
            return null;
        if (this.cur)
            this.back.push(this.cur);
        this.cur = p;
        return p;
    }
    /** sessions that went away take their stops with them */
    prune(alive) {
        this.back = this.back.filter((p) => alive(p.session));
        this.fwd = this.fwd.filter((p) => alive(p.session));
    }
}
//# sourceMappingURL=navhistory.js.map