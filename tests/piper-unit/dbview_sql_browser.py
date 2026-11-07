"""Headless SQL/metadata regression over actual view and manager; synthetic SQLite driver."""
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
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tests/piper-unit'))
import _env
from test_dbtools import DbManagerTest
import dbtools
DbManagerTest.setUpClass();case=DbManagerTest('test_console_schema_change_refused_until_transaction_ends');case.setUp()
manager,key=case.m,case.key;connection=key+'/local'
store=manager._db_store();store['connections']=[dict(c,engine='postgres') for c in store['connections'] if c['name']==connection];manager._db_save(store);dbtools.write_secret(connection,'fixture-only')
other=Path(_env.ROOT)/'other-scope.sqlite'
with contextlib.closing(sqlite3.connect(other)) as c:
    c.executescript("CREATE TABLE scope_probe(id INTEGER PRIMARY KEY,label TEXT); INSERT INTO scope_probe VALUES(1,'other-before'); CREATE TABLE parent(a INTEGER,b INTEGER,label TEXT); INSERT INTO parent VALUES(1,2,'target'),(1,3,'sibling')")
with contextlib.closing(sqlite3.connect(case.dbs['dev'])) as c:
    c.executescript('''CREATE TABLE scope_probe(id INTEGER PRIMARY KEY,label TEXT); INSERT INTO scope_probe VALUES(1,'main-before');
CREATE TABLE child(a INTEGER,b INTEGER); INSERT INTO child VALUES(1,2);
CREATE TABLE "Order Items"(id INTEGER); INSERT INTO "Order Items" VALUES(5);''')
driver=Path(_env.ROOT)/'sql-driver.py'
driver.write_text('''import sys,re
sys.path.insert(0,__TESTS__)
from dbfake_sqlite import SQLite,SQLiteCursor
SCHEMA={}
class Cursor(SQLiteCursor):
    def execute(self,sql,params=()):
        sql=re.sub(r'(?<![.\\w"])\\bscope_probe\\b', '"'+SCHEMA.get(self.connection,'main')+'"."scope_probe"',sql)
        sql=re.sub(r'(\\$[A-Za-z_][A-Za-z0-9_]*\\$|\\$\\$)(.*?)\\1',lambda m:"'"+m[2].replace("'","''")+"'",sql,flags=re.S)
        return super().execute(sql,params)
class Fixture(SQLite):
    def connect(self,mode):
        c=super().connect(mode);c.execute('ATTACH DATABASE ? AS other',(__OTHER__,));SCHEMA[c]='main';return c
    def cursor(self,c,unbuffered): return c.cursor(factory=Cursor)
    def begin_read(self,c,schema): SCHEMA[c]=schema or 'main';super().begin_read(c,schema)
    def begin_write(self,c,schema): SCHEMA[c]=schema or 'main';super().begin_write(c,schema)
    def meta(self,c,what,schema,name):
        if what=='schemas': return [['main'],['other']]
        if what=='fks' and name=='child': return [['parent_fk','a','other','parent','a'],['parent_fk','b','other','parent','b']]
        if schema=='other':
            if what=='columns': return [[r[1],r[2],'YES','PRI' if r[5] else '',r[4],'',''] for r in c.execute('PRAGMA other.table_info("'+name+'")')]
            if what=='ddl': return [[c.execute('SELECT sql FROM other.sqlite_master WHERE name=?',(name,)).fetchone()[0]]]
            if what=='objects': return [[r[0],r[1],'InnoDB',None,''] for r in c.execute("SELECT name,type FROM other.sqlite_master WHERE type IN ('table','view')")]
        return super().meta(c,what,schema,name)
DRIVERS={'postgres':Fixture}
'''.replace('__TESTS__',repr(str(ROOT/'tests/piper-unit'))).replace('__OTHER__',repr(str(other))))
os.environ['PIPER_DB_TEST_DRIVERS']=str(driver)
HTML='''<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/piper/app.css">
<style>html,body,#fixture{height:100%;margin:0}#fixture{display:flex}#toast{position:fixed;bottom:0;left:0;z-index:99;background:var(--panel);padding:10px}</style>
<div id="fixture"></div><div id="toast" role="status"></div><script type="module">
import {createDbView,dbApi} from '/piper/dist/dbview.js';
window.errors=[];window.requests=[];window.addEventListener('error',e=>errors.push(e.message));
const request=async(name,args)=>{requests.push({name,args});const r=await fetch('/fixture/'+name,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(args)});const data=await r.json();if(!r.ok)throw new Error(data.error);return {data}};
for(const name of ['all','meta','connections','history'])dbApi[name]=(...args)=>request(name,args);
for(const name of ['query','execute','tx'])dbApi[name]=b=>request(name,[b]);
dbApi.running=async()=>({data:{running:[]}});
window.view=createDbView({toast:t=>document.querySelector('#toast').textContent=t,modal:()=>{},closeModal:()=>{},projects:()=>[{key:'__KEY__',title:'Synthetic SQL'}],pending:()=>[]});
view.el.style.cssText='width:100%;height:100%;min-width:0';document.querySelector('#fixture').append(view.el);view.show();window.addEventListener('keydown',e=>view.onKey(e));window.ready=true;
</script>'''.replace('__KEY__',key)
metadata_error=True
metadata_hold=False
metadata_entered, metadata_release = threading.Event(), threading.Event()
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self,*a,**kw):super().__init__(*a,directory=str(ROOT),**kw)
    def log_message(self,*a):pass
    def do_GET(self):
        if self.path=='/':self.send_response(200);self.send_header('content-type','text/html');self.end_headers();self.wfile.write(HTML.encode())
        elif self.path.startswith('/piper/'):self.path='/piper/web/'+self.path[7:];super().do_GET()
        else:self.send_error(404)
    def do_POST(self):
        try:
            args=json.loads(self.rfile.read(int(self.headers['content-length'])));name=self.path.rsplit('/',1)[1]
            if name=='meta' and args[2]=='schemas' and metadata_error:raise ValueError('Synthetic metadata permission denied')
            if name=='meta' and args[2]=='schemas' and metadata_hold:
                metadata_entered.set(); metadata_release.wait(10)
            if name in ('query','execute','tx'):
                kwargs=dict(args[0]);kwargs['key']=kwargs.pop('project');result=getattr(manager,'db_gui_'+name)(**kwargs)
            elif name=='all':
                result=manager.db_gui_all();result['status']['ready']=False
            else:result=getattr(manager,'db_gui_'+name)(*args)
            self.send_response(200)
        except Exception as e:self.send_response(409);result={'error':str(e)}
        self.send_header('content-type','application/json');self.end_headers();self.wfile.write(json.dumps(result).encode())
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
env=dict(os.environ,AGENT_BROWSER_PROFILE=str(Path(os.environ['TEST_SANDBOX_WORK'])/'db3-browser-profile'));SESSION='audit-db-sql-'+str(os.getpid())
def ab(*args):
    r=subprocess.run(['agent-browser','--session',SESSION,'--json',*args],capture_output=True,text=True,env=env,timeout=45)
    if r.returncode:raise RuntimeError(r.stderr or r.stdout)
    data=json.loads(r.stdout)
    if not data.get('success'):raise RuntimeError(str(data))
    return data.get('data',{})
