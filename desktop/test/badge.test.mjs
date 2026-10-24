import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const {badge}=createRequire(import.meta.url)('../src/lib/badge.cjs');
test('tray bitmap shows a count on platforms without native tray titles',()=>{
  const base=Buffer.alloc(24*24*4);assert.deepEqual(badge(base,0),base);
  const one=badge(base,1);assert.notDeepEqual(one,base);assert.equal(one.length,base.length);assert.equal(one[(6*24+17)*4+3],255);
  assert.notDeepEqual(badge(base,12),one);assert.deepEqual(base,Buffer.alloc(base.length));
  assert.notDeepEqual(badge(base,12),badge(base,13));
});
