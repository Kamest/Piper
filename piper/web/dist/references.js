const EXT = /\.(?:[cm]?[jt]sx?|java|kt|kts|py|go|rs|rb|php|c|h|cpp|hpp|cs|swift|scala|sh|bash|zsh|fish|sql|http|rest|json|jsonl|ya?ml|xml|toml|ini|cfg|conf|properties|env|lock|md|txt|log|csv|tsv|html?|css|scss|vue|svelte|gradle|patch|diff|png|jpe?g|gif|webp|svg|ico|bmp|pdf|zip|gz|xlsx?|docx?|pptx?)$/i;
/** An explicit repository wins; otherwise a linked issue identifies its own number, never a same-number MR. */
export function referenceProjects(ref, session) {
    if (ref.project)
        return [ref.project];
    const issue = session.issue;
    if (issue && !issue.gl.startsWith('gh:') && String(issue.iid) === ref.id
        && (issue.type === 'mr' ? 'merge_requests' : 'issues') === ref.resource)
        return [issue.gl];
    return [...new Set([...(session.gitlab ?? []), ...(session.gitlab_extra ?? [])])];
}
/** TUI word wrapping emits real line breaks after '-' and '/', unlike xterm's soft wraps.
 * Keep displayed offsets while joining only a continuous reference token. Ordinary newlines,
 * paragraphs and independently rooted references retain their boundaries. */