def js(code):return ab('eval',code).get('result')
def wait(code):ab('wait','--fn',code)
def button(name):
    if name=='Commit':ab('click','.db-tx button.primary')
    else:ab('find','role','button','click','--name',name,'--exact')
def check(value,label):
    assert value,label
    checks.append(label);print('CHECK '+label,flush=True)
def screen(name):
    p=Path(os.environ['TEST_SANDBOX_WORK'])/name;ab('screenshot',str(p));screens[name]=base64.b64encode(p.read_bytes()).decode()
def sql(text,selected=False):
    ab('fill','textarea[aria-label=SQL]',text)
    js('(()=>{const t=document.querySelector("textarea[aria-label=SQL]");t.focus();t.setSelectionRange('+('0' if selected else str(len(text)))+','+str(len(text))+');t.dispatchEvent(new Event("keyup"));})()')
def run(text,selected=False):
    before=js('requests.filter(r=>r.name==="tx"&&r.args[0].op==="exec"||r.name==="query").length')
    sql(text,selected);ab('press','Control+Enter');wait('requests.filter(r=>r.name==="tx"&&r.args[0].op==="exec"||r.name==="query").length>'+str(before)+' && ![...document.querySelectorAll(".db-bar button")].some(b=>b.textContent==="Cancel")')
def source(path,sql):
    with contextlib.closing(sqlite3.connect(path)) as c:return c.execute(sql).fetchall()
