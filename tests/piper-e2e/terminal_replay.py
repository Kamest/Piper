"""Terminal reconnect browser regression; run ONLY through scripts/piper-sandbox.py.

No live Piper sockets, credentials, model calls or user output. Set
PIPER_E2E_ARTIFACT_STDOUT=1 to emit one base64 ZIP for an artifact collector.
"""
from pathlib import Path
import base64
import functools
import http.server
import io
import json
import math
import os
import subprocess
import sys
import threading
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'piper'))
import testsandbox
testsandbox.require()
sys.path.insert(0, str(ROOT / 'tests/piper-unit'))
import _env
import ptyhost
import piperd
import wsproto

WORK = Path(os.environ['TEST_SANDBOX_WORK'])
OUT = WORK / 'artifacts'
OUT.mkdir()


def history(widths, full_redraw=False):
    session = ptyhost.Session('synthetic', 0, -1, widths[0], 47, None, False)
    events = []
    def data(text):
        session.append(text.encode())
        events.append({'data': text})
    data('\x1b[2J\x1b[H')
    paragraph = 'Service and controllers updated. Next: regression tests for registration endpoints.'
    frame = 0
    for index, width in enumerate(widths):
        if index:
            session.resize(width, 47)
            events.append({'cols': width, 'rows': 47})
        # A normal-buffer TUI redraws a two-line progress region in place. Text
        # wraps at the width at the time of that frame; earlier transcript stays.
        for _ in range(3):
            if frame:
                height = math.ceil(len(paragraph) / width) + 1
                data(f'\r\x1b[{height}A\x1b[J')
            frame += 1
            data(paragraph + '\r\n')
            data(f'Running hooks: frame {frame}\r\n')
    if full_redraw:
        data('\x1b[2J\x1b[H' + paragraph + '\r\n' + f'Running hooks: frame {frame}\r\n')
    start, replay = session.slice(session.replay_from())
    return {'events': events, 'cols': session.cols, 'rows': session.rows,
            'replay': replay.decode(), 'safeOffset': start, 'wire': list(session.replay(start)),
            'bytes': len(session.buf), 'widths': widths}


FIXTURES = {name: history(widths) for name, widths in {
    'constant': [48, 48, 48],
    'wide-to-grid': [96, 48],
    'grid-wide-grid': [48, 96, 48],
}.items()}
FIXTURES['grid-wide-grid-full-redraw'] = history([48, 96, 48], full_redraw=True)


def relay_interleaving():
    frames = []
    class Socket:
        def send_text(self, data): frames.append(json.loads(data))
        def send_binary(self, data): frames.append({'data': data.decode()})
        def recv(self): raise wsproto.WSClosed()
        def close(self, *_args): pass
    class Peer:
        def __init__(self, callback): self.callback = callback
        def call(self, op, **_kwargs):
            assert op == 'subscribe'
            # Reply captured at 96 columns; another client resizes after that
            # reply but before the request thread gets scheduled to send hello.
            self.callback({'ev': 'size', 'cols': 48, 'rows': 47})
            self.callback({'ev': 'data', 'data': base64.b64encode(b'new output\r\n').decode()})
            return {'ok': True, 'info': {'cols': 96, 'rows': 47, 'tty': 0}}
        def close(self): pass
    with mock.patch.object(piperd, 'pty_client', side_effect=Peer):
        piperd.Handler._terminal(None, Socket(), 'synthetic', authorized=lambda: True, replay_geometry=True)
    return frames


RELAY = relay_interleaving()
IMPORTS = {'@xterm/' + name: '/piper/web/vendor/xterm/' + file + '.mjs' for name, file in {
    'xterm': 'xterm', 'addon-fit': 'addon-fit', 'addon-webgl': 'addon-webgl',
    'addon-unicode11': 'addon-unicode11', 'addon-web-links': 'addon-web-links',
    'addon-search': 'addon-search'}.items()}
