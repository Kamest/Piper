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
PAGE = b'''<!doctype html><html data-scheme="dark"><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/piper/app.css"><link rel="stylesheet" href="/piper/mobile.css"></head><body class="no-queue no-inspector"><div id="app"><nav id="actbar"></nav><main id="main"><header class="head">Conversation</header><div class="body"><div class="view"></div></div></main></div><div id="overlay"></div><script type="module" src="/fixture.js"></script></body></html>'''
FIXTURE = r'''
import {api} from '/piper/dist/api.js';
import {createConvo} from '/piper/dist/convo.js';
import {installMobileViewport,showMobileState,clearMobileState} from '/piper/dist/mobileui.js';
installMobileViewport();
const data = id => ({agent:'codex',turns:Array.from({length:60},(_,i)=>({role:i%2?'assistant':'user',text:`${id} message ${i}\n\n`+'Readable transcript content. '.repeat(12)}))});
const f = window.fixture = {data:{a:data('A'),b:data('B')},time:0,poll:null,defer:false,pending:[],showMobileState,clearMobileState};
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
f.ready=true;
'''.encode()

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
    check('long pairing card remains scrollable above the keyboard', '''const f=fixture;f.showMobileState('unpaired','Studio Mac');await f.frame();const stage=document.querySelector('.mobile-stage'),input=stage.querySelector('textarea'),button=[...stage.querySelectorAll('button')].find(node=>node.textContent==='Continue');input.focus();stage.scrollTop=stage.scrollHeight;await f.frame();const r=button.getBoundingClientRect(),s=stage.getBoundingClientRect();return stage.scrollHeight>stage.clientHeight&&r.bottom<=s.bottom&&r.top>=s.top&&scrollY===0;''')
    ab('set', 'viewport', '390', '580')
    js('visualViewport.height=580;window.dispatchEvent(new Event("resize"));await fixture.frame()')
    ab('screenshot', str(OUT / 'pair-keyboard.png'))
finally:
    try:
        ab('close')
    finally:
        server.shutdown()
    if os.environ.get('PIPER_E2E_ARTIFACTS') == '1':
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w',zipfile.ZIP_DEFLATED) as z:
            for path in OUT.iterdir():z.write(path,path.name)
        print('ARTIFACTS '+base64.b64encode(data.getvalue()).decode(),flush=True)