function terminalText(text, fullWidthBreaks) {
    const chars = [], offsets = [], tentative = new Set();
    for (let i = 0; i < text.length; i++) {
        if (text[i] === '\n') {
            let end = chars.length, next = i + 1;
            while (end && /[ \t\r]/.test(chars[end - 1]))
                end--;
            while (/[ \t]/.test(text[next] ?? '') && next - i <= 17)
                next++;
            let start = end;
            while (start && /[^\s"'`<>()]/.test(chars[start - 1]))
                start--;
            const left = chars.slice(start, end).join('');
            const right = /^[^\s"'`<>()]+/.exec(text.slice(next))?.[0] ?? '';
            // A standalone issue/MR starts a new reference even when the preceding row filled the terminal.
            const separate = references(right).some(hit => hit.ref.kind === 'gitlab' && hit.start === 0
                && /^[.,;!?]*$/.test(right.slice(hit.end)));
            const url = /^(?:https?|file):\/\//i.test(left);
            // wrap-ansi cuts only a word longer than its row: a word (with its quotes and brackets) that fits the content
            // of this full row was wrapped whole, so "… in" | "src/b.ts" and "config.js" | "on" are two words
            let word = end;
            while (word && /\S/.test(chars[word - 1]))
                word--;
            const row = text.slice(text.lastIndexOf('\n', i - 1) + 1, i).trimEnd();
            const nextWord = /^\S+/.exec(text.slice(next))?.[0] ?? '';
            const indent = next - i - 1, width = row.length - indent;
            const goesOn = text[next + nextWord.length] === '\n' && fullWidthBreaks.has(next + nextWord.length);
            // the word starts at its first character, or after a tentative join (the prose word was not part of it)
            const full = fullWidthBreaks.has(i) && [word, ...[...tentative].filter(t => t > word && t < end)].some(from => {
                const joined = end - from + nextWord.length;
                return joined > width && (goesOn || cutFits(text, offsets[from] ?? i, indent, width, joined));
            });
            const candidate = (left + right).replace(/[.,;:!?]+$/, '');
            // "(https" | "://example.com/…", "https:" | "//…": the cut fell inside the scheme
            const scheme = !url && full && /^(?:https?|file):\/\/[^\s/]/i.test(candidate);
            const link = url || scheme;
            const punctuation = /[-/]$/.test(left) || link && /[?&=#%]$/.test(left);
            const continuous = link || references(candidate).some(hit => hit.ref.kind === 'file' && hit.start === 0 && hit.end === candidate.length);
            const bare = left.replace(/[.,;:!?]+$/, '');
            // a single word that adds no folder, dot or line: "Do", "and", "Next" (wrap-ansi cuts a long path anywhere)
            const plain = /^[\p{L}\p{N}_-]+(?::\d+(?::\d+)?)?[.,;:!?]*$/u.test(right)
                && !(text[next + right.length] === '\n' && fullWidthBreaks.has(next + right.length)); // a cut word goes on
            // prose, never the rest of a link: a capitalised word or one with letters a URL does not carry unencoded
            const prose = /[^\x00-\x7f]/.test(right) || plain && /^\p{Lu}/u.test(right);
            // "(…/term.ts" | ":706)": a line number on the next row belongs to the name before it
            const lineTail = /^:\d+(?::\d+)?[.,;:!?]*$/.test(right) && EXT.test(left);
            // "…/AddressForm.ts" | "x:12:5": the cut fell inside a longer extension
            const extCut = full && !link && plain && bare === left && EXT.test(left) && /^[\p{L}\p{N}]/u.test(right)
                && EXT.test(candidate.replace(/:\d+(?::\d+)?$/, ''));
            // A row cut right before '/' ("…/acme/shop" | "/development/…") continues the path, never a new absolute one.
            const rooted = /^(?:\.{0,2}\/|~\/|(?:https?|file):\/\/)/i.test(right)
                && !(scheme || full && /^\/(?!\/)/.test(right) && bare === left && !EXT.test(left));
            // A CLI breaks a word longer than its width inside the first segment ("dev" | "elopment/…"). A prose word
            // that merely ends at the edge looks the same, so that join stays tentative: the right part is the fallback.
            const midWord = full && !link && !left.includes('/') && right.includes('/') && !EXT.test(left) && bare === left;
            // Ending punctuation is a sentence ("…/stav.md." | "Do", "…/session/." | "src/b.ts") unless the right part only
            // completes the name: a cut extension ("src/app." | "ts:4"), line ("term.ts:" | "706") or port, a dotted host.
            const completes = /:$/.test(left) && /^\d+(?::\d+)?[.,;:!?]*$/.test(right) && (link || EXT.test(bare))
                || !EXT.test(bare) && (plain ? !link && EXT.test(candidate.replace(/:\d+(?::\d+)?$/, ''))
                    : right.includes('/') && /[\p{L}\p{N}_-]\.$/u.test(left) && !(link && /:\/\/[^/]+\//.test(left)));
            const sentence = !scheme && (link ? /[.,;:!]$/ : /[.,;:!?]$/).test(left) && !completes;
            // After '/' or '-' a lone word can start the next sentence ("…/session/" | "Next").
            const lone = link ? prose : /[-/]$/.test(left) && plain && !/^(?:Dockerfile|Makefile|README|LICENSE)\b/.test(right);
            if (left && right && !separate && !sentence && !lone && next - i <= 17 && !rooted
                && (punctuation || lineTail || extCut || full && (link ? !(plain && EXT.test(left)) : left.includes('/') && !EXT.test(left))
                    || midWord) && continuous) {
                chars.length = end;
                offsets.length = end;
                if (midWord && !punctuation)
                    tentative.add(end);
                i = next - 1;
                continue;
            }
        }
        chars.push(text[i]);
        offsets.push(i);
    }
    return { value: chars.join(''), offsets, tentative };
}
/** Could wrap-ansi `{hard: true}` have laid out one word of `length` (> `width`) starting at text offset `start`?
 * A long word starts on its row only when that costs no extra break; otherwise it moves to the next row. So a word
 * sitting alone at a continuation row's start after a short row, or one begun mid-row where moving was cheaper,
 * was a different, shorter word: "…app:" | "https://…/4321" | "viz" never was "https://…/4321viz". */
function cutFits(text, start, indent, width, length) {
    const rowStart = text.lastIndexOf('\n', start - 1) + 1, before = text.slice(rowStart, start);
    const breaks = (used) => {
        const thisLine = 1 + Math.floor((length - (width - used) - 1) / width), nextLine = Math.floor((length - 1) / width);
        return nextLine < thisLine; // true: wrap-ansi moves the word to the next row
    };
    if (before.length > indent)
        return !breaks(before.length - indent); // begun mid-row: it stayed there
    if (!rowStart || before.length !== indent || /\S/.test(before))
        return true; // first row of a paragraph or item
    const previous = text.slice(text.lastIndexOf('\n', rowStart - 2) + 1, rowStart - 1).trimEnd();
    if (previous.length <= indent)
        return true;
    return breaks(previous.length - indent + 1); // at a continuation row's start: it was moved off the row above
}
/** Terminal providers include whole soft wraps and bounded, explicit TUI hard-wrap continuations. */
export function terminalReferences(text, fullWidthBreaks = new Set()) {
    const { value, offsets, tentative } = terminalText(text, fullWidthBreaks);
    const out = references(value).map(hit => {
        const join = [...tentative].find(j => j > hit.start && j < hit.end);
        if (hit.ref.kind !== 'file' || join === undefined || !hit.text.startsWith(hit.ref.path))
            return hit;
        return { ...hit, ref: { ...hit.ref, fallback: hit.ref.path.slice(join - hit.start) } };
    });
    for (const hit of value.matchAll(/(?:https?|file):\/\/[^\s<>"`]+/gi)) {
        let raw = hit[0].replace(/[.,;:!?]+$/, '');
        // "(…/4321)." and "(…/4321.)": an unbalanced closing parenthesis and the punctuation around it end the link
        for (let prev = ''; prev !== raw;) {
            prev = raw;
            raw = raw.replace(/[.,;:!?]+$/, '');
            if (raw.endsWith(')') && (raw.match(/\)/g)?.length ?? 0) > (raw.match(/\(/g)?.length ?? 0))
                raw = raw.slice(0, -1);
        }
        try {
            const url = new URL(raw);
            if (url.username || url.password)
                continue;
            const ref = url.protocol === 'file:' ? fileUrlReference(raw) : { kind: 'url', url: raw };
            out.push({ start: hit.index, end: hit.index + raw.length, text: raw, ref });
        }
        catch { /* An invalid or non-local file URL never becomes a guessed local path. */ }
    }
    return out.map(hit => ({ ...hit, start: offsets[hit.start], end: offsets[hit.end - 1] + 1 })).sort((a, b) => a.start - b.start);
}
/** Human references may prefix a file with a registered project key or checkout folder name.
 * Callers first check the emitting checkout and filter projects to the same host. */
export function projectFileTargets(path, projects) {
    if (/^(?:\/|\.{1,2}\/|~\/)/.test(path))
        return [];
    const found = [];
    for (const project of projects) {
        const root = project.path.replace(/\/$/, '');
        for (const prefix of new Set([project.key, root.split('/').at(-1)])) {
            if (!prefix || !path.startsWith(prefix + '/'))
                continue;
            const relative = relativeFile(path.slice(prefix.length + 1), root);
            if (relative !== null)
                found.push({ project: project.key, path: root + '/' + relative, prefix: prefix.length });
        }
    }
    const longest = Math.max(0, ...found.map(x => x.prefix));
    return [...new Map(found.filter(x => x.prefix === longest).map(({ project, path }) => [path, { project, path }])).values()];
}
export function references(text) {
    const out = [];
    const urls = [...text.matchAll(/(?:https?|file|piper):\/\/[^\s<>"`]+/gi)].map(m => [m.index, m.index + m[0].length]);
    const add = (start, value, ref) => {
        const end = start + value.length;
        if ([...urls, ...out.map(x => [x.start, x.end])].some(([a, b]) => start < b && end > a))
            return;
        out.push({ start, end, text: value, ref });
    };
    for (const m of text.matchAll(/\b(issue|MR|merge request|pipeline)\s+([#!]?\d+)\b/gi)) {
        add(m.index, m[0], { kind: 'gitlab', resource: /^pipeline$/i.test(m[1]) ? 'pipelines' : /^(MR|merge request)$/i.test(m[1]) ? 'merge_requests' : 'issues', id: m[2].replace(/^[#!]/, '') });
    }
    for (const m of text.matchAll(/(?:^|[\s(`])((?:[\w.-]+\/[\w./-]+)?([#!])(\d+))\b/g)) {
        const raw = m[1], project = raw.slice(0, raw.lastIndexOf(m[2]));
        add(m.index + m[0].indexOf(raw), raw, { kind: 'gitlab', resource: m[2] === '!' ? 'merge_requests' : 'issues', id: m[3], ...(project ? { project } : {}) });
    }
    for (const m of text.matchAll(/\bsession\s+([a-f\d]{6,64})\b/gi))
        add(m.index, m[0], { kind: 'session', id: m[1] });
    for (const m of text.matchAll(/(?:^|[\s(`])([0-9a-f]{7,40})(?!\.[\w])(?=$|[\s)`.,;:])/g)) {
        if (/[a-f]/.test(m[1]))
            add(m.index + m[0].indexOf(m[1]), m[1], { kind: 'commit', hash: m[1] });
    }
    // Quoted paths include spaces. Bare paths include Unicode names, dotfiles and extensionless build files.
    const file = (raw, start) => {
        const m = /^(.*?)(?::(\d+)(?::\d+)?|#L(\d+)|\((\d+)(?:,\d+)?\)|:\[(\d+),\d+\])?$/.exec(raw);
        const path = m[1];
        // counts and ratios (`10/10`, `3/4`, `459/0/0/2`) are numbers, not folders
        if (!path || /^(?:https?|file|piper):/i.test(path) || /^\d+(?:[.,]\d+)?%?(?:\/\d+(?:[.,]\d+)?%?)+$/.test(path))
            return;
        if (path.includes('/') || EXT.test(path) || /^(?:Dockerfile|Makefile|README|LICENSE|\.env(?:\.[\w-]+)?|\.gitignore)$/i.test(path)) {
            const line = Number(m[2] || m[3] || m[4] || m[5] || 0);
            const end = text.indexOf('\n', start);
            const context = text.slice(text.lastIndexOf('\n', start) + 1, end < 0 ? text.length : end);
            add(start, raw, { kind: 'file', path, line, ...(line && /\b(?:error|warning)\b/i.test(context) ? { diagnostic: true } : {}) });
        }
    };
    for (const m of text.matchAll(/(["'`])([^\n"'`]+)\1(?::\d+(?::\d+)?|, line \d+)?/g)) {
        const suffix = m[0].slice(m[2].length + 2).replace(/^, line (\d+)$/, ':$1');
        // Include quotes in the clickable span, but not in the resolved path.
        const before = out.length;
        file(m[2] + suffix, m.index);
        if (out.length > before) {
            out.at(-1).text = m[0];
            out.at(-1).end = m.index + m[0].length;
        }
        else if (m[1] === '`' && /^[\p{L}_][\p{L}\p{N}_$]*$/u.test(m[2]))
            add(m.index, m[0], { kind: 'db', name: m[2] });
    }
    for (const m of text.matchAll(/(?:^|[\s(["'`=])((?:\.{0,2}\/|\/)?[\p{L}\p{N}\p{M}\p{S}\u200D\uFE0F_@.~-]+(?:\/[\p{L}\p{N}\p{M}\p{S}\u200D\uFE0F_@.~-]+)*\/?(?::\d+(?::\d+)?|#L\d+|\(\d+(?:,\d+)?\)|:\[\d+,\d+\])?)/gu)) {
        // a backtick is a code quote, not part of the name (its pair may sit on the next terminal row)
        const lead = /^`*/.exec(m[1])[0].length;
        const raw = m[1].slice(lead).replace(/[.,;`]+$/, '');
        if (raw)
            file(raw, m.index + m[0].indexOf(m[1]) + lead);
    }
    // Bare table names require an SQL/table cue; qualified schema.table is unambiguous enough to resolve on click.
    for (const m of text.matchAll(/\b(?:FROM|JOIN|UPDATE|INTO|TABLE|tabulka|tabulky)\s+([`"]?[\p{L}_][\p{L}\p{N}_$]*[`"]?(?:\.[`"]?[\p{L}_][\p{L}\p{N}_$]*[`"]?)?)/giu)) {
        const raw = m[1];
        add(m.index + m[0].lastIndexOf(raw), raw, { kind: 'db', name: raw.replace(/[`"]+/g, '') });
    }
    for (const m of text.matchAll(/(?:^|[\s(`])([\p{L}_][\p{L}\p{N}_$]*\.[\p{L}_][\p{L}\p{N}_$]*)\b(?![.(])/gu))
        add(m.index + m[0].indexOf(m[1]), m[1], { kind: 'db', name: m[1] });
    return out.sort((a, b) => a.start - b.start);
}
/** Exact origins only: localhost on another port, lookalike hosts and embedded credentials are not Piper. */
export function piperUrl(raw, here, origins = [], mobile = false) {
    try {
        const u = new URL(raw, here);
        if (u.username || u.password || !/^https?:$/.test(u.protocol))
            return null;
        const current = new URL(here).origin;
        // The public mobile homepage is also an ordinary web link in agent answers.
        // Explicit routes (including pairing fragments) and desktop aliases stay internal.
        if (mobile && u.origin === current && u.pathname === '/' && !u.search && !u.hash)
            return null;
        return [current, ...origins].includes(u.origin) ? u : null;
    }
    catch {
        return null;
    }
}
export function relativeFile(path, root) {
    const base = root.replace(/\/$/, '');
    if (path.startsWith('/')) {
        if (path === base)
            return '.';
        if (!path.startsWith(base + '/'))
            return null;
        path = path.slice(base.length + 1);
    }
    const parts = [];
    for (const part of path.split('/')) {
        if (!part || part === '.')
            continue;
        if (part === '..') {
            if (!parts.length)
                return null;
            parts.pop();
        }
        else
            parts.push(part);
    }
    return parts.join('/') || '.';
}
export function fileMatches(files, path) {
    if (files.includes(path))
        return [path];
    const n = path.normalize('NFC');
    return files.filter(f => f.normalize('NFC') === n || f.normalize('NFC').endsWith('/' + n));
}
export function gitlabUrl(host, project, resource, id) {
    const base = new URL(host.includes('://') ? host : `https://${host}`);
    if (!/^https?:$/.test(base.protocol) || base.username || base.password || !/^\d+$/.test(id) || !/^[\w.-]+(?:\/[\w.-]+)+$/.test(project))
        throw new Error('Invalid GitLab reference');
    return `${base.origin}/${project.split('/').map(encodeURIComponent).join('/')}/-/${resource}/${id}`;
}
/** Stable shareable routes use /?session=<id>&view=files&path=...&line=...; path/hash forms are accepted too. */
export function piperTarget(url) {
    const hash = url.hash.slice(1);
    const hashPath = hash.startsWith('/') ? new URL(hash, url.origin) : null;
    const q = new URLSearchParams(url.search);
    for (const [k, v] of (hashPath?.searchParams ?? new URLSearchParams(hash)))
        if (k !== 'token')
            q.set(k, v);
    const path = (hashPath ?? url).pathname;
    const match = /^\/(?:sessions?|s)\/([^/]+)(?:\/([^/]+))?\/?$/.exec(path);
    return { session: q.get('session') || q.get('sid') || (match ? decodeURIComponent(match[1]) : ''),
        view: q.get('view') || match?.[2] || '', path: q.get('path') || q.get('file') || '',
        line: Math.max(0, Number(q.get('line')) || 0), connection: q.get('connection') || '',
        table: q.get('table') || '', commit: q.get('commit') || '' };
}
export function fileUrlReference(raw) {
    const url = new URL(raw);
    if (url.protocol !== 'file:' || url.host && url.host !== 'localhost')
        throw new Error('Only local file links can be opened');
    const at = /^(.*?):(\d+)(?::\d+)?$/.exec(url.pathname);
    return { kind: 'file', path: decodeURIComponent(at?.[1] ?? url.pathname),
        line: Number(url.hash.replace(/^#L?/, '')) || Number(at?.[2]) || 0 };
}
//# sourceMappingURL=references.js.map