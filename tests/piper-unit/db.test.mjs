// node --test: pure helpers of the Database view (dbview.ts), from web/dist
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { filterIdeaProjects, cellShown, cellText, SQL_DEFAULT, SQL_NOW_DATE, SQL_NOW_TIME, SQL_NOW_TS, changeStatements, isTemporal, sqlNow, coerceEdit, completions, fuzzyMatch, formatSql, historySql, mentionedTables, quoteIdent, showStatement, sqlLiteral, statementAt, toCsv, toInserts } from '../../piper/web/dist/dbview.js';

test('quoteIdent per engine', () => {
  assert.equal(quoteIdent('a`b', 'mysql'), '`a``b`');
  assert.equal(quoteIdent('a"b', 'postgres'), '"a""b"');
});

test('changeStatements: deletes, updates by primary key, inserts - values as params', () => {
  const cols = ['id', 'name', 'price'];
  const rows = [[1, 'a', 10], [2, 'b', 20], [3, null, 30]];
  const edits = new Map([[0, new Map([[1, 'A'], [2, 11]])], [1, new Map([[1, 'gone']])]]);
  const out = changeStatements('mysql', 'shop', 'item', cols, ['id'], rows, edits, new Set([1]), [new Map([[1, 'new'], [2, 5]]), new Map()]);
  assert.deepEqual(out, [
    { sql: 'DELETE FROM `shop`.`item` WHERE `id` = %s', params: [2] },
    { sql: 'UPDATE `shop`.`item` SET `name` = %s, `price` = %s WHERE `id` = %s', params: ['A', 11, 1] },
    { sql: 'INSERT INTO `shop`.`item` (`name`, `price`) VALUES (%s, %s)', params: ['new', 5] },
    { sql: 'INSERT INTO `shop`.`item` () VALUES ()', params: [] },
  ]);
  const pg = changeStatements('postgres', null, 't', ['a', 'b'], ['a', 'b'], [[null, 2]], new Map([[0, new Map([[1, 3]])]]), new Set(), [new Map()]);
  assert.deepEqual(pg, [{ sql: 'UPDATE "t" SET "b" = %s WHERE "a" IS NULL AND "b" = %s', params: [3, 2] },
    { sql: 'INSERT INTO "t" DEFAULT VALUES', params: [] }]);
  assert.throws(() => changeStatements('mysql', null, 't', ['x'], ['id'], [[1]], new Map(), new Set([0]), []));
});

test('showStatement inlines params for display only', () => {
  assert.equal(showStatement({ sql: 'UPDATE t SET a = %s WHERE id = %s', params: ["it's", 3] }), "UPDATE t SET a = 'it''s' WHERE id = 3");
  assert.equal(sqlLiteral(null), 'NULL');
  assert.equal(sqlLiteral('a\\b'), "'a\\\\b'");
  assert.equal(sqlLiteral('a\\b', 'postgres'), "'a\\b'");   // only MySQL escapes with a backslash
  assert.equal(sqlLiteral("it's", 'mssql'), "'it''s'");
});

test('statementAt: the statement under the caret, ; in strings and comments ignored', () => {
  const t = "SELECT 'a;b' FROM t;\n-- x; y\nUPDATE u SET a=1 WHERE id=2;\nSELECT 3";
  assert.equal(statementAt(t, 3).sql, "SELECT 'a;b' FROM t");
  assert.equal(statementAt(t, t.indexOf('UPDATE') + 2).sql, '-- x; y\nUPDATE u SET a=1 WHERE id=2');
  assert.equal(statementAt(t, t.length).sql, 'SELECT 3');
  assert.equal(statementAt('SELECT 1;', 9).sql, 'SELECT 1');
});

test('toCsv / toInserts', () => {
  assert.equal(toCsv(['a', 'b'], [[1, 'x,y'], [null, 'q"']]), 'a,b\n1,"x,y"\n,"q"""\n');
  assert.equal(toInserts('mysql', null, 't', ['a', 'b'], [[1, null]]), 'INSERT INTO `t` (`a`, `b`) VALUES (1, NULL);\n');
});

