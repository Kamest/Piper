#!/usr/bin/env python3
"""Real browser WASM → local TLS relay → native bridge → isolated piperd.

Run ONLY via scripts/piper-sandbox.py, with optional iroh Python runtime and
PIPER_E2E_RELAY_BIN naming iroh-relay 1.4.1. No public relay or live Piper is used.
The ephemeral test certificate is trusted by its SPKI in this test browser only.
"""
import asyncio
import base64
import functools
import gzip
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, parse_qs

SK = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SK / "tests" / "piper-unit"))
import _env  # noqa: E402 — enforces the OS test sandbox before Piper imports
import mobile  # noqa: E402
import iroh  # noqa: E402

WORK = Path(os.environ["TEST_SANDBOX_WORK"]) / "mobile-e2e"
WORK.mkdir()
OUT = WORK / "screenshots"
OUT.mkdir()


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def terminal_input(js, until, out, network):
    """Actual xterm input -> WASM -> relay -> fixture PTY -> painted echo, never live user input."""
    await until("!!window.fixtureActiveTerm?.term && !window.fixtureActiveTerm.replaying && window.fixtureActiveTerm.ws?.readyState===1")
    samples = []
    try:
        for phase in ('normal', 'delayed-2Mbps'):
            network.update(delay=.075 if phase != 'normal' else 0,
                           bytes_per_second=256000 if phase != 'normal' else 0)
            for index in range(4):
                marker = 'fixture-input-' + phase + '-' + str(index)
                metric = await js(r"""(async()=>{
                  const term=window.fixtureActiveTerm.term, marker=""" + json.dumps(marker) + r""";
                  const text=()=>{const b=term.buffer.active;return Array.from({length:Math.min(30,b.length)},(_,i)=>b.getLine(b.length-Math.min(30,b.length)+i)?.translateToString(true)||'').join('\n')};
                  let started=0;
                  const echoed=new Promise((resolve,reject)=>{
                    const watch=term.onWriteParsed(()=>{if(text().includes('got:'+marker)){watch.dispose();clearTimeout(timeout);resolve(performance.now()-started)}});
                    const timeout=setTimeout(()=>{watch.dispose();reject(Error('terminal input echo timed out'))},5000);
                  });
                  // Match separate key events rather than a single paste; the final Enter is sent once.
                  for(const char of marker){term.input(char,true);await new Promise(r=>setTimeout(r,20))}
                  started=performance.now();term.input('\r',true);
                  return {echo_ms:await echoed};
                })()""")
                samples.append({'phase': phase, **metric})
                assert metric['echo_ms'] < 2000, 'fixture typing waits behind terminal traffic'
    finally:
        network.update(delay=0, bytes_per_second=0)
    (out / 'terminal-input-metrics.json').write_text(json.dumps(samples, indent=2))
    print('PASS: real xterm typing through WASM/relay/PTY, maximum painted echo ms ' +
          str(round(max(row['echo_ms'] for row in samples))), flush=True)


async def terminal_key_latency(js, until, out, network):
    """Measure DOM input -> dispatch -> PTY echo -> render before Enter, under output pressure."""
    await until("!!window.fixtureActiveTerm?.term?.textarea")
    results = []
    try:
        for phase in ('idle', 'render-pressure', 'delayed-2Mbps'):
            network.update(delay=.075 if phase == 'delayed-2Mbps' else 0,
                           bytes_per_second=256000 if phase == 'delayed-2Mbps' else 0)
            result = await js(r"""(async()=>{
              const view=window.fixtureActiveTerm, term=view.term, phase=PHASE;
              const sleep=ms=>new Promise(r=>setTimeout(r,ms));
              const text=()=>{const b=term.buffer.active;return Array.from({length:Math.min(30,b.length)},(_,i)=>b.getLine(b.length-Math.min(30,b.length)+i)?.translateToString(true)||'').join('')};
              const waitText=async value=>{const end=performance.now()+8000;while(!text().includes(value)){if(performance.now()>end)throw Error('per-key echo timeout');await sleep(10)}};
              const seed=Math.random().toString(36).slice(2,8),lead='key'+seed+':', reset='reset-'+seed+'-done',chars='abcdefghijklmnopqrst';
              term.input('\r'+reset+'\r',true);await waitText('got:'+reset);term.input(lead,true);await waitText(lead);
              const samples=[], listeners=[], oldSend=view.ws.send.bind(view.ws);
              let pending=null, parsed=-1, rendered=-1, pressure=0;
              view.ws.send=data=>{if(pending && typeof data==='string'){const m=JSON.parse(data);if(m.t==='in'&&m.d===pending.char)pending.dispatch_ms=performance.now()-pending.start}return oldSend(data)};
              listeners.push(term.onWriteParsed(()=>{
                const value=text();for(let i=parsed+1;i<samples.length;i++)if(value.includes(lead+chars.slice(0,i+1))){samples[i].parse_ms=performance.now()-samples[i].start;parsed=i}
              }));
              listeners.push(term.onRender(({start,end})=>{const row=term.buffer.active.cursorY;if(row<start||row>end)return;for(let i=rendered+1;i<=parsed;i++){samples[i].paint_ms=performance.now()-samples[i].start;rendered=i}}));
              try{
                if(phase==='render-pressure')pressure=setInterval(()=>term.write('\x1b7\x1b[1;1H'+('agent output frame\r\n').repeat(10)+'\x1b8'),16);
                for(let i=0;i<chars.length;i++){
                  pending={char:chars[i],start:performance.now()};samples.push(pending);
                  const ta=term.textarea;ta.dispatchEvent(new KeyboardEvent('keyup',{key:'Unidentified',keyCode:0,bubbles:true}));ta.value+=chars[i];ta.dispatchEvent(new InputEvent('input',{data:chars[i],inputType:'insertText',bubbles:true,composed:true}));
                  pending=null;await sleep(80);
                }
                await waitText(lead+chars);const end=performance.now()+2000;while(rendered<samples.length-1&&performance.now()<end)await sleep(10);
                if(samples.some(s=>s.dispatch_ms===undefined||s.parse_ms===undefined||s.paint_ms===undefined))throw Error('missing per-key stage');
                return {phase,samples:samples.map(({start,char,...timing})=>timing),textarea:{font:getComputedStyle(term.textarea).fontSize,autocomplete:term.textarea.autocomplete},rows:term.rows,cols:term.cols};
              }finally{clearInterval(pressure);view.ws.send=oldSend;listeners.forEach(l=>l.dispose());term.input('\r',true)}
            })().catch(error=>({error:String(error.message)}))""".replace('PHASE', json.dumps(phase)))
            assert 'error' not in result, result
            results.append(result)
            print('Per-key latency: ' + json.dumps({'phase': phase, 'count': len(result['samples']),
                **{name: round(max(s[name] for s in result['samples']), 1) for name in ('dispatch_ms', 'parse_ms', 'paint_ms')}}), flush=True)
            assert max(s['paint_ms'] for s in result['samples']) < 2000, 'individual terminal keys stall'
    finally:
        network.update(delay=0, bytes_per_second=0)
        (out / 'terminal-key-metrics.json').write_text(json.dumps(results, indent=2))


