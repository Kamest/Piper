"""Actual CSV cancellation and pre-dispatch console cancellation, with an isolated headless browser."""
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
    db.execute('CREATE TABLE cancel_item(id INTEGER PRIMARY KEY,label TEXT)')
    db.execute("""CREATE TRIGGER slow_csv BEFORE INSERT ON cancel_item WHEN NEW.id BETWEEN 55000 AND 55103
        BEGIN SELECT (WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<100000000) SELECT sum(i) FROM n); END""")
endpoint_entered, endpoint_release = threading.Event(), threading.Event()
hold_endpoint = False
original_endpoint = manager._db_endpoint
dispatched = []
original_call = manager._db_gui_call
def held_endpoint(*args, **kwargs):
    if hold_endpoint:
        endpoint_entered.set()
        while not endpoint_release.wait(.02):
            manager._db_gui_check_cancelled()
    return original_endpoint(*args, **kwargs)
def logged_call(worker, op, timeout, **kwargs):
    dispatched.append((op, kwargs.get('sql')))
    return original_call(worker, op, timeout, **kwargs)
manager._db_endpoint, manager._db_gui_call = held_endpoint, logged_call
driver = Path(_env.ROOT) / 'cancel-driver.py'
driver.write_text("import sys\nsys.path.insert(0," + repr(str(ROOT / 'tests/piper-unit')) + ")\nfrom dbfake_sqlite import SQLite\nDRIVERS={'postgres':SQLite}\n")
os.environ['PIPER_DB_TEST_DRIVERS'] = str(driver)
HTML = '''<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/piper/app.css">
<style>html,body,#fixture{height:100%;margin:0}#fixture{display:flex}#toast{position:fixed;bottom:0;left:0;z-index:99;background:var(--panel);padding:10px}</style>
<div id="modal" hidden style="position:fixed;z-index:100;background:var(--bg);padding:24px;top:120px;left:350px;right:350px"></div><div id="fixture"></div><div id="toast" role="status"></div><script>window.startupErrors=[];window.addEventListener("error",e=>startupErrors.push(e.message||e.target?.src));</script><script type="module">
import {createDbView,dbApi} from '/piper/dist/dbview.js';
window.errors=[];window.requests=[];
window.addEventListener('error',e=>errors.push(e.message+' '+(e.error?.stack??'')));
const request=async(name,args)=>{
  requests.push({name,args});
  const r=await fetch('/fixture/'+name,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(args)});
  const data=await r.json();
  if(!r.ok)throw new Error(data.error);
  return {data};
};
for(const name of ['all','meta','connections','history'])dbApi[name]=(...args)=>request(name,args);
for(const name of ['query','execute','tx'])dbApi[name]=b=>request(name,[b]);
dbApi.running=async()=>({data:{running:[]}});dbApi.cancel=(...args)=>request('cancel',args);
window.view=createDbView({toast:t=>document.querySelector('#toast').textContent=t,modal:(title,...children)=>{const m=document.querySelector('#modal');m.replaceChildren();const h=document.createElement('h3');h.textContent=title;m.append(h,...children);m.hidden=false;},closeModal:()=>{document.querySelector('#modal').hidden=true;},projects:()=>[{key:'__KEY__',title:'Synthetic cancellation'}],pending:()=>[]});
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
            elif name == 'cancel':
                result = manager.db_gui_cancel(args[0], args[1], session=args[4], owner=args[2], request_tag=args[3])
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
env = dict(os.environ, AGENT_BROWSER_PROFILE=str(Path(os.environ['TEST_SANDBOX_WORK']) / 'db-cancel-profile'))
SESSION = 'audit-db-cancel-' + str(os.getpid())
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
def check(value, label):
    assert value, label
    checks.append(label); print('CHECK ' + label, flush=True)
def screen(name):
    path = Path(os.environ['TEST_SANDBOX_WORK']) / name
    ab('screenshot', str(path)); screens[name] = base64.b64encode(path.read_bytes()).decode()
def count(label):
    with contextlib.closing(sqlite3.connect(case.dbs['dev'])) as db:
        return db.execute('SELECT count(*) FROM cancel_item WHERE label=?', (label,)).fetchone()[0]
def running():
    return manager.db_gui_running(key, connection)['running']
def poll(predicate):
    import time
    end = time.time() + 15
    while time.time() < end:
        if predicate(): return
        time.sleep(.02)
    raise AssertionError('timed out waiting for fixture state')
def open_csv(marker):
    ab('click', '.db-main .db-bar button[aria-label=more]')
    ab('find', 'text', 'Import CSV…', 'click', '--exact')
    path = Path(os.environ['TEST_SANDBOX_WORK']) / ('csv-' + str(marker) + '.csv')
    path.write_text('id,label\n' + ''.join(str(marker + i) + ',batch-' + str(marker) + '\n' for i in range(4)))
    ab('upload', '#modal input[type=file]', str(path))
    wait('!document.querySelector("#modal button.primary").disabled')
checks, screens, error = [], {}, None
try:
    ab('open', f'http://127.0.0.1:{server.server_port}/'); ab('set', 'viewport', '1440', '1000')
    wait('window.ready&&!!document.querySelector(".db-cname2")'); ab('click', '.db-cname2')
    wait('document.querySelector(".db-tree")?.innerText.includes("cancel_item")')
    js('view.openReference(' + json.dumps(key) + ',' + json.dumps(connection) + ',"main","cancel_item")')
    wait('!![...document.querySelectorAll("button")].find(b=>b.textContent==="+ Row")')
    for marker in (55000, 55100):
        open_csv(marker); ab('click', '#modal button.primary')
        poll(lambda: any(r['op'] == 'tx_exec' for r in running()))
        check(count('batch-' + str(marker)) == 0, 'running CSV remains uncommitted ' + str(marker))
        if marker == 55000: screen('db-cancel-csv-running.png')
        ab('find', 'role', 'button', 'click', '--name', 'Cancel', '--exact')
        wait('document.querySelector("#modal").hidden&&document.querySelector("#toast").textContent.includes("Import cancelled")')
        check(count('batch-' + str(marker)) == 0, 'Cancel rolls back every CSV row ' + str(marker))
        check(js('requests.filter(r=>r.name==="tx"&&r.args[0].op==="commit").length') == 0, 'CSV Cancel never sends Commit ' + str(marker))
        check(not running(), 'CSV running worker cleaned after Cancel ' + str(marker))
    screen('db-cancel-csv-rolled-back.png')
    open_csv(56000); ab('click', '#modal button.primary')
    wait('document.querySelector("#modal").hidden&&document.querySelector("#toast").textContent.includes("Imported 4 rows")')
    check(count('batch-56000') == 4, 'explicit new CSV imports after cancellation')
    check(js('(()=>{const a=requests.filter(r=>r.name==="tx"&&r.args[0].op==="exec");return a.every(r=>r.args[0].request_tag===r.args[0].tx)&&new Set(a.map(r=>r.args[0].tx)).size===3})()'), 'each import uses its own transaction and Cancel request tag')
    # New RO console uses a controlled real manager endpoint-preparation wait.
    ab('find', 'role', 'button', 'click', '--name', 'Open console', '--exact')
    wait('!!document.querySelector("textarea[aria-label=SQL]")')
    for marker in (68101, 68102):
        endpoint_entered.clear(); endpoint_release.clear(); hold_endpoint = True
        ab('fill', 'textarea[aria-label=SQL]', 'SELECT ' + str(marker))
        ab('focus', 'textarea[aria-label=SQL]'); ab('press', 'Control+Enter')
        check(endpoint_entered.wait(10), 'console reaches endpoint preparation ' + str(marker))
        check(not any(op == 'query' and sql == 'SELECT ' + str(marker) for op, sql in dispatched), 'SQL not dispatched before Cancel ' + str(marker))
        if marker == 68101: screen('db-cancel-console-preparing.png')
        ab('click', '.db-main .db-bar button.danger')
        wait('document.querySelector(".db-err")?.textContent.includes("cancelled")')
        hold_endpoint = False; endpoint_release.set()
        check(not any(op == 'query' and sql == 'SELECT ' + str(marker) for op, sql in dispatched), 'cancelled preparation never dispatches SQL ' + str(marker))
        check(not manager._db_gui_requests, 'preparation request lifecycle removed ' + str(marker))
    screen('db-cancel-console-cancelled.png')
    ab('fill', 'textarea[aria-label=SQL]', 'SELECT 69003')
    ab('focus', 'textarea[aria-label=SQL]'); ab('press', 'Control+Enter')
    wait('document.querySelector(".db-main").innerText.includes("69003")&&!document.querySelector(".db-err")')
    check(('query', 'SELECT 69003') in dispatched, 'new deliberate console request succeeds after Cancel')
    screen('db-cancel-console-recovered.png')
    check(not js('errors.length'), 'no browser JavaScript errors')
except Exception:
    error = traceback.format_exc() + '\nHTTP ' + str(http_events)
    try: screen('db-cancel-error.png')
    except Exception: pass
    try: error += '\nDOM ' + str(js('({text:document.body.innerText,ready:window.ready,errors:window.errors,requests:window.requests?.slice(-6)})'))
    except Exception: pass
finally:
    endpoint_release.set()
    try: ab('close')
    except Exception: pass
    server.shutdown(); server.server_close()
    for worker in list(manager._db_workers.values()): worker.kill()
    case.doCleanups(); DbManagerTest.doClassCleanups()
print('RESULT ' + json.dumps({'checks': checks, 'screenshots': screens, 'error': error}))
if error: sys.exit(1)
