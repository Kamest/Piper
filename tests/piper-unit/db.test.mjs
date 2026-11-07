// node --test: pure helpers of the Database view (dbview.ts), from web/dist
import './_sandbox.mjs';   // only inside the test sandbox
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { guessKube, filterKube, kubeOptions, kubeDefaults, singleFlight, filterIdeaProjects, cellShown, cellText, cellIsTruncated, SQL_DEFAULT, SQL_NOW_DATE, SQL_NOW_TIME, SQL_NOW_TS, changeStatements, isTemporal, sqlNow, coerceEdit, completions, fuzzyMatch, formatSql, historySql, mentionedTables, quoteIdent, showStatement, sqlLiteral, statementAt, toCsv, toInserts } from '../../piper/web/dist/dbview.js';

test('database dashboards remain visible and searchable without SQL connections', async () => {
  const { hasDatabaseEntries, databaseLinkMatches } = await import('../../piper/web/dist/dbview.js');
  const group = { project: 'acme/shop', title: 'Shop', connections: [], links: [{ label: 'Document dashboard', url: 'https://db.example.com/' }] };
  assert.equal(hasDatabaseEntries([]), false);
  assert.equal(hasDatabaseEntries([{ ...group, links: [] }]), false);
  assert.equal(hasDatabaseEntries([group]), true);
  assert.equal(hasDatabaseEntries([{ ...group, links: [], connections: [{ name: 'shop/dev' }] }]), true);
  for (const query of ['', 'shop', 'acme document', 'DASHBOARD']) assert.equal(databaseLinkMatches(group, query), true);
  assert.equal(databaseLinkMatches(group, 'missing'), false);
  assert.equal(databaseLinkMatches({ ...group, links: [] }, 'shop'), false);
});

test('connection ports reject invalid input instead of serializing NaN as the default', async () => {
  const { connectionPort } = await import('../../piper/web/dist/dbview.js');
  assert.equal(connectionPort(''), null);
  assert.equal(connectionPort(' 5432 '), 5432);
  for (const value of ['abc', '0', '65536', '-1', '1.5', '1e3', 'Infinity']) {
    assert.throws(() => connectionPort(value), /Invalid port/);
  }
});

test('connection save distinguishes Add from an editor version, even without renaming', async () => {
  const { connectionGuard } = await import('../../piper/web/dist/dbview.js');
  const def = { name: 'app/test/main', cls: 'test', engine: 'mysql' };
  assert.deepEqual(connectionGuard(def), { ...def, create: true });
  const existing = { name: def.name, def: { ...def, revision: 'opened-version' } };
  assert.deepEqual(connectionGuard(def, existing), { ...def, old_name: def.name, expected_revision: 'opened-version' });
  assert.equal(connectionGuard({ ...def, name: 'app/test/renamed' }, existing).old_name, def.name);
});

test('same-name IDEA sources get separate default connection names', async () => {
  const { ideaConnectionName } = await import('../../piper/web/dist/dbview.js');
  const source = { name: 'TEST', database: 'shop', cls_guess: 'test', engine: 'postgres' };
  assert.equal(ideaConnectionName('app', source, []), 'app/test/shop');
  assert.equal(ideaConnectionName('app', source, ['app/test/shop', 'app/test/shop-2']), 'app/test/shop-3');
});

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
    { sql: 'UPDATE `shop`.`item` SET `name` = %s, `price` = %s WHERE `id` = %s AND CAST(`name` AS BINARY) = CAST(%s AS BINARY) AND `price` = %s', params: ['A', 11, 1, 'a', 10], expect_rowcount: 1 },
    { sql: 'INSERT INTO `shop`.`item` (`name`, `price`) VALUES (%s, %s)', params: ['new', 5] },
    { sql: 'INSERT INTO `shop`.`item` () VALUES ()', params: [] },
  ]);
  const pg = changeStatements('postgres', null, 't', ['a', 'b'], ['a', 'b'], [[null, 2]], new Map([[0, new Map([[1, 3]])]]), new Set(), [new Map()]);
  assert.deepEqual(pg, [{ sql: 'UPDATE "t" SET "b" = %s WHERE "a" IS NULL AND "b" = %s', params: [3, 2], expect_rowcount: 1 },
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
    { sql: 'UPDATE `t` SET `a` = DEFAULT, `b` = %s WHERE `id` = %s AND CAST(`a` AS BINARY) = CAST(%s AS BINARY) AND CAST(`b` AS BINARY) = CAST(%s AS BINARY)', params: [null, 1, 'x', 'y'], expect_rowcount: 1 },
    { sql: 'INSERT INTO `t` (`id`, `a`) VALUES (%s, DEFAULT)', params: [5] },
  ]);
  assert.equal(showStatement(out[0]), "UPDATE `t` SET `a` = DEFAULT, `b` = NULL WHERE `id` = 1 AND CAST(`a` AS BINARY) = CAST('x' AS BINARY) AND CAST(`b` AS BINARY) = CAST('y' AS BINARY)");
});

