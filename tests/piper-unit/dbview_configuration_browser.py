"""Headless first-use/IDEA/mobile regression over the actual DB view and manager."""
import base64
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import traceback
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests/piper-unit'))
import _env
import common
import projects

owner = unittest.TestCase()
manager = _env.manager(owner)
assert not projects.load(), 'fixture must begin with no actual registered/discovered projects'
key = 'acme/db-config'
project = None
source = None
HTML = '''<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/piper/app.css">
<style>html,body,#fixture{height:100%;margin:0}#fixture{display:flex}#toast{position:fixed;bottom:0;left:0;z-index:99;background:var(--panel);padding:10px}</style>
<div id="fixture"></div><div id="overlay"></div><div id="toast" role="status"></div><script type="module">
import {createDbView,dbApi} from '/piper/dist/dbview.js';
window.errors=[];window.requests=[];window.projectOptions=[];window.registrationCalls=0;
window.addEventListener('error',e=>errors.push(e.message+' '+(e.error?.stack??'')));
const request=async(name,args)=>{requests.push({name,args});const r=await fetch('/fixture/'+name,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(args)});const data=await r.json();if(!r.ok)throw new Error(data.error);return {data};};
for(const name of ['all','connections','history','kube'])dbApi[name]=(...args)=>request(name,args);
dbApi.save=(...args)=>request('save',args);dbApi.running=async()=>({data:{running:[]}});
const overlay=document.querySelector('#overlay');
const close=()=>{overlay.replaceChildren();overlay.classList.remove('open');};
window.view=createDbView({toast:t=>document.querySelector('#toast').textContent=t,
 modal:(title,...children)=>{close();const layer=document.createElement('div');layer.className='olayer';const modal=document.createElement('div');modal.className='modal';modal.setAttribute('role','dialog');modal.setAttribute('aria-label',title);const h=document.createElement('h2');h.textContent=title;modal.append(h,...children.filter(Boolean));layer.append(modal);overlay.append(layer);overlay.classList.add('open');},
 closeModal:close,projects:()=>projectOptions,pending:()=>[],onRegisterProject:()=>{registrationCalls++;document.querySelector('#toast').textContent='Settings → Projects requested';}});
view.el.style.cssText='width:100%;height:100%;min-width:0';document.querySelector('#fixture').append(view.el);view.show();window.addEventListener('keydown',e=>view.onKey(e));window.ready=true;
</script>'''