PAGE = r'''<!doctype html><meta charset="utf-8"><title>Piper synthetic terminal replay</title>
<script>window.reproErrors=[];window.addEventListener('error',e=>window.reproErrors.push(e.message));window.addEventListener('unhandledrejection',e=>window.reproErrors.push(String(e.reason)));</script>
<link rel="stylesheet" href="/piper/web/vendor/xterm/xterm.css">
<style>body{background:#151515;color:#ddd;font:14px monospace}main{display:flex;gap:24px}.host,.thost{width:430px;height:730px}h2{font:16px monospace}.hidden{display:none}</style>
<main><section><h2>Live timeline</h2><div id="live" class="host"></div></section>
<section><h2>Reconnect: current replay</h2><div id="replay" class="host"></div></section>
<section><h2>Replay with resize timeline</h2><div id="ordered" class="host"></div></section></main>
<script type="module">
import { Terminal } from '/piper/web/vendor/xterm/xterm.mjs';
import { TermView } from '/piper/web/dist/term.js';
import { installTransport } from '/piper/web/dist/transport.js';
class FakeSocket {
 static OPEN=1; static last;
 readyState=1; sent=[];
 constructor(url){if(!url.includes('/ws/'))throw Error('unexpected socket');FakeSocket.last=this;}
 send(data){this.sent.push(data);} close(){this.onclose?.({code:1000});}
 server(m){this.onmessage({data:m instanceof ArrayBuffer?m:typeof m==='string'?new TextEncoder().encode(m).buffer:JSON.stringify(m)});}
}
window.WebSocket=FakeSocket;
const write=(t,s)=>new Promise(r=>t.write(s,r));
const snapshot=t=>{const b=t.buffer.active;return {lines:Array.from({length:b.length},(_,i)=>({text:b.getLine(i).translateToString(true),wrapped:b.getLine(i).isWrapped})),x:b.cursorX,y:b.cursorY,base:b.baseY,cols:t.cols,rows:t.rows}};
const makeView=(cols,rows,hello=true)=>{
 const view=new TermView('synthetic','/synthetic',{onRefused(){},onExit(){},onConnection(){},onImage:async()=>''});
 document.querySelector('#replay').replaceChildren(view.el);view.term.open(view.host);
 // Hold the renderer at the announced geometry; layout/focus is not under test.
 view.frozen=true;view.connect();const ws=FakeSocket.last;ws.onopen();if(hello)ws.server({t:'hello',info:{cols,rows,tty:0,replay_geometry:true}});
 return {view,ws};
};
window.runCase=async(name)=>{
 const f=await(await fetch('/fixtures.json')).json().then(x=>x[name]);
 const terms={};
 for(const id of ['live','ordered']){document.querySelector('#'+id).replaceChildren();const t=new Terminal({cols:f.widths[0],rows:f.rows,scrollback:10000,fontSize:13.5,lineHeight:1.3,fontFamily:'"JetBrains Mono", "JetBrainsMono Nerd Font Mono", "JetBrainsMono NFM", "SF Mono", Menlo, monospace',allowProposedApi:true});t.open(document.querySelector('#'+id));terms[id]=t;}
 for(const ev of f.events){for(const id of ['live','ordered']){if(ev.data!==undefined)await write(terms[id],ev.data);else terms[id].resize(ev.cols,ev.rows);}}
 const {view,ws}=makeView(f.cols,f.rows);terms.replay=view.term;
 for(const event of f.wire){if(event.ev==='data')ws.server(Uint8Array.from(atob(event.data),c=>c.charCodeAt(0)).buffer);else if(event.ev==='size')ws.server({t:'size',c:event.cols,r:event.rows,replay:true});else ws.server({t:'replay-end'});}
 await write(view.term,'');
 const snaps=Object.fromEntries(Object.entries(terms).map(([k,t])=>[k,snapshot(t)]));
 const result={name,widths:f.widths,safeOffset:f.safeOffset,bytes:f.bytes,rawReplayMatches:JSON.stringify(snaps.live)===JSON.stringify(snaps.replay),orderedReplayMatches:JSON.stringify(snaps.live)===JSON.stringify(snaps.ordered),snapshots:snaps};
 window.lastResult=result; window.terms=terms;window.lastView=view;return result;
};
window.runOrdering=async()=>{
 window.lastView?.dispose();
 const {view,ws}=makeView(96,47);await write(view.term,'');
 const expected=new Terminal({cols:96,rows:47,allowProposedApi:true});expected.open(document.querySelector('#ordered'));
 const events=[{data:'older frame\r\n'},{t:'size',c:48,r:47},{data:'N'.repeat(70)+'\r\n'},{t:'size',c:96,r:47},{data:'W'.repeat(80)+'\r\n'}];
 for(const ev of events)ws.server(ev.data??ev);
 for(const ev of events){if(ev.data!==undefined)await write(expected,ev.data);else expected.resize(ev.c,ev.r);}
 await write(view.term,'');
 const orderedBurstMatches=JSON.stringify(snapshot(view.term))===JSON.stringify(snapshot(expected));
 view.dispose();
 const next=makeView(96,47,false);
 const relay=await(await fetch('/relay.json')).json();
 for(const frame of relay){next.ws.server(frame.data??frame);if(frame.t==='size')await write(next.view.term,'');}
 await write(next.view.term,'');
 const result={orderedBurstMatches,relayFrames:relay.map(x=>x.t??'data'),finalRendererCols:next.view.term.cols,latestPtyCols:48,helloRaceReproduced:next.view.term.cols!==48};
 next.view.dispose();expected.dispose();return result;
};

window.runQuietGeometry=async()=>{
 window.lastView?.dispose();
 const host=document.createElement('div');Object.assign(host.style,{position:'fixed',inset:'16px',background:'#151515',zIndex:10});document.body.append(host);
 const view=new TermView('geometry-quiet','/synthetic',{onRefused(){},onExit(){},onConnection(){},onImage:async()=>''});
 view.host.style.width=(innerWidth-32)+'px';view.host.style.height=(innerHeight-32)+'px';
 view.mount(host);const ws=FakeSocket.last;ws.onopen();
 await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
 const dimensions=view.fit.proposeDimensions();ws.server({t:'hello',info:{...dimensions,tty:0,replay_geometry:true}});
 ws.server(Array.from({length:200},(_,i)=>'Historical line '+i+'\r\n').join(''));
 ws.server({t:'replay-end'});await write(view.term,'');await new Promise(r=>setTimeout(r,150));
 // A real agent receives SIGWINCH even for unchanged dimensions. Count every resize request.
 const sent=()=>ws.sent.map(x=>JSON.parse(x)).filter(x=>x.t==='resize');
 const afterConnect=sent().length;
 view.term.scrollToLine(70);await new Promise(r=>requestAnimationFrame(r));const anchor=view.term.buffer.active.viewportY;
 for(let i=0;i<8;i++){view.refit();await new Promise(r=>setTimeout(r,80));}
 const result={width:innerWidth,afterConnect,afterEightReturns:sent().length,anchor,after:view.term.buffer.active.viewportY};
 Object.defineProperty(document,'hidden',{configurable:true,value:true});view.host.style.height=(innerHeight-160)+'px';
 await new Promise(r=>setTimeout(r,150));result.backgroundResizes=sent().length-result.afterEightReturns;
 Object.defineProperty(document,'hidden',{configurable:true,value:false});view.reclaim();await new Promise(r=>setTimeout(r,150));result.foregroundResizes=sent().length-result.afterEightReturns;
 window.quietView=view;window.quietHost=host;return result;
};

window.runCheckpoint=async(size)=>{
 window.lastView?.dispose();
 const urls=[];installTransport({visibleTerminalsOnly:true,socket(url){urls.push(url);return new FakeSocket(url)},fetch(){throw Error('unused')}});
 const view=new TermView('checkpoint','/synthetic',{onRefused(){},onExit(){},onConnection(){},onImage:async()=>''});
 document.querySelector('#replay').replaceChildren(view.el);view.term.open(view.host);view.frozen=true;
 const expected=new Terminal({cols:96,rows:47,scrollback:10000,allowProposedApi:true});document.querySelector('#ordered').replaceChildren();expected.open(document.querySelector('#ordered'));
 const enc=new TextEncoder(),seed='history line: checkpoint and retained screen\r\n';
 const all=enc.encode('\x1b[2J\x1b[H'+seed.repeat(Math.ceil(size/seed.length))+'\x1b[31mGrü');
 // The last UTF-8 character is deliberately split between old and resumed streams.
 const prefix=all.slice(0,-1),tail=new Uint8Array([all.at(-1),...enc.encode('ße aus Köln\x1b[0m\r\nresume-ok\r\n')]);
 const wait=async condition=>{const end=performance.now()+10000;while(!condition()){if(performance.now()>end)throw Error('checkpoint timeout');await new Promise(r=>setTimeout(r,5))}};
 const body=(ws,bytes,off)=>{ws.server({t:'chunk',off,bytes:bytes.length});for(let i=0;i<bytes.length;i+=65536)ws.server(bytes.slice(i,i+65536).buffer)};
 try{
  view.setInputState(true,true,'Fixture');await wait(()=>urls.length===1);
  let ws=FakeSocket.last;ws.onopen();
  ws.server({t:'hello',checkpoint:true,resumed:false,info:{cols:96,rows:47,pid:111,started:12.5,bytes:prefix.length,replay_geometry:true}});
  body(ws,prefix,0);ws.server({t:'replay-end'});await write(view.term,'');await wait(()=>!view.replaying);
  await write(expected,prefix);
  const resets=view.term.reset.bind(view.term);let resetCount=0;view.term.reset=()=>{resetCount++;resets()};
  view.term.scrollToLine(50);const anchor=view.term.buffer.active.viewportY;
  const start=performance.now();view.setInputState(true,false,'Fixture');view.setInputState(true,true,'Fixture');await wait(()=>urls.length===2);
  const from=new URL(urls[1]).searchParams.get('from');ws=FakeSocket.last;ws.onopen();
  ws.server({t:'hello',checkpoint:true,resumed:true,info:{cols:48,rows:47,pid:111,started:12.5,bytes:prefix.length+tail.length,replay_geometry:true}});
  // Restore the geometry at the offset, then replay the later resize in order.
  ws.server({t:'size',c:96,r:47,replay:true});body(ws,tail,prefix.length);ws.server({t:'size',c:48,r:47,replay:true});ws.server({t:'replay-end'});
  await write(view.term,'');await wait(()=>!view.replaying);await write(expected,tail);expected.resize(48,47);
  const result={history_bytes:prefix.length,resume_bytes:tail.length,from:Number(from),resetCount,ready_ms:performance.now()-start,
   matches:JSON.stringify(snapshot(view.term))===JSON.stringify(snapshot(expected)),committed:view.checkpoint?.offset,expected:prefix.length+tail.length,anchor,after:view.term.buffer.active.viewportY};
  window.checkpointView=view;return result;
 }finally{expected.dispose();view.dispose();installTransport(null)}
};
window.ready=true;
</script>'''
PAGE = PAGE.replace('<script type="module">', '<script type="importmap">' + json.dumps({'imports': IMPORTS}) + '</script><script type="module">')


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path in ('/', '/fixtures.json', '/relay.json'):
            data = (PAGE if self.path == '/' else json.dumps(RELAY if self.path == '/relay.json' else FIXTURES)).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html' if self.path == '/' else 'application/json')
            self.end_headers()
            self.wfile.write(data)
        else:
            super().do_GET()


