"""Lost acknowledgements after real durable writes, with an isolated headless browser."""
import base64
import contextlib
import http.server
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests/piper-unit'))
import _env
from test_dbtools import DbManagerTest
import dbtools

DbManagerTest.setUpClass()
case = DbManagerTest('test_grid_operation_replay_and_unknown_retry_never_duplicate')
case.setUp()
manager, key = case.m, case.key
connection = key + '/local'
store = manager._db_store()
store['connections'] = [dict(c, engine='postgres') for c in store['connections'] if c['name'] == connection]
manager._db_save(store)
dbtools.write_secret(connection, 'fixture-only')
with contextlib.closing(sqlite3.connect(case.dbs['dev'])) as db:
    db.execute('CREATE TABLE ack_item(id INTEGER PRIMARY KEY,label TEXT)')
driver = Path(_env.ROOT) / 'ack-driver.py'
driver.write_text("import sys\nsys.path.insert(0," + repr(str(ROOT / 'tests/piper-unit')) + ")\nfrom dbfake_sqlite import SQLite\nDRIVERS={'postgres':SQLite}\n")
os.environ['PIPER_DB_TEST_DRIVERS'] = str(driver)
HTML = '''<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/piper/app.css">
<style>html,body,#fixture{height:100%;margin:0}#fixture{display:flex}#toast{position:fixed;bottom:0;left:0;z-index:99;background:var(--panel);padding:10px}</style>
<div id="fixture"></div><div id="toast" role="status"></div><script>window.startupErrors=[];window.addEventListener("error",e=>startupErrors.push(e.message||e.target?.src));</script><script type="module">
import {createDbView,dbApi} from '/piper/dist/dbview.js';
window.errors=[];window.requests=[];window.lost=[];window.drop='';window.closedCommit=false;
window.addEventListener('error',e=>errors.push(e.message+' '+(e.error?.stack??'')));
const request=async(name,args)=>{
  requests.push({name,args});
  const r=await fetch('/fixture/'+name,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(args)});
  const data=await r.json();
  if(!r.ok)throw new Error(data.error);
  if(data.committed===true && (drop==='grid' && name==='execute' || drop==='commit' && name==='tx' && args[0].op==='commit')) {
    lost.push({name,args,status:r.status,data});drop='';throw new TypeError('Synthetic lost acknowledgement after completed HTTP 200');
  }
  return {data};
};
for(const name of ['all','meta','connections','history'])dbApi[name]=(...args)=>request(name,args);
for(const name of ['query','execute','tx'])dbApi[name]=b=>request(name,[b]);
dbApi.running=async()=>({data:{running:[]}});
window.view=createDbView({toast:t=>document.querySelector('#toast').textContent=t,modal:()=>{},closeModal:()=>{},projects:()=>[{key:'__KEY__',title:'Synthetic acknowledgements'}],pending:()=>[]});
view.el.style.cssText='width:100%;height:100%;min-width:0';document.querySelector('#fixture').append(view.el);view.show();window.addEventListener('keydown',e=>view.onKey(e));window.ready=true;
</script>'''.replace('__KEY__', key)

http_events = []

class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)
    def log_message(self, *args):
        http_events.append(str(args))
    def do_GET(self):
        if self.path == '/':
            self.send_response(200); self.send_header('content-type', 'text/html'); self.send_header('content-length', str(len(HTML.encode()))); self.end_headers(); self.wfile.write(HTML.encode())
        elif self.path.startswith('/piper/'):
            self.path = '/piper/web/' + self.path[7:]; super().do_GET()
        else:
            self.send_error(404)
    def do_POST(self):
        try:
            args = json.loads(self.rfile.read(int(self.headers['content-length'])))
            name = self.path.rsplit('/', 1)[1]
            if name in ('query', 'execute', 'tx'):
                kwargs = dict(args[0]); kwargs['key'] = kwargs.pop('project')
                result = getattr(manager, 'db_gui_' + name)(**kwargs)
            elif name == 'all':
                result = manager.db_gui_all(); result['status']['ready'] = False
            else:
                result = getattr(manager, 'db_gui_' + name)(*args)
            self.send_response(200)
        except Exception as error:
            self.send_response(409); result = {'error': str(error)}
        payload = json.dumps(result).encode()
        self.send_header('content-type', 'application/json'); self.send_header('content-length', str(len(payload))); self.end_headers(); self.wfile.write(payload)

class FixtureServer(http.server.ThreadingHTTPServer):
    request_queue_size = 128

server = FixtureServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
env = dict(os.environ, AGENT_BROWSER_PROFILE=str(Path(os.environ['TEST_SANDBOX_WORK']) / 'db-ack-profile'))
SESSION = 'audit-db-ack-' + str(os.getpid())
def ab(*args):
    result = subprocess.run(['agent-browser', '--session', SESSION, '--json', *args], capture_output=True, text=True, env=env, timeout=45)
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    data = json.loads(result.stdout)
    if not data.get('success'):
        raise RuntimeError(str(data))
    return data.get('data', {})
def js(code):
    return ab('eval', code).get('result')
def wait(code):
    ab('wait', '--fn', code)
def button(name):
    if name == 'Commit': ab('click', '.db-tx button.primary')
    else: ab('find', 'role', 'button', 'click', '--name', name, '--exact')
def check(value, label):
    assert value, label
    checks.append(label); print('CHECK ' + label, flush=True)
def screen(name):
    # The fixture's persistent status node stands in for the app's transient toast.
    # Confirmed / closed-result captures show the new result after that toast expires.
    if 'confirmed' in name or 'closed' in name:
        js('document.querySelector("#toast").textContent=""')
    path = Path(os.environ['TEST_SANDBOX_WORK']) / name
    ab('screenshot', str(path)); screens[name] = base64.b64encode(path.read_bytes()).decode()
def count(label):
    with contextlib.closing(sqlite3.connect(case.dbs['dev'])) as db:
        return db.execute('SELECT count(*) FROM ack_item WHERE label=?', (label,)).fetchone()[0]
def row(label):
    button('+ Row')
    cell = '.db-gc[data-r="-1"][data-c="1"]'
    ab('click', cell); ab('focus', '.db-grid'); ab('press', 'F2'); wait('!!document.querySelector(".db-gc.editing input,.db-gc.editing textarea")')
    ab('fill', '.db-gc.editing input,.db-gc.editing textarea', label); ab('press', 'Enter')
    wait('!![...document.querySelectorAll("button")].find(b=>b.textContent.includes("Submit"))')
def submit():
    ab('click', '.db-pending button.primary')
def run(sql):
    before = js('requests.filter(r=>r.name==="tx"&&r.args[0].op==="exec").length')
    ab('fill', 'textarea[aria-label=SQL]', sql)
    js('(()=>{const t=document.querySelector("textarea[aria-label=SQL]");t.focus();t.setSelectionRange(t.value.length,t.value.length);t.dispatchEvent(new Event("keyup"));})()')
    ab('press', 'Control+Enter')
    wait('requests.filter(r=>r.name==="tx"&&r.args[0].op==="exec").length>' + str(before) + '&&!document.querySelector(".db-bar button[aria-label=cancel]")')

