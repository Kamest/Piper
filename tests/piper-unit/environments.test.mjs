import './_sandbox.mjs';
import { test } from 'node:test';
import assert from 'node:assert/strict';

class Element extends EventTarget {
  constructor(tag) { super(); this.tagName = tag; this.children = []; }
  setAttribute(key, value) { if (key === 'value') this.value = value; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren() { this.children = []; }
  get textContent() { return this.children.map(n => n.textContent).join(''); }
  focus() {}
  querySelector(selector) { return descendants(this).find(n => selector.startsWith('.') ? n.className?.split(' ').includes(selector.slice(1)) : n.tagName === selector) ?? null; }
}
const descendants = node => node.children.flatMap(child => child instanceof Element ? [child, ...descendants(child)] : []);
globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }) };
globalThis.window = { localStorage: { getItem: () => 'fixture', setItem() {}, removeItem() {} } };
globalThis.CustomEvent ??= class extends Event {};
const { createServerSettings } = await import('../../piper/web/dist/environments.js');
const { hosts } = await import('../../piper/web/dist/environment.js');
const tick = () => new Promise(resolve => setTimeout(resolve, 0));

test('Claude Pen R3 exposes Recommended as a separate service badge', async () => {
  globalThis.fetch = async () => new Response('{"environments":[],"providers":[]}');
  const settings = createServerSettings({ changed() {}, toast() {}, modal() {}, closeModal() {} });
  settings.show(); await tick();
  descendants(settings.el).find(n => n.tagName === 'button' && n.textContent === 'Add server').dispatchEvent(new Event('click'));
  const badge = descendants(settings.el).find(n => n.tagName === 'span' && n.textContent === 'Recommended');
  assert.ok(badge, 'R3 requires a separate Recommended badge beside Piper service');
  assert.ok(descendants(settings.el).some(n => n.tagName === 'span' && n.textContent.includes('no sudo, no open ports')),
    'the badge must not replace the service explanation');
});

test('Servers initial load failure exposes retry and recovers without losing the page', async () => {
  let calls = 0, recover;
  globalThis.fetch = async () => {
    calls++;
    if (calls === 1) return new Response('{"error":"Server list unavailable"}', { status: 503 });
    return new Promise(resolve => { recover = () => resolve(new Response('{"environments":[],"providers":[]}')); });
  };
  const settings = createServerSettings({ changed() {}, toast() {}, modal() {}, closeModal() {} });
  settings.show();
  await tick();
  assert.match(settings.el.textContent, /Server list unavailable/);
  assert.doesNotMatch(settings.el.textContent, /Loading servers/);
  const retry = settings.el.children.find(n => n.tagName === 'button');
  assert.equal(retry.textContent, 'Try again');
  retry.dispatchEvent(new Event('click'));
  settings.show(); // repeated navigation cannot duplicate an in-flight request
  assert.equal(calls, 2);
  assert.match(settings.el.textContent, /Loading servers/);
  recover();
  await tick();
  assert.match(settings.el.textContent, /No servers yet/);
  assert.equal(settings.dirty(), false);
});

test('declining installation keeps the saved connection and offers the authored SSH-only alternative', async () => {
  const host = { id: 'build', label: 'Build box', host: 'build.example', project_path: '/srv/projects', mode: 'service' };
  const posts = [];
  globalThis.fetch = async (path, init) => {
    if (init.method === 'POST') {
      posts.push(path);
      return new Response(JSON.stringify({ service_installed: false, service_running: false, systemd: true, tmux: true }));
    }
    return new Response(JSON.stringify({ environments: [host], providers: [] }));
  };
  const settings = createServerSettings({ changed() {}, toast() {}, modal() {}, closeModal() {} });
  const click = text => descendants(settings.el).find(n => n.tagName === 'button' && n.textContent === text).dispatchEvent(new Event('click'));
  settings.show(); await tick();
  settings.el.querySelector('.server-row').dispatchEvent(new Event('click'));
  click('Test again'); await tick();
  assert.match(settings.el.textContent, /Install Piper service on Build box\?/);
  click('Not now');
  assert.equal(settings.el.querySelector('.server-install'), null);
  assert.equal(settings.dirty(), false);
  assert.deepEqual(posts, ['/api/environments/build/probe']);
  click('Test again'); await tick();
  click('Add this host as an SSH-only server…');
  assert.ok(descendants(settings.el).some(n => n.className === 'sc-level on' && n.textContent.startsWith('SSH only')));
  assert.equal(posts.length, 2, 'choosing SSH only must not install or save without Add and connect');
});