test('formatSql: keywords upper-case, clauses on lines, strings untouched', () => {
  assert.equal(formatSql("select a from t where b = 'select x from y' order by a"), "SELECT a\nFROM t\nWHERE b = 'select x from y'\nORDER BY a");
});

test('completions: columns of mentioned tables first, then tables, then keywords', () => {
  const cols = new Map([['product', ['price', 'product_id']], ['order', ['placed']]]);
  assert.deepEqual(completions('pr', ['product', 'order', 'price_list'], cols, 'SELECT pr FROM product'), ['price', 'product_id', 'product', 'price_list']);
  assert.deepEqual(completions('sel', [], new Map(), ''), ['SELECT']);
  assert.deepEqual(completions('', ['a'], new Map(), ''), []);
  assert.deepEqual(mentionedTables('select * from product p join `order` o', ['product', 'order', 'x']), ['product', 'order']);
});

test('parseCsv: quotes, escaped quotes, CRLF, the delimiter from the header', async () => {
  const { parseCsv, csvInserts } = await import('../../piper/web/dist/dbview.js');
  assert.deepEqual(parseCsv('a,b\r\n1,"x, ""y"""\r\n2,\n'), [['a', 'b'], ['1', 'x, "y"'], ['2', '']]);
  assert.deepEqual(parseCsv('a;b\n1;2'), [['a', 'b'], ['1', '2']]);
  assert.deepEqual(parseCsv('a,b\n"multi\nline",3\n'), [['a', 'b'], ['multi\nline', '3']]);
  const st = csvInserts('mysql', null, 't', ['a', 'b'], [[1, 2], [3, null], [5, 6]], 2);
  assert.deepEqual(st, [{ sql: 'INSERT INTO `t` (`a`, `b`) VALUES (%s, %s), (%s, %s)', params: [1, 2, 3, null] },
    { sql: 'INSERT INTO `t` (`a`, `b`) VALUES (%s, %s)', params: [5, 6] }]);
});

test('Set DEFAULT: the keyword in UPDATE and INSERT, no parameter', () => {
  const out = changeStatements('mysql', null, 't', ['id', 'a', 'b'], ['id'], [[1, 'x', 'y']],
    new Map([[0, new Map([[1, SQL_DEFAULT], [2, null]])]]), new Set(), [new Map([[0, 5], [1, SQL_DEFAULT]])]);
  assert.deepEqual(out, [
    { sql: 'UPDATE `t` SET `a` = DEFAULT, `b` = %s WHERE `id` = %s', params: [null, 1] },
    { sql: 'INSERT INTO `t` (`id`, `a`) VALUES (%s, DEFAULT)', params: [5] },
  ]);
  assert.equal(showStatement(out[0]), 'UPDATE `t` SET `a` = DEFAULT, `b` = NULL WHERE `id` = 1');
});

test('Set now: the engine keyword in UPDATE and INSERT, no parameter', () => {
  const out = changeStatements('postgres', null, 't', ['id', 'at', 'd'], ['id'], [[1, 'x', 'y']],
    new Map([[0, new Map([[1, SQL_NOW_TS], [2, 'z']])]]), new Set(), [new Map([[0, 5], [2, SQL_NOW_DATE]])]);
  assert.deepEqual(out, [
    { sql: 'UPDATE "t" SET "at" = CURRENT_TIMESTAMP, "d" = %s WHERE "id" = %s', params: ['z', 1] },
    { sql: 'INSERT INTO "t" ("id", "d") VALUES (%s, CURRENT_DATE)', params: [5] },
  ]);
  assert.equal(showStatement(out[0], 'postgres'), 'UPDATE "t" SET "at" = CURRENT_TIMESTAMP, "d" = \'z\' WHERE "id" = 1');
  assert.equal(sqlLiteral(SQL_NOW_TIME), 'CURRENT_TIME');
  // the narrower keywords only where the engine has them
  assert.equal(sqlNow('mysql', 'date'), SQL_NOW_DATE);
  assert.equal(sqlNow('postgres', 'time without time zone'), SQL_NOW_TIME);
  assert.equal(sqlNow('mysql', 'datetime(6)'), SQL_NOW_TS);
  assert.equal(sqlNow('postgres', 'timestamp with time zone'), SQL_NOW_TS);
  assert.equal(sqlNow('mssql', 'date'), SQL_NOW_TS);
  assert.equal(sqlNow('oracle', 'date'), SQL_NOW_DATE);
  assert.equal(sqlNow('oracle', 'time'), SQL_NOW_TS);
  // only date / time columns get the menu item
  assert.ok(isTemporal('DATETIME') && isTemporal('timestamp(3)') && isTemporal('date') && isTemporal('time'));
  assert.ok(!isTemporal('varchar(32)') && !isTemporal('int') && !isTemporal(undefined) && !isTemporal('year'));
});

