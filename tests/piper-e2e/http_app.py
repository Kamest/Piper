#!/usr/bin/env python3
"""Real ijhttp + isolated piperd + agent-browser HTTP compatibility regression.

Run through scripts/piper-sandbox.py with PIPER_IJHTTP and JAVA_HOME set.
The optional PIPER_E2E_HTTP_APP_HOLD keeps screenshots available until Enter.
"""
import json
import hashlib
import os
import signal
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
NATIVE = os.environ.get('PIPER_IJHTTP')
if not NATIVE or not os.access(NATIVE, os.X_OK):
    sys.exit('PIPER_IJHTTP must name the real executable')

if '--serve' in sys.argv:
    sys.argv.remove('--serve')
    import serve
    serve.R.ENV['PIPER_IJHTTP'] = NATIVE
    raise SystemExit(serve.main())

sys.argv = [__file__, '--database-only', '--presets', 'all', '--widths', '1024',
            '--port', '17981', '--out', os.path.join(os.getcwd(), 'http-app-artifacts')]
import gui as g

BODY = b'<?xml version="1.0" encoding="UTF-8"?><feed>\xc4\x8d</feed>\r\n'
BINARY = b'%PDF-1.7\r\n' + bytes(range(256)) * 100 + b'\r\n%%EOF'
hits = []
uploads = []
browser_auth = {"authorized":False}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith('/auth-browser-'):
            import urllib.parse
            fields = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            path = self.path.split('?')[0]
            hits.append(path)
            if path == '/auth-browser-authorize':
                browser_auth['challenge'] = fields.get('code_challenge')
                browser_auth['redirect'] = fields.get('redirect_uri')
                target = fields['redirect_uri'] + '?' + urllib.parse.urlencode({'code':'app-browser-code42','state':fields['state']})
                body = ('<a id="fixture-authorize" href="' + target.replace('&','&amp;') + '">Authorize test account</a>').encode()
            elif path == '/auth-browser-verify':
                body = b'<a id="fixture-authorize" href="/auth-browser-device-complete">Authorize test device</a>'
            else:
                browser_auth['authorized'] = True
                body = b'Device authorized; return to Piper.'
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        hits.append(self.path)
        if self.path == '/sse-app':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Connection', 'close')
            self.end_headers()
            for chunk in (b'data: first\n\n', b'data: \xc3', b'\xa9\n\n', b'data: tail'):
                self.wfile.write(chunk)
                self.wfile.flush()
                time.sleep(.03)
            self.close_connection = True
            return
        if self.path == '/overflow':
            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Length', str((64 << 20) + 1))
            self.end_headers()
            return
        cookie = self.path.startswith('/cookie-v5')
        auth = self.path.startswith('/auth-v5-resource')
        body = (json.dumps({'auth':self.headers.get('Authorization', '')}).encode() if auth else
                json.dumps({'cookie':self.headers.get('Cookie', '')}).encode() if cookie else
                BINARY + self.path.encode() if self.path.startswith('/iteration-binary') else
                BINARY if self.path == '/binary' else BODY)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json' if cookie or auth else
                         'application/pdf' if self.path == '/binary' or self.path.startswith('/iteration-binary') else 'application/xml; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        if self.path == '/cookie-v5-login':
            self.send_header('Set-Cookie', 'appSession=synthetic-cookie-session-v5; Path=/; HttpOnly; SameSite=Lax')
        elif self.path == '/cookie-v5-agent-login':
            self.send_header('Set-Cookie', 'appSession=synthetic-agent-cookie-session-v5; Path=/; HttpOnly; SameSite=Lax')
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        hits.append(self.path)
        uploads.append(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        if self.path in ('/auth-browser-token','/auth-browser-device'):
            import urllib.parse
            import base64
            fields = dict(urllib.parse.parse_qsl(uploads[-1].decode()))
            if self.path == '/auth-browser-device':
                value, status = {'device_code':'app-private-device-code42','user_code':'APP-CODE','verification_uri':
                                 'http://127.0.0.1:%d/auth-browser-verify' % server.server_port,'expires_in':60,'interval':.1},200
            elif fields.get('grant_type') == 'authorization_code':
                challenge = base64.urlsafe_b64encode(hashlib.sha256(fields.get('code_verifier','').encode()).digest()).rstrip(b'=').decode()
                valid = challenge == browser_auth.get('challenge') and fields.get('code') == 'app-browser-code42' and fields.get('redirect_uri') == browser_auth.get('redirect')
                value, status = ({'access_token':'synthetic-app-access-v5','expires_in':3600},200) if valid else ({'error':'invalid_grant'},400)
            elif browser_auth['authorized'] and fields.get('device_code') == 'app-private-device-code42':
                value, status = {'access_token':'synthetic-app-access-v5','expires_in':3600},200
            else:
                value, status = {'error':'authorization_pending'},400
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == '/auth-v5-token':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"access_token":"synthetic-app-access-v5","id_token":"synthetic-app-id-v5","expires_in":3600}')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"ok":true}')


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def start_serve():
    g.SERVE_WORK = os.path.join(g.OUT, 'serve')
    process = subprocess.Popen([sys.executable, __file__, '--serve', '--port', str(g.A.port),
                                '--work', g.SERVE_WORK], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, start_new_session=True)
    log = []
    end = time.time() + 180
    while time.time() < end:
        line = process.stdout.readline()
        if not line:
            break
        log.append(line)
        if line.startswith('SERVE '):
            return process, line.split()[1], log
    if process.poll() is None:
        os.kill(process.pid, signal.SIGTERM)
    raise RuntimeError('test piperd did not start: ' + ''.join(log[-20:]))