test('Set now: the engine keyword in UPDATE and INSERT, no parameter', () => {
  const out = changeStatements('postgres', null, 't', ['id', 'at', 'd'], ['id'], [[1, 'x', 'y']],
    new Map([[0, new Map([[1, SQL_NOW_TS], [2, 'z']])]]), new Set(), [new Map([[0, 5], [2, SQL_NOW_DATE]])], ['integer', 'timestamp', 'date']);
  assert.deepEqual(out, [
    { sql: 'UPDATE "t" SET "at" = CURRENT_TIMESTAMP, "d" = %s WHERE "id" = %s AND "at" = %s AND "d" = %s', params: ['z', 1, 'x', 'y'], expect_rowcount: 1 },
    { sql: 'INSERT INTO "t" ("id", "d") VALUES (%s, CURRENT_DATE)', params: [5] },
  ]);
  assert.equal(showStatement(out[0], 'postgres'), 'UPDATE "t" SET "at" = CURRENT_TIMESTAMP, "d" = \'z\' WHERE "id" = 1 AND "at" = \'x\' AND "d" = \'y\'');
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

test('grid edits guard the original edited cells, including NULL and PostgreSQL JSON', () => {
  const [statement] = changeStatements('postgres', null, 'item', ['id', 'note', 'other'], ['id'], [[1, null, 'unrelated']],
    new Map([[0, new Map([[1, 'new']])]]), new Set(), []);
  assert.equal(statement.sql, 'UPDATE "item" SET "note" = %s WHERE "id" = %s AND "note" IS NULL');
  assert.deepEqual(statement.params, ['new', 1]);
  assert.equal(statement.expect_rowcount, 1);
  const [json] = changeStatements('postgres', null, 'item', ['id', 'data'], ['id'], [[1, '{"v": 1}']],
    new Map([[0, new Map([[1, '{"v": 2}']])]]), new Set(), [], ['integer', 'json']);
  assert.match(json.sql, /CAST\("data" AS jsonb\) = CAST\(%s AS jsonb\)/);
});

test('an incomplete original cell cannot be submitted after editing or used as a key', () => {
  const cut = 'prefix…⟪truncated - full value: db_cell⟫';
  assert.throws(() => changeStatements('postgres', null, 'item', ['id', 'note'], ['id'], [[1, cut]],
    new Map([[0, new Map([[1, 'replacement']])]]), new Set(), [], [], [[0, 'note']]), /incomplete/);
  assert.throws(() => changeStatements('postgres', null, 'item', ['id'], ['id'], [[cut]],
    new Map(), new Set([0]), [], [], [[0, 'id']]), /incomplete/);
  assert.equal(cellIsTruncated({ offset: 500, truncated_cells: [[501, 'note']] }, 1, 'note'), true);
  assert.equal(cellIsTruncated({ offset: 500, truncated_cells: [[501, 'note']] }, 0, 'note'), false);
  assert.equal(cellIsTruncated({ offset: 500, truncated_cells: [[501, 'note']] }, -1, 'note'), false);
  assert.equal(cellIsTruncated({ offset: 0 }, 0, 'note'), false);
});

test('binary edits and INSERT exports preserve bytes, while ordinary 0x text stays text', () => {
  const value = coerceEdit('bytea', '0x1020FF');
  assert.deepEqual(value, { sqlBinary: '1020ff' });
  assert.equal(cellText(value), '0x1020ff');
  assert.deepEqual(coerceEdit('varbinary(20)', '\\x0001ff'), { sqlBinary: '0001ff' });
  assert.throws(() => coerceEdit('bytea', '0x123'), /hexadecimal/);
  assert.throws(() => coerceEdit('bytea', '0x12zz'), /hexadecimal/);
  assert.throws(() => coerceEdit('bytea', "0x00'); DELETE FROM t;--"), /hexadecimal/);
  for (const [engine, expression] of [['postgres', "decode(%s, 'hex')"], ['mysql', 'UNHEX(%s)'],
    ['oracle', 'HEXTORAW(%s)'], ['mssql', 'CONVERT(varbinary(max), %s, 2)']]) {
    const [statement] = changeStatements(engine, null, 'item', ['id', 'payload'], ['id'], [[1, '0x0001ff']],
      new Map([[0, new Map([[1, value]])]]), new Set(), [], ['int', 'bytea']);
    assert.ok(statement.sql.includes(`SET ${quoteIdent('payload', engine)} = ${expression}`));
    assert.ok(statement.sql.includes(`AND ${quoteIdent('payload', engine)} = ${expression}`));
    assert.deepEqual(statement.params, ['1020ff', 1, '0001ff']);
  }
  assert.equal(toInserts('postgres', null, 'item', ['payload', 'text'], [['0x0001ff', '0x0001ff']], ['bytea', 'text']),
    'INSERT INTO "item" ("payload", "text") VALUES (decode(\'0001ff\', \'hex\'), \'0x0001ff\');\n');
  assert.equal(sqlLiteral(value, 'mysql'), "X'1020ff'");
  assert.equal(sqlLiteral(value, 'mssql'), '0x1020ff');
  assert.equal(sqlLiteral(value, 'oracle'), "HEXTORAW('1020ff')");
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
  assert.match(src, /function focusForm\(\): void \{\s*setTimeout\(\(\) => document\.querySelector<HTMLElement>\('#overlay \.olayer:last-child \.modal :is\(input, select, textarea, \.db-project-trigger\):not\(\[disabled\]\):not\(\.hidden \*\)'\)\?\.focus\(\), 0\);/);
  for (const title of ["c ? `Edit · ${short(c)}` : 'Add connection'", "'Import from IDEA'", '`Import CSV · ${t.table}`']) {
    const at = src.indexOf('deps.modal(' + title);
    assert.ok(at > 0, title);
    const after = src.slice(at, src.indexOf('\n  }\n', at));
    assert.match(after, title === "'Import from IDEA'" ? /\n    pick\.focus\(\);/ : /\n    focusForm\(\);/, title + ': focus first control');
  }
});

test('9.161: Add connection - Project and Kubeconfig are typeaheads, not plain selects', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/dbview.ts', import.meta.url), 'utf8');
  const ed = src.slice(src.indexOf('function editConn('), src.indexOf('\n  }\n', src.indexOf('deps.modal(c ? `Edit')));
  assert.doesNotMatch(ed, /h\('select', \{ name: 'project'/, 'the project <select> is gone');
  assert.match(ed, /typeahead\(\{\s*id: 'db-edit-project', name: 'project'[^]*?disabled: !!c,/, 'Project: a typeahead, fixed while editing');
  assert.match(ed, /options: \(q\) => filterIdeaProjects\(projects, q\)/, 'the same matching as Import from IDEA');
  assert.match(ed, /typeahead\(\{\s*id: 'db-kube', name: 'kubeconfig'/, 'Kubeconfig: the same picker');
  assert.match(ed, /h\('label', \{\}, 'Project'\), projPick\.el\)/);
  const ta = src.slice(src.indexOf('function typeahead('), src.indexOf('function ic('));
  for (const k of ["'ArrowDown'", "'Enter'", "'Escape'", 'e.stopPropagation()', "'focusout'", 'aria-activedescendant'])
    assert.ok(ta.includes(k), 'typeahead keyboard / a11y: ' + k);
  assert.match(ta, /if \(!x\.disabled\) choose\(x\)/, 'a disabled option (the other class\'s kubeconfig) is not picked');
  const css = readFileSync(new URL('../../piper/web/app.css', import.meta.url), 'utf8');
  assert.match(css, /\n\.db-form-row\.db-form-top \{ align-items: start; \}/, 'the label stays at the top while the list is open (beats .db-form-row\'s later align-items: center)');
});

test('9.163: Import from IDEA (a localhost port) - the new connection gets K8S over the likely kubeconfig', async () => {
  const k = (file, group, cluster, kind, env) => ({ file, group, cluster, kind, env, namespace: '' });
  const all = [k('wid/wid-widgets2024-test-app.ctx.yaml', 'wid', 'wid-widgets2024-test', 'app', 'test'),
    k('wid/wid-widgets2024-test-mysql.ctx.yaml', 'wid', 'wid-widgets2024-test', 'mysql', 'test'),
    k('wid/wid-widgets2024-prod-mysql.ctx.yaml', 'wid', 'wid-widgets2024-prod', 'mysql', 'prod'),
    k('hooli/hooli-shop-test-mysql.ctx.yaml', 'hooli', 'hooli-shop-test', 'mysql', 'test'),
    k('hooli/hooli-shop-test-app.ctx.yaml', 'hooli', 'hooli-shop-test', 'app', 'test'),
    k('globex/globex-store-test-mysql.ctx.yaml', 'globex', 'globex-store-test', 'mysql', 'test'),
    k('initech/initech-store-test-mysql.ctx.yaml', 'initech', 'initech-store-test', 'mysql', 'test')];
  const g = (cls, ...h) => guessKube(all, cls, h)?.file ?? null;
  assert.equal(g('test', 'widgets2024', 'widgets2024/test/main', '', 'TEST - tunnel'), 'wid/wid-widgets2024-test-mysql.ctx.yaml', 'the project name, the database file wins');
  assert.equal(g('test', 'widgets2024', 'widgets2024/test/widgets_store', 'widgets_store', 'TEST - tunnel'), 'wid/wid-widgets2024-test-mysql.ctx.yaml',
    'store is in many clusters (globex-store …) - the word naming one cluster decides, not a tie');
  assert.equal(guessKube(all.slice(0, 2).concat(all[5]), 'test', ['widgets2024', 'widgets2024/test/widgets_store', 'widgets_store'])?.file,
    'wid/wid-widgets2024-test-mysql.ctx.yaml', 'one other store cluster: the project name still outweighs the database word');
  assert.equal(g('test', 'store', 'store/test/main'), null, 'a word of several clusters alone: a tie, none');
  assert.equal(g('prod', 'widgets2024', 'widgets2024/prod/wid_widgets2024_globex', 'wid_widgets2024_globex'), 'wid/wid-widgets2024-prod-mysql.ctx.yaml', 'the class decides');
  assert.equal(g('test', 'portal', 'portal/test/hooli_shop_globex', 'hooli_shop_globex', 'hooli_shop_globex'), 'hooli/hooli-shop-test-mysql.ctx.yaml', 'the database name');
  assert.equal(g('test', 'portal', 'portal/test/main', '', 'TEST'), null, 'no shared word: the user picks');
  assert.equal(g('test', 'p', 'test/main', 'mysql', 'localhost test'), null, 'generic words do not count');
  assert.equal(guessKube([all[0], k('x/x-widgets2024-test-app.ctx.yaml', 'x', 'x-widgets2024-test', 'app', 'test')], 'test', ['widgets2024']), null, 'a tie: none');
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../piper/web/src/dbview.ts', import.meta.url), 'utf8');
  const gr = src.slice(src.indexOf('const guessReach = () => {'), src.indexOf('\n    };', src.indexOf('const guessReach = () => {')));
  assert.match(gr, /if \(c \|\| d\.tunnel \|\| d\.ssh \|\| d\.host \|\| kubeFile \|\| !kubeLoaded \|\| !proj \|\| cls\.value === 'dev' \|\| \(via\.value && via\.value !== 'k8s'\)\) return;/,
    'only a new test / prod connection without a reach the user or IDEA (ssh) chose');
  assert.match(gr, /via\.value = 'k8s';\s*sync\(\);/);
  for (const at of ["kubeLoaded = true; kube.redraw(); guessReach();", "sync();\n      guessReach();\n    };"]) assert.ok(src.includes(at), 'guessed after both loads: ' + at);
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
  assert.equal((list.match(/class: 'primary'[^\n]*'Add connection'/g) ?? []).length, 2, 'Add connection is the one primary header action (in each of the two header states; the zero-project card offers registration)');
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

test('singleFlight: opening the Database view sends one /api/db/all, not two that slow each other', async () => {
  const once = singleFlight();
  let runs = 0;
  const releases = [];
  const run = () => { runs++; return new Promise((r) => { releases.push(r); }); };
  const a = once('', false, run), b = once('', false, run);
  assert.equal(runs, 1);
  once('p_x/dbo', false, run);   // another scope is another list
  assert.equal(runs, 2);
  once('p_x/dbo', true, run);    // a forced reload (after Save) always asks again
  assert.equal(runs, 3);
  releases.forEach((r) => r()); await Promise.all([a, b]);
  await once('', false, async () => { runs++; });   // finished: the next one runs again
  assert.equal(runs, 4);
});

test('the Kubeconfig select: the class first, -mysql before others, the other class disabled', () => {
  const t = (cluster, kind, env) => ({ file: `c/${cluster}-${kind}.ctx.yaml`, group: 'c', cluster, kind, env, namespace: '' });
  const out = kubeOptions([t('zz-prod', 'mysql', 'prod'), t('b-test', 'app', 'test'), t('hooli-shop-test', 'mysql', 'test'), t('a-test', 'mysql', 'test')], 'test');
  assert.deepEqual(out.map((x) => `${x.cluster}-${x.kind}:${x.disabled}`),
    ['a-test-mysql:false', 'hooli-shop-test-mysql:false', 'b-test-app:false', 'zz-prod-mysql:true']);
  assert.deepEqual(kubeOptions([t('zz-prod', 'mysql', 'prod'), t('a-test', 'mysql', 'test')], 'prod').map((x) => x.disabled), [false, true]);
  assert.deepEqual(kubeDefaults({ kind: 'mysql' }), { resource: 'service/mysql', port: '3306' });
  assert.deepEqual(kubeDefaults({ kind: 'app' }), { resource: '', port: '' });
});

test('the Kubeconfig typeahead: every word in cluster / group / namespace / file, kubeOptions order kept', () => {
  const t = (group, cluster, kind, env) => ({ file: `${group}/${cluster}-${kind}.ctx.yaml`, group, cluster, kind, env, namespace: `${cluster}-${kind}` });
  const all = [t('umbrella', 'umbrella-store-test', 'mysql', 'test'), t('hooli', 'hooli-shop-test', 'app', 'test'), t('hooli', 'hooli-shop-prod', 'mysql', 'prod'),
    t('hooli', 'hooli-shop-test', 'mysql', 'test')];
  assert.deepEqual(filterKube(all, 'test', 'hooli').map((x) => `${x.cluster}-${x.kind}:${x.disabled}`),
    ['hooli-shop-test-mysql:false', 'hooli-shop-test-app:false', 'hooli-shop-prod-mysql:true']);
  assert.deepEqual(filterKube(all, 'test', 'HOOLI mysql').map((x) => x.cluster), ['hooli-shop-test', 'hooli-shop-prod']);
  assert.equal(filterKube(all, 'test', '').length, 4);
  assert.deepEqual(filterKube(all, 'test', 'nothing-like-this'), []);
});

test('9.174 localForwardWarn: test / prod straight to localhost warns, dev / remote hosts / tunnels do not', async () => {
  const { localForwardWarn, isLocalHost } = await import('../../piper/web/dist/dbview.js');
  for (const h of ['localhost', 'LOCALHOST.', '127.0.0.1', '127.1', '0.0.0.0', '::1', 'db.localhost', ' 127.0.0.2 ']) assert.ok(isLocalHost(h), h);
  for (const h of ['db.example.test', 'db127.example', '10.0.0.5', '1270.0.0.1', '']) assert.ok(!isLocalHost(h), h);
  assert.match(localForwardWarn('test', 'direct', '127.0.0.1'), /^test on localhost = your own port-forward/);
  assert.match(localForwardWarn('prod', 'direct', 'localhost'), /pinned at first use/);
  assert.equal(localForwardWarn('dev', 'direct', '127.0.0.1'), '');
  assert.equal(localForwardWarn('test', 'ssh', '127.0.0.1'), '');   // behind ssh localhost is the server itself
  assert.equal(localForwardWarn('test', 'direct', 'db.example.test'), '');
});


test('PostgreSQL caret and selected scripts preserve dollar quotes and nested comments', async () => {
  const { sqlStatements } = await import('../../piper/web/dist/dbview.js');
  const sql = 'SELECT $$first;value$$ AS one; SELECT $tag$second;value$tag$ AS two;';
  assert.equal(statementAt(sql, sql.length, 'postgres').sql, 'SELECT $tag$second;value$tag$ AS two');
  assert.deepEqual(sqlStatements(sql, 'postgres'), ['SELECT $$first;value$$ AS one', 'SELECT $tag$second;value$tag$ AS two']);
  assert.deepEqual(sqlStatements('/* outer /* inner ; */ ; */ SELECT 1; SELECT 2', 'postgres'), ['/* outer /* inner ; */ ; */ SELECT 1', 'SELECT 2']);
  assert.deepEqual(sqlStatements("SELECT 'it''s;valid'; SELECT 2", 'postgres'), ["SELECT 'it''s;valid'", 'SELECT 2']);
  assert.deepEqual(sqlStatements('SELECT [a;b] FROM t; SELECT 2', 'mssql'), ['SELECT [a;b] FROM t', 'SELECT 2']);
});

test('selected SQL omits comment-only entries and each remaining entry passes the real backend classifier', async () => {
  const { sqlStatements } = await import('../../piper/web/dist/dbview.js');
  const { spawnSync } = await import('node:child_process');
  const { fileURLToPath } = await import('node:url');
  for (const engine of ['mysql', 'postgres', 'mssql', 'oracle']) {
    const entries = sqlStatements('-- introduction\n; SELECT 1; /* between */; SELECT 2; -- trailing explanation', engine);
    assert.deepEqual(entries, ['SELECT 1', 'SELECT 2'], engine);
    const result = spawnSync('python3', ['-c',
      'import sys,json;sys.path.insert(0,sys.argv[1]);import dbsql;print(json.dumps([dbsql.kind(s,sys.argv[2]) for s in json.loads(sys.argv[3])]))',
      fileURLToPath(new URL('../../piper', import.meta.url)), engine, JSON.stringify(entries)], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), ['select', 'select']);
    assert.deepEqual(sqlStatements('/* ordinary comment */ -- another\n', engine), []);
    assert.deepEqual(sqlStatements("SELECT '-- text; /* not a comment */'; /* tail */", engine), ["SELECT '-- text; /* not a comment */'"]);
    assert.deepEqual(sqlStatements('/* keep with query */ SELECT 1; -- keep too\nSELECT 2', engine), ['/* keep with query */ SELECT 1', '-- keep too\nSELECT 2']);
  }
  assert.deepEqual(sqlStatements('SELECT $$-- ; /* literal */$$; /* outer /* inner */ ; */', 'postgres'), ['SELECT $$-- ; /* literal */$$']);
  assert.deepEqual(sqlStatements('SELECT 1; # trailing comment', 'mysql'), ['SELECT 1']);
  assert.deepEqual(sqlStatements('/*!80000 SELECT 1 */; /*+ hint */; SELECT 2', 'mysql'), ['/*!80000 SELECT 1 */', '/*+ hint */', 'SELECT 2'], 'executable comments reach backend validation');
  assert.deepEqual(sqlStatements('SELECT 1; /* unterminated', 'postgres'), ['SELECT 1', '/* unterminated'], 'invalid syntax reaches backend validation');
  assert.deepEqual(sqlStatements('SELECT 1--2; SELECT 3', 'mysql'), ['SELECT 1--2', 'SELECT 3'], 'MySQL -- needs following whitespace');
});

test('composite FK uses every source column and refuses missing or null parts', async () => {
  const { foreignKeyFilter } = await import('../../piper/web/dist/dbview.js');
  const a = { name: 'parent_fk', column: 'a', ref_schema: 'other', ref_table: 'parent', ref_column: 'parent_a' };
  const b = { ...a, column: 'b', ref_column: 'parent_b' };
  const unrelated = { ...a, name: 'other_fk', column: 'c' };
  const cols = ['a', 'b', 'c'];
  assert.equal(foreignKeyFilter(a, [a, b, unrelated], cols, [1, 2, 9], 'postgres'), '"parent_a" = 1 AND "parent_b" = 2');
  assert.equal(foreignKeyFilter(a, [a, b], cols, [1, null], 'postgres'), null);
  assert.equal(foreignKeyFilter(a, [a, b], ['a'], [1], 'postgres'), null);
  assert.equal(foreignKeyFilter(a, [a, b], cols, [1, SQL_DEFAULT], 'postgres'), null);
});

test('completion inserts quoted identifiers while respecting a partial quoted token', async () => {
  const { acceptCompletion } = await import('../../piper/web/dist/dbview.js');
  for (const [engine, expected] of [['postgres', '"Order Items"'], ['mysql', '`Order Items`'], ['mssql', '"Order Items"'], ['oracle', '"Order Items"']]) {
    assert.deepEqual(acceptCompletion('SELECT * FROM audit.Ord', 23, 'Order Items', true, engine), { text: 'SELECT * FROM audit.' + expected, caret: 20 + expected.length });
  }
  assert.deepEqual(acceptCompletion('SELECT * FROM "Ord"', 18, 'Order Items', true, 'postgres'), { text: 'SELECT * FROM "Order Items"', caret: 27 });
  assert.equal(acceptCompletion('sel', 3, 'SELECT', false, 'postgres').text, 'SELECT');
  assert.equal(acceptCompletion('SELECT \"id\" FROM Ord', 20, 'Order Items', true, 'postgres').text, 'SELECT \"id\" FROM \"Order Items\"');
  assert.equal(acceptCompletion('SELECT * FROM \"Order I\" WHERE id=1', 22, 'Order Items', true, 'postgres').text, 'SELECT * FROM \"Order Items\" WHERE id=1');
  assert.equal(acceptCompletion('Ord', 3, 'Order', true, 'postgres').text, '"Order"');
});


test('Submit retries an uncertain acknowledgement with its original identity and preserves correctable refusals', async () => {
  const { readFileSync } = await import('node:fs');
  const source = readFileSync(new URL('../../piper/web/dist/dbview.js', import.meta.url), 'utf8');
  const code = source.slice(source.indexOf('async function submit(t)'), source.indexOf('function revert(t)'));
  const calls = [], toasts = [];
  let fail = true, rejected = false, sequence = 0;
  const state = { statements: [{ sql: 'INSERT INTO item(label) VALUES(?)', params: ['ack'] }] };
  const pendingSaves = new Map();
  const submit = new Function('state', 'pendingSaves', 'execute', 'toast', 'operationId', `
    const curConn=()=>({class:'dev'}),pendingCount=()=>1,pendingStatements=()=>state.statements;
    const bodyEl={contains:()=>false},document={activeElement:null},saving=new Set(),session='',pj=()=> 'acme/shop';
    const deps={toast},dbApi={execute},rowsText=String,errText=String,loadTable=async()=>{},draw=()=>{},focusGrid=()=>{};
    ${code};return submit;
  `)(state, pendingSaves, async body => {
    calls.push(body);
    if (fail) { fail = false; throw new TypeError('lost successful response'); }
    return { data: rejected ? { outcome: 'rejected', error: 'row changed' } : { committed: true, rowcount: [1] } };
  }, message => toasts.push(message), () => 'op-' + ++sequence);
  const row = () => ({ id: 'table', conn: 'db', schema: 'main', edits: new Map(), deleted: new Set(), inserted: [new Map()], sel: null });
  await submit(row());
  assert.match(toasts.at(-1), /Save outcome unknown/);
  assert.equal(pendingSaves.size, 1);
  state.statements = [{ sql: 'INSERT INTO item(label) VALUES(?)', params: ['changed'] }];
  await submit(row());
  assert.equal(calls.length, 1, 'changing an unknown pending write cannot silently create a new operation');
  state.statements[0].params = ['ack'];
  await submit(row());
  assert.equal(calls[0].operation_id, calls[1].operation_id);
  assert.equal(calls[1].retry, true);
  assert.equal(pendingSaves.size, 0);
  await submit(row());
  assert.notEqual(calls[2].operation_id, calls[0].operation_id);
  rejected = true;
  const rejectedRow = row();
  await submit(rejectedRow);
  assert.match(toasts.at(-1), /Not saved: row changed/);
  assert.equal(pendingSaves.size, 0, 'confirmed refusal releases the attempt for corrected input');
  assert.equal(rejectedRow.inserted.length, 1, 'refusal preserves the draft');
  state.statements[0].params = ['corrected'];
  rejected = false;
  await submit(rejectedRow);
  assert.notEqual(calls.at(-1).operation_id, calls.at(-2).operation_id);
  assert.equal(rejectedRow.inserted.length, 0);
});

test('Commit retries reuse a receipt id, new transactions get new ids, and missing outcomes never claim success', async () => {
  const { readFileSync } = await import('node:fs');
  const source = readFileSync(new URL('../../piper/web/dist/dbview.js', import.meta.url), 'utf8');
  const start = source.indexOf('async function txEnd(');
  const code = source.slice(start, source.indexOf('/** the console', start));
  const calls = [], toasts = [], pendingEnds = new Map();
  let fail = true, sequence = 0, committed = true;
  const end = new Function('pendingEnds', 'execute', 'toast', 'operationId', `
    const joined=t=>[t],curConn=()=>({class:'dev'}),document={activeElement:null},bodyEl={querySelector:()=>undefined};
    const txId=()=> 'same-console',session='',pj=()=> 'acme/shop',syncTx=()=>{},forgetTx=()=>{},described=new Map(),loadObjects=()=>{},loadHistory=()=>{};
    const draw=()=>{},refocusEditor=()=>{},deps={toast},dbApi={tx:execute},errText=String;
    ${code};return txEnd;
  `)(pendingEnds, async body => {
    calls.push(body);
    if (fail) { fail = false; throw new TypeError('lost commit response'); }
    return { data: { committed, tx_open: false } };
  }, text => toasts.push(text), () => 'end-' + ++sequence);
  const tab = { conn: 'db', tx: { open: true }, results: [] };
  await end(tab, 'commit');
  assert.match(toasts.at(-1), /Transaction outcome unknown/);
  await end(tab, 'rollback');
  assert.equal(calls.length, 1, 'unknown commit cannot be replaced by rollback');
  await end(tab, 'commit');
  assert.equal(calls[0].operation_id, calls[1].operation_id);
  assert.equal(calls[1].retry, true);
  tab.tx = { open: true };
  await end(tab, 'commit');
  assert.notEqual(calls[2].operation_id, calls[0].operation_id);
  assert.equal(calls[2].tx, calls[0].tx);
  committed = false;
  tab.tx = { open: true };
  await end(tab, 'commit');
  assert.equal(tab.results.at(-1).title, 'Outcome unknown');
  assert.equal(tab.results.at(-1).note, 'Transaction outcome unknown');
  assert.doesNotMatch(tab.results.at(-1).error, /Run the statements again/);
});

test('CSV Cancel rolls back a late successful batch and never sends Commit', async () => {
  const { readFileSync } = await import('node:fs');
  const { parseCsv, csvInserts } = await import('../../piper/web/dist/dbview.js');
  const source = readFileSync(new URL('../../piper/web/dist/dbview.js', import.meta.url), 'utf8');
  const code = source.slice(source.indexOf('function importCsv(t, c)'), source.indexOf('function drawConsole(t)'));
  assert.ok(code.length > 100, 'extract actual CSV flow');
  const nodes = [], requests = [], messages = [];
  const h = (tag, props = {}, ...children) => {
    const node = { tag, ...props, children, handlers: {}, append(...items) { this.children.push(...items); },
      addEventListener(name, handler) { this.handlers[name] = handler; } };
    nodes.push(node); return node;
  };
  let resolveExec, closed = 0;
  const dbApi = { tx: async (b) => {
    requests.push(b);
    if (b.op === 'exec') return new Promise((resolve) => { resolveExec = resolve; });
    return { data: { tx_open: false } };
  }, cancel: async (...args) => { requests.push({ op: 'cancel', args }); return { data: { cancelled: 0 } }; } };
  const deps = { modal() {}, closeModal() { closed++; }, toast(text) { messages.push(text); } };
  const create = new Function('h', 'dbApi', 'deps', 'parseCsv', 'csvInserts', `
    const operationId=()=> '12345678-1234-1234-1234-123456789abc', viewTx='view-',session='';
    const pj=()=> 'acme/shop', rowsText=n=>n+' rows', clear=n=>{n.children=[]},focusForm=()=>{},markBidi=s=>s,errText=String,loadTable=()=>{};
    ${code};return importCsv;`)(h, dbApi, deps, parseCsv, csvInserts);
  create({ id: 't1', conn: 'db', table: 'item', schema: 'main', cols: [{ name: 'id' }] }, { engine: 'postgres', class: 'dev' });
  const file = nodes.find((n) => n.tag === 'input' && n.type === 'file');
  file.files = [{ size: 8, text: async () => 'id\n55000\n' }];
  await file.handlers.change();
  const go = nodes.find((n) => n.tag === 'button' && n.children[0] === 'Import');
  const cancel = nodes.find((n) => n.tag === 'button' && n.children[0] === 'Cancel');
  go.handlers.click();
  assert.equal(requests[0].op, 'exec');
  assert.equal(requests[0].request_tag, requests[0].tx);
  assert.equal(file.disabled, true);
  go.handlers.click();
  assert.equal(requests.length, 1, 'a second click cannot start another import');
  cancel.onclick();
  assert.equal(closed, 0, 'dialog stays until rollback confirmation');
  assert.equal(requests[1].op, 'cancel');
  assert.equal(requests[1].args[3], requests[0].request_tag);
  // Cancel may arrive after the server finished its batch: cancelled:0 is still a rollback decision.
  resolveExec({ data: { tx_open: true } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(requests.map((r) => r.op), ['exec', 'cancel', 'rollback']);
  assert.equal(closed, 1);
  assert.equal(messages.at(-1), 'Import cancelled; nothing was written.');
});
