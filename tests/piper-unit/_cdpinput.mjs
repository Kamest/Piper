// A real mouse click with modifiers for the headless browser tests (agent-browser's click has none): Chrome's
// Input.dispatchMouseEvent on the page whose URL contains a marker.
// usage: node _cdpinput.mjs <cdp websocket url> <url marker> <x> <y> <modifiers: alt,meta,ctrl,shift or ->
const [cdp, marker, x, y, mods = '-'] = process.argv.slice(2);
const base = new URL(cdp.replace(/^ws/, 'http'));
const pages = await (await fetch(`${base.protocol}//${base.host}/json/list`)).json();
const page = pages.find((p) => p.type === 'page' && p.url.includes(marker));
if (!page) throw new Error(`no page with ${marker}`);
const bits = { alt: 1, ctrl: 2, meta: 4, shift: 8 };
const modifiers = mods === '-' ? 0 : mods.split(',').reduce((n, m) => n | bits[m], 0);
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((ok, bad) => { ws.onopen = ok; ws.onerror = bad; });
let id = 0;
const send = (method, params) => new Promise((ok, bad) => {
  const my = ++id;
  ws.addEventListener('message', function on(m) {
    const r = JSON.parse(m.data);
    if (r.id !== my) return;
    ws.removeEventListener('message', on);
    if (r.error) bad(new Error(r.error.message)); else ok(r.result);
  });
  ws.send(JSON.stringify({ id: my, method, params }));
});
const at = { x: Number(x), y: Number(y), modifiers };
await send('Input.dispatchMouseEvent', { type: 'mouseMoved', ...at });
await send('Input.dispatchMouseEvent', { type: 'mousePressed', button: 'left', buttons: 1, clickCount: 1, ...at });
await send('Input.dispatchMouseEvent', { type: 'mouseReleased', button: 'left', buttons: 0, clickCount: 1, ...at });
ws.close();
console.log('ok');