test('coerceEdit: BIT takes a number, everything else stays text', () => {
  assert.equal(coerceEdit('bit', '1'), 1);
  assert.equal(coerceEdit('bit', 'false'), 0);
  assert.equal(coerceEdit('bit', 'x'), 'x');
  assert.equal(coerceEdit('varchar', '1'), '1');
  assert.equal(coerceEdit(undefined, '1'), '1');
});

test('fuzzyMatch: ranks exact, prefix, word prefix, initials, substring; all words must match', () => {
  const names = ['acme_order', 'acme_order_item', 'ACME_ORDER_PAYMENT', 'customer', 'DARWIN_PATCH', 'AcmePageTree', 'v_order_summary'];
  const rank = (q) => names.map((n) => [n, fuzzyMatch(q, n)]).filter(([, m]) => m).sort((a, b) => b[1].score - a[1].score).map(([n]) => n);
  assert.deepEqual(rank('acme_order').slice(0, 2), ['acme_order', 'acme_order_item']);
  assert.equal(rank('order')[0], 'acme_order');            // a word of the name, shortest first
  assert.equal(rank('eoi')[0], 'acme_order_item');          // initials
  assert.equal(rank('ept')[0], 'AcmePageTree');             // camel humps
  assert.equal(rank('ordit')[0], 'acme_order_item');        // a word, then a jump to the next one
  assert.deepEqual(rank('order pay'), ['ACME_ORDER_PAYMENT']); // every word must match
  assert.equal(rank('orderitem')[0], 'acme_order_item');    // underscores ignored
  assert.deepEqual(rank('zzz'), []);
  assert.deepEqual(fuzzyMatch('dp', 'DARWIN_PATCH').hits, [0, 7]);
  assert.deepEqual(fuzzyMatch('', 'x'), { score: 0, hits: [] });
});

test('rowsText: one row, more rows (F-DB-PROD G-2)', async () => {
  const { rowsText } = await import('../../piper/web/dist/dbview.js');
  assert.equal(rowsText(1), '1 row');
  assert.equal(rowsText(0), '0 rows');
  assert.equal(rowsText(' 1 '), '1 row');
  assert.match(rowsText(1234), /^1[\s,.\u00a0\u202f]?234 rows$/);   // the viewer's locale, no fixed one
});

test('nameForClass: the suggested name follows the class, a hand-made name stays (G-1)', async () => {
  const { nameForClass } = await import('../../piper/web/dist/dbview.js');
  assert.equal(nameForClass('team-x/shop/test/main', 'dev'), 'team-x/shop/dev/main');
  assert.equal(nameForClass('team-x/shop/prod/', 'test'), 'team-x/shop/test/');
  assert.equal(nameForClass('my-connection', 'prod'), 'my-connection');
});

test('gridSql: the own sort, else the primary key - a stable order and paging (G-7)', async () => {
  const { gridSql } = await import('../../piper/web/dist/dbview.js');
  assert.equal(gridSql('postgres', 'customer', '', '', false, ['id']), 'SELECT * FROM "customer" ORDER BY "id"');
  assert.equal(gridSql('mysql', 'o', 'x > 1', 'total', true, ['a', 'b']), 'SELECT * FROM `o` WHERE x > 1 ORDER BY `total` DESC');
  assert.equal(gridSql('mysql', 'v', '', '', false, []), 'SELECT * FROM `v`');
  assert.equal(gridSql('mysql', 'o', '', '', false, ['a', 'b']), 'SELECT * FROM `o` ORDER BY `a`, `b`');
});

