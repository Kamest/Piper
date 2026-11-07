"""Headless communication regressions in a private piperd, provider and PTY."""
import _env
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
import urllib.parse
import urllib.request
from unittest import mock

import common
import features
import feed
import github
import gitlab
import inbox
import piperd
import sessions
from test_inbox import FakeGitLab


def run():
    case = unittest.TestCase()
    manager = _env.manager(case, background=True)
    root = os.path.realpath(_env.make_project("delivery-fixture", {"README.md": "Owned public delivery fixture\n"}))
    key = "acme/delivery-fixture"
    _env.register(key, root)
    for args in (("init", "-q", "-b", "main"), ("add", "README.md"), ("commit", "-qm", "fixture"),
                 ("remote", "add", "origin", "https://gitlab.com/acme/first.git")):
        subprocess.run(["git", "-C", root, *args], check=True, capture_output=True)
    manager.repo_top(root, record="start")
    features.save({"agent": True, "gitlab": True, "github": True}, onboarded=True)
    feed._write(feed.CONFIG_FILE, {"enabled": True})
    fake = FakeGitLab()
    for iid in (7, 8, 9):
        fake.issues.append(fake.obj("acme/first", "issue", iid, title="Owned issue %s" % iid))
    records = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            path = urllib.parse.unquote(self.path)
            note = {"id": 8000 + len(records), "body": data["body"], "path": path}
            records.append(note)
            common.write_json(os.path.join(os.environ["TEST_SANDBOX_WORK"], "delivery-provider.json"), records)
            if path.endswith("/issues/8/notes"):
                time.sleep(gitlab.TIMEOUT + 2)  # actual configured product timeout, after durable acceptance
            body = json.dumps(dict(note, html_url="https://github.com/acme/shop/issues/7#issuecomment-%s" % note["id"])).encode()
            try:
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider.daemon_threads = True
    threading.Thread(target=provider.serve_forever, daemon=True).start()

    def opener(request, timeout):
        if request.get_method() != "POST":
            path = urllib.parse.unquote(urllib.parse.urlsplit(request.full_url).path)
            if path.endswith("/issues") and "/projects/" in path:
                project = path.split("/projects/")[1][:-len("/issues")]
                title = "First repository issue" if project == "acme/first" else "Second repository issue"
                return inbox_response([fake.obj(project, "issue", 17, title=title)])
            return fake(request, timeout)
        local = "http://127.0.0.1:%d%s" % (provider.server_port, urllib.parse.urlsplit(request.full_url).path)
        return urllib.request.urlopen(urllib.request.Request(local, data=request.data, method="POST",
                                     headers={"Content-Type": "application/json"}), timeout=timeout)

    def inbox_response(data):
        import io
        return io.BytesIO(json.dumps(data).encode())

    token_patch = mock.patch.object(gitlab, "token", return_value="synthetic-fixture-token")
    token_patch.start()
    case.addCleanup(token_patch.stop)
    manager.gitlab_opener = opener
    manager._inbox_poll()
    # The GUI's picker constructs the same provider client directly.
    opener_patch = mock.patch.object(gitlab._OPENER, "open", side_effect=opener)
    opener_patch.start()
    case.addCleanup(opener_patch.stop)
    gh_patch = mock.patch.object(manager, "_ib_gh", return_value=(github.GitHub("github.com", "synthetic", opener=opener), "acme/shop"))
    gh_patch.start()
    case.addCleanup(gh_patch.stop)
    gh_key = "gh:acme/shop#issue#7"
    with manager._ib_lock:
        manager._ib_items[gh_key] = dict(manager._ib_items["acme/first#issue#7"], key=gh_key, gl="gh:acme/shop",
                                       web_url="https://github.com/acme/shop/issues/7")
    owner = sessions.Session(project=key, path=root, title="Owned PTY recipient", state="ready", runtime="hole",
                             gitlab=["acme/first"], review_pending=True)
    manager.sessions[owner.id] = owner
    owner.save()
    receiver = os.path.join(owner.dir, "receiver.log")
    Path(receiver).write_text("")   # the fault changes metadata parent permissions, not receiver storage
    program = "import sys;print('owned receiver ready',flush=True)\nfor line in sys.stdin:\n with open(sys.argv[1],'a') as f: f.write('got:'+line)\n print('got:'+line,flush=True)"
    spawned = manager.pty.call("spawn", id=owner.id, argv=[sys.executable, "-u", "-c", program, receiver],
                               env={"PATH": os.environ["PATH"]}, cwd=root, locked=False,
                               cols=120, rows=35, log=os.path.join(owner.dir, "pty.log"))
    case.assertTrue(spawned["ok"], spawned)
    owner.pid = spawned["pid"]
    # This owned PTY has no writable sandbox mount; it must not block host-origin refresh.
    owner.state = "exited"
    old_manager, old_port = piperd.MANAGER, piperd.PORT
    server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
    piperd.MANAGER, piperd.PORT = manager, server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    browser = "delivery-regression-%s" % os.getpid()
    checks, shots, browser_errors = [], [], []

    def cli(*args):
        result = subprocess.run(["agent-browser", "--session", browser, "--json", *args], capture_output=True, text=True, timeout=35)
        case.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token("gui"), "[token]"))
        data = json.loads(result.stdout)
        case.assertTrue(data["success"], data)
        return data.get("data", {}).get("result")

    def ready(code, timeout=30):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if cli("eval", code) is True:
                return
            time.sleep(.1)
        raise AssertionError("Browser condition timed out: " + code)

    def check(name, condition=True):
        case.assertTrue(condition, name)
        checks.append(name)
        print("CHECK " + name, flush=True)

    def shot(name):
        path = Path(os.environ["TEST_SANDBOX_WORK"], "delivery-" + name + ".png")
        cli("screenshot", str(path))
        shots.append(name)
        if os.environ.get("PIPER_E2E_EXPORT_PNG") == "1":
            print("SCREENSHOT " + json.dumps({"name": path.name, "data": base64.b64encode(path.read_bytes()).decode()}), flush=True)

    def reload_browser():
        browser_errors.extend(cli("eval", "window.deliveryErrors??[]") or [])
        cli("reload")
        ready('!!document.querySelector(\'#actbar [aria-label="New session"]\')')
        cli("eval", 'window.deliveryErrors=[];window.deliveryToasts=[];window.addEventListener("error",e=>deliveryErrors.push(e.message));window.addEventListener("unhandledrejection",e=>deliveryErrors.push(String(e.reason)));true')

    def comment(target, text, source=None):
        # Only the modal mounting/toast adapter is owned by the fixture; the actual
        # comment component, API module, production HTTP handler and manager execute.
        cli("eval", """(async()=>{const {openComment}=await import('/piper/dist/comment.js');
          const {h}=await import('/piper/dist/dom.js');
          window.closeDelivery=()=>{document.querySelector('#overlay').replaceChildren();document.querySelector('#overlay').classList.remove('open');};
          openComment({modal:(title,...nodes)=>{const host=document.querySelector('#overlay');host.replaceChildren(h('div',{class:'olayer'},h('div',{class:'modal',role:'dialog','aria-modal':'true','aria-label':title},h('h2',{},title),...nodes)));host.classList.add('open');},closeModal:closeDelivery,toast:t=>{window.deliveryToasts.push(t);}},
          {key:%s,target:'Owned public issue',webUrl:%s,text:%s,source:%s,todos:[]});return true;})()""" %
          (json.dumps(target), json.dumps("https://github.com/acme/shop/issues/7" if target.startswith("gh:") else "https://gitlab.com/acme/first/-/issues/" + target.split("#")[-1]), json.dumps(text), json.dumps(source)))
        ready('!!document.querySelector(".cdlg textarea")')

    def send_comment():
        cli("click", ".cfoot button.primary")

    try:
        cli("open", piperd.gui_url())
        cli("set", "viewport", "1440", "1000")
        ready('!!document.querySelector(\'#actbar [aria-label="New session"]\')')
        cli("eval", 'window.deliveryErrors=[];window.deliveryToasts=[];window.addEventListener("error",e=>deliveryErrors.push(e.message));window.addEventListener("unhandledrejection",e=>deliveryErrors.push(String(e.reason)));true')
        dev = Path(root, "development")
        dev.mkdir(exist_ok=True)
        draft = dev / "gitlab-7-comment.md"
        owner.state = "waiting"
        owner.pid = None
        owner.issue = {"key": "acme/first#issue#7", "gl": "acme/first", "type": "issue", "iid": 7, "title": "Owned issue", "web_url": "https://gitlab.com/acme/first/-/issues/7"}
        draft.write_text("opened public V1")
        opened = manager.session_draft(owner.id)
        comment(opened["key"], opened["text"], {"path": opened["path"], "mtime": opened["mtime"], "digest": opened["digest"]})
        cli("fill", ".cdlg textarea", "reviewer edited public V1")
        draft.write_text("newer public unsent V2")
        send_comment()
        ready('document.querySelector(".cdone")?.textContent.includes("The comment is in GitLab")===true')
        check("A11-002 provider received only opened V1", records[-1]["body"] == "reviewer edited public V1")
        check("A11-002 newer file remains unsent", manager.session_draft(owner.id)["sent"] is False)
        cli("eval", "closeDelivery();true")
        comment(opened["key"], manager.session_draft(owner.id)["text"])
        check("A11-002 V2 is offered again", cli("eval", 'document.querySelector(".cdlg textarea").value') == "newer public unsent V2")
        shot("newer-draft-remains-unsent")
        cli("eval", "closeDelivery();true")
        comment(gh_key, "one public GitHub note")
        check("A11-001 target label is GitHub", cli("eval", 'document.querySelector(".cfoot button.primary").textContent.includes("Send to GitHub")') is True)
        send_comment()
        ready('document.querySelector(".cdone")?.textContent.includes("The comment is in GitHub")===true')
        check("A11-001 actual POST uses GitHub endpoint", "/repos/acme/shop/issues/7/comments" in records[-1]["path"])
        check("A11-001 success link and toast are GitHub", cli("eval", 'document.querySelector(".cdone a").textContent.includes("GitHub")&&deliveryToasts.at(-1).includes("GitHub")') is True)
        shot("github-confirmed-target")
        cli("eval", "closeDelivery();true")
        cli("set", "viewport", "1024", "768")   # the Claude board's original viewport
        comment("acme/first#issue#8", "one public note with delayed acknowledgement")
        before = len(records)
        send_comment()
        ready('document.querySelector(".cstatus")?.textContent.includes("Delivery could not be confirmed")===true', gitlab.TIMEOUT + 12)
        check("A11-003 actual timeout follows one durable POST", len(records) == before + 1)
        check("A11-003 status never asserts Not sent", cli("eval", '!document.querySelector(".cstatus").textContent.includes("Not sent")') is True)
        check("A11-003 text stays editable", cli("eval", 'document.querySelector(".cdlg textarea").value') == "one public note with delayed acknowledgement")
        send_comment()
        ready('document.querySelector(".cfoot button.primary")?.textContent.includes("Check delivery")===true')
        check("A11-003 receipt check makes no second POST", len(records) == before + 1)
        shot("unknown-delivery-receipt-check")
        reload_browser()
        ready('!!document.querySelector(\'#actbar [aria-label="New session"]\')')
        cli("eval", 'window.deliveryToasts=[];true')
        comment("acme/first#issue#8", "one public note with delayed acknowledgement")
        check("A11-003 reload preserves pending intent", cli("eval", 'document.querySelector(".cfoot button.primary").textContent.includes("Check delivery")') is True)
        send_comment()
        ready('document.querySelector(".cstatus")?.textContent.includes("Delivery could not be confirmed")===true')
        check("A11-003 restored receipt check makes no POST", len(records) == before + 1)
        cli("eval", "closeDelivery();true")
        comment("acme/first#issue#9", "one public note with lost local reply")
        cli("eval", """(()=>{const original=window.fetch.bind(window);let drop=true;window.fetch=async(...args)=>{const r=await original(...args);if(drop&&String(args[0]).includes('/api/inbox/comment')){drop=false;await r.clone().text();throw new TypeError('owned fixture dropped completed reply');}return r;};return true;})()""")
        before = len(records)
        send_comment()
        ready('document.querySelector(".cfoot button.primary")?.textContent.includes("Check delivery")===true')
        send_comment()
        ready('document.querySelector(".cdone")?.textContent.includes("The comment is in GitLab")===true')
        check("A11-003 lost local reply recovers confirmed receipt without duplicate", len(records) == before + 1)
        shot("confirmed-receipt-after-lost-reply")
        cli("eval", "closeDelivery();true")
        cli("set", "viewport", "1440", "1000")
        # The actual full-main Feed drawer, with a real filesystem write fault.
        cli("press", "Meta+0")
        ready('!!document.querySelector("#drawer:not(.hidden) .feed-input")')
        cli("fill", "#drawer .feed-input", "one durable public queue intent")
        mode = os.stat(feed.FEED_DIR).st_mode & 0o777
        try:
            os.chmod(feed.FEED_DIR, 0o500)
            cli("click", "#drawer .feed-send")
            ready('Array.from(document.querySelectorAll(".toast")).some(n=>n.textContent.includes("Not sent"))')
            check("A16-F01 refused write publishes no queue message", not manager._feed_chat)
        finally:
            os.chmod(feed.FEED_DIR, mode)
        cli("click", "#drawer .feed-send")
        ready('document.querySelector("#drawer .feed-input").value===""')
        check("A16-F01 one retry creates exactly one durable message", len(common.read_json(feed.CHAT_FILE, [])) == 1)
        cli("eval", "document.querySelectorAll('.toast .tclose').forEach(b=>b.click());true")
        shot("one-durable-queue-message")
        cli("press", "Meta+0")
        # Exercise the real PTY and HTTP route. The real Composer module is
        # mounted through a fixture dependency adapter, without replacing send().
        owner.state, owner.pid = "ready", spawned["pid"]
        owner.review_pending = True
        owner.save()
        cli("eval", """(async()=>{const {createComposer}=await import('/piper/dist/composer.js');const {api}=await import('/piper/dist/api.js');
          const c=createComposer({current:()=>%s,title:()=> 'Owned PTY recipient',others:()=>[],sendTo:async(id,text)=>{await api.send(id,text);return true;},confirm:()=>{},toast:()=>{},busy:()=>false,live:()=>true});
          document.querySelector('#main').append(c.el);window.deliveryComposer=c;c.replyTo(%s);c.el.id='delivery-composer';return true;})()""" % (json.dumps(owner.id), json.dumps(owner.id)))
        cli("fill", "#delivery-composer textarea", "one confirmed public PTY send")
        mode = os.stat(owner.dir).st_mode & 0o777
        try:
            os.chmod(owner.dir, 0o500)
            cli("click", "#delivery-composer button.primary")
            ready('document.querySelector("#delivery-composer textarea").value===""')
            until = time.monotonic() + 5
            while time.monotonic() < until and not Path(receiver).exists():
                time.sleep(.05)
            check("A16-F03 confirmed HTTP success clears Composer", bool(owner.storage_error))
            check("A16-F03 actual PTY receives one input despite metadata fault", Path(receiver).read_text().count("got:") == 1)
            ready('document.body.innerText.includes("History not saved")')
            check("A16-F03 existing history warning is visible in main", cli("eval", 'document.body.innerText.includes("Check disk space and permissions")') is True)
            shot("confirmed-pty-send-storage-warning")
        finally:
            os.chmod(owner.dir, mode)
        owner.storage_retry = 0
        owner.save_activity()
        owner.state, owner.pid = "exited", None
        owner.issue = None
        cli("eval", 'document.querySelector("#delivery-composer").remove();true')
        # Original origin-change scenario, using the actual main launcher and picker.
        for project, title in (("acme/first", "First repository issue"), ("acme/second", "Second repository issue"), ("acme/first", "First repository issue")):
            subprocess.run(["git", "-C", root, "remote", "set-url", "origin", "https://gitlab.com/" + project + ".git"], check=True)
            reload_browser()
            ready('!!document.querySelector(\'#actbar [aria-label="New session"]\')')
            cli("click", '#actbar [aria-label="New session"]')
            ready('!!document.querySelector("#new-session-project")')
            cli("click", "#new-session-project")
            ready('!!document.querySelector(\'.wk-project-pop:not(.hidden) [data-project="%s"]\')' % key)
            cli("click", '.wk-project-pop:not(.hidden) [data-project="%s"]' % key)
            ready('!!document.querySelector(".wk-issuelink")')
            cli("click", ".wk-issuelink")
            ready('Array.from(document.querySelectorAll(".wk-issue-row")).some(n=>n.textContent.includes(%s))' % json.dumps(title))
            check("A01-B06 picker after reload uses " + project)
            if project == "acme/second":
                shot("origin-refreshed-issue-picker")
        check("no unhandled browser errors", not browser_errors and cli("eval", '(window.deliveryErrors??[]).length===0') is True)
        return {"checks": checks, "screenshots": shots, "provider_posts": len(records), "error": None}
    finally:
        try:
            cli("close")
        finally:
            server.shutdown()
            server.server_close()
            provider.shutdown()
            provider.server_close()
            piperd.MANAGER, piperd.PORT = old_manager, old_port
            case.doCleanups()


if __name__ == "__main__":
    print("DELIVERY_BROWSER_RESULT " + json.dumps(run()), flush=True)
