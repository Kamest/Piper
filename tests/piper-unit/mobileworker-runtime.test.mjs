import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

async function runtime() {
  const replies = [], pending = [];
  const stream = { async write() {}, async read() { return new Uint8Array([195,169]); }, async finish() {},
    async read_to_end() { return new Uint8Array([1,2]); }, async cancel() {} };
  globalThis.fixtureWorkerNative = { async init() {}, MobileConnection: { async connect() {
    return { open() { return new Promise(resolve => pending.push(() => resolve(stream))); } };
  } } };
  globalThis.postMessage = reply => replies.push(reply);
  const path = new URL('../../piper/web/dist/mobileworker.js', import.meta.url);
  const code = readFileSync(path, 'utf8').replace(/^import .*piper_mobile\.js';/m,
    'const {init,MobileConnection}=globalThis.fixtureWorkerNative;') + '\n//' + Math.random();
  await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
  const message = globalThis.onmessage;
  let serial = 0;
  const send = command => message({ data: { ...command, id: ++serial } });
  const key = new Uint8Array(32).fill(4);
  await send({op:'connect',endpoint:'fixture',relay:'https://relay.example',key});
  assert.deepEqual([...key],Array(32).fill(0),'worker erases its received key after dial');
  return {send,pending,replies};
}

test('worker reserves stream slots before asynchronous opens complete and releases them on terminal cancellation',async()=>{
  const {send,pending,replies}=await runtime();
  const opens=Array.from({length:64},()=>send({op:'open'}));
  await send({op:'open'});
  assert.equal(pending.length,64,'an unresolved open must already consume its slot');
  assert.equal(replies.at(-1).ok,false);
  pending.splice(0).forEach(resolve=>resolve());await Promise.all(opens);
  const ids=replies.filter(x=>typeof x.value==='number').map(x=>x.value);
  assert.equal(new Set(ids).size,64);
  await send({op:'cancel',stream:ids[0]});
  const again=send({op:'open'});assert.equal(pending.length,1);pending.shift()();await again;
  assert.equal(replies.at(-1).ok,true);
});

test('HTTP read_to_end retires registry entries; terminal read preserves its stream until explicit cancellation',async()=>{
  const {send,pending,replies}=await runtime();
  const opening=send({op:'open'});pending.shift()();await opening;const id=replies.at(-1).value;
  await send({op:'read',stream:id,count:2});assert.deepEqual(replies.at(-1).value,new Uint8Array([195,169]));
  await send({op:'finish',stream:id});assert.equal(replies.at(-1).ok,true);
  await send({op:'read_to_end',stream:id});assert.deepEqual(replies.at(-1).value,new Uint8Array([1,2]));
  await send({op:'read',stream:id,count:1});assert.deepEqual(replies.at(-1),{id:6,ok:false});
});