test('historySql: a grid write comes back runnable (values in), a masked one keeps its placeholders', () => {
  // live Oracle: the history put `UPDATE "CUSTOMER" SET "NOTE" = %s WHERE "ID" = %s` into the console
  const r = { sql: 'UPDATE "CUSTOMER" SET "NOTE" = %s WHERE "ID" = %s\n;\nDELETE FROM "CUSTOMER" WHERE "ID" = %s\n;\nUPDATE "CUSTOMER" SET "EMAIL" = %s WHERE "ID" = %s',
    params: [["it's", 5], [7], null] };
  assert.equal(historySql(r, 'oracle'),
    'UPDATE "CUSTOMER" SET "NOTE" = \'it\'\'s\' WHERE "ID" = 5;\nDELETE FROM "CUSTOMER" WHERE "ID" = 7;\nUPDATE "CUSTOMER" SET "EMAIL" = %s WHERE "ID" = %s');
  assert.equal(historySql({ sql: 'SELECT 1', params: null }), 'SELECT 1');
  assert.equal(historySql({ sql: null }), '');
});

test('N3: the grid shows a bidi control in a value as [U+202E], the value itself stays as it is', () => {
  assert.equal(cellShown('rtl \u202Eevil', 300), 'rtl [U+202E]evil');
  assert.equal(cellText('rtl \u202Eevil'), 'rtl \u202Eevil');
  assert.equal(cellShown('Jürgen 🎉 <script>', 300), 'Jürgen 🎉 <script>');
  assert.equal(cellShown(null, 300), 'NULL');
});

test('N4: table / column / FK / routine names with a bidi control show as marks in the tree, grid header, tabs, menus, structure and DDL (over dist)', async () => {
  const { readFileSync } = await import('node:fs');
  const js = readFileSync(new URL('../../piper/web/dist/dbview.js', import.meta.url), 'utf8');
  for (const s of ["'db-tn' }, showBidi(it.name)", "'db-tn' }, showBidi(r.name)", "'db-ghn' }, showBidi(c.name)", "'db-mi-t' }, showBidi(text)",
    "? showBidi(t.table) : `Console", "'db-rcn' }, showBidi(c.name)", 'showBidi(`FK → ${fk.ref_table}`)', "h('b', {}, showBidi(run)) : showBidi(run)"]) assert.ok(js.includes(s), s);
});

test('N9: the DB console warns when the SQL holds a direction control (it shows the text raw, reversed on screen)', async () => {
  const { hasBidi } = await import('../../piper/web/dist/text.js');
  assert.equal(hasBidi("SELECT '‮evil'"), true);
  assert.equal(hasBidi("SELECT 'a⁦b'"), true);
  assert.equal(hasBidi("SELECT 'café 👨‍👩‍👧'"), false, 'ZWJ / NFD are not direction controls');
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/dbview.ts', import.meta.url), 'utf8');
  assert.match(src, /bidiWarn\.classList\.toggle\('hidden', !hasBidi\(ta\.value\)\)/);
});

