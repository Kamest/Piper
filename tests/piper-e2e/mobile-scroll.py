#!/usr/bin/env python3
"""Real-browser mobile shell/conversation regressions, only in piper-sandbox.py.

Serves the actual compiled UI modules and CSS with a controlled transcript API.
No live Piper, device key, relay or user conversation is accessed.
"""
import base64
import functools
import http.server
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import zipfile

SK = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SK / 'tests/piper-unit'))
import _env  # noqa: E402

WORK = Path(os.environ['TEST_SANDBOX_WORK']) / 'mobile-scroll'
WORK.mkdir()
OUT = WORK / 'screenshots'
OUT.mkdir()
PAGE = b'''<!doctype html><html data-scheme="dark"><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/piper/vendor/xterm/xterm.css"><link rel="stylesheet" href="/piper/app.css"><link rel="stylesheet" href="/piper/mobile.css"></head><body class="no-queue no-inspector"><div id="app"><nav id="actbar"></nav><main id="main"><header class="head">Conversation</header><div class="body"><div class="view"></div></div></main></div><div id="overlay"></div><script type="module" src="/fixture.js"></script></body></html>'''
FIXTURE = r'''
import {api} from '/piper/dist/api.js';
import {createConvo} from '/piper/dist/convo.js';
import {applyTheme} from '/piper/dist/theme.js';
import {TermView,TERM_FLAGS} from '/piper/dist/term.js';
import {installTransport} from '/piper/dist/transport.js';
import {installMobileViewport,showMobileState,clearMobileState} from '/piper/dist/mobileui.js';
installMobileViewport();
const data = id => ({agent:'codex',turns:Array.from({length:60},(_,i)=>({role:i%2?'assistant':'user',text:`${id} message ${i}\n\n`+'Readable transcript content. '.repeat(12)}))});
const f = window.fixture = {data:{a:data('A'),b:data('B')},time:0,poll:null,defer:false,pending:[],showMobileState,clearMobileState};
f.nativeViewport=window.visualViewport;f.themes=await(await fetch('/piper/themes.json')).json();f.theme=scheme=>{applyTheme(f.themes,scheme);f.term?.retheme()};
const now=Date.now;Date.now=()=>now()+f.time;
const interval=window.setInterval;window.setInterval=(fn,ms,...args)=>ms===5000?(f.poll=fn,1001):interval(fn,ms,...args);
const clearInterval=window.clearInterval;window.clearInterval=id=>{if(id!==1001)clearInterval(id)};
api.conversation=async id=>{const result={data:structuredClone(f.data[id]),etag:''};return f.defer?new Promise(resolve=>f.pending.push(()=>resolve(result))):result};
f.convo=createConvo({toast(){},modal(){},closeModal(){},jump(){}});
f.mount=document.querySelector('.view');
f.convo.show('a');f.mount.append(f.convo.el);
f.turns=()=>f.convo.el.querySelector('.turns');
f.frame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
f.anchor=()=>{const box=f.turns(),top=box.getBoundingClientRect().top;const row=[...box.querySelectorAll('[data-turn]')].find(r=>r.getBoundingClientRect().bottom>top);return {index:row.dataset.turn,offset:row.getBoundingClientRect().top-top}};
f.offset=a=>{const box=f.turns();return box.querySelector(`[data-turn="${a.index}"]`).getBoundingClientRect().top-box.getBoundingClientRect().top};
f.refresh=async()=>{f.time+=6000;f.poll();await f.frame()};
class FakeSocket {
  static OPEN=1;readyState=1;sent=[];
  constructor(){f.socket=this}
  send(value){this.sent.push(JSON.parse(value))}
  close(){this.readyState=3}
  server(value){this.onmessage({data:typeof value==='string'?new TextEncoder().encode(value).buffer:JSON.stringify(value)})}
}
f.opened=[];window.open=(...args)=>{f.opened.push(args);return {}};
f.terminal=async()=>{
  f.clearMobileState();f.convo.hide();f.convo.el.remove();
  f.term=new TermView('fixture','/synthetic',{onRefused:message=>{f.refused=message},onConnection(){},onExit(){},onUrl(uri){if(uri.includes('?session=')){f.internal=uri;return true}return false}});
  installTransport({visibleTerminalsOnly:true,socket(){return new FakeSocket()},fetch(){throw Error('unexpected host-browser request')}});
  f.term.setInputState(true,true,'Mobile fixture');
  f.mount.classList.add('hidden');f.term.mount(document.querySelector('#main > .body'));while(!f.socket)await f.frame();f.socket.onopen();
  f.socket.server({t:'hello',info:{cols:40,rows:30,tty:0,replay_geometry:true}});
  f.socket.server(Array.from({length:150},(_,i)=>'History '+i+'\r\n').join(''));
  f.socket.server({t:'replay-end'});await new Promise(r=>f.term.term.write('',r));await new Promise(r=>setTimeout(r,200));
  document.querySelector('header.head').outerHTML='<header class="head detail one"><div class="dh-top"><div class="title">grp/demo</div><span class="grow"></span><div class="actions"><button class="icon" aria-label="more actions">⋯</button></div><div class="session-tabs-row"><nav class="tabs" aria-label="session view"><button class="tab on">Terminal</button><button class="tab">Conversation</button><button class="tab">Files</button><button class="tab">HTTP</button><button class="tab">History</button></nav><span class="session-keys-slot"></span></div></div></header>';
  document.querySelector('.session-keys-slot').append(f.term.keysToggle());await f.frame();
  TERM_FLAGS.hostLinks=true;
};
f.linkFrame=async()=>{await new Promise(r=>f.term.term.write('\x1b[2J\x1b[Hhttps://example.test/plain\r\n\x1b]8;;https://example.test/osc\x07OSC hyperlink\x1b]8;;\x07\r\n\x1b]8;;http://127.0.0.1:17895/?session=fixture\x07Internal session\x1b]8;;\x07',r));await f.frame()};
f.point=(col,row)=>{const r=f.term.host.querySelector('.xterm-screen').getBoundingClientRect();return {x:r.left+(col-.5)*r.width/f.term.term.cols,y:r.top+(row-.5)*r.height/f.term.term.rows}};
f.questionFrame=async()=>{const t=f.term.term;t.scrollToBottom();await new Promise(r=>t.write('\x1b[2J\x1b[H'+Array.from({length:Math.max(0,t.rows-11)},()=>"\r\n").join('')+'Explored TaskService.java\r\nI can fix this in two places.\r\nQuestion 1/2 - Scope Tests\r\nWhich module should be fixed?\r\n> 1. core (Recommended)\r\n  2. api\r\n  3. both\r\n  4. None of the above\r\n\r\nenter submit - esc dismiss',r));await f.frame()};
f.geometry=()=>{const t=f.term.term,r=f.term.host.getBoundingClientRect();return {cols:t.cols,rows:t.rows,y:t.buffer.active.viewportY,width:r.width,height:r.height,resizes:f.socket.sent.filter(x=>x.t==='resize').length}};
f.ready=true;
'''.encode()