def completion_scenario(meta, root):
    common = ('={% import {value} from "./modules/nested/value.js"; %}\n### Native collection\n'
              '< {% if(value !== 42 || client.global.get("dynamicKey").value !== 42) throw Error("common/shared scope missing"); '
              'request.variables.set("id", ["first","second"]); '
              'client.global.set("appExport", {path:"xml-feed-output/app-iteration-" + request.iteration() + ".pdf"}); %}\n'
              'GET {{host}}/iteration-binary?id={{id}}\n\n'
              '> {% client.test("app common iteration " + request.iteration(), () => client.assert(value === 42 && response.status === 200)); '
              'client.global.set("appExport", {path:"xml-feed-output/app-after-handler.pdf"}); %}\n'
              '>>! {{appExport.path}}\n')
    payload = b'{"order":42,"token":"synthetic-preview-secret-phrase"}'
    runtime = ('< {% request.variables.set("input", "runtime-payload.json"); %}\nPOST {{host}}/runtime-app-upload\n'
               'Content-Type: application/json\n\n< {{input}}\n')
    for path, data in (('api/completion.http',common.encode()), ('api/runtime.http',runtime.encode()),
                       ('api/scope-seed.http', b'GET {{host}}/scope-seed\n\n> {% client.global.set("dynamicKey", {value:42}); %}\n'),
                       ('api/http-client.env.json', json.dumps({'dev':{'host':'http://127.0.0.1:%d' % server.server_port}}).encode()),
                       ('api/runtime-payload.json',payload), ('api/http-client.private.env.json',
                        json.dumps({'dev':{'token':'synthetic-preview-secret-phrase'}}).encode())):
        with open(os.path.join(root,path),'wb') as output:
            output.write(data)
    def open_http(path):
        g.reload()
        g.palette_run(g.DEMO_TITLE)
        g.click('nav.tabs button','HTTP')
        g.ab('wait','--fn','!!document.querySelector(".hpfile")')
        g.click('.hpfile',path,'title')
        g.ab('wait','--fn','document.querySelector(".httppanel .fpath")?.textContent === ' + json.dumps(path) +
             ' && !!document.querySelector(".httprun")')
        g.ab('select', '.httppanel select.httpenv', 'dev')
    # Earlier no-environment scenarios intentionally removed both env documents.
    # Seed state again through a real run, from the exact restored env source.
    open_http('api/scope-seed.http')
    g.js('document.querySelector(".httprun").click()')
    seeded = g.ab('wait','--fn','!document.querySelector(".httprun").disabled && document.querySelector(".httppanel .httpres-h .httpst")?.classList.contains("ok")',timeout=90)
    g.expect('cross-file global seeded through native app run', seeded.get('success') and hits[-1:] == ['/scope-seed'], 'scope seed failed')
    open_http('api/completion.http')
    before = len(hits)
    g.js('document.querySelector(".httprun").click()')
    wait = g.ab('wait','--fn','!document.querySelector(".httprun").disabled && document.querySelector(".httppanel .httpres-h .httpst")?.classList.contains("ok")',timeout=90)
    g.ab('click','.hpreq-toggle[data-req="0"]')
    g.js('document.querySelectorAll(".hpdetail .httpreq-h").forEach(e => e.click())')
    g.expect('real app common imports cross-file globals and native collection tests', wait.get('success') and
             hits[before:] == ['/iteration-binary?id=first','/iteration-binary?id=second'] and
             g.js('document.querySelector(".httppanel").textContent.includes("app common iteration 0") && '
                  'document.querySelector(".httppanel").textContent.includes("app common iteration 1")'), 'collection/common/global result missing')
    for index, identity in enumerate(('first','second')):
        path = os.path.join(root,'api/xml-feed-output/app-iteration-%d.pdf' % index)
        g.expect('app iteration exact binary #' + str(index), os.path.isfile(path) and open(path,'rb').read() ==
                 BINARY + ('/iteration-binary?id=' + identity).encode(), path)
    g.expect('app export JSONPath is fixed before response code changes globals',
             not os.path.exists(os.path.join(root,'api/xml-feed-output/app-after-handler.pdf')), 'response handler changed export target')
    g.shot('http-common-globals-collection-exports')
    g.scan('HTTP common globals collection exports')
    generated = ('GET {{host}}/binary\n\n'
                 '> {% client.global.set("appDeferredExportV4", "xml-feed-output/generated-app.pdf"); %}\n'
                 '>>! {{appDeferredExportV4}}\n\n### Consume\nPOST {{host}}/generated-app-upload\n'
                 'Content-Type: application/octet-stream\n\n< xml-feed-output/generated-app.pdf\n')
    with open(os.path.join(root,'api/generated.http'),'w') as output:
        output.write(generated)
    open_http('api/generated.http')
    before, before_hits = len(uploads), len(hits)
    g.js('document.querySelector(".httprun").click()')
    wait = g.ab('wait','--fn','!document.querySelector(".httprun").disabled && document.querySelector(".httppanel .httpres-h .httpst")?.classList.contains("ok")',timeout=90)
    g.expect('actual app dynamic response export feeds a later body without replay', wait.get('success') and
             hits[before_hits:] == ['/binary','/generated-app-upload'] and uploads[before:] == [BINARY] and
             open(os.path.join(root,'api/xml-feed-output/generated-app.pdf'),'rb').read() == BINARY and
             open(os.path.join(root,'api/generated.http')).read() == generated, 'generated body or original source mismatch')
    g.ab('click','.hpreq-toggle[data-req="1"]')
    g.shot('http-dynamic-export-followed-by-upload')
    g.scan('HTTP dynamic export followed by upload')
    open_http('api/runtime.http')
    before = len(uploads)
    g.js('document.querySelector(".httprun").click()')
    wait = g.ab('wait','--fn','!document.querySelector(".httprun").disabled && document.querySelector(".httppanel .httpres-h .httpst")?.classList.contains("ok")',timeout=90)
    g.expect('human GUI runtime input needs no extra card and keeps native bytes', wait.get('success') and
             uploads[before:] == [payload] and not g.js('document.querySelector("#pending")?.textContent.includes("HTTP runtime file")'), 'GUI runtime upload failed')
    g.ab('click','.hpreq-toggle[data-req="0"]')
    g.shot('http-runtime-body-human-run')
    g.scan('HTTP runtime body human run')
    sid = meta['sessions']['grp/demo']
    mailbox = os.path.join(g.SERVE_WORK,'ph/sessions',sid,'claude/piper-mailbox')
    def agent_run(source, approve, expected=payload, expected_code=None, expected_auth=False, *, file='api/runtime.http'):
        with open(os.path.join(root,file),'w') as output:
            output.write(source)
        request_id = os.urandom(16).hex()
        os.makedirs(os.path.join(mailbox,'out'),exist_ok=True)
        temporary = os.path.join(mailbox,'out','.' + request_id + '.tmp')
        with open(temporary,'w') as output:
            json.dump({'id':request_id,'tool':'http_run','args':{'file':file,'env':'dev'}},output)
        os.rename(temporary, os.path.join(mailbox,'out',request_id + '.json'))
        response = os.path.join(mailbox,'in',request_id + '.json')
        deadline, runtime_seen, handled = time.time() + 100, False, set()
        while time.time() < deadline:
            if os.path.isfile(response):
                return json.load(open(response)), runtime_seen
            cards = [card for card in g.api('/api/state').get('pending',[]) if card['session'] == sid and card['id'] not in handled]
            for card in cards:
                handled.add(card['id'])
                if card['kind'] == 'http_runtime_code':
                    runtime_seen = True
                    g.ab('wait','--fn','document.querySelector("#pending .phead")?.textContent.includes("HTTP runtime code")')
                    entry = card.get('payload', {}).get('entry', {})
                    g.expect('agent runtime code card has exact fingerprint masked preview and no remembered grants',
                        expected_code is not None and entry.get('sha256') == hashlib.sha256(expected_code).hexdigest() and
                        len(card['payload'].get('deps', [])) == 1 and 'session_always' not in card and 'always' not in card and
                        g.js('document.querySelectorAll("#pending .pwarn").length === 2 && '
                             '!document.querySelector("#pending").textContent.includes("synthetic-preview-secret-phrase") && '
                             '!document.querySelector("#pending .pcaret") && !document.querySelector("#pending .pwhy")'), 'code card mismatch')
                    g.shot('http-runtime-code-agent-' + ('allow' if approve else 'deny'))
                    g.scan('HTTP runtime code agent ' + ('allow' if approve else 'deny'))
                    if approve is not None:
                        g.click_start('#pending .pbtns button', 'Allow' if approve else 'Deny')
                elif card['kind'] == 'http_runtime_file':
                    runtime_seen = True
                    g.ab('wait','--fn','document.querySelector("#pending .phead")?.textContent.includes("HTTP runtime file")')
                    expected_hash = hashlib.sha256(expected).hexdigest()
                    g.expect('actual runtime card shows masked bounded source preview and full fingerprint',
                        card.get('payload',{}).get('sha256') == expected_hash and
                        'session_always' not in card and 'always' not in card and
                        g.js('document.querySelector("#pending .pfact")?.textContent.includes(' + json.dumps(expected_hash) + ') && '
                             '!document.querySelector("#pending").textContent.includes("synthetic-preview-secret-phrase") && '
                             '!document.querySelector("#pending .pcaret") && ' +
                             ('document.querySelector("#pending .pbody pre")?.textContent.includes("••••")' if expected == payload else
                              '!document.querySelector("#pending .pbody pre") && document.querySelector("#pending").textContent.includes("from an earlier response; not published yet")')),
                        'runtime card differs from its one-shot snapshot')
                    decision = 'timeout' if approve is None else 'allow' if approve else 'deny'
                    if expected != payload:
                        decision = 'generated-binary-' + decision
                    g.shot('http-runtime-file-card-' + decision)
                    g.scan('HTTP runtime file card ' + decision)
                    if approve is not None:
                        g.click_start('#pending .pbtns button','Allow' if approve else 'Deny')
                else:
                    # Normal initial HTTP/host approval, also through the actual card.
                    g.ab('wait','--fn','!!document.querySelector("#pending .pbtns button")')
                    if expected_auth:
                        g.expect('managed auth initial card names the fixed issuer and never displays its client secret',
                            '/auth-v5-token' in card['summary'] and 'token/device POSTs do not follow redirects' in card['summary'] and
                            'synthetic-app-client-secret-v5' not in json.dumps(card), 'managed auth approval mismatch')
                        g.shot('http-managed-auth-initial-' + ('allow' if approve else 'deny'))
                        g.scan('HTTP managed auth initial ' + ('allow' if approve else 'deny'))
                    g.click_start('#pending .pbtns button','Deny' if expected_auth and not approve else 'Allow')
            time.sleep(.1)
        return {'ok':False,'error':'runtime app mailbox timed out'}, runtime_seen
    before = len(uploads)
    result, seen = agent_run(runtime, True)
    body = result.get('result') or result
    g.expect('actual app one-shot runtime Allow sends the selected snapshot once', seen and body.get('ok') and
             uploads[before:] == [payload], str(result)[:800])
    before, before_hits = len(uploads), len(hits)
    result, seen = agent_run(generated.replace('appDeferredExportV4', 'agentDeferredExportV4'), True, BINARY)
    body = result.get('result') or result
    g.expect('actual app agent approves exact generated binary without a text preview', seen and body.get('ok') and
             hits[before_hits:] == ['/binary','/generated-app-upload'] and uploads[before:] == [BINARY], str(result)[:800])
    earlier = '### Earlier\nGET {{host}}/earlier-runtime\n\n### Input\n' + runtime + '\n### Later\nGET {{host}}/later-runtime\n'
    before_hits, before_uploads = len(hits), len(uploads)
    result, seen = agent_run(earlier,False)
    body = result.get('result') or result
    g.expect('actual app runtime Deny stops later requests without replay', seen and not body.get('ok') and
             hits[before_hits:] == ['/earlier-runtime'] and len(uploads) == before_uploads, str(result)[:800])
    g.ab('wait','--fn','[...document.querySelectorAll(".q-gone-s")].some(e => e.textContent.includes("earlier requests"))')
    g.shot('http-runtime-file-declined-earlier-results')
    g.scan('HTTP runtime file declined earlier results')
    before_hits, before_uploads = len(hits), len(uploads)
    result, seen = agent_run(earlier, None)
    body = result.get('result') or result
    g.expect('actual app unanswered runtime card expires without upload replay or later requests', seen and
             not body.get('ok') and hits[before_hits:] == ['/earlier-runtime'] and len(uploads) == before_uploads and
             not g.api('/api/state').get('pending'), str(result)[:800])
    g.shot('http-runtime-file-timeout-earlier-results')
    g.scan('HTTP runtime file timeout earlier results')
    missing = earlier.replace('request.variables.set("input", "runtime-payload.json")', 'request.variables.set("unrelated", true)')
    before_hits, before_uploads = len(hits), len(uploads)
    result, seen = agent_run(missing, True)
    body = result.get('result') or result
    g.expect('actual app unresolved runtime input keeps exactly three request identities', not seen and not body.get('ok') and
             len(body.get('requests',[])) == 3 and hits[before_hits:] == ['/earlier-runtime'] and len(uploads) == before_uploads,
             str(result)[:800])
    open_http('api/runtime.http')
    g.ab('click','.hpreq-toggle[data-req="0"]')
    g.shot('http-runtime-unresolved-input-identities')
    g.scan('HTTP unresolved runtime input identities')
    g.expect('new app adapters keep source files unchanged', open(os.path.join(root,'api/completion.http')).read() == common and
             open(os.path.join(root,'api/runtime.http')).read() == missing and open(os.path.join(root,'api/runtime-payload.json'),'rb').read() == payload,
             'an authored HTTP/input file changed')
    cookie_scenario(root, open_http, agent_run)
    code_scenario(root, open_http, agent_run)
    auth_scenario(root, open_http, agent_run)
    certificate_scenario(root, open_http)
    browser_auth_scenario(root, open_http)
    stream_scenario(root, open_http)
    worktree_database_scenario()