test('N10-C-1: the console scrolls the active result tab into its strip (a new result was off screen at 1024)', async () => {
  const { scrollToShow } = await import('../../piper/web/dist/dbview.js');
  assert.equal(scrollToShow(0, 465, 480, 150), 165, 'a tab past the right edge: its right edge on the strip\'s');
  assert.equal(scrollToShow(300, 465, 100, 120), 100, 'a tab left of the view: its left edge first');
  assert.equal(scrollToShow(50, 465, 100, 120), 50, 'a tab in view: the strip stays');
  assert.equal(scrollToShow(0, 100, 0, 300), 0, 'a tab wider than the strip: its start');
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/dbview.ts', import.meta.url), 'utf8');
  assert.match(src, /rtabs\.scrollLeft = scrollToShow\(/);
  assert.match(src, /\n    showActiveResult\(\);   \/\/ N10-C-1/, 'drawConsole applies it after every draw (a redraw resets scrollLeft)');
});

test('N10-C-2 / C-4: a narrow console keeps the mode and the path in the status bar, the transaction banner puts its buttons under the text', async () => {
  const { readFileSync } = await import('node:fs');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  const src = readFileSync(new URL('../../piper/web/src/dbview.ts', import.meta.url), 'utf8');
  assert.match(src, /class: 'mut db-st-hint' }, (kbd\()?'⌘↩ run/, 'the console hint has its own class');
  assert.match(css, /\.db-main \{[^}]*container: dbmain \/ inline-size/, '.db-main is a size container');
  const q = css.match(/@container dbmain \(max-width: (\d+)px\) \{([^]*?)\n\}/);
  assert.ok(q, '@container dbmain block');
  assert.ok(Number(q[1]) >= 560, 'the 1024 + drawer width (341 px) is inside it');
  assert.match(css, /\.db-status > \.mut\.db-st-hint \{[^}]*flex: 0 1000 auto;[^}]*min-width: 0/,
    'the shortcut hint gives way first (ellipsis), never the mode (tx open) or the path (both were 0 px at 1024 + drawer)');
  assert.match(q[2], /\.db-tx \{[^}]*flex-wrap: wrap/, 'the banner wraps');
  assert.match(q[2], /\.db-rhead > \.db-rtabs \{ flex: 1 0 100%; \}/, 'the result tabs get a row of their own (96 px beside the pager and Export)');
  assert.match(q[2], /\.db-status > \.mut\.db-st-hint \{ display: none; \}/, 'below that the hint goes (a 7 px sliver of it at 341 px)');
  assert.match(src, /new ResizeObserver\(\(\) => showActiveResult\(\)\)/, 'a narrower strip (the drawer opened) keeps the active result in view');
  assert.match(q[2], /\.db-tx-t \{[^}]*flex: 1 1 calc\(100% - 40px\)/, 'its text takes the row, the buttons go below');
});

test('N10-C-3: the Database view menus (Switch, rows per page, Export, Sort, cell menu...) work from the keyboard like every other menu', async () => {
  const { menuMove } = await import('../../piper/web/dist/dbview.js');
  assert.equal(menuMove(3, 0, 'ArrowDown'), 1);
  assert.equal(menuMove(3, 2, 'ArrowDown'), 0, 'wraps');
  assert.equal(menuMove(3, 0, 'ArrowUp'), 2, 'wraps');
  assert.equal(menuMove(3, -1, 'ArrowDown'), 0);
  assert.equal(menuMove(3, 1, 'Home'), 0);
  assert.equal(menuMove(3, 1, 'End'), 2);
  assert.equal(menuMove(3, 1, 'x'), -1, 'other keys: not the menu\'s');
  assert.equal(menuMove(0, 0, 'ArrowDown'), -1);
  const { readFileSync } = await import('node:fs');
  // W11: the Database view's popMenu is an adapter over the shared menu (menu.ts), which carries the keyboard
  const src = readFileSync(new URL('../../piper/web/src/dbview.ts', import.meta.url), 'utf8');
  const pm = src.slice(src.indexOf('function popMenu('), src.indexOf('function switchMenu('));
  assert.match(pm, /openMenu\(/, 'the shared menu');
  const menu = readFileSync(new URL('../../piper/web/src/menu.ts', import.meta.url), 'utf8');
  assert.match(menu, /const was = document\.activeElement/, 'remembers where the focus was');
  assert.match(menu, /\(was\?\.isConnected \? was : anchor\)\?\.focus/, 'a close gives it back');
  assert.match(menu, /\(bs\[focusIdx\] \?\? box\)\.focus/, 'the first item takes the focus');
  assert.match(menu, /e\.key === 'Tab'\) \{ stop\(\); close\(true\)/, 'Tab closes it');
});

test('N10-C-5: the Database view picks up Claude\'s queries while it is open (refresh() was never called: History / Claude queries stayed stale)', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/dbview.ts', import.meta.url), 'utf8');
  assert.match(src, /window\.setInterval\(\(\) => \{ if \(!document\.hidden && el\.isConnected && el\.offsetParent !== null\) void loadHistory\(true\); \}, HIST_MS\)/,
    'polled only while the view is on screen');
  const lh = src.slice(src.indexOf('async function loadHistory('), src.indexOf('// ---------------------------------------------------------------- tabs'));
  assert.match(lh, /if \(quiet && key === histKey\) return;/, 'an unchanged history redraws nothing (the list keeps its scroll, a click its target)');
  assert.match(lh, /scrollTop = sc/, 'a changed one keeps the scroll position');
  assert.match(lh, /if \(quiet && rightEl\.contains\(document\.activeElement\)\) return;/, 'never under the keyboard: a control of the right column keeps its focus (the next tick redraws)');
});

test('N10-C-6: the Database view\'s form dialogs (Add / Edit connection, Import from IDEA, Import CSV) put the focus in their first field', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/dbview.ts', import.meta.url), 'utf8');
  assert.match(src, /function focusForm\(\): void \{\s*setTimeout\(\(\) => document\.querySelector<HTMLElement>\('#overlay \.olayer:last-child \.modal :is\(input, select, textarea\):not\(\[disabled\]\)'\)\?\.focus\(\), 0\);/);
  for (const title of ["c ? `Edit · ${short(c)}` : 'Add connection'", "'Import from IDEA'", '`Import CSV · ${t.table}`']) {
    const at = src.indexOf('deps.modal(' + title);
    assert.ok(at > 0, title);
    const after = src.slice(at, src.indexOf('\n  }\n', at));
    assert.match(after, title === "'Import from IDEA'" ? /\n    pick\.focus\(\);/ : /\n    focusForm\(\);/, title + ': focus first control');
  }
});

test('N10-C-7: Format keeps a multi-word clause on one line (LEFT JOIN became "LEFT\\nJOIN", DELETE FROM "DELETE\\nFROM")', () => {
  assert.equal(formatSql('select c.id from customer c left join orders o on o.customer_id=c.id where c.id<10'),
    'SELECT c.id\nFROM customer c\nLEFT JOIN orders o ON o.customer_id=c.id\nWHERE c.id<10');
  assert.equal(formatSql('delete from t where a = 1'), 'DELETE FROM t\nWHERE a = 1');
  assert.equal(formatSql('insert into t (a) values (1) on duplicate key update a = 2'), 'INSERT INTO t (a)\nVALUES (1)\nON DUPLICATE KEY UPDATE a = 2');
  assert.equal(formatSql('select a from t left outer join u on 1 = 1 inner join v on 2 = 2'), 'SELECT a\nFROM t\nLEFT OUTER JOIN u ON 1 = 1\nINNER JOIN v ON 2 = 2');
  assert.equal(formatSql('select 1 union all select 2'), 'SELECT 1\nUNION ALL\nSELECT 2');
  assert.equal(formatSql("select 'left join x' from t"), "SELECT 'left join x'\nFROM t", 'strings untouched');
});

test('N10-C-8: the connections overview keeps Test / Edit / ⋯ on screen when the table scrolls sideways (1024 + drawer: off to the right, no cue)', async () => {
  const { readFileSync } = await import('node:fs');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(css, /\.db-crow > \.db-cacts \{[^}]*position: sticky; right: 0;[^}]*background: var\(--page\)/);
});


