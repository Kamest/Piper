import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { changeStatements } from '../../piper/web/dist/dbview.js';

const change = (engine, original, type = 'text', key = 1) => changeStatements(engine, null, 'item', ['id', 'name'], ['id'],
  [[key, original]], new Map([[0, new Map([[1, 'Local edit']])]]), new Set(), [], [typeof key === 'string' ? 'varchar' : 'int', type])[0];

test('grid optimistic text guards reject case, accent and trailing-space changes under a lossy collation', () => {
  const cases = ['ANN', 'Ánn', 'Ann ', 'Ann\0', 'Ann', 'x'.repeat(600000) + 'OLD'];
  const statements = cases.map((external, i) => ({external, statement: change('sqlite', i === 5 ? 'x'.repeat(600000) + 'old' : 'Ann')}));
  const probe = spawnSync('python3', ['-c', `
import json, sqlite3, sys, unicodedata
def fold(s): return ''.join(c for c in unicodedata.normalize('NFD', s.casefold()) if not unicodedata.combining(c)).rstrip(' ')
db = sqlite3.connect(':memory:')
db.create_collation('LOSSY', lambda a,b: (fold(a)>fold(b))-(fold(a)<fold(b)))
db.execute('CREATE TABLE item(id INTEGER PRIMARY KEY, name TEXT COLLATE LOSSY)')
out=[]
for case in json.load(sys.stdin):
 db.execute('DELETE FROM item'); db.execute('INSERT INTO item VALUES(1, ?)', [case['external']])
 st=case['statement']; cur=db.execute(st['sql'].replace('%s','?'),st['params'])
 out.append({'rows':cur.rowcount,'value':db.execute('SELECT name FROM item').fetchone()[0]})
print(json.dumps(out))
`], {input: JSON.stringify(statements), encoding:'utf8', maxBuffer:8*1024*1024});
  assert.equal(probe.status, 0, probe.stderr);
  const results = JSON.parse(probe.stdout);
  for (let i=0; i<cases.length; i++) {
    assert.equal(results[i].rows, i === 4 ? 1 : 0, `external value ${i} must match the complete original bytes`);
    assert.equal(results[i].value, i === 4 ? 'Local edit' : cases[i]);
  }
});

test('grid text guards use dialect-specific exact comparisons while retaining indexed key predicates', () => {
  const mysql = change('mysql', 'Ann', 'varchar(80)', 'key');
  assert.match(mysql.sql, /`id` = %s AND CAST\(`id` AS BINARY\) = CAST\(%s AS BINARY\)/);
  assert.match(mysql.sql, /CAST\(`name` AS BINARY\) = CAST\(%s AS BINARY\)/);
  assert.deepEqual(mysql.params, ['Local edit', 'key', 'key', 'Ann']);
  assert.match(change('postgres', 'Ann').sql, /CAST\("name" AS text\) COLLATE "C" = CAST\(%s AS text\) COLLATE "C"/);
  const mssql = change('mssql', 'Ann', 'nvarchar(80)');
  assert.match(mssql.sql, /CONVERT\(varbinary\(max\), CONVERT\(nvarchar\(max\), "name"\)\)/);
  assert.match(mssql.sql, /DATALENGTH/);
  assert.deepEqual(mssql.params, ['Local edit', 1, 'Ann', 6]);
  assert.deepEqual(change('mssql', 'a😀\0', 'nvarchar(max)').params, ['Local edit', 1, 'a😀\0', 8]);
  assert.match(change('oracle', 'Ann', 'VARCHAR2').sql, /DBMS_LOB\.COMPARE\(TO_CLOB\("name"\), TO_CLOB\(%s\)\) = 0/);
  assert.match(change('oracle', 'Ann', 'NCLOB').sql, /DBMS_LOB\.COMPARE\(TO_NCLOB\("name"\), TO_NCLOB\(%s\)\) = 0/);
  for (const engine of ['mysql','postgres','mssql','oracle','sqlite']) {
    for (const type of ['decimal(18,2)', 'timestamp', 'date', 'uuid']) {
      assert.match(change(engine, '123.00', type).sql, /["`]name["`] = %s$/);
    }
  }
});
