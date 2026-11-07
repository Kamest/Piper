"""Headless first-use/workflow regressions against an isolated real HTTP server."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import _env
import common
import features
import piperd
import sessions
import settings


@unittest.skipUnless(os.environ.get("PIPER_BROWSER_TESTS") == "1" and shutil.which("agent-browser"),
                     "set PIPER_BROWSER_TESTS=1 for headless first-use/workflow checks")
class AuditFirstUseWorkflowTest(unittest.TestCase):
    def test_first_use_and_workflow_recovery_preserve_drafts(self):
        manager = _env.manager(self, background=True)
        root = _env.make_project("first-use", {"README.md": "Keep this checkout\n"})
        subprocess.run(["git", "init", "-q", root], check=True, capture_output=True)
        key = "acme/first-use"
        _env.register(key, root, obs="synthetic-observability")
        # Outside PROJECT_PATH: it becomes registered only through the actual editor.
        extra = Path(_env.ROOT, "new-checkout")
        extra.mkdir()
        (extra / "README.md").write_text("Registered from the launcher\n")
        subprocess.run(["git", "init", "-q", str(extra)], check=True, capture_output=True)
        first, other = [sessions.Session(project=key, path=root, title=title, state="waiting", runtime="direct")
                        for title in ("First session", "Other session")]
        manager.sessions = {s.id: s for s in (first, other)}
        receiver = Path(_env.ROOT, "workflow-receiver.log")
        receiver.write_text("")
        for item in (first, other):
            item.save()
            program = "import sys;print('owned receiver ready',flush=True)\nfor line in sys.stdin:\n with open(sys.argv[1],'a') as f: f.write('got:'+line)\n print('got:'+line,flush=True)"
            spawned = manager.pty.call("spawn", id=item.id, argv=[sys.executable, "-u", "-c", program, str(receiver)],
                env={"PATH": os.environ["PATH"]}, cwd=root, locked=False, cols=120, rows=35, log=os.path.join(item.dir, "pty.log"))
            self.assertTrue(spawned["ok"])
            item.pid = spawned["pid"]
        features.save(modules={"composer": True}, onboarded=True)
        settings.update("interface", {"restore_view": True})
        manager.reload_policy()
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
        browser = "piper-first-use-%s" % os.getpid()
        checks = []

        def cli(*args):
            r = subprocess.run(["agent-browser", "--session", browser, "--json", *args], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, (r.stdout + r.stderr).replace(common.own_token("gui"), "[token]"))
            data = json.loads(r.stdout)
            self.assertTrue(data["success"], "agent-browser action failed")
            return data.get("data", {}).get("result")

        def ready(code):
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if cli("eval", code) is True:
                    return
                time.sleep(.1)
            self.fail(code)

        def text_button(scope, text):
            code = "Array.from(document.querySelectorAll(%s)).find(b=>b.textContent.trim()===%s)" % (json.dumps(scope + " button"), json.dumps(text))
            ready("!!(" + code + ")")
            cli("eval", code + ".click();true")

        def shot(name):
            path = Path(os.environ["TEST_SANDBOX_WORK"], "first-use-" + name + ".png")
            cli("eval", "document.querySelectorAll('.toast .tclose').forEach(b=>b.click());true")
            cli("screenshot", str(path))
            if os.environ.get("PIPER_E2E_EXPORT_PNG") == "1":
                print("SCREENSHOT " + json.dumps({"name": path.name, "data": base64.b64encode(path.read_bytes()).decode()}), flush=True)

        with mock.patch.object(piperd, "MANAGER", manager), mock.patch.object(piperd, "PORT", server.server_port):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                cli("open", piperd.gui_url())
                cli("set", "viewport", "1280", "633")
                ready('!!document.querySelector(".beside")')
                cli("click", '#actbar [aria-label="Settings"]')
                ready('!!document.querySelector(".settings .st-side")')
                cli("click", '#actbar [aria-label="New session"]')
                ready('!!document.querySelector("#new-session-task")')
                cli("fill", "#new-session-task", "Keep this unsent task")
                cli("eval", "(()=>{const d=new DataTransfer();d.items.add(new File([Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1kAAAAASUVORK5CYII='),c=>c.charCodeAt(0))],'fixture.png',{type:'image/png'}));document.querySelector('#new-session-task').dispatchEvent(new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true}));return true;})()")
                ready('document.querySelectorAll(".wk-image img").length===1')
                cli("click", "#new-session-project")
                cli("fill", "#new-session-project-search", "not-registered-yet")
                ready('!!document.querySelector(".wk-manage-projects")')
                shot("missing-project")
                cli("click", ".wk-manage-projects")
                ready('!!document.querySelector(".wk-project-modal .st-projects")')
                cli("eval", "window.__editorPolls=0;const original=window.fetch;window.fetch=async(...args)=>{const r=await original(...args);if(String(args[0]).includes('/api/state')&&r.status===200)window.__editorPolls++;return r;};window.__restoreEditorFetch=()=>window.fetch=original;true")
                other.title = "Updated while editor is open"
                manager.bump()
                ready('window.__editorPolls>0')
                self.assertTrue(cli("eval", '!!document.querySelector(".wk-project-modal .settings .st-projects")'))
                cli("eval", "window.__restoreEditorFetch();true")
                cli("click", ".wk-project-modal .st-prm")
                cli("click", ".wk-project-modal .st-save")
                ready('document.querySelectorAll(".olayer").length===3')
                text_button(".olayer:last-child", "Cancel")
                ready('document.querySelectorAll(".olayer").length===2')
                self.assertTrue(cli("eval", '!!document.querySelector(".wk-project-modal .settings .st-projects")'))
                text_button(".wk-project-modal .st-project-removed", "Undo")
                checks.append("A01-002 editor stays in its modal through real state polling and removal-confirmation Cancel")
                shot("register-project-editor")
                cli("click", ".wk-project-back")
                ready('document.querySelectorAll(".olayer").length===1')
                ready('document.querySelector("#new-session-project-search")?.value==="not-registered-yet"')
                self.assertEqual(cli("eval", 'document.querySelector("#new-session-task").value'), "Keep this unsent task")
                checks.append("A01-002 Back restores the launcher query and task")
                cli("click", ".wk-manage-projects")
                ready('!!document.querySelector(".wk-project-modal .st-projects")')
                text_button(".wk-project-modal", "Add a project by hand")
                cli("fill", '.wk-project-modal tbody tr:first-child input[placeholder="client/repo"]', "acme/new-checkout")
                cli("fill", '.wk-project-modal tbody tr:first-child input[placeholder="/path/to/checkout"]', str(extra))
                cli("click", ".wk-project-modal .st-save")
                ready('document.querySelectorAll(".olayer").length===1')
                ready('document.querySelector("#new-session-project-name")?.textContent==="acme/new-checkout"')
                self.assertEqual(cli("eval", 'document.querySelector("#new-session-task").value'), "Keep this unsent task")
                self.assertEqual(cli("eval", 'document.querySelectorAll(".wk-image img").length'), 1)
                checks.append("A01-002 Save selects the unique newly registered project and retains task and attached image")
                ready('!document.querySelector(".where input[value=new]").disabled')
                cli("click", ".where input[value=new]")
                cli("fill", '.where input[aria-label="Worktree name"]', "keep-name")
                ready('!document.querySelector(".wnew-empty").classList.contains("hidden")')
                self.assertTrue(cli("eval", 'document.querySelector(".wk-start").disabled'))
                self.assertTrue(cli("eval", 'document.querySelector(".wnewf button").disabled'))
                self.assertIn("No commits yet", cli("eval", 'document.querySelector(".wnewf").textContent'))
                self.assertTrue(cli("eval", '(()=>{const s=document.querySelector(".wnew-empty > span").getBoundingClientRect(),b=document.querySelector(".wnew-empty > button").getBoundingClientRect();return b.left>=s.right&&b.top<s.bottom;})()'))
                shot("unborn-worktree")
                text_button(".wnew-empty", "Use main checkout")
                ready('document.querySelector(".where input[value=main]").checked && !document.querySelector(".wk-start").disabled')
                self.assertEqual(cli("eval", 'document.querySelector(".where input[aria-label=\\"Worktree name\\"]").value'), "keep-name")
                self.assertEqual(cli("eval", 'document.querySelector("#new-session-task").value'), "Keep this unsent task")
                checks.append("A01-005 unborn New is blocked, main recovery preserves task and name")
                # The existing two-step Close guard discards only this test draft.
                cli("click", ".wk-close")
                ready('document.querySelector(".olayer")?.dataset.escArmed==="1"')
                cli("click", ".wk-close")
                ready('document.querySelectorAll(".olayer").length===0')
                cli("click", '#actbar [aria-label="Settings"]')
                ready('!!document.querySelector(".settings [data-nav=projects]")')
                cli("click", ".settings [data-nav=projects]")
                ready('!!document.querySelector(".st-projects")')
                cli("eval", "Array.from(document.querySelectorAll('.st-ptable tbody tr')).find(r=>r.textContent.includes('acme/new-checkout')||r.querySelector('input')?.value==='acme/new-checkout').querySelector('.st-prm').click();true")
                ready('!!document.querySelector(".st-project-removed")')
                self.assertIn("Files on disk are not deleted", cli("eval", 'document.querySelector(".st-project-removed").textContent'))
                self.assertIn("after confirmation", cli("eval", 'document.querySelector("[data-field=projects]").textContent'))
                self.assertNotIn("takes effect immediately", cli("eval", 'document.querySelector("[data-field=projects]").textContent'))
                shot("remove-project-pending")
                text_button(".st-project-removed", "Undo")
                self.assertFalse(cli("eval", '!!document.querySelector(".st-project-removed")'))
                cli("eval", "Array.from(document.querySelectorAll('.st-ptable tbody tr')).find(r=>r.querySelector('input')?.value==='acme/new-checkout').querySelector('.st-prm').click();true")
                cli("click", 'section[data-sid=projects] .st-save')
                ready('document.querySelector(".olayer:last-child h2")?.textContent.includes("Remove 1 project")===true')
                text_button(".olayer:last-child", "Cancel")
                self.assertIn("acme/new-checkout", common.read_json(common.PROJECTS_FILE, {})["projects"])
                cli("click", 'section[data-sid=projects] .st-save')
                text_button(".olayer:last-child", "Remove")
                ready('document.querySelector("section[data-sid=projects] .st-note")?.textContent.startsWith("Saved:")===true')
                self.assertNotIn("acme/new-checkout", common.read_json(common.PROJECTS_FILE, {})["projects"])
                self.assertEqual((extra / "README.md").read_text(), "Registered from the launcher\n")
                checks.append("A01-003 removal is staged, undoable and confirmed; real registry removed, files retained")
                # Navigation has its own scroll area; search never overlaps an active item.
                cli("set", "viewport", "1440", "1000")
                cli("eval", "document.querySelector('.st-navlist').scrollTop=9999;true")
                cli("click", '.settings .st-gfold') if cli("eval", 'document.querySelector(".st-gfold")?.getAttribute("aria-expanded")==="false"') else None
                cli("eval", "document.querySelector('.st-navlist').scrollTop=9999;true")
                cli("click", '.settings [data-nav=mobile]')
                ready('!!document.querySelector("section[data-sid=mobile]")')
                shot("settings-search-navigation")
                cli("eval", "document.querySelector('.st-navlist').scrollTop=9999;true")
                cli("click", '.settings [data-nav=env]')
                ready('!!document.querySelector("section[data-sid=env]")')
                self.assertTrue(cli("eval", "(()=>{const a=document.querySelector('.st-searchbox').getBoundingClientRect(),b=document.querySelector('.st-navi.on').getBoundingClientRect();return b.top>=a.bottom;})()"))
                shot("settings-search-last-section")
                cli("set", "viewport", "390", "844")
                cli("eval", "document.querySelector('.st-navlist').scrollTop=9999;true")
                self.assertTrue(cli("eval", "(()=>{const a=document.querySelector('.st-searchbox').getBoundingClientRect(),b=document.querySelector('.st-navlist').getBoundingClientRect();return b.top>=a.bottom && b.height>0;})()"))
                shot("settings-search-mobile")
                checks.append("G01 independently scrolling navigation keeps search and Environment reachable at desktop and 390px")
                # Preserve an existing Files grid behind Single, then test the two distinct menu actions.
                cli("set", "viewport", "1024", "768")
                cli("eval", "localStorage.setItem('piper.grid',%s);localStorage.setItem('piper.layout',JSON.stringify({v:1,left:'strip',leftBack:'strip',right:'hidden',center:{kind:'single'}}));localStorage.setItem('piper.last',JSON.stringify({active:%s,view:'term'}));true" % (json.dumps(json.dumps([first.id, "files"])), json.dumps(first.id)))
                cli("reload")
                ready('!!document.querySelector(".beside")')
                cli("click", ".beside")
                ready('document.querySelector(".besidemenu")?.textContent.includes("Restore previous layout")===true')
                self.assertTrue(cli("eval", "(()=>{const bs=Array.from(document.querySelectorAll('.besidemenu button'));const a=bs.findIndex(b=>b.textContent.includes('Another session')),r=bs.findIndex(b=>b.textContent.includes('Restore previous layout')),c=bs.findIndex(b=>b.textContent.includes('Composer'));return a<r&&r<c&&bs[r].querySelector('.small').textContent==='Files · Grid 2';})()"))
                shot("beside-explicit-restore")
                text_button(".besidemenu", "Another session…")
                ready('!!document.querySelector(".addpane")')
                self.assertFalse(cli("eval", '!!document.querySelector(".gcell.filespane")'))
                cli("press", "Escape")
                checks.append("W02 Another session opens a chooser without restoring the saved Files pane")
                cli("click", ".beside")
                cli("eval", "Array.from(document.querySelectorAll('.besidemenu button')).find(b=>b.textContent.includes('Grid 2')).click();true")
                ready('!!document.querySelector(".gcell.filespane")')
                checks.append("W02 explicit Restore restores the previous Files layout")
                cli("press", "Meta+g")
                cli("set", "viewport", "1440", "1000")
                cli("click", ".beside")
                cli("eval", "Array.from(document.querySelectorAll('.besidemenu button')).find(b=>b.textContent.includes('Terminal')&&!b.textContent.includes('Composer')).click();true")
                ready('!!document.querySelector(".st-feature-link")')
                ready('document.activeElement?.getAttribute("data-fk")==="sw:host"')
                self.assertIn("Host shells is off", cli("eval", 'document.querySelector(".st-feature-link").textContent'))
                self.assertTrue(cli("eval", "(()=>{const b=document.querySelector('.st-feature-link').getBoundingClientRect();return b.top>=0&&b.bottom<=innerHeight;})()"))
                self.assertIn("Host shells", cli("eval", 'document.querySelector(".st-crumb").textContent'))
                cli("press", "Tab")
                self.assertEqual(cli("eval", 'document.activeElement.textContent'), "Back to Terminal")
                shot("host-shell-exact-feature")
                self.assertFalse(features.on("host"))
                cli("click", '[data-module=host] [role=switch]')
                cli("click", 'section[data-sid=features] .st-save')
                ready('!!document.querySelector(".olayer .st-ack input")')
                cli("click", '.olayer .st-ack input')
                text_button(".olayer:last-child", "Save anyway")
                ready('document.querySelector(".st-feature-link")?.textContent.includes("Host shells is on")===true')
                self.assertTrue(features.on("host"))
                shot("host-shell-enabled")
                text_button(".st-feature-link", "Back to Terminal")
                ready('document.querySelector(".beside")?.offsetParent!==null')
                checks.append("W03 disabled Terminal focuses Host shells and returns without opening a shell")
                cli("set", "viewport", "1440", "1000")
                cli("click", ".beside")
                cli("eval", "Array.from(document.querySelectorAll('.besidemenu button')).find(b=>b.textContent.includes('Composer')).click();true")
                ready('!document.querySelector(".composer").classList.contains("hidden")')
                cli("fill", ".composer textarea", "Composer stays available")
                for kind in ("git_commit", "db_write_test", "http_prod"):
                    card_id = "synthetic-" + kind
                    payload = {"project": key}
                    if kind == "db_write_test":
                        payload["card"] = {"type": "db_write", "cls": "test", "project": key, "connection": "owned-fixture",
                            "lead": "Synthetic DB approval", "sql": "UPDATE example SET value = 1;", "preview": "1 row", "preview_ok": True,
                            "warnings": [], "reason": "Owned layout regression"}
                    with manager.lock:
                        manager.pending = {card_id: {"id": card_id, "kind": kind, "session": other.id,
                            "title": other.title, "summary": "Synthetic approval", "payload": payload,
                            "created": time.time(), "_ev": threading.Event(), "_ok": False}}
                    manager.bump()
                    ready("document.querySelector('#main > #pending')?.dataset.ids.includes(" + json.dumps(card_id) + ")===true")
                    if kind == 'db_write_test':
                        self.assertTrue(cli('eval', '!!document.querySelector("#pending .dbpending")'))
                    self.assertTrue(cli("eval", "(()=>{const a=document.querySelector('#pending').getBoundingClientRect(),b=document.querySelector('.composer button.primary').getBoundingClientRect();return a.bottom<=b.top && b.bottom<=innerHeight && document.elementFromPoint(b.x+b.width/2,b.y+b.height/2)?.closest('button')===document.querySelector('.composer button.primary');})()"))
                    self.assertEqual(cli("eval", 'document.querySelector(".composer textarea").value'), "Composer stays available")
                shot("approval-composer-flow")
                cli("set", "viewport", "390", "844")
                self.assertTrue(cli("eval", "document.querySelector('#pending').getBoundingClientRect().bottom<=document.querySelector('.composer button.primary').getBoundingClientRect().top"))
                shot("approval-composer-mobile")
                checks.append("W01 Git, DB and HTTP approval cards remain above clickable Send with the draft kept, including 390px")
                cli("click", ".composer button.primary")
                deadline = time.monotonic() + 10
                while "Composer stays available" not in receiver.read_text() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertEqual(receiver.read_text().count("Composer stays available"), 1)
                checks.append("W01 visible Send delivers exactly once to the owned real PTY while approval waits")
                manager.pending.clear()
                print("FIRST_USE_RESULT " + json.dumps({"checks": checks}), flush=True)
            finally:
                subprocess.run(["agent-browser", "--session", browser, "close"], capture_output=True, timeout=30)
                server.shutdown()
                server.server_close()

    def test_strict_direct_recovery_uses_same_session_and_keeps_strict(self):
        manager = _env.manager(self, background=True)
        root = _env.make_project("strict-recovery", {"README.md": "Owned Strict fixture\n"})
        _env.register("acme/strict-recovery", root)
        blocked = sessions.Session(project="acme/strict-recovery", path=root, title="Strict blocked",
            state="blocked", runtime="direct", task="Keep blocked task",
            detail="Strict requires Hole with filtered networking and no Docker start option")
        blocked.save()
        manager.sessions = {blocked.id: blocked}
        settings.update("interface", {"restore_view": True})
        manager.reload_policy()
        manager.policy["security_strict"] = True
        features.save(onboarded=True)
        browser = "piper-strict-recovery-%s" % os.getpid()
        server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)

        def cli(*args):
            r = subprocess.run(["agent-browser", "--session", browser, "--json", *args], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, (r.stdout + r.stderr).replace(common.own_token("gui"), "[token]"))
            d = json.loads(r.stdout)
            self.assertTrue(d["success"])
            return d.get("data", {}).get("result")

        with mock.patch.object(piperd, "MANAGER", manager), mock.patch.object(piperd, "PORT", server.server_port):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                cli("open", piperd.gui_url())
                cli("set", "viewport", "1280", "633")
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if cli("eval", "Array.from(document.querySelectorAll('button')).some(b=>b.textContent==='Use Hole runtime')"):
                        break
                    time.sleep(.1)
                else:
                    print("STRICT_DEBUG " + json.dumps({"state": blocked.state, "runtime": blocked.runtime,
                        "detail": blocked.detail, "ui": cli("eval", "document.querySelector('#main')?.innerText.slice(0,2000)")}), flush=True)
                    self.fail("Strict recovery button was not rendered")
                self.assertNotIn("Approve and start", cli("eval", "document.querySelector('.panel.blocked').textContent"))
                self.assertEqual(cli("eval", "Array.from(document.querySelectorAll('.panel.blocked button')).map(b=>b.textContent)"), ["Security settings", "Use Hole runtime"])
                path = Path(os.environ["TEST_SANDBOX_WORK"], "first-use-strict-direct-recovery.png")
                cli("eval", "document.querySelectorAll('.toast .tclose').forEach(b=>b.click());true")
                cli("screenshot", str(path))
                if os.environ.get("PIPER_E2E_EXPORT_PNG") == "1":
                    print("SCREENSHOT " + json.dumps({"name": path.name, "data": base64.b64encode(path.read_bytes()).decode()}), flush=True)
                with mock.patch.object(manager, "_start") as start:
                    cli("click", ".panel.blocked button.primary")
                    deadline = time.monotonic() + 10
                    while not start.called and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertTrue(start.called, cli("eval", "document.querySelector('#toasts')?.innerText"))
                self.assertEqual(blocked.runtime, "hole")
                self.assertEqual(blocked.task, "Keep blocked task")
                self.assertTrue(manager.policy["security_strict"])
                self.assertEqual(list(manager.sessions), [blocked.id])
                print("STRICT_RESULT " + json.dumps({"checks": ["A01-B01 same session/task and Strict retained through actual retry route"]}), flush=True)
            finally:
                subprocess.run(["agent-browser", "--session", browser, "close"], capture_output=True, timeout=30)
                server.shutdown()
                server.server_close()
