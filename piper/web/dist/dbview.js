import { machineStorage, refHost, rawProjectKey } from './environment.js';
// The Database view:
// the human's DB client - what IntelliJ's Database tool window did. Left: the project's connections
// (class colour first: dev green, test orange, prod red) and the object tree; centre: tabs of tables
// (a grid with inline editing, pending changes, FK jumps, export) and SQL consoles (highlighting,
// completion from the schema, read-only or read-write with an explicit Commit / Rollback); right:
// the table's columns and references, or the query history (yours and Claude's).
//
// The human sees raw data (no masking) and needs no card: the click is the approval. A production
// change asks once more here, in red. Everything is in piperd's DB audit like a session's calls.
import { ApiError, call } from './api.js';
import { kbd } from './clipkeys.js';
import { clear, fi, h, rawText, sideSplitter } from './dom.js';
import { highlightLines } from './highlight.js';
import { labelText, openMenu } from './menu.js';
import { hasBidi, showBidi, markBidi } from './text.js';
/** Preserve existing access; new test/prod connections start private. */
export function agentsEnabled(def, editing) {
    return typeof def.agent_enabled === 'boolean' ? def.agent_enabled : editing || def.cls === 'dev';
}
function privateMark(c) {
    return c.def?.agent_enabled === false ? h('span', { class: 'chip db-private', title: 'Not visible or accessible to agents through MCP' }, fi('user'), 'only you') : null;
}
/** Match the project's title and registry key, requiring every query word. */
export function filterIdeaProjects(projects, query) {
    const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    return projects.filter((p) => {
        const text = `${p.title} ${p.key}`.toLowerCase();
        return terms.every((term) => text.includes(term));
    });
}
/** All search words must match the project's identity or the connection's visible metadata. */
export function connectionMatches(c, projectTitle, query) {
    const text = [projectTitle, c.project, c.name, c.label, c.class, c.engine, c.database, c.user].join(' ').toLowerCase();
    return query.trim().toLowerCase().split(/\s+/).every((word) => text.includes(word));
}
/** a history entry's SQL with the grid's values put in (runnable in the console), else as recorded */
export function historySql(r, engine = 'mysql') {
    if (!r.sql || !r.params)
        return r.sql ?? '';
    const parts = r.sql.split('\n;\n');
    return parts.map((sql, i) => (r.params?.[i] ? showStatement({ sql, params: r.params[i] ?? undefined }, engine) : sql)).join(';\n');
}
const qs = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== '')).toString();
export const dbApi = {
    all: (host = '') => call('GET', '/api/db/all', undefined, undefined, 12000, host),
    connections: (project, session = '') => call('GET', `/api/db/connections?${qs({ project, session })}`),
    meta: (project, connection, what, schema = '', name = '', session = '', refresh = false) => call('GET', `/api/db/meta?${qs({ project, connection, what, schema, name, session, refresh: refresh ? '1' : '' })}`),
    history: (project, session = '', limit = 200) => call('GET', `/api/db/history?${qs({ project, session, limit: String(limit) })}`),
    query: (b) => call('POST', '/api/db/query', b),
    fetch: (result_id, offset, max_rows) => call('POST', '/api/db/fetch', { result_id, offset, max_rows }),
    execute: (b) => call('POST', '/api/db/execute', b),
    tx: (b) => call('POST', '/api/db/tx', b),
    /** owner "" = the GUI's own statements; a session id = that session's running reads (L7) */
    cancel: (project, connection, owner = '', request_tag = '', session = '') => call('POST', '/api/db/cancel', { project, connection, ...(owner ? { owner } : {}), ...(request_tag ? { request_tag } : {}), ...(session ? { session } : {}) }),
    running: (project, connection = '', session = '') => call('GET', `/api/db/running?${qs({ project, connection, session })}`),
    save: (project, connection, password) => call('POST', '/api/db/save', password === undefined ? { project, connection } : { project, connection, password }),
    del: (project, name) => call('POST', '/api/db/delete', { project, name }),
    unpin: (name, host = '') => call('POST', '/api/db/unpin', { name }, undefined, 0, host),
    tunnel: (project, connection, op) => call('POST', '/api/db/tunnel', { project, connection, op }),
    install: (host = '') => call('POST', '/api/db/install', {}, undefined, 0, host),
};
// ------------------------------------------------------------------ pure helpers (db.test.mjs)
/** the cell value "the column's DEFAULT" (context menu Set DEFAULT): the keyword in the SQL, no parameter */
export const SQL_DEFAULT = Object.freeze({ sqlDefault: true });
export const SQL_NOW_TS = Object.freeze({ sqlNow: 'CURRENT_TIMESTAMP' });
export const SQL_NOW_DATE = Object.freeze({ sqlNow: 'CURRENT_DATE' });
export const SQL_NOW_TIME = Object.freeze({ sqlNow: 'CURRENT_TIME' });
/** the SQL keyword of a sentinel value (DEFAULT, CURRENT_TIMESTAMP, ...), null for an ordinary value */
export function sqlKeyword(v) {
    if (v === SQL_DEFAULT)
        return 'DEFAULT';
    if (v === SQL_NOW_TS || v === SQL_NOW_DATE || v === SQL_NOW_TIME)
        return v.sqlNow;
    return null;
}
/** is the column a date / time one - the only cells that get Set now */
export function isTemporal(type) {
    return /^(date|time|timestamp|datetime|smalldatetime|datetimeoffset|datetime2|interval)\b/i.test((type ?? '').trim());
}
/** "now" for a column: DATE / TIME columns get the narrower keyword where the engine has it (MySQL, PostgreSQL);
 *  SQL Server has no CURRENT_DATE / CURRENT_TIME and Oracle no CURRENT_TIME - CURRENT_TIMESTAMP converts there */
export function sqlNow(engine, type) {
    const t = (type ?? '').trim().toLowerCase();
    if (/^date\b/.test(t) && engine !== 'mssql')
        return SQL_NOW_DATE;
    if (/^time\b/.test(t) && !/^timestamp/.test(t) && (engine === 'mysql' || engine === 'postgres'))
        return SQL_NOW_TIME;
    return SQL_NOW_TS;
}
/** a typed value -> what goes to the driver: MySQL BIT takes a number ('1' is the byte 0x31, too long for BIT(1)) */
/** the suggested name "<project>/<class>/<db>" with another class; any other name unchanged */
export function nameForClass(name, cls) {
    return name.replace(/^(.*)\/(dev|test|prod)\/([^/]*)$/, (_m, p, _c, rest) => `${p}/${cls}/${rest}`);
}
/** a table tab's query. The schema goes with the request (USE / search_path), so the SQL reads like the design:
 *  `shop_order`. Without a sort of its own it is ordered by the primary key: PostgreSQL returns an updated row
 *  elsewhere (MVCC) and the next page (OFFSET) must continue the same order */
export function gridSql(engine, table, where, order, desc, pk) {
    let sql = `SELECT * FROM ${tableRef(null, table, engine)}`;
    if (where.trim())
        sql += ` WHERE ${where.trim()}`;
    if (order)
        sql += ` ORDER BY ${quoteIdent(order, engine)}${desc ? ' DESC' : ''}`;
    else if (pk.length)
        sql += ` ORDER BY ${pk.map((k) => quoteIdent(k, engine)).join(', ')}`;
    return sql;
}
const HIST_MS = 5000; // N10-C-5: the History / Claude queries poll while the view is on screen
/** N10-C-1: the scrollLeft that shows a tab (start / size within the strip's content) in a strip `view` wide
 *  scrolled to `cur` - unchanged when it is in view, else the nearest edge (a wider tab: its start) */
export function scrollToShow(cur, view, start, size) {
    if (start < cur || size >= view)
        return start;
    return start + size > cur + view ? start + size - view : cur;
}
/** N10-C-3 → W11: the menu keys live in the one menu component */
export { menuMove } from './menu.js';
/** "1 row" / "3 rows" (n as shown: a number or text like "1,234") */
export function rowsText(n) {
    const t = typeof n === 'number' ? n.toLocaleString() : n.trim();
    return `${t} ${t === '1' ? 'row' : 'rows'}`;
}
export function coerceEdit(type, raw) {
    if (/^bit\b/i.test(type ?? '')) {
        const v = raw.trim().toLowerCase();
        if (v === 'true')
            return 1;
        if (v === 'false')
            return 0;
        if (/^\d+$/.test(v))
            return Number(v);
    }
    return raw;
}
/** where a word of an identifier starts: after _ - . space $, at a lower->Upper or letter<->digit change */
function wordStarts(name) {
    return [...name].map((ch, i) => {
        if (i === 0)
            return true;
        const p = name[i - 1];
        return /[_\-. $]/.test(p) || (/[A-Z]/.test(ch) && /[a-z]/.test(p)) || (/\d/.test(ch) !== /\d/.test(p) && /[A-Za-z0-9]/.test(ch));
    });
}
/** one query word against a name: exact > prefix > word prefix > camel humps / initials (soi = shop_order_item) >
 *  substring > ignoring underscores > subsequence; null = no match */
function matchWord(tk, name) {
    const n = name.toLowerCase();
    const range = (a, len) => Array.from({ length: len }, (_, i) => a + i);
    if (n === tk)
        return { score: 1000, hits: range(0, tk.length) };
    if (n.startsWith(tk))
        return { score: 850, hits: range(0, tk.length) };
    const ws = wordStarts(name);
    for (let i = 1; i < n.length; i++) {
        // a whole word (order in shop_order) beats a word's beginning; the position only breaks ties
        if (ws[i] && n.startsWith(tk, i))
            return { score: 700 + (i + tk.length === n.length || ws[i + tk.length] ? 50 : 0) - i / 100, hits: range(i, tk.length) };
    }
    // humps: each letter continues the current word or jumps to the next word starting with it
    const hump = () => {
        const hits = [];
        let prev = -1;
        for (const ch of tk) {
            let j = -1;
            if (prev >= 0 && n[prev + 1] === ch)
                j = prev + 1;
            else
                for (let k = prev + 1; k < n.length; k++)
                    if (ws[k] && n[k] === ch) {
                        j = k;
                        break;
                    }
            if (j < 0)
                return null;
            hits.push(j);
            prev = j;
        }
        return hits;
    };
    const hh = tk.length > 1 ? hump() : null;
    if (hh && ws[hh[0]])
        return { score: 600 - (hh[hh.length - 1] - hh[0]), hits: hh };
    const at = n.indexOf(tk);
    if (at >= 0)
        return { score: 500 - at / 100, hits: range(at, tk.length) };
    const map = [];
    let flat = '';
    [...n].forEach((ch, i) => { if (!/[_\-. ]/.test(ch)) {
        flat += ch;
        map.push(i);
    } });
    const ft = tk.replace(/[_\-. ]/g, '');
    const fa = ft ? flat.indexOf(ft) : -1;
    if (fa >= 0)
        return { score: 400 - fa, hits: map.slice(fa, fa + ft.length) };
    if (hh)
        return { score: 300 - (hh[hh.length - 1] - hh[0]), hits: hh };
    const sub = [];
    let k = 0;
    for (const ch of tk) {
        k = n.indexOf(ch, k);
        if (k < 0)
            return null;
        sub.push(k++);
    }
    return tk.length >= 3 ? { score: 100 - (sub[sub.length - 1] - sub[0]), hits: sub } : null;
}
/** "Find table or column": every space-separated word must match; the score ranks, `hits` are the matched
 *  letters (bold in the tree); shorter names win a tie */