test('a failed remote revoke refreshes the already revoked local grant without replay', async () => {
  const host = { id: 'build', label: 'Build', host: 'build.example', project_path: '/srv/projects', mode: 'service' };
  let providers = [{ environment: 'build', remote_project: 'repo', local_project: 'repo', enabled: true }];
  const requests = [];
  globalThis.fetch = async (path, init) => {
    requests.push([init.method, path]);
    if (init.method === 'POST') {
      providers = []; // controller revokes locally before SSH delivery
      return new Response('{"error":"Server unavailable"}', { status: 502 });
    }
    return new Response(JSON.stringify({ environments: [host], providers }));
  };
  const settings = createServerSettings({ changed() {}, toast() {}, modal() {}, closeModal() {} });
  settings.show(); await tick();
  settings.el.querySelector('.server-row').dispatchEvent(new Event('click'));
  descendants(settings.el).find(n => n.tagName === 'button' && n.textContent === 'Revoke').dispatchEvent(new Event('click'));
  await tick();
  assert.match(settings.el.textContent, /Server unavailable/);
  assert.match(settings.el.textContent, /No projects mapped yet/);
  assert.ok(!descendants(settings.el).some(n => n.textContent === 'Revoke'));
  assert.equal(requests.filter(([method]) => method === 'POST').length, 1);
  assert.equal(requests.filter(([method]) => method === 'GET').length, 2);
  assert.equal(settings.dirty(), false);
});

test('an unknown install outcome offers status verification and prevents an accidental reinstall', async () => {
  const host = { id: 'build', label: 'Build box', host: 'build.example', project_path: '/srv/projects', mode: 'service' };
  let installed = false, installs = 0;
  globalThis.fetch = async (path, init) => {
    if (path.endsWith('/install')) {
      installs++; installed = true;
      return new Response('{"error":"Connection interrupted"}', { status: 502 });
    }
    if (init.method === 'POST') return new Response(JSON.stringify({ service_installed: installed, service_running: installed, systemd: true }));
    return new Response(JSON.stringify({ environments: [host], providers: [] }));
  };
  const settings = createServerSettings({ changed() {}, toast() {}, modal() {}, closeModal() {} });
  const click = text => descendants(settings.el).find(n => n.tagName === 'button' && n.textContent === text).dispatchEvent(new Event('click'));
  settings.show(); await tick();
  settings.el.querySelector('.server-row').dispatchEvent(new Event('click'));
  click('Test again'); await tick();
  click('Install Piper service'); await tick();
  assert.match(settings.el.textContent, /Install result unknown/);
  assert.ok(!descendants(settings.el).some(n => n.tagName === 'button' && n.textContent === 'Install Piper service'));
  click('Check status'); await tick();
  assert.doesNotMatch(settings.el.textContent, /Install result unknown/);
  assert.match(settings.el.textContent, /Connected · Piper service/);
  assert.equal(installs, 1);
});