def worktree_database_scenario():
    """The original report: configured test/prod are visible in a real linked worktree."""
    base = 'demo/plain'
    for cls in ('test', 'prod'):
        g.api('/api/db/save', {'project':base, 'connection':{
            'name':base + '/' + cls + '/http-shared', 'cls':cls, 'engine':'mysql',
            'host':'db.example.test', 'database':cls + '_shared', 'user':'fixture'},
            'password':'synthetic-fixture-only'})
    worktree = g.api('/api/git/worktree', {'project':base, 'action':'add', 'name':'http-db-check', 'new':True})
    key = 'demo/plain-worktrees/http-db-check'
    registry = os.path.join(g.SERVE_WORK, 'ph', 'projects.json')
    with open(registry) as stream:
        projects = json.load(stream)
    projects['projects'][key] = {'path':worktree['path']}
    with open(registry, 'w') as stream:
        json.dump(projects, stream)
    g.reload()
    g.palette_run('Database')
    g.ab('wait', '--fn', '!!document.querySelector(".db-connpage input[type=search]")')
    g.ab('fill', '.db-connpage input[type=search]', key)
    g.ab('wait', '--fn', 'document.querySelectorAll(".db-ctable .db-crow:not(.db-chead)").length === 2')
    g.expect('actual linked worktree displays inherited test and prod connections',
             g.js('(() => {const rows = [...document.querySelectorAll(".db-ctable .db-crow:not(.db-chead)")]; '
                  'return rows.length === 2 && rows.some(e => e.textContent.includes("test")) '
                  '&& rows.some(e => e.textContent.includes("prod"));})()'), 'shared remote connections are absent')
    g.expect('database credentials remain absent from published state',
             'synthetic-fixture-only' not in json.dumps(g.api('/api/state')), 'fixture credential leaked to state')
    g.shot('http-worktree-shared-test-prod')
    g.scan('HTTP task worktree database inheritance')
    g.expect('inherited connection editor opens', g.click('.db-cacts button', 'Edit'), 'Edit was unavailable')
    g.ab('wait', '--fn', '!!document.querySelector("#overlay select[name=project]")')
    g.expect('editing an inherited connection retains its original owner',
             g.js('document.querySelector("#overlay select[name=project]").disabled && '
                  'document.querySelector("#overlay select[name=project]").value === "demo/plain"'),
             'worktree edit changed the connection owner')
    g.ab('fill', '#overlay input[name=label]', 'Shared connection reviewed')
    g.shot('http-worktree-shared-connection-editor')
    g.scan('HTTP task inherited database editor')
    g.expect('inherited connection editor saves', g.click('#overlay button', 'Save'), 'Save was unavailable')
    g.ab('wait', '--fn', '!document.querySelector("#overlay select[name=project]")')
    connections = g.api('/api/db/connections?project=' + key)['connections']
    edited = [c for c in connections if c.get('label') == 'Shared connection reviewed']
    g.expect('inherited connection edit preserves one shared definition and its password',
             len(connections) == 2 and len(edited) == 1 and edited[0]['project'] == base and edited[0]['has_password'],
             'edit duplicated the definition, changed its owner or dropped the stored password')


def stream_scenario(root, open_http):
    path = 'api/finite-stream.http'
    source = ('GET {{host}}/sse-app\n\n> {%\nlet lines = [];\n'
              'response.body.onEachLine(line => lines.push(line), () => {\n'
              '  client.log("finished SSE: " + JSON.stringify(lines));\n'
              '  client.global.set("appSseFinished", "finished");\n'
              '  client.test("SSE received complete UTF-8 and final line", () => client.assert('
              'lines.includes("data: first") && lines.includes("data: é") && lines.includes("data: tail") '
              '&& lines.every(line => typeof line === "string")));\n});\n%}\n\n'
              '### after stream\nGET {{host}}/sse-after\nX-SSE: {{appSseFinished}}\n\n'
              '> {% client.test("request after SSE", () => client.assert(response.status === 200)); %}\n')
    with open(os.path.join(root, path), 'w') as output:
        output.write(source)
    open_http(path)
    before = len(hits)
    g.js('document.querySelector(".httprun").click()')
    done = g.ab('wait', '--fn', '!document.querySelector(".httprun").disabled && '
                'document.querySelector(".httppanel .httpres-h .httpst")?.classList.contains("ok")', timeout=90)
    g.expect('actual app finishes native SSE and following request without replay', done.get('success') and
             hits[before:] == ['/sse-app', '/sse-after'] and open(os.path.join(root, path)).read() == source,
             {'hits':hits[before:], 'wait':done})
    g.ab('click', '.hpreq-toggle[data-req="0"]')
    g.expect('actual app shows late native SSE test and exact decoded content', g.js('(() => {'
             'const pane = document.querySelector(".httppanel"); '
             'return [...pane.querySelectorAll(".httptests li.ok")].some(e => e.textContent.includes('
             '"SSE received complete UTF-8 and final line")) && pane.textContent.includes("data: é") '
             '&& pane.textContent.includes("data: tail");})()'), 'SSE content or native late test missing')
    g.js('[...document.querySelectorAll(".hpdetail details")].forEach(d => {'
         'if (d.querySelector("summary")?.textContent.startsWith("Script log")) d.open = true;})')
    g.shot('http-finite-native-stream')
    g.scan('HTTP finite SSE native result')


def cookie_scenario(root, open_http, agent_run):
    login = 'GET {{host}}/cookie-v5-login\n'
    echo = ('GET {{host}}/cookie-v5-echo\n\n> {% client.test("saved login cookie", () => '
            'client.assert(response.body.cookie.includes("appSession=synthetic-cookie-session-v5"))); %}\n')
    for path, source in (('api/cookie-login.http', login), ('api/cookie-echo.http', echo)):
        with open(os.path.join(root, path), 'w') as output:
            output.write(source)
    before = len(hits)
    for path in ('api/cookie-login.http', 'api/cookie-echo.http'):
        open_http(path)
        g.js('document.querySelector(".httprun").click()')
        wait = g.ab('wait','--fn','!document.querySelector(".httprun").disabled && document.querySelector(".httppanel .httpres-h .httpst")?.classList.contains("ok")',timeout=90)
        g.expect('actual app cookie native run ' + path, wait.get('success'), 'cookie run failed')
    g.expect('actual app reuses login across HTTP files without replay', hits[before:] == ['/cookie-v5-login', '/cookie-v5-echo'], hits[before:])
    g.ab('click','.hpreq-toggle[data-req="0"]')
    g.expect('actual app shows cookie regression test', 'saved login cookie' in g.js('document.querySelector(".httppanel").textContent'),
             'cookie test result missing')
    g.shot('http-cookie-login-persists-across-files')
    g.scan('HTTP cookie login persists across files')
    before = len(hits)
    isolated = ('GET {{host}}/cookie-v5-echo\n\n> {% client.test("isolated sandbox cookie", () => '
                'client.assert(response.body.cookie === "")); %}\n')
    result, seen = agent_run(isolated, True)
    body = result.get('result') or result
    g.expect('actual agent never inherits the host login cookie',
             body.get('ok') and not seen and hits[before:] == ['/cookie-v5-echo'] and
             'synthetic-cookie-session-v5' not in json.dumps(body), str(result)[:800])
    before = len(hits)
    result, seen = agent_run('GET {{host}}/cookie-v5-agent-login\n', True)
    body = result.get('result') or result
    g.expect('actual agent can obtain its own login without another runtime card', body.get('ok') and not seen and
             hits[before:] == ['/cookie-v5-agent-login'], str(result)[:800])
    before = len(hits)
    result, seen = agent_run(echo.replace('synthetic-cookie-session-v5', 'synthetic-agent-cookie-session-v5'),
                             True, file='api/cookie-agent-echo.http')
    body = result.get('result') or result
    g.expect('actual agent retains its own login across files and masks cookie credentials', body.get('ok') and not seen and
             hits[before:] == ['/cookie-v5-echo'] and 'synthetic-agent-cookie-session-v5' not in json.dumps(body), str(result)[:800])
    before = len(hits)
    open_http('api/cookie-echo.http')
    g.js('document.querySelector(".httprun").click()')
    wait = g.ab('wait','--fn','!document.querySelector(".httprun").disabled && document.querySelector(".httppanel .httpres-h .httpst")?.classList.contains("ok")',timeout=90)
    g.expect('actual host login survives the agent Set-Cookie without poisoning', wait.get('success') and
             hits[before:] == ['/cookie-v5-echo'], 'host login changed')
    g.shot('http-cookie-host-login-after-agent')
    g.scan('HTTP host cookie after sandbox login')
    g.expect('cookie app requests stay unchanged', open(os.path.join(root, 'api/cookie-login.http')).read() == login and
             open(os.path.join(root, 'api/cookie-echo.http')).read() == echo, 'cookie source changed')