async def stress(js, until, ab, out, operations, network):
    """Optional real relay soak and degraded-network matrix; only disposable fixture reads."""
    seconds = max(180, int(os.environ['PIPER_E2E_STRESS_SECONDS']))
    workers_before = await js('window.fixtureWorkers.started')
    await js("""(async()=>{
      const {transportFetch,terminalSocket}=await import('/piper/dist/transport.js');
      const state=await(await transportFetch('/api/state',{})).json();
      const ids=state.sessions.filter(s=>['ready','running'].includes(s.state)).slice(0,2).map(s=>s.id);
      const tab=[...document.querySelectorAll('button')].find(e=>e.textContent==='Terminal'&&e.getClientRects().length);
      tab?.click();
      window.fixtureSoak={samples:[],errors:[],closed:0,done:false,phase:'normal',switches:0,sockets:[]};
      const t=window.fixtureSoak;
      for(let i=0;i<3;i++)await new Promise((resolve,reject)=>{
        const ws=terminalSocket('ws://unused/ws/'+ids[i%ids.length]);t.sockets.push(ws);
        const timer=setTimeout(()=>reject(Error('soak terminal timeout')),8000);
        ws.onmessage=e=>{if(e.data instanceof ArrayBuffer&&e.data.byteLength){clearTimeout(timer);resolve(true)}};
        ws.onclose=()=>{t.closed++;clearTimeout(timer)};ws.onerror=()=>{};
      });
      t.start=performance.now();
      const worker=async n=>{
        while(performance.now()-t.start<SECONDS*1000){
          const start=performance.now(),phase=t.phase;
          try{
            const path=n===0?'/api/state':n===1?'/api/ping':'/api/sessions/'+ids[n%ids.length]+'/conversation';
            const response=await transportFetch(path,{signal:AbortSignal.timeout(8000)});
            if(!response.ok)throw Error('HTTP '+response.status);
            const bytes=(await response.arrayBuffer()).byteLength;t.samples.push({ms:performance.now()-start,phase,path,bytes});
          }catch(error){t.errors.push(error.name+': '+error.message)}
          if(n===0&&t.switches<20){
            const id=ids[t.switches%ids.length];document.querySelector('.qm[data-sid="'+CSS.escape(id)+'"]')?.click();t.switches++;
          }
          await new Promise(resolve=>setTimeout(resolve,150));
        }
      };
      void Promise.all(Array.from({length:4},(_,i)=>worker(i))).finally(()=>{t.elapsed=performance.now()-t.start;t.done=true});
      return true;
    })()""".replace('SECONDS', str(seconds)))
    print('Soak started: four HTTP workers, three persistent terminals, twenty GUI switches', flush=True)
    await asyncio.sleep(30)
    network.update(delay=.075, bytes_per_second=256000)
    await js("window.fixtureSoak.phase='delayed-2Mbps'")
    print('Soak: injecting 75ms per relay write and 2Mbps per direction', flush=True)
    await asyncio.sleep(45)
    network.update(delay=0, bytes_per_second=0)
    await js("window.fixtureSoak.phase='restored'")
    await until('window.fixtureSoak.done', seconds + 30)
    metrics = await js("""(()=>{
      const t=window.fixtureSoak;
      const stats=values=>{const a=values.sort((a,b)=>a-b);return {count:a.length,p50:a[Math.floor((a.length-1)*.5)],p95:a[Math.floor((a.length-1)*.95)],p99:a[Math.floor((a.length-1)*.99)],max:a.at(-1)}};
      const phases=Object.fromEntries([...new Set(t.samples.map(s=>s.phase))].map(p=>[p,stats(t.samples.filter(s=>s.phase===p).map(s=>s.ms))]));
      const result={elapsed_ms:t.elapsed,requests:t.samples.length,errors:t.errors,terminal_closes:t.closed,switches:t.switches,phases};
      t.sockets.forEach(s=>s.close());return result;
    })()""")
    assert not metrics['errors'], metrics
    assert metrics['terminal_closes'] == 0, metrics
    assert metrics['requests'] >= 1000, metrics
    assert await js('window.fixtureWorkers.started') == workers_before, 'soak unexpectedly reconnected the whole phone'
    print('PASS soak: ' + json.dumps(metrics), flush=True)
    recoveries = await recover_network(js, until, ab, operations)
    metrics['recoveries'] = recoveries
    (out / 'stability-metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
    await ab('screenshot', str(out / 'soak-restored-real-gui.png'))


async def slow_requests(js, until, ab, out, faults, operations):
    """Real browser/QUIC: slow conversation refresh and an independent state timeout."""
    faults['conversations'] = True
    await js("""(async()=>{
      const {transportFetch}=await import('/piper/dist/transport.js');
      const {createConvo}=await import('/piper/dist/convo.js');
      const state=await(await transportFetch('/api/state',{})).json();
      const id=state.sessions.find(s=>s.project==='grp/demo').id;
      const errors=[];const convo=createConvo({toast:e=>errors.push(e),modal(){},closeModal(){},jump(){}});
      const host=document.createElement('div');host.style.cssText='position:fixed;inset:60px 0 0;background:var(--bg);z-index:1000;overflow:auto';
      host.append(convo.el);document.body.append(host);
      window.fixtureSlow={host,convo,errors,start:performance.now()};convo.show(id);return true;
    })()""")
    before = faults['conversation_requests']
    await until("window.fixtureSlow.convo.el.querySelector('.turn-list')?.textContent.includes('What is left before production')", 20)
    elapsed = await js("performance.now()-window.fixtureSlow.start")
    assert faults['conversation_requests'] - before <= 1, 'refresh duplicated the pending conversation'
    assert elapsed >= 6000, 'slow response fixture did not run'
    await ab('screenshot', str(out / 'slow-conversation-real-gui.png'))
    await js("(()=>{fixtureSlow.convo.hide();fixtureSlow.host.remove();if(fixtureSlow.errors.length)throw Error('conversation error');return true})()")
    faults['conversations'] = False
    workers_before = await js('window.fixtureWorkers.started')
    faults['state'] = True
    await js("""(async()=>{
      const {transportFetch,terminalSocket}=await import('/piper/dist/transport.js');
      const state=await(await transportFetch('/api/state',{})).json();
      const id=state.sessions.find(s=>s.state==='ready').id;
      let closed=0;const ws=terminalSocket('ws://unused/ws/'+id);
      await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error('terminal unavailable')),8000);ws.onmessage=e=>{if(e.data instanceof ArrayBuffer){clearTimeout(timer);resolve(true)}};ws.onerror=reject});
      ws.onclose=()=>{closed++};
      const app=document.getElementById('app');let offline=false;
      const observer=new MutationObserver(()=>{if(app.inert)offline=true});observer.observe(app,{attributes:true,attributeFilter:['inert']});
      try{
        const slow=transportFetch('/api/state?fixture-slow-state',{signal:AbortSignal.timeout(8000)}).catch(e=>e.name);
        const conversation=transportFetch('/api/sessions/'+id+'/conversation',{}).then(r=>r.json());
        if(await slow!=='TimeoutError')throw Error('deadline fixture did not time out');
        await conversation;await new Promise(resolve=>setTimeout(resolve,1000));
        if(!(await transportFetch('/api/ping',{})).ok||closed||offline)throw Error('one deadline reset shared transport');
      }finally{observer.disconnect();ws.close()}
      return true;
    })()""")
    faults['state'] = False
    assert await js('window.fixtureWorkers.started') == workers_before, 'a healthy deadline probe must not replace the worker'
    print('PASS: >6s conversation renders without overlapping refresh; 8s state deadline preserves concurrent HTTP and terminal', flush=True)


async def large_question(js, until, ab, out, network, faults):
    network.update(delay=.075, bytes_per_second=256000)
    try:
        await js("""(()=>{
          const t=window.fixtureSlow;t.host.replaceChildren(t.convo.el);document.body.append(t.host);
          t.start=performance.now();t.convo.show('fixture-codex-question');return true;
        })()""")
        await until("fixtureSlow.convo.el.textContent.includes('Which environment should be tested?')", 15)
        assert await js("fixtureSlow.convo.el.textContent.includes('Test (Recommended)') && !document.getElementById('app').inert")
        elapsed = await js("performance.now()-fixtureSlow.start")
        assert elapsed < 8000, 'compressed 1.5MB fixture should fit the state deadline on 2Mbps'
        assert faults['compressed_bytes'] < faults['decoded_bytes'] // 5
        await ab('screenshot', str(out / 'codex-question-degraded-network.png'))
        print('PASS: Codex question/options render over 2Mbps relay with large payload: ' + json.dumps({
            'elapsed_ms': elapsed, 'wire_bytes': faults['compressed_bytes'], 'decoded_bytes': faults['decoded_bytes']}), flush=True)
    finally:
        network.update(delay=0, bytes_per_second=0)
        await js("(()=>{fixtureSlow.convo.hide();fixtureSlow.host.remove();return true})()")


async def recover_network(js, until, ab, operations):
    pairs_before = operations.count('pair')
    # Actual CDP network loss, including a failed foreground reconnect before online.
    recoveries = []
    for i, duration in enumerate((.2, 2, 12, .2, 2, 20)):
        await js("(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));return true})()")
        await ab('set', 'offline', 'on')
        if i == 5:
            await js("(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'));return true})()")
        await asyncio.sleep(duration)
        start = time.monotonic()
        await ab('set', 'offline', 'off')
        if i != 5:
            await js("(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'));return true})()")
        await until("!document.getElementById('app').inert && !document.getElementById('mobile-offline')", 45)
        assert await js("(await(await import('/piper/dist/transport.js')).transportFetch('/api/ping',{})).ok")
        transport_ms = round((time.monotonic()-start)*1000)
        await until("!document.getElementById('banner')", 12)
        recoveries.append({'offline_seconds': duration, 'transport_ms': transport_ms, 'gui_ms': round((time.monotonic()-start)*1000)})
        print('PASS recovery: ' + json.dumps(recoveries[-1]), flush=True)
    assert operations.count('pair') == pairs_before, 'network recovery repeated consent/pairing'
    return recoveries


async def cancelled_streams(js, ab, out, operations):
    """Repeatedly discard real WASM streams while preserving one live terminal."""
    workers_before = await js('window.fixtureWorkers.started')
    await js("""(async()=>{
      const {fixtureConnect}=await import('/piper/dist/mobileboot.js');
      const {transportFetch,terminalSocket}=await import('/piper/dist/transport.js');
      const state=await(await transportFetch('/api/state',{})).json();
      const sid=state.sessions.find(s=>s.state==='ready')?.id;
      if(!sid)throw Error('cancellation fixture needs a terminal');
      let closed=false;
      const ws=terminalSocket('ws://unused/ws/'+sid);
      try{
        await new Promise((resolve,reject)=>{
          const timer=setTimeout(()=>reject(Error('cancellation terminal timeout')),8000);
          ws.onmessage=e=>{if(e.data instanceof ArrayBuffer&&e.data.byteLength){clearTimeout(timer);resolve(true)}};
          ws.onclose=()=>{closed=true;clearTimeout(timer);reject(Error('cancellation terminal closed'))};
          ws.onerror=()=>{};
        });
        const raw=await fixtureConnect();
        for(let round=0;round<3;round++){
          const held=[];
          try{
            for(let i=0;i<16;i++){
              const stream=await raw.open();held.push(stream);
              const body=new TextEncoder().encode(JSON.stringify({v:1,op:'http',path:'/api/ping?fixture-abandoned'}));
              const packet=new Uint8Array(4+body.length);new DataView(packet.buffer).setUint32(0,body.length);packet.set(body,4);
              await stream.write(packet); // no FIN: bridge waits for a body which the caller discards
            }
            await new Promise(resolve=>setTimeout(resolve,150));
          }finally{await Promise.all(held.map(stream=>stream.cancel()))}
          await new Promise(resolve=>setTimeout(resolve,150));
          const response=await transportFetch('/api/ping', {signal:AbortSignal.timeout(2000)});
          if(response.status!==200 || closed || document.getElementById('app').inert)
            throw Error('discarded stream result '+JSON.stringify({status:response.status,closed,inert:document.getElementById('app').inert,workers:window.fixtureWorkers,round}));
        }
      }finally{ws.close()}
      return true;
    })()""")
    assert await js('window.fixtureWorkers.started') == workers_before, 'cancellation required a connection replacement'
    await ab('screenshot', str(out / 'cancelled-streams-recovered.png'))
    print('PASS: 48 discarded WASM streams release capacity without reconnecting or closing a healthy terminal', flush=True)


async def main():
    print('Mobile test artifacts: ' + str(OUT), flush=True)
    processes, tasks = [], []
    servers = []
    relay_port, tls_port, static_port = port(), port(), port()
    session = "pm-" + str(os.getpid())
    browser_sessions = set()
    env = dict(os.environ)
    env["AGENT_BROWSER_PROFILE"] = str(WORK / "browser")
    relay_url = "https://127.0.0.1:%d" % tls_port
    relay_capability = "x" * 43  # Fixture-only relay credential, never a deployment secret.
    endpoint = None
    running_guard = None
    web = None
    operations = []
    network = {"delay": 0, "bytes_per_second": 0}
    faults = {'conversations': False, 'state': False, 'conversation_requests': 0}
    original_read_header = mobile.read_header
    original_proxy = mobile.proxy_http
    import digest
    question = digest.readable_record({'type': 'response_item', 'payload': {'type': 'function_call',
        'name': 'request_user_input_async', 'arguments': json.dumps({'questions': [{
            'title': 'Which environment should be tested?', 'options': ['Test (Recommended)', 'Development']}]})}},
        'fixture-codex', 'codex')
    padding = '\n'.join('fixture/project-%05d/src/main/package/service/file-%s: permitted example coverage entry' %
                        (i, hashlib.sha256(str(i).encode()).hexdigest()[:8]) for i in range(16000))
    question_body = json.dumps({'agent': 'codex', 'usage': None, 'turns': [question], 'fixture_padding': padding}).encode()

    def delayed_proxy(value, *args, **kwargs):
        path = value.get('path', '')
        if path == '/api/sessions/fixture-codex-question/conversation':
            data = gzip.compress(question_body, 6) if value.get('accept_encoding') == 'gzip' else question_body
            faults.update(compressed_bytes=len(data), decoded_bytes=len(question_body))
            return {'v': 1, 'status': 200, 'headers': {'Content-Type': 'application/json'},
                    **({'encoding': 'gzip'} if value.get('accept_encoding') == 'gzip' else {})}, data
        if path.endswith('/conversation') and faults['conversations']:
            faults['conversation_requests'] += 1
            time.sleep(6.5)
        if path == '/api/state?fixture-slow-state' and faults['state']:
            time.sleep(9)
        return original_proxy(value, *args, **kwargs)
    mobile.proxy_http = delayed_proxy

    async def observed_header(recv):
        value = await original_read_header(recv)
        operations.append(value.get('op', 'unknown'))  # never record the pairing code or request body
        return value
    mobile.read_header = observed_header

    async def ab(*args):
        browser_sessions.add(session)
        browser_env = dict(env, AGENT_BROWSER_PROFILE=str(WORK / ('browser-' + session)))
        result = await asyncio.to_thread(subprocess.run,
            ["agent-browser", "--session", session,
             *(["--headed", "--profile", browser_env["AGENT_BROWSER_PROFILE"]] if os.environ.get('PIPER_E2E_WEBPUSH') == '1' else []),
             "--json", *args],
            env=browser_env, capture_output=True, text=True, timeout=40)
        try:
            value = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise AssertionError("browser command failed: " + result.stderr[:300])
        assert value.get("success"), value.get("error")
        return value.get("data", {})

    async def js(code):
        return (await ab("eval", "(async () => (" + code + "))()" )).get("result")

    async def until(code, timeout=25):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if await js(code):
                return
            await asyncio.sleep(.2)
        await ab('screenshot', str(OUT / 'failure.png'))
        print('Failure UI: ' + str(await js("({state: document.querySelector('.mobile-stage')?.dataset.state, queue: document.getElementById('queue')?.children.length, banner: document.getElementById('banner')?.textContent,installClicks:window.fixtureInstallClicks})")), flush=True)
        print('Bridge diagnostics: ' + json.dumps({'operations': operations, 'devices': len(mobile.load_state()['devices'])}), flush=True)
        if os.environ.get('PIPER_E2E_MOBILE_TRACE') == '1':
            print('Bootstrap diagnostics: ' + str(await js('window.mobileTestErrors ?? []')), flush=True)
        raise AssertionError("browser condition timed out: " + code)

    def spawn(command, log):
        handle = open(WORK / log, "w")
        process = subprocess.Popen(command, stdout=handle, stderr=subprocess.STDOUT)
        handle.close()
        processes.append(process)
        return process

    try:
        # TLS only wraps the local relay socket; QUIC authenticates the pinned PC key independently.
        key, cert = WORK / "key.pem", WORK / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=127.0.0.1", "-keyout", str(key), "-out", str(cert)],
            check=True, capture_output=True)
        public_key = subprocess.run(["openssl", "x509", "-in", str(cert), "-pubkey", "-noout"],
                                    check=True, capture_output=True).stdout
        der = subprocess.run(["openssl", "pkey", "-pubin", "-outform", "DER"], input=public_key,
                             check=True, capture_output=True).stdout
        spki = base64.b64encode(hashlib.sha256(der).digest()).decode()
        env["AGENT_BROWSER_ARGS"] = env.get("AGENT_BROWSER_ARGS", "--no-sandbox") + ",--ignore-certificate-errors-spki-list=" + spki
        config = WORK / "relay.toml"
        config.write_text('http_bind_addr = "127.0.0.1:%d"\nenable_quic_addr_discovery = false\nenable_metrics = false\naccess.shared_token = ["%s"]\n' % (relay_port, relay_capability))
        relay = spawn([os.environ["PIPER_E2E_RELAY_BIN"], "--dev", "-c", str(config)], "relay.log")

        async def tunnel(reader, writer):
            upstream = None
            try:
                remote, upstream = await asyncio.open_connection("127.0.0.1", relay_port)

                # Match the production reverse proxy: a private query capability
                # becomes an Authorization header and never reaches upstream URLs.
                request = await reader.readuntil(b'\r\n\r\n')
                assert len(request) <= 16384
                lines = request.decode('ascii').split('\r\n')
                method, target, version = lines[0].split(' ')
                url = urlsplit(target)
                credential = parse_qs(url.query).get('token', [''])[0]
                filtered = [line for line in lines[1:] if line and not line.lower().startswith(('authorization:', 'cookie:', 'referer:'))]
                forwarded = ['%s %s %s' % (method, url.path, version), *filtered, 'Authorization: Bearer ' + credential, '', '']
                upstream.write('\r\n'.join(forwarded).encode('ascii'))
                await upstream.drain()

                async def copy(source, target):
                    while data := await source.read(65536):
                        delay = network['delay']
                        rate = network['bytes_per_second']
                        if delay or rate:
                            await asyncio.sleep(delay + (len(data) / rate if rate else 0))
                        target.write(data)
                        await target.drain()
                pair = [asyncio.create_task(copy(reader, upstream)), asyncio.create_task(copy(remote, writer))]
                try:
                    await asyncio.wait(pair, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in pair:
                        task.cancel()
                    await asyncio.gather(*pair, return_exceptions=True)
            finally:
                writer.close()
                if upstream:
                    upstream.close()

        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        servers.append(await asyncio.start_server(tunnel, "127.0.0.1", tls_port, ssl=tls))
        serve_work = WORK / "piperd"
        # Five neighbouring ports are required by the existing isolated GUI fixture.
        serve_port = int(os.environ.get('PIPER_E2E_PORT', '18961'))
        spawn([sys.executable, str(SK / "tests" / "piper-e2e" / "serve.py"), "--port", str(serve_port),
               "--work", str(serve_work)], "piperd.log")
        deadline = time.monotonic() + 45
        while not (serve_work / "serve.json").exists():
            assert time.monotonic() < deadline, "isolated piperd did not start"
            await asyncio.sleep(.2)
        fixture = json.loads((serve_work / "serve.json").read_text())
        mobile.PORT = serve_port
        mobile.STATE = str(serve_work / "ph" / "mobile.json")
        class LocalPreset(iroh.Preset):
            def apply(self, builder):
                builder.apply_minimal()
                builder.bind_addr("127.0.0.1:0")
                builder.bind_addr("[::1]:0")
        unauthenticated = await iroh.Endpoint.bind(iroh.EndpointOptions(preset=LocalPreset(), alpns=[mobile.ALPN],
            relay_mode=iroh.RelayMode.custom_from_urls(["http://127.0.0.1:%d" % relay_port])))
        try:
            try:
                await asyncio.wait_for(unauthenticated.online(), 2)
            except asyncio.TimeoutError:
                pass
            else:
                raise AssertionError('relay admitted an endpoint without a capability')
        finally:
            await unauthenticated.close()
        # Deliberately HTTP *inside the fixture*, not an option in production code.
        endpoint = await iroh.Endpoint.bind(iroh.EndpointOptions(preset=LocalPreset(), alpns=[mobile.ALPN],
            relay_mode=iroh.RelayMode.custom_from_urls(["http://127.0.0.1:%d?token=%s" % (relay_port, relay_capability)])))
        await asyncio.wait_for(endpoint.online(), 15)
        assert relay.poll() is None
        with mobile.state_lock():
            mobile.save_state({"v": 1, "endpoint": endpoint.id().to_bytes().hex(),
                               "relay": relay_url + "?token=" + relay_capability, "name": "Studio Mac", "devices": {}})
        running_guard = mobile.runtime_lock()
        running = running_guard.__enter__()
        running.write('ready')
        running.flush()
        # Distinct fixture-only bridge token: the mobile caller must not administer pairing.
        bridge_token = 'fixture-mobile-bridge-token'
        with (serve_work / 'ph' / 'tokens').open('a') as token_file:
            token_file.write(bridge_token + '\t*\tmobile-bridge\n')

        async def accept():
            await mobile.serve_endpoint(endpoint, bridge_token)
        tasks.append(asyncio.create_task(accept()))
        import mobilepush
        tasks.append(asyncio.create_task(mobilepush.run(bridge_token)))
        spec = importlib.util.spec_from_file_location("mobile_export", SK / "scripts" / "piper-mobile-export.py")
        exporter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(exporter)
        public = WORK / "public"
        exporter.export(public, relay_url)
        boot = public / 'piper/dist/mobileboot.js'
        observation = """const NativeWorker=Worker;
window.fixtureWorkers={started:0,live:0,maximum:0};
globalThis.Worker=class extends NativeWorker{
 constructor(...args){super(...args);this.fixtureTerminated=false;const s=window.fixtureWorkers;s.started++;s.live++;s.maximum=Math.max(s.maximum,s.live)}
 terminate(){if(!this.fixtureTerminated){this.fixtureTerminated=true;window.fixtureWorkers.live--}return super.terminate()}
};
"""
        boot.write_text(boot.read_text().replace('void boot();', observation+'void boot();')+'\nexport {connect as fixtureConnect};\n')
        if os.environ.get('PIPER_E2E_MOBILE_TRACE') == '1':
            # Test-only observation of caught bootstrap errors, without logging keys/codes/bodies.
            boot = public / 'piper' / 'dist' / 'mobileboot.js'
            boot.write_text(boot.read_text().replace('catch {', "catch (error) { (window.mobileTestErrors ??= []).push({name:error?.name, message:String(error?.message ?? '').replace(/[A-Za-z0-9_+\\/-]{32,}/g,'[redacted]').slice(0,240)});"))
        class Static(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass  # the pairing fragment and keys must never be logged
            def end_headers(self):
                for line in (public / "_headers").read_text().splitlines()[1:]:
                    name, value = line.strip().split(": ", 1)
                    self.send_header(name, value)
                super().end_headers()
        class StaticServer(http.server.ThreadingHTTPServer):
            # The GUI imports a whole module graph. Match piperd's backlog rather
            # than socketserver's default five, which can drop parallel asset loads.
            request_queue_size = 128
        web = StaticServer(("127.0.0.1", static_port), functools.partial(Static, directory=str(public)))
        web.socket = tls.wrap_socket(web.socket, server_side=True)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        origin = "https://127.0.0.1:%d" % static_port
        with mobile.state_lock():
            state = mobile.load_state(); state['client'] = origin; mobile.save_state(state)
        # Exercise desktop Settings through the real local GUI, without printing private links.
        await ab('open', fixture['url'])
        await ab('set', 'viewport', '1200', '850')
        await until("!!document.querySelector('#queue [data-sid]')")
        await ab('find', 'role', 'button', 'click', '--name', 'Settings', '--exact')
        await until("!!document.querySelector('[data-nav=\"mobile\"]')")
        await js("document.querySelector('[data-nav=\"mobile\"]').click()")
        await until("document.querySelector('.mobile-settings')?.textContent.includes('No phones yet')")
        await ab('screenshot', str(OUT / 'desktop-empty.png'))
        await ab('find', 'role', 'button', 'click', '--name', 'Pair phone', '--exact')
        await ab('screenshot', str(OUT / 'desktop-name.png'))
        assert await js("(()=>{const qr=document.querySelector('.mobile-install .mobile-qr').getBoundingClientRect(), svg=document.querySelector('.mobile-install .mobile-qr svg').getBoundingClientRect();return svg.left>=qr.left && svg.top>=qr.top && svg.right<=qr.right && svg.bottom<=qr.bottom})()"), 'installation QR is clipped and cannot be scanned'
        assert mobile.desktop_status()['pending'] is None, 'installation QR created a timed invitation'
        assert not mobile.load_state()['devices'], 'installation granted access'
        assert await js("document.querySelector('.mobile-qr svg').getAttribute('aria-label') === 'Installation QR code'")
        await ab('find', 'role', 'button', 'click', '--name', 'Use in a browser only', '--exact')
        await until("!!document.querySelector('.mobile-qr svg')")
        await ab('screenshot', str(OUT / 'desktop-qr.png'))
        for width, height in ((390, 844), (320, 568)):
            await ab('set', 'viewport', str(width), str(height))
            assert await js("(()=>{const r=document.querySelector('.mobile-qr').getBoundingClientRect();return r.left>=0 && r.right<=innerWidth && document.documentElement.scrollWidth<=innerWidth})()"), 'narrow desktop pairing overflows'
            await ab('screenshot', str(OUT / ('desktop-qr-' + str(width) + '.png')))
        await ab('set', 'viewport', '1200', '850')
        assert mobile.desktop_status()['pending'], 'GUI did not issue invitation'
        await ab('press', 'Escape')
        deadline = time.monotonic() + 5
        while mobile.desktop_status()['pending']:
            assert time.monotonic() < deadline, 'Escape did not cancel invitation'
            await asyncio.sleep(.1)
        # Create again, expire server-side, and renew without returning to a command.
        await ab('find', 'role', 'button', 'click', '--name', 'Pair phone', '--exact')
        await ab('find', 'role', 'button', 'click', '--name', 'Use in a browser only', '--exact')
        await until("!!document.querySelector('.mobile-qr svg')")
        with mobile.state_lock():
            state = mobile.load_state(); state['pending']['expires'] = time.time() - 1; mobile.save_state(state)
        await until("document.querySelector('.mobile-qr')?.classList.contains('expired')")
        await ab('screenshot', str(OUT / 'desktop-expired.png'))
        await ab('find', 'role', 'button', 'click', '--name', 'Generate new code', '--exact')
        await until("!!document.querySelector('.mobile-qr svg')")
        # Copy through a test-only clipboard stub; the private link never enters stdout.
        await js("Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{writeText:async value=>{window.fixturePairingLink=value}}})")
        await ab('find', 'role', 'button', 'click', '--name', 'Copy pairing link', '--exact')
        gui_link = await js('window.fixturePairingLink')
        assert gui_link and '#iroh=' in gui_link
        desktop_session = session
        session = desktop_session + '-m'
        await ab("open", origin)
        print('Browser opened exported client', flush=True)
        await ab("set", "viewport", "390", "844")
        await until("document.querySelector('.mobile-stage')?.dataset.state === 'install'")
        assert await js("!localStorage.getItem('piper.mobile.profile')"), 'install-only page created a profile'
        # The centered card moves during viewport/font settling. Wait for those
        # real layout updates before a coordinate-based native browser click.
        await js("(async()=>{await document.fonts.ready;await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));window.fixtureInstallClicks=[];document.addEventListener('click',e=>{if(document.querySelector('.mobile-stage')?.dataset.state==='install')fixtureInstallClicks.push({tag:e.target.tagName,button:e.target.closest('button')?.textContent})},true);return true})()")
        await ab('find','role','button','click','--name','Use Piper in this browser instead','--exact')
        await until("document.querySelector('.mobile-stage')?.dataset.state === 'unpaired'")
        audit = await ab('a11y', '--json')
        assert audit['counts']['violations'] == 0, 'unpaired accessibility violations'
        (WORK / 'a11y.json').write_text(json.dumps(audit))
        await ab("screenshot", str(OUT / "unpaired.png"))

        await js("(()=>{Object.defineProperty(navigator,'platform',{configurable:true,value:'MacIntel'});Object.defineProperty(navigator,'maxTouchPoints',{configurable:true,value:5});window.fixtureMatchMedia=window.matchMedia.bind(window);window.matchMedia=q=>q==='(display-mode: standalone)'?{matches:false}:window.fixtureMatchMedia(q);return true})()")
        await js("window.mobileUI = await import('/piper/dist/mobileui.js')")
        await js("window.mobileUI.showMobileState('install', 'Piper', {browser:()=>window.mobileUI.showMobileState('unpaired')})")
        for width, height in ((390, 844), (320, 568)):
            await ab('set', 'viewport', str(width), str(height))
            assert await js("document.documentElement.scrollWidth<=innerWidth"), 'install state overflows'
            await ab('screenshot', str(OUT / ('install-browser-' + str(width) + '.png')))
        assert await js("!localStorage.getItem('piper.mobile.profile')"), 'installation created an identity'
        assert not mobile.load_state()['devices'], 'installation paired a browser'
        await ab('find','role','button','click','--name','Use Piper in this browser instead','--exact')
        assert await js("document.querySelector('.mobile-stage').textContent.includes('Pair this browser')")
        await js("(()=>{window.matchMedia=q=>q==='(display-mode: standalone)'?{matches:true}:window.fixtureMatchMedia(q);window.mobileUI.showMobileState('unpaired');return true})()")
        for width, height in ((390, 844), (320, 568)):
            await ab('set', 'viewport', str(width), str(height))
            await ab('screenshot', str(OUT / ('unpaired-app-' + str(width) + '.png')))
        assert await js("document.querySelector('.mobile-stage').textContent.includes('You pair this app once')")

        # Capture every authored pairing state with the real compiled renderer, dark/light and two widths.
        await js("window.mobileUI = await import('/piper/dist/mobileui.js')")
        await js("window.mobileTheme = await import('/piper/dist/theme.js')")
        for theme in (() if os.environ.get('PIPER_E2E_MOBILE_QUICK') == '1' else ("dark", "light")):
            await js("window.mobileTheme.applyChoice(window.__PIPER__.themes, " + json.dumps(theme) + ")")
            for width, height in ((390, 844), (320, 568)):
                await ab("set", "viewport", str(width), str(height))
                for state in ("pair", "connecting", "pairing", "expired", "revoked", "identity", "offline"):
                    await js("window.mobileUI.showMobileState(" + json.dumps(state) + ", 'Studio Mac')")
                    assert await js("document.documentElement.scrollWidth <= innerWidth"), "horizontal overflow"
                    assert await js("[...document.querySelectorAll('.mobile-stage button')].every(e => e.getBoundingClientRect().height >= 44)"), "small touch target"
                    await ab("screenshot", str(OUT / ("%s-%s-%d.png" % (state, theme, width))))
                    if state == 'pair' and width == 390:
                        audit = await ab('a11y', '--json')
                        assert audit['counts']['violations'] == 0, 'pairing accessibility violations'
                        (OUT / ('a11y-pair-' + theme + '.json')).write_text(json.dumps(audit))
        await js("window.mobileUI.showMobileState('offline', 'Studio Mac', {}, false, false)")
        assert await js("!document.querySelector('.mobile-stage').textContent.includes('This phone stays paired')"), 'unconfirmed pairing reported as established'
        await ab("set", "viewport", "390", "844")
        await js("window.mobileUI.clearMobileState()")
        print('Pairing states captured', flush=True)

        # Open the real invitation without exposing its fragment in test output.
        phone_session = session
        session = desktop_session
        await ab('press', 'Escape')
        await ab('find', 'role', 'button', 'click', '--name', 'Pair phone', '--exact')
        await ab('find', 'role', 'button', 'click', '--name', 'Next: pair the app', '--exact')
        await until("!!document.querySelector('.mobile-pair-app')")
        first_app_invite = mobile.desktop_status()['pending']['id']
        await ab('find','role','button','click','--name','Back','--exact')
        await until("!!document.querySelector('.mobile-install')")
        assert mobile.desktop_status()['pending'] is None, 'Back left a pairing link valid'
        assert await js("document.getElementById('mobile-label').value === 'iPhone'"), 'Back lost the phone name'
        await ab('find','role','button','click','--name','Next: pair the app','--exact')
        await until("!!document.querySelector('.mobile-pair-app')")
        assert mobile.desktop_status()['pending']['id'] != first_app_invite
        with mobile.state_lock():
            state = mobile.load_state(); state['pending']['expires'] = time.time() - 1; mobile.save_state(state)
        await until("document.querySelector('.mobile-pair-app h2')?.textContent === 'Pairing link expired'")
        assert await js("!document.querySelector('.mobile-pair-app .mobile-qr')")
        await ab('screenshot', str(OUT / 'desktop-app-expired.png'))
        await ab('find','role','button','click','--name','Generate new link','--exact')
        await until("document.querySelector('.mobile-pair-app h2')?.textContent === 'Pair Piper on iPhone'")
        assert await js("!document.querySelector('.mobile-pair-app .mobile-qr')"), 'app pairing sent the user back to the Camera/browser'
        await until("document.querySelector('.mobile-wait')?.textContent.includes('expires in')")
        await ab('screenshot', str(OUT / 'desktop-pair-app.png'))
        await ab('find', 'role', 'button', 'click', '--name', 'Copy pairing link', '--exact')
        link = await js('window.fixturePairingLink')
        session = phone_session
        await ab("reload")
        await until("document.querySelector('.mobile-stage')?.dataset.state === 'unpaired'")
        await js("(()=>{const input=document.getElementById('mobile-link');input.value=" + json.dumps(link) + ";input.dispatchEvent(new Event('input'));return true})()")
        await ab('find', 'role', 'button', 'click', '--name', 'Continue', '--exact')
        await until("document.querySelector('.mobile-stage')?.dataset.state === 'pair'")
        assert not mobile.load_state()["devices"], "consent was bypassed"
        assert await js("location.hash === ''"), "pairing secret remained in history"
        await ab("snapshot", "-i")
        await js("(async()=>{const {TermView}=await import('/piper/dist/term.js');const original=TermView.prototype.setInputState;TermView.prototype.setInputState=function(...args){if(args[1])window.fixtureActiveTerm=this;return original.apply(this,args)};return true})()")
        await ab("find", "role", "button", "click", "--name", "Pair with full access", "--exact")
        await until("!document.querySelector('.mobile-stage') && !!document.querySelector('#queue [data-sid]')")
        await terminal_input(js, until, OUT, network)
        await terminal_key_latency(js, until, OUT, network)
        assert len(mobile.load_state()["devices"]) == 1
        await ab("screenshot", str(OUT / "paired-real-gui.png"))
        if os.environ.get('PIPER_E2E_WEBPUSH') == '1':
            from push_browser import verify
            await verify(js, until, ab, OUT, origin, fixture, WORK)
            # The closed-page push check reopens the document; its old prototype/global probe is gone.
            await js("(async()=>{const {TermView}=await import('/piper/dist/term.js');const original=TermView.prototype.setInputState;TermView.prototype.setInputState=function(...args){if(args[1])window.fixtureActiveTerm=this;return original.apply(this,args)};return true})()")
        # One missing/ended terminal is a stream error, not a failure of the phone connection.
        workers_before_stream = await js('window.fixtureWorkers.started')
        await js("""(async()=>{
          const app=document.getElementById('app');window.fixtureWentOffline=false;
          window.fixtureOfflineObserver=new MutationObserver(()=>{if(app.inert)window.fixtureWentOffline=true});
          fixtureOfflineObserver.observe(app,{attributes:true,attributeFilter:['inert']});
          const {terminalSocket,transportFetch}=await import('/piper/dist/transport.js');
          await new Promise((resolve,reject)=>{
            const ws=terminalSocket('ws://unused/ws/fixture-missing-session');
            const timer=setTimeout(()=>{ws.close();reject(Error('missing terminal did not close'))},5000);
            ws.onerror=()=>{};ws.onclose=()=>{clearTimeout(timer);resolve(true)};
          });
          await new Promise(resolve=>requestAnimationFrame(resolve));
          if(window.fixtureWentOffline || !(await transportFetch('/api/ping',{})).ok)throw Error('one terminal reset ended shared connection');
          fixtureOfflineObserver.disconnect();return true;
        })()""")
        assert await js('window.fixtureWorkers.started') == workers_before_stream, 'one terminal forced a full reconnect'
        await js("""(async()=>{
          const {transportFetch}=await import('/piper/dist/transport.js');
          const state=await(await transportFetch('/api/state',{})).json();
          const ids=state.sessions.filter(s=>['ready','running'].includes(s.state)).slice(0,2).map(s=>s.id);
          if(ids.length<2)throw Error('fixture needs two active sessions');
          for(const id of [...ids,...ids]){
            const tile=document.querySelector('.qm[data-sid="'+CSS.escape(id)+'"]');
            if(!tile)throw Error('session tile missing');tile.click();
            await new Promise(resolve=>setTimeout(resolve,100));
            if(document.getElementById('app').inert || !(await transportFetch('/api/ping',{})).ok)throw Error('session switch disconnected phone');
          }return true;
        })()""")
        await ab('screenshot', str(OUT / 'session-switch-real-gui.png'))
        print('PASS: isolated terminal reset and repeated session switches preserve the shared connection', flush=True)
        # The session HTTP tab must use the installed transport for its entire load.
        await js("""(async()=>{
          const {transportFetch}=await import('/piper/dist/transport.js');
          const state=await(await transportFetch('/api/state',{})).json();
          const s=state.sessions.find(s=>s.project==='grp/demo');
          if(!s)throw Error('HTTP fixture session missing');
          document.querySelector('.qm[data-sid="'+CSS.escape(s.id)+'"]').click();return true;
        })()""")
        await until("[...document.querySelectorAll('button')].some(e=>e.textContent==='HTTP' && e.getClientRects().length)")
        await js("[...document.querySelectorAll('button')].find(e=>e.textContent==='HTTP' && e.getClientRects().length).click()")
        await until("!!document.querySelector('.hpfile')")
        await js("document.querySelector('.hpfile').click()")
        await until("!!document.querySelector('.httpenv') && document.querySelector('.hpright').textContent.includes('GET')")
        assert await js("!document.getElementById('app').inert && document.querySelector('.httpenv').options.length>=3"), 'HTTP tab did not load environments'
        await ab('screenshot', str(OUT / 'http-real-gui.png'))
        print('PASS: session HTTP tab, file requests and environments load through relay', flush=True)
        await cancelled_streams(js, ab, OUT, operations)
        await slow_requests(js, until, ab, OUT, faults, operations)
        await large_question(js, until, ab, OUT, network, faults)
        if int(os.environ.get('PIPER_E2E_STRESS_SECONDS', '0')):
            await stress(js, until, ab, OUT, operations, network)
        elif os.environ.get('PIPER_E2E_RECOVERY_ONLY') == '1':
            recovery = await recover_network(js, until, ab, operations)
            (OUT / 'gui-recovery-metrics.json').write_text(json.dumps(recovery, indent=2) + '\n')
            await ab('screenshot', str(OUT / 'network-recovered-real-gui.png'))
        # Select a visible terminal: retained views intentionally have no mobile data stream.
        await js("[...document.querySelectorAll('button')].find(e=>e.textContent==='Terminal' && e.getClientRects().length)?.click()")
        await until("window.fixtureActiveTerm?.ws?.readyState===1 && !window.fixtureActiveTerm.replaying")
        # Background + actual browser network loss must recover without reload or a new pairing.
        await js(r"""(async()=>{
          const view=window.fixtureActiveTerm;await new Promise(r=>view.term.write('',r));
          if(!view.checkpoint||view.checkpoint.offset===null)throw Error('fixture host checkpoint unavailable');
          const observe=window.fixtureWake={offset:view.checkpoint.offset,bytes:0,resets:0,hellos:[]};
          const connect=view.connectNow.bind(view),reset=view.term.reset.bind(view.term);
          view.term.reset=()=>{observe.resets++;reset()};
          view.connectNow=()=>{connect();const ws=view.ws;if(!ws)return;const message=ws.onmessage;ws.onmessage=e=>{
            if(e.data instanceof ArrayBuffer)observe.bytes+=e.data.byteLength;
            else{const m=JSON.parse(e.data);if(m.t==='hello')observe.hellos.push({resumed:m.resumed,checkpoint:m.checkpoint})}
            message(e);
          }};
          window.fixtureEndWake=()=>{view.connectNow=connect;view.term.reset=reset};return true;
        })()""")
        pairs_before = operations.count('pair')
        hello_before = operations.count('hello')
        await js("(()=>{window.fixtureGuiMarker='still-running';window.fixturePairedProfile=localStorage.getItem('piper.mobile.profile');return true})()")
        await js("(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));return true})()")
        await ab('set', 'offline', 'on')
        await ab('set', 'offline', 'off')
        await js("(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'));return true})()")
        await until("!document.getElementById('app').inert && !document.getElementById('mobile-offline') && !document.getElementById('banner')")
        assert await js("(await (await import('/piper/dist/transport.js')).transportFetch('/api/ping',{})).ok"), 'HTTP after screen return'
        assert await js("window.fixtureGuiMarker==='still-running' && window.fixturePairedProfile===localStorage.getItem('piper.mobile.profile')"), 'resume reloaded GUI or replaced pairing'
        assert operations.count('hello') > hello_before, 'resume did not establish a new authenticated connection'
        assert operations.count('pair') == pairs_before, 'resume repeated pairing'
        await until("window.fixtureActiveTerm?.ws?.readyState===1 && !window.fixtureActiveTerm.replaying")
        wake = await js('window.fixtureWake')
        assert wake['resets'] == 0 and wake['hellos'] == [{'resumed': True, 'checkpoint': True}], wake
        assert wake['bytes'] < 65536, 'wake retransferred retained fixture terminal history'
        (OUT / 'terminal-wake-metrics.json').write_text(json.dumps(wake, indent=2))
        await js('(()=>{window.fixtureEndWake();return true})()')
        await terminal_key_latency(js, until, OUT, network)
        assert await js('window.fixtureWorkers.maximum===1 && window.fixtureWorkers.live===1'), 'resume leaked competing WASM workers'
        await ab('screenshot', str(OUT / 'resumed-real-gui.png'))
        print('PASS: background/network return renewed authenticated connection without reload or pairing', flush=True)
        # Lose an HTTP response after opening a real stream, without an online/visibility event.
        hello_before = operations.count('hello')
        await js("""(async()=>{
          const {transportFetch}=await import('/piper/dist/transport.js');
          const post=Worker.prototype.postMessage,marked=new WeakMap();let failed=0;
          Worker.prototype.postMessage=function(value,...args){
            if(value.op==='write')try{
              const bytes=value.data,n=new DataView(bytes.buffer,bytes.byteOffset,bytes.byteLength).getUint32(0);
              const meta=JSON.parse(new TextDecoder().decode(bytes.slice(4,4+n)));
              if(meta.path==='/api/ping?fixture-read-loss'){let set=marked.get(this);if(!set)marked.set(this,set=new Set());set.add(value.stream)}
            }catch{}
            if(value.op==='read_to_end'&&marked.get(this)?.delete(value.stream)){
              const handler=this.onmessage;
              this.onmessage=event=>{if(event.data.id===value.id){this.onmessage=handler;failed++;handler.call(this,{data:{id:value.id,ok:false}})}else handler.call(this,event)};
            }
            return post.call(this,value,...args);
          };
          try{
            await transportFetch('/api/ping?fixture-read-loss',{});
            throw Error('fixture request unexpectedly succeeded');
          }catch(error){if(!failed||error.message!=='Mobile transport operation failed')throw error}
          finally{Worker.prototype.postMessage=post}
          if(failed!==1)throw Error('failed request was replayed');
          return true;
        })()""")
        await until("!document.getElementById('app').inert && !document.getElementById('mobile-offline') && !document.getElementById('banner')")
        assert await js("(await (await import('/piper/dist/transport.js')).transportFetch('/api/ping',{})).ok")
        assert operations.count('hello') > hello_before, 'HTTP read failure reused the stale connection'
        assert operations.count('pair') == pairs_before, 'HTTP recovery repeated pairing'
        assert await js("window.fixtureGuiMarker==='still-running' && window.fixturePairedProfile===localStorage.getItem('piper.mobile.profile')")
        await ab('screenshot', str(OUT / 'http-read-recovered-real-gui.png'))
        print('PASS: HTTP response loss renews the connection without reload, pairing or request replay', flush=True)
        await js("document.querySelector('.mobile-paired-toast')?.remove()")
        for width, height in ((390, 844), (320, 568)):
            await ab('set', 'viewport', str(width), str(height))
            await until("[...document.querySelectorAll('.qm-lim,.ql-provider')].some(e=>!e.hidden&&e.getClientRects().length)")
            await js("(()=>{window.fixtureLimitsButton=[...document.querySelectorAll('.qm-lim,.ql-provider')].find(e=>!e.hidden&&e.getClientRects().length);fixtureLimitsButton.dispatchEvent(new PointerEvent('pointerdown',{pointerType:'touch',bubbles:true}));fixtureLimitsButton.focus();fixtureLimitsButton.click();return true})()")
            await ab('screenshot', str(OUT / ('limits-phone-' + str(width) + '.png')))
            assert await js("(()=>{const p=document.querySelector('.qm-peek');if(!p||p.hidden)return false;const r=p.getBoundingClientRect(),a=fixtureLimitsButton.getBoundingClientRect();return a.top>=0&&a.bottom<=innerHeight&&r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight&&p.textContent.includes('limits')})()"), 'mobile limits detail not visible within viewport: ' + str(width)
            await ab('press', 'Escape')
        await ab('set', 'viewport', '390', '844')
        print('PASS: mobile limits detail is visible within viewport', flush=True)
        assert await js("!JSON.stringify(localStorage).includes(" + json.dumps(fixture["gui_token"]) + ")"), "bearer reached phone"
        # Exercise real terminal framing, including loopback authentication and binary output.
        result = await js("""(async () => {
          const {transportFetch, terminalSocket} = await import('/piper/dist/transport.js');
          const state = await (await transportFetch('/api/state', {})).json();
          const sid = state.sessions.find(s => s.state === 'ready')?.id || state.sessions[0].id;
          return await new Promise((resolve, reject) => {
            const ws = terminalSocket('ws://unused/ws/' + sid);
            const timer = setTimeout(() => { ws.close(); reject(Error('no terminal output')); }, 8000);
            ws.onopen = () => ws.send(JSON.stringify({auth:'must-not-cross-relay'}));
            ws.onmessage = e => { if (e.data instanceof ArrayBuffer && e.data.byteLength) { clearTimeout(timer); ws.close(); resolve(true); } };
            ws.onerror = () => { clearTimeout(timer); reject(Error('terminal refused')); };
          });
        })()""")
        assert result, "terminal did not replay"
        peer = next(iter(mobile.load_state()["devices"]))
        previous_grant = mobile.grant(peer)
        # A paired phone cannot use local administration even with full Piper access.
        assert await js("(async()=>{const {transportFetch}=await import('/piper/dist/transport.js');return (await transportFetch('/api/mobile',{})).status})()") == 403
        phone_session = session
        session = desktop_session
        await until("document.querySelector('#overlay')?.textContent.includes('iPhone paired')")
        await ab('screenshot', str(OUT / 'desktop-paired.png'))
        await ab('find', 'role', 'button', 'click', '--name', 'Done', '--exact')
        await ab('find', 'role', 'button', 'click', '--name', 'Remove iPhone', '--exact')
        await ab('screenshot', str(OUT / 'desktop-remove.png'))
        await ab('find', 'role', 'button', 'click', '--name', 'Remove phone', '--exact')
        await until("document.querySelector('.mobile-settings')?.textContent.includes('No phones yet')")
        await ab('close')
        session = phone_session
        await until("document.querySelector('.mobile-stage')?.dataset.state === 'revoked'", 15)
        assert await js("document.getElementById('app').inert"), "revoked GUI accepts input"
        assert await js("!document.querySelector('.mobile-paired-toast')"), 'pairing toast survives revocation'
        assert await js("(() => { const r = document.querySelector('.mobile-revoked').getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; })()"), 'revocation panel overflows viewport'
        await ab("screenshot", str(OUT / "revoked-real-gui.png"))
        # Re-pair the same phone with a new invitation, preserving the existing browser profile.
        fragment = mobile.invitation("https://mobile.example").split("#", 1)[1]
        await js("location.href = " + json.dumps(origin + "/#" + fragment))
        await ab("reload")
        await until("document.querySelector('.mobile-stage')?.dataset.state === 'pair'")
        assert not mobile.load_state()["devices"], 'new link silently bypassed consent after revoke'
        await ab('screenshot', str(OUT / 'repair-consent-real-gui.png'))
        await ab("snapshot", "-i")
        await ab("find", "role", "button", "click", "--name", "Pair with full access", "--exact")
        await until("!document.querySelector('.mobile-stage') && !!document.querySelector('#queue [data-sid]')")
        assert list(mobile.load_state()["devices"]) == [peer], 're-pairing replaced the phone identity'
        assert mobile.grant(peer) != previous_grant, 'old grant was reused'
        await ab('screenshot', str(OUT / 'repaired-real-gui.png'))
        print("PASS: WASM/CSP, explicit pairing, real relay HTTP + binary terminal, token isolation, live revocation and re-pairing without clearing storage; 390/320 dark/light captures", flush=True)
        print("Artifacts: " + str(OUT), flush=True)
        if os.environ.get('PIPER_E2E_MOBILE_TRACE') == '1':
            print('Bootstrap diagnostics: ' + str(await js('window.mobileTestErrors ?? []')), flush=True)
        if os.environ.get('PIPER_E2E_ARTIFACTS') == '1':
            # The sandbox's parent may archive public fixture screenshots without granting tests
            # write access to the checkout or any directory outside their disposable work root.
            import io
            import zipfile
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                for path in OUT.iterdir():
                    archive.write(path, path.name)
                archive.write(WORK / 'a11y.json', 'a11y.json')
            print('ARTIFACTS ' + base64.b64encode(buffer.getvalue()).decode(), flush=True)
    finally:
        mobile.read_header = original_read_header
        for opened in list(browser_sessions):
            session = opened
            try:
                await ab("close")
            except Exception:
                pass
        if web:
            web.shutdown()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if endpoint:
            await endpoint.close()
        if running_guard:
            running_guard.__exit__(None, None, None)
        for server in servers:
            server.close()
            await server.wait_closed()
        for process in reversed(processes):
            process.terminate()
        for process in reversed(processes):
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    asyncio.run(main())