class StaticServer(http.server.ThreadingHTTPServer):
    request_queue_size = 128  # Match piperd's module-graph backlog, including concurrent mobile browsers.


server = StaticServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(ROOT)))
threading.Thread(target=server.serve_forever, daemon=True).start()
session = 'replay-' + str(os.getpid())
base = [os.environ.get('AGENT_BROWSER', 'agent-browser'), '--session', session, '--json']


def browser(*args, stdin=None):
    r = subprocess.run(base + list(args), input=stdin, text=True, capture_output=True, timeout=45)
    try:
        parsed = json.loads(r.stdout)
    except ValueError:
        raise AssertionError('Browser response: ' + r.stderr[:500])
    assert r.returncode == 0 and parsed.get('success'), parsed
    return parsed.get('data', {}).get('result')


try:
    browser('open', f'http://127.0.0.1:{server.server_port}/')
    browser('set', 'viewport', '1440', '1000')
    try:
        browser('wait', '--fn', 'window.ready === true')
    except AssertionError:
        print('PAGE_ERRORS:', browser('eval', 'window.reproErrors'))
        raise
    results = []
    for name in FIXTURES:
        result = browser('eval', '--stdin', stdin=f'(async()=>await window.runCase({json.dumps(name)}))()')
        results.append(result)
        browser('screenshot', str(OUT / (name + '.png')))
    (OUT / 'results.json').write_text(json.dumps(results, indent=2))
    assert results[0]['rawReplayMatches'], 'constant-width control must match'
    assert all(r['orderedReplayMatches'] for r in results), 'resize-aware control must match'
    assert all(r['rawReplayMatches'] for r in results), 'all reconnects must match the live timeline'
    assert results[3]['rawReplayMatches'], 'full redraw must recover replay'
    ordering = browser('eval', '--stdin', stdin='(async()=>await window.runOrdering())()')
    (OUT / 'ordering.json').write_text(json.dumps(ordering, indent=2))
    assert ordering['orderedBurstMatches'], 'async xterm resize must precede following output'
    assert not ordering['helloRaceReproduced'], 'hello must precede size and preserve latest geometry'
    checkpoint = []
    for size in (512 * 1024, 4 * 1024 * 1024, 8 * 1024 * 1024):
        result = browser('eval', '--stdin', stdin=f'window.runCheckpoint({size})')
        checkpoint.append(result)
        assert result['from'] == result['history_bytes'], 'resume requests received rather than parsed bytes'
        assert result['resetCount'] == 0, 'resume resets retained emulator'
        assert result['matches'], 'split UTF-8/ANSI and geometry differ from uninterrupted output'
        assert result['committed'] == result['expected'], 'parsed offset is incomplete'
        assert result['resume_bytes'] < 100, 'resume retransmits retained history'
    (OUT / 'checkpoint.json').write_text(json.dumps(checkpoint, indent=2))
    print('PASS terminal checkpoints: ' + json.dumps(checkpoint))
    print(json.dumps([{k:v for k,v in r.items() if k != 'snapshots'} for r in results]))
    print(json.dumps(ordering))
    quiet = []
    for width, height in ((390, 844), (834, 1112)):
        browser('set', 'viewport', str(width), str(height))
        result = browser('eval', '--stdin', stdin='window.runQuietGeometry()')
        quiet.append(result)
        assert result['afterConnect'] == 0, 'unchanged reconnect redraws the agent'
        assert result['afterEightReturns'] == 0, 'unchanged view returns redraw the agent'
        assert result['anchor'] > 0 and result['anchor'] == result['after'], 'unchanged layout moves terminal history'
        assert result['backgroundResizes'] == 0, 'background device resizes the shared PTY'
        assert result['foregroundResizes'] == 1, 'foreground device must still fit a genuinely changed viewport'
        browser('screenshot', str(OUT / ('quiet-terminal-' + str(width) + '.png')))
        browser('eval', 'window.quietView.dispose();window.quietHost.remove();true')
    (OUT / 'quiet-geometry.json').write_text(json.dumps(quiet, indent=2))
    print('PASS quiet terminal geometry: ' + json.dumps(quiet))
finally:
    browser('close')
    server.shutdown()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in OUT.iterdir():
            archive.write(path, path.name)
    if os.environ.get('PIPER_E2E_ARTIFACT_STDOUT'):
        print('ARTIFACT_ZIP:' + base64.b64encode(stream.getvalue()).decode())