checks=[];screens={};error=None
try:
    ab('open',f'http://127.0.0.1:{server.server_port}/');ab('set','viewport','1440','1000');wait('window.ready && !!document.querySelector(".db-cname2")');ab('click','.db-cname2')
    wait('document.body.innerText.includes("Synthetic metadata permission denied")');check(js('!![...document.querySelectorAll("button")].find(b=>b.textContent==="Try again")'),'schema error remains visible with existing retry despite unavailable driver banner');screen('db-sql-metadata-error.png')
    metadata_error=False;button('Try again');wait('document.querySelectorAll(".db-ti").length>3')
    ab('press','Control+Shift+l');wait('!!document.querySelector("textarea[aria-label=SQL]")');button('Read-write')
    run('SELECT * FROM scope_probe;');wait('!!document.querySelector(".db-tx")')
    check(js('document.querySelector(".db-schsel").disabled'),'Own TX schema picker locked')
    check(js('document.querySelector(`button[aria-label="Refresh metadata"]`).disabled'),'metadata refresh is disabled during an open transaction')
    check('main' in js('document.querySelector(".db-tx-s").textContent'),'Own TX banner shows actual main scope');screen('db-sql-schema-locked.png')
    run("UPDATE scope_probe SET label='main-after' WHERE id=1");button('Commit');wait('!document.querySelector(".db-tx")')
    check(source(case.dbs['dev'],'SELECT label FROM scope_probe')==[('main-after',)] and source(other,'SELECT label FROM scope_probe')==[('other-before',)],'first commit writes only the selected main database')
    ab('select','.db-schsel','other');run("UPDATE scope_probe SET label='other-after' WHERE id=1");wait('!!document.querySelector(".db-tx")')
    check('other' in js('document.querySelector(".db-tx-s").textContent'),'next transaction banner uses newly selected other schema');button('Commit');wait('!document.querySelector(".db-tx")')
    check(source(case.dbs['dev'],'SELECT label FROM scope_probe')==[('main-after',)] and source(other,'SELECT label FROM scope_probe')==[('other-after',)],'new transaction after schema switch writes only other fixture database')
    ab('select','.db-schsel','main');run('SELECT 11 AS first_value; SELECT 22 AS second_value;',True)
    wait('document.body.innerText.includes("second_value")');check(js('requests.filter(r=>r.name==="tx"&&r.args[0].op==="exec").at(-1).args[0].statements.length')==2,'selected two SQL statements sent as two entries');check(js('document.querySelectorAll(".db-rtab").length')>=2,'separate result tabs rendered for selected SQL');screen('db-sql-multi-result.png');button('Rollback');wait('!document.querySelector(".db-tx")')
    button('Read-only');run('SELECT $tag$fresh;value$tag$ AS marker;');wait('document.body.innerText.includes("fresh;value")')
    check(js('requests.filter(r=>r.name==="query").at(-1).args[0].sql')=='SELECT $tag$fresh;value$tag$ AS marker','dollar-quoted caret request preserves the complete PostgreSQL statement')
    sql('SELECT * FROM Ord');ab('press','Control+Space');wait('!!document.querySelector(".db-aci")');ab('click','.db-aci')
    check(js('document.querySelector("textarea").value')=='SELECT * FROM "Order Items"','completion inserts the engine quoted table identifier');ab('press','Control+Enter');wait('document.body.innerText.includes("id")');screen('db-sql-quoted-completion.png')
    js('view.openReference('+json.dumps(key)+','+json.dumps(connection)+',"main","child")');wait('!!document.querySelector(".db-fklink")');ab('click','.db-fklink');wait('document.body.innerText.includes("target")')
    check(js('document.querySelector(".db-where input")?.value || document.querySelector("input[aria-label=WHERE]")?.value || document.querySelector(".db-where").innerText').find('b')>=0 or js('requests.filter(r=>r.name==="query").at(-1).args[0].sql').find('"b" = 2')>=0,'FK filter includes the second key column')
    check(not js('document.querySelector(".db-grid")?.innerText.includes("sibling")'),'composite FK does not show unrelated sibling');check('other' in js('document.querySelector(".db-st-path").textContent'),'FK target footer identifies the other schema');screen('db-sql-composite-fk.png')
    js('view.openReference('+json.dumps(key)+','+json.dumps(connection)+',"main","customer")');wait('!!document.querySelector("button[aria-label=more]")');ab('click','.db-rbtn');wait('!!document.querySelector(".db-ddl")');check('outside' not in js('document.querySelector(".db-ddl").innerText'),'DDL starts from the original definition')
    with contextlib.closing(sqlite3.connect(case.dbs['dev'])) as c:c.executescript('ALTER TABLE customer ADD COLUMN outside TEXT; CREATE TABLE external_new(id INTEGER)')
    button('Data');ab('click','.db-rbtn');wait('document.querySelector(".db-ddl")?.innerText.includes("outside")');screen('db-sql-ddl-fresh.png');check(True,'Structure Show DDL bypasses stale server and client metadata caches')
    button('Data')
    with contextlib.closing(sqlite3.connect(case.dbs['dev'])) as c:c.execute('ALTER TABLE customer ADD COLUMN menu_added TEXT')
    button('more');ab('find','role','menuitem','click','--name','Show DDL','--exact');wait('document.querySelector(".db-ddl")?.innerText.includes("menu_added")');check(True,'menu Show DDL also fetches the current definition')
    button('Switch');ab('find','role','menuitem','click','--name','All databases · overview','--exact');wait('!!document.querySelector(".db-cname2")');ab('click','.db-cname2');wait('document.body.innerText.includes("external_new")');check(True,'reopening connection refreshes tree after an external CREATE');screen('db-sql-tree-fresh.png')
    metadata_error=True
    button('Switch');ab('find','role','menuitem','click','--name','All databases · overview','--exact');wait('!!document.querySelector(".db-cname2")');ab('click','.db-cname2');wait('document.querySelector(".db-tree")?.innerText.includes("Synthetic metadata permission denied")');check(True,'failed schema refresh is visible instead of showing cached objects')
    metadata_error=False;button('Try again');wait('document.querySelector(".db-tree")?.innerText.includes("external_new")');check(True,'schema refresh retry restores the current tree')
    for suffix in ('first','second'):
        ab('press','Control+Shift+l');wait('!!document.querySelector("textarea[aria-label=SQL]")')
        draft='SELECT 910 /* unsent '+suffix+' draft */;';sql(draft)
        with contextlib.closing(sqlite3.connect(case.dbs['dev'])) as c:c.execute('CREATE TABLE refresh_'+suffix+'(id INTEGER)')
        check('refresh_'+suffix not in js('document.querySelector(".db-tree").innerText'),'external CREATE stays absent before explicit refresh '+suffix)
        before=js('requests.filter(r=>["query","execute","tx"].includes(r.name)).length')
        if suffix=='first': metadata_hold=True
        button('Refresh metadata')
        if suffix=='first':
            check(metadata_entered.wait(2),'refresh reaches actual metadata HTTP request')
            wait('document.querySelector(`button[aria-label="Refresh metadata"]`).disabled && !!document.querySelector(`button[aria-label="Refresh metadata"] .spinner`)')
            before_refresh=js('requests.filter(r=>r.name==="meta"&&r.args[2]==="schemas").length')
            js('document.querySelector(`button[aria-label="Refresh metadata"]`).click()')
            check(js('requests.filter(r=>r.name==="meta"&&r.args[2]==="schemas").length')==before_refresh,'busy refresh does not issue parallel metadata request')
            screen('db-metadata-refresh-loading.png');metadata_hold=False;metadata_release.set()
        wait('document.querySelector(".db-tree")?.innerText.includes("refresh_'+suffix+'")')
        check(js('document.querySelector("textarea[aria-label=SQL]").value')==draft,'metadata refresh keeps unsent SQL draft '+suffix)
        check(js('requests.filter(r=>["query","execute","tx"].includes(r.name)).length')==before,'metadata refresh dispatches no SQL or transaction action '+suffix)
        check(js('requests.filter(r=>r.name==="meta"&&r.args[2]==="schemas").at(-1).args[6]') is True,'explicit refresh bypasses server metadata cache '+suffix)
    screen('db-metadata-explicit-refresh.png')
    metadata_error=True;button('Refresh metadata');wait('document.querySelector(".db-tree")?.innerText.includes("Synthetic metadata permission denied")')
    check(not js('document.querySelector(`button[aria-label="Refresh metadata"]`).disabled'),'failed explicit refresh restores available control')
    screen('db-metadata-refresh-error.png');metadata_error=False;button('Try again');wait('document.querySelector(".db-tree")?.innerText.includes("refresh_second")')
    check(js('document.querySelector("textarea[aria-label=SQL]").value')==draft,'refresh error and Retry preserve SQL draft')
    js('view.openReference('+json.dumps(key)+','+json.dumps(connection)+',"main","customer")');wait('!!document.querySelector(".db-grid") || !!document.querySelector(".db-ddl")')
    if js('!!document.querySelector(".db-ddl")'):button('Data')
    ab('click','.db-gc[data-r="0"][data-c="1"]');ab('focus','.db-grid');ab('press','F2');wait('!!document.querySelector(".db-cedit")');ab('fill','.db-cedit','unsaved-grid-draft');ab('press','Enter')
    button('Refresh metadata');wait('!document.querySelector(`button[aria-label="Refresh metadata"]`).disabled')
    check(js('document.querySelector(`.db-gc[data-r="0"][data-c="1"]`).textContent')=='unsaved-grid-draft','metadata refresh preserves pending grid edits')
    check(source(case.dbs['dev'],'SELECT name FROM customer WHERE id=1')==[('Ann',)],'metadata refresh does not commit grid draft')
    screen('db-metadata-keeps-grid-draft.png')
    check(not js('errors.length'),'no browser JavaScript errors')
except Exception:
    error=traceback.format_exc()
    try:error+='\nDOM '+str(js('({text:document.body.innerText,errors,requests:requests.slice(-4)})'));screen('db-sql-error.png')
    except Exception:pass
finally:
    try:ab('close')
    except Exception:pass
    server.shutdown();server.server_close()
    for worker in list(manager._db_workers.values()):worker.kill()
    case.doCleanups();DbManagerTest.doClassCleanups()
print('RESULT '+json.dumps({'checks':checks,'screenshots':screens,'error':error}))
if error:sys.exit(1)