def code_scenario(root, open_http, agent_run):
    directory = os.path.join(root, 'api/code-v5')
    os.makedirs(directory, exist_ok=True)
    entry = ('import {value} from "./dep.js"; request.variables.set("codeValue", value); export {value};\n'
             '// synthetic-preview-secret-phrase\n').encode()
    with open(os.path.join(directory, 'sign.js'), 'wb') as output:
        output.write(entry)
    with open(os.path.join(directory, 'dep.js'), 'w') as output:
        output.write('export const value = 42;')
    source = ('GET {{host}}/code-v5-seed\n\n> {% client.global.set("appScriptName", "code-v5/sign.js"); %}\n'
              '\n### Runtime code\n< {{appScriptName}}\nGET {{host}}/code-v5-gui\n\n'
              '> {% client.test("runtime code value", () => client.assert(request.variables.get("codeValue") === 42)); %}\n')
    with open(os.path.join(root, 'api/runtime-code.http'), 'w') as output:
        output.write(source)
    open_http('api/runtime-code.http')
    before = len(hits)
    g.js('document.querySelector(".httprun").click()')
    g.ab('wait','--fn','document.querySelector("#pending .phead")?.textContent.includes("HTTP runtime code")',timeout=60)
    card = next((p for p in g.api('/api/state').get('pending', []) if p['kind'] == 'http_runtime_code'), {})
    g.expect('GUI runtime code approval belongs only to this user run', card.get('session') == '' and
        card.get('title', '').endswith(' · your run') and card.get('payload', {}).get('by') == 'gui' and
        card.get('payload', {}).get('entry', {}).get('sha256') == hashlib.sha256(entry).hexdigest() and
        len(card.get('payload', {}).get('deps', [])) == 1 and hits[before:] == ['/code-v5-seed'], card)
    g.ab('wait','--fn','document.querySelector(".httpbusy")?.textContent.includes("waiting for your approval")')
    g.expect('GUI runtime code card shows two warnings full hashes and complete collapsed import list',
        g.js('document.querySelectorAll("#pending .pwarn").length === 2 && '
             'document.querySelector("#pending .pfact").textContent.includes(' + json.dumps(hashlib.sha256(entry).hexdigest()) + ') && '
             '!!document.querySelector("#pending details.pdeps") && !document.querySelector("#pending details.pdeps").open && '
             '!document.querySelector("#pending .pcaret") && !document.querySelector("#pending .pwhy") && '
             '!document.querySelector("#pending").textContent.includes("synthetic-preview-secret-phrase")'), 'GUI code rendering mismatch')
    g.shot('http-runtime-code-gui-variable-script-card')
    # Exactly one fetch is deliberately held: this GUI /api/http/run waits on the
    # visible card. Keep every layout/error/animation check and wait for quiet frames;
    # unrelated inflight requests still prevent settling. The ordinary harness rule
    # correctly cannot settle while waiting for an unanswered GUI run.
    settled = g.SETTLED
    g.SETTLED = settled.replace('!state.inflight',
        'state.inflight === 1 && !!document.querySelector("#pending .pending[data-http-run]")')
    try:
        g.scan('HTTP runtime code GUI variable script card')
    finally:
        g.SETTLED = settled
    g.click_start('#pending .pbtns button', 'Allow')
    done = g.ab('wait','--fn','!document.querySelector(".httprun").disabled && document.querySelector(".httpres-h .httpst")?.classList.contains("ok")',timeout=90)
    g.expect('GUI runtime script runs after one concrete approval without replay', done.get('success') and
        hits[before:] == ['/code-v5-seed', '/code-v5-gui'], 'GUI code run failed')
    g.ab('click', '.hpreq-toggle[data-req="1"]')
    g.shot('http-runtime-code-gui-native-result')
    g.scan('HTTP runtime code GUI native result')
    computed = ('< {% const path = "./code-v5/sign.js"; await import(path); %}\nGET {{host}}/code-v5-agent\n\n'
                '> {% client.test("agent runtime code", () => client.assert(request.variables.get("codeValue") === 42)); %}\n')
    before = len(hits)
    result, seen = agent_run(computed, True, expected_code=entry)
    body = result.get('result') or result
    g.expect('agent computed code works through actual approval and native engine', seen and body.get('ok') and
        hits[before:] == ['/code-v5-agent'], str(result)[:800])
    before = len(hits)
    result, seen = agent_run('GET {{host}}/code-v5-earlier\n\n### Computed\n' + computed, False, expected_code=entry)
    body = result.get('result') or result
    g.expect('agent code denial preserves earlier response and never sends the later request', seen and not body.get('ok') and
        hits[before:] == ['/code-v5-earlier'], str(result)[:800])
    g.expect('runtime code keeps original HTTP and JavaScript files unchanged',
        open(os.path.join(root, 'api/runtime-code.http')).read() == source and open(os.path.join(directory, 'sign.js'), 'rb').read() == entry,
        'runtime code source changed')


def auth_scenario(root, open_http, agent_run):
    env_path = os.path.join(root, 'api/http-client.env.json')
    private_path = os.path.join(root, 'api/http-client.private.env.json')
    original, original_private = open(env_path).read(), open(private_path).read()
    public, private = json.loads(original), json.loads(original_private)
    public['dev']['Security'] = {'Auth':{'app':{'Type':'OAuth2', 'Grant Type':'Client Credentials',
        'Client ID':'synthetic-app-client-v5', 'Token URL':'http://127.0.0.1:%d/auth-v5-token' % server.server_port}}}
    private['dev']['Security'] = {'Auth':{'app':{'Client Secret':'synthetic-app-client-secret-v5'}}}
    source = ('GET {{host}}/auth-v5-resource\nAuthorization: Bearer {{$auth.token("app")}}\n\n'
              '> {% client.test("managed app token", () => client.assert(response.body.auth === "Bearer synthetic-app-access-v5")); %}\n'
              '\n### ID token in same run\nGET {{host}}/auth-v5-resource-id\nAuthorization: Bearer {{$auth.idToken("app")}}\n')
    for path, content in ((env_path, json.dumps(public)), (private_path, json.dumps(private)),
                          (root + '/api/managed-auth.http', source)):
        with open(path, 'w') as output:
            output.write(content)
    try:
        open_http('api/managed-auth.http')
        before = len(hits)
        g.js('document.querySelector(".httprun").click()')
        wait = g.ab('wait','--fn','!document.querySelector(".httprun").disabled && document.querySelector(".httpres-h .httpst")?.classList.contains("ok")',timeout=90)
        g.expect('GUI original auth.token/idToken performs one issuer request and both native resource requests', wait.get('success') and
            hits[before:] == ['/auth-v5-token', '/auth-v5-resource', '/auth-v5-resource-id'], repr(hits[before:]))
        g.ab('click', '.hpreq-toggle[data-req="0"]')
        g.expect('GUI displays the real native managed-auth test', 'managed app token' in g.js('document.querySelector(".httppanel").textContent'), 'managed auth native test missing')
        g.shot('http-managed-auth-gui-native-result')
        g.scan('HTTP managed auth GUI native result')
        before = len(hits)
        result, _ = agent_run(source, True, expected_auth=True)
        body = result.get('result') or result
        g.expect('agent managed auth keeps token acquisition inside the approved run and masks its echo', body.get('ok') and
            hits[before:] == ['/auth-v5-token', '/auth-v5-resource', '/auth-v5-resource-id'] and
            not any(value in json.dumps(result) for value in ('synthetic-app-access-v5', 'synthetic-app-id-v5', 'synthetic-app-client-secret-v5')), str(result)[:800])
        before = len(hits)
        result, _ = agent_run(source, False, expected_auth=True)
        body = result.get('result') or result
        g.expect('agent initial auth Deny sends neither token acquisition nor resource requests', not body.get('ok') and
            len(hits) == before, str(result)[:800])
        g.expect('managed auth keeps the original HTTP source unchanged', open(root + '/api/managed-auth.http').read() == source, 'auth source changed')
    finally:
        for path, content in ((env_path, original), (private_path, original_private)):
            with open(path, 'w') as output:
                output.write(content)


def browser_auth_scenario(root, open_http):
    section = next(item for item in g.api('/api/settings')['sections'] if item['id'] == 'http')
    previous = {field['key']:field['value'] for field in section['fields']}
    # The settings scenario deliberately chose 15 s. Browser sign-in includes
    # screenshots, scans and provider-tab interaction; give this fixture its
    # own budget so parallel unit load cannot exhaust the HTTP run mid-login.
    try:
        g.api('/api/settings', {'section':'http', 'values':dict(previous, timeout=90)})
        browser_auth_interactions(root, open_http)
    finally:
        g.api('/api/settings', {'section':'http', 'values':previous})