export function fuzzyMatch(query, name) {
    const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length)
        return { score: 0, hits: [] };
    let score = 0;
    const hits = new Set();
    for (const w of words) {
        const m = matchWord(w, name);
        if (!m)
            return null;
        score += m.score;
        for (const x of m.hits)
            hits.add(x);
    }
    return { score: score - name.length / 100, hits: [...hits].sort((a, b) => a - b) };
}
export function quoteIdent(name, engine) {
    return engine === 'mysql' ? '`' + name.replace(/`/g, '``') + '`' : '"' + name.replace(/"/g, '""') + '"';
}
export function tableRef(schema, table, engine) {
    return (schema ? quoteIdent(schema, engine) + '.' : '') + quoteIdent(table, engine);
}
/** the UPDATE / DELETE / INSERT statements of the grid's pending changes (placeholders %s, values as params) */
export function changeStatements(engine, schema, table, columns, pk, rows, edits, deleted, inserted) {
    const t = tableRef(schema, table, engine);
    const pki = pk.map((k) => columns.indexOf(k));
    if (pki.some((i) => i < 0))
        throw new Error('the primary key is not among the loaded columns');
    const where = (row) => ({
        sql: pk.map((k, j) => (row[pki[j]] === null ? `${quoteIdent(k, engine)} IS NULL` : `${quoteIdent(k, engine)} = %s`)).join(' AND '),
        params: pki.map((i) => row[i]).filter((v) => v !== null),
    });
    const out = [];
    for (const r of [...deleted].sort((a, b) => a - b)) {
        const w = where(rows[r]);
        out.push({ sql: `DELETE FROM ${t} WHERE ${w.sql}`, params: w.params });
    }
    for (const [r, cols] of [...edits.entries()].sort((a, b) => a[0] - b[0])) {
        if (deleted.has(r) || !cols.size)
            continue;
        const sets = [...cols.entries()].sort((a, b) => a[0] - b[0]);
        const w = where(rows[r]);
        out.push({ sql: `UPDATE ${t} SET ${sets.map(([c, v]) => `${quoteIdent(columns[c], engine)} = ${sqlKeyword(v) ?? '%s'}`).join(', ')} WHERE ${w.sql}`,
            params: [...sets.map(([, v]) => v).filter((v) => sqlKeyword(v) === null), ...w.params] });
    }
    for (const ins of inserted) {
        const cols = [...ins.entries()].filter(([, v]) => v !== undefined).sort((a, b) => a[0] - b[0]);
        out.push(cols.length
            ? { sql: `INSERT INTO ${t} (${cols.map(([c]) => quoteIdent(columns[c], engine)).join(', ')}) VALUES (${cols.map(([, v]) => sqlKeyword(v) ?? '%s').join(', ')})`,
                params: cols.map(([, v]) => v).filter((v) => sqlKeyword(v) === null) }
            : { sql: engine === 'mysql' ? `INSERT INTO ${t} () VALUES ()` : `INSERT INTO ${t} DEFAULT VALUES`, params: [] });
    }
    return out;
}
/** a statement as text for the pending bar and the confirmation (params inlined as literals, display only) */
export function showStatement(st, engine = 'mysql') {
    let i = 0;
    return st.sql.replace(/%s/g, () => sqlLiteral((st.params ?? [])[i++], engine));
}
/** a value as an SQL literal: MySQL treats \\ as an escape in strings, PostgreSQL / Oracle / SQL Server do not */
export function sqlLiteral(v, engine = 'mysql') {
    if (v === null || v === undefined)
        return 'NULL';
    const kw = sqlKeyword(v);
    if (kw)
        return kw;
    if (typeof v === 'number' || typeof v === 'boolean')
        return String(v);
    const t = String(v).replace(/'/g, "''");
    return "'" + (engine === 'mysql' ? t.replace(/\\/g, '\\\\') : t) + "'";
}
export function cellText(v) {
    if (v === null || v === undefined)
        return 'NULL';
    const kw = sqlKeyword(v);
    if (kw)
        return kw;
    return typeof v === 'string' ? v : JSON.stringify(v);
}
/** a cell as the grid SHOWS it: cut, a bidi control / invisible character visible as [U+202E] (the value
 *  itself - copy, edit, SQL - stays cellText): 'rtl \u202Eevil' must not read as 'rtl live' */
export function cellShown(v, max) {
    return showBidi(cellText(v).slice(0, max));
}
export function toCsv(columns, rows) {
    const esc = (v) => {
        if (v === null || v === undefined)
            return '';
        const s = typeof v === 'string' ? v : String(v);
        return /[",\n\r;]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    };
    return [columns.map(esc).join(','), ...rows.map((r) => r.map(esc).join(','))].join('\n') + '\n';
}
export function toInserts(engine, schema, table, columns, rows) {
    const t = tableRef(schema, table, engine);
    const cols = columns.map((c) => quoteIdent(c, engine)).join(', ');
    return rows.map((r) => `INSERT INTO ${t} (${cols}) VALUES (${r.map((v) => sqlLiteral(v, engine)).join(', ')});`).join('\n') + '\n';
}
/** RFC 4180 CSV (quotes, "" escapes, CRLF); the delimiter is guessed from the header line (, ; tab) */
export function parseCsv(text) {
    const first = text.split(/\r?\n/, 1)[0] ?? '';
    const delim = [',', ';', '\t'].map((d) => [d, first.split(d).length]).sort((a, b) => b[1] - a[1])[0][0];
    const rows = [];
    let row = [];
    let cell = '';
    let q = false;
    for (let i = 0; i < text.length; i++) {
        const c = text[i];
        if (q) {
            if (c === '"') {
                if (text[i + 1] === '"') {
                    cell += '"';
                    i++;
                }
                else
                    q = false;
            }
            else
                cell += c;
        }
        else if (c === '"' && cell === '')
            q = true;
        else if (c === delim) {
            row.push(cell);
            cell = '';
        }
        else if (c === '\n' || c === '\r') {
            if (c === '\r' && text[i + 1] === '\n')
                i++;
            row.push(cell);
            cell = '';
            if (row.length > 1 || row[0] !== '')
                rows.push(row);
            row = [];
        }
        else
            cell += c;
    }
    if (cell !== '' || row.length) {
        row.push(cell);
        rows.push(row);
    }
    return rows;
}
/** INSERT statements for imported rows, `batch` rows per multi-row VALUES, values as params */
export function csvInserts(engine, schema, table, columns, rows, batch = 100) {
    const t = tableRef(schema, table, engine);
    const cols = columns.map((c) => quoteIdent(c, engine)).join(', ');
    const one = '(' + columns.map(() => '%s').join(', ') + ')';
    const out = [];
    for (let i = 0; i < rows.length; i += batch) {
        const part = rows.slice(i, i + batch);
        out.push({ sql: `INSERT INTO ${t} (${cols}) VALUES ${part.map(() => one).join(', ')}`, params: part.flat() });
    }
    return out;
}
/** the statement under the caret (or the selection): `;` outside quotes and comments separates */
export function statementAt(text, caret) {
    const bounds = [-1];
    let q = '';
    for (let i = 0; i < text.length; i++) {
        const c = text[i];
        if (q) {
            if (c === '\\' && q !== '`') {
                i++;
                continue;
            }
            if (c === q)
                q = '';
            continue;
        }
        if (c === '-' && text[i + 1] === '-') {
            const e = text.indexOf('\n', i);
            i = e < 0 ? text.length : e;
            continue;
        }
        if (c === '#') {
            const e = text.indexOf('\n', i);
            i = e < 0 ? text.length : e;
            continue;
        }
        if (c === '/' && text[i + 1] === '*') {
            const e = text.indexOf('*/', i + 2);
            i = e < 0 ? text.length : e + 1;
            continue;
        }
        if (c === "'" || c === '"' || c === '`') {
            q = c;
            continue;
        }
        if (c === ';')
            bounds.push(i);
    }
    bounds.push(text.length);
    for (let k = 0; k < bounds.length - 1; k++) {
        const a = bounds[k] + 1;
        const b = bounds[k + 1];
        if (caret >= a && caret <= b) {
            const raw = text.slice(a, b);
            if (raw.trim() || k === 0)
                return { sql: raw.trim(), start: a, end: b };
            // the caret sits right after a `;` on an empty tail: the statement before it
            const pa = bounds[k - 1] + 1;
            return { sql: text.slice(pa, a - 1).trim(), start: pa, end: a - 1 };
        }
    }
    return { sql: text.trim(), start: 0, end: text.length };
}
const CLAUSES = ['SELECT', 'FROM', 'WHERE', 'GROUP BY', 'HAVING', 'ORDER BY', 'LIMIT', 'LEFT OUTER JOIN', 'RIGHT OUTER JOIN', 'LEFT JOIN', 'RIGHT JOIN',
    'INNER JOIN', 'JOIN', 'UNION ALL', 'UNION', 'SET', 'VALUES', 'UPDATE', 'DELETE FROM', 'INSERT INTO', 'ON DUPLICATE KEY UPDATE', 'RETURNING'];
/** N10-C-7: one pass, the longest clause first - clause by clause, JOIN split a LEFT JOIN made before it ("LEFT\nJOIN"),
 *  FROM a DELETE FROM at the start, UPDATE an ON DUPLICATE KEY UPDATE */
const CLAUSE_RX = new RegExp(`(^|\\s+)(${[...CLAUSES].sort((a, b) => b.length - a.length).map((c) => c.replace(/ /g, '\\s+')).join('|')})\\b`, 'gi');
const KEYWORDS = ['SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'NULL', 'IS', 'IN', 'LIKE', 'BETWEEN', 'EXISTS', 'JOIN', 'LEFT',
    'RIGHT', 'INNER', 'OUTER', 'ON', 'AS', 'GROUP', 'BY', 'ORDER', 'ASC', 'DESC', 'LIMIT', 'OFFSET', 'HAVING', 'DISTINCT', 'COUNT',
    'SUM', 'MIN', 'MAX', 'AVG', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END', 'UNION', 'ALL', 'INSERT', 'INTO', 'VALUES', 'UPDATE', 'SET',
    'DELETE', 'WITH', 'SHOW', 'TABLES', 'DESCRIBE', 'EXPLAIN', 'CREATE', 'ALTER', 'TABLE', 'INDEX', 'DROP', 'COALESCE', 'IFNULL', 'NOW',
    'DATE', 'INTERVAL', 'CAST', 'CONCAT', 'LOWER', 'UPPER', 'TRUE', 'FALSE'];
/** a light formatter: keywords upper-case, the main clauses on their own lines (strings and comments untouched) */
export function formatSql(text) {
    const parts = [];
    const re = /('(?:[^'\\]|\\.|'')*'|"(?:[^"\\]|\\.)*"|`[^`]*`|--[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/)/g;
    let last = 0;
    let m;
    const kw = new Set(KEYWORDS);
    const fmt = (s) => {
        const o = s.replace(/\s+/g, ' ').replace(/\b([A-Za-z_]+)\b/g, (w) => (kw.has(w.toUpperCase()) ? w.toUpperCase() : w));
        return o.replace(CLAUSE_RX, (_m, pre, c) => (pre ? '\n' : '') + c.toUpperCase().replace(/\s+/g, ' '));
    };
    while ((m = re.exec(text))) {
        parts.push(fmt(text.slice(last, m.index)), m[0]);
        last = m.index + m[0].length;
    }
    parts.push(fmt(text.slice(last)));
    return parts.join('').replace(/^\n+/, '').replace(/[ \t]+\n/g, '\n').trim();
}
/** completion candidates for the word before the caret: tables, columns of the tables in the text, keywords */
export function completions(prefix, tables, columns, text) {
    const p = prefix.toLowerCase();
    if (!p)
        return [];
    const used = tables.filter((t) => new RegExp(`\\b${t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'i').test(text));
    const cols = [...new Set(used.flatMap((t) => columns.get(t) ?? []))];
    const out = [];
    const add = (xs) => { for (const x of xs)
        if (x.toLowerCase().startsWith(p) && x.toLowerCase() !== p && !out.includes(x))
            out.push(x); };
    add(cols);
    add(tables);
    add(KEYWORDS);
    return out.slice(0, 12);
}
/** the tables a statement mentions after FROM / JOIN / UPDATE / INTO (for completion of their columns) */
export function mentionedTables(text, tables) {
    return tables.filter((t) => new RegExp(`(?:from|join|update|into)\\s+[\`"]?${t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'i').test(text));
}
const CLS_LABEL = { dev: 'dev', test: 'test', prod: 'prod' };
/** history entries without SQL: what the console's buttons did */
const TOOL_LABEL = { gui_tx_commit: 'Commit', gui_tx_rollback: 'Rollback', gui_tx_state: 'transaction state' };
export function classBadge(cls, text) {
    return h('span', { class: `db-cls db-cls-${cls}` }, h('span', { class: 'db-dot' }), text ?? CLS_LABEL[cls]);
}
/** F10: the class in a list - a coloured dot and the word, no filled chip (prod's word stays red) */
export function classDot(cls, text) {
    return h('span', { class: `db-cls db-cls-${cls} db-cls-dot` }, h('span', { class: 'db-dot' }), text ?? CLS_LABEL[cls]);
}
/** F10: a connection needs a password only when it is not the stack's and none is stored */
export function needsPassword(c) {
    return c.reach !== 'stack' && !c.has_password;
}
/** F10: the Reach column as a short text beside its icon: "stack :3406", "ssh · closed", "tunnel · open" */
export function reachText(c) {
    if (c.reach === 'stack') {
        const port = c.host?.split(':').pop();
        return port ? `stack :${port}` : 'stack';
    }
    if (c.reach === 'tunnel' || c.reach === 'ssh')
        return `${c.reach} · ${c.tunnel?.state ?? 'closed'}`;
    if (c.reach === 'direct')
        return 'direct';
    return 'needs tunnel target';
}
/** a db_* write card: title with the class, ⚠ lines, what runs, the rolled-back preview with the rows
 *  before → after, the reason; buttons from the caller (Deny / the session button / Allow) */
export function dbCard(title, c, buttons) {
    const pre = h('pre', { class: 'dbc-sql' });
    const lines = c.sql.split('\n');
    highlightLines(lines, 'sql').forEach((row, i) => {
        for (const tk of row)
            pre.append(tk.c ? h('span', { class: 'hl-' + tk.c }, tk.t) : markBidi(tk.t));
        if (i < lines.length - 1)
            pre.append('\n');
    });
    let table = null;
    if (c.sample && c.sample.rows.length) {
        table = h('div', { class: 'dbc-rows' });
        const tpl = `repeat(${c.sample.columns.length}, minmax(60px, auto))`;
        const head = h('div', { class: 'dbc-tr dbc-th' }, ...c.sample.columns.map((k) => h('span', {}, k)));
        head.style.gridTemplateColumns = tpl;
        table.append(head);
        for (const r of c.sample.rows.slice(0, 6)) {
            const tr = h('div', { class: 'dbc-tr' + (c.sample.deleted ? ' del' : '') });
            tr.style.gridTemplateColumns = tpl;
            for (const cell of r) {
                tr.append('to' in cell ? h('span', { class: 'chg' }, `${cellText(cell.v)} → ${cellText(cell.to)}`) : h('span', {}, cellText(cell.v)));
            }
            table.append(tr);
        }
        const total = c.sample.total ?? c.sample.rows.length;
        if (total > 6)
            table.append(h('div', { class: 'dbc-more' }, `… ${total - Math.min(6, c.sample.rows.length)} more row${total - 6 === 1 ? '' : 's'}`));
    }
    const prod = c.cls === 'prod';
    return h('div', { class: `dbc dbc-${c.cls}` }, h('div', { class: 'dbc-h', title }, ic('db'), h('span', { class: 'dbc-title' }, `Database write · ${prod ? 'PROD · ' : ''}${c.project}`), h('span', { class: 'grow' }), classBadge(c.cls)), h('div', { class: 'dbc-b' }, ...(prod ? c.warnings.slice(0, 1) : []).map((w) => h('div', { class: 'dbc-w prod' }, ic('warn'), w)), h('div', { class: 'dbc-lead' }, c.lead), pre, h('div', { class: 'dbc-pv' + (c.preview_ok ? ' ok' : ' warn') }, c.preview), ...(prod ? c.warnings.slice(1) : c.warnings).map((w) => h('div', { class: 'dbc-w small' }, ic('warn'), w)), table, c.reason ? h('div', { class: 'dbc-reason' }, `Reason: ${c.reason}`) : null, c.note ? h('div', { class: 'dbc-note' }, c.note) : null), buttons);
}
const ROW_H = 28;
/** this page load's prefix of transaction names: a reloaded page's "Console 1" never joins the old page's open transaction */
const PAGE_TX = Math.random().toString(36).slice(2, 10) + '-';
let viewSequence = 0;
const CONN_KEY = 'piper.db.conn';
const TX_KEY = 'piper.db.tx'; // this tab's open console transactions (sessionStorage: survives a reload of the tab)   // the last open connection (this browser only)
/** marks the view's own lookups ("3 rows ›" of Referenced by): audited, but not shown in History */
const LOOKUP = '/* piper: referenced-by count */';
function readStore(k) {
    try {
        return machineStorage(localStorage).getItem(k);
    }
    catch {
        return null;
    }
}
function writeStore(k, v) {
    try {
        machineStorage(localStorage).setItem(k, v);
    }
    catch { /* private window: not remembered */ }
}
const PAGE_KEY = 'piper.db.page'; // rows per page, picked next to the results (IDEA's 500); this browser only
const PAGE_SIZES = [100, 500, 1000, 5000];
const PAGE_MAX = 10000; // below piperd's 50 000 clamp: a MySQL / PostgreSQL table asks page + 1 rows
function pageSize() {
    const n = Math.floor(Number(readStore(PAGE_KEY)));
    return n >= 1 && n <= PAGE_MAX ? n : 500;
}
const fmtRows = (n) => n.toLocaleString();
const SKEW = 'piperd is older than this GUI (it ignored the page offset) - restart piperd';
export function createDbView(deps) {
    const viewTx = PAGE_TX + (++viewSequence).toString(36) + '-';
    const sharedIds = new Map();
    const scopes = new Map();
    const shared = (name) => {
        if (!scopes.has(name))
            scopes.set(name, readStore('piper.db.txscope.' + name) === 'shared');
        return scopes.get(name);
    };
    const txId = (t) => {
        if (!shared(t.conn))
            return viewTx + t.id;
        if (!sharedIds.has(t.conn))
            sharedIds.set(t.conn, viewTx + 's' + sharedIds.size.toString(36));
        return sharedIds.get(t.conn);
    };
    const leftEl = h('div', { class: 'db-left' });
    const tabsEl = h('div', { class: 'db-tabs', role: 'tablist' });
    const bodyEl = h('div', { class: 'db-body' });
    const rightEl = h('div', { class: 'db-right' });
    const mainEl = h('div', { class: 'db-main' }, tabsEl, bodyEl);
    const el = h('div', { class: 'dbview' + (deps.compact ? ' compact' : '') }, leftEl, mainEl, rightEl);
    sideSplitter(el, leftEl, '--db-l-w', 'piper.w.dbl', 200, 480, () => deps.compact ? 210 : 300, 'right', () => 280 + rightEl.getBoundingClientRect().width);
    sideSplitter(el, rightEl, '--db-r-w', 'piper.w.dbr', 220, 480, 300, 'left', () => 280 + leftEl.getBoundingClientRect().width);
    /** N10-C-1: the console's active result tab in its strip's view (after a draw, and when the strip narrows - the drawer) */
    function showActiveResult() {
        const rtabs = bodyEl.querySelector('.db-rtabs');
        const on = rtabs?.querySelector('.db-rtab.on');
        if (!rtabs || !on)
            return;
        rtabs.scrollLeft = scrollToShow(rtabs.scrollLeft, rtabs.clientWidth, on.getBoundingClientRect().left - rtabs.getBoundingClientRect().left + rtabs.scrollLeft, on.offsetWidth);
    }
    if (typeof ResizeObserver !== 'undefined')
        new ResizeObserver(() => showActiveResult()).observe(mainEl);
    const connKey = deps.storeKey ? `${CONN_KEY}.${deps.storeKey}` : CONN_KEY;
    let scopeAt = ''; // the project the loaded list was filtered for (a scoped instance follows its session)
    const session = deps.session ?? '';
    let all = null;
    let listErr = '';
    let connectionFilter = ''; // retained while switching between overview and a connection
    const projInfo = new Map(); // project -> its details for the editor (tunnel targets, IDEA)
    let conn = readStore(connKey) ?? ''; // the open connection (S1 / S2); '' = the overview of every database (S4)
    const lastTab = new Map(); // conn -> its last active tab (tabs belong to their connection)
    const objects = new Map(); // conn|schema -> objects
    const schemas = new Map();
    const schemaOf = new Map(); // conn -> the schema shown in the tree
    const described = new Map(); // conn|schema|table
    let tabs = [];
    const joined = (t) => tabs.filter((x) => x.kind === 'console' && x.conn === t.conn && x.mode === 'rw');
    const joinTx = (t) => {
        const peer = shared(t.conn) && joined(t).find((other) => other !== t && other.tx?.open);
        if (peer) {
            t.tx = peer.tx;
            t.schema = peer.schema;
        }
    };
    const syncTx = (t) => {
        if (shared(t.conn))
            for (const other of joined(t)) {
                other.tx = t.tx;
                if (t.tx?.open)
                    other.schema = t.schema; // one physical transaction has one active schema
            }
    };
    let activeTab = '';
    let treeFilter = '';
    /** 9.39: a narrow pane (compact, under 720 px) shows the tree over its tabs - ▤ Tables; any opened tab hides it again */
    let treeOn = false;
    let history = [];
    let histKey = '';
    let histFilter = 'all';
    let seq = 0;
    let rightHistory = false;
    let lastDown = null; // a double click survives the repaint between its clicks
    let editSeed = null; // the key that started an edit by typing (it replaces the value, like a spreadsheet)
    const saving = new Set(); // table tabs whose Submit is running
    const colIndex = new Map(); // conn|schema
    let findAt = 0; // the picked search result (↑ ↓)
    let treeRedraw = null; // the tree of the open connection, without rebuilding the search input
    let treeOpen = null; // Enter in the search
    let liveEdit = null; // the open cell editor, kept across repaints and polls
    const refCounts = new Map(); // conn|schema|table.col=value -> rows   // the table tab's right panel shows the history instead of the table
    const errText = (e) => (e instanceof ApiError ? e.message : String(e));
    const allConns = () => (all?.projects ?? []).flatMap((g) => g.connections);
    const curConn = (name = conn) => allConns().find((c) => c.name === name) ?? null;
    /** the project a connection belongs to - what every /api/db call names */
    const pj = (name = conn) => curConn(name)?.project ?? '';
    const tab = () => tabs.find((t) => t.id === activeTab && t.conn === conn) ?? null;
    const connTabs = () => tabs.filter((t) => t.conn === conn);
    /** a connection with an open transaction or unsaved grid edits - visible even when another one is open */
    const busy = (name) => tabs.some((t) => t.conn === name && (t.kind === 'console' ? t.running || !!t.tx?.open : pendingCount(t) > 0));
    const busyMark = (name) => (busy(name) ? h('span', { class: 'db-dirty', title: 'open transaction or unsaved changes' }, '●') : null);
    const readTx = () => { try {
        return JSON.parse(machineStorage(sessionStorage).getItem(TX_KEY) ?? '[]');
    }
    catch {
        return [];
    } };
    const writeTx = (v) => { try {
        machineStorage(sessionStorage).setItem(TX_KEY, JSON.stringify(v));
    }
    catch { /* not remembered */ } };
    function rememberTx(x) { writeTx([...readTx().filter((y) => y.tx !== x.tx || y.connection !== x.connection), x]); }
    function forgetTx(tx) { writeTx(readTx().filter((y) => y.tx !== tx)); }
    let txSwept = false;
    async function sweepTx() {
        if (txSwept)
            return;
        txSwept = true;
        const left = readTx().filter((x) => !x.tx.startsWith(PAGE_TX));
        let n = 0;
        for (const x of left) {
            try {
                await dbApi.tx({ project: x.project, connection: x.connection, op: 'rollback', session: x.session ?? '', tx: x.tx });
                n++;
                forgetTx(x.tx);
            }
            catch {
                deps.toast('Could not roll back a transaction left by the previous page. Reload to retry.');
            }
        }
        if (n)
            deps.toast(`Rolled back ${n} console transaction${n === 1 ? '' : 's'} the reloaded page left open`);
    }
    // ---------------------------------------------------------------- loading
    async function load(force = false) {
        void sweepTx();
        if (all && !force && (!deps.scope || deps.scope() === scopeAt))
            return;
        try {
            const scope = deps.scope?.() ?? '';
            const scoped = session ? (await dbApi.connections(scope, session)).data : null;
            const data = session ? scoped ? { projects: [{ project: scoped.project,
                        title: deps.projects().find((p) => p.key === scoped.project)?.title ?? scoped.project,
                        root: scoped.root ?? '', connections: scoped.connections, ...(scoped.links ? { links: scoped.links } : {}) }], orphans: [], status: scoped.status } : null
                : (await dbApi.all(refHost(scope))).data;
            if (scoped)
                projInfo.set(scope, scoped);
            if (!data || deps.scope && scope !== deps.scope())
                return;
            all = data;
            if (deps.scope) {
                scopeAt = scope;
                all = { ...data, projects: data.projects.filter((g) => g.project === scopeAt) };
                if (conn && !curConn(conn))
                    conn = all.projects[0]?.connections.find((c) => c.class === 'dev')?.name ?? '';
            }
            listErr = '';
            if (conn && !curConn(conn))
                conn = '';
        }
        catch (e) {
            listErr = errText(e);
        }
        draw();
        if (conn)
            void loadObjects(conn);
        void loadHistory();
    }
    async function projectInfo(key, force = false) {
        if (projInfo.has(key) && !force)
            return projInfo.get(key);
        try {
            const { data } = await dbApi.connections(key, session);
            if (data)
                projInfo.set(key, data);
            return data;
        }
        catch (e) {
            deps.toast(errText(e));
            return null;
        }
    }
    async function loadObjects(name, refresh = false) {
        const c = curConn(name);
        if (!c || (!c.has_password && c.reach !== 'stack'))
            return;
        if (!schemas.has(name)) {
            try {
                const { data } = await dbApi.meta(pj(name), name, 'schemas', '', '', session);
                schemas.set(name, data?.schemas ?? []);
                if (!schemaOf.has(name))
                    schemaOf.set(name, data?.default ?? c.database ?? '');
            }
            catch (e) {
                objects.set(name + '|', { error: errText(e) });
                draw();
                return;
            }
        }
        const sch = schemaOf.get(name) ?? '';
        const key = name + '|' + sch;
        if (refresh)
            colIndex.delete(key);
        if (objects.has(key) && !refresh && !('error' in objects.get(key)))
            return;
        try {
            const { data } = await dbApi.meta(pj(name), name, 'objects', sch, '', session, refresh);
            objects.set(key, data ?? { schema: sch, objects: [], routines: [] });
        }
        catch (e) {
            objects.set(key, { error: errText(e) });
        }
        draw();
    }
    async function describe(name, schema, table) {
        const key = `${name}|${schema}|${table}`;
        if (described.has(key))
            return described.get(key);
        try {
            const { data } = await dbApi.meta(pj(name), name, 'describe', schema, table, session);
            if (data)
                described.set(key, data);
            return data;
        }
        catch {
            return null;
        }
    }
    /** quiet: the poll below - nothing redrawn when nothing changed, the list's scroll kept when it did */
    async function loadHistory(quiet = false) {
        let rows;
        try {
            const { data } = await dbApi.history('', '', 300); // everything: yours and every session's
            rows = data?.queries ?? [];
        }
        catch {
            if (quiet)
                return;
            rows = [];
        }
        const key = rows.map((r) => r.id).join(' ');
        if (quiet && key === histKey)
            return;
        if (quiet && rightEl.contains(document.activeElement))
            return; // never under the keyboard: the next tick redraws
        histKey = key;
        history = rows;
        const sc = rightEl.querySelector('.db-rscroll')?.scrollTop ?? 0;
        drawRight();
        const sc2 = quiet ? rightEl.querySelector('.db-rscroll') : null;
        if (sc2)
            sc2.scrollTop = sc;
    }
    // N10-C-5: Claude's queries (History, the table's Claude queries) arrive while the view is open - refresh() had no caller
    window.setInterval(() => { if (!document.hidden && el.isConnected && el.offsetParent !== null)
        void loadHistory(true); }, HIST_MS);
    // ---------------------------------------------------------------- tabs
    function openTable(name, schema, table, where = '') {
        leaveEdit();
        treeOn = false;
        conn = name;
        const existing = tabs.find((t) => t.kind === 'table' && t.conn === name && t.schema === schema && t.table === table);
        if (existing && !where) {
            activeTab = existing.id;
            draw();
            return;
        }
        const t = { kind: 'table', id: 't' + ++seq, conn: name, schema, table, where, order: '', desc: false, offset: 0, res: null, rows: [],
            cols: [], info: null, loading: true, error: '', edits: new Map(), deleted: new Set(), inserted: [], sel: null, editing: null,
            showDdl: false, find: '', scroll: 0, scrollX: 0, hidden: new Set(), sqlOpen: false };
        tabs.push(t);
        activeTab = t.id;
        draw();
        void loadTable(t);
    }
    function openConsole(name = conn, text = '') {
        if (!name)
            return;
        conn = name;
        treeOn = false;
        const t = { kind: 'console', id: 'c' + ++seq, conn: name, text, mode: 'ro', results: [], active: 0, running: false,
            tx: null, caret: text.length, schema: schemaOf.get(name) ?? '' };
        tabs.push(t);
        activeTab = t.id;
        draw();
        setTimeout(() => bodyEl.querySelector('.db-editor textarea')?.focus(), 0);
    }
    /** every database of every project (S4) - the only place that lists them all */
    function leaving() {
        if (conn && busy(conn))
            deps.toast(`${short(curConn())} keeps an open transaction or unsaved changes - ● in Switch`);
    }
    function openOverview() {
        leaveEdit();
        leaving();
        if (conn && activeTab)
            lastTab.set(conn, activeTab);
        conn = '';
        draw();
    }
    /** open one connection (S1): its tree on the left, its tabs in the middle */
    function openConn(name) {
        leaveEdit();
        if (conn !== name)
            leaving();
        if (conn && activeTab)
            lastTab.set(conn, activeTab);
        conn = name;
        const mine = connTabs();
        activeTab = mine.find((t) => t.id === lastTab.get(name))?.id ?? mine[mine.length - 1]?.id ?? '';
        treeFilter = '';
        if (deps.compact && !activeTab) {
            openConsole(name);
            void loadObjects(name);
            return;
        } // beside a terminal: straight to a console
        draw();
        void loadObjects(name);
    }
    /** the cell being typed goes into the pending changes before the view moves elsewhere */
    function leaveEdit() {
        const cur = tab();
        if (cur?.kind === 'table' && cur.editing)
            commitEdit(cur);
    }
    function activate(id) {
        leaveEdit();
        treeOn = false;
        activeTab = id;
        const t = tabs.find((x) => x.id === id);
        if (t)
            conn = t.conn;
        draw();
    }
    function closeTab(id) {
        const t = tabs.find((x) => x.id === id);
        if (t?.kind === 'console' && t.running) {
            deps.toast('Wait for the running statement to finish before closing the console');
            return;
        }
        if (t && t.kind === 'table' && pendingCount(t)) {
            deps.modal('Discard changes?', h('p', {}, `${t.table} has ${pendingCount(t)} unsaved change(s).`), h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Keep'), h('button', { class: 'danger', onclick: () => { deps.closeModal(); t.edits.clear(); t.deleted.clear(); t.inserted = []; closeTab(id); } }, 'Discard')));
            return;
        }
        if (t && t.kind === 'console' && t.tx?.open) {
            deps.toast('Commit or roll back the open transaction first');
            return;
        }
        const mine = connTabs();
        const i = mine.findIndex((x) => x.id === id);
        tabs = tabs.filter((x) => x.id !== id);
        const rest = mine.filter((x) => x.id !== id);
        if (activeTab === id)
            activeTab = rest[Math.min(i, rest.length - 1)]?.id ?? '';
        draw();
    }
    // ---------------------------------------------------------------- table data
    function tableSql(t, engine) {
        return gridSql(engine, t.table, t.where, t.order, t.desc, t.info?.primary_key ?? []);
    }
    /** reload for a filter / sort / ↻: pending grid changes are not dropped silently */
    function reloadTable(t, then, keepPage = false) {
        const n = pendingCount(t);
        const go = () => { if (!keepPage)
            t.offset = 0; then?.(); void loadTable(t); };
        if (!n) {
            go();
            return;
        }
        deps.modal('Discard changes?', h('p', {}, `${t.table} has ${n} unsaved change${n === 1 ? '' : 's'}. Reloading drops them.`), h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Keep'), h('button', { class: 'danger', onclick: () => { deps.closeModal(); go(); } }, 'Discard and reload')));
    }
    async function loadTable(t) {
        const c = curConn(t.conn);
        if (!c)
            return;
        t.loading = true;
        t.error = '';
        draw();
        if (!t.info)
            t.info = await describe(t.conn, t.schema, t.table);
        try {
            // one page (as IDEA): MySQL / PostgreSQL by LIMIT / OFFSET (one row more tells whether there is a next page),
            // the others run the statement and piperd skips the rows before the page
            const page = pageSize();
            const off = t.offset;
            const lim = c.engine === 'mysql' || c.engine === 'postgres';
            const { data } = await dbApi.query({ project: pj(t.conn), connection: t.conn, sql: lim ? `${tableSql(t, c.engine)}\nLIMIT ${page + 1} OFFSET ${off}` : tableSql(t, c.engine), // LIMIT on its own line: a WHERE ending in `-- comment` must not swallow it
                schema: t.schema, max_rows: lim ? page + 1 : page, offset: lim ? 0 : off, session });
            if (data && lim)
                t.res = { ...data, rows: data.rows.slice(0, page), offset: off, more: data.rows.length > page };
            else if (data && data.offset !== off) {
                t.res = null;
                t.error = SKEW;
            }
            else if (data && !data.rows.length && off)
                t.res = { ...data, more: false }; // past piperd's row limits: the end, not a live › forever
            else
                t.res = data;
            t.rows = t.res?.rows ?? [];
            t.cols = data?.columns ?? [];
            t.edits.clear();
            t.deleted.clear();
            t.inserted = [];
            t.sel = t.rows.length ? { r: 0, c: 0 } : null;
        }
        catch (e) {
            t.error = errText(e);
        }
        t.loading = false;
        draw();
        void loadHistory();
    }
    /** ‹ › : another page of a table tab replaces the rows shown (pending changes ask first, like a reload) */
    function gotoPage(t, offset) {
        if (t.loading)
            return;
        reloadTable(t, () => { t.offset = Math.max(0, offset); }, true);
    }
    /** ‹ › : a page of a console result - from piperd's cache when it holds it, else the statement runs again */
    async function consolePage(t, r, offset) {
        if (!r.res || t.running)
            return;
        if (r.tx && !t.tx?.open) {
            deps.toast('The transaction this result came from is closed - run the statement again.');
            return;
        } // never a new one opened by a page
        const page = pageSize();
        offset = Math.max(0, offset);
        t.running = true;
        drawBody();
        try {
            let data = null;
            if (r.res.result_id) { // the cache holds what the last run fetched: every earlier page, a shorter one only at the end
                try {
                    const { data: d } = await dbApi.fetch(r.res.result_id, offset, page);
                    if (d && (d.rows.length >= page || !d.more))
                        data = d;
                }
                catch { /* expired (15 min) or dropped from the cache: the statement runs again below */ }
            }
            if (!data && r.sql !== undefined) {
                if (r.tx) { // the open transaction: the SELECT once more, the rows before the page skipped here
                    if (offset + page > 50000)
                        throw new Error('page too deep for a result inside a transaction (piperd returns at most 50 000 rows) - narrow the query');
                    const { data: st } = await dbApi.tx({ project: pj(t.conn), connection: t.conn, op: 'state', session, tx: txId(t) });
                    if (!st?.tx_open) { // the server ended it (idle worker reaped = rolled back): a page must not open a fresh, empty one
                        t.tx = null;
                        syncTx(t);
                        forgetTx(txId(t));
                        throw new Error('The transaction ended on the server (rolled back after 10 min idle) - run the statements again.');
                    }
                    const { data: d } = await dbApi.tx({ project: pj(t.conn), connection: t.conn, op: 'exec', statements: [{ sql: r.sql }], schema: t.schema, session,
                        tx: txId(t), max_rows: offset + page, request_tag: viewTx + t.id });
                    const x = d?.results[0];
                    data = x?.columns ? { result_id: '', columns: x.columns, rows: (x.rows ?? []).slice(offset), offset, row_count: 0, more: !!x.truncated } : null;
                }
                else {
                    data = (await dbApi.query({ project: pj(t.conn), connection: t.conn, sql: r.sql, schema: t.schema, session, explain: !!r.explain, max_rows: page, offset, request_tag: viewTx + t.id })).data;
                    if (data && data.offset !== offset) {
                        deps.toast(SKEW);
                        data = null;
                    }
                }
            }
            if (data) {
                if (!data.rows.length && offset)
                    data = { ...data, more: false }; // past piperd's row limits: the end, not a live › forever
                r.rows = data.rows;
                r.res = { ...r.res, ...data, result_id: data.result_id || r.res.result_id };
            }
            else if (r.sql === undefined)
                deps.toast('This result cannot be paged - run the statement again.');
        }
        catch (e) {
            deps.toast(errText(e));
            // a page inside the transaction failed (the server dropped the connection, rolled back): ask the worker,
            // never keep the banner, Commit and "tx open" for changes that are gone (as runConsole)
            if (r.tx && t.tx) {
                try {
                    const { data: st } = await dbApi.tx({ project: pj(t.conn), connection: t.conn, op: 'state', session, tx: txId(t) });
                    if (!st?.tx_open) {
                        t.tx = null;
                        syncTx(t);
                        forgetTx(txId(t));
                    }
                }
                catch { /* unknown: keep what the console knows */ }
            }
        }
        t.running = false;
        drawBody();
    }
    /** "501–1 000+ rows": the page shown; + = there is a next page */
    function rangeText(offset, n, more) {
        if (!n)
            return '0 rows';
        if (!offset && !more)
            return rowsText(n);
        return `${fmtRows(offset + 1)}–${fmtRows(offset + n)}${more ? '+' : ''} rows`;
    }
    /** the IDEA-like page control next to a result: ‹ rows per page ▾ › */
    function pageCtl(offset, more, busy, go, changed) {
        const cur = pageSize();
        const size = h('button', { title: 'rows per page (remembered in this browser)' }, fmtRows(cur), fi('chev-d', 'db-caret'));
        size.addEventListener('click', () => popMenu(size, [
            ...PAGE_SIZES.map((n) => [`${fmtRows(n)} rows`, () => { writeStore(PAGE_KEY, String(n)); changed(); }, n === cur ? 'on' : 'off']),
            null,
            ['Custom…', () => {
                    const inp = h('input', { type: 'number', min: '1', max: String(PAGE_MAX), value: String(cur) });
                    const ok = () => {
                        const n = Math.floor(Number(inp.value));
                        if (n >= 1 && n <= PAGE_MAX) {
                            writeStore(PAGE_KEY, String(n));
                            deps.closeModal();
                            changed();
                            return;
                        }
                        inp.setCustomValidity(`A number from 1 to ${fmtRows(PAGE_MAX)}`); // never a silent no-op on Apply
                        inp.reportValidity();
                    };
                    inp.addEventListener('input', () => inp.setCustomValidity(''));
                    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter')
                        ok(); });
                    deps.modal('Rows per page', h('p', { class: 'mut' }, `1 – ${fmtRows(PAGE_MAX)}. A page past what piperd holds runs the statement again.`), inp, h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), h('button', { class: 'primary', onclick: ok }, 'Apply')));
                    setTimeout(() => inp.select(), 0);
                }]
        ]));
        return h('span', { class: 'db-page' }, h('button', { class: 'icon', title: offset ? `the previous ${fmtRows(cur)} rows` : 'the first page', 'aria-label': 'previous page', disabled: !offset || busy, onclick: () => go(offset - cur) }, fi('chev-l')), size, h('button', { class: 'icon', title: more ? `the next ${fmtRows(cur)} rows` : 'no more rows', 'aria-label': 'next page', disabled: !more || busy, onclick: () => go(offset + cur) }, fi('chev-r')));
    }
    function pendingCount(t) {
        let n = t.deleted.size + t.inserted.length;
        for (const [r, m] of t.edits)
            if (!t.deleted.has(r))
                n += m.size;
        return n;
    }
    function pendingStatements(t) {
        const c = curConn(t.conn);
        return changeStatements(c.engine, null, t.table, t.cols.map((x) => x.name), t.info?.primary_key ?? [], t.rows, t.edits, t.deleted, t.inserted);
    }
    async function submit(t) {
        const c = curConn(t.conn);
        if (!c || !pendingCount(t))
            return;
        let stmts;
        try {
            stmts = pendingStatements(t);
        }
        catch (e) {
            deps.toast(String(e));
            return;
        }
        const keys = bodyEl.contains(document.activeElement); // ⌘S from the grid: the keyboard stays there after the redraw
        const run = async (confirm) => {
            if (saving.has(t.id))
                return; // one Submit at a time
            saving.add(t.id);
            try {
                const { data } = await dbApi.execute({ project: pj(t.conn), connection: t.conn, statements: stmts, schema: t.schema, session, confirm });
                deps.toast(`Saved: ${rowsText((data?.rowcount ?? []).reduce((a, b) => a + (b > 0 ? b : 0), 0))} changed`);
                // only edits: the rows keep their place (ordered by the key) - the cursor stays on the cell it edited,
                // so the next ⌘⌥N / ⌘⌫ never lands on row 1; deleted / new rows shift the rows: start over
                const keep = !t.deleted.size && !t.inserted.length && t.sel && t.sel.r >= 0 ? t.sel : null;
                t.edits.clear(); // committed: a failing reload must not leave them for a second Submit
                t.deleted.clear();
                t.inserted = [];
                t.sel = null;
                await loadTable(t);
                if (keep && keep.r < t.rows.length && keep.c < t.cols.length) {
                    t.sel = keep;
                    draw();
                }
            }
            catch (e) {
                deps.toast('Not saved: ' + errText(e));
            }
            finally {
                saving.delete(t.id);
                if (keys)
                    setTimeout(focusGrid, 0);
            }
        };
        if (c.class === 'prod') {
            deps.modal('Change PRODUCTION data?', h('p', {}, classBadge('prod'), ` ${c.label ?? c.name} - ${stmts.length} ${stmts.length === 1 ? 'statement' : 'statements'} in one transaction:`), h('pre', { class: 'db-sqlbox' }, stmts.map((x) => showStatement(x, c.engine)).join(';\n') + ';'), h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), h('button', { class: 'danger', onclick: () => { deps.closeModal(); void run('prod'); } }, 'Change production')));
            return;
        }
        await run('');
    }
    function revert(t) {
        if (t.sel && t.sel.r < 0)
            t.sel = null; // the new rows are gone
        t.edits.clear();
        t.deleted.clear();
        t.inserted = [];
        t.editing = null;
        liveEdit = null;
        draw();
    }
    // ---------------------------------------------------------------- console
    async function runConsole(t, explain = false) {
        if (shared(t.conn) && joined(t).some((other) => other.running))
            return void deps.toast('Wait for the shared transaction to finish its running statement.');
        const ta = bodyEl.querySelector('.db-editor textarea');
        const typing = !!ta && document.activeElement === ta; // ⌘↩ then ⌘⇧↩: the redraw must not take the keyboard away
        const selStart = ta?.selectionStart ?? t.caret;
        const selEnd = ta?.selectionEnd ?? t.caret;
        const sql = selEnd > selStart ? t.text.slice(selStart, selEnd).trim().replace(/;\s*$/, '') : statementAt(t.text, selStart).sql.replace(/;\s*$/, '');
        if (!sql)
            return;
        const c = curConn(t.conn);
        if (!c)
            return;
        const pane = { title: (explain ? 'Explain: ' : '') + sql.replace(/\s+/g, ' ').slice(0, 40), res: null, rows: [], error: '', note: '', ms: 0 };
        const doRun = async (confirm) => {
            t.running = true;
            draw();
            const t0 = Date.now();
            try {
                if (t.mode === 'rw' && !explain) {
                    if (t.tx?.open) {
                        const state = (await dbApi.tx({ project: pj(t.conn), connection: t.conn, op: 'state', session, tx: txId(t) })).data;
                        if (!state?.tx_open) {
                            t.tx = null;
                            syncTx(t);
                            forgetTx(txId(t));
                            throw new Error('The previous transaction ended and was rolled back. This statement did not run; check the data before starting again.');
                        }
                        if (state && 'schema' in state) {
                            t.schema = state.schema ?? '';
                            syncTx(t);
                        }
                    }
                    const { data } = await dbApi.tx({ project: pj(t.conn), connection: t.conn, op: 'exec', statements: [{ sql }], schema: t.schema, session, confirm, tx: txId(t),
                        max_rows: pageSize(), request_tag: viewTx + t.id });
                    const r = data?.results[0];
                    if (r?.columns) {
                        pane.res = { result_id: '', columns: r.columns, rows: r.rows ?? [], offset: 0, row_count: (r.rows ?? []).length, more: !!r.truncated };
                        pane.rows = r.rows ?? [];
                        if (r.kind === 'select') {
                            pane.sql = sql;
                            pane.tx = true;
                        } // piperd's kind: a SELECT may run again for its next page, a write (CTE DML with RETURNING too) never
                    }
                    else {
                        pane.note = `${rowsText(Math.max(0, r?.rowcount ?? 0))} affected · not committed`;
                        pane.affected = Math.max(0, r?.rowcount ?? 0);
                        const smp = data?.sample;
                        if (smp && smp.before.rows.length) { // old -> new under the banner (S2)
                            pane.res = { result_id: '', columns: smp.before.columns, rows: smp.before.rows, offset: 0, row_count: smp.before.rows.length, more: !!smp.before.more };
                            pane.rows = smp.before.rows;
                            pane.deleted = !smp.after;
                            pane.changed = new Map();
                            (smp.after?.rows ?? []).forEach((ar, i) => {
                                const m = new Map();
                                ar.forEach((v, j) => { if (cellText(v) !== cellText(smp.before.rows[i]?.[j]))
                                    m.set(j, v); });
                                if (m.size)
                                    pane.changed.set(i, m);
                            });
                        }
                        const m = /^\s*(update|delete\s+from|insert\s+into|replace\s+into)\s+([`"\w.]+)/i.exec(sql);
                        pane.title = m ? `${m[1].split(/\s+/)[0].toUpperCase()} ${m[2].replace(/[`"]/g, '')}` : sql.replace(/\s+/g, ' ').slice(0, 30);
                        if (t.tx)
                            t.tx.affected += Math.max(0, r?.rowcount ?? 0);
                    }
                    t.tx = data?.tx_open ? { open: true, since: data.since, affected: t.tx?.affected ?? (r?.columns ? 0 : Math.max(0, r?.rowcount ?? 0)) } : null; // a SELECT's rows changed nothing
                    if (data?.tx_open && 'schema' in data)
                        t.schema = data.schema ?? '';
                    syncTx(t);
                    if (t.tx)
                        rememberTx({ project: pj(t.conn), connection: t.conn, tx: txId(t), session });
                    else
                        forgetTx(txId(t));
                }
                else {
                    const { data } = await dbApi.query({ project: pj(t.conn), connection: t.conn, sql, schema: t.schema, session, explain, max_rows: pageSize(), request_tag: viewTx + t.id });
                    pane.res = data;
                    pane.rows = data?.rows ?? [];
                    pane.sql = sql;
                    pane.explain = explain;
                }
            }
            catch (e) {
                pane.error = errText(e);
                // Even the first failed statement can leave a transaction open; a dropped connection can end one.
                // Ask in both cases so the real worker state always has a visible Commit / Rollback and reload cleanup.
                if (t.mode === 'rw' && !explain) {
                    try {
                        const { data } = await dbApi.tx({ project: pj(t.conn), connection: t.conn, op: 'state', session, tx: txId(t) });
                        t.tx = data?.tx_open ? t.tx ?? { open: true, since: data.since, affected: 0 } : null;
                        if (data?.tx_open && 'schema' in data)
                            t.schema = data.schema ?? '';
                        syncTx(t);
                        if (t.tx)
                            rememberTx({ project: pj(t.conn), connection: t.conn, tx: txId(t), session });
                        else
                            forgetTx(txId(t));
                    }
                    catch { /* unknown: keep what the console knows */ }
                }
            }
            pane.ms = Date.now() - t0;
            t.results.push(pane);
            if (t.results.length > 8)
                t.results.shift();
            t.active = t.results.length - 1;
            t.running = false;
            draw();
            if (typing)
                refocusEditor();
            void loadHistory();
        };
        const writes = !/^\s*\(*\s*(select|with|show|describe|desc|explain|table|values|help)\b/i.test(sql);
        if (t.mode === 'rw' && c.class === 'prod' && writes && !explain) {
            deps.modal('Run on PRODUCTION?', h('p', {}, classBadge('prod'), ` ${c.label ?? c.name} - inside the open transaction (Commit asks again):`), h('pre', { class: 'db-sqlbox' }, sql), h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), h('button', { class: 'danger', onclick: () => { deps.closeModal(); void doRun('prod'); } }, 'Run on production')));
            return;
        }
        await doRun('');
    }
    async function txEnd(t, op) {
        if (joined(t).some((other) => other.running))
            return void deps.toast('Wait for the running statement before ending the transaction.');
        const c = curConn(t.conn);
        const typing = document.activeElement === bodyEl.querySelector('.db-editor textarea');
        const go = async (confirm) => {
            try {
                const { data } = await dbApi.tx({ project: pj(t.conn), connection: t.conn, op, session, confirm, tx: txId(t) });
                t.tx = null;
                syncTx(t);
                forgetTx(txId(t));
                const lost = op === 'commit' && data?.committed === false; // the transaction had ended (its connection went away)
                t.results.push({ title: op === 'commit' ? (lost ? 'Not committed' : 'Committed') : 'Rolled back', res: null, rows: [], error: lost ? 'Nothing was committed - the transaction had already ended (rolled back by the server). Run the statements again.' : '',
                    note: op === 'commit' ? 'Transaction committed' : 'Transaction rolled back - nothing changed', ms: 0 });
                t.active = t.results.length - 1;
            }
            catch (e) {
                deps.toast(errText(e));
            }
            draw();
            if (typing)
                refocusEditor();
            void loadHistory();
        };
        if (op === 'commit' && c?.class === 'prod') {
            deps.modal('Commit on PRODUCTION?', h('p', {}, classBadge('prod'), ` ${shared(t.conn) ? `Shared transaction · ${joined(t).length} consoles · ` : ''}${rowsText(t.tx?.affected ?? 0)} changed in this transaction.`), h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), h('button', { class: 'danger', onclick: () => { deps.closeModal(); void go('prod'); } }, 'Commit to production')));
            return;
        }
        await go('');
    }
    /** the console's editor gets the keyboard back after a redraw (its caret is restored by drawBody) */
    function refocusEditor() {
        setTimeout(() => bodyEl.querySelector('.db-editor textarea')?.focus(), 0);
    }
    // ---------------------------------------------------------------- drawing
    function draw() {
        el.classList.toggle('wide', !conn); // the overview of every database takes the whole width (S4)
        if (!connTabs().length)
            treeOn = false; // no tab: the tree shows anyway
        el.classList.toggle('db-treeon', treeOn && !!conn);
        writeStore(connKey, conn);
        drawLeft();
        drawTabs();
        drawBody();
        drawRight();
    }
    const SYS_SCHEMAS = new Set(['information_schema', 'performance_schema', 'mysql', 'sys', 'pg_catalog', 'pg_toast']);
    const short = (c) => c.label || c.name.replace(c.project + '/', '').replace(/\//g, ' / ');
    /** "dev / shop_live · shop_live" - the connection as the header and the status bar name it */
    function connPath(c, schema = schemaOf.get(c.name) ?? c.database) {
        const n = short(c);
        return `${n}${schema && !n.endsWith(schema) ? ' · ' + schema : ''}`; // dev / shop_live, not dev / shop_live · shop_live
    }
    function connMeta(c) {
        const o = objects.get(c.name + '|' + (schemaOf.get(c.name) ?? '')) ?? objects.get(c.name + '|');
        const up = !o ? '' : !('error' in o) ? ' running' : /Connection refused|Can't connect|could not connect|ECONNREFUSED/i.test(o.error) ? ' not running' : '';
        const where = c.reach === 'stack' ? `${c.host ?? 'local stack'} · ${session && c.stack !== 'main' ? 'this session\'s checkout' : 'main checkout'} · stack${up}`
            : c.reach === 'tunnel' ? `tunnel ${c.tunnel?.env}/${c.tunnel?.target} · ${c.tunnel?.state ?? 'closed'}`
                : c.reach === 'ssh' ? `ssh ${c.tunnel?.target} → ${c.host ?? ''} · ${c.tunnel?.state ?? 'closed'}`
                    : c.reach === 'direct' ? `direct ${c.host ?? ''}` : 'needs a tunnel target';
        return `${where}${c.note ? ' · ' + c.note : ''}`;
    }
    /** a state of the connection (S5 "States the grid and tree must show"): icon, title, text, actions */
    function stateCard(icon, title, text, ...actions) {
        const acts = actions.filter((x) => !!x);
        return h('div', { class: 'db-state db-state-' + icon }, h('div', { class: 'db-state-h' }, ic(icon), h('strong', {}, title)), h('div', { class: 'db-state-t' }, text), acts.length ? h('div', { class: 'db-state-a' }, ...acts) : null);
    }
    /** a dropdown (W11: the one menu component): an item without an action is a group label, cls 'danger' / 'on' / 'off' */
    function popMenu(anchor, items) {
        openMenu(anchor, items.map((it) => {
            if (!it)
                return null;
            const [label, act, cls = '', search] = it;
            if (!act)
                return { group: labelText(label) };
            return { label, run: act, danger: cls === 'danger', on: cls === 'on' ? true : cls === 'off' ? false : undefined, search };
        }), { label: anchor instanceof HTMLElement ? (anchor.getAttribute('aria-label') || anchor.textContent?.trim() || 'menu') : 'menu' });
    }
    /** Switch ▾: every connection of every project, grouped, plus the overview (S4) */
    function switchMenu(anchor) {
        const items = [['All databases · overview', () => openOverview()], null];
        for (const g of all?.projects ?? []) {
            items.push([g.title, null]);
            for (const c of g.connections) {
                items.push([h('span', { class: 'db-mi-c' }, classBadge(c.class), h('span', { class: 'db-mi-t' }, short(c)), privateMark(c), busyMark(c.name)), () => openConn(c.name), c.name === conn ? 'on' : '', short(c)]); // typing "de" finds no "dev" chip
            }
        }
        popMenu(anchor, items);
    }
    /** the left panel of S1 / S2: ONE connection - project, class, Switch ▾, its path, state and object tree */
    /** Missing drivers: the same notice in the overview and beside an opened connection. */
    function driversWarn(st) {
        return h('div', { class: 'db-warn' }, h('div', {}, st.installing ? 'Installing database drivers…' : 'The database drivers are not installed - nothing can be queried until they are.'), st.install_error ? h('pre', { class: 'small' }, st.install_error.slice(0, 400)) : null, st.installing ? null : h('button', { class: 'primary', onclick: async () => {
                try {
                    await dbApi.install(refHost(deps.scope?.() ?? ''));
                    deps.toast('Installing drivers (pip, about a minute)…');
                }
                catch (e) {
                    deps.toast(errText(e));
                }
                setTimeout(() => void load(true), 4000);
            } }, 'Install drivers'));
    }
    function drawLeft() {
        clear(leftEl);
        const c = curConn();
        if (!c)
            return; // the overview (S4) has no left panel
        const g = all?.projects.find((x) => x.project === c.project);
        const other = allConns().some((x) => x.name !== c.name && busy(x.name));
        const sw = h('button', { class: 'db-switch', title: 'switch the connection - every database of every project' + (other ? '\n● another connection has an open transaction or unsaved changes' : '') }, 'Switch ', other ? h('span', { class: 'db-dirty' }, '●') : null, fi('chev-d', 'db-caret'));
        sw.addEventListener('click', () => switchMenu(sw));
        leftEl.append(h('div', { class: 'db-lhead' }, h('span', { class: 'db-pchip', title: g?.root ?? c.project }, g?.title ?? c.project), classBadge(c.class), h('span', { class: 'grow' }), sw));
        const schs = (schemas.get(c.name) ?? []).filter((x) => !SYS_SCHEMAS.has(x));
        const cur = schemaOf.get(c.name) ?? c.database;
        const n = short(c);
        const path = h('div', { class: 'db-cpath', title: c.name }, n, cur && (schs.length > 1 || !n.endsWith(cur)) ? ' · ' : '');
        if (schs.length > 1) {
            const sel = h('select', { class: 'db-schsel', title: c.engine === 'mysql' ? 'database' : 'schema', onchange: (e) => {
                    schemaOf.set(c.name, e.target.value);
                    void loadObjects(c.name);
                    draw();
                } });
            for (const x of schs)
                sel.append(h('option', { value: x, selected: x === cur }, x));
            path.append(sel);
        }
        else if (cur && !n.endsWith(cur))
            path.append(cur);
        leftEl.append(path, h('div', { class: 'db-cmeta', title: c.name }, connMeta(c)));
        if (all && !all.status.ready)
            leftEl.append(driversWarn(all.status));
        if (!c.has_password && c.reach !== 'stack') {
            leftEl.append(stateCard('key', `Password needed · ${short(c)}`, `${c.source === 'idea' ? 'Imported from IDEA' : 'Registered'} without a password. Enter it once; it is stored in Piper's home on this machine (mode 600), never in a sandbox.`, h('button', { onclick: () => setPassword(c) }, 'Enter password')));
            return;
        }
        if (c.tunnel?.state === 'connecting') {
            leftEl.append(stateCard('shield', 'Tunnel · connecting', `${c.tunnel.target} via the dev stack · ${c.tunnel.env}. The OIDC sign-in may open in your browser.`, h('button', { onclick: async () => { try {
                    await dbApi.tunnel(pj(c.name), c.name, 'close');
                }
                catch (e) {
                    deps.toast(errText(e));
                } void load(true); } }, 'Cancel')));
        }
        else if (c.tunnel?.state === 'signin') {
            leftEl.append(stateCard('shield', 'Tunnel · sign in', 'kubectl oidc-login waits for your sign-in in the browser.', c.tunnel.url ? h('a', { class: 'btn', href: c.tunnel.url, target: '_blank', rel: 'noopener' }, 'Open the sign-in page') : null));
        }
        else if (c.class === 'prod' && c.def?.agent_enabled !== false) {
            leftEl.append(stateCard('lock', 'prod · locked for Claude', 'Claude\'s first read on prod waits for your card. Your own queries in this view run at once.'));
        }
        if (c.def?.agent_enabled !== false && c.masked_for_agents && c.class !== 'dev') {
            leftEl.append(stateCard('eyeoff', 'Masked for Claude only', 'email, phone, street, password hashes… are ••••-masked in Claude\'s results. You see raw data here.'));
        }
        const find = h('input', { type: 'search', class: 'db-search', placeholder: 'Find table or column…', value: treeFilter, 'aria-label': 'find a table or column' });
        const tree = h('div', { class: 'db-tree', role: 'tree' });
        treeRedraw = () => drawTree(tree, c);
        find.addEventListener('input', () => { treeFilter = find.value; findAt = 0; drawTree(tree, c); });
        find.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault();
                findAt += e.key === 'ArrowDown' ? 1 : -1;
                drawTree(tree, c);
            }
            else if (e.key === 'Enter') {
                e.preventDefault();
                if (treeFilter.trim())
                    treeOpen?.();
                else
                    tree.querySelector('.db-ti')?.click();
            }
            else if (e.key === 'Escape') {
                find.value = '';
                treeFilter = '';
                findAt = 0;
                drawTree(tree, c);
            }
        });
        leftEl.append(h('label', { class: 'db-searchbox' }, ic('search'), find, h('span', { class: 'db-kbd' }, kbd('⌘P', 'Ctrl'))), tree);
        drawTree(tree, c);
    }
    function drawTree(tree, c) {
        clear(tree);
        const sch = schemaOf.get(c.name) ?? '';
        const o = objects.get(c.name + '|' + sch) ?? objects.get(c.name + '|');
        if (!c.has_password && c.reach !== 'stack')
            return;
        if (!o) {
            tree.append(h('p', { class: 'mut db-pad' }, h('span', { class: 'spinner' }), ' loading…'));
            return;
        }
        if ('error' in o) {
            if (c.reach === 'stack' && /Connection refused|Can't connect|could not connect|ECONNREFUSED/i.test(o.error)) {
                tree.append(stateCard('stack', 'The stack\'s database is not running', `Nothing listens on ${c.host ?? 'its port'}. Start the project's dev stack (Stack in a session's context panel, or its own start command), then refresh.`, h('button', { onclick: () => { objects.delete(c.name + '|'); schemas.delete(c.name); void loadObjects(c.name, true); } }, 'Try again')));
            }
            else if (!all || all.status.ready) { // pages polish: missing drivers are said once, by the banner above with Install drivers
                tree.append(h('div', { class: 'db-pad db-treeerr' }, h('p', { class: 'bad' }, o.error), h('button', { onclick: () => { objects.delete(c.name + '|'); schemas.delete(c.name); void loadObjects(c.name, true); } }, 'Try again')));
            }
            return;
        }
        const f = treeFilter.trim();
        const at = tab();
        const open = at && at.kind === 'table' && at.conn === c.name ? at.table : '';
        const rc = (it) => (it.rows !== null && it.rows !== undefined && it.rows >= 0 ? h('span', { class: 'db-rc' }, String(it.rows)) : null); // PostgreSQL: -1 = never analyzed
        if (f) {
            drawFound(tree, c, sch, o, f, open, rc);
            return;
        }
        const section = (title, n) => tree.append(h('div', { class: 'db-tg' }, `${title} · ${n}`));
        const tables = o.objects.filter((x) => x.kind === 'table');
        const views = o.objects.filter((x) => x.kind === 'view');
        section('Tables', tables.length);
        for (const it of tables) {
            tree.append(h('button', { class: 'db-ti' + (it.name === open ? ' on' : ''), role: 'treeitem', title: showBidi(it.comment || it.name), onclick: () => openTable(c.name, sch, it.name) }, ic('table'), h('span', { class: 'db-tn' }, showBidi(it.name)), rc(it)));
        }
        section('Views', views.length);
        for (const it of views) {
            tree.append(h('button', { class: 'db-ti' + (it.name === open ? ' on' : ''), role: 'treeitem', onclick: () => openTable(c.name, sch, it.name) }, ic('eye'), h('span', { class: 'db-tn' }, showBidi(it.name))));
        }
        section('Routines', o.routines.length);
        for (const r of o.routines)
            tree.append(h('div', { class: 'db-ti ro', title: r.kind }, ic('fn'), h('span', { class: 'db-tn' }, showBidi(r.name))));
    }
    /** the name with the matched letters in bold */
    function marked(name, hits) {
        const out = h('span', { class: 'db-tn' });
        const set = new Set(hits);
        let run = '';
        let bold = false;
        const flush = () => { if (run)
            out.append(bold ? h('b', {}, showBidi(run)) : showBidi(run)); run = ''; };
        [...name].forEach((ch, i) => { if (set.has(i) !== bold) {
            flush();
            bold = set.has(i);
        } run += ch; });
        flush();
        return out;
    }
    /** every column of the schema, for "Find table or column" (one request per connection and schema) */
    function columnIndex(c, sch) {
        const key = c.name + '|' + sch;
        const hit = colIndex.get(key);
        if (hit)
            return hit;
        colIndex.set(key, 'loading');
        void dbApi.meta(pj(c.name), c.name, 'columns', sch, '', session)
            .then(({ data }) => {
            const cols = new Map();
            for (const [t, col, type] of data?.columns ?? [])
                (cols.get(t) ?? cols.set(t, []).get(t)).push([col, type]);
            colIndex.set(key, { cols, truncated: !!data?.truncated });
        })
            .catch(() => colIndex.set(key, 'error'))
            .finally(() => { if (conn === c.name && treeFilter.trim())
            treeRedraw?.(); });
        return 'loading';
    }
    /** search results, best first: table / view names (fuzzy, initials, several words), a column of a table
     *  ("table · column"), `table.column`, routines; ↑ ↓ pick, Enter opens */
    function drawFound(tree, c, sch, o, f, open, rc) {
        const ix = columnIndex(c, sch);
        const cols = typeof ix === 'object' ? ix.cols : null;
        const dot = f.indexOf('.');
        const tq = dot >= 0 ? f.slice(0, dot) : f;
        const cq = dot >= 0 ? f.slice(dot + 1) : '';
        const found = [];
        for (const it of o.objects) {
            const tm = tq ? fuzzyMatch(tq, it.name) : { score: 0, hits: [] };
            let col = null;
            const best = (q) => {
                for (const [name, type] of cols?.get(it.name) ?? []) {
                    const m = q ? fuzzyMatch(q, name) : null;
                    if (m && (!col || m.score > col.score))
                        col = { name, type, hits: m.hits, score: m.score };
                }
            };
            let score;
            let tHits = [];
            if (dot >= 0) {
                if (!tm)
                    continue;
                if (cq) {
                    best(cq);
                    if (!col)
                        continue;
                }
                score = tm.score + (col ? col.score : 0);
            }
            else if (tm)
                score = tm.score + 1000; // a name match always beats a column match
            else {
                // the words may split between the table and a column: "order pay" = shop_payment · order_id
                let bestT = [];
                for (const [name, type] of cols?.get(it.name) ?? []) {
                    const m = fuzzyMatch(f, `${it.name}.${name}`);
                    if (m && (!col || m.score > col.score)) {
                        col = { name, type, hits: m.hits.filter((x) => x > it.name.length).map((x) => x - it.name.length - 1), score: m.score };
                        bestT = m.hits.filter((x) => x < it.name.length);
                    }
                }
                if (!col)
                    continue;
                score = col.score;
                tHits = bestT;
            }
            const cc = col;
            const go = () => openTable(c.name, sch, it.name);
            const th = tm && tq ? tm.hits : tHits;
            found.push({ score, open: go, node: h('button', { class: 'db-ti' + (it.name === open ? ' on' : ''), role: 'treeitem', title: showBidi((it.comment || it.name) + (cc ? `\ncolumn ${cc.name} · ${cc.type}` : '')), onclick: go }, ic(it.kind === 'view' ? 'eye' : 'table'), marked(it.name, th), cc ? h('span', { class: 'db-tcol' }, '· ', marked(cc.name, cc.hits)) : null, cc ? null : rc(it)) });
        }
        if (dot < 0) {
            for (const r of o.routines) {
                const m = fuzzyMatch(f, r.name);
                if (m)
                    found.push({ score: m.score + 500, node: h('div', { class: 'db-ti ro', title: r.kind }, ic('fn'), marked(r.name, m.hits)) });
            }
        }
        found.sort((x, y) => y.score - x.score);
        const shown = found.slice(0, 200);
        const openable = shown.filter((x) => x.open);
        findAt = Math.max(0, Math.min(findAt, openable.length - 1));
        tree.append(h('div', { class: 'db-tg' }, `${found.length} match${found.length === 1 ? '' : 'es'}`, ix === 'loading' ? h('span', { class: 'db-tgn' }, ' · reading columns…') : null, typeof ix === 'object' && ix.truncated ? h('span', { class: 'db-tgn' }, ' · columns cut') : null));
        for (const x of shown) {
            if (x.open && openable[findAt] === x)
                x.node.classList.add('act');
            tree.append(x.node);
        }
        if (!found.length)
            tree.append(h('p', { class: 'mut db-pad' }, ix === 'loading' ? 'no table matches - reading the columns…' : 'nothing matches · words in any order, initials (eoi), table.column'));
        treeOpen = () => openable[findAt]?.open?.();
        tree.querySelector('.db-ti.act')?.scrollIntoView({ block: 'nearest' });
    }
    function drawTabs() {
        clear(tabsEl);
        tabsEl.classList.toggle('hidden', !conn);
        if (!conn)
            return;
        // shown in a narrow grid pane and (N10) the Database view ≤ 640 px (app.css; hidden otherwise)
        if (connTabs().length)
            tabsEl.append(h('button', { class: 'db-tab-tree' + (treeOn ? ' on' : ''), 'aria-pressed': String(treeOn),
                title: treeOn ? 'back to the open tab (Esc)' : 'the tables of this connection - a click opens one', onclick: () => {
                    treeOn = !treeOn;
                    draw();
                    if (treeOn)
                        setTimeout(() => leftEl.querySelector('.db-search')?.focus(), 0);
                } }, '▤ Tables'));
        let n = 0;
        for (const t of connTabs()) {
            if (t.kind === 'console')
                n++;
            const c = curConn(t.conn);
            const label = t.kind === 'table' ? showBidi(t.table) : `Console ${n}`;
            const dirty = t.kind === 'table' && pendingCount(t) > 0;
            tabsEl.append(h('div', { class: 'db-tab' + (t.id === activeTab ? ' on' : '') + (c ? ' db-tab-' + c.class : ''), role: 'tab', 'aria-selected': String(t.id === activeTab),
                title: c ? `${CLS_LABEL[c.class]} · ${c.name}` : '' }, h('button', { class: 'db-tab-b', onclick: () => activate(t.id) }, label, t.kind === 'console' && t.mode === 'rw' && shared(t.conn) ? fi('link') : null, dirty ? h('span', { class: 'db-dirty', title: 'unsaved changes' }, '●') : null, t.kind === 'console' && t.tx?.open ? h('span', { class: 'db-dirty', title: 'open transaction' }, '●') : null), h('button', { class: 'db-tab-x', title: 'close', 'aria-label': 'close tab', onclick: () => closeTab(t.id) }, fi('x'))));
        }
        tabsEl.append(h('button', { class: 'db-tab-new', title: kbd('new SQL console (⌘⇧L)', 'Ctrl'), 'aria-label': 'new console', onclick: () => openConsole() }, fi('plus')));
    }
    function drawBody() {
        clear(bodyEl);
        if (!conn) {
            drawConnections();
            return;
        }
        const t = tab();
        if (!t) {
            const c = curConn();
            bodyEl.append(h('div', { class: 'db-empty' }, ic('db'), h('h2', {}, c ? connPath(c) : 'Database'), h('p', { class: 'mut' }, 'Open a table from the tree, or a console. Use a console for SQL; agent queries appear on the right when agent access is enabled.'), h('div', { class: 'row' }, h('button', { class: 'primary', onclick: () => openConsole() }, kbd('New console  ⌘⇧L', 'Ctrl')), h('button', { onclick: openOverview }, 'All databases'))));
            return;
        }
        if (t.kind === 'table')
            drawTable(t);
        else
            drawConsole(t);
    }
    // ---------------------------------------------------------------- the grid
    /** column widths from the header and the first rows (monospace 12px) */
    function widths(cols, rows, marks) {
        return cols.map((c, j) => {
            let w = c.name.length + marks(c);
            for (let i = 0; i < rows.length; i += i < 300 ? 1 : Math.max(1, Math.floor(rows.length / 300)))
                w = Math.max(w, Math.min(48, cellText(rows[i][j]).length));
            return Math.round(Math.min(400, Math.max(56, w * 7.9 + 26)));
        });
    }
    /** the cell editor: Enter / Tab keep the value, Esc drops it, a click elsewhere keeps it */
    function editorInput(t, r, j, v, name) {
        if (liveEdit && liveEdit.tab === t.id && liveEdit.r === r && liveEdit.c === j)
            return liveEdit.inp;
        const seed = editSeed;
        editSeed = null;
        const blank = v === null || sqlKeyword(v) !== null;
        const inp = h('input', { class: 'db-cedit', value: seed ?? (blank ? '' : cellText(v)), placeholder: sqlKeyword(v) ?? 'NULL', 'aria-label': name, spellcheck: false });
        inp.dataset.orig = blank ? '\u0000' : cellText(v);
        inp.addEventListener('keydown', (e) => {
            e.stopPropagation();
            if (e.key === 'Enter') {
                e.preventDefault();
                commitEdit(t, e.shiftKey && !inp.value ? null : undefined);
                moveSel(t, 1, 0);
            }
            else if (e.key === 'Tab') {
                e.preventDefault();
                commitEdit(t);
                moveSel(t, 0, e.shiftKey ? -1 : 1);
            }
            else if (e.key === 'Escape') {
                e.preventDefault();
                t.editing = null;
                liveEdit = null;
                drawBody();
                focusGrid();
            }
            else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's') {
                e.preventDefault();
                commitEdit(t);
                focusGrid();
                void submit(t);
            }
        });
        // a repaint detaches the input and puts it back focused; only a real blur (focus went elsewhere) keeps the value
        inp.addEventListener('blur', () => setTimeout(() => {
            if (liveEdit?.inp === inp && inp.isConnected && document.activeElement !== inp && t.editing?.r === r && t.editing.c === j)
                commitEdit(t);
        }, 0));
        setTimeout(() => {
            inp.focus({ preventScroll: true });
            if (seed === null)
                inp.select();
            else
                inp.setSelectionRange(inp.value.length, inp.value.length);
        }, 0);
        liveEdit = { tab: t.id, r, c: j, inp };
        return inp;
    }
    /** the edited value goes into the pending changes (null: Shift+Enter on an empty cell = NULL) */
    function commitEdit(t, forced) {
        const ed = t.editing;
        if (!ed)
            return;
        const inp = liveEdit && liveEdit.tab === t.id ? liveEdit.inp : null;
        t.editing = null;
        liveEdit = null;
        if (!inp) {
            drawBody();
            return;
        }
        const wasNull = inp.dataset.orig === '\u0000';
        if (ed.r < 0 && forced !== null && inp.value === '' && wasNull) {
            drawBody();
            return;
        } // a new row's untouched cell keeps the column default
        setCell(t, ed.r, ed.c, forced === null ? null : inp.value === '' && wasNull ? null : coerceEdit(t.cols[ed.c]?.type, inp.value));
    }
    /** start editing the selected cell; `seed` = the typed key that replaces the value */
    function startEdit(t, seed = null) {
        if (!t.sel)
            return;
        t.editing = { ...t.sel };
        editSeed = seed;
        liveEdit = null;
        drawBody();
    }
    function focusGrid() {
        bodyEl.querySelector('.db-grid')?.focus({ preventScroll: true });
    }
    /** the value a cell shows now (a pending edit, a new row) */
    function cellNow(t, rows, r, j) {
        if (t && r < 0)
            return t.inserted[-1 - r]?.get(j) ?? null;
        const m = t?.edits.get(r);
        return m?.has(j) ? m.get(j) : rows[r]?.[j];
    }
    /** right click on a cell - IntelliJ's data editor menu: edit, Set NULL / DEFAULT / now, revert, clone / delete the
     *  row, copy (value, row, INSERT, column name), filter by the value, sort, follow the FK, hide the column */
    function cellMenu(at, cols, rows, r, j, info, t, editable) {
        const col = cols[j];
        if (!col)
            return;
        const mi = (text, hint = '') => h('span', { class: 'db-mi-c' }, h('span', { class: 'db-mi-t' }, showBidi(text)), hint ? h('span', { class: 'db-mi-d' }, kbd(hint, 'Ctrl')) : null);
        const ci = info?.columns.find((x) => x.name === col.name);
        const v = cellNow(t, rows, r, j);
        const row = cols.map((_, k) => cellNow(t, rows, r, k));
        const c = t ? curConn(t.conn) : null;
        const eng = c?.engine ?? 'mysql';
        const copy = async (text, what) => { await navigator.clipboard?.writeText(text); deps.toast(`Copied ${what}`); };
        const items = [];
        if (t && editable) {
            const edited = r >= 0 && !!t.edits.get(r)?.has(j);
            const rowEdited = r >= 0 && (t.edits.has(r) || t.deleted.has(r));
            items.push([mi('Edit', '↵'), () => startEdit(t)]);
            if (ci?.nullable !== false)
                items.push([mi('Set NULL', '⌘⌥N'), () => setCell(t, r, j, null)]);
            items.push([mi('Set DEFAULT', ci?.default !== null && ci?.default !== undefined ? String(ci.default).slice(0, 24) : ci?.extra || ''), () => setCell(t, r, j, SQL_DEFAULT)]);
            if (isTemporal(ci?.type ?? col.type))
                items.push([mi('Set now', sqlNow(eng, ci?.type ?? col.type).sqlNow), () => setCell(t, r, j, sqlNow(eng, ci?.type ?? col.type))]);
            if (edited)
                items.push([mi('Revert cell'), () => { setCell(t, r, j, rows[r][j]); }]);
            if (rowEdited)
                items.push([mi('Revert row'), () => { t.edits.delete(r); t.deleted.delete(r); drawBody(); drawTabs(); }]);
            items.push(null);
            const pk = new Set(info?.primary_key ?? []);
            items.push([mi('Clone row'), () => {
                    // a new row with the same values - except the primary key and auto-generated columns
                    const auto = new Set((info?.columns ?? []).filter((x) => /auto_increment|identity|nextval/i.test(`${x.extra} ${x.default ?? ''}`)).map((x) => x.name));
                    t.inserted.push(new Map(cols.map((_, k) => [k, row[k]]).filter(([k, val]) => !pk.has(cols[k].name) && !auto.has(cols[k].name) && val !== null)));
                    t.sel = { r: -t.inserted.length, c: j };
                    draw();
                }]);
            if (r < 0)
                items.push([mi('Remove the new row'), () => { t.inserted.splice(-1 - r, 1); t.sel = null; draw(); }, 'danger']);
            else
                items.push([mi(t.deleted.has(r) ? 'Undelete row' : 'Delete row', '⌘⌫'), () => { if (t.deleted.has(r))
                        t.deleted.delete(r);
                    else
                        t.deleted.add(r); draw(); }, t.deleted.has(r) ? '' : 'danger']);
            items.push(null);
        }
        items.push([mi('Copy value', '⌘C'), () => void copy(v === null || v === undefined ? '' : cellText(v), 'the value')]);
        items.push([mi('Copy row'), () => void copy(row.map((x) => (x === null || x === undefined ? '' : cellText(x))).join('\t'), 'the row (TSV)')]);
        if (t)
            items.push([mi('Copy row as SQL INSERT'), () => void copy(toInserts(eng, t.schema || null, t.table, cols.map((x) => x.name), [row]), 'as INSERT')]);
        items.push([mi('Copy column name'), () => void copy(col.name, col.name)]);
        if (t && r >= 0) {
            const q = quoteIdent(col.name, eng);
            const and = (cond) => reloadTable(t, () => { t.where = t.where.trim() ? `(${t.where.trim()}) AND ${cond}` : cond; });
            items.push(null);
            items.push([mi(v === null ? `Filter: ${col.name} IS NULL` : `Filter: ${col.name} = ${sqlLiteral(v, eng).slice(0, 30)}`), () => and(v === null ? `${q} IS NULL` : `${q} = ${sqlLiteral(v, eng)}`)]);
            items.push([mi(v === null ? `Filter: ${col.name} IS NOT NULL` : `Filter: ${col.name} <> ${sqlLiteral(v, eng).slice(0, 30)}`), () => and(v === null ? `${q} IS NOT NULL` : `${q} <> ${sqlLiteral(v, eng)}`)]);
            if (t.where)
                items.push([mi('Clear the filter'), () => reloadTable(t, () => { t.where = ''; })]);
            items.push([mi(`Sort by ${col.name} ↑`), () => reloadTable(t, () => { t.order = col.name; t.desc = false; })]);
            items.push([mi(`Sort by ${col.name} ↓`), () => reloadTable(t, () => { t.order = col.name; t.desc = true; })]);
            const fk = info?.foreign_keys.find((x) => x.column === col.name);
            if (fk && v !== null && v !== undefined)
                items.push([mi(`Open ${fk.ref_table} row`, '›'), () => openTable(t.conn, fk.ref_schema || t.schema, fk.ref_table, `${quoteIdent(fk.ref_column, eng)} = ${sqlLiteral(v, eng)}`)]);
            if (t.hidden.size < t.cols.length - 1)
                items.push([mi(`Hide ${col.name}`), () => { t.hidden.add(col.name); drawBody(); }]);
        }
        popMenu(at, items);
    }
    function grid(cols, rows, opts) {
        const t = opts.t;
        const fkOf = new Map((opts.info?.foreign_keys ?? []).map((f) => [f.column, f]));
        const pk = new Set(opts.info?.primary_key ?? []);
        const vis = cols.map((c, j) => [c, j]).filter(([c]) => !opts.hidden?.has(c.name));
        // edited values count too: a longer new value must not be cut off in the cell it was typed into
        const edited = t ? [...t.edits.values()].map((m) => cols.map((_, j) => (m.has(j) ? m.get(j) : ''))) : [];
        const allW = widths(cols, rows.concat(edited, opts.extra ?? []), (c) => (pk.has(c.name) || fkOf.has(c.name) ? 2 : 0));
        const template = vis.map(([, j]) => allW[j] + 'px').join(' ') + ' minmax(0, 1fr)';
        const wrap = h('div', { class: 'db-grid', tabindex: '0' });
        let inp = null; // the open cell editor - kept across repaints (scroll, resize)
        const head = h('div', { class: 'db-gh' });
        head.style.gridTemplateColumns = template;
        for (const [c] of vis) {
            const info = opts.info?.columns.find((x) => x.name === c.name);
            const fk = fkOf.get(c.name);
            head.append(h('div', { class: 'db-gc db-ghc' + (opts.sort === c.name ? ' sorted' : ''), title: showBidi(`${c.name} · ${info?.type ?? c.type}${info?.nullable === false ? ' · NOT NULL' : ''}${fk ? ` · → ${fk.ref_table}.${fk.ref_column}` : ''}${info?.comment ? '\n' + info.comment : ''}${opts.onSort ? '\nclick sorts' : ''}`),
                onclick: opts.onSort ? () => opts.onSort(c.name) : undefined }, h('span', { class: 'db-ghn' }, showBidi(c.name)), pk.has(c.name) ? ic('key', 'db-pk') : fk ? ic('link', 'db-fkic') : null, opts.sort === c.name ? h('span', { class: 'db-sort' }, opts.desc ? '↓' : '↑') : null));
        }
        head.append(h('div', { class: 'db-gc' }));
        const find = (t?.find ?? '').toLowerCase();
        const idx = [];
        rows.forEach((r, i) => { if (!find || r.some((v) => cellText(v).toLowerCase().includes(find)))
            idx.push(i); });
        const extra = t ? t.inserted.length : 0;
        const total = idx.length + extra;
        const viewport = h('div', { class: 'db-gv' });
        const spacer = h('div', { class: 'db-gsp' });
        spacer.style.height = `${total * ROW_H}px`;
        const layer = h('div', { class: 'db-gl' });
        spacer.append(layer);
        viewport.append(head, spacer);
        wrap.append(viewport);
        if (!total)
            wrap.append(h('div', { class: 'db-gempty mut' }, find ? 'no row matches' : 'no rows'));
        const paint = () => {
            const hadFocus = !!inp && document.activeElement === inp;
            clear(layer);
            const top = viewport.scrollTop;
            const first = Math.max(0, Math.floor(top / ROW_H) - 10);
            const last = Math.min(total, Math.ceil((top + (viewport.clientHeight || 1200)) / ROW_H) + 10); // not laid out yet: a screenful
            layer.style.transform = `translateY(${first * ROW_H}px)`;
            for (let k = first; k < last; k++) {
                const isNew = k >= idx.length;
                const r = isNew ? -1 - (k - idx.length) : idx[k];
                const row = isNew ? cols.map((_, j) => t.inserted[k - idx.length].get(j) ?? null) : rows[r];
                const edits = t && !isNew ? t.edits.get(r) : undefined;
                const del = !!t && !isNew && t.deleted.has(r);
                const selRow = !!t?.sel && t.sel.r === r;
                const line = h('div', { class: 'db-gr' + (del ? ' del' : '') + (isNew ? ' new' : '') + (selRow ? ' selrow' : '') });
                line.style.gridTemplateColumns = template;
                for (const [col, j] of vis) {
                    const v0 = row[j];
                    const edited = !!edits?.has(j) || (isNew && v0 !== null);
                    const v = edits?.has(j) ? edits.get(j) : v0;
                    const sel = selRow && t.sel.c === j;
                    const cell = h('div', { class: 'db-gc' + (v === null || sqlKeyword(v) !== null ? ' null' : '') + (edited ? ' edited' : '') + (sel ? ' sel' : ''),
                        title: v === null ? 'NULL' : cellText(v).length > 40 ? cellShown(v, 2000) : '' });
                    cell.dataset.r = String(r);
                    cell.dataset.c = String(j);
                    if (t?.editing && t.editing.r === r && t.editing.c === j && opts.editable) {
                        if (!inp)
                            inp = editorInput(t, r, j, v, col.name);
                        cell.classList.add('editing');
                        cell.append(inp);
                    }
                    else {
                        const fk = fkOf.get(col.name);
                        const custom = opts.mark?.(r, j, v);
                        if (custom)
                            cell.append(custom);
                        else if (fk && v !== null && !isNew) {
                            cell.classList.add('fk');
                            cell.append(h('span', { class: 'db-fkv' }, cellShown(v, 200)), h('button', { class: 'db-fklink', title: showBidi(`open ${fk.ref_table} where ${fk.ref_column} = ${cellText(v)}`),
                                onmousedown: (e) => e.stopPropagation(),
                                onclick: (e) => {
                                    e.stopPropagation();
                                    openTable(t?.conn ?? conn, fk.ref_schema || t?.schema || '', fk.ref_table, `${quoteIdent(fk.ref_column, curConn(t?.conn)?.engine ?? 'mysql')} = ${sqlLiteral(v, curConn(t?.conn)?.engine ?? 'mysql')}`);
                                } }, fi('chev-r', 'db-chev')));
                        }
                        else
                            cell.append(v === null ? '—' : cellShown(v, 300));
                    }
                    line.append(cell);
                }
                line.append(h('div', { class: 'db-gc' }));
                layer.append(line);
            }
            const ae = document.activeElement;
            if (inp?.isConnected && (hadFocus || !(ae instanceof HTMLInputElement || ae instanceof HTMLTextAreaElement || ae instanceof HTMLSelectElement)))
                inp.focus({ preventScroll: true });
        };
        viewport.addEventListener('scroll', () => { if (t) {
            t.scroll = viewport.scrollTop;
            t.scrollX = viewport.scrollLeft;
        } paint(); });
        // one handler for every cell (the rows are repainted while scrolling): select in place, a second
        // click on the same cell within 450 ms edits it - even when the first click rebuilt the grid
        if (t)
            layer.addEventListener('mousedown', (e) => {
                const cell = e.target.closest('.db-gc[data-r]');
                if (!cell || e.button !== 0)
                    return;
                const r = Number(cell.dataset.r);
                const j = Number(cell.dataset.c);
                if (t.editing) {
                    if (t.editing.r === r && t.editing.c === j)
                        return; // a click inside the input
                    e.preventDefault();
                    t.sel = { r, c: j };
                    commitEdit(t);
                    lastDown = { tab: t.id, r, c: j, at: Date.now() };
                    focusGrid();
                    return;
                }
                const now = Date.now();
                const dbl = !!lastDown && lastDown.tab === t.id && lastDown.r === r && lastDown.c === j && now - lastDown.at < 450;
                lastDown = { tab: t.id, r, c: j, at: dbl ? 0 : now };
                t.sel = { r, c: j };
                if (dbl && opts.editable) {
                    e.preventDefault();
                    startEdit(t);
                    return;
                }
                layer.querySelectorAll('.sel, .selrow').forEach((x) => x.classList.remove('sel', 'selrow'));
                cell.classList.add('sel');
                cell.parentElement?.classList.add('selrow');
                drawRight();
            });
        layer.addEventListener('contextmenu', (e) => {
            const cell = e.target.closest('.db-gc[data-r]');
            if (!cell)
                return;
            e.preventDefault();
            const r = Number(cell.dataset.r);
            const j = Number(cell.dataset.c);
            if (t) {
                t.sel = { r, c: j };
                if (t.editing)
                    commitEdit(t);
                else {
                    layer.querySelectorAll('.sel, .selrow').forEach((x) => x.classList.remove('sel', 'selrow'));
                    cell.classList.add('sel');
                    cell.parentElement?.classList.add('selrow');
                    drawRight();
                }
            }
            cellMenu({ x: e.clientX, y: e.clientY }, cols, rows, r, j, opts.info ?? null, t, !!opts.editable);
        });
        paint();
        let restored = false;
        new ResizeObserver(() => {
            if (!restored && t) {
                // a rebuilt grid (edit, Enter, Tab) comes back where it was - both ways - with the selected cell in view
                restored = true;
                viewport.scrollTop = t.scroll;
                viewport.scrollLeft = t.scrollX;
                const cell = layer.querySelector('.db-gc.sel');
                if (cell) {
                    const cr = cell.getBoundingClientRect();
                    const vr = viewport.getBoundingClientRect();
                    if (cr.left < vr.left)
                        viewport.scrollLeft -= vr.left - cr.left;
                    else if (cr.right > vr.right)
                        viewport.scrollLeft += Math.min(cr.right - vr.right, cr.left - vr.left);
                }
            }
            paint();
        }).observe(viewport);
        if (t)
            wrap.addEventListener('keydown', (e) => gridKey(t, e, opts.editable ?? false));
        return wrap;
    }
    function setCell(t, r, c, v) {
        if (r < 0) {
            t.inserted[-1 - r].set(c, v);
        }
        else {
            const orig = t.rows[r][c];
            const m = t.edits.get(r) ?? new Map();
            if (sqlKeyword(v) === null && cellText(orig) === cellText(v) && (orig === null) === (v === null))
                m.delete(c);
            else
                m.set(c, v);
            if (m.size)
                t.edits.set(r, m);
            else
                t.edits.delete(r);
        }
        drawBody();
        drawTabs();
    }
    function moveSel(t, dr, dc) {
        if (!t.sel)
            t.sel = { r: 0, c: 0 };
        const visCols = t.cols.map((_, j) => j).filter((j) => !t.hidden.has(t.cols[j].name));
        const ci = Math.max(0, visCols.indexOf(t.sel.c));
        const nc = visCols[Math.max(0, Math.min(visCols.length - 1, ci + dc))] ?? 0;
        let nr = t.sel.r;
        if (dr) {
            const order = [...t.rows.keys(), ...t.inserted.map((_, i) => -1 - i)];
            const at = order.indexOf(t.sel.r);
            nr = order[Math.max(0, Math.min(order.length - 1, at + dr))] ?? 0;
            const top = Math.max(0, order.indexOf(nr)) * ROW_H;
            const vh = bodyEl.querySelector('.db-gv')?.clientHeight ?? 600;
            if (top < t.scroll)
                t.scroll = top;
            else if (top + ROW_H * 2 > t.scroll + vh)
                t.scroll = top + ROW_H * 2 - vh;
        }
        t.sel = { r: nr, c: nc };
        drawBody();
        bodyEl.querySelector('.db-grid')?.focus();
    }
    function gridKey(t, e, editable) {
        if (t.editing)
            return;
        const mod = e.metaKey || e.ctrlKey;
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            moveSel(t, 1, 0);
        }
        else if (e.key === 'ArrowUp') {
            e.preventDefault();
            moveSel(t, -1, 0);
        }
        else if (e.key === 'ArrowRight' || (e.key === 'Tab' && !e.shiftKey)) {
            e.preventDefault();
            moveSel(t, 0, 1);
        }
        else if (e.key === 'ArrowLeft' || (e.key === 'Tab' && e.shiftKey)) {
            e.preventDefault();
            moveSel(t, 0, -1);
        }
        else if ((e.key === 'Enter' || e.key === 'F2') && editable && t.sel) {
            e.preventDefault();
            startEdit(t);
        }
        else if (mod && e.altKey && e.code === 'KeyN' && editable && t.sel) { // IntelliJ's Set NULL - like the menu, never on NOT NULL
            e.preventDefault();
            const name = t.cols[t.sel.c]?.name;
            if (t.info?.columns.find((x) => x.name === name)?.nullable === false)
                deps.toast(`${name} is NOT NULL`);
            else
                setCell(t, t.sel.r, t.sel.c, null);
        }
        else if (e.key === 'Escape' && pendingCount(t)) {
            e.preventDefault();
            revert(t);
        }
        else if (mod && e.key.toLowerCase() === 'c' && t.sel) {
            e.preventDefault();
            const v = t.sel.r < 0 ? t.inserted[-1 - t.sel.r].get(t.sel.c) : t.edits.get(t.sel.r)?.has(t.sel.c) ? t.edits.get(t.sel.r).get(t.sel.c) : t.rows[t.sel.r][t.sel.c];
            void navigator.clipboard?.writeText(v === null || v === undefined ? '' : cellText(v));
            deps.toast('Copied');
        }
        else if (editable && mod && e.key === 'Backspace' && t.sel && t.sel.r >= 0) {
            e.preventDefault();
            if (t.deleted.has(t.sel.r))
                t.deleted.delete(t.sel.r);
            else
                t.deleted.add(t.sel.r);
            drawBody();
            drawTabs();
        }
        else if (editable && t.sel && e.key.length === 1 && !mod && !e.altKey) {
            e.preventDefault(); // typing starts an edit: the key replaces the value
            startEdit(t, e.key);
        }
        else if (editable && t.sel && (e.key === 'Delete' || e.key === 'Backspace') && !mod) {
            e.preventDefault();
            startEdit(t, '');
        }
        // ⌘⌫ / ⌘⌥N / Esc redraw the grid: the keyboard stays on it (⌘S, the arrows next), an edit's input keeps its own
        if (e.defaultPrevented && !t.editing && !bodyEl.contains(document.activeElement))
            focusGrid();
    }
    function claudeLast(name) {
        return history.filter((r) => r.origin === 'mcp' && r.connection === name);
    }
    function statusBar(c, mid, midCls = '') {
        const cl = claudeLast(c.name);
        const mine = session ? cl.filter((r) => r.session === session) : cl;
        return h('div', { class: 'db-status' }, classBadge(c.class), h('span', { class: 'db-st-path' }, `${rawProjectKey(c.project).split('/').pop()} · ${connPath(c)}`), h('span', { class: midCls }, mid), h('span', { class: 'grow' }), h('span', { class: 'mut' }, mine.length ? `Claude: ${mine.length} ${mine.length === 1 ? 'query' : 'queries'}${session ? ' this session' : ''} · last ${agoWords(mine[0].ts)}` : 'Claude: no queries'));
    }
    function drawTable(t) {
        const c = curConn(t.conn);
        if (!c) {
            bodyEl.append(h('p', { class: 'bad db-pad' }, `connection ${t.conn} is gone`));
            return;
        }
        const pk = t.info?.primary_key ?? [];
        const isView = (() => {
            const o = objects.get(t.conn + '|' + t.schema);
            return !!o && !('error' in o) && o.objects.some((x) => x.name === t.table && x.kind === 'view');
        })();
        const editable = !!pk.length && !isView && t.cols.length > 0 && pk.every((k) => t.cols.some((x) => x.name === k));
        const where = h('input', { class: 'db-where-in', placeholder: 'condition, e.g. stock < 10 AND cat_id = 2', value: t.where, spellcheck: false, 'aria-label': 'WHERE condition' });
        const apply = () => reloadTable(t, () => { t.where = where.value; });
        where.addEventListener('keydown', (e) => { if (e.key === 'Enter')
            apply(); if (e.key === 'Escape' && where.value) {
            where.value = '';
            apply();
        } });
        const sortBtn = h('button', { title: 'sort by a column (or click a column header)' }, t.order ? `Sort: ${t.order} ${t.desc ? '↓' : '↑'}` : 'Sort', fi('chev-d', 'db-caret'));
        sortBtn.addEventListener('click', () => popMenu(sortBtn, [
            t.order ? ['No sort', () => reloadTable(t, () => { t.order = ''; t.desc = false; })] : null,
            ...t.cols.flatMap((x) => [[`${x.name} ↑`, () => reloadTable(t, () => { t.order = x.name; t.desc = false; })],
                [`${x.name} ↓`, () => reloadTable(t, () => { t.order = x.name; t.desc = true; })]])
        ]));
        const colBtn = h('button', { title: 'show / hide columns' }, t.hidden.size ? `Columns ${t.cols.length - t.hidden.size}/${t.cols.length}` : 'Columns', fi('chev-d', 'db-caret'));
        colBtn.addEventListener('click', () => popMenu(colBtn, [
            t.hidden.size ? ['Show all', () => { t.hidden.clear(); drawBody(); }] : null,
            ...t.cols.map((x) => [x.name, () => {
                    if (t.hidden.has(x.name))
                        t.hidden.delete(x.name);
                    else if (t.hidden.size < t.cols.length - 1)
                        t.hidden.add(x.name);
                    drawBody();
                }, t.hidden.has(x.name) ? 'off' : 'on'])
        ]));
        const copy = async (what) => {
            const cols = t.cols.map((x) => x.name);
            const text = what === 'csv' ? toCsv(cols, t.rows) : what === 'insert' ? toInserts(c.engine, t.schema || null, t.table, cols, t.rows)
                : JSON.stringify(t.rows.map((r) => Object.fromEntries(cols.map((k, i) => [k, r[i]]))), null, 2);
            await navigator.clipboard?.writeText(text);
            deps.toast(`Copied ${rowsText(t.rows.length)} as ${what === 'insert' ? 'SQL INSERT' : what.toUpperCase()}`);
        };
        const more = h('button', { class: 'icon', title: 'more: copy rows, delete the row, open in a console', 'aria-label': 'more' }, fi('more'));
        more.addEventListener('click', () => popMenu(more, [
            ['Copy rows as CSV', () => void copy('csv')], ['Copy rows as SQL INSERT', () => void copy('insert')], ['Copy rows as JSON', () => void copy('json')], null,
            editable && t.sel && t.sel.r >= 0 ? [t.deleted.has(t.sel.r) ? 'Undelete the row' : kbd('Delete the row  ⌘⌫', 'Ctrl'), () => {
                    if (t.sel && t.sel.r >= 0) {
                        if (t.deleted.has(t.sel.r))
                            t.deleted.delete(t.sel.r);
                        else
                            t.deleted.add(t.sel.r);
                        draw();
                    }
                }, 'danger'] : null,
            editable ? ['Import CSV…', () => importCsv(t, c)] : null,
            ['Open the query in a console', () => openConsole(t.conn, tableSql(t, c.engine) + ';')],
            ['Show DDL', () => { t.showDdl = !t.showDdl; drawBody(); }]
        ]));
        const nrows = t.rows.length;
        bodyEl.append(h('div', { class: 'db-bar' }, h('label', { class: 'db-where' }, h('span', { class: 'db-where-k' }, 'WHERE'), where, h('button', { class: 'db-where-go', title: 'apply (Enter)', 'aria-label': 'apply', onclick: apply }, '↵')), sortBtn, colBtn, editable ? h('button', { title: 'add a row (filled in the grid, saved with Submit)', onclick: () => { t.inserted.push(new Map()); t.sel = { r: -t.inserted.length, c: 0 }; draw(); } }, '+ Row') : null, h('button', { class: 'icon', title: 'reload', 'aria-label': 'reload', onclick: apply }, fi('refresh')), more, h('span', { class: 'db-count' }, t.loading ? 'loading…' : `${rangeText(t.res?.offset ?? 0, nrows, !!t.res?.more)} · ${t.res?.elapsed_ms ?? 0} ms`), pageCtl(t.res?.offset ?? 0, !!t.res?.more, t.loading, (off) => gotoPage(t, off), () => reloadTable(t))));
        if (t.error)
            bodyEl.append(h('div', { class: 'db-err' }, t.error));
        if (t.showDdl) {
            const pre = h('pre', { class: 'db-ddl' });
            const lines = (t.info?.ddl ?? '-- no DDL').split('\n');
            highlightLines(lines, 'sql').forEach((row, i) => {
                for (const tk of row)
                    pre.append(tk.c ? h('span', { class: 'hl-' + tk.c }, showBidi(tk.t)) : showBidi(tk.t));
                if (i < lines.length - 1)
                    pre.append('\n');
            });
            bodyEl.append(h('div', { class: 'db-ddlbar' }, h('strong', {}, showBidi(`DDL · ${t.table}`)), h('span', { class: 'grow' }), h('button', { onclick: async () => { await navigator.clipboard?.writeText(t.info?.ddl ?? ''); deps.toast('Copied'); } }, 'Copy'), h('button', { onclick: () => { t.showDdl = false; drawBody(); } }, 'Data')), pre);
        }
        else if (t.loading && !t.rows.length)
            bodyEl.append(h('div', { class: 'db-pad mut grow' }, h('span', { class: 'spinner' }), ' loading…'));
        else {
            bodyEl.append(grid(t.cols, t.rows, { t, info: t.info, editable, sort: t.order, desc: t.desc, hidden: t.hidden,
                onSort: (col) => reloadTable(t, () => { if (t.order === col) {
                    if (t.desc) {
                        t.order = '';
                        t.desc = false;
                    }
                    else
                        t.desc = true;
                }
                else {
                    t.order = col;
                    t.desc = false;
                } }) }));
        }
        const n = pendingCount(t);
        if (n) {
            let stmts = [];
            let err = '';
            try {
                stmts = pendingStatements(t);
            }
            catch (e) {
                err = String(e);
            }
            const rowsTouched = new Set([...t.edits.keys(), ...t.deleted]).size + t.inserted.length;
            const text = err || stmts.map((x) => showStatement(x, c.engine)).join(';\n') + ';';
            bodyEl.append(h('div', { class: 'db-pending' + (c.class === 'prod' ? ' prod' : '') }, h('div', { class: 'db-pend-h' }, ic('pencil', 'db-pend-ic'), h('strong', {}, `${n} change${n === 1 ? '' : 's'} in ${rowsTouched} row${rowsTouched === 1 ? '' : 's'}`), c.class === 'prod' ? h('span', { class: 'bad' }, 'PRODUCTION') : null, t.sqlOpen ? h('span', { class: 'grow' }) : h('code', { class: 'db-pend-sql' }, text.replace(/\s+/g, ' ')), h('button', { class: t.sqlOpen ? 'on' : undefined, title: 'the statements Submit runs, in one transaction', onclick: () => { t.sqlOpen = !t.sqlOpen; drawBody(); } }, 'SQL'), h('button', { onclick: () => revert(t) }, 'Revert ', h('span', { class: 'db-k' }, 'Esc')), h('button', { class: c.class === 'prod' ? 'danger' : 'primary', disabled: !!err, onclick: () => void submit(t) }, c.class === 'prod' ? 'Submit to PROD ' : 'Submit ', h('span', { class: 'db-k' }, kbd('⌘S', 'Ctrl')))), t.sqlOpen ? h('pre', { class: 'db-sqlbox' }, text) : null));
        }
        bodyEl.append(statusBar(c, editable ? 'read-write · autocommit off' : isView ? 'a view · read-only' : pk.length ? 'read-only'
            : !t.info && t.loading ? 'loading…' : 'no primary key · read-only')); // no info yet: the key is unknown, not missing
    }
    // ---------------------------------------------------------------- CSV import (one transaction through the console's tx worker)
    function importCsv(t, c) {
        const file = h('input', { type: 'file', accept: '.csv,text/csv,text/plain' });
        const info = h('div', { class: 'db-csv' }, h('p', { class: 'mut' }, `Rows go into ${t.table} in ONE transaction - a failed row rolls everything back. The first line names the columns.`));
        let parsed = [];
        let nullEmpty = true;
        const go = h('button', { class: 'primary', disabled: true }, 'Import');
        const tableCols = t.info?.columns.map((x) => x.name) ?? t.cols.map((x) => x.name);
        const mapping = () => (parsed[0] ?? []).map((hd) => tableCols.find((k) => k.toLowerCase() === hd.trim().toLowerCase()) ?? null);
        const drawInfo = () => {
            clear(info);
            if (!parsed.length)
                return;
            const m = mapping();
            const used = m.filter((x) => !!x);
            const skipped = (parsed[0] ?? []).filter((_, i) => !m[i]);
            info.append(h('p', {}, `${rowsText(parsed.length - 1)} · columns: `, h('span', { class: 'db-mono' }, used.join(', ') || 'none matches'), skipped.length ? h('span', { class: 'warn' }, ` · not in the table, skipped: ${skipped.join(', ')}`) : null));
            const nul = h('input', { type: 'checkbox' });
            nul.checked = nullEmpty;
            nul.addEventListener('change', () => { nullEmpty = nul.checked; });
            info.append(h('label', { class: 'small' }, nul, ' an empty cell is NULL'));
            const pv = h('pre', { class: 'db-sqlbox' }, parsed.slice(0, 6).map((r) => r.join(' | ')).join('\n'));
            info.append(pv);
            go.disabled = !used.length || parsed.length < 2;
        };
        file.addEventListener('change', async () => {
            const f = file.files?.[0];
            if (!f)
                return;
            if (f.size > 20 << 20) {
                deps.toast('At most 20 MB');
                return;
            }
            parsed = parseCsv(await f.text());
            drawInfo();
        });
        const csvTx = viewTx + 'csv-' + t.id; // its own transaction: never commits a console's open changes
        const run = async (confirm) => {
            const m = mapping();
            const idx = m.map((x, i) => (x ? i : -1)).filter((i) => i >= 0);
            const cols = idx.map((i) => m[i]);
            const rows = parsed.slice(1).map((r) => idx.map((i) => (nullEmpty && (r[i] ?? '') === '' ? null : r[i] ?? null)));
            const stmts = csvInserts(c.engine, t.schema || null, t.table, cols, rows);
            go.disabled = true;
            try {
                let batch = [];
                let size = 0;
                const flush = async () => {
                    if (!batch.length)
                        return;
                    await dbApi.tx({ project: pj(t.conn), connection: t.conn, op: 'exec', statements: batch, schema: t.schema, session, confirm, tx: csvTx });
                    batch = [];
                    size = 0;
                };
                for (const st of stmts) {
                    const n = JSON.stringify(st).length;
                    if (size + n > 600_000)
                        await flush();
                    batch.push(st);
                    size += n;
                }
                await flush();
                await dbApi.tx({ project: pj(t.conn), connection: t.conn, op: 'commit', session, confirm, tx: csvTx });
                deps.closeModal();
                deps.toast(`Imported ${rowsText(rows.length)} into ${t.table}`);
                void loadTable(t);
            }
            catch (e) {
                try {
                    await dbApi.tx({ project: pj(t.conn), connection: t.conn, op: 'rollback', session, tx: csvTx });
                }
                catch { /* the worker is gone: rolled back */ }
                deps.toast('Import failed, nothing was written: ' + errText(e));
                go.disabled = false;
            }
        };
        let armed = false;
        go.addEventListener('click', () => {
            if (c.class !== 'prod') {
                void run('');
                return;
            }
            if (!armed) { // production: a second, deliberate click on the red button
                armed = true;
                go.className = 'danger';
                go.textContent = markBidi(`Import ${rowsText(parsed.length - 1)} into PRODUCTION`);
                return;
            }
            void run('prod');
        });
        deps.modal(`Import CSV · ${t.table}`, file, info, h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), go));
        focusForm();
    }
    // ---------------------------------------------------------------- console
    function drawConsole(t) {
        const c = curConn(t.conn);
        const pickBtn = h('button', { class: 'db-connpick', title: 'connection', disabled: t.running || !!t.tx?.open }, c ? classBadge(c.class) : null, h('span', { class: 'db-cp-t' }, c ? `${rawProjectKey(c.project).split('/').pop()} · ${short(c)}` : 'connection…'), c ? privateMark(c) : null, fi('chev-d', 'db-caret'));
        pickBtn.addEventListener('click', () => popMenu(pickBtn, allConns().map((x) => [`${CLS_LABEL[x.class]} · ${rawProjectKey(x.project).split('/').pop()} · ${short(x)}${x.def?.agent_enabled === false ? ' · only you' : ''}`, () => {
                t.conn = x.name;
                t.schema = schemaOf.get(x.name) ?? '';
                conn = x.name;
                if (t.mode === 'rw')
                    joinTx(t);
                draw();
                void loadObjects(x.name);
            }])));
        const mode = (m, icon, label, title) => h('button', { class: 'db-seg' + (t.mode === m ? ' on' : '') + (m === 'rw' ? ' rw' : '') + (m === 'rw' && c?.class === 'prod' ? ' prod' : ''), title,
            disabled: t.running || !!t.tx?.open && m === 'ro', 'aria-pressed': String(t.mode === m), onclick: () => {
                t.mode = m;
                if (m === 'rw')
                    joinTx(t);
                draw();
            } }, ic(icon), label);
        const scopeLocked = tabs.some((x) => x.conn === t.conn && x.kind === 'console' && (x.running || x.tx?.open));
        const scope = (on, label) => h('button', { class: 'db-seg' + (shared(t.conn) === on ? ' on' : ''),
            disabled: scopeLocked, 'aria-pressed': String(shared(t.conn) === on),
            title: scopeLocked ? 'Commit or roll back first: change scope only with no transaction open.'
                : on ? 'One connection for these read-write consoles. Commit or Rollback ends it for all. Agents and CSV keep their own transactions.' : 'An independent transaction for each console.',
            onclick: () => { scopes.set(t.conn, on); writeStore('piper.db.txscope.' + t.conn, on ? 'shared' : 'own'); draw(); } }, on ? fi('link') : null, label);
        bodyEl.append(h('div', { class: 'db-bar' }, pickBtn, h('span', { class: 'db-segs' }, mode('ro', 'eye', 'Read-only', 'every statement in a read-only transaction'), mode('rw', 'pencil', 'Read-write', 'statements run in one open transaction - nothing is written until you Commit')), t.mode === 'rw' ? h('span', { class: 'db-segs', role: 'group', 'aria-label': 'Transaction scope' }, scope(false, 'Own tx'), scope(true, 'Shared')) : null, h('span', { class: 'grow' }), deps.toAgent && c?.def?.agent_enabled !== false && deps.agentLabel?.() !== '' ? h('button', { title: 'ask the session to run this itself with its db tools (results reach it masked)',
            disabled: !t.text.trim(), onclick: () => deps.toAgent(`Run this on the database connection \`${t.conn}\` with your db tools and tell me what you find:\n\n\`\`\`sql\n${t.text.trim()}\n\`\`\``) }, deps.agentLabel ? `→ ${deps.agentLabel()}` : '→ Claude') : null, h('button', { title: 'format the SQL', onclick: () => { t.text = formatSql(t.text); drawBody(); } }, 'Format'), h('button', { title: kbd('EXPLAIN the statement at the caret (⌘E)', 'Ctrl'), disabled: t.running, onclick: () => void runConsole(t, true) }, 'Explain ', h('span', { class: 'db-k' }, kbd('⌘E', 'Ctrl'))), t.running ? h('button', { class: 'danger', onclick: () => void dbApi.cancel(pj(t.conn), t.conn, '', viewTx + t.id, session) }, 'Cancel')
            : h('button', { class: 'primary', title: kbd('run the statement at the caret or the selection (⌘↩)', 'Ctrl'), onclick: () => void runConsole(t) }, '▶ Run ', h('span', { class: 'db-k' }, kbd('⌘↩', 'Ctrl')))));
        bodyEl.append(editor(t));
        // results
        const rtabs = h('div', { class: 'db-rtabs' });
        t.results.forEach((r, i) => rtabs.append(h('button', { class: 'db-rtab' + (i === t.active ? ' on' : '') + (r.error ? ' bad' : ''), title: r.title, onclick: () => { t.active = i; drawBody(); } }, r.title.startsWith('Explain') ? 'Explain' : `Result ${i + 1}`, ' · ', r.error ? 'error' : r.affected !== undefined ? `${r.affected} affected`
            : r.res ? rangeText(r.res.offset, r.rows.length, r.res.more) : r.note.startsWith('Transaction') ? r.title.toLowerCase() : '')));
        const r = t.results[t.active];
        const exp = h('button', { disabled: !r?.res }, 'Export', fi('chev-d', 'db-caret'));
        exp.addEventListener('click', () => {
            if (!r?.res)
                return;
            const cols = r.res.columns.map((x) => x.name);
            popMenu(exp, [['Copy as CSV', async () => { await navigator.clipboard?.writeText(toCsv(cols, r.rows)); deps.toast('Copied as CSV'); }],
                ['Copy as JSON', async () => { await navigator.clipboard?.writeText(JSON.stringify(r.rows.map((x) => Object.fromEntries(cols.map((k, i) => [k, x[i]]))), null, 2)); deps.toast('Copied as JSON'); }]]);
        });
        bodyEl.append(h('div', { class: 'db-rhead' }, rtabs, h('span', { class: 'grow' }), pageCtl(r?.res?.offset ?? 0, !!r?.res?.more, t.running, (off) => { if (r)
            void consolePage(t, r, off); }, () => { if (r?.res && (r.sql !== undefined || r.res.result_id) && !(r.tx && !t.tx?.open))
            void consolePage(t, r, 0);
        else
            drawBody(); }), exp));
        showActiveResult(); // N10-C-1: the result just run (the last tab) or picked is in view - a redraw resets the strip to its start
        if (t.tx?.open) {
            const last = [...t.results].reverse().find((x) => x.affected !== undefined);
            bodyEl.append(h('div', { class: 'db-tx' + (c?.class === 'prod' ? ' prod' : '') }, ic('warn', 'db-tx-ic'), h('div', { class: 'db-tx-t' }, h('strong', {}, shared(t.conn)
                ? `Shared transaction · ${joined(t).length} consoles · ${rowsText(t.tx.affected)} affected · not committed`
                : `${last?.title ?? 'Transaction'} · ${rowsText(t.tx.affected)} affected${last ? ` · ${last.ms} ms` : ''} · not committed`), h('div', { class: 'db-tx-s' }, shared(t.conn) ? 'One connection for these read-write consoles. Commit and Rollback affect all of them. CSV and agents keep their own transactions.'
                : `Statements ran on ${c ? connPath(c) : t.conn} in an open transaction. Nothing is written until you commit.`)), h('button', { onclick: () => void txEnd(t, 'rollback') }, shared(t.conn) ? 'Rollback all' : 'Rollback'), h('button', { class: c?.class === 'prod' ? 'danger' : 'primary', title: kbd('⌘⇧↩', 'Ctrl'), onclick: () => void txEnd(t, 'commit') }, shared(t.conn) ? 'Commit all ' : 'Commit ', h('span', { class: 'db-k' }, kbd('⌘⇧↩', 'Ctrl')))));
        }
        if (!r)
            bodyEl.append(h('div', { class: 'db-gempty mut grow' }, kbd('Run a statement: ⌘↩ runs the one at the caret, a selection runs as it is.', 'Ctrl')));
        else if (r.error)
            bodyEl.append(h('div', { class: 'db-err' }, r.error));
        else if (r.res) {
            const ch = r.changed;
            const extra = ch ? [...ch.entries()].map(([ri, m]) => r.rows[ri].map((v, j) => (m.has(j) ? `${cellText(v)} → ${cellText(m.get(j))}` : ''))) : [];
            bodyEl.append(grid(r.res.columns, r.rows, ch || r.deleted ? {
                extra,
                mark: (ri, j, v) => (ch?.get(ri)?.has(j) ? h('span', { class: 'db-chg' }, `${cellText(v)} → ${cellText(ch.get(ri).get(j))}`)
                    : r.deleted ? h('span', { class: 'db-gone' }, v === null ? '—' : cellText(v)) : null),
            } : {}));
        }
        else if (!t.tx?.open || r.affected === undefined)
            bodyEl.append(h('div', { class: 'db-note' }, r.note));
        if (!r?.res)
            bodyEl.append(h('div', { class: 'grow' }));
        if (c) {
            const mid = t.tx?.open ? `read-write · tx open · ${rowsText(t.tx.affected)}` : t.mode === 'rw' ? 'read-write · autocommit off' : 'read-only';
            const bar = statusBar(c, mid, t.tx?.open ? 'warn' : t.mode === 'rw' ? 'warn' : 'mut');
            bar.lastElementChild.replaceWith(h('span', { class: 'mut db-st-hint' }, kbd('⌘↩ run · ⌘E explain · ⌘⇧↩ commit · ⌃Space complete', 'Ctrl')));
            bodyEl.append(bar);
        }
    }
    function editor(t) {
        const gutter = h('div', { class: 'db-gut', 'aria-hidden': 'true' });
        const back = h('pre', { class: 'db-hl', 'aria-hidden': 'true' });
        const curLine = h('div', { class: 'db-curline', 'aria-hidden': 'true' });
        // N9: the console shows the SQL raw (it must line up with the textarea): a direction control reversed the line on screen
        // with no sign of it (the Files editor marks them) - say so under the code
        const bidiWarn = h('div', { class: 'db-bidi hidden', role: 'status' }, '⚠ The SQL holds invisible direction characters (e.g. U+202E): the line may read in another order than it runs.');
        const ta = h('textarea', { spellcheck: false, wrap: 'off', 'aria-label': 'SQL', placeholder: kbd('SELECT …   ⌘↩ runs the statement at the caret', 'Ctrl') });
        ta.value = t.text;
        const pop = h('div', { class: 'db-ac hidden', role: 'listbox' });
        let items = [];
        let at = 0;
        const LH = 20;
        const paint = () => {
            clear(back);
            const lines = ta.value.split('\n');
            highlightLines(lines, 'sql').forEach((row, i) => {
                for (const tk of row)
                    back.append(tk.c ? h('span', { class: 'hl-' + tk.c }, rawText(tk.t)) : tk.t); // raw: lines up with the textarea
                if (i < lines.length - 1)
                    back.append('\n');
            });
            back.append('\n');
            bidiWarn.classList.toggle('hidden', !hasBidi(ta.value));
            const st = statementAt(ta.value, ta.selectionStart);
            const a = ta.value.slice(0, st.start).split('\n').length - 1 + (ta.value.slice(st.start, st.end).match(/^\s*\n/)?.[0].split('\n').length ?? 1) - 1;
            const caretLine = ta.value.slice(0, ta.selectionStart).split('\n').length - 1;
            clear(gutter);
            lines.forEach((_, i) => gutter.append(h('div', { class: 'db-ln' + (i === a && st.sql ? ' run' : '') }, i === a && st.sql ? h('span', { class: 'db-runmark' }, '▷') : null, String(i + 1))));
            curLine.style.top = `${8 + caretLine * LH - ta.scrollTop}px`;
            back.scrollTop = ta.scrollTop;
            back.scrollLeft = ta.scrollLeft;
            gutter.scrollTop = ta.scrollTop;
        };
        const objs = () => {
            const o = objects.get(t.conn + '|' + (t.schema || schemaOf.get(t.conn) || ''));
            return o && !('error' in o) ? o.objects : [];
        };
        const colMap = new Map();
        const typeMap = new Map();
        const wordBefore = () => /([A-Za-z_][A-Za-z0-9_]*)$/.exec(ta.value.slice(0, ta.selectionStart))?.[1] ?? '';
        const closePop = () => { pop.classList.add('hidden'); items = []; };
        const showPop = async () => {
            const w = wordBefore();
            const os = objs();
            const names = os.map((x) => x.name);
            for (const tb of mentionedTables(ta.value, names)) {
                if (!colMap.has(tb)) {
                    colMap.set(tb, []);
                    const d = await describe(t.conn, t.schema || schemaOf.get(t.conn) || '', tb);
                    colMap.set(tb, d?.columns.map((x) => x.name) ?? []);
                    for (const x of d?.columns ?? [])
                        typeMap.set(x.name, x.type);
                }
            }
            items = completions(w, names, colMap, ta.value);
            if (!items.length || !w) {
                closePop();
                return;
            }
            at = 0;
            clear(pop);
            items.forEach((it, i) => {
                const o = os.find((x) => x.name === it);
                const isCol = !o && typeMap.has(it);
                pop.append(h('div', { class: 'db-aci' + (i === at ? ' on' : ''), onmousedown: (e) => { e.preventDefault(); at = i; accept(); } }, ic(o ? (o.kind === 'view' ? 'eye' : 'table') : isCol ? 'col' : 'kw'), h('span', { class: 'db-aci-t' }, it), h('span', { class: 'db-aci-d' }, o ? (o.rows !== null && o.rows !== undefined ? rowsText(o.rows) : o.kind) : isCol ? typeMap.get(it) : 'keyword')));
            });
            const before = ta.value.slice(0, ta.selectionStart);
            const line = before.split('\n').length - 1;
            const col = before.length - before.lastIndexOf('\n') - 1;
            pop.style.top = `${(line + 1) * LH + 12 - ta.scrollTop}px`;
            pop.style.left = `${48 + Math.max(0, col - w.length) * 7.8 + 12 - ta.scrollLeft}px`;
            pop.classList.remove('hidden');
        };
        const accept = () => {
            const w = wordBefore();
            const it = items[at];
            if (!it)
                return;
            const s = ta.selectionStart;
            ta.value = ta.value.slice(0, s - w.length) + it + ta.value.slice(s);
            ta.selectionStart = ta.selectionEnd = s - w.length + it.length;
            t.text = ta.value;
            closePop();
            paint();
        };
        ta.addEventListener('input', () => { t.text = ta.value; t.caret = ta.selectionStart; paint(); void showPop(); });
        ta.addEventListener('scroll', paint);
        ta.addEventListener('keyup', () => { t.caret = ta.selectionStart; paint(); });
        ta.addEventListener('click', () => { t.caret = ta.selectionStart; closePop(); paint(); });
        ta.addEventListener('blur', () => setTimeout(closePop, 100));
        ta.addEventListener('keydown', (e) => {
            const mod = e.metaKey || e.ctrlKey;
            if (!pop.classList.contains('hidden')) {
                if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                    e.preventDefault();
                    at = (at + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length;
                    [...pop.children].forEach((x, i) => x.classList.toggle('on', i === at));
                    return;
                }
                if ((e.key === 'Enter' && !mod) || e.key === 'Tab') {
                    e.preventDefault();
                    accept();
                    return;
                }
                if (e.key === 'Escape') {
                    e.preventDefault();
                    closePop();
                    return;
                }
            }
            if (mod && e.key === 'Enter' && e.shiftKey) {
                e.preventDefault();
                if (t.tx?.open)
                    void txEnd(t, 'commit');
                return;
            }
            if (mod && e.key === 'Enter') {
                e.preventDefault();
                t.caret = ta.selectionStart;
                void runConsole(t);
                return;
            }
            if (mod && e.key.toLowerCase() === 'e') {
                e.preventDefault();
                t.caret = ta.selectionStart;
                void runConsole(t, true);
                return;
            }
            if (e.key === 'Tab' && !mod) {
                e.preventDefault();
                const s = ta.selectionStart;
                ta.value = ta.value.slice(0, s) + '  ' + ta.value.slice(ta.selectionEnd);
                ta.selectionStart = ta.selectionEnd = s + 2;
                t.text = ta.value;
                paint();
            }
            if (e.ctrlKey && e.key === ' ') {
                e.preventDefault();
                void showPop();
            }
        });
        paint();
        setTimeout(() => { ta.selectionStart = ta.selectionEnd = Math.min(t.caret, ta.value.length); paint(); }, 0);
        return h('div', { class: 'db-editor' }, gutter, h('div', { class: 'db-code' }, curLine, back, ta, bidiWarn), pop);
    }
    // ---------------------------------------------------------------- connections page
    function drawConnections() {
        const box = h('div', { class: 'db-connpage' });
        bodyEl.append(box);
        if (!all) {
            box.append(h('p', { class: listErr ? 'bad db-pad' : 'mut db-pad' }, listErr || 'loading…'));
            return;
        }
        if (listErr)
            box.append(h('p', { class: 'bad db-pad' }, listErr));
        const st = all.status;
        const conns = allConns();
        if (deps.compact) { // beside a terminal: the project's connections only - the rest is on the Database tab
            box.append(conns.length ? h('div', { class: 'db-pick' }, ...conns.map((c) => h('button', { class: 'db-pick-row', title: c.name, onclick: () => openConn(c.name) }, classDot(c.class), h('span', { class: 'db-pick-name' }, short(c)), privateMark(c), needsPassword(c) ? h('span', { class: 'warn small' }, 'no password') : null)))
                : h('p', { class: 'mut db-pad' }, 'No database for this project - add one on the Database tab.'));
            return;
        }
        // F10: Scan stacks and Import from IDEA in one Import ▾, Add connection the one primary action
        const importBtn = h('button', { class: 'ghost', 'aria-haspopup': 'menu', title: 'scan the dev stacks again, or import from IDEA' }, 'Import', fi('chev-d'));
        importBtn.addEventListener('click', () => popMenu(importBtn, [
            ['Scan stacks', () => void load(true)],
            ['Import from IDEA…', () => ideaImport('')]
        ]));
        const countEl = h('span', { class: 'mut db-cp-count', title: 'passwords stay in Piper\'s home on this machine, never in a sandbox' }, `${conns.length} connection${conns.length === 1 ? '' : 's'} · ${all.projects.length} project${all.projects.length === 1 ? '' : 's'}`);
        const head = h('div', { class: 'db-cp-head' }, h('h2', {}, 'Databases'));
        box.append(head);
        if (!st.ready)
            box.append(driversWarn(st));
        if (!conns.length) {
            head.append(countEl, h('span', { class: 'grow' }), importBtn, h('button', { class: 'primary', onclick: () => editConn(null) }, 'Add connection'));
            box.append(stateCard('db', 'No connections yet', 'No project has a dev stack connection and nothing is registered or imported.', h('button', { onclick: () => void load(true) }, 'Scan stacks'), h('button', { onclick: () => ideaImport('') }, 'Import from IDEA'), h('button', { onclick: () => editConn(null) }, 'Add')));
            return;
        }
        const search = h('input', { type: 'search', class: 'db-search', placeholder: 'Find connection…',
            'aria-label': 'Find project or connection', value: connectionFilter });
        const clearSearch = h('button', { type: 'button', class: 'db-find-clear', 'aria-label': 'Clear database search', title: 'Clear search' }, fi('x'));
        const summary = h('span', { class: 'mut small', 'aria-live': 'polite' });
        const results = h('div');
        const drawResults = () => {
            clear(results);
            const tbl = h('div', { class: 'db-ctable', role: 'table' });
            // F10: no visible column heads (the board) - kept for screen readers
            tbl.append(h('div', { class: 'db-crow db-chead', role: 'row' }, ...['Connection', 'Class', 'Engine', 'Database', 'Reach', ''].map((x) => h('div', { role: 'columnheader' }, x))));
            let count = 0;
            for (const g of all.projects) {
                const matches = g.connections.filter((c) => connectionMatches(c, g.title, connectionFilter));
                if (connectionFilter.trim() && !matches.length)
                    continue;
                count += matches.length;
                const missing = matches.filter((c) => c.reach === 'none').length;
                // F10: the project as a section heading; an extension's links in its ⋯ (on hover / focus)
                const links = g.links ?? [];
                const gmore = links.length ? h('button', { class: 'icon ghost', title: 'more', 'aria-label': `more for ${g.title}` }, fi('more')) : null;
                gmore?.addEventListener('click', () => popMenu(gmore, links.map((l) => [l.label, () => { window.open(l.url, '_blank', 'noopener'); }])));
                tbl.append(h('div', { class: 'db-cgroup ra-row' }, h('span', { class: 'sec-h' }, g.title), h('span', { class: 'db-mono mut small' }, g.project !== g.title ? g.project : ''), h('span', { class: 'grow' }), missing ? h('span', { class: 'warn' }, `${missing} need${missing === 1 ? 's' : ''} a tunnel target`) : null, gmore ? h('span', { class: 'row-actions' }, gmore) : null));
                for (const c of matches)
                    tbl.append(connRow(c));
            }
            clearSearch.classList.toggle('hidden', !connectionFilter);
            const filtering = !!connectionFilter.trim();
            summary.textContent = filtering ? markBidi(`${count} of ${conns.length} connections`) : '';
            countEl.hidden = filtering;
            results.append(count || !connectionFilter.trim() ? tbl : h('p', { class: 'mut db-pad', role: 'status' }, 'No matching connections.'));
        };
        search.addEventListener('input', () => { connectionFilter = search.value; drawResults(); });
        clearSearch.addEventListener('click', () => { search.value = ''; connectionFilter = ''; drawResults(); search.focus(); });
        head.append(h('div', { class: 'db-find' }, ic('search'), search, clearSearch), countEl, summary, h('span', { class: 'grow' }), importBtn, h('button', { class: 'primary', onclick: () => editConn(null) }, 'Add connection'));
        box.append(results);
        drawResults();
        if (all.orphans.length)
            box.append(h('p', { class: 'db-cp-foot warn' }, `Connections of projects Piper no longer finds: ${all.orphans.join(', ')}`));
        box.append(h('p', { class: 'db-cp-foot mut' }, 'Test / prod host:port is never taken from IDEA - a connection reaches test or prod only through a tunnel Piper opens, and the server is pinned at first use.'));
    }
    function connRow(c) {
        // F10: Reach = its icon + a short text; the target / host moved to the tooltip
        const reach = c.reach === 'stack' ? h('span', { class: 'db-reach ok', title: `the stack's database${c.host ? ' at ' + c.host : ''}` }, ic('stack'), reachText(c))
            : c.reach === 'tunnel' ? h('span', { class: 'db-reach' + (c.tunnel?.state === 'open' ? (c.class === 'prod' ? ' bad' : ' ok') : c.tunnel?.state === 'signin' ? ' warn' : ''), title: `tunnel · ${c.tunnel?.target ?? ''}` }, ic('shield'), reachText(c), c.tunnel?.state === 'signin' && c.tunnel.url ? h('a', { href: c.tunnel.url, target: '_blank', rel: 'noopener' }, ' · sign in') : null)
                : c.reach === 'ssh' ? h('span', { class: 'db-reach' + (c.tunnel?.state === 'open' ? (c.class === 'prod' ? ' bad' : ' ok') : ''), title: `ssh · ${c.tunnel?.target ?? ''} - the database as the SSH server sees it: ${c.host ?? ''}` }, ic('key'), reachText(c))
                    : c.reach === 'direct' ? h('span', { class: 'db-reach', title: `direct · ${c.host ?? ''}` }, ic('globe'), reachText(c)) : h('span', { class: 'db-reach warn' }, ic('warn'), reachText(c));
        const more = h('button', { class: 'icon', title: 'more', 'aria-label': 'more actions' }, fi('more'));
        more.addEventListener('click', () => {
            const open = c.tunnel?.state === 'open' || c.tunnel?.state === 'connecting' || c.tunnel?.state === 'signin';
            const items = [
                c.reach !== 'stack' ? [c.has_password ? 'Change password…' : 'Set password…', () => setPassword(c)] : null,
                c.reach === 'tunnel' || c.reach === 'ssh' ? [open ? 'Close tunnel' : 'Open tunnel', async () => { try {
                        await dbApi.tunnel(pj(c.name), c.name, open ? 'close' : 'open');
                    }
                    catch (e) {
                        deps.toast(errText(e));
                    } void load(true); }] : null,
                c.pinned ? ['Reset server pin', async () => { await dbApi.unpin(c.name, refHost(c.project)); deps.toast('Pin reset - the next connection pins the server again'); void load(true); }] : null,
                ['New console', () => { conn = c.name; openConsole(c.name); }],
                c.def ? ['Delete…', () => deps.modal('Delete connection?', h('p', {}, `${c.name} and its stored password.`), h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), h('button', { class: 'danger', onclick: async () => { deps.closeModal(); try {
                            await dbApi.del(pj(c.name), c.name);
                        }
                        catch (e) {
                            deps.toast(errText(e));
                        } void load(true); } }, 'Delete'))), 'danger'] : null
            ];
            popMenu(more, items.filter((it) => it)); // null is a separator in popMenu - here it only marks an item that does not apply
        });
        const testBtn = h('button', { title: 'connect and read the schemas' }, 'Test');
        testBtn.addEventListener('click', async () => {
            testBtn.textContent = '…';
            try {
                const { data } = await dbApi.meta(pj(c.name), c.name, 'schemas', '', '', session, true);
                deps.toast(`${short(c)}: connected · ${data?.schemas.length ?? 0} schema${data?.schemas.length === 1 ? '' : 's'}`);
                testBtn.textContent = '✓ Test';
            }
            catch (e) {
                deps.toast(`${short(c)}: ${errText(e)}`);
                testBtn.textContent = '✗ Test';
            }
        });
        // F10: a click on the row opens it (the name is the keyboard's way in); Test · Edit · ⋯ on hover / focus;
        // the password only when it is missing; the user in the tooltip (mostly the database's name)
        return h('div', { class: 'db-crow ra-row', role: 'row', onclick: (e) => { if (!e.target.closest('button, a, input'))
                openConn(c.name); } }, h('button', { class: 'db-cname2', title: `open ${c.name}: tables, console, history`, onclick: () => openConn(c.name) }, h('span', { class: 'db-mono' }, short(c)), privateMark(c), busyMark(c.name)), h('div', {}, classDot(c.class)), h('div', { class: 'mut' }, { mysql: 'MySQL', postgres: 'PostgreSQL', oracle: 'Oracle', mssql: 'SQL Server' }[c.engine] ?? c.engine, c.boundary === 'parser' ? h('div', { class: 'mut small', title: 'no read-only transaction on the server: every agent read on test/prod asks' }, 'reads ask') : null), h('div', { class: 'db-mono mut', title: `database ${c.database || '-'} · user ${c.user || '-'}` }, c.database || '-'), h('div', { class: 'db-creach' }, needsPassword(c) ? h('span', { class: 'warn db-nopw', title: 'no password stored - ⋯ › Set password…' }, ic('warn'), 'no password') : null, reach), h('div', { class: 'db-cacts row-actions' }, testBtn, h('button', { disabled: !c.def && c.reach !== 'stack', onclick: () => editConn(c) }, 'Edit'), more));
    }
    /** IDEA's dataSources.xml of a project: names, users, databases (pick the project first) */
    async function ideaImport(key) {
        const projs = deps.projects();
        let selected = key;
        let generation = 0;
        const projectLabel = () => projs.find((p) => p.key === selected)?.title || selected || 'Choose project…';
        const label = h('span', { class: 'grow' }, projectLabel());
        const pick = h('button', { type: 'button', class: 'db-project-trigger', 'aria-label': 'Project', 'aria-haspopup': 'listbox', 'aria-expanded': 'false' }, ic('folder'), label, fi('chev-d', 'db-caret'));
        const search = h('input', { type: 'search', role: 'combobox', placeholder: 'Find project…', 'aria-label': 'Find project', 'aria-autocomplete': 'list', 'aria-controls': 'db-idea-projects', 'aria-expanded': 'false' });
        const choices = h('div', { class: 'db-project-options', id: 'db-idea-projects', role: 'listbox', 'aria-label': 'Projects' });
        const popup = h('div', { class: 'db-project-popup hidden' }, h('div', { class: 'db-find' }, ic('search'), search), choices, h('div', { class: 'db-project-hint' }, '↑↓ navigate · Enter select · Esc close'));
        const picker = h('div', { class: 'db-project-picker' }, pick, popup);
        let shown = projs;
        let active = 0;
        const rows = h('div', { class: 'db-ideal', 'aria-live': 'polite' }, h('p', { class: 'mut' }, 'Select a project to see its IDEA connections.'));
        const closePicker = (focus = false) => {
            popup.classList.add('hidden');
            pick.setAttribute('aria-expanded', 'false');
            search.setAttribute('aria-expanded', 'false');
            search.removeAttribute('aria-activedescendant');
            if (focus)
                pick.focus();
        };
        const highlight = () => {
            [...choices.children].forEach((el, i) => el.classList.toggle('active', i === active));
            const option = choices.children[active];
            if (shown.length && option) {
                search.setAttribute('aria-activedescendant', option.id);
                option.scrollIntoView({ block: 'nearest' });
            }
            else
                search.removeAttribute('aria-activedescendant');
        };
        const selectProject = (project) => {
            selected = project.key;
            label.textContent = markBidi(projectLabel());
            closePicker(true);
            void fill();
        };
        const drawProjects = () => {
            shown = filterIdeaProjects(projs, search.value);
            active = Math.max(0, shown.findIndex((p) => p.key === selected));
            clear(choices);
            shown.forEach((p, i) => choices.append(h('button', {
                type: 'button', class: 'db-project-option', id: `db-idea-project-${i}`, role: 'option', tabindex: -1,
                'aria-selected': String(p.key === selected), title: p.key,
                onmousedown: (e) => e.preventDefault(), onclick: () => selectProject(p),
            }, ic('folder'), h('span', { class: 'db-project-text' }, h('strong', {}, p.title || p.key), h('span', { class: 'mut db-mono' }, p.key)), p.key === selected ? fi('check', 'db-project-check') : null)));
            if (!shown.length)
                choices.append(h('p', { class: 'mut', role: 'status' }, 'No matching projects.'));
            highlight();
        };
        const openPicker = () => {
            popup.classList.remove('hidden');
            pick.setAttribute('aria-expanded', 'true');
            search.setAttribute('aria-expanded', 'true');
            search.value = '';
            drawProjects();
            search.focus();
        };
        const drawSources = (project, sources) => {
            clear(rows);
            rows.append(h('div', { class: 'db-idea-heading' }, h('strong', {}, 'IDEA connections'), h('span', { class: 'mut small' }, String(sources.length))));
            if (!sources.length)
                rows.append(h('p', { class: 'mut' }, 'No IDEA datasource in this project.'));
            for (const d of sources) {
                rows.append(h('div', { class: 'db-ider' }, classBadge(d.cls_guess), h('span', { class: 'db-mono grow' }, d.name), h('span', { class: 'mut db-mono' }, `${d.user || '?'}@${d.database || 'default'}`), d.ssh ? h('span', { class: 'mut small', title: "IDEA's SSH tunnel" }, `ssh ${d.ssh.user ? d.ssh.user + '@' : ''}${d.ssh.host}`) : null, h('button', { 'aria-label': `Use ${d.name}`, onclick: () => {
                        deps.closeModal();
                        editConn(null, { name: `${project}/${d.cls_guess}/${(d.database || d.user || 'main').toLowerCase().replace(/[^a-z0-9_.-]/g, '-')}`, cls: d.cls_guess,
                            engine: ['postgres', 'oracle', 'mssql'].includes(d.engine) ? d.engine : 'mysql', database: d.database, user: d.user, label: d.name,
                            ...(d.ssh ? { ssh: d.ssh, host: d.db_host ?? '127.0.0.1', port: d.db_port ?? null } : {}) }, project);
                    } }, 'Use')));
            }
        };
        const fill = async () => {
            const request = ++generation;
            const project = selected;
            clear(rows);
            if (!project)
                return;
            rows.append(h('p', { class: 'mut' }, 'reading .idea/dataSources.xml…'));
            const info = await projectInfo(project);
            if (request !== generation || !rows.isConnected)
                return;
            if (!info) {
                clear(rows);
                rows.append(h('p', { class: 'warn' }, 'Could not load IDEA datasources.', h('button', { onclick: () => void fill() }, 'Retry')));
                return;
            }
            drawSources(project, info.idea);
        };
        pick.addEventListener('click', () => popup.classList.contains('hidden') ? openPicker() : closePicker());
        pick.addEventListener('keydown', (e) => { if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            openPicker();
        } });
        search.addEventListener('input', drawProjects);
        search.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                e.preventDefault();
                if (shown.length)
                    active = (active + (e.key === 'ArrowDown' ? 1 : -1) + shown.length) % shown.length;
                highlight();
            }
            else if (e.key === 'Enter') {
                e.preventDefault();
                if (shown[active])
                    selectProject(shown[active]);
            }
            else if (e.key === 'Escape') {
                e.preventDefault();
                e.stopPropagation();
                closePicker(true);
            }
            else if (e.key === 'Tab')
                closePicker();
        });
        picker.addEventListener('focusout', (e) => { if (!(e.relatedTarget instanceof Node) || !picker.contains(e.relatedTarget))
            closePicker(); });
        deps.modal('Import from IDEA', h('div', { class: 'db-idea-import' }, h('p', { class: 'mut' }, "Choose a project to import its IDEA database connections."), h('div', { class: 'db-project-field' }, h('span', { class: 'mut small' }, 'Project'), picker), rows));
        pick.focus();
        if (key)
            void fill();
    }
    /** N10-C-6: a form dialog starts in its first field (the app's other form dialogs do; the focus was on body) */
    function focusForm() {
        setTimeout(() => document.querySelector('#overlay .olayer:last-child .modal :is(input, select, textarea):not([disabled])')?.focus(), 0);
    }
    function editConn(c, preset = {}, projectKey = '') {
        let proj = c ? c.project : projectKey;
        const d = c?.def ?? (c ? { name: c.name, cls: c.class, engine: c.engine, mask: c.mask ?? null } : { name: `${proj || 'project'}/test/main`, cls: 'test', engine: 'mysql', ...preset });
        const stackConn = !!c && c.reach === 'stack';
        const f = (k, v, attrs = {}) => h('input', { name: k, value: v, ...attrs });
        const projSel = h('select', { name: 'project', disabled: !!c }, h('option', { value: '' }, 'project…'), ...deps.projects().map((p) => h('option', { value: p.key, selected: p.key === proj }, p.title || p.key)));
        const name = f('name', d.name, stackConn ? { readonly: 'true' } : {});
        const label = f('label', d.label ?? '', { placeholder: 'short alias (optional)' });
        const cls = h('select', { name: 'cls', disabled: stackConn }, ...['dev', 'test', 'prod'].map((x) => h('option', { value: x, selected: x === d.cls }, x)));
        const ENG = { mysql: 'MySQL / MariaDB', postgres: 'PostgreSQL', oracle: 'Oracle (service name as database)', mssql: 'SQL Server' };
        const eng = h('select', { name: 'engine', disabled: stackConn }, ...Object.keys(ENG).map((x) => h('option', { value: x, selected: x === d.engine }, ENG[x])));
        const db = f('database', d.database ?? '', { placeholder: 'database' });
        const user = f('user', d.user ?? '');
        const via = h('select', { name: 'via' });
        const fillVia = async () => {
            clear(via);
            via.append(h('option', { value: '' }, proj ? '- reach -' : 'pick the project first'));
            const targets = proj ? await projectInfo(proj).then((x) => x?.tunnel_targets ?? []).catch(() => []) : [];
            for (const x of targets) {
                const on = !!d.tunnel && d.tunnel.env === x.env && d.tunnel.target === x.target;
                via.append(h('option', { value: `tunnel:${x.env}:${x.target}`, selected: on, disabled: !x.available && !on }, `tunnel ${x.env} / ${x.target}${x.available ? '' : ' - ' + x.why}`));
            }
            if (d.tunnel && !targets.some((x) => x.env === d.tunnel.env && x.target === d.tunnel.target)) {
                via.append(h('option', { value: `tunnel:${d.tunnel.env}:${d.tunnel.target}`, selected: true }, `tunnel ${d.tunnel.env} / ${d.tunnel.target}`));
            }
            via.append(h('option', { value: 'ssh', selected: !!d.ssh }, 'SSH tunnel (like IDEA\'s SSH/SSL tab)'));
            via.append(h('option', { value: 'direct', selected: !!d.host && !d.ssh }, 'direct host (not localhost)'));
            saveBtn.disabled = false; // Save sends the reach: not before the select knows it
            sync();
        };
        // a new connection's suggested name follows the class (…/test/main → …/dev/main), a name typed by hand stays
        cls.addEventListener('change', () => {
            if (!c)
                name.value = nameForClass(name.value, cls.value);
            if (!c && !agentTouched) {
                agentAccess.checked = cls.value === 'dev';
                syncAgents();
            }
        });
        projSel.addEventListener('change', () => {
            const old = proj;
            proj = projSel.value;
            if (!c && (name.value.startsWith(old + '/') || name.value.startsWith('project/')))
                name.value = name.value.replace(/^[^]*?\/(dev|test|prod)\//, `${proj}/$1/`);
            void fillVia();
        });
        const host = f('host', d.host ?? '', { placeholder: 'db.example.com' });
        const port = f('port', d.port ? String(d.port) : '', { placeholder: '3306', inputmode: 'numeric' });
        const pw = f('password', '', { type: 'password', placeholder: c ? 'unchanged' : 'password', autocomplete: 'new-password' });
        const mask = h('select', { name: 'mask' }, h('option', { value: '', selected: d.mask === null || d.mask === undefined }, 'default (production masked)'), h('option', { value: 'on', selected: d.mask === true }, 'masked for agents'), h('option', { value: 'off', selected: d.mask === false }, 'not masked'));
        const agentAccess = h('input', { type: 'checkbox', name: 'agent_enabled', role: 'switch', 'aria-label': 'Agents can use this connection' });
        agentAccess.checked = agentsEnabled(d, !!c);
        let agentTouched = false;
        const agentText = h('strong');
        const agentHint = h('p', { class: 'mut small' });
        const syncAgents = () => {
            agentText.textContent = agentAccess.checked ? 'Agents can use it' : 'Only you';
            agentHint.textContent = agentAccess.checked ? 'Sessions see this connection in their database tools. Approval rules still apply.'
                : 'Only in your Database view. Hidden from session tools and MCP.';
            mask.disabled = !agentAccess.checked;
            mask.title = agentAccess.checked ? '' : 'Not used: agents have no access. Your masking choice is kept.';
        };
        agentAccess.addEventListener('change', () => { agentTouched = true; syncAgents(); });
        syncAgents();
        const err = h('p', { class: 'bad' });
        const saveBtn = h('button', { class: 'primary', disabled: !stackConn, onclick: () => void save() }, 'Save');
        const row = (lbl, ...el) => h('div', { class: 'db-form-row' }, h('label', {}, lbl), ...el);
        const hostL = h('label', {}, 'Host');
        const portL = h('label', {}, 'Port');
        const directRow = h('div', { class: 'db-form-row db-form-2' }, hostL, host, portL, port);
        const sshHost = f('ssh_host', d.ssh?.host ?? '', { placeholder: 'db-test.example.com or a Host from ~/.ssh/config' });
        const sshPort = f('ssh_port', d.ssh?.port ? String(d.ssh.port) : '', { placeholder: '22', inputmode: 'numeric' });
        const sshUser = f('ssh_user', d.ssh?.user ?? '', { placeholder: 'from ~/.ssh/config' });
        const sshKey = f('ssh_key', d.ssh?.key ?? '', { placeholder: '~/.ssh/id_ed25519 (empty: config + agent)' });
        const sshRows = h('div', {}, h('div', { class: 'db-form-row db-form-2' }, h('label', {}, 'SSH host'), sshHost, h('label', {}, 'Port'), sshPort), h('div', { class: 'db-form-row db-form-2' }, h('label', {}, 'SSH user'), sshUser, h('label', {}, 'Key'), sshKey));
        const hostWarn = h('p', { class: 'warn small hidden' });
        // the project's IDEA SSH configurations: a known SSH host brings its user and key (IDEA's sshConfigs.xml)
        const fromIdea = async () => {
            const known = proj ? ((await projectInfo(proj))?.idea ?? []).map((x) => x.ssh).find((x) => x && x.host === sshHost.value.trim()) : null;
            if (!known)
                return;
            if ((!sshUser.value.trim() || sshUser.value.trim() === dfltUser) && known.user)
                sshUser.value = known.user;
            if (!sshKey.value.trim() && known.key)
                sshKey.value = known.key;
            if (!sshPort.value.trim() && known.port)
                sshPort.value = String(known.port);
            check();
        };
        let dfltUser = '';
        const defaultUser = async () => {
            dfltUser = proj ? (await projectInfo(proj))?.ssh_user ?? '' : '';
            if (dfltUser && via.value === 'ssh' && !sshUser.value.trim()) {
                sshUser.value = dfltUser;
                check();
            }
        };
        const check = () => {
            const same = via.value === 'ssh' && !!host.value.trim() && host.value.trim() === sshHost.value.trim();
            hostWarn.textContent = markBidi(same ? `DB host is the address as ${sshHost.value.trim()} itself sees it - usually 127.0.0.1 (IDEA: localhost), not the server's own name.`
                : via.value === 'ssh' && !sshUser.value.trim() ? 'SSH user empty: ssh logs in with your Mac user name unless ~/.ssh/config names one.' : '');
            hostWarn.classList.toggle('hidden', !hostWarn.textContent);
        };
        for (const x of [host, sshHost, sshUser])
            x.addEventListener('input', check);
        sshHost.addEventListener('change', () => void fromIdea());
        const sync = () => {
            const ssh = via.value === 'ssh';
            directRow.classList.toggle('hidden', via.value !== 'direct' && !ssh);
            sshRows.classList.toggle('hidden', !ssh);
            hostL.textContent = ssh ? 'DB host' : 'Host'; // behind ssh: the database as the SSH server sees it
            host.placeholder = ssh ? '127.0.0.1 (on the SSH server)' : 'db.example.com';
            portL.textContent = ssh ? 'DB port' : 'Port';
            if (ssh && !host.value.trim())
                host.value = '127.0.0.1';
            check();
            if (ssh)
                void defaultUser();
        };
        via.addEventListener('change', sync);
        void fillVia();
        let savedName = c?.name ?? ''; // Test saves too: a later Save renames / keeps the password like an edit
        const save = async (test = false) => {
            if (!proj) {
                err.textContent = 'pick the project';
                return;
            }
            const def = { name: name.value.trim(), cls: cls.value, engine: eng.value, database: db.value.trim(), user: user.value.trim(), label: label.value.trim() };
            def.agent_enabled = agentAccess.checked;
            if (savedName && savedName !== def.name)
                def.old_name = savedName;
            if (via.value.startsWith('tunnel:')) {
                const [, env, target] = via.value.split(':');
                def.tunnel = { env: env, target: target };
            }
            else if (via.value === 'direct') {
                def.host = host.value.trim();
                def.port = Number(port.value) || null;
            }
            else if (via.value === 'ssh') {
                def.ssh = { host: sshHost.value.trim(), ...(sshPort.value ? { port: Number(sshPort.value) } : {}), ...(sshUser.value.trim() ? { user: sshUser.value.trim() } : {}),
                    ...(sshKey.value.trim() ? { key: sshKey.value.trim() } : {}) };
                def.host = host.value.trim() || '127.0.0.1';
                def.port = Number(port.value) || null;
            }
            if (mask.value)
                def.mask = mask.value === 'on';
            err.textContent = '';
            testOut.textContent = '';
            try {
                await dbApi.save(proj, def, pw.value ? pw.value : savedName ? undefined : '');
                savedName = def.name;
                void load(true);
                if (!test) {
                    deps.closeModal();
                    deps.toast('Saved');
                    return;
                }
                testOut.textContent = 'Connecting…';
                const { data } = await dbApi.meta(proj, def.name, 'schemas', '', '', '', true);
                testOut.textContent = markBidi(`Connected · ${data?.schemas.length ?? 0} schemas`);
            }
            catch (e) {
                testOut.textContent = '';
                err.textContent = markBidi(errText(e));
            }
        };
        const testOut = h('p', { class: 'ok small' });
        const testBtn = h('button', { onclick: () => void save(true), title: 'Save without closing and connect' }, 'Test');
        deps.modal(c ? `Edit · ${short(c)}` : 'Add connection', h('div', { class: 'db-form' }, row('Project', projSel), row('Name', name), row('Alias', label), h('div', { class: 'db-form-row db-form-2' }, h('label', {}, 'Class'), cls, h('label', {}, 'Engine'), eng), stackConn ? null : row('Database', db), stackConn ? null : row('User', user), stackConn ? null : row('Reach', via), stackConn ? null : sshRows, stackConn ? null : directRow, stackConn ? null : hostWarn, stackConn ? null : row('Password', pw), row('Agents', h('div', {}, h('div', { class: 'row left' }, h('label', { class: 'switch' }, agentAccess, h('span', { class: 'track', 'aria-hidden': 'true' })), agentText), agentHint)), row('Masking', mask), h('p', { class: 'mut small' }, stackConn ? 'A dev connection comes from the checkout\'s dev stack; the alias, agent access and masking are yours.'
            : 'test / prod go only through a tunnel Piper opens (dev stack or ssh) - never your own port-forward on localhost - and the server is pinned at first use. SSH runs without a terminal: the key must be in ssh-agent / the keychain and the server in known_hosts. The password stays in Piper\'s home on this machine (mode 600); sessions never see it.'), err, testOut, h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), stackConn ? null : testBtn, saveBtn)));
        focusForm();
    }
    function setPassword(c) {
        const pw = h('input', { type: 'password', autocomplete: 'new-password', placeholder: 'password' });
        const err = h('p', { class: 'bad' });
        const go = async (value) => {
            try {
                await dbApi.save(pj(c.name), c.def ?? { name: c.name, cls: c.class, engine: c.engine }, value);
                deps.closeModal();
                deps.toast(value ? 'Password stored' : 'Password removed');
                void load(true);
            }
            catch (e) {
                err.textContent = markBidi(errText(e));
            }
        };
        pw.addEventListener('keydown', (e) => { if (e.key === 'Enter')
            void go(pw.value); });
        deps.modal(`Password · ${short(c)}`, h('p', { class: 'mut' }, 'Stored in Piper\'s home on this machine (mode 600). Sessions never see it.'), pw, err, h('div', { class: 'row' }, h('button', { onclick: deps.closeModal }, 'Cancel'), c.has_password ? h('button', { class: 'danger', onclick: () => void go('') }, 'Remove') : null, h('button', { class: 'primary', onclick: () => void go(pw.value) }, 'Store')));
        setTimeout(() => pw.focus(), 0);
    }
    // ---------------------------------------------------------------- right panel
    function drawRight() {
        clear(rightEl);
        const t = tab();
        const scroll = h('div', { class: 'db-rscroll' });
        rightEl.append(scroll);
        if (t && t.kind === 'table' && !rightHistory) {
            const i = t.info;
            scroll.append(h('div', { class: 'db-rsec' }, 'Table · ', h('span', { class: 'db-mono db-rsec-v' }, showBidi(t.table))));
            if (i) {
                const cl = h('div', { class: 'db-rlist' });
                for (const c of i.columns) {
                    const fk = i.foreign_keys.find((f) => f.column === c.name);
                    cl.append(h('div', { class: 'db-rcol', title: showBidi(c.comment || '') }, h('span', { class: 'db-rcn' }, showBidi(c.name)), h('span', { class: 'db-rct' }, c.type), h('span', { class: 'grow' }), i.primary_key.includes(c.name) ? h('span', { class: 'db-pkt' }, 'PK')
                        : fk ? h('button', { class: 'db-fkt', title: showBidi(`open ${fk.ref_table}`), onclick: () => openTable(t.conn, fk.ref_schema || t.schema, fk.ref_table) }, showBidi(`FK → ${fk.ref_table}`)) : null));
                }
                scroll.append(cl, h('button', { class: 'db-rbtn', onclick: () => { t.showDdl = !t.showDdl; drawBody(); } }, t.showDdl ? 'Show data' : 'Show DDL'));
                if (i.referenced_by.length) {
                    scroll.append(h('div', { class: 'db-rsec' }, 'Referenced by'));
                    const pkCol = i.primary_key[0];
                    const row = t.sel && t.sel.r >= 0 ? t.rows[t.sel.r] : null;
                    for (const r of i.referenced_by) {
                        const ci = t.cols.findIndex((x) => x.name === (r.ref_column || pkCol));
                        const v = row && ci >= 0 ? row[ci] : null;
                        const ck = `${t.conn}|${r.schema || t.schema}|${r.table}.${r.column}=${cellText(v)}`;
                        const eng = curConn(t.conn)?.engine ?? 'mysql';
                        if (v !== null && v !== undefined && !refCounts.has(ck)) { // "3 rows ›" for the selected row
                            refCounts.set(ck, 'loading');
                            void dbApi.query({ project: pj(t.conn), connection: t.conn, schema: r.schema || t.schema, session, max_rows: 1,
                                sql: `SELECT COUNT(*) FROM ${quoteIdent(r.table, eng)} WHERE ${quoteIdent(r.column, eng)} = ${sqlLiteral(v, eng)} ${LOOKUP}` })
                                .then(({ data }) => { refCounts.set(ck, Number(data?.rows[0]?.[0] ?? 0)); drawRight(); })
                                .catch(() => { refCounts.set(ck, 'error'); });
                        }
                        const n = refCounts.get(ck);
                        scroll.append(h('button', { class: 'db-rlink', title: v === null || v === undefined ? `open ${r.table}` : `the rows of ${r.table} referencing the selected row`, onclick: () => {
                                openTable(t.conn, r.schema || t.schema, r.table, v === null || v === undefined ? '' : `${quoteIdent(r.column, eng)} = ${sqlLiteral(v, eng)}`);
                            } }, h('span', { class: 'db-mono' }, showBidi(`${r.table}.${r.column}`)), h('span', { class: 'grow' }), h('span', { class: 'mut' }, typeof n === 'number' ? `${n} row${n === 1 ? '' : 's'} ›` : '›')));
                    }
                }
            }
            else
                scroll.append(h('p', { class: 'mut db-pad' }, 'loading…'));
            const cq = claudeLast(t.conn).filter((r) => !session || r.session === session);
            const waiting = deps.pending().filter((p) => p.kind.startsWith('db_') && p.summary.includes(t.conn) && (!session || p.session === session));
            scroll.append(h('div', { class: 'db-rsec' }, `Claude queries · ${cq.length + waiting.length}`));
            for (const p of waiting) {
                const sql = p.summary.split('\n').find((l) => /^\s*(UPDATE|INSERT|DELETE|REPLACE|CREATE|ALTER|DROP|SELECT|WITH|TRUNCATE)\b/i.test(l)) ?? p.summary.split('\n')[0] ?? '';
                const rows = /affects ([\d, ]+) row/.exec(p.summary)?.[1];
                scroll.append(h('div', { class: 'db-cq' }, h('div', { class: 'db-cq-l' }, h('span', { class: 'db-cq-t' }, 'now'), h('span', { class: 'db-mono' }, sql.slice(0, 160))), h('div', { class: 'db-cq-m warn' }, `waiting for your card${rows ? ` · ${rowsText(rows)}` : ''}`)));
            }
            for (const r of cq.slice(0, 12)) {
                scroll.append(h('button', { class: 'db-cq', title: r.sql ?? r.tool, onclick: () => r.sql && openConsole(t.conn, r.sql + ';') }, h('div', { class: 'db-cq-l' }, h('span', { class: 'db-cq-t' }, agoWords(r.ts, true)), h('span', { class: 'db-mono' }, (r.sql ?? r.tool.replace(/^db_/, 'db_')).replace(/\s+/g, ' ').slice(0, 160))), h('div', { class: 'db-cq-m' + (r.error ? ' bad' : '') }, r.error ? r.error.slice(0, 100) : histMeta(r))));
            }
            if (!cq.length && !waiting.length)
                scroll.append(h('p', { class: 'mut db-pad' }, 'Claude has not used this connection yet.'));
        }
        else {
            const rows = history.filter((r) => (histFilter === 'all' || r.origin === histFilter) && !r.sql?.endsWith(LOOKUP)); // every connection, every session; not the view's own lookups
            const pill = (k, label) => h('button', { class: 'db-pill' + (histFilter === k ? ' on' : ''), onclick: () => { histFilter = k; drawRight(); } }, label);
            const today = rows.length && new Date(rows[0].ts * 1000).toDateString() === new Date().toDateString();
            scroll.append(h('div', { class: 'db-rsec' }, 'History · ', h('span', { class: 'db-rsec-v' }, today || !rows.length ? 'today' : 'recent')), h('div', { class: 'db-pills' }, pill('all', 'All'), pill('gui', 'Mine'), pill('mcp', 'Claude')));
            for (const r of rows.slice(0, 80)) {
                const sql = historySql(r, curConn(r.connection)?.engine);
                scroll.append(h('button', { class: 'db-hi' + (r.error ? ' bad' : ''), title: (sql || r.tool) + (r.error ? '\n\n' + r.error : '') + '\n\nclick: into the console', onclick: () => {
                        if (!sql)
                            return;
                        const ct = tab();
                        if (ct && ct.kind === 'console') {
                            ct.text = (ct.text.trim() ? ct.text.replace(/\s*$/, '\n\n') : '') + sql + (sql.trim().endsWith(';') ? '' : ';');
                            ct.caret = ct.text.length;
                            drawBody();
                        }
                        else
                            openConsole(curConn(r.connection) ? r.connection : conn, sql + ';');
                    } }, h('div', { class: 'db-hm' }, ic(r.origin === 'mcp' ? 'bot' : 'user', 'db-who'), h('span', { class: 'mut' }, new Date(r.ts * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false })), classBadge(r.class ?? 'prod'), 
                // the list holds every connection: one that is not the open one says which (else "SELECT 1 · test" is any of four)
                r.connection && r.connection !== conn ? h('span', { class: 'mut db-mono', title: r.connection }, r.connection.split('/').slice(-1)[0] ?? '') : null, // db_connections / db_history: no connection
                h('span', { class: 'grow' }), h('span', { class: 'db-ins', title: 'into the console' }, '↵')), h('div', { class: 'db-hs' }, (sql || TOOL_LABEL[r.tool] || r.tool).replace(/\s+/g, ' ').slice(0, 220)), h('div', { class: 'db-hmeta' + (r.error ? ' bad' : r.tool.startsWith('gui_tx_exec') && /^\s*(update|insert|delete|replace|merge)\b/i.test(r.sql ?? '') ? ' warn' : '') }, r.error ? r.error.slice(0, 120) : histMeta(r))));
            }
            if (!rows.length)
                scroll.append(h('p', { class: 'mut db-pad' }, 'No queries yet.'));
        }
        if (t && t.kind === 'table') {
            rightEl.append(h('div', { class: 'db-rfoot' }, h('button', { class: rightHistory ? 'on' : undefined, onclick: () => { rightHistory = !rightHistory; drawRight(); } }, rightHistory ? 'Table' : 'History'), h('button', { onclick: () => openConsole(t.conn, `SELECT * FROM ${tableRef(t.schema, t.table, curConn(t.conn)?.engine ?? 'mysql')}${t.where ? ' WHERE ' + t.where : ''};\n`) }, 'Open console'))); // the caret on a line of its own: what you type is a next statement
        }
    }
    function histMeta(r) {
        if (r.approved === 'idempotent-replay')
            return 'same statements again - answered from the first run, nothing ran';
        const parts = [];
        const dml = /^\s*(update|insert|delete|replace|merge)\b/i.test(r.sql ?? '');
        if (r.rowcount && dml)
            parts.push(`${rowsText(r.rowcount.reduce((a, b) => a + Math.max(0, b), 0))}${r.tool.startsWith('gui_tx_exec') ? ' · not committed' : ' changed'}`);
        else if (r.rowcount && r.rows === null)
            parts.push(rowsText(r.rowcount.reduce((a, b) => a + Math.max(0, b), 0)));
        else if (r.rows !== null && r.rows !== undefined)
            parts.push(`${r.rows} row${r.rows === 1 ? '' : 's'}`);
        if (r.elapsed_ms !== null && r.elapsed_ms !== undefined)
            parts.push(`${r.elapsed_ms} ms`);
        if (r.approved === 'card')
            parts.push('approved on a card');
        else if (r.approved === 'session')
            parts.push('allowed for the session');
        return parts.join(' · ') || r.tool;
    }
    // ---------------------------------------------------------------- keys (when the view is shown)
    function onKey(e) {
        const mod = e.metaKey || e.ctrlKey;
        if (treeOn && e.key === 'Escape' && !mod) {
            e.preventDefault();
            treeOn = false;
            draw();
            return true;
        }
        const t = tab();
        // ⌘⇧L: a new console here; with no connection open it stays the global key (the queue)
        if (mod && e.shiftKey && e.key.toLowerCase() === 'l' && conn) {
            e.preventDefault();
            openConsole();
            return true;
        }
        if (mod && e.key.toLowerCase() === 's' && t?.kind === 'table') {
            e.preventDefault();
            void submit(t);
            return true;
        }
        // a console's ⌘E / ⌘↩ (its toolbar says so): never the global Composer / send behind it, also with the focus
        // on a toolbar button or a result; the editor's own handler ran first when the caret is in it
        if (t?.kind === 'console' && mod && !e.altKey && !e.shiftKey && (e.key.toLowerCase() === 'e' || e.key === 'Enter')) {
            if (!e.defaultPrevented) {
                e.preventDefault();
                void runConsole(t, e.key.toLowerCase() === 'e');
            }
            return true;
        }
        // ⌘P finds a table (S1); without an open connection it stays the global quick open
        if (conn && mod && !e.shiftKey && e.key.toLowerCase() === 'p') {
            const f = leftEl.querySelector('.db-search');
            if (!f)
                return false;
            e.preventDefault();
            f.focus();
            f.select();
            return true;
        }
        return false;
    }
    /** open the view; `connection` opens that one (the context panel's "Open view"), else the last state */
    function show(connection = '') {
        if (connection && curConn(connection))
            openConn(connection);
        else
            draw();
        void load(!!connection || !all).then(() => { if (connection && conn !== connection && curConn(connection))
            openConn(connection); });
    }
    /** a poll arrived (tunnel states, a session's queries): refresh what is cheap */
    function refresh() {
        void load(true);
    }
    /** W26: the open connection for a grid pane's title (null = the picker / overview) */
    function current() {
        const c = conn ? curConn(conn) : undefined;
        return c ? { name: c.name, label: short(c), cls: c.class, project: all?.projects.find((g) => g.project === c.project)?.title ?? c.project, key: c.project, engine: c.engine } : null;
    }
    async function openReference(project, connection, schema, table) {
        await load(true);
        const c = curConn(connection);
        if (!c || c.project !== project)
            throw new Error('The table connection does not belong to this project');
        openTable(connection, schema, table);
    }
    return { el, show, refresh, onKey, current, openReference, hasUnsaved: () => tabs.some((t) => (t.kind === 'table' && pendingCount(t) > 0) || (t.kind === 'console' && (t.running || !!t.tx?.open))) };
}
/** "2 min" / "now" (short: for the Claude queries list) or "2 min ago" */
function agoWords(ts, shortForm = false) {
    const s = Math.max(0, Date.now() / 1000 - ts);
    const v = s < 60 ? 'now' : s < 3600 ? `${Math.floor(s / 60)} min` : s < 86400 ? `${Math.floor(s / 3600)} h` : `${Math.floor(s / 86400)} d`;
    return shortForm || v === 'now' ? v : `${v} ago`;
}
const ICON_PATHS = {
    search: ['M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14z', 'M20 20l-4-4'],
    table: ['M4 5h16v14H4z', 'M4 10h16', 'M10 10v9'],
    eye: ['M2 12s3.5-6 10-6s10 6 10 6s-3.5 6-10 6S2 12 2 12z', 'M12 9a3 3 0 1 0 0 6a3 3 0 1 0 0-6z'],
    fn: ['M9 4c-2 0-3 1-3 3v10c0 2-1 3-3 3', 'M4 10h7', 'M14 9l6 6', 'M20 9l-6 6'],
    col: ['M5 4h14v16H5z', 'M10 4v16', 'M14 4v16'],
    kw: ['M5 7h14', 'M5 12h10', 'M5 17h6'],
    key: ['M8 14a4 4 0 1 1 3-6.6L20 7v3h-2v2h-2v2h-3.4A4 4 0 0 1 8 14z'],
    link: ['M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1', 'M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1'],
    pencil: ['M4 20h4L19 9l-4-4L4 16z', 'M13.5 6.5l4 4'],
    warn: ['M12 4l9 16H3z', 'M12 10v4', 'M12 17h.01'],
    user: ['M12 4a4 4 0 1 0 0 8a4 4 0 1 0 0-8z', 'M4 21c1-4 4-6 8-6s7 2 8 6'],
    bot: ['M5 9h14v10H5z', 'M12 5v4', 'M9 14h.01', 'M15 14h.01', 'M2 13v3', 'M22 13v3'],
    db: ['M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3s-3.6 3-8 3s-8-1.3-8-3z', 'M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6', 'M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3'],
    stack: ['M4 5h16v6H4z', 'M4 13h16v6H4z', 'M8 8h.01', 'M8 16h.01'],
    shield: ['M12 3l8 3v6c0 5-3.5 8-8 9c-4.5-1-8-4-8-9V6z'],
    globe: ['M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18z', 'M3 12h18', 'M12 3c3 3 3 15 0 18', 'M12 3c-3 3-3 15 0 18'],
    folder: ['M3 6h6l2 2h10v11H3z'],
    lock: ['M6 11h12v9H6z', 'M8 11V8a4 4 0 0 1 8 0v3'],
    eyeoff: ['M3 3l18 18', 'M10.6 6.1A10 10 0 0 1 12 6c6.5 0 10 6 10 6a17 17 0 0 1-3 3.6', 'M6.6 6.6C3.8 8.4 2 12 2 12s3.5 6 10 6a9.7 9.7 0 0 0 5.4-1.6', 'M9.9 9.9a3 3 0 0 0 4.2 4.2'],
};
function ic(name, cls = '') {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('class', 'db-ic' + (cls ? ' ' + cls : ''));
    for (const d of ICON_PATHS[name] ?? []) {
        const p = document.createElementNS(ns, 'path');
        p.setAttribute('d', d);
        svg.append(p);
    }
    return svg;
}
//# sourceMappingURL=dbview.js.map