test('overview search combines project and connection metadata, ignores case and whitespace', async () => {
  const { connectionMatches } = await import('../../piper/web/dist/dbview.js');
  const c = { project: 'shop/shop', name: 'shop/shop/test', label: 'Reporting', class: 'test', engine: 'postgres', database: 'orders', user: 'reader' };
  for (const q of ['', '   ', 'SHOP test', ' Reporting  orders ', 'Glass reader', 'postgres']) {
    assert.equal(connectionMatches(c, 'Glass shop', q), true, q);
  }
  for (const q of ['shop prod', 'missing', 'mysql']) assert.equal(connectionMatches(c, 'Glass shop', q), false, q);
});

test('IDEA import searches project names and keys, requiring all query words', () => {
  const a = { title: 'Acme', key: 'shop/shop' };
  const b = { title: 'Acme 2024', key: 'shop/shop2024' };
  const c = { title: '', key: 'other/local' };
  const projects = [a, b, c];
  assert.deepEqual(filterIdeaProjects(projects, '  '), projects);
  assert.deepEqual(filterIdeaProjects(projects, 'ACME'), [a, b]);
  assert.deepEqual(filterIdeaProjects(projects, '  shop 2024 '), [b]);
  assert.deepEqual(filterIdeaProjects(projects, 'other/local'), [c]);
  assert.deepEqual(filterIdeaProjects(projects, 'reader catalog'), []);
  assert.deepEqual(filterIdeaProjects([], 'shop'), []);
  assert.deepEqual(projects, [a, b, c]);
});