checks, screens, error = [], {}, None
try:
    ab('open', f'http://127.0.0.1:{server.server_port}/'); ab('set', 'viewport', '1440', '1000')
    wait('window.ready&&!!document.querySelector(".db-cname2")'); ab('click', '.db-cname2')
    wait('document.querySelector(".db-tree")?.innerText.includes("ack_item")')
    js('view.openReference(' + json.dumps(key) + ',' + json.dumps(connection) + ',"main","ack_item")')
    wait('!![...document.querySelectorAll("button")].find(b=>b.textContent==="+ Row")')
    row('grid-lost'); js('window.drop="grid"'); submit()
    wait('document.querySelector("#toast").textContent.includes("Save outcome unknown")')
    check(count('grid-lost') == 1, 'lost grid HTTP200 acknowledgement follows one durable generated row')
    check(js('lost[0].status===200&&lost[0].data.committed===true'), 'seam drops only a finished successful execute response')
    check('Not saved' not in js('document.querySelector("#toast").textContent'), 'unknown save does not falsely claim Not saved')
    screen('db-ack-grid-unknown.png')
    submit(); wait('!document.querySelector(".db-pending")')
    check(count('grid-lost') == 1, 'explicit Submit retry retrieves receipt without duplicate row')
    check(js('(()=>{const a=requests.filter(r=>r.name==="execute");return a[0].args[0].operation_id===a[1].args[0].operation_id&&a[1].args[0].retry===true})()'), 'grid retry keeps exact operation id and marks explicit retry')
    row('grid-lost'); submit(); wait('!document.querySelector(".db-pending")')
    check(count('grid-lost') == 2, 'new intentional identical grid INSERT creates a second row')
    check(js('requests.filter(r=>r.name==="execute").at(-1).args[0].operation_id!==requests.filter(r=>r.name==="execute")[0].args[0].operation_id'), 'new intentional grid operation has a new id')
    screen('db-ack-grid-confirmed.png')
    ab('press', 'Control+Shift+l'); wait('!!document.querySelector("textarea[aria-label=SQL]")'); button('Read-write')
    run("INSERT INTO ack_item(label) VALUES('tx-lost')"); wait('!!document.querySelector(".db-tx")')
    js('window.drop="commit"'); button('Commit')
    wait('document.querySelector("#toast").textContent.includes("Transaction outcome unknown")')
    check(count('tx-lost') == 1, 'lost console Commit acknowledgement follows one durable row')
    check(js('lost[1].status===200&&lost[1].data.committed===true'), 'seam drops only a finished successful Commit response')
    check('outcome unknown' in js('document.querySelector(".db-status").innerText'), 'unknown Commit status bar does not claim an open transaction')
    check('not committed' not in js('document.querySelector(".db-tx").innerText'), 'unknown transaction banner never falsely claims not committed')
    check('Nothing is written until' not in js('document.querySelector(".db-tx").innerText'), 'unknown banner does not claim the write is still pending')
    screen('db-ack-commit-unknown.png')
    before = js('requests.filter(r=>r.name==="tx"&&r.args[0].op==="exec").length')
    ab('press', 'Control+Enter')
    check(js('requests.filter(r=>r.name==="tx"&&r.args[0].op==="exec").length') == before, 'unknown Commit blocks repeated SQL until acknowledgement is resolved')
    button('Commit'); wait('!document.querySelector(".db-tx")')
    check(count('tx-lost') == 1, 'explicit Commit retry confirms previous commit without repeating SQL')
    check('Run the statements again' not in js('document.body.innerText'), 'Commit recovery never advises rerunning committed SQL')
    check(js('(()=>{const a=requests.filter(r=>r.name==="tx"&&r.args[0].op==="commit");return a[0].args[0].operation_id===a[1].args[0].operation_id&&a[1].args[0].retry===true})()'), 'Commit retry keeps its own operation identity')
    run("INSERT INTO ack_item(label) VALUES('tx-lost')"); wait('!!document.querySelector(".db-tx")'); button('Commit'); wait('!document.querySelector(".db-tx")')
    check(count('tx-lost') == 2, 'new transaction in same console commits intentional same SQL')
    check(js('(()=>{const a=requests.filter(r=>r.name==="tx"&&r.args[0].op==="commit");return a[0].args[0].tx===a.at(-1).args[0].tx&&a[0].args[0].operation_id!==a.at(-1).args[0].operation_id})()'), 'reused console txId has a distinct new Commit operation id')
    screen('db-ack-commit-confirmed.png')
    # A genuinely absent worker is UNKNOWN, including the secondary result note.
    run('SELECT 1'); wait('!!document.querySelector(".db-tx")')
    tx = js('requests.filter(r=>r.name==="tx"&&r.args[0].op==="exec").at(-1).args[0].tx')
    manager.db_gui_tx(key, connection, 'rollback', tx=tx)
    button('Commit'); wait('!document.querySelector(".db-tx")')
    check('its outcome could not be confirmed' in js('document.querySelector(".db-err").innerText'), 'closed transaction error says outcome unknown')
    check('Transaction committed' not in js('document.querySelector(".db-result")?.innerText??document.querySelector(".db-body").innerText'), 'unknown result note does not falsely claim committed')
    screen('db-ack-closed-unknown.png')
    check(not js('errors.length'), 'no browser JavaScript errors')
except Exception:
    error = traceback.format_exc() + '\nHTTP ' + str(http_events)
    try:
        screen('db-ack-error.png')
    except Exception as diagnostic:
        error += '\nSCREEN ' + str(diagnostic)
    try:
        error += '\nDOM ' + str(js('({text:document.body.innerText,ready:window.ready,startupErrors:window.startupErrors,errors:window.errors,requests:window.requests?.slice(-6)})'))
    except Exception as diagnostic:
        error += '\nDOM unavailable ' + str(diagnostic)
finally:
    try: ab('close')
    except Exception: pass
    server.shutdown(); server.server_close()
    for worker in list(manager._db_workers.values()): worker.kill()
    case.doCleanups(); DbManagerTest.doClassCleanups()
print('RESULT ' + json.dumps({'checks': checks, 'screenshots': screens, 'error': error}))
if error: sys.exit(1)