PAGE=PAGE.replace(b'<script type="module"', b'<script type="importmap">' + b'{"imports": {"@xterm/xterm": "/piper/vendor/xterm/xterm.mjs", "@xterm/addon-fit": "/piper/vendor/xterm/addon-fit.mjs", "@xterm/addon-webgl": "/piper/vendor/xterm/addon-webgl.mjs", "@xterm/addon-unicode11": "/piper/vendor/xterm/addon-unicode11.mjs", "@xterm/addon-web-links": "/piper/vendor/xterm/addon-web-links.mjs", "@xterm/addon-search": "/piper/vendor/xterm/addon-search.mjs"}}' + b'</script><script type="module"')

class Static(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path in ('/', '/fixture.js'):
            body = PAGE if self.path == '/' else FIXTURE
            self.send_response(200)
            self.send_header('Content-Type', 'text/html' if self.path == '/' else 'application/javascript')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

class FixtureServer(http.server.ThreadingHTTPServer):
    # The browser fetches the ES-module graph concurrently. The default backlog
    # of five loses connections on macOS before the fixture can initialize.
    request_queue_size = 128


server = FixtureServer(('127.0.0.1', 0), functools.partial(Static, directory=str(SK / 'piper/web')))
# Match actual /piper/* asset paths without copying or modifying the sources.
Static.translate_path = lambda self, path: str(SK / 'piper/web' / path.split('?',1)[0].removeprefix('/piper/'))
threading.Thread(target=server.serve_forever, daemon=True).start()
session = 'ps-' + str(os.getpid())
env = dict(os.environ, AGENT_BROWSER_PROFILE=str(WORK / 'browser'))


def ab(*args):
    r = subprocess.run(['agent-browser', '--session', session, '--json', *args], env=env,
                       capture_output=True, text=True, timeout=40)
    result = json.loads(r.stdout.strip().splitlines()[-1])
    assert result.get('success'), result.get('error')
    return result.get('data', {})


def js(code):
    return ab('eval', '(async()=>{' + code + '})()').get('result')


def check(name, code):
    if os.environ.get('PIPER_E2E_SCROLL_CASE', '') not in name:
        return
    result = js(code)
    if result is not True:
        ab('screenshot', str(OUT / 'failure.png'))
    assert result is True, name + ': ' + json.dumps(result)
    print('PASS:', name, flush=True)


cdp_ws = None
cdp_sid = None
cdp_serial = 0


def cdp(commands):
    # Keep this session attached: detaching resets Chrome's touch/viewport emulation.
    global cdp_ws, cdp_sid, cdp_serial
    from websockets.sync.client import connect
    def command(method, params, session_id=None):
        global cdp_serial
        cdp_serial += 1
        request = {'id': cdp_serial, 'method': method, 'params': params}
        if session_id:
            request['sessionId'] = session_id
        cdp_ws.send(json.dumps(request))
        while True:
            response = json.loads(cdp_ws.recv())
            if response.get('id') == cdp_serial:
                assert 'error' not in response, response
                return response.get('result', {})
    if cdp_ws is None:
        cdp_ws = connect(ab('get', 'cdp-url')['cdpUrl'], proxy=None)
        targets = command('Target.getTargets', {})['targetInfos']
        target = next(t for t in targets if t['type'] == 'page' and t['url'].startswith('http://127.0.0.1:' + str(server.server_port)))
        cdp_sid = command('Target.attachToTarget', {'targetId': target['targetId'], 'flatten': True})['sessionId']
    return [command(method, params, cdp_sid) for method, params in commands]


def touch_tap(point):
    # Actual touch input, with no preceding hover/mousemove.
    cdp([('Input.dispatchTouchEvent', {'type': 'touchStart', 'touchPoints': [{'x': point['x'], 'y': point['y']}]}),
         ('Input.dispatchTouchEvent', {'type': 'touchEnd', 'touchPoints': []})])


def touch_viewport(width, height):
    # agent-browser set viewport resets hasTouch; retain actual coarse-pointer emulation.
    cdp([('Emulation.setDeviceMetricsOverride', {'width': width, 'height': height, 'deviceScaleFactor': 1, 'mobile': True}),
         ('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 1})])


try:
    ab('open', 'http://127.0.0.1:' + str(server.server_port))
    ab('set', 'device', 'iPhone 12')
    try:
        ab('wait', '.turns [data-turn="59"]')
    except AssertionError:
        print('BROWSER ERRORS:', ab('errors'), flush=True)
        print('FIXTURE STATE:', js("return {url:location.href,ready:!!window.fixture?.ready,rows:document.querySelectorAll('[data-turn]').length,text:document.querySelector('.view')?.textContent.slice(0,500),resources:performance.getEntriesByType('resource').map(r=>({name:r.name,status:r.responseStatus,size:r.decodedBodySize}))}"), flush=True)
        ab('screenshot', str(OUT / 'startup-failure.png'))
        raise
    js('await fixture.frame();await fixture.frame()')
    js('fixture.turns().scrollTop=1400;await fixture.frame()')
    ab('screenshot', str(OUT / 'history-before.png'))
    check('history remains anchored after append', '''const f=fixture,t=f.turns();t.scrollTop=1400;await f.frame();const a=f.anchor();f.data.a.turns.push({role:'assistant',text:'A new answer. '.repeat(40)});await f.refresh();return Math.abs(f.offset(a)-a.offset)<2||{a,after:f.anchor(),scroll:t.scrollTop};''')
    check('user scroll during redraw wins over pending restoration', '''const f=fixture,t=f.turns();t.scrollTop=1400;await f.frame();f.data.a.turns.push({role:'assistant',text:'Concurrent update'});f.time+=6000;f.poll();await Promise.resolve();await Promise.resolve();t.scrollTop=2100;t.dispatchEvent(new Event('scroll'));const a=f.anchor();await f.frame();await f.frame();return Math.abs(f.offset(a)-a.offset)<2||{a,after:f.anchor(),scroll:t.scrollTop};''')
    check('repeated streaming redraws never reclaim a newer reading position', '''const f=fixture,t=f.turns();for(let i=0;i<30;i++){f.data.a.turns.at(-1).text+=' streaming';f.time+=6000;f.poll();await Promise.resolve();await Promise.resolve();t.scrollTop=600+(i%9)*170;t.dispatchEvent(new Event('scroll'));const a=f.anchor();await f.frame();if(Math.abs(f.offset(a)-a.offset)>=2)return {iteration:i,a,after:f.anchor()}}return true;''')
    ab('screenshot', str(OUT / 'history-after.png'))
    check('hidden conversation preserves reading position on return', '''const f=fixture,a=f.anchor();f.convo.hide();f.mount.classList.add('hidden');f.convo.show('a');f.mount.classList.remove('hidden');await f.frame();return Math.abs(f.offset(a)-a.offset)<2;''')
    check('detached conversation preserves reading position on return', '''const f=fixture,a=f.anchor();f.convo.hide();f.convo.el.remove();f.convo.show('a');f.mount.append(f.convo.el);await f.frame();return Math.abs(f.offset(a)-a.offset)<2;''')
    check('earlier message growth preserves visible message', '''const f=fixture;f.turns().scrollTop=1400;await f.frame();const a=f.anchor();f.data.a.turns[0].text+=' Earlier content. '.repeat(100);await f.refresh();return f.turns().querySelector('[data-turn="0"]').textContent.includes('Earlier content.')&&Math.abs(f.offset(a)-a.offset)<2;''')
    check('switching sessions preserves each reading position', '''const f=fixture,a=f.anchor();f.convo.show('b');await f.frame();f.turns().scrollTop=600;await f.frame();f.convo.show('a');await f.frame();return Math.abs(f.offset(a)-a.offset)<2;''')
    check('following the end survives new answers', '''const f=fixture,t=f.turns();t.scrollTop=t.scrollHeight;await f.frame();f.data.a.turns.push({role:'assistant',text:'More output. '.repeat(100)});await f.refresh();return t.scrollHeight-t.scrollTop-t.clientHeight<2;''')
    check('slow response survives repeated refresh without duplicate requests', '''const f=fixture;f.defer=true;f.data.a.turns.push({role:'assistant',text:'Slow answer'});await f.refresh();f.data.a.turns.push({role:'assistant',text:'Newest answer'});await f.refresh();if(f.pending.length!==1)return {requests:f.pending.length};f.pending[0]();await f.frame();f.defer=false;f.pending=[];return f.turns().lastElementChild.textContent.includes('Slow answer');''')
    check('completed slow response releases the next refresh', '''const f=fixture;await f.refresh();return f.turns().lastElementChild.textContent.includes('Newest answer');''')
    check('explicit turn navigation remains local', '''fixture.convo.show('a',12);await fixture.frame();const t=fixture.turns(),a=t.querySelector('[data-turn="12"]').getBoundingClientRect(),r=t.getBoundingClientRect();return a.top<r.bottom&&a.bottom>r.top&&scrollY===0;''')
    ab('screenshot', str(OUT / 'conversation.png'))
    check('short viewport contains the app without root scrolling', '''const viewport=new EventTarget();viewport.height=innerHeight-240;viewport.offsetTop=0;viewport.scale=1;Object.defineProperty(window,'visualViewport',{configurable:true,value:viewport});window.dispatchEvent(new Event('resize'));viewport.dispatchEvent(new Event('resize'));await fixture.frame();const r=document.getElementById('app').getBoundingClientRect();return r.bottom<=viewport.height+1&&document.scrollingElement.scrollHeight<=document.scrollingElement.clientHeight;''')
    check('viewport resize preserves the visible message', '''const f=fixture;f.turns().scrollTop=1400;await f.frame();const a=f.anchor();visualViewport.height-=120;window.dispatchEvent(new Event('resize'));await f.frame();return Math.abs(f.offset(a)-a.offset)<2;''')
    check('late content layout preserves the visible message', '''const f=fixture,a=f.anchor();f.turns().querySelector('[data-turn="0"]').style.minHeight='2000px';await f.frame();await f.frame();return Math.abs(f.offset(a)-a.offset)<2;''')
    check('filter focus does not move the page', '''fixture.convo.el.querySelector('input').focus();await fixture.frame();return scrollY===0&&document.scrollingElement.scrollWidth<=innerWidth;''')
    check('long pairing card remains scrollable above the keyboard', '''const f=fixture;f.showMobileState('unpaired','Studio Mac');await f.frame();const stage=document.querySelector('.mobile-stage');stage.querySelector('.mobile-link-fallback > summary').click();await f.frame();const input=stage.querySelector('textarea'),button=[...stage.querySelectorAll('button')].find(node=>node.textContent==='Continue');input.focus();stage.scrollTop=stage.scrollHeight;await f.frame();const r=button.getBoundingClientRect(),s=stage.getBoundingClientRect();return stage.scrollHeight>stage.clientHeight&&r.bottom<=s.bottom&&r.top>=s.top&&scrollY===0;''')
    ab('set', 'viewport', '390', '580')
    js('visualViewport.height=580;window.dispatchEvent(new Event("resize"));await fixture.frame()')
    ab('screenshot', str(OUT / 'pair-keyboard.png'))
    if os.environ.get('PIPER_E2E_TERMINAL_INTERACTION') == '1':
        ab('set', 'viewport', '390', '844')
        touch_viewport(390, 844)
        js("Object.defineProperty(window,'visualViewport',{configurable:true,value:fixture.nativeViewport});window.dispatchEvent(new Event('resize'));await fixture.terminal();await fixture.linkFrame()")
        for row, suffix in ((1, 'plain'), (2, 'osc')):
            touch_tap(js(f'return fixture.point(5,{row})'))
            result = js('await fixture.frame();return {opened:fixture.opened.map(x=>x[0]),refused:fixture.refused}')
            assert result['opened'].count('https://example.test/' + suffix) == 1, result
            print('PASS: touch-only terminal ' + suffix + ' URL opens exactly once on the phone', flush=True)
        touch_tap(js('return fixture.point(5,3)'))
        assert js("return fixture.internal?.includes('session=fixture') && fixture.opened.length===2")
        ab('screenshot', str(OUT / 'terminal-links.png'))
        check('keys are closed by default and their toggle is outside the scrolling tabs', '''const f=fixture,k=f.term.touchKeys;return k.el.hidden&&document.querySelector('.session-keys-slot button')?.getAttribute('aria-expanded')==='false'&&!document.querySelector('.tabs').contains(document.querySelector('.termkeys-toggle'));''')
        check('pinned key toggle is visible and inside the phone viewport', '''const b=document.querySelector('.termkeys-toggle'),r=b.getBoundingClientRect(),h=document.querySelector('.head');return !b.hidden&&r.width>=44&&r.left>=0&&r.right<=innerWidth||{hidden:b.hidden,coarse:matchMedia('(pointer: coarse)').matches,hover:matchMedia('(hover: none)').matches,button:r.toJSON(),header:h.getBoundingClientRect().toJSON(),container:getComputedStyle(h).container,row:getComputedStyle(document.querySelector('.session-tabs-row')).display,phoneWidth:innerWidth};''')
        js('fixture.term.term.scrollToLine(50);await fixture.frame();fixture.before=fixture.geometry();fixture.focusBefore=document.activeElement')
        ab('screenshot', str(OUT / 'keys-390-dark-closed.png'))
        ab('click', '.session-keys-slot .termkeys-toggle')
        check('opening keys preserves terminal cells, scrollback and PTY geometry', '''await fixture.frame();return !fixture.term.touchKeys.el.hidden&&JSON.stringify(fixture.before)===JSON.stringify(fixture.geometry())&&document.activeElement===fixture.focusBefore||{hidden:fixture.term.touchKeys.el.hidden,before:fixture.before,after:fixture.geometry(),focused:document.activeElement?.outerHTML.slice(0,200)};''')
        ab('screenshot', str(OUT / 'keys-390-dark-open.png'))
        ab('click', '.termkeys-close')
        check('closing keys preserves terminal cells, scrollback and PTY geometry', '''await fixture.frame();return fixture.term.touchKeys.el.hidden&&JSON.stringify(fixture.before)===JSON.stringify(fixture.geometry());''')
        for width, height in ((390, 844), (320, 568), (844, 390)):
            ab('set', 'viewport', str(width), str(height))
            touch_viewport(width, height)
            for scheme in ('dark', 'light'):
                ab('set', 'media', scheme)
                js("fixture.theme(" + json.dumps(scheme) + ");window.dispatchEvent(new Event('resize'));await fixture.frame()")
                if not js('return fixture.term.touchKeys.el.hidden'):
                    ab('click', '.termkeys-close')
                ab('screenshot', str(OUT / f'keys-{width}-{scheme}-closed.png'))
                ab('click', '.session-keys-slot .termkeys-toggle')
                check('key targets fit inside the terminal at ' + str(width) + ' ' + scheme, '''await fixture.frame();const k=fixture.term.touchKeys.el,p=fixture.term.host.parentElement.getBoundingClientRect(),r=k.getBoundingClientRect(),keys=[...k.querySelectorAll('button')];return r.left>=p.left&&r.right<=p.right&&r.bottom<=p.bottom+1&&keys.every(b=>b.getBoundingClientRect().width>=44&&b.getBoundingClientRect().height>=44)&&document.documentElement.scrollWidth<=innerWidth||{panel:r.toJSON(),pane:p.toJSON(),targets:keys.map(b=>({label:b.textContent,width:b.getBoundingClientRect().width,height:b.getBoundingClientRect().height}))};''')
                check('key layout follows pane width at ' + str(width) + ' ' + scheme, '''const k=fixture.term.touchKeys.el,b=[...k.querySelectorAll('.termkeys-grid button')].map(e=>e.getBoundingClientRect()),wide=fixture.term.host.parentElement.clientWidth>=620;return wide?b.every(r=>r.top===b[0].top):b[0].top!==b[5].top;''')
                js('await fixture.questionFrame();fixture.term.term.blur();fixture.term.syncKeys()')
                if width == 320 and scheme == 'light':
                    ab('click', '.termkeys-grid button[aria-label="Shift, applies to next arrow or Tab"]')
                    check('compact Shift label stays on one line', '''const b=fixture.term.touchKeys.el.querySelector('button[aria-label^="Shift,"]'),r=document.createRange();r.selectNodeContents(b);const rects=[...r.getClientRects()];return rects.every(x=>Math.abs(x.top-rects[0].top)<1)&&b.scrollWidth<=b.clientWidth;''')
                ab('screenshot', str(OUT / f'keys-{width}-{scheme}-open.png'))
                if width == 320 and scheme == 'light':
                    ab('click', '.termkeys-grid button[aria-label="Shift, applies to next arrow or Tab"]')
        ab('set','media','dark');js("fixture.theme('dark')")
        ab('set','viewport','390','508');touch_viewport(390,508)
        ab('click', '.termkeys-grid button[aria-label="Show keyboard"]')
        js('await fixture.frame();await fixture.questionFrame()')
        ab('screenshot', str(OUT / 'keys-390-keyboard.png'))
        ab('click', '.termkeys-grid button[aria-label="Hide keyboard"]')
        # The pinned control stays in place while only the view tabs scroll.
        ab('set', 'viewport', '320', '568');touch_viewport(320,568)
        check('key toggle does not scroll with view tabs', '''await fixture.frame();const b=document.querySelector('.termkeys-toggle'),tabs=document.querySelector('.tabs'),left=b.getBoundingClientRect().left;tabs.scrollLeft=1000;await fixture.frame();return b.getBoundingClientRect().left===left&&tabs.scrollLeft>0;''')
        js('fixture.term.term.blur();fixture.term.syncKeys();fixture.focusBefore=document.activeElement')
        ab('click', '.termkeys-grid button[aria-label="Show keyboard"]')
        check('Type alone focuses terminal input', '''return document.activeElement===fixture.term.term.textarea;''')
        # Simulate the usable visual viewport above a keyboard; Chromium has no native iOS keyboard.
        ab('set', 'viewport', '320', '308');touch_viewport(320,308)
        check('keyboard-sized viewport preserves open keys inside its pane', '''await fixture.frame();await fixture.frame();const k=fixture.term.touchKeys.el,r=k.getBoundingClientRect(),p=fixture.term.host.parentElement.getBoundingClientRect();return !k.hidden&&r.top>=p.top&&r.bottom<=p.bottom+1;''')
        ab('screenshot', str(OUT / 'keys-320-keyboard.png'))
        ab('click', '.termkeys-grid button[aria-label="Hide keyboard"]')
        check('Hide alone releases terminal input focus', '''return document.activeElement!==fixture.term.term.textarea;''')
        js('fixture.inputsBefore=fixture.socket.sent.filter(m=>m.t==="in").length;fixture.socket.readyState=3;fixture.term.syncKeys()')
        ab('click', '.termkeys-grid button[aria-label="Enter"]')
        check('disconnected panel stays visible, disabled, and never queues input', '''return !fixture.term.touchKeys.el.hidden&&[...fixture.term.touchKeys.el.querySelectorAll('.termkeys-grid button')].every(b=>b.getAttribute('aria-disabled')==='true')&&fixture.socket.sent.filter(m=>m.t==='in').length===fixture.inputsBefore;''')
        ab('screenshot', str(OUT / 'keys-320-keyboard-disconnected.png'))
        js('fixture.socket.readyState=1;fixture.term.syncKeys()')
        check('recovery enables keys without sending deferred input', '''return !fixture.term.touchKeys.el.hidden&&fixture.socket.sent.filter(m=>m.t==='in').length===fixture.inputsBefore&&fixture.term.touchKeys.el.querySelector('button[aria-label="Enter"]').getAttribute('aria-disabled')==='false';''')
        ab('set', 'viewport', '320', '180');touch_viewport(320,180)
        check('very short pane scrolls keys while Close stays reachable', '''await fixture.frame();const k=fixture.term.touchKeys.el;k.scrollTop=k.scrollHeight;await fixture.frame();const r=k.getBoundingClientRect(),c=k.querySelector('.termkeys-close').getBoundingClientRect();return k.scrollHeight>k.clientHeight&&c.top>=r.top&&c.bottom<=r.bottom;''')
        ab('screenshot', str(OUT / 'keys-short-scroll.png'))
        ab('set', 'viewport', '320', '568');touch_viewport(320,568)
        check('background and foreground keep keys closed until a new click', '''Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'));await fixture.frame();return fixture.term.touchKeys.el.hidden;''')
        ab('click', '.session-keys-slot .termkeys-toggle')
        check('switching away and returning never retargets an open panel', '''fixture.term.setInputState(true,false,'Inactive');fixture.term.setInputState(true,true,'Mobile fixture');return fixture.term.touchKeys.el.hidden;''')
        for width in (252,251,268):
            js('fixture.term.el.style.width='+json.dumps(str(width)+'px')+';fixture.term.el.style.flex="none";await fixture.frame();await fixture.frame()')
            check('pane width availability ' + str(width), 'return document.querySelector(".termkeys-toggle").hidden==='+str(width<252).lower()+'&&fixture.term.touchKeys.el.hidden;')
            if width == 252:
                ab('click','.session-keys-slot .termkeys-toggle')
                check('minimum pane still has eleven 44px targets', '''return [...fixture.term.touchKeys.el.querySelectorAll('button')].every(b=>b.getBoundingClientRect().width>=44&&b.getBoundingClientRect().height>=44);''')
                ab('click','.termkeys-close')
        ab('click', '.session-keys-slot .termkeys-toggle')
        cdp([('Emulation.setTouchEmulationEnabled', {'enabled':False})])
        check('desktop fine pointer closes the mobile panel', '''await fixture.frame();return !matchMedia('(pointer: coarse)').matches&&fixture.term.touchKeys.el.hidden&&document.querySelector('.termkeys-toggle').hidden;''')
finally:
    if cdp_ws is not None:
        cdp_ws.close()
    try:
        ab('close')
    finally:
        server.shutdown()
    if os.environ.get('PIPER_E2E_ARTIFACTS') == '1':
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w',zipfile.ZIP_DEFLATED) as z:
            for path in OUT.iterdir():z.write(path,path.name)
        print('ARTIFACTS '+base64.b64encode(data.getvalue()).decode(),flush=True)