test('a server with active sessions keeps the Pen removal guard and explains how to proceed', async () => {
  const host = { id: 'busy', label: 'Build box', host: 'build.example', project_path: '/srv/projects', mode: 'service' };
  hosts.set(host.id, { host, online: true, snapshot: { sessions: [{ state: 'working', hidden: false }] } });
  globalThis.fetch = async () => new Response(JSON.stringify({ environments: [host], providers: [] }));
  try {
    const settings = createServerSettings({ changed() {}, toast() {}, modal() {}, closeModal() {} });
    settings.show(); await tick();
    settings.el.querySelector('.server-row').dispatchEvent(new Event('click'));
    assert.equal(descendants(settings.el).find(n => n.textContent === 'Remove server…').disabled, true);
    assert.match(settings.el.textContent, /Stop or end its 1 session first/);
    assert.match(settings.el.textContent, /Fixed — 1 session runs here/);
  } finally { hosts.delete(host.id); }
});

test('a failed SSH host-key check exposes the authored terminal instruction and explicit retry', async () => {
  const host={ id:'build',label:'Build box',host:'build.example',project_path:'/srv/projects',mode:'service' };
  let checks=0;
  globalThis.fetch=async (path,init)=>{
    if(init.method==='POST'){checks++;return new Response('{"error":"Host key verification failed."}',{status:502});}
    return new Response(JSON.stringify({environments:[host],providers:[]}));
  };
  const settings=createServerSettings({changed(){},toast(){},modal(){},closeModal(){}});
  settings.show();await tick();settings.el.querySelector('.server-row').dispatchEvent(new Event('click'));
  descendants(settings.el).find(n=>n.tagName==='button'&&n.textContent==='Test again').dispatchEvent(new Event('click'));await tick();
  assert.match(settings.el.textContent,/Can't connect over SSH/);
  assert.match(settings.el.textContent,/Connect once from a terminal on this computer/);
  assert.match(settings.el.textContent,/ssh build.example/);
  assert.ok(descendants(settings.el).some(n=>n.tagName==='button'&&n.textContent==='Try again'));
  assert.equal(checks,1,'no automatic retry of a rejected host key');
});

test('Claude R11 detects a running service root mismatch and requires an explicit update', async () => {
  const host={id:'build',label:'Build box',host:'build.example',project_path:'/srv/projects',mode:'service'};
  let installs=0, match=false;
  globalThis.fetch=async (path,init)=>{
    if(path.endsWith('/install')){installs++;return new Response(JSON.stringify({error:"Can't update yet: 2 server sessions are active. Stop them, then try Update service again. No configuration was changed."}),{status:400});}
    if(init.method==='POST') return new Response(JSON.stringify({service_installed:true,service_running:true,project_root_matches:match,requested_project_root:'/srv/projects',service_project_root:match?'/srv/projects':'/old/projects',systemd:true}));
    return new Response(JSON.stringify({environments:[host],providers:[]}));
  };
  const settings=createServerSettings({changed(){},toast(){},modal(){},closeModal(){}});
  const click=text=>descendants(settings.el).find(n=>n.tagName==='button'&&n.textContent===text).dispatchEvent(new Event('click'));
  settings.show();await tick();settings.el.querySelector('.server-row').dispatchEvent(new Event('click'));
  click('Test again');await tick();
  assert.match(settings.el.textContent,/runs in another projects folder/);
  assert.match(settings.el.querySelector('.server-root-paths').textContent,/Expected\/srv\/projectsRunning\/old\/projects/);
  assert.match(settings.el.textContent,/this account's shared Piper service/);
  assert.equal(installs,0,'connecting must never automatically reconfigure the account');
  click('Update service');await tick();
  assert.equal(installs,1);
  assert.match(settings.el.textContent,/2 server sessions are active/);
  assert.doesNotMatch(settings.el.textContent,/Install result unknown/,'explicit refusal has a known outcome');
  assert.ok(descendants(settings.el).some(n=>n.tagName==='button'&&n.textContent==='Update service'));
  click('Not now');assert.equal(settings.el.querySelector('.server-install'),null);assert.equal(settings.dirty(),false);
  match=true;click('Test again');await tick();
  assert.doesNotMatch(settings.el.textContent,/runs in another projects folder/);
  assert.equal(settings.el.querySelector('.server-root-paths'),null);
});