def browser_auth_interactions(root, open_http):
    import socket
    base = 'http://127.0.0.1:%d' % server.server_port
    def tabs():
        return (g.ab('tab','list').get('data') or {}).get('tabs', [])
    def tab_id(row):
        return str(row.get('id') or row.get('targetId'))
    for grant in ('Authorization Code','Device Authorization'):
        browser_auth['authorized'] = False
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0))
            callback = 'http://127.0.0.1:%d/callback' % listener.getsockname()[1]
        config = {'Type':'OAuth2','Grant Type':grant,'Client ID':'app-browser-fixture','Client Credentials':'none',
                  'Auth URL':base + '/auth-browser-authorize','Device Auth URL':base + '/auth-browser-device',
                  'Token URL':base + '/auth-browser-token','Redirect URL':callback,'PKCE':True,
                  'Start Polling After Browser':grant == 'Device Authorization'}
        public_path = root + '/api/http-client.env.json'
        public = json.load(open(public_path))
        public['dev']['Security'] = {'Auth':{'browser':config}}
        with open(public_path,'w') as output:
            json.dump(public,output)
        source = ('GET {{host}}/auth-v5-resource-browser\nAuthorization: Bearer {{$auth.token("browser")}}\n\n'
                  '> {% client.test("browser app token", () => client.assert(response.body.auth === "Bearer synthetic-app-access-v5")); %}\n')
        with open(root + '/api/browser-auth.http','w') as output:
            output.write(source)
        open_http('api/browser-auth.http')
        for outcome in ('deny','allow'):
            before = len(hits)
            g.js('document.querySelector(".httprun").click()')
            g.ab('wait','--fn','document.querySelector("#pending .phead")?.textContent.includes("HTTP sign-in")',timeout=60)
            card = next((p for p in g.api('/api/state').get('pending',[]) if p['kind'] == 'http_auth_signin'),{})
            g.expect('GUI ' + grant + ' ready card belongs to this run and carries no protocol secret',
                     card.get('session') == '' and card.get('payload',{}).get('state') == 'ready' and
                     not any(secret in json.dumps(card) for secret in ('code_verifier','app-private-device-code42','app-browser-code42')) and
                     '/auth-v5-resource-browser' not in hits[before:], card)
            g.shot('http-browser-' + grant + '-ready-' + outcome)
            settled = g.SETTLED
            g.SETTLED = settled.replace('!state.inflight','state.inflight === 1 && !!document.querySelector("#pending .pending[data-http-run]")')
            try:
                g.scan('HTTP ' + grant + ' ready ' + outcome)
            finally:
                g.SETTLED = settled
            if outcome == 'deny':
                g.ab('click', '#pending .pbtns button:first-child')
            else:
                app_tab = next(row for row in tabs() if ':17981/' in row.get('url',''))
                g.ab('click', '#pending .pbtns button.primary')
                deadline = time.monotonic() + 10
                popup = None
                while time.monotonic() < deadline:
                    popup = next((row for row in tabs() if row.get('url','').startswith(base + '/auth-browser-')),None)
                    if popup:
                        break
                    time.sleep(.1)
                g.expect('Open sign-in really opens the approved provider in a browser tab', bool(popup), 'browser popup missing')
                g.ab('tab',tab_id(app_tab))
                g.ab('wait','--fn','document.querySelector("#pending")?.textContent.includes("Waiting for the sign-in in your browser")',timeout=10)
                g.shot('http-browser-' + grant + '-waiting')
                g.SETTLED = settled.replace('!state.inflight','state.inflight === 1 && !!document.querySelector("#pending .pending[data-http-run]")')
                try:
                    g.scan('HTTP ' + grant + ' waiting')
                finally:
                    g.SETTLED = settled
                g.expect('opening a provider does not approve the protected request', '/auth-v5-resource-browser' not in hits[before:], hits[before:])
                if popup:
                    g.ab('tab',tab_id(popup))
                    g.ab('click','#fixture-authorize')
                    g.ab('tab',tab_id(app_tab))
                    if grant == 'Device Authorization':
                        g.expect('device polling waits for the actual browser tab to close',
                                 '/auth-browser-token' not in hits[before:] and '/auth-v5-resource-browser' not in hits[before:], hits[before:])
                        g.ab('tab','close',tab_id(popup))
                        popup = None
            done = g.ab('wait','--fn','!document.querySelector(".httprun").disabled && !document.querySelector("#pending .phead")',timeout=90)
            g.expect('GUI ' + grant + ' ' + outcome + ' result', done.get('success') and
                     (hits[before:].count('/auth-v5-resource-browser') == 1 if outcome == 'allow' else '/auth-v5-resource-browser' not in hits[before:]) and
                     (g.js('document.querySelector(".httpres-h .httpst")?.classList.contains("ok")') if outcome == 'allow' else True),hits[before:])
            g.shot('http-browser-' + grant + '-result-' + outcome)
            g.scan('HTTP ' + grant + ' result ' + outcome)
            if outcome == 'allow' and popup:
                g.ab('tab','close',tab_id(popup))


def certificate_scenario(root, open_http):
    # demo_project protects exactly the ROOT private env in .hole/settings.json.
    # Its api/ env files are intentionally visible/writable to exercise agent cards.
    directory = root
    os.makedirs(directory, exist_ok=True)
    certificate, key = directory + '/client.pem', directory + '/client.key'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', key,
                    '-out', certificate, '-days', '1', '-subj', '/CN=piper-mtls-fixture'], check=True, capture_output=True)
    secure = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    context.load_verify_locations(certificate)
    context.verify_mode = ssl.CERT_REQUIRED
    secure.socket = context.wrap_socket(secure.socket, server_side=True)
    threading.Thread(target=secure.serve_forever, daemon=True).start()
    source = 'GET {{host}}/mtls-app\n\n> {% client.test("mTLS authenticated", () => client.assert(response.status === 200)); %}\n'
    documents = {'client-certificate.http':source,
                 'http-client.env.json':json.dumps({'dev':{'host':'https://localhost:%d' % secure.server_port}}),
                 'http-client.private.env.json':json.dumps({'dev':{'SSLConfiguration':{
                     'clientCertificate':'client.pem', 'clientCertificateKey':'client.key', 'verifyHostCertificate':False}}})}
    for filename, content in documents.items():
        with open(os.path.join(directory, filename), 'w') as output:
            output.write(content)
    try:
        open_http('client-certificate.http')
        before = len(hits)
        g.js('document.querySelector(".httprun").click()')
        wait = g.ab('wait','--fn','!document.querySelector(".httprun").disabled && document.querySelector(".httppanel .httpres-h .httpst")?.classList.contains("ok")',timeout=90)
        g.expect('actual app authenticates with the private env client certificate without editing HTTP source',
                 wait.get('success') and hits[before:] == ['/mtls-app'] and open(directory + '/client-certificate.http').read() == source,
                 'client identity was absent, request replayed, or source changed')
        g.ab('click','.hpreq-toggle[data-req="0"]')
        g.expect('actual app shows the native mTLS response test', 'mTLS authenticated' in g.js('document.querySelector(".httppanel").textContent'),
                 'native client certificate test is absent')
        g.shot('http-configured-client-certificate')
        g.scan('HTTP configured client certificate')
        password = 'app-certificate-passphrase-v5'
        subprocess.run(['openssl', 'pkcs12', '-export', '-inkey', key, '-in', certificate,
                        '-out', directory + '/encrypted.p12', '-passout', 'pass:' + password], check=True, capture_output=True)
        with open(directory + '/http-client.private.env.json', 'w') as output:
            json.dump({'dev':{'SSLConfiguration':{'clientCertificate':'encrypted.p12', 'hasCertificatePassphrase':True,
                                                  'verifyHostCertificate':False}}}, output)
        for outcome in ('deny', 'wrong', 'allow'):
            before = len(hits)
            g.js('document.querySelector(".httprun").click()')
            g.ab('wait', '--fn', '!!document.querySelector("#pending input[data-http-passphrase]")', timeout=60)
            cards = [p for p in g.api('/api/state').get('pending', []) if p['kind'] == 'http_cert_passphrase']
            g.expect('certificate prompt is GUI-owned and mandatory ' + outcome,
                     len(cards) == 1 and cards[0].get('session') == '' and not cards[0].get('always') and not cards[0].get('session_always') and
                     password not in json.dumps(cards) and hits[before:] == [], cards)
            g.expect('empty passphrase cannot approve and has no remembered grant',
                     g.js('document.querySelector("#pending button.primary").disabled && !document.querySelector("#pending .pcaret")'), 'empty password accepted')
            if outcome != 'deny':
                g.ab('fill', '#pending input[data-http-passphrase]', password if outcome == 'allow' else 'wrong-app-password')
                g.expect('passphrase is never copied to published state', password not in json.dumps(g.api('/api/state')), 'credential leaked to state')
            g.shot('http-certificate-passphrase-' + outcome)
            settled = g.SETTLED
            g.SETTLED = settled.replace('!state.inflight', 'state.inflight === 1 && !!document.querySelector("#pending .pending[data-http-run]")')
            try:
                g.scan('HTTP certificate passphrase ' + outcome)
            finally:
                g.SETTLED = settled
            g.ab('click', '#pending .pbtns button:first-child' if outcome == 'deny' else '#pending .pbtns button.primary')
            done = g.ab('wait', '--fn', '!document.querySelector(".httprun").disabled && !document.querySelector("#pending input[data-http-passphrase]")', timeout=90)
            text = g.js('document.querySelector(".httppanel").textContent')
            g.expect('encrypted certificate request outcome ' + outcome, done.get('success') and
                     (hits[before:] == ['/mtls-app'] if outcome == 'allow' else hits[before:] == []) and
                     (g.js('document.querySelector(".httpres-h .httpst")?.classList.contains("ok")') if outcome == 'allow' else
                      ('not given' in text if outcome == 'deny' else 'passphrase is wrong' in text)), text)
            g.shot('http-certificate-result-' + outcome)
            g.scan('HTTP encrypted certificate result ' + outcome)
    finally:
        secure.shutdown()
        secure.server_close()


