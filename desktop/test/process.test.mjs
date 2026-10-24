import '../../tests/piper-unit/_sandbox.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { PassThrough } from 'node:stream';
import { EventEmitter } from 'node:events';
const require=createRequire(import.meta.url);
const {runner}=require('../src/lib/process.cjs');
test('split secrets across chunks never escape into streamed logs',async()=>{
  const lines=[];
  const spawn=()=>{
    const child=new EventEmitter();child.stdout=new PassThrough();child.stderr=new PassThrough();child.kill=()=>{};
    queueMicrotask(()=>{child.stdout.write('http://127.0.0.1/#tok');child.stdout.write('en=fake-only-secret\n');child.stdout.end();child.stderr.end();setImmediate(()=>child.emit('close',0));});return child;
  };
  const result=await runner(spawn,{})('fake',[],{onLine:l=>lines.push(l)});
  assert.equal(lines.length,1);assert.ok(!lines[0].includes('fake-only-secret'));assert.ok(result.stdout.includes('fake-only-secret'));
});
test('errors do not echo executable args or captured token output',async()=>{
  const child=new EventEmitter();child.stdout=new PassThrough();child.stderr=new PassThrough();child.kill=()=>{};
  const r=runner(()=>{queueMicrotask(()=>child.emit('error',Error('Bearer fake-only-secret')));return child;},{});
  await assert.rejects(r('fake',['fake-only-secret']),e=>!e.message.includes('fake-only-secret'));
});