class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)
    def log_message(self, *args):
        pass
    def do_GET(self):
        if self.path == '/':
            payload = HTML.encode(); self.send_response(200); self.send_header('content-type', 'text/html'); self.send_header('content-length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
        elif self.path.startswith('/piper/'):
            self.path = '/piper/web/' + self.path[7:]; super().do_GET()
        else:
            self.send_error(404)
    def do_POST(self):
        try:
            args = json.loads(self.rfile.read(int(self.headers['content-length'])))
            name = self.path.rsplit('/', 1)[1]
            if name == 'save':
                result = manager.db_gui_save(*args)
            elif name == 'all':
                result = manager.db_gui_all(); result['status']['ready'] = False
            else:
                result = getattr(manager, 'db_gui_' + name)(*args)
            self.send_response(200)
        except Exception as error:
            self.send_response(409); result = {'error': str(error)}
        payload = json.dumps(result).encode(); self.send_header('content-type', 'application/json'); self.send_header('content-length', str(len(payload))); self.end_headers(); self.wfile.write(payload)

server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
env = dict(os.environ, AGENT_BROWSER_PROFILE=str(Path(os.environ['TEST_SANDBOX_WORK']) / 'db-config-profile'))
SESSION = 'audit-db-config-' + str(os.getpid())
def ab(*args):
    run = subprocess.run(['agent-browser', '--session', SESSION, '--json', *args], capture_output=True, text=True, env=env, timeout=45)
    if run.returncode:
        raise RuntimeError(run.stderr or run.stdout)
    data = json.loads(run.stdout)
    if not data.get('success'):
        raise RuntimeError(str(data))
    return data.get('data', {})
def js(code):
    return ab('eval', code).get('result')
def wait(code):
    ab('wait', '--fn', code)
def button(name):
    ab('find', 'role', 'button', 'click', '--name', name, '--exact')
def check(value, label):
    assert value, label
    checks.append(label); print('CHECK ' + label, flush=True)
def screen(name):
    path = Path(os.environ['TEST_SANDBOX_WORK']) / name; ab('screenshot', str(path)); screens[name] = base64.b64encode(path.read_bytes()).decode()
def import_picker():
    button('Import'); ab('find', 'role', 'menuitem', 'click', '--name', 'Import from IDEA…', '--exact')
    wait('!!document.querySelector(".db-idea-import")'); button('Project'); ab('click', '.db-project-option')
def fits():
    return js('''(()=>{const m=document.querySelector('.modal');const b=m.getBoundingClientRect();return {viewport:b.left>=-1&&b.right<=innerWidth+1,modal:m.scrollWidth<=m.clientWidth+1,fields:[...m.querySelectorAll('input:not([type=checkbox]),select,label,button')].filter(e=>e.offsetParent).every(e=>{const r=e.getBoundingClientRect();return r.left>=b.left-1&&r.right<=b.right+1})}})()''')
checks, screens, error = [], {}, None
try:
    ab('open', f'http://127.0.0.1:{server.server_port}/'); ab('set', 'viewport', '1280', '633')
    for cycle in range(2):
        if cycle: ab('reload')
        wait('window.ready && document.body.innerText.includes("No projects registered yet")')
        check(not projects.load(), 'actual zero projects before registration ' + str(cycle))
        check(js('[...document.querySelectorAll(".db-connpage button")].filter(b=>["Add connection","Import","Scan stacks","Import from IDEA","Add"].includes(b.textContent.trim())).every(b=>b.disabled)'), 'zero-project dead-end actions are disabled ' + str(cycle))
        button('Register project'); wait('registrationCalls===1')
        check(js('registrationCalls') == 1, 'Register project invokes supplied navigation once ' + str(cycle))
        check(not js('document.querySelector("#overlay").classList.contains("open")'), 'zero-project action does not open empty project picker ' + str(cycle))
    screen('db-config-zero-projects.png')
    project = Path(_env.make_project('db-config', {'.idea/dataSources.xml': '''<project>
<data-source name="dev supported" uuid="supported"><jdbc-url>jdbc:postgresql://db.example.com:5432/shop</jdbc-url><user-name>reader</user-name></data-source>
<data-source name="dev embedded" uuid="embedded"><jdbc-url>jdbc:h2:mem:shop;PASSWORD=SENTINEL_DO_NOT_RENDER</jdbc-url></data-source></project>'''}))
    subprocess.run(['git', 'init', '-q', str(project)], check=True, capture_output=True)
    _env.register(key, str(project))
    source = project / '.idea/dataSources.xml'; raw = source.read_bytes()
    js('window.projectOptions=' + json.dumps([{'key': key, 'title': 'Synthetic DB configuration'}]) + ';view.refresh()')
    wait('!document.querySelector(".db-cp-head button.primary").disabled')
    check(len(projects.load()) == 1, 'actual project registration unlocks connection creation')
    for cycle in range(2):
        source.chmod(0)
        result = manager.db_gui_connections(key)
        check(result['idea'] == [] and any('permission denied' in e for e in result['idea_errors']), 'actual OS unreadable datasource is reported ' + str(cycle))
        js('view.settingsChanged()'); import_picker()
        wait('document.querySelector(".db-ideal")?.innerText.includes("permission denied")')
        check('No IDEA datasource' not in js('document.querySelector(".db-ideal").innerText'), 'unreadable source is not presented as absent ' + str(cycle))
        if cycle == 0: screen('db-config-idea-permission.png')
        source.chmod(0o600); check(source.read_bytes() == raw, 'permission recovery leaves exact XML bytes unchanged ' + str(cycle))
        button('Retry'); wait('document.querySelector(".db-ideal")?.innerText.includes("Unsupported engine: H2")')
        check('permission denied' not in js('document.querySelector(".db-ideal").innerText'), 'Retry bypasses cached error after permission recovery ' + str(cycle))
        check(js('document.querySelector(`button[aria-label="Use dev embedded"]`).disabled'), 'unsupported H2 Use is disabled ' + str(cycle))
        js('document.querySelector(`button[aria-label="Use dev embedded"]`).click()')
        check(js('!!document.querySelector(".db-idea-import") && !document.querySelector(".db-form")'), 'unsupported engine cannot open MySQL editor ' + str(cycle))
        check(not js('document.body.innerText.includes("SENTINEL_DO_NOT_RENDER")'), 'raw JDBC secret-like fixture sentinel is not rendered ' + str(cycle))
        if cycle == 0: screen('db-config-idea-unsupported.png')
        button('Use dev supported'); wait('document.querySelector("select[name=engine]")?.value==="postgres"')
        check(js('document.querySelector("input[name=database]").value') == 'shop', 'supported import retains actual engine and database ' + str(cycle))
        button('Cancel')
    ab('set', 'viewport', '390', '844')
    for suffix in ('first', 'second'):
        button('Add connection'); wait('!!document.querySelector(".db-form")'); ab('click', 'button[name=project]'); ab('click', '.db-project-option')
        wait('!!document.querySelector("select[name=via] option[value=direct]")')
        ab('select', 'select[name=cls]', 'test'); ab('select', 'select[name=engine]', 'postgres')
        name = key + '/test/mobile-' + suffix
        for field, value in {'name': name, 'database': 'shop', 'user': 'reader', 'label': 'mobile-' + suffix}.items(): ab('fill', 'input[name=' + field + ']', value)
        ab('select', 'select[name=via]', 'direct'); ab('fill', 'input[name=host]', '127.0.0.1'); ab('fill', 'input[name=port]', '5432')
        check(all(fits().values()), '390px Add has no horizontal overflow and every field/action fits ' + suffix)
        screen('db-config-add-390-' + suffix + '.png')
        ab('scrollintoview', '#overlay .modal button.primary'); screen('db-config-add-390-actions-' + suffix + '.png'); button('Save')
        wait('!document.querySelector("#overlay").classList.contains("open") && !![...document.querySelectorAll(".db-cname2")].find(b=>b.querySelector(".db-mono")?.textContent==="mobile-' + suffix + '")')
        check(any(c['name'] == name and c['engine'] == 'postgres' for c in manager._db_store()['connections']), '390px Add persists exact engine/connection ' + suffix)
        js('(()=>{const row=[...document.querySelectorAll(".db-crow")].find(r=>r.querySelector(".db-cname2 .db-mono")?.textContent===' + json.dumps('mobile-' + suffix) + ');[...row.querySelectorAll("button")].find(b=>b.textContent==="Edit").click()})()')
        wait('!!document.querySelector(".db-form")')
        check(all(fits().values()), '390px Edit has no horizontal overflow and every field/action fits ' + suffix)
        ab('fill', 'input[name=label]', 'edited-' + suffix); ab('scrollintoview', '#overlay .modal button.primary'); screen('db-config-edit-390-' + suffix + '.png'); button('Save')
        wait('!document.querySelector("#overlay").classList.contains("open") && !![...document.querySelectorAll(".db-cname2")].find(b=>b.querySelector(".db-mono")?.textContent==="edited-' + suffix + '")')
        check(any(c['name'] == name and c['label'] == 'edited-' + suffix for c in manager._db_store()['connections']), '390px Edit persists alias change ' + suffix)
    ab('set', 'viewport', '1280', '633'); button('Add connection'); wait('!!document.querySelector(".db-form")')
    check(js('getComputedStyle(document.querySelector(".db-form-row")).display') == 'grid', 'desktop retains existing grid form layout')
    button('Cancel'); check(not js('errors.length'), 'no browser JavaScript errors')
except Exception:
    error = traceback.format_exc()
    try: error += '\nDOM ' + str(js('({text:document.body.innerText,errors,requests:requests.slice(-4)})')); screen('db-config-error.png')
    except Exception: pass
finally:
    if source: source.chmod(0o600)
    try: ab('close')
    except Exception: pass
    server.shutdown(); server.server_close(); owner.doCleanups()
print('RESULT ' + json.dumps({'checks': checks, 'screenshots': screens, 'error': error}), flush=True)
if error: raise SystemExit(1)