def scenario(widths):
    g.CTX.update(preset='all', width=1024)
    meta = json.load(open(os.path.join(g.SERVE_WORK, 'serve.json')))
    root = meta['projects']['grp/demo']
    source = ('@handler = ./modules/handler.js\n### External module\n# @no-log\nGET {{host}}/external\n\n> {{handler}}\n'
              '>> xml-feed-output/cs/feed.xml\n\n'
              '### Inline module\nGET {{host}}/inline\n\n'
              '> {% import {value} from "./modules/nested/value.js"; '
              'client.test("inline module", () => client.assert(value === 42 && client.global.get("dynamicKey").value === 42)); %}\n')
    files = {
        'api/demo.http': source,
        'api/http-client.env.json': json.dumps({'dev': {'host': 'http://127.0.0.1:%d' % server.server_port}}),
        'api/modules/handler.js': 'import {check} from "./check.js"; check(); '
            'client.global.set("dynamic" + "Key", {value:42}); client.log("app response log");',
        'api/modules/check.js': 'import {value} from "./nested/value.js"; export function check() {'
            'client.test("nested module", () => client.assert(value === 42 && response.status === 200));}',
        'api/modules/nested/value.js': 'import "../check.js"; export const value = 42;'}
    for path, content in files.items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as output:
            output.write(content)
    g.ab('set', 'viewport', '1024', '768')
    g.api('/api/features', {'preset': 'all'})
    g.reload()
    g.palette_run(g.DEMO_TITLE)
    # Dismiss only this isolated harness's seeded global-agent approval; it would otherwise
    # cover the result's sharing control at 1024px. No live session/provider is involved.
    g.click_start('#pending button', 'Deny')
    g.expect('HTTP tab opens', g.click('nav.tabs button', 'HTTP'), 'HTTP tab unavailable')
    g.ab('wait', '--fn', '!!document.querySelector(".hpfile")')
    if os.environ.get('PIPER_E2E_HTTP_APP_DB_ONLY') == '1':
        worktree_database_scenario()
        return
    if os.environ.get('PIPER_E2E_HTTP_APP_AUTH_ONLY') == '1' or os.environ.get('PIPER_E2E_HTTP_APP_STREAM_ONLY') == '1':
        def open_auth(path):
            g.reload()
            g.palette_run(g.DEMO_TITLE)
            g.click('nav.tabs button','HTTP')
            g.ab('wait','--fn','!!document.querySelector(".hpfile")')
            g.expect('auth fixture opens ' + path, g.click('.hpfile',path,'title'), 'HTTP fixture not listed')
            g.ab('wait','--fn','document.querySelector(".httppanel .fpath")?.textContent === ' + json.dumps(path) + ' && !!document.querySelector(".httprun")')
            g.ab('select','.httppanel select.httpenv','dev')
        if os.environ.get('PIPER_E2E_HTTP_APP_STREAM_ONLY') == '1':
            stream_scenario(root, open_auth)
        else:
            certificate_scenario(root, open_auth)
            browser_auth_scenario(root, open_auth)
        return
    g.click('.hpfile', 'demo.http')
    g.expect('native HTTP run starts', g.js('(() => {const e = [...document.querySelectorAll("button")].find(e => '
             'e.textContent.trim().startsWith("Run all on dev")); if (!e || e.disabled) return false; '
             'e.click(); return true;})()'), 'Run button unavailable')
    wait = g.ab('wait', '--fn', '[...document.querySelectorAll(".httppanel .httpcode")].filter(e => e.textContent === "200").length === 2', timeout=90)
    g.expect('native HTTP result visible', wait.get('success'), str(wait.get('error', 'no saved result')))
    g.ab('click', '.hpreq-toggle[data-req="0"]')
    g.expect('native export and handler visible', g.js('document.querySelector(".httppanel").textContent.includes('
             '"Saved to api/xml-feed-output/cs/feed.xml") && [...document.querySelectorAll(".httptests li.ok")].some('
             'e => e.textContent.includes("nested module"))'), 'saved response or external test missing')
    g.expect('response script log is distinct from user tests', g.js('(() => {const d = [...document.querySelectorAll(' +
             '".hpdetail details")].find(d => d.querySelector("summary")?.textContent === "Script log (1 line)"); '
             'if (!d) return false; d.open = true; return d.querySelector("pre")?.textContent === "app response log\\n" && '
             '[...document.querySelectorAll(".httptests li")].every(e => !e.textContent.includes("__piper_g__"));})()'), 'response log missing or counted as a test')
    g.shot('http-real-native-external-module-export')
    g.expect('native requests sent once in order', hits == ['/external', '/inline'], repr(hits))
    output = os.path.join(root, 'api/xml-feed-output/cs/feed.xml')
    g.expect('native exact XML bytes', os.path.isfile(output) and open(output, 'rb').read() == BODY, 'XML mismatch')
    g.expect('authored HTTP and JS sources preserved', all(open(os.path.join(root, p)).read() == s
             for p, s in files.items()), 'a source changed')
    runs = g.api('/api/http/runs?project=grp%2Fdemo&path=api%2Fdemo.http')['runs']
    g.ab('click', '.hpreq-toggle[data-req="1"]')
    g.expect('native inline module test visible and passes', g.js('[...document.querySelectorAll(".httptests li.ok")].some('
             'e => e.textContent.includes("inline module"))'),
             'native handler tests missing from the GUI')
    g.expect('no-log batch absent from persisted history', runs == [], repr(runs))
    g.expect('no-log result files absent', not any(name.endswith('.json') for name in
             os.listdir(os.path.join(g.SERVE_WORK, 'ph/http/runs'))) if
             os.path.isdir(os.path.join(g.SERVE_WORK, 'ph/http/runs')) else True, 'response was persisted')
    g.shot('http-real-native-modules-and-export')
    g.scan('HTTP native modules and export')
    g.expect('original HTTP opens in Files', g.click('button', 'Open in Files'), 'file editor did not open')
    g.ab('wait', '--fn', '[...document.querySelectorAll(".httprun")].some(e => e.getBoundingClientRect().width > 0)')
    g.expect('file editor native run starts', g.js('(() => {const e = [...document.querySelectorAll(".httprun")].find('
             'e => e.getBoundingClientRect().width > 0); if (!e || e.disabled) return false; e.click(); return true;})()'),
             'file editor run unavailable')
    wait = g.ab('wait', '--fn', 'document.querySelectorAll(".httpres .httpreq-h").length === 2', timeout=90)
    g.expect('file editor native export result', wait.get('success'), str(wait.get('error', 'missing export')))
    g.js('document.querySelector(".httpres .httpreq-h")?.click()')
    g.expect('file editor native export visible', g.js('document.querySelector(".httpres")?.textContent.includes("feed-1.xml")'),
             'saved export missing from the expanded result')
    g.expect('no-log sharing is disabled and explained', g.js('(() => {const e = document.querySelector('
             '".httpres .httpshare input"); return !!e && e.disabled && !e.checked && '
             'e.title.includes("@no-log") && e.title.includes("cannot be shared");})()'),
             'non-persistent result still offers sharing')
    g.js('document.querySelector(".httpres .httpshare input").dispatchEvent(new Event("change"))')
    suffix = os.path.join(root, 'api/xml-feed-output/cs/feed-1.xml')
    g.expect('file editor original redirect suffix and exact bytes', os.path.isfile(suffix) and
             open(suffix, 'rb').read() == BODY, 'suffix output mismatch')
    g.expect('both GUI runs send each request exactly once', hits == ['/external', '/inline'] * 2, repr(hits))
    g.expect('file editor no-log creates no history',
             g.api('/api/http/runs?project=grp%2Fdemo&path=api%2Fdemo.http')['runs'] == [], 'history was persisted')
    g.shot('http-real-native-file-editor')
    g.scan('HTTP native file editor')
    with open(os.path.join(root, 'api/modules/nested/value.js'), 'w') as output:
        output.write('import "../../../../outside.js";')
    g.js('(() => {const e = [...document.querySelectorAll(".httprun")].find('
         'e => e.getBoundingClientRect().width > 0); e.click();})()')
    rejected = g.ab('wait', '--fn', '[...document.querySelectorAll(".httperr")].some('
                    'e => e.textContent.includes("inside the project"))')
    g.expect('unsafe module rejected in app before any HTTP request', rejected.get('success') and
             hits == ['/external', '/inline'] * 2, repr(hits))
    g.shot('http-real-native-unsafe-module-refused')
    # A normal persisted run must retain the same sharing control and still update history.
    with open(os.path.join(root, 'api/modules/nested/value.js'), 'w') as output:
        output.write(files['api/modules/nested/value.js'])
    with open(os.path.join(root, 'api/demo.http'), 'w') as output:
        output.write(source.replace('# @no-log\n', ''))
    record = g.api('/api/http/run', {'project': 'grp/demo', 'path': 'api/demo.http', 'env': 'dev'})
    g.expect('ordinary result persists successfully', record.get('ok') and not record.get('no_log'), 'native run failed')
    g.reload()
    g.palette_run(g.DEMO_TITLE)
    g.click('nav.tabs button', 'HTTP')
    g.ab('wait', '--fn', '!!document.querySelector(".hpfile")')
    g.click('.hpfile', 'demo.http')
    g.click('button', 'Open in Files')
    g.ab('wait', '--fn', '!!document.querySelector(".httpres .httpshare input")')
    g.expect('ordinary sharing remains enabled', g.js('(() => {const e = document.querySelector('
             '".httpres .httpshare input"); return !!e && !e.disabled && e.checked;})()'), 'saved result sharing unavailable')
    clicked = g.ab('click', '.httpres .httpshare input')
    g.expect('ordinary sharing checkbox clickable', clicked.get('success'), str(clicked.get('error', 'click failed')))
    g.settle()
    g.expect('ordinary sharing updates the stored record',
             g.api('/api/http/run?id=' + record['id'])['share'] is False, 'share update was not persisted')
    g.expect('sharing creates no extra native requests', hits == ['/external', '/inline'] * 3, repr(hits))
    g.shot('http-real-native-saved-sharing')
    g.scan('HTTP ordinary result sharing')

    record = g.api('/api/http/run', {'project': 'grp/demo', 'path': 'api/demo.http', 'env': 'dev', 'index': 1})
    g.expect('computed global restores for a separate native run in the app', record.get('ok') and
             any(t['name'] == 'inline module' and t['ok'] for t in record['requests'][0]['tests']), 'computed global was lost')

    graph_source = '### Imported batch\nrun ./graph/children.http\n'
    child_source = ('### Same name\n< {% client.log("before binary"); %}\nGET {{host}}/binary\n\n'
                    '> {% client.test("binary callback", () => {client.log("inside binary callback"); '
                    'client.assert(response.status === 200);}); %}\n>>! output.pdf\n\n'
                    '### Same name\nGET {{host}}/after-binary\n\n'
                    '> {% client.log("before exit"); client.global.set("appExit", 42); client.exit(); '
                    'client.test("canceled", () => client.assert(false)); %}\n')
    os.makedirs(os.path.join(root, 'api/graph'), exist_ok=True)
    for path, content in [('api/batch.http', graph_source), ('api/graph/children.http', child_source)]:
        with open(os.path.join(root, path), 'w') as output:
            output.write(content)
    before = len(hits)
    g.reload()
    g.palette_run(g.DEMO_TITLE)
    g.click('nav.tabs button', 'HTTP')
    g.ab('wait', '--fn', '!!document.querySelector(".hpfile")')
    g.click('.hpfile', 'api/batch.http', 'title')
    g.ab('wait', '--fn', 'document.querySelector(".httppanel .fpath")?.textContent === "api/batch.http" && !!document.querySelector(".httprun")')
    g.js('document.querySelector(".httprun").click()')
    wait = g.ab('wait', '--fn', '!document.querySelector(".httprun").disabled && document.querySelector(".hpreq .httpcode")?.textContent === "2/2"', timeout=90)
    g.expect('imported batch groups actual responses under its RUN row', wait.get('success') and
             hits[before:] == ['/binary', '/after-binary'], repr(hits[before:]))
    g.ab('click', '.hpreq-toggle[data-req="0"]')
    g.expect('both same-named imported responses are separately accessible',
             g.js('document.querySelectorAll(".hpdetail .httpreq-h").length === 2'), 'imported result was lost by name')
    g.js('document.querySelectorAll(".hpdetail .httpreq-h").forEach(e => e.click())')
    g.expect('native callback and exit logs are visible without invented canceled tests',
             g.js('(() => {document.querySelectorAll(".hpdetail details").forEach(e => e.open = true); '
                  'const t=document.querySelector(".hpdetail").textContent; return t.includes("before binary") && '
                  't.includes("inside binary callback") && t.includes("before exit") && !t.includes("canceled") && '
                  't.includes("api/graph/children.http:");})()'), 'runtime metadata or source link missing')
    exported = os.path.join(root, 'api/graph/output.pdf')
    g.expect('app binary export is byte-exact and original scripts unchanged', os.path.isfile(exported) and
             open(exported, 'rb').read() == BINARY and open(os.path.join(root, 'api/batch.http')).read() == graph_source and
             open(os.path.join(root, 'api/graph/children.http')).read() == child_source, 'binary or source mismatch')
    g.shot('http-imported-batch-binary-callback-exit')
    g.scan('HTTP imported batch identities and binary export')
    g.expect('imported source link opens the authored file', g.js('(() => {const e=[...document.querySelectorAll(".hpdetail button")].find('
             'e=>e.textContent.startsWith("api/graph/children.http:")); if(!e)return false; e.click(); return true;})()'), 'source link missing')
    g.ab('wait', '--fn', 'document.querySelector(".ftab.on")?.getAttribute("title") === "api/graph/children.http" && !!document.querySelector(".httpbar select.httpenv")')
    before = len(hits)
    saved = g.api('/api/http/run', {'project':'grp/demo', 'path':'api/graph/children.http', 'env':'dev',
                                   'index':0, 'save_to':'development/native-scripted.pdf'})
    g.expect('app save_to preserves native tests and exact binary with one transmission', saved.get('ok') and
             any(t['name']=='binary callback' and t['ok'] for t in saved['requests'][0]['tests']) and
             hits[before:]==['/binary'] and open(os.path.join(root, 'development/native-scripted.pdf'), 'rb').read()==BINARY, str(saved))
    with open(os.path.join(root, 'api/batch.http'), 'w') as output:
        output.write(graph_source + '\n# edited after result\n')
    g.reload()
    g.palette_run(g.DEMO_TITLE)
    g.click('nav.tabs button', 'HTTP')
    g.ab('wait', '--fn', '!!document.querySelector(".hpfile")')
    g.click('.hpfile', 'api/batch.http', 'title')
    g.ab('wait', '--fn', 'document.querySelector(".httppanel .fpath")?.textContent === "api/batch.http"')
    g.ab('click', '.hpreq-toggle[data-req="0"]')
    g.expect('source edit detaches stale batch results from request rows', g.js('document.querySelectorAll(".hpdetail .httpreq-h").length===0 && '
             'document.querySelector(".hpdetail").textContent.includes("file changed")'), 'stale responses matched changed source')
    g.shot('http-imported-batch-stale-source')
    g.scan('HTTP stale batch source')

    flow = ('@output = xml-feed-output/static-flow.xml\n### Static export\nGET http://127.0.0.1:%d/source\n\n>>! {{output}}\n\n'
            '### Consume\nPOST http://127.0.0.1:%d/consume\nContent-Type: application/xml\n\n< ./xml-feed-output/static-flow.xml\n') % (server.server_port, server.server_port)
    with open(os.path.join(root, 'api/no-env.HTTP'), 'w') as output:
        output.write(flow)
    g.reload()
    g.palette_run(g.DEMO_TITLE)
    g.click('nav.tabs button', 'HTTP')
    g.ab('wait', '--fn', '!!document.querySelector(".hpfile")')
    g.expect('literal URL file selected', g.click('.hpfile', 'api/no-env.HTTP', 'title'), 'no-env file not selected')
    g.ab('wait', '--fn', 'document.querySelector(".httppanel .fpath")?.textContent === "api/no-env.HTTP" && !!document.querySelector(".httppanel select.httpenv")')
    g.ab('select', '.httppanel select.httpenv', '')
    g.expect('No environment is an enabled production choice', g.js('document.querySelector(".httppanel select.httpenv").value === "" && '
             'document.querySelector(".httppanel .httpbar").dataset.cls === "prod" && !document.querySelector(".httppanel .httprun").disabled'), 'no-env still disabled')
    g.js('document.querySelector(".httppanel .httprun").click()')
    g.expect('no-env writes require explicit production acknowledgement', g.js('document.querySelector(".modal").textContent.includes("Production · no environment") && '
             'document.querySelector(".modal button.danger").disabled && document.querySelector(".modal").textContent.includes("certificate check is on")'), 'production protection missing')
    g.shot('http-no-env-production-confirmation')
    g.ab('check', '.modal .httpack input')
    g.js('document.querySelector(".modal button.danger").click()')
    wait = g.ab('wait', '--fn', '[...document.querySelectorAll(".httppanel .httpcode")].filter(e => e.textContent === "200").length === 2 && !document.querySelector(".httppanel .httprun").disabled', timeout=90)
    g.expect('no-env static export and upload succeed', wait.get('success') and uploads == [BODY] and
             hits[-2:] == ['/source', '/consume'], repr({'hits': hits, 'uploads': uploads}))
    g.expect('original no-env request file unchanged', open(os.path.join(root, 'api/no-env.HTTP')).read() == flow, 'source changed')
    g.ab('click', '.hpreq-toggle[data-req="0"]')
    g.shot('http-no-env-native-result')
    g.scan('HTTP no-env native export and upload')
    g.click('button', 'Open in Files')
    g.ab('wait', '--fn', 'document.querySelector(".ftab.on")?.getAttribute("title") === "api/no-env.HTTP" && !!document.querySelector(".httpbar select.httpenv")')
    # A newly opened view uses the normal dev preselection; no-env was never saved.
    g.expect('new editor view retains normal environment preselection', g.js('document.querySelector(".httpbar select.httpenv").value === "dev"'), 'no-env leaked into persistent selection')
    g.ab('select', '.httpbar select.httpenv', '')
    g.js('document.querySelector(".httpcfg").click()')
    g.ab('wait', '--fn', 'document.querySelector(".modal")?.textContent.includes("Edit environments")')
    g.expect('no-env Environment button opens existing JSON editor', g.js('!!document.querySelector(".modal textarea")'), 'environment editor unavailable')
    g.shot('http-no-env-environment-editor')
    g.click('.modal button', 'Cancel')

    g.palette_run('Settings')
    g.ab('wait', '--fn', '!!document.querySelector(".st-side")')
    g.click('.st-side button', 'HTTP requests')
    g.ab('wait', '--fn', '[...document.querySelectorAll("input[type=number]")].length === 3')
    g.expect('all three timeout controls are available', g.js('document.body.textContent.includes("Connection timeout (milliseconds)") && '
             'document.body.textContent.includes("Socket timeout (milliseconds)") && document.body.textContent.toLowerCase().includes("applies from the next run")'), 'timeout controls missing')
    g.ab('fill', '#st-http-timeout', '0')
    g.expect('timeout range blocks Save before any write', g.js('document.querySelector(".st-save").disabled && '
             '!!document.querySelector("#st-error-http-timeout").textContent'), 'invalid timeout can be saved')
    for key, value in (('timeout', 15), ('connect_timeout_ms', 2500), ('socket_timeout_ms', 2500)):
        g.ab('fill', '#st-http-' + key, str(value))
    g.ab('click', '.st-save')
    g.settle()
    fields = next(s for s in g.api('/api/settings')['sections'] if s['id'] == 'http')['fields']
    g.expect('timeout settings persist from the real GUI', {f['key']: f['value'] for f in fields} ==
             {'timeout': 15, 'connect_timeout_ms': 2500, 'socket_timeout_ms': 2500}, repr(fields))
    g.shot('http-timeout-settings')
    g.scan('HTTP timeout settings')

    # No environment file anywhere: the only option remains runnable in Files too.
    os.unlink(os.path.join(root, 'api/http-client.env.json'))
    os.unlink(os.path.join(root, 'http-client.env.json'))
    g.reload()
    g.palette_run(g.DEMO_TITLE)
    g.click('nav.tabs button', 'HTTP')
    g.ab('wait', '--fn', '!!document.querySelector(".hpfile")')
    g.click('.hpfile', 'api/no-env.HTTP', 'title')
    g.ab('wait', '--fn', 'document.querySelector(".httppanel .fpath")?.textContent === "api/no-env.HTTP"')
    g.click('button', 'Open in Files')
    g.ab('wait', '--fn', 'document.querySelector(".ftab.on")?.getAttribute("title") === "api/no-env.HTTP" && !!document.querySelector(".httpbar select.httpenv")')
    g.expect('file with zero environments offers an enabled run', g.js('document.querySelector(".httpbar select.httpenv").options.length === 1 && '
             'document.querySelector(".httpbar select.httpenv").value === "" && !document.querySelector(".httprun").disabled'), 'zero-env file still blocked')
    g.js('document.querySelector(".httprun").click()')
    g.ab('check', '.modal .httpack input')
    g.js('document.querySelector(".modal button.danger").click()')
    wait = g.ab('wait', '--fn', '!document.querySelector(".httprun").disabled && document.querySelectorAll(".httpres .httpreq-h").length === 2 && '
               'document.querySelector(".httpres .httpst")?.classList.contains("ok")', timeout=90)
    g.expect('zero-env file runs natively with configured timeouts', wait.get('success') and uploads == [BODY, BODY] and
             hits[-2:] == ['/source', '/consume'], repr({'hits': hits, 'uploads': uploads}))
    g.shot('http-no-env-file-editor')
    g.scan('HTTP zero environments in Files')

    with open(os.path.join(root, 'api/confirm.http'), 'w') as output:
        output.write('run ./confirm-leaves.http\n')
    with open(os.path.join(root, 'api/confirm-leaves.http'), 'w') as output:
        output.write('GET http://127.0.0.1:%d/preview-read\n\n### Write\nPOST http://127.0.0.1:%d/preview-write\n' %
                     (server.server_port, server.server_port))
    g.reload()
    g.palette_run(g.DEMO_TITLE)
    g.click('nav.tabs button', 'HTTP')
    g.ab('wait', '--fn', '!!document.querySelector(".hpfile")')
    g.click('.hpfile', 'api/confirm.http', 'title')
    g.ab('wait', '--fn', 'document.querySelector(".httppanel .fpath")?.textContent === "api/confirm.http" && !!document.querySelector(".httprun")')
    before = len(hits)
    g.js('document.querySelector(".httprun").click()')
    wait = g.ab('wait', '--fn', 'document.querySelectorAll(".modal .httpconfirm li").length===2')
    g.expect('production import confirmation displays actual leaf methods and URLs', wait.get('success') and
             g.js('(() => {const t=document.querySelector(".modal").textContent; return t.includes("GET http://127.0.0.1:") && '
                  't.includes("/preview-read") && t.includes("POST http://127.0.0.1:") && t.includes("/preview-write") && '
                  'document.querySelector(".modal button.danger").disabled && !!document.querySelector(".modal .httpack input");})()') and
             len(hits)==before, 'confirmation omitted imported operations or sent a request')
    g.shot('http-imported-production-confirmation')
    g.scan('HTTP imported production confirmation')
    g.click('.modal button', 'Cancel')

    # Dynamic code is native again. OS isolation blocks host commands instead.
    guard_source = '< {% eval("request.variables.set(\\\"answer\\\", 42)"); %}\nGET http://127.0.0.1:' + str(server.server_port) + '/guard\n'
    with open(os.path.join(root, 'api/guard.http'), 'w') as output:
        output.write(guard_source)
    g.reload()
    g.palette_run(g.DEMO_TITLE)
    g.click('nav.tabs button', 'HTTP')
    g.ab('wait', '--fn', '!!document.querySelector(".hpfile")')
    g.click('.hpfile', 'api/guard.http', 'title')
    g.ab('wait', '--fn', 'document.querySelector(".httppanel .fpath")?.textContent === "api/guard.http" && !!document.querySelector(".httprun")')
    before = len(hits)
    g.js('document.querySelector(".httprun").click()')
    g.js('document.querySelector(".modal button.danger").click()')
    wait = g.ab('wait', '--fn', '!document.querySelector(".httprun").disabled && document.querySelector(".httppanel .httpres-h .httpst")?.classList.contains("ok")', timeout=90)
    g.expect('native eval works in isolated runner in the real app', wait.get('success') and len(hits) == before + 1 and
             open(os.path.join(root, 'api/guard.http')).read() == guard_source, 'native eval failed or source changed')
    g.shot('http-isolated-native-eval')
    guard_source = '< {% execSync("/usr/bin/printf piper-probe"); %}\nGET http://127.0.0.1:' + str(server.server_port) + '/guard\n'
    with open(os.path.join(root, 'api/guard.http'), 'w') as output:
        output.write(guard_source)
    g.reload()
    g.palette_run(g.DEMO_TITLE)
    g.click('nav.tabs button', 'HTTP')
    g.ab('wait', '--fn', '!!document.querySelector(".hpfile")')
    g.click('.hpfile', 'api/guard.http', 'title')
    g.ab('wait', '--fn', 'document.querySelector(".httppanel .fpath")?.textContent === "api/guard.http" && !!document.querySelector(".httprun")')
    before = len(hits)
    g.js('document.querySelector(".httprun").click()')
    g.js('document.querySelector(".modal button.danger").click()')
    wait = g.ab('wait', '--fn', '!document.querySelector(".httprun").disabled && document.querySelector(".httppanel .httpres-h .httpst")?.classList.contains("bad")', timeout=90)
    g.ab('click', '.hpreq-toggle[data-req="0"]')
    g.expect('OS process rejection visible in the real app', wait.get('success') and len(hits) == before and
             open(os.path.join(root, 'api/guard.http')).read() == guard_source, 'host process was allowed or source changed')
    g.shot('http-runtime-isolation')
    g.scan('HTTP OS isolation')

    overflow = 'GET http://127.0.0.1:%d/overflow\n\n>>! overflow.pdf\n' % server.server_port
    with open(os.path.join(root, 'api/overflow.http'), 'w') as output:
        output.write(overflow)
    g.reload()
    g.palette_run(g.DEMO_TITLE)
    g.click('nav.tabs button', 'HTTP')
    g.ab('wait', '--fn', '!!document.querySelector(".hpfile")')
    g.click('.hpfile', 'api/overflow.http', 'title')
    g.ab('wait', '--fn', 'document.querySelector(".httppanel .fpath")?.textContent === "api/overflow.http" && !!document.querySelector(".httprun")')
    before = len(hits)
    g.js('document.querySelector(".httprun").click()')
    g.js('document.querySelector(".modal button.danger").click()')
    wait = g.ab('wait', '--fn', '!document.querySelector(".httprun").disabled && document.querySelector(".httperr")?.textContent.includes("64 MiB")', timeout=45)
    g.expect('app shows the binary size limit without timeout replay or publication', wait.get('success') and
             hits[before:]==['/overflow'] and not os.path.exists(os.path.join(root, 'api/overflow.pdf')) and
             open(os.path.join(root, 'api/overflow.http')).read()==overflow, 'limit error, byte safety or request count differs')
    g.shot('http-native-binary-size-limit')
    g.scan('HTTP native binary size limit', expected_errors=('HTTP 409 POST /api/http/run',))
    completion_scenario(meta, root)


g.start_serve = start_serve
g.database_search_and_chooser = scenario
try:
    result = g.main()
    summary = {'ok': result == 0, 'native_ijhttp': True, 'screens': g.SCREENS,
               'findings': g.FINDINGS, 'requests': hits}
    with open(os.path.join(g.OUT, 'http-app.json'), 'w') as output:
        json.dump(summary, output, indent=2)
    print(json.dumps(summary), flush=True)
    print('HTTP_APP_ARTIFACTS ' + g.OUT, flush=True)
    if os.environ.get('PIPER_E2E_HTTP_APP_HOLD'):
        input()
finally:
    server.shutdown()
raise SystemExit(result)
