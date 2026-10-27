import './_sandbox.mjs';
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createMobileConnection} from '../../piper/web/dist/mobileworkerclient.js';

class FakeWorker {
  static instances=[];
  sent=[]; terminated=0;
  constructor(url,options){this.url=url;this.options=options;FakeWorker.instances.push(this)}
  postMessage(data,transfer){this.sent.push(structuredClone(data,{transfer}))}
  terminate(){this.terminated++}
  reply(request,value){this.onmessage({data:{id:request.id,ok:true,...(value===undefined?{}:{value})}})}
}
globalThis.Worker=FakeWorker;
const key=()=>new Uint8Array(32).fill(7);
async function connected(){const controller=new AbortController();const ready=createMobileConnection('11'.repeat(32),'https://relay.example',key(),controller.signal);const worker=FakeWorker.instances.at(-1);worker.reply(worker.sent[0]);return {connection:await ready,worker,controller}}

test('aborting an unresolved dial terminates its entire worker and settles immediately',async()=>{
  const controller=new AbortController(),bytes=key();
  const ready=createMobileConnection('11'.repeat(32),'https://relay.example',bytes,controller.signal);
  const worker=FakeWorker.instances.at(-1);
  assert.equal(bytes.byteLength,32,'caller key must not be detached');
  assert.deepEqual([...bytes],[...key()]);
  controller.abort(new Error('superseded'));
  await assert.rejects(ready,/superseded/);
  assert.equal(worker.terminated,1);
  worker.reply(worker.sent[0]); // late queued reply cannot resurrect the terminated runtime
  assert.equal(worker.terminated,1);
});

test('closing a connection terminates all pending stream reads and writes exactly once',async()=>{
  const {connection,worker}=await connected();
  const opening=connection.open();worker.reply(worker.sent.at(-1),1);const stream=await opening;
  const read=stream.read(5),write=stream.write(new Uint8Array([1,2]));
  const rejected=Promise.all([assert.rejects(read,/closed/),assert.rejects(write,/closed/)]);
  await connection.close();await rejected;await connection.close();
  assert.equal(worker.terminated,1);
  await assert.rejects(connection.open(),/ended/);
});

test('stream RPC preserves binary bytes, ordering and independent stream identities',async()=>{
  const {connection,worker}=await connected();
  const opening=connection.open();worker.reply(worker.sent.at(-1),42);const stream=await opening;
  const bytes=new Uint8Array([0,255,195,169]);const writing=stream.write(bytes);const request=worker.sent.at(-1);
  assert.equal(request.stream,42);assert.deepEqual(request.data,bytes);assert.equal(bytes.byteLength,4);
  worker.reply(request);await writing;
  const reading=stream.read(4);worker.reply(worker.sent.at(-1),new Uint8Array(bytes));assert.deepEqual(await reading,bytes);
  await connection.close();
});

test('worker crash rejects all operations without leaking native error strings or retrying writes',async()=>{
  const {connection,worker}=await connected();const pending=connection.open();
  const failure=assert.rejects(pending,/Mobile transport stopped/);
  worker.onerror({message:'private relay URL must not escape'});await failure;
  assert.equal(worker.terminated,1);assert.equal(worker.sent.filter(x=>x.op==='open').length,1);
});

test('an already aborted signal creates no worker',async()=>{
 const controller=new AbortController();controller.abort();const before=FakeWorker.instances.length;
 await assert.rejects(createMobileConnection('11'.repeat(32),'https://relay.example',key(),controller.signal),{name:'AbortError'});
 assert.equal(FakeWorker.instances.length,before);
});