// ---- F10: a quiet connection list - actions on hover, the password only when missing
test('F10 needsPassword / reachText: the warning only where a password is missing, Reach as a short text', async () => {
  const { needsPassword, reachText } = await import('../../piper/web/dist/dbview.js');
  assert.equal(needsPassword({ reach: 'stack', has_password: false }), false, 'the stack brings its own');
  assert.equal(needsPassword({ reach: 'ssh', has_password: false }), true);
  assert.equal(needsPassword({ reach: 'tunnel', has_password: true }), false);
  assert.equal(reachText({ reach: 'stack', host: '127.0.0.1:3406' }), 'stack :3406');
  assert.equal(reachText({ reach: 'ssh', tunnel: { state: 'closed' } }), 'ssh · closed');
  assert.equal(reachText({ reach: 'tunnel' }), 'tunnel · closed');
  assert.equal(reachText({ reach: 'tunnel', tunnel: { state: 'open' } }), 'tunnel · open');
  assert.equal(reachText({ reach: 'direct', host: 'h:1' }), 'direct');
  assert.equal(reachText({ reach: 'none' }), 'needs tunnel target');
});

test('F10 wiring: row = ra-row, a click opens, Test · Edit · ⋯ in row-actions, one primary action, Import ▾', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/dbview.ts', import.meta.url), 'utf8');
  const row = src.slice(src.indexOf('function connRow('), src.indexOf('async function ideaImport('));
  assert.match(row, /class: 'db-crow ra-row', role: 'row', onclick: \(e: Event\) => \{ if \(!\(e\.target as HTMLElement\)\.closest\('button, a, input'\)\) openConn\(c\.name\); \}/);
  assert.match(row, /h\('div', \{ class: 'db-cacts row-actions' \}, testBtn, h\('button'[^\n]*'Edit'\), more\)/);
  assert.match(row, /needsPassword\(c\) \? h\('span', \{ class: 'warn db-nopw'[^\n]*'no password'\) : null/);
  assert.doesNotMatch(row, /'set'\)|'needs password'/, 'no Password column');
  assert.match(row, /classDot\(c\.class\)/);
  const list = src.slice(src.indexOf('function drawConnections('), src.indexOf('function connRow('));
  assert.equal((list.match(/class: 'primary'/g) ?? []).length, 2, 'Add connection is the one primary action (in each of the two header states)');
  assert.match(list, /\['Scan stacks', \(\) => void load\(true\)\],\s*\['Import from IDEA…', \(\) => ideaImport\(''\)\]/);
  assert.doesNotMatch(list, /'\+ Add connection'/);
  assert.match(list, /class: 'db-cgroup ra-row' \}, h\('span', \{ class: 'sec-h' \}, g\.title\)/, 'the project as a section heading');
  assert.match(list, /popMenu\(gmore, links\.map/, 'an extension\'s links in the project ⋯');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  const blk = css.slice(css.indexOf('/* ==== 9.67 F10'), css.indexOf('/* ==== end F10 ==== */'));
  assert.match(blk, /\.db-cls\.db-cls-dot \{ background: none;/, 'the class without a filled chip');
  assert.match(blk, /\.db-cls\.db-cls-dot\.db-cls-prod \{ color: var\(--bad\); \}/, 'prod stays red');
  assert.doesNotMatch(blk, /font-size: \d|border-radius: [1-9]/);
  // the console, tree and RO/RW stay: classBadge is still there for them
  assert.match(src, /export function classBadge\(cls: DbClass, text\?: string\): HTMLElement \{\n  return h\('span', \{ class: `db-cls db-cls-\$\{cls\}` \}/);
});